"""通过 CDP 驱动真实 Chrome 访问国网 412 站点，验证瑞数挑战能否通过。

用法：
    python scripts/cdp_probe.py http://www.bj.sgcc.com.cn/
"""
from __future__ import annotations

import json
import sys
import time

import httpx
import websocket  # type: ignore

import os
CDP = os.environ.get("CDP_URL", "http://127.0.0.1:9222")


def http_json(path: str):
    r = httpx.get(CDP + path, timeout=10, trust_env=False)
    r.raise_for_status()
    return r.json()


class CDPSession:
    def __init__(self, ws_url: str):
        # 新版 Chrome 默认校验 WebSocket 的 Origin 头，Python 直连需抑制该头
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self._id = 0

    def send(self, method: str, params: dict | None = None, wait: bool = True, timeout: float = 30):
        self._id += 1
        msg_id = self._id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        if not wait:
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = self.ws.recv()
            data = json.loads(raw)
            if data.get("id") == msg_id:
                return data
        raise TimeoutError(f"{method} 超时")

    def close(self):
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://www.bj.sgcc.com.cn/"
    # 新建页面
    target = http_json("/json/new?" + url) if False else None
    # /json/new 需要 PUT（新版 Chrome）
    r = httpx.put(CDP + "/json/new?" + url, timeout=10, trust_env=False)
    target = r.json()
    ws_url = target["webSocketDebuggerUrl"]
    tid = target["id"]

    sess = CDPSession(ws_url)
    sess.send("Page.enable")
    sess.send("Runtime.enable")
    print(f"[*] 打开 {url}")

    # 轮询页面就绪状态
    for i in range(20):
        time.sleep(1.5)
        res = sess.send(
            "Runtime.evaluate",
            {"expression": "document.readyState + '|' + document.title + '|' + document.body.innerText.length", "returnByValue": True},
        )
        val = res.get("result", {}).get("result", {}).get("value", "?")
        print(f"  [{i}] {val}")
        if isinstance(val, str):
            parts = val.split("|")
            if len(parts) == 3 and parts[0] == "complete" and int(parts[2] or 0) > 200:
                print("[+] 页面已渲染出内容")
                break

    # 读取 cookie 和 DOM
    res = sess.send("Runtime.evaluate", {"expression": "document.documentElement.outerHTML.length", "returnByValue": True})
    print("HTML 长度:", res.get("result", {}).get("result", {}).get("value"))
    res = sess.send("Runtime.evaluate", {"expression": "document.body.innerText.slice(0, 400)", "returnByValue": True})
    print("正文预览:\n", res.get("result", {}).get("result", {}).get("value"))
    res = sess.send("Network.getAllCookies")
    cookies = res.get("result", {}).get("cookies", [])
    for c in cookies:
        print("cookie:", c["name"], "len=", len(str(c.get("value", ""))), "domain=", c["domain"])

    sess.close()
    httpx.get(CDP + f"/json/close/{tid}", timeout=5, trust_env=False)


if __name__ == "__main__":
    main()
