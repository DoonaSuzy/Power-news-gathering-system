"""本地采集接收端：把浏览器里抓到的数据落盘。

为什么需要它
------------
浏览器内 ``fetch`` 能拿到**渲染后的真实 DOM**（Scrapy 静态抓取拿不到的），
但抓到的数据在页面内存里，取出来要经过工具返回 —— 数据一多就要分批转录，
既慢又容易出错。

这个服务让浏览器**直接把结果 POST 过来写文件**：一次请求落盘，零转录。
打通后所有站点的浏览器采集都能复用同一条链路。

仅监听 ``127.0.0.1``，不对外暴露。

用法::

    python scripts/collect_sink.py --port 8899 --out-dir data/collected

浏览器侧::

    await fetch('http://127.0.0.1:8899/save/china5e', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(items),
    })
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

#: 路径名安全化：只允许字母数字与 -_，防止 ../ 穿越
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def build_handler(out_dir: pathlib.Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _cors(self) -> None:
            # 浏览器页面在别的 origin 上，必须放行 CORS 才能 POST 过来
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")

        def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            name = unquote(self.path.strip("/").split("/")[-1] or "data")
            if not _SAFE_NAME.match(name):
                self.send_response(400)
                self._cors()
                self.end_headers()
                self.wfile.write(b'{"ok":false,"error":"bad name"}')
                return
            try:
                payload = json.loads(raw or b"null")
            except ValueError as exc:
                self.send_response(400)
                self._cors()
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(exc)}).encode())
                return

            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{name}.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            count = len(payload) if isinstance(payload, list) else 1
            print(f"[sink] {name}: {count} 条 → {path}", flush=True)

            body = json.dumps({"ok": True, "saved": count, "path": str(path)}).encode()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            """静音默认访问日志（只在成功落盘时打印一行）。"""

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description="浏览器采集落盘接收端")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--out-dir", default="data/collected")
    args = ap.parse_args()

    root = pathlib.Path(__file__).resolve().parents[1]
    out_dir = (root / args.out_dir) if not pathlib.Path(args.out_dir).is_absolute() \
        else pathlib.Path(args.out_dir)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), build_handler(out_dir))
    print(f"[sink] 监听 http://127.0.0.1:{args.port}  输出目录 {out_dir}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
