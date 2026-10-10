"""瑞数动态防护 · CDP 深度诊断脚本。

驱动真实 Chrome 打开目标站，订阅 Network / Runtime / Log 事件，
输出：指纹检测结果、网络请求状态序列、控制台报错、最终 DOM 与 Cookie。

用法：
    python scripts/cdp_river.py http://www.bj.sgcc.com.cn/ [等待秒数]
"""
from __future__ import annotations

import json
import sys
import threading
import time

import httpx
import websocket  # type: ignore

import os
CDP = os.environ.get("CDP_URL", "http://127.0.0.1:9222")


class Client:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        self._id = 0
        self.events: list[dict] = []
        self.responses: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._stop = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        while not self._stop:
            try:
                raw = self.ws.recv()
            except Exception:  # noqa: BLE001
                break
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            with self._lock:
                if "id" in data:
                    # CDP 响应（可能带 result 或 error），按 id 缓存
                    self.responses[data["id"]] = data
                if "method" in data:
                    self.events.append(data)

    def send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + 30
        while time.time() < deadline:
            with self._lock:
                if mid in self.responses:
                    return self.responses.pop(mid)
            time.sleep(0.02)
        return {}

    def eval(self, expr: str):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True})
        return r.get("result", {}).get("result", {}).get("value")

    def drain(self) -> list[dict]:
        with self._lock:
            evs = list(self.events)
            self.events.clear()
        return evs

    def close(self) -> None:
        self._stop = True
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://www.bj.sgcc.com.cn/"
    wait = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    r = httpx.put(CDP + "/json/new?" + url, timeout=10, trust_env=False)
    target = r.json()
    tid = target["id"]
    cli = Client(target["webSocketDebuggerUrl"])

    for dom in ("Page", "Network", "Runtime", "Log"):
        cli.send(f"{dom}.enable")
    cli.drain()

    print(f"[*] 目标: {url}")
    print(f"[*] 等待 {wait}s 观察挑战过程 ...\n")

    t0 = time.time()
    while time.time() - t0 < wait:
        time.sleep(2)
        state = cli.eval("document.readyState + '|' + document.body.innerText.length")
        print(f"  +{int(time.time()-t0):>2}s  readyState|textLen = {state}")

    # ---- 指纹 ----
    print("\n=== 浏览器指纹 ===")
    fp = cli.eval(
        "JSON.stringify({"
        "wd: navigator.webdriver,"
        "ua: navigator.userAgent,"
        "plugins: navigator.plugins.length,"
        "langs: (navigator.languages||[]).join(','),"
        "hardwareConcurrency: navigator.hardwareConcurrency,"
        "deviceMemory: navigator.deviceMemory,"
        "hasChrome: !!window.chrome,"
        "webglVendor: (()=>{try{const c=document.createElement('canvas');"
        "const g=c.getContext('webgl');"
        "return g?g.getParameter(g.VENDOR):'no-webgl';}catch(e){return 'err';}})()"
        "})"
    )
    print(fp)

    # ---- 网络请求 ----
    print("\n=== 网络请求（最近 40 条）===")
    evs = cli.drain()
    reqs = {}
    for ev in evs:
        m = ev.get("method", "")
        p = ev.get("params", {})
        if m == "Network.requestWillBeSent":
            reqs[p["requestId"]] = {"url": p["request"]["url"], "method": p["request"]["method"]}
        elif m == "Network.responseReceived":
            rid = p["requestId"]
            if rid in reqs:
                reqs[rid]["status"] = p["response"]["status"]
                reqs[rid]["mime"] = p["response"].get("mimeType", "")
    for rid, info in list(reqs.items())[:40]:
        print(f"  [{info.get('status','?')}] {info['method']:4} {info['url'][:90]}")

    # ---- 控制台 ----
    print("\n=== 控制台 / 异常 ===")
    for ev in evs:
        m = ev.get("method", "")
        p = ev.get("params", {})
        if m == "Runtime.consoleAPICalled":
            args = [str(a.get("value", a.get("description", "")))[:100] for a in p.get("args", [])]
            print(f"  console.{p.get('type')}: {' '.join(args)}")
        elif m == "Runtime.exceptionThrown":
            print(f"  EXC: {str(p.get('exceptionDetails', {}).get('text'))[:140]}")
        elif m == "Log.entryAdded":
            print(f"  log[{p.get('entry', {}).get('level')}]: {str(p.get('entry', {}).get('text'))[:140]}")

    # ---- 最终 DOM / Cookie ----
    html_len = cli.eval("document.documentElement.outerHTML.length")
    print(f"\n=== 最终 DOM ===")
    print(f"  outerHTML 长度: {html_len}")
    print(f"  body.innerText(前200): {str(cli.eval('document.body.innerText.slice(0,200)'))[:200]}")
    print(f"  title: {cli.eval('document.title')}")

    print("\n=== Cookie ===")
    ck = cli.send("Network.getAllCookies")
    for c in ck.get("result", {}).get("cookies", []):
        print(f"  {c['name']:<20} len={len(str(c.get('value',''))):<5} domain={c['domain']}")

    cli.close()
    httpx.get(CDP + f"/json/close/{tid}", timeout=5, trust_env=False)


if __name__ == "__main__":
    main()
