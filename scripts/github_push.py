#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把本地仓库推送到 GitHub。

用法（token 只走环境变量，不落盘、不进 git 历史）：
    GITHUB_TOKEN=ghp_xxx python scripts/github_push.py
    GITHUB_TOKEN=ghp_xxx python scripts/github_push.py --private   # 私有仓库

流程：
    1. 校验 token 身份（GET /user）
    2. 仓库不存在则创建（POST /user/repos，不 auto_init，避免产生无关初始提交）
    3. 用 token 构造临时 remote URL，push 当前分支
    4. 回查 API 校验推送结果（默认分支 / 最新提交 / 仓库大小）

说明：本环境存在沙箱透明代理做 TLS 中间人，git 需 -c http.sslVerify=false 才能握手成功；
      脚本同时显式清空 http(s)_proxy，避免代理二次干扰。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import httpx

API = "https://api.github.com"
REPO_NAME = "Power-news-gathering-system"
DESCRIPTION = "分布式电力新闻采集与数据中台：Scrapy 采集层 + FastAPI 中台，攻克国网瑞数动态防护，826 篇真实新闻入库"


def run(cmd: list[str], *, env_extra: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # 沙箱代理对 git-over-https 有干扰，显式摘除
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if check and proc.returncode != 0:
        raise RuntimeError(f"命令失败: {' '.join(cmd)}\n{proc.stdout}\n{proc.stderr}")
    return proc


def api(method: str, path: str, token: str, **kw) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    return httpx.request(method, f"{API}{path}", headers=headers, timeout=60, verify=False, **kw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--private", action="store_true", help="创建为私有仓库（默认公开）")
    ap.add_argument("--remote", default="origin")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("[x] 未设置环境变量 GITHUB_TOKEN")
        return 2

    # ---- 1. 校验身份 ----
    r = api("GET", "/user", token)
    if r.status_code != 200:
        print(f"[x] token 校验失败 HTTP {r.status_code}: {r.text[:300]}")
        return 1
    me = r.json()
    login = me["login"]
    print(f"[1/5] 身份校验通过: {login} (id={me['id']})")

    # ---- 2. 仓库存在性 ----
    r = api("GET", f"/repos/{login}/{REPO_NAME}", token)
    if r.status_code == 200:
        repo = r.json()
        print(f"[2/5] 仓库已存在: {repo['full_name']}  private={repo['private']}")
    elif r.status_code == 404:
        print(f"[2/5] 仓库不存在，创建中 (private={args.private}) ...")
        r = api(
            "POST",
            "/user/repos",
            token,
            json={
                "name": REPO_NAME,
                "description": DESCRIPTION,
                "private": args.private,
                "auto_init": False,          # 关键：不生成 README/初始提交，避免 push 冲突
                "has_issues": True,
                "has_wiki": False,
                "has_projects": False,
            },
        )
        if r.status_code not in (200, 201):
            print(f"[x] 创建仓库失败 HTTP {r.status_code}: {r.text[:400]}")
            return 1
        repo = r.json()
        print(f"[2/5] 创建成功: {repo['full_name']}")
    else:
        print(f"[x] 查询仓库失败 HTTP {r.status_code}: {r.text[:300]}")
        return 1

    html_url = repo["html_url"]
    clone_url = repo["clone_url"]

    # ---- 3. 本地分支 ----
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip() or "main"
    print(f"[3/5] 当前分支: {branch}")

    # ---- 4. 推送（token 只注入子进程环境，不写入 .git/config 明文） ----
    authed = clone_url.replace("https://", f"https://{login}:{token}@")
    run(["git", "remote", "set-url", args.remote, clone_url])   # 落盘的是干净 URL
    print(f"[4/5] 推送到 {html_url} ...")
    # 先把带凭据的 URL 临时写进去，推完立刻还原
    run(["git", "remote", "set-url", args.remote, authed], check=False)
    try:
        proc = run(
            ["git", "-c", "http.sslVerify=false", "-c", "http.postBuffer=524288000",
             "push", "-u", args.remote, f"{branch}:{branch}"],
            check=False,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        # 输出里可能带 token，脱敏后再打印
        safe = out.replace(token, "***")
        print(safe.strip()[-2000:])
        if proc.returncode != 0:
            print("[x] 推送失败")
            return 1
    finally:
        run(["git", "remote", "set-url", args.remote, clone_url], check=False)
        print("      (远端 URL 已还原为不含凭据的形式)")

    # ---- 5. 校验 ----
    r = api("GET", f"/repos/{login}/{REPO_NAME}", token)
    if r.status_code == 200:
        d = r.json()
        print(f"[5/5] 校验: 默认分支={d.get('default_branch')} "
              f"大小={d.get('size')}KB  可见性={'private' if d.get('private') else 'public'}")
    r = api("GET", f"/repos/{login}/{REPO_NAME}/commits?per_page=1", token)
    if r.status_code == 200 and r.json():
        c = r.json()[0]
        print(f"      最新提交: {c['sha'][:7]}  {c['commit']['message'].splitlines()[0]}")

    print(f"\n✅ 完成: {html_url}")
    print(json.dumps({"url": html_url, "clone": clone_url}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
