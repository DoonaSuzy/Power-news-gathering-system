"""浏览器采集通道客户端 —— 连通 84 上的 CDP 服务拿渲染后 HTML。

## 为什么需要这个模块

国网 13 个站点用**瑞数（RiverSecurity）动态防护**：响应 `412`，
页面是 2~6KB 混淆 JS，要求浏览器**执行 JS 挑战、本地算指纹、回传换 Cookie**。
`httpx`/`Scrapy` 无法执行 JS，永远停在 412。

破解办法（见 `docs/各省份攻克日志/README.md` §1.3）：CDP 驱动真实 Chrome，
**浏览器只负责取 HTML，解析仍交回既有适配器**（零改动复用）。

## 架构：为什么浏览器单独一个容器

见 `docker-compose.yml` 的 `browser` 服务注释。要点：
主镜像被 4 个服务共用，装 Chrome 会让每个容器 +400MB；
且浏览器崩溃不该影响采集主进程。

## 两个必须由客户端做的事

`playwright run-server` **无法传浏览器启动参数**，所以反爬规避要在客户端做：

| 特征 | 不处理的后果 | 本模块的处理 |
| --- | --- | --- |
| `navigator.webdriver === true` | **瑞数秒识别**（最典型特征） | `add_init_script` 覆写为 `undefined` |
| UA 含 `HeadlessChrome` / `X11; Linux` | 被识别为自动化环境 | `new_context(user_agent=...)` 覆盖成真实 Windows Chrome UA |

## 用法

```python
from scripts.browser_channel import BrowserChannel

async with BrowserChannel("ws://192.168.101.84:3010/") as ch:
    html = await ch.fetch("https://www.bj.sgcc.com.cn/")
```

或命令行：

```bash
python scripts/browser_channel.py https://www.bj.sgcc.com.cn/ --out page.html
```

## 依赖

```bash
uv add playwright        # 或用 pip
python -m playwright install --dry-run   # 不需要装浏览器：我们连远程
```

**注意**：客户端只装 `playwright` **库**（约几 MB），**不要**跑
`playwright install`（那会下载 ~400MB 浏览器，而浏览器在远端容器里已有）。

## 限制

**本通道无法绕过「IP 段限制」**。实测：
- 84 连国网多数站点 **TCP 直接拒绝**（0/0，0.13s 快速失败）
  → 这种站点浏览器也连不上，**必须在能连通的网络里跑**
- 国网子页面另有**路径级防护**（只放行首页，其余 `ERR_EMPTY_RESPONSE`）
  → 属站点策略，代码层无解

**本模块解决的是「能连上但被 JS 挑战挡住」这一类**（如北京的 412）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

__all__ = ["BrowserChannel", "DEFAULT_UA", "STEALTH_INIT_SCRIPT", "fetch_html"]

#: 真实 Windows Chrome UA。
#: **必须覆盖** —— Playwright 默认 UA 带 `HeadlessChrome`，
#: 瑞数这类防护第一眼就认出来。
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)

#: 反自动化检测注入脚本。
#:
#: 为什么必须做：Playwright 启动的浏览器 `navigator.webdriver === true`，
#: 这是**最典型**的自动化特征，瑞数直接据此拒绝。
#: 实测未处理时 `WEBDRIVER=true`（84 上验证过）。
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
// 补上 headless 下常缺的插件列表（部分风控会查）
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
// window.chrome 在 headless 下可能缺失
if (!window.chrome) {
  window.chrome = { runtime: {} };
}
"""


