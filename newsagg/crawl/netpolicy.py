"""网络访问策略：协议降级 + 代理清理 + 阻断识别。

## 为什么需要这个模块

`docs/省份攻克日志/README.md` §1.3 记录了国网采集的**三件套**，
但在产品代码里一直没有对应实现 —— 结果就是 27 个国网来源集体失败：

| 现象 | 根因 | 本项目实测 |
| --- | --- | --- |
| `https://www.tj.sgcc.com.cn/p1/` ConnectError | **沙箱代理劫持 TLS** | 环境变量 `http_proxy=http://127.0.0.1:35xxx` |
| 换 `http://` 后返回 200 | **国网站群 HTTPS 被阻断、HTTP(80) 可通** | 200 / 58,901 字节 |
| `200` 但「无候选」 | **路径随站群改版失效** | 江苏声明 `/p1/` 返回 404，改用 `/xwzx/` 后入库 40 篇 |

三件套 = **清代理变量 + `trust_env=False` + 协议降级**。
本模块把它们落成可单测的纯函数，Scrapy 侧只做接线。

## 三条策略各自的边界

1. **代理清理** —— 只清 `http_proxy`/`https_proxy`（含大小写），
   **不动 `no_proxy`**：内网直连依赖它，清了反而会走代理。
2. **协议降级** —— 只对 **`https → http`** 单向降级。
   反向（http → https）不自动做：站点若只支持 http，试 https 纯属浪费一次请求，
   且 http 能通说明链路正常。需要反向时由 adapter 显式声明。
3. **阻断识别** —— `412`（瑞数）/`403`（WAF）/`202`（风控）语义不同，
   **不能一律当「失败重试」**：
   - `412` 重试无用（要浏览器执行 JS 挑战），应立即标记
   - `202` 是排队/风控，加 Referer 或降频可能就过
   - `403` 多为路径级拒绝，**换路径常可直接解决**（江西实测）
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "PROXY_ENV_KEYS",
    "BLOCK_KINDS",
    "protocol_fallbacks",
    "strip_proxy_env",
    "classify_block",
    "is_protocol_downgrade",
]

#: 会被清理的代理环境变量（大小写都清 —— 不同工具读不同的键）
PROXY_ENV_KEYS = (
    "http_proxy", "https_proxy", "all_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
)

#: 阻断状态码 → 处置建议。键是 HTTP 状态码，值是 (种类, 是否值得原样重试)
BLOCK_KINDS: dict[int, tuple[str, bool]] = {
    412: ("river_security", False),  # 瑞数：必须浏览器执行 JS，原样重试无用
    403: ("path_or_waf", True),      # 可能只是路径错，换路径/换协议值得一试
    405: ("method_block", False),
    429: ("rate_limit", True),       # 限速：降频或等待后可重试
    202: ("risk_queue", True),       # 风控排队：加 Referer 或降频可解
}


def strip_proxy_env(env: dict[str, str] | None = None) -> list[str]:
    """清掉进程环境里的代理变量（就地修改 ``os.environ``）。

    Args:
        env: 待清理的映射；``None`` 表示直接改 ``os.environ``。

    Returns:
        实际被删掉的键名列表（便于日志与单测）。

    Note:
        **刻意不动 `no_proxy`/`NO_PROXY`** —— 内网地址直连依赖它，
        清掉会让内网请求也走代理，反而把问题扩大。
    """
    target = os.environ if env is None else env
    removed: list[str] = []
    for key in PROXY_ENV_KEYS:
        if key in target:
            target.pop(key, None)
            removed.append(key)
    return removed


def protocol_fallbacks(url: str) -> list[str]:
    """生成协议互备候选，**原协议在前**。

    Args:
        url: 原始 URL。

    Returns:
        候选列表。只有 ``https`` 会追加 ``http`` 版本；
        ``http`` 开头或非 http(s) 的 URL 原样返回单元素列表。

    Example:
        >>> protocol_fallbacks("https://a.cn/p1/")
        ['https://a.cn/p1/', 'http://a.cn/p1/']
        >>> protocol_fallbacks("http://a.cn/p1/")
        ['http://a.cn/p1/']
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        return [url]
    return [url, urlunsplit(parts._replace(scheme="http"))]


def is_protocol_downgrade(original: str, candidate: str) -> bool:
    """判断 candidate 是否为 original 的「同址降级」版本。

    用途：决定是否允许这次重试 —— 只允许**同主机同路径**的协议切换，
    避免把重定向目标误判成降级候选而跳出站点范围。
    """
    a, b = urlsplit(original), urlsplit(candidate)
    return (
        a.netloc == b.netloc
        and a.path == b.path
        and a.query == b.query
        and a.scheme == "https"
        and b.scheme == "http"
    )


def classify_block(status: int) -> tuple[str, bool] | None:
    """识别阻断类状态码。

    Args:
        status: HTTP 状态码。

    Returns:
        ``(种类, 是否值得原样重试)``；非阻断码返回 ``None``。

    Note:
        调用方应据第二项决定策略：``False`` 表示**原样重试没意义**
        （典型是 412 瑞数），应直接标记为「需浏览器」而不是白白重试。
    """
    return BLOCK_KINDS.get(status)