class BrowserChannel:
    """到远端 CDP 服务的采集通道（异步上下文管理器）。

    Args:
        endpoint: CDP WebSocket 端点，如 ``ws://192.168.101.84:3010/``。
        user_agent: 覆盖 UA；默认 :data:`DEFAULT_UA`。
        timeout_ms: 单页加载超时（毫秒）。

    Example:
        >>> async with BrowserChannel("ws://127.0.0.1:3010/") as ch:
        ...     html = await ch.fetch("https://example.com/")
    """

    def __init__(
        self,
        endpoint: str,
        *,
        user_agent: str = DEFAULT_UA,
        timeout_ms: int = 45_000,
    ) -> None:
        self.endpoint = endpoint
        self.user_agent = user_agent
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None

    async def __aenter__(self) -> BrowserChannel:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "缺少 playwright 客户端库。装它（**只装库，不要装浏览器**）：\n"
                "    uv add playwright   # 或 pip install playwright\n"
                "浏览器在远端容器里已有，不需要 `playwright install`。"
            ) from exc

        self._pw = await async_playwright().start()
        # connect（而非 launch）：浏览器跑在远端容器，本地不下载任何浏览器
        self._browser = await self._pw.chromium.connect(self.endpoint)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()

    async def fetch(self, url: str, *, wait_until: str = "domcontentloaded") -> str:
        """打开页面并返回**渲染后**的 HTML。

        Args:
            url: 目标地址。
            wait_until: 等待条件（``domcontentloaded`` / ``load`` / ``networkidle``）。
                对瑞数站点用 ``domcontentloaded`` 即可 —— 挑战脚本在更早阶段执行。

        Returns:
            渲染后的完整 HTML（``document.documentElement.outerHTML``）。

        Raises:
            RuntimeError: 未通过上下文管理器进入，或页面加载失败。
        """
        if self._browser is None:
            raise RuntimeError("请用 `async with BrowserChannel(...) as ch:` 使用")

        ctx = await self._browser.new_context(
            user_agent=self.user_agent,          # ← 覆盖 HeadlessChrome UA
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        # 每个 context 都要注入：规避 navigator.webdriver
        await ctx.add_init_script(STEALTH_INIT_SCRIPT)
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until=wait_until, timeout=self.timeout_ms)
            # 瑞数挑战完成后 DOM 体积会显著变大，等它稳定再取
            await _wait_dom_stable(page, self.timeout_ms)
            return await page.evaluate(
                "() => document.documentElement.outerHTML"
            )
        finally:
            await page.close()
            await ctx.close()


async def _wait_dom_stable(page, timeout_ms: int, *, rounds: int = 3) -> None:
    """等 DOM 体积连续 ``rounds`` 次不变（瑞数挑战完成的判据）。

    为什么不等固定时长：挑战耗时随网络波动，固定 sleep 要么白等、要么不够。
    体积稳定说明渲染结束。
    """
    last = -1
    stable = 0
    step = 400
    for _ in range(max(1, timeout_ms // step)):
        await asyncio.sleep(step / 1000)
        size = await page.evaluate("() => document.documentElement.outerHTML.length")
        if size == last:
            stable += 1
            if stable >= rounds:
                return
        else:
            stable = 0
            last = size


def fetch_html(endpoint: str, url: str, *, timeout_ms: int = 45_000) -> str:
    """同步封装（内部起事件循环），方便脚本里直接调用。"""
    async def _run() -> str:
        async with BrowserChannel(endpoint, timeout_ms=timeout_ms) as ch:
            return await ch.fetch(url)

    return asyncio.run(_run())


def main() -> int:
    ap = argparse.ArgumentParser(description="浏览器通道：取渲染后 HTML")
    ap.add_argument("url", help="目标 URL")
    ap.add_argument("--endpoint", default="ws://192.168.101.84:3010/",
                    help="CDP 端点（默认 84 上的 browser 容器）")
    ap.add_argument("--out", default="", help="输出文件；省略则打印统计")
    ap.add_argument("--timeout-ms", type=int, default=45_000)
    args = ap.parse_args()

    html = fetch_html(args.endpoint, args.url, timeout_ms=args.timeout_ms)
    if args.out:
        Path(args.out).write_text(html, encoding="utf-8")
        print(f"已保存 {len(html)} 字节 -> {args.out}")
    else:
        print(f"HTML 长度: {len(html)}")
        print(f"前 300 字: {html[:300]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
