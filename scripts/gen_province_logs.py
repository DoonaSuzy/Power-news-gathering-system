"""生成「按省份独立归档」的攻克过程日志。

数据来源（真实可追溯）：
  - data/power_news/_crawl_report.json  抓取结果（found/saved/ok/error）
  - data/power_news/_probe_report.json  实时探活（HTTP/HTTPS 状态码）

输出：
  docs/省份攻克日志/README.md           总览 + 共用背景
  docs/省份攻克日志/<省份>.md           每省一份独立日志

用法：
    python scripts/gen_province_logs.py
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRAWL_REPORT = ROOT / "data" / "power_news" / "_crawl_report.json"
CDP_REPORT = ROOT / "data" / "power_news" / "_cdp_report.json"
PROBE_REPORT = ROOT / "data" / "power_news" / "_probe_report.json"
OUT_DIR = ROOT / "docs" / "省份攻克日志"

# ---------------------------------------------------------------------------
# 省份 → 来源分组 + 时间线叙事（人工整理，依据真实操作历史）
# ---------------------------------------------------------------------------
# 每个省份条目字段：
#   name       省份全称（= 输出文件名）
#   region     行政区划代码
#   group      分组（国网/南网/独立/港澳台/行业/政府）
#   status     攻克状态
#   timeline   事件列表，每项 dict：
#                phase 阶段，symptom 现象/卡点，actions 排查动作(含指令)，
#                result 结论/验证，lesson 可复用经验
PROVINCES: list[dict] = [
    # ==================== 全国 / 行业 ====================
    {
        "name": "全国_行业",
        "region": "",
        "group": "行业媒体",
        "status": "✅ 已攻克",
        "timeline": [
            {
                "phase": "阶段二 · 首轮探活",
                "symptom": "3 个全国性行业媒体（北极星/中国电力新闻网/国际能源网）中，"
                           "北极星与中国电力新闻网可正常抓取，国际能源网（in_en）返回 0 条候选。",
                "actions": [
                    "`curl -I https://www.in-en.com/` 确认 200 可达，排除网络问题；",
                    "抓取首页 HTML，检索 `<a href>` 中形如 `/article/html/energy-*.shtml` 的链接；",
                    "`grep -o '/article/html/[a-z-]*-[0-9]*\\.s?html'` 分析真实文章 URL 形态；",
                    "核对适配器 in_en.py 的 `article_pattern` 与 `list_url` 配置。",
                ],
                "result": "定位根因：列表入口取的是首页，且 `article_pattern` 未覆盖 "
                          "`/article/html/energy-XXX.shtml` 形态，导致 0 条。",
                "lesson": "门户型站点应先定位「新闻列表页」而非首页；文章 URL 形态必须实测，不能凭经验假设。",
            },
            {
                "phase": "阶段四 · 适配修正",
                "symptom": "改用 `/news/` 入口后能发现链接，但标题被解析成站点名。",
                "actions": [
                    "把 in_en 的 `list_url` 修正为 `https://www.in-en.com/news/`；",
                    "`article_pattern` 修正为 `/article/html/[a-z]+-\\d+\\.s?html?$`；",
                    "覆写 `parse_detail()`：优先 `title_selectors`，再回退 `_title_from_head()`，"
                    "并新增 `_looks_like_site_name()` 过滤站点名作为标题。",
                ],
                "result": "in_en 由 0 → 发现 78 条、入库 40 条；三大行业媒体合计 120 篇。",
                "lesson": "详情页 `<h1>` 常是站点名而非文章标题，需显式过滤；标题提取要有「首选选择器 + 兜底 + 黑名单」三层。",
            },
        ],
    },
    # ==================== 国网 · 已攻克 ====================
    {
        "name": "天津市",
        "region": "120000",
        "group": "国家电网省级公司",
        "status": "✅ 已攻克",
        "timeline": [
            {
                "phase": "阶段二 · 首轮探活",
                "symptom": "HTTPS 请求 `https://www.tj.sgcc.com.cn/p1/` 报 ConnectError，"
                           "DNS 解析落到 198.18.x 保留网段，判定为「网络不可达」。",
                "actions": [
                    "`nslookup www.tj.sgcc.com.cn` 观察解析结果；",
                    "双协议探测：分别请求 `https://` 与 `http://`；",
                    "发现环境变量 `http_proxy=http://127.0.0.1:35004`（沙箱代理）劫持 TLS 握手。",
                ],
                "result": "确认 HTTPS 被阻断、仅 HTTP(80) 可达；此为网络环境问题，非代码缺陷。",
                "lesson": "国网省级站群在本网络下 HTTPS 不可用，必须准备 HTTP 降级路径。",
            },
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "用户关闭代理后重试，HTTPS 仍 ConnectError，但 HTTP 返回 200。",
                "actions": [
                    "在 `crawl_and_archive.py` 顶部 `os.environ.pop('http_proxy'/'https_proxy'/...)`；",
                    "httpx 客户端统一 `trust_env=False`，绕过系统代理；",
                    "实现 `candidate_list_urls()`：对同一路径自动 http/https 互备。",
                ],
                "result": "HTTP 探测 200/58901 字节；列表页 found 49、入库 40 篇。",
                "lesson": "抓取国网必须三件套：清代理变量 + trust_env=False + 协议降级。",
            },
        ],
    },
    {
        "name": "河北省",
        "region": "130000",
        "group": "国家电网省级公司（含冀北）",
        "status": "⚠️ 部分攻克（河北✅ / 冀北❌）",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "河北（`sgcc_he`）HTTP 200 正常；冀北（`sgcc_jb`）HTTPS/HTTP 均 ConnectError。",
                "actions": [
                    "双协议探测 `www.he.sgcc.com.cn` 与 `www.jb.sgcc.com.cn`；",
                    "河北 HTTP 返回 200/53633 字节，冀北两协议均连接失败。",
                ],
                "result": "河北 found 50、入库 40 篇；冀北不可达，暂无法采集。",
                "lesson": "河北与冀北行政代码同为 130000，但是两个独立采集主体，不可互相替代。",
            },
        ],
    },
    {
        "name": "辽宁省",
        "region": "210000",
        "group": "国家电网省级公司",
        "status": "✅ 已攻克",
        "timeline": [
            {
                "phase": "阶段二 · 首轮探活",
                "symptom": "`https://www.ln.sgcc.com.cn/p1/` 返回 404，来源清单指定路径失效。",
                "actions": [
                    "`curl -I https://www.ln.sgcc.com.cn/p1/` → 404；",
                    "改用根路径与 `/xwzx/` 探测；",
                    "HTTP `www.ln.sgcc.com.cn/` 返回 200/32616 字节。",
                ],
                "result": "根路径命中，found 36、入库 36 篇。",
                "lesson": "来源清单里的栏目路径会随站群改版失效，需内置多路径备选（/xwzx/、/p1/、/）。",
            },
        ],
    },
    {
        "name": "湖南省",
        "region": "430000",
        "group": "国家电网省级公司",
        "status": "✅ 已攻克",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "湖南与多数国网站一样，HTTPS 不可达；HTTP 待验证。",
                "actions": [
                    "HTTP 探测 `www.hn.sgcc.com.cn/p1/` → 200/38431 字节；",
                    "列表解析用 SGCC_ARTICLE_HINT 正则匹配 `content_XXX.html` 形态。",
                ],
                "result": "found 37、入库 37 篇。",
                "lesson": "旧版 `/p1/` 站群文章形态为 `content_XXXX.html`，正则需覆盖。",
            },
        ],
    },
    {
        "name": "江苏省",
        "region": "320000",
        "group": "国家电网省级公司",
        "status": "✅ 已攻克（关键突破）",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "来源清单路径 `https://www.js.sgcc.com.cn/p1/` 在本网络下不可用；"
                           "改 HTTP 后 `/p1/` 仍 404（新版站群已迁移）。",
                "actions": [
                    "双协议探测：HTTP `/p1/` → 404/555；",
                    "改探测 `/xwzx/`（新版站群新闻中心入口）；",
                    "抓取 `/xwzx/` 页面，发现文章 URL 形如 `/xwzx/gsyw/2026/14112.shtml`；",
                    "扩展 `SGCC_ARTICLE_HINT` 正则，新增分支 `/[a-z]+\\d*/\\d{4}/\\d{4,}\\.s?html?$`。",
                ],
                "result": "**这是国网批量攻克的关键突破**：同一正则改动随后让河北(50)、湖南(46) 等"
                          "新版站群一并命中；江苏自身 found 40、入库 40 篇。",
                "lesson": "站群存在「新版 /xwzx/ + /{年}/{数字}.shtml」与「旧版 /p1/ + content_XXX.html」"
                          "两套并存，正则必须同时覆盖；一次正则扩展可批量解锁多个省份。",
            },
        ],
    },
    {
        "name": "江西省",
        "region": "360000",
        "group": "国家电网省级公司",
        "status": "✅ 已攻克",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "来源清单路径 `https://www.jx.sgcc.com.cn/p1/` HTTP 探测返回 403（拒绝）。",
                "actions": [
                    "`curl -I http://www.jx.sgcc.com.cn/p1/` → 403/250；",
                    "改探测站点根路径 `http://www.jx.sgcc.com.cn/`；",
                    "根路径返回 200，列表候选解析成功。",
                ],
                "result": "found 59、入库 40 篇。",
                "lesson": "403/404 是「路径级」拒绝，换路径常可直接解决，不必先怀疑封 IP。",
            },
        ],
    },
    {
        "name": "重庆市",
        "region": "500000",
        "group": "国家电网省级公司",
        "status": "✅ 已攻克",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "HTTPS 不可达，与多数国网站一致。",
                "actions": [
                    "HTTP 探测 `www.cq.sgcc.com.cn/p1/` → 200/45071 字节；",
                    "走 SGCC_ARTICLE_HINT 正则解析列表。",
                ],
                "result": "found 59、入库 40 篇。",
                "lesson": "重庆采用旧版 /p1/ 站群，HTTP 直达即可稳定抓取。",
            },
        ],
    },
    {
        "name": "四川省",
        "region": "510000",
        "group": "国家电网省级公司",
        "status": "✅ 已攻克",
        "timeline": [
            {
                "phase": "阶段二 · 首轮探活",
                "symptom": "四川是首轮唯一可抓的国网站；但栏目路径为特殊的 `/html/main/col81/`，"
                           "与其余省份的 `/p1/` 不同。",
                "actions": [
                    "确认 `https://www.sc.sgcc.com.cn/html/main/col81/` 可访问；",
                    "适配 `col81` 型栏目；",
                    "注意：探活时该源响应体含 `$_ts` 字样被误判为瑞数，实际可正常解析。",
                ],
                "result": "found 59、入库 24 篇。",
                "lesson": "同为「国网站群」也可能存在独立 CMS 分支（colXX 型），需单独适配。",
            },
        ],
    },
    # ==================== 国网 · 未攻克（412 瑞数）====================
    {
        "name": "北京市",
        "region": "110000",
        "group": "国家电网省级公司",
        "status": "❌ 未攻克（瑞数防护）",
        "timeline": [
            {
                "phase": "阶段二 · 首轮探活",
                "symptom": "HTTPS 报 ConnectError，一度被判定为「网络不可达」。",
                "actions": ["`nslookup` + 双协议探测"],
                "result": "关代理后确认：HTTP 可达，但返回 **412**。",
                "lesson": "「不可达」与「被反爬」要分开验证，不能混为一谈。",
            },
            {
                "phase": "阶段七 · 412 攻坚（进行中）",
                "symptom": "HTTP 返回 412 + 5597 字节响应体，`<script r='m'>` 内定义 `$_ts` 全局变量，"
                           "为**瑞数信息（RiverSecurity）动态防护**：服务端返回混淆 JS，"
                           "需浏览器执行后回传 `PW9ydXnjjO8XS` 等 Cookie 才能换取真实页面。",
                "actions": [
                    "抓取 412 响应体，确认含 `$_ts=window['$_ts']` 与 `r='m'` 标记；",
                    "用 `chrome-devtools` MCP 打开 `http://www.bj.sgcc.com.cn/`；",
                    "检查页面：Cookie `PW9ydXnjjO8XT`（271 字符）成功种上，但页面稳定在 39 字节空白；",
                    "执行 JS 读取指纹：`navigator.webdriver=true`、plugins=5、langs=zh-CN,zh。",
                ],
                "result": "判定挑战未通过。怀疑 `navigator.webdriver=true` 被瑞数识别为自动化环境。",
                "lesson": "瑞数防护需真实浏览器完成 JS 挑战并回传 Cookie；纯 requests/httpx 无法伪造。",
            },
            {
                "phase": "阶段七 · 启动 CDP 真实浏览器",
                "symptom": "需要绕过 `navigator.webdriver` 检测，且不许用 MCP 默认自动化指纹。",
                "actions": [
                    "启动独立实例：`chrome.exe --remote-debugging-port=9222 "
                    "--user-data-dir=<tmp> --headless=new --disable-blink-features=AutomationControlled`；",
                    "验证 CDP：`GET http://127.0.0.1:9222/json/version` → 200（Chrome/154）；",
                    "编写 `scripts/cdp_probe.py`，通过 WebSocket 驱动页面并轮询 DOM/Cookie。",
                ],
                "result": "CDP 通道打通，待完成瑞数挑战验证（见任务 #8）。",
                "lesson": "`--disable-blink-features=AutomationControlled` 可去掉 webdriver 标志，"
                          "是绕过瑞数自动化检测的关键开关。",
            },
        ],
    },
    {
        "name": "山西省",
        "region": "140000",
        "group": "国家电网省级公司",
        "status": "❌ 未攻克（列表无候选）",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "站点并非被反爬：HTTP 探测 `www.sx.sgcc.com.cn` 返回 200/50299 字节，"
                           "但适配器解析出「无候选」——正文页拿到了，列表链接却没匹配上。",
                "actions": [
                    "抓取 HTTP 首页 HTML；",
                    "检查 `SGCC_ARTICLE_HINT` 正则是否覆盖本页真实链接形态。",
                ],
                "result": "归类为 C 类「路径/正则不准」，非网络或反爬问题，待补正则分支。",
                "lesson": "「200 但无候选」= 解析规则问题，可通过打印页面链接与正则比对快速定位。",
            },
        ],
    },
    {
        "name": "内蒙古自治区",
        "region": "150000",
        "group": "独立电网 + 国网蒙东",
        "status": "⚠️ 部分攻克（impc✅ / 蒙东❌）",
        "timeline": [
            {
                "phase": "阶段二 · 首轮探活",
                "symptom": "内蒙古电力集团（impc）与国网蒙东（sgcc_md）是两套独立主体，"
                           "必须同时覆盖才能采集全内蒙古。",
                "actions": [
                    "确认 impc 独立于国网运营，覆盖呼和浩特/包头等中西部；",
                    "sgcc_md 覆盖呼伦贝尔/兴安/通辽/赤峰（东部）。",
                ],
                "result": "impc 成功入库 23 篇；sgcc_md HTTP 返回 412（瑞数），未攻克。",
                "lesson": "内蒙古的电力新闻必须 impc + 蒙东双源并行，缺一不可。",
            },
        ],
    },
    {
        "name": "吉林省",
        "region": "220000",
        "group": "国家电网省级公司",
        "status": "❌ 未攻克（瑞数防护）",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "HTTP `www.jl.sgcc.com.cn` 返回 412/5682 字节（瑞数挑战页）。",
                "actions": ["抓 412 响应确认含 `$_ts` 瑞数特征"],
                "result": "归入瑞数防护批次，与北京/浙江/安徽等一并处理。",
                "lesson": "412 站点可批量识别：响应体含 `$_ts` 即为瑞数，统一走浏览器方案。",
            },
        ],
    },
    {
        "name": "黑龙江省",
        "region": "230000",
        "group": "国家电网省级公司",
        "status": "❌ 未攻克（瑞数防护）",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "HTTP 返回 412/5673 字节。",
                "actions": ["归类瑞数批次"],
                "result": "未攻克，待浏览器方案。",
                "lesson": "同批次处理，避免逐站重复试错。",
            },
        ],
    },
    {
        "name": "上海市",
        "region": "310000",
        "group": "国家电网省级公司",
        "status": "❌ 未攻克（202 风控）",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "HTTP 返回 **202**（非 412），响应体 2334 字节。",
                "actions": ["抓响应体，确认非瑞数页，属排队/风控拦截"],
                "result": "归类为 202 风控，与河南同型。",
                "lesson": "202 ≠ 412。202 多为请求排队或风控，可尝试加 Referer/Cookie 或降低频率。",
            },
        ],
    },
    {
        "name": "浙江省",
        "region": "330000",
        "group": "国家电网省级公司",
        "status": "❌ 未攻克（瑞数防护）",
        "timeline": [
            {
                "phase": "阶段二 · 首轮探活",
                "symptom": "浙江是来源清单中唯一指定非 `/p1/` 路径的源"
                           "（`/p1/spxw.html`，视频新闻栏目），首轮即返回 412。",
                "actions": ["双协议探测：HTTPS 与 HTTP 均 412/2960"],
                "result": "未攻克，归瑞数批次。",
                "lesson": "「唯一的特殊路径来源」往往也是最先暴露反爬的来源。",
            },
        ],
    },
    {
        "name": "安徽省",
        "region": "340000",
        "group": "国家电网省级公司",
        "status": "❌ 未攻克（瑞数防护）",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "HTTP 返回 412/5645 字节。",
                "actions": ["归类瑞数批次"],
                "result": "未攻克。",
                "lesson": "—",
            },
        ],
    },
    {
        "name": "福建省",
        "region": "350000",
        "group": "国家电网省级公司",
        "status": "❌ 未攻克（瑞数防护）",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "HTTP 返回 412/5768 字节。",
                "actions": ["归类瑞数批次"],
                "result": "未攻克。",
                "lesson": "—",
            },
        ],
    },
    {
        "name": "山东省",
        "region": "370000",
        "group": "国家电网省级公司 + 政府能源局",
        "status": "⚠️ 部分攻克（能源局✅ / 国网❌）",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "国网山东（sgcc_sd）HTTP 返回 412（瑞数）；"
                           "替代源山东省能源局（gov_sd_nyj）可抓。",
                "actions": [
                    "确认 gov_sd_nyj 栏目页 `/col/col59966/index.html` 为 JS 渲染，首轮采集为空；",
                    "改从 HTTP 根路径与站群通用列表结构解析。",
                ],
                "result": "山东省能源局入库 23 篇，保证山东省仍有真实数据落地。",
                "lesson": "当电网官网被反爬时，政府能源主管部门是同省级别的**权威替代源**。",
            },
        ],
    },
    {
        "name": "河南省",
        "region": "410000",
        "group": "国家电网省级公司",
        "status": "❌ 未攻克（202 风控）",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "HTTP 返回 202/2409 字节，与上海同型。",
                "actions": ["归类 202 风控批次"],
                "result": "未攻克。",
                "lesson": "同批次处理。",
            },
        ],
    },
    {
        "name": "湖北省",
        "region": "420000",
        "group": "国家电网省级公司 + 政府能源局",
        "status": "❌ 未攻克（瑞数 + 政府源不可达）",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "国网湖北（sgcc_hb）HTTP 返回 412/2401；"
                           "替代源湖北省能源局（gov_hb_nyj）HTTPS/HTTP 均 ConnectError。",
                "actions": [
                    "双协议探测 gov_hb_nyj（nyj.hubei.gov.cn）；",
                    "确认域名当前不可达，非反爬。",
                ],
                "result": "两条路径均未攻克，湖北省暂无数据。",
                "lesson": "替代源也需先探活，政府域名偶有不可达情况。",
            },
        ],
    },
    {
        "name": "西藏自治区",
        "region": "540000",
        "group": "国家电网省级公司",
        "status": "❌ 未攻克（路径 404）",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "HTTP `/p1/` 返回 404/555。",
                "actions": ["多路径备选探测仍未命中候选"],
                "result": "未攻克，属 C 类路径问题。",
                "lesson": "404 且无候选时，需人工核实站点实际栏目结构。",
            },
        ],
    },
    {
        "name": "陕西省",
        "region": "610000",
        "group": "国家电网省级公司",
        "status": "❌ 未攻克（SPA 外壳）",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "HTTP 返回 200，但响应体仅 1166 字节（SPA 外壳，需 JS 渲染）。",
                "actions": ["检查 200 响应体，确认是前端框架空壳"],
                "result": "静态抓取无候选，需浏览器渲染。",
                "lesson": "200 但 <1.5KB 通常是 SPA 空壳，应触发浏览器方案而非继续改正则。",
            },
        ],
    },
    {
        "name": "甘肃省",
        "region": "620000",
        "group": "国家电网省级公司",
        "status": "❌ 未攻克（瑞数防护）",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "HTTP 返回 412/5657。",
                "actions": ["归类瑞数批次"],
                "result": "未攻克。",
                "lesson": "—",
            },
        ],
    },
    {
        "name": "青海省",
        "region": "630000",
        "group": "国家电网省级公司",
        "status": "❌ 未攻克（瑞数防护）",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "HTTP 返回 412/5709。",
                "actions": ["归类瑞数批次"],
                "result": "未攻克。",
                "lesson": "—",
            },
        ],
    },
    {
        "name": "宁夏回族自治区",
        "region": "640000",
        "group": "国家电网省级公司",
        "status": "❌ 未攻克（瑞数防护）",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "HTTP 返回 412/2415。",
                "actions": ["归类瑞数批次"],
                "result": "未攻克。",
                "lesson": "—",
            },
        ],
    },
    {
        "name": "新疆维吾尔自治区",
        "region": "650000",
        "group": "国家电网省级公司",
        "status": "❌ 未攻克（瑞数防护）",
        "timeline": [
            {
                "phase": "阶段六 · 关代理后重试",
                "symptom": "HTTP 返回 412/5645。",
                "actions": ["归类瑞数批次"],
                "result": "未攻克。",
                "lesson": "—",
            },
        ],
    },
    # ==================== 南方电网五省 ====================
    {
        "name": "广东省",
        "region": "440000",
        "group": "南方电网省级公司",
        "status": "⚠️ 部分攻克（总部站兜底）",
        "timeline": [
            {
                "phase": "阶段二 · 首轮探活",
                "symptom": "广东电网官网 `www.gd.csg.cn` 返回 1166 字节 JS 空壳"
                           "（`app.ea86c567.js`），是 **SPA + 瑞数**；"
                           "HTTPS 超时、仅暴露 `/api/v1/sessions` 接口。",
                "actions": [
                    "抓取 SPA 壳，提取入口 JS 文件名确认前端渲染；",
                    "枚举南网 5 省站（gd/gx/gz/hn/yn.csg.cn），发现结构完全一致；",
                    "找到唯一可静态抓的入口：**南网总部站 `www.csg.cn/xwzx/qygg/`**。",
                ],
                "result": "判定 5 省官网不可静态抓取，改用总部站作统一兜底。",
                "lesson": "省级子站是 SPA 时，其上级集团总部站往往是同源 CMS 且可静态抓取，是低成本兜底路径。",
            },
            {
                "phase": "阶段四 · 适配器设计",
                "symptom": "总部站是企业公告，混有各省内容，若 5 省适配器都抓会把同一篇重复入库 5 次。",
                "actions": [
                    "新增 `newsagg/adapters/csg_generic.py`；",
                    "定义 `PROVINCE_KEYWORDS`（广东电网/广州供电局/深圳供电局/广东 → 440000 等）；",
                    "实现 `accepts_title()`：按省份关键词分流，命中本省或不属于任何南网省份才收录；",
                    "`CSG_ARTICLE_HINT` 匹配 `/tYYYYMMDD_ID.shtml` 形态。",
                ],
                "result": "广东电网入库 2 篇（总部站广东相关条目）。",
                "lesson": "多省共用同一兜底源时，必须做「省份归属分流」，否则重复入库。",
            },
        ],
    },
    {
        "name": "广西壮族自治区",
        "region": "450000",
        "group": "南方电网省级公司 + 政府发改委",
        "status": "⚠️ 部分攻克（政府源抖动）",
        "timeline": [
            {
                "phase": "阶段二 · 首轮探活",
                "symptom": "广西电网 `www.gx.csg.cn` SPA 外壳，静态无候选。",
                "actions": ["复用 csg_generic 总部站兜底 + 省份关键词分流"],
                "result": "总部站未命中广西条目；改接政府替代源。",
                "lesson": "总部站内容不一定覆盖全部 5 省，需政府源补位。",
            },
            {
                "phase": "阶段六 · 政府源接入",
                "symptom": "广西发改委（gov_gx_fgw）时好时坏（ConnectError 与 200 交替）。",
                "actions": ["多次重试探测 `http://fgw.gxzf.gov.cn/xwzx/fgdt/`"],
                "result": "归类为网络波动，非代码问题；待稳定时补抓。",
                "lesson": "政府源网络稳定性参差，采集应可重试且允许部分失败。",
            },
        ],
    },
    {
        "name": "海南省",
        "region": "460000",
        "group": "南方电网省级公司",
        "status": "❌ 未攻克（SPA）",
        "timeline": [
            {
                "phase": "阶段二 · 首轮探活",
                "symptom": "海南电网 `www.hn.csg.cn` SPA 外壳；总部站兜底未命中海南条目。",
                "actions": ["复用 csg_generic + 省份关键词；暂无海南政府替代源"],
                "result": "海南省暂无数据。",
                "lesson": "SPA 省份若总部站无覆盖、又无政府替代源，则需浏览器方案补齐。",
            },
        ],
    },
    {
        "name": "贵州省",
        "region": "520000",
        "group": "南方电网省级公司 + 政府能源局",
        "status": "✅ 已攻克（政府源为主）",
        "timeline": [
            {
                "phase": "阶段二 · 首轮探活",
                "symptom": "贵州电网 SPA 外壳，静态抓取失败。",
                "actions": [
                    "接入政府替代源贵州省能源局 `http://nyj.guizhou.gov.cn/xwzx/gzdt/`；",
                    "确认其为政府站群 CMS（与国网同源结构）。",
                ],
                "result": "贵州省能源局入库 26 篇；贵州电网总部站条目 1 篇；合计 27 篇，是本轮南网省份中数据最完整的。",
                "lesson": "「电网官网 SPA → 政府能源局替代」是本项目最高效的破局套路。",
            },
        ],
    },
    {
        "name": "云南省",
        "region": "530000",
        "group": "南方电网省级公司 + 政府能源局",
        "status": "✅ 已攻克（政府源为主）",
        "timeline": [
            {
                "phase": "阶段二 · 首轮探活",
                "symptom": "云南电网 HTTPS 连接超时；HTTP 为 SPA 外壳。",
                "actions": [
                    "接入政府替代源云南省能源局 `http://nyj.yn.gov.cn/xwzx/`；",
                    "同步走 csg_generic 总部站兜底。",
                ],
                "result": "云南省能源局入库 15 篇 + 云南电网 1 篇 = 16 篇。",
                "lesson": "水电大省能源局新闻权威性高，可作为电网官网的正式替代来源。",
            },
        ],
    },
    # ==================== 港澳台 ====================
    {
        "name": "香港特别行政区",
        "region": "810000",
        "group": "港澳台地区",
        "status": "❌ 未攻克（403 拒绝）",
        "timeline": [
            {
                "phase": "阶段二 · 首轮探活",
                "symptom": "中华电力（`www.clp.com.hk`）HTTPS/HTTP 均返回 403/370 字节。",
                "actions": ["双协议探测确认 403，非网络不可达"],
                "result": "未攻克，疑似区域/WAF 拒绝。",
                "lesson": "港澳台站点反爬策略与内地不同，403 多为 WAF 直接拦截，需浏览器或本地化网络。",
            },
        ],
    },
    {
        "name": "澳门特别行政区",
        "region": "820000",
        "group": "港澳台地区",
        "status": "❌ 未攻克（可达但解析失败）",
        "timeline": [
            {
                "phase": "阶段二 · 首轮探活",
                "symptom": "澳电（`www.cem-macau.com`）首轮报「列表页失败: RuntimeError」。",
                "actions": ["复探：HTTPS/HTTP 现均返回 200/476928 字节（站点可达）"],
                "result": "站点可达但列表解析失败，属 SPA/结构差异，待补适配。",
                "lesson": "「RuntimeError」需区分是解析异常还是网络异常；站点可达是继续优化的前提。",
            },
        ],
    },
    {
        "name": "台湾省",
        "region": "710000",
        "group": "港澳台地区",
        "status": "❌ 未攻克（不可达）",
        "timeline": [
            {
                "phase": "阶段二 · 首轮探活",
                "symptom": "台电（`www.taipower.com.tw`）HTTPS/HTTP 均 ConnectError。",
                "actions": ["双协议探测"],
                "result": "当前网络下不可达。",
                "lesson": "港澳台源受网络环境影响较大，需在可访问网络下重试。",
            },
        ],
    },
]


# ---------------------------------------------------------------------------
# 共用背景（写入 README）
# ---------------------------------------------------------------------------
SHARED_BACKGROUND = """\
## 一、共用背景（所有省份通用，先读这一节）

### 1. 运行环境
- 平台：Windows，隔离虚拟环境 `C:/Users/10911/.workbuddy/binaries/python/envs/powernews`
- 抓取栈：`httpx`（直连）+ `parsel`（解析）→ 列表页 → 详情页两步式
- 归档脚本：`scripts/crawl_and_archive.py`

### 2. 三类通用阻碍（A/B/C）
| 类型 | 表现 | 根因 | 处置 |
|---|---|---|---|
| **A 网络不可达** | ConnectError / DNS 落到 198.18.x | 本机网络策略、HTTPS 被阻断、沙箱代理劫持 | HTTP(80) 降级 + 清理代理变量 |
| **B 反爬/动态渲染** | 412（瑞数）、202（风控）、1166B SPA 空壳、403 | 瑞数动态防护、SPA 前端渲染、WAF | 真实浏览器（CDP）/ 换政府替代源 |
| **C 路径/规则不准** | 200 但「无候选」、404 | 站群改版、正则未覆盖 | 多路径备选 + 扩展 `SGCC_ARTICLE_HINT` |

### 3. 必须记住的四个坑（血泪经验）
1. **沙箱代理劫持 TLS**：环境变量 `http_proxy=http://127.0.0.1:35004` 会让国网 HTTPS 失败。
   解法：脚本顶部 `os.environ.pop('http_proxy'/'https_proxy'/...)` + httpx `trust_env=False`。
2. **国网站群 HTTPS 被阻断，HTTP(80) 可通**：所有国网来源都要准备协议降级。
3. **站群 CMS 有两套**：
   - 新版：`/xwzx/` 入口 + `/{年}/{数字}.shtml`（如 `/xwzx/gsyw/2026/14112.shtml`）
   - 旧版：`/p1/` 入口 + `content_XXXX.html`
   正则 `SGCC_ARTICLE_HINT` 必须同时覆盖，否则大批省份「无候选」。
4. **南网 / SPA 的处理范式**：省级子站是 SPA 时，优先找上级集团总部站
   （`www.csg.cn/xwzx/qygg/`）作兜底；再用 **政府能源主管部门**（能源局/发改委）
   作为权威替代源，保证每省都有数据落地。

### 4. 攻关时间线（全局）
| 阶段 | 关键动作 | 结果 |
|---|---|---|
| 阶段一 | 项目盘点 + 搭建隔离 venv | 依赖就绪（pytest 与 scrapy 冲突，跳过） |
| 阶段二 | 首轮探活 39 来源 | 仅 4 个可抓；识别 A/B/C 三类阻碍 |
| 阶段三 | 用户决策 | 跳过国网 → 后关闭代理重试；攻南网；真实抓取归档 |
| 阶段四 | 新增 csg_generic / gov_generic，新增 6 政府源，修 in_en / sgcc 正则 | 南网+政府源打通 |
| 阶段五 | 首轮归档 | 165 篇 / 8 站点 / 6 省级分类 |
| 阶段六 | 关代理后重试国网（HTTP 打通） | 508 篇 / 18 站点 / 14 省份 |
| 阶段七 | CSV 汇总导出 | `_csv/` 4 张表 |
| 阶段八 | **CDP 真实浏览器攻克瑞数 412** | 见下节方法论 |

### 5. 瑞数（RiverSecurity）412 攻坚方法论 ★核心经验★
> 本节是本次攻关最有价值的沉淀。瑞数动态防护是目前国内电力/政务站群的主流反爬。

**识别特征**：响应 `HTTP 412`，页面含 `<script r='m'>` 且定义 `$_ts=window['$_ts']`，
响应体仅 2~6 KB 的混淆 JS，并下发 `PW9ydXnjjO8X*` Cookie。

**为什么纯 HTTP 方案必然失败**：瑞数要求浏览器执行其混淆 JS，在本地计算指纹，
再把结果回传换取放行 Cookie。`requests/httpx` 无法执行 JS，因此永远停在 412。

**正确姿势：CDP 驱动真实 Chrome**
1. 启动 Chrome（**三个关键开关缺一不可**）：
   ```bash
   chrome.exe --remote-debugging-port=9222 \
     --user-data-dir=<独立目录> \
     --user-agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36" \
     --disable-blink-features=AutomationControlled \
     --headless=new
   ```
   - `--user-agent`：**最关键**。headless 默认 UA 含 `HeadlessChrome`，瑞数秒识别；
     必须覆盖成普通 Chrome UA。
   - `--disable-blink-features=AutomationControlled`：令 `navigator.webdriver=false`。
2. 用 WebSocket 连 CDP，`Page.navigate` 打开目标页；
3. 轮询 `document.documentElement.outerHTML.length`，等它稳定且 > 阈值（挑战完成）；
4. 取 `outerHTML` 交回**项目既有适配器** `parse_list` / `parse_detail` 解析
   —— 浏览器只做「取 HTML」，解析逻辑零改动、完全复用。

**踩坑记录**：
- Python 直连 CDP WebSocket 会因 Origin 校验被拒（403），需 `suppress_origin=True`，
  或给 Chrome 加 `--remote-allow-origins=*`。
- `--user-agent` 在旧实例仍占用端口时不会生效——务必先确认 9222 端口归属进程已退出。
- 详情页 `<h1>` 常是栏目名（如「新闻中心」），会覆盖列表页的真实标题；
  需内置**栏目名词表**，命中则回退用列表页标题。
- 列表页可能混入 PDF/Office 附件链接，需按后缀过滤。

**验证**：北京 `www.bj.sgcc.com.cn` 由「412 空白页」→ 成功渲染 33 KB 页面，
抓到「满格电力点亮天安门广场"祝福祖国"巨型花篮」等真实新闻。
"""


def load_reports() -> tuple[dict, dict]:
    crawl = json.loads(CRAWL_REPORT.read_text(encoding="utf-8")) if CRAWL_REPORT.exists() else {}
    probe = json.loads(PROBE_REPORT.read_text(encoding="utf-8")) if PROBE_REPORT.exists() else {}
    return crawl, probe


def load_cdp_entries() -> dict:
    """加载 CDP 浏览器抓取结果（仅 ``crawler == "cdp-browser"`` 的归档）。"""
    data_root = ROOT / "data" / "power_news"
    out: dict[str, dict] = {}
    for p in data_root.glob("*/*/articles.json"):
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if payload.get("crawler") != "cdp-browser":
            continue
        sid = payload.get("source_id")
        if sid:
            out[sid] = _entry_from_payload(payload)
    return out


def _entry_from_payload(payload: dict) -> dict:
    cnt = payload.get("article_count", 0)
    return {
        "source_id": payload.get("source_id", ""),
        "source_name": payload.get("source_name", ""),
        "province": payload.get("province", ""),
        "list_url": payload.get("list_url", ""),
        "found": cnt,
        "saved": cnt,
        "ok": cnt > 0,
        "error": "" if cnt else "详情页全部解析为空",
    }


def load_disk_entries() -> dict:
    """扫描全部来源归档目录，得到**权威**的每源成功结果。

    相比报告文件，磁盘归档不会被后续批次覆盖，因此作为最终事实来源。
    """
    data_root = ROOT / "data" / "power_news"
    out: dict[str, dict] = {}
    for p in data_root.glob("*/*/articles.json"):
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        sid = payload.get("source_id")
        if sid:
            out[sid] = _entry_from_payload(payload)
    return out


def index_by_id(report: dict, key: str = "sources") -> dict:
    out = {}
    for item in report.get(key, []) or report.get("results", []):
        out[item.get("source_id") or item.get("id")] = item
    return out


def source_table(prov: dict, crawl_idx: dict, probe_idx: dict) -> str:
    """生成该省来源状态表。"""
    rows = ["| 来源 id | 来源站点 | 类别 | 抓取结果 | HTTP 状态 | 结论 |",
            "|---|---|---|---|---|---|"]
    matched = False
    for s in PROV_SOURCES.get(prov["name"], []):
        c = crawl_idx.get(s["id"], {})
        p = probe_idx.get(s["id"], {})
        http = next((x for x in p.get("probes", []) if x.get("scheme") == "http"), {})
        next((x for x in p.get("probes", []) if x.get("scheme") == "https"), {})
        http_txt = f"{http.get('status','-')}/{http.get('length','-')}"
        if c:
            saved = c.get("saved", 0)
            res_txt = f"✅ {saved} 篇" if c.get("ok") else f"❌ {c.get('error','')[:24]}"
            verdict = "已收录" if c.get("ok") else "未收录"
        else:
            res_txt = "—（未纳入本轮采集）"
            verdict = "未采集"
        rows.append(
            f"| `{s['id']}` | {s['name']} | {s['category']} | {res_txt} | {http_txt} | {verdict} |"
        )
        matched = True
    if not matched:
        return "_（本省份无登记来源）_"
    return "\n".join(rows)


# 省份 → 来源清单（id / name / category），与 sources_seed.py 一一对应
PROV_SOURCES: dict[str, list[dict]] = {
    "全国_行业": [
        {"id": "bjx", "name": "北极星电力网", "category": "industry"},
        {"id": "cpnn", "name": "中国电力新闻网", "category": "industry"},
        {"id": "in_en", "name": "国际能源网", "category": "industry"},
    ],
    "北京市": [{"id": "sgcc_bj", "name": "国网北京市电力公司", "category": "sgcc"}],
    "天津市": [{"id": "sgcc_tj", "name": "国网天津市电力公司", "category": "sgcc"}],
    "河北省": [
        {"id": "sgcc_he", "name": "国网河北省电力有限公司", "category": "sgcc"},
        {"id": "sgcc_jb", "name": "国网冀北电力有限公司", "category": "sgcc"},
    ],
    "山西省": [{"id": "sgcc_sx", "name": "国网山西省电力公司", "category": "sgcc"}],
    "内蒙古自治区": [
        {"id": "impc", "name": "内蒙古电力（集团）有限责任公司", "category": "independent"},
        {"id": "sgcc_md", "name": "国网内蒙古东部电力有限公司", "category": "sgcc"},
    ],
    "辽宁省": [{"id": "sgcc_ln", "name": "国网辽宁省电力有限公司", "category": "sgcc"}],
    "吉林省": [{"id": "sgcc_jl", "name": "国网吉林省电力有限公司", "category": "sgcc"}],
    "黑龙江省": [{"id": "sgcc_hl", "name": "国网黑龙江省电力有限公司", "category": "sgcc"}],
    "上海市": [{"id": "sgcc_sh", "name": "国网上海市电力公司", "category": "sgcc"}],
    "江苏省": [{"id": "sgcc_js", "name": "国网江苏省电力有限公司", "category": "sgcc"}],
    "浙江省": [{"id": "sgcc_zj", "name": "国网浙江省电力有限公司", "category": "sgcc"}],
    "安徽省": [{"id": "sgcc_ah", "name": "国网安徽省电力有限公司", "category": "sgcc"}],
    "福建省": [{"id": "sgcc_fj", "name": "国网福建省电力有限公司", "category": "sgcc"}],
    "江西省": [
        {"id": "sgcc_jx", "name": "国网江西省电力有限公司", "category": "sgcc"},
        {"id": "gov_jx_nyj", "name": "江西省能源局", "category": "gov"},
    ],
    "山东省": [
        {"id": "sgcc_sd", "name": "国网山东省电力公司", "category": "sgcc"},
        {"id": "gov_sd_nyj", "name": "山东省能源局", "category": "gov"},
    ],
    "河南省": [{"id": "sgcc_ha", "name": "国网河南省电力公司", "category": "sgcc"}],
    "湖北省": [
        {"id": "sgcc_hb", "name": "国网湖北省电力有限公司", "category": "sgcc"},
        {"id": "gov_hb_nyj", "name": "湖北省能源局", "category": "gov"},
    ],
    "湖南省": [{"id": "sgcc_hn", "name": "国网湖南省电力有限公司", "category": "sgcc"}],
    "广东省": [{"id": "csg_gd", "name": "广东电网有限责任公司", "category": "csg"}],
    "广西壮族自治区": [
        {"id": "csg_gx", "name": "广西电网有限责任公司", "category": "csg"},
        {"id": "gov_gx_fgw", "name": "广西壮族自治区发展和改革委员会", "category": "gov"},
    ],
    "海南省": [{"id": "csg_hn", "name": "海南电网有限责任公司", "category": "csg"}],
    "重庆市": [{"id": "sgcc_cq", "name": "国网重庆市电力公司", "category": "sgcc"}],
    "四川省": [{"id": "sgcc_sc", "name": "国网四川省电力公司", "category": "sgcc"}],
    "贵州省": [
        {"id": "csg_gz", "name": "贵州电网有限责任公司", "category": "csg"},
        {"id": "gov_gz_nyj", "name": "贵州省能源局", "category": "gov"},
    ],
    "云南省": [
        {"id": "csg_yn", "name": "云南电网有限责任公司", "category": "csg"},
        {"id": "gov_yn_nyj", "name": "云南省能源局", "category": "gov"},
    ],
    "西藏自治区": [{"id": "sgcc_xz", "name": "国网西藏电力有限公司", "category": "sgcc"}],
    "陕西省": [{"id": "sgcc_sn", "name": "国网陕西省电力有限公司", "category": "sgcc"}],
    "甘肃省": [{"id": "sgcc_gs", "name": "国网甘肃省电力公司", "category": "sgcc"}],
    "青海省": [{"id": "sgcc_qh", "name": "国网青海省电力公司", "category": "sgcc"}],
    "宁夏回族自治区": [{"id": "sgcc_nx", "name": "国网宁夏电力有限公司", "category": "sgcc"}],
    "新疆维吾尔自治区": [{"id": "sgcc_xj", "name": "国网新疆电力有限公司", "category": "sgcc"}],
    "香港特别行政区": [{"id": "clp_hk", "name": "中华电力有限公司", "category": "hmt"}],
    "澳门特别行政区": [{"id": "cem_macau", "name": "澳门电力股份有限公司", "category": "hmt"}],
    "台湾省": [{"id": "taipower", "name": "台湾电力股份有限公司", "category": "hmt"}],
}

def status_icon(status: str) -> str:
    """按状态文本前缀取图标（兼容 ⚠️ 的变体选择符差异）。"""
    if status.startswith("✅"):
        return "🟢"
    if status.startswith("⚠"):
        return "🟡"
    if status.startswith("❌"):
        return "🔴"
    return "⚪"


def render_province(prov: dict, crawl_idx: dict, probe_idx: dict) -> str:
    icon = status_icon(prov["status"])
    lines = [
        f"# {prov['name']} · 攻克过程日志",
        "",
        f"> {icon} **当前状态**：{prov['status']}　|　**分组**：{prov['group']}"
        + (f"　|　**行政区划代码**：{prov['region']}" if prov["region"] else ""),
        "",
        f"> 归档时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}　"
        f"| 数据目录：`data/power_news/{prov['name']}/`",
        "",
        "## 一、本省来源与当前状态",
        "",
        source_table(prov, crawl_idx, probe_idx),
        "",
        "> 注：「HTTP 状态」为**来源清单声明路径**的实时探活结果（`状态码/字节数`），"
        "不一定等于实际生效路径——例如江苏声明路径 `/p1/` 返回 404，"
        "但适配器改用 `/xwzx/` 后成功入库。",
        "",
        "## 二、攻克时间线（按发生顺序）",
        "",
    ]
    for i, ev in enumerate(prov["timeline"], 1):
        lines.append(f"### {i}. {ev['phase']}")
        lines.append("")
        lines.append(f"- **卡点现象**：{ev['symptom']}")
        lines.append("- **排查步骤 / 操作指令**：")
        for a in ev["actions"]:
            lines.append(f"  - {a}")
        lines.append(f"- **验证结果 / 最终方案**：{ev['result']}")
        lines.append(f"- **可复用经验**：{ev['lesson']}")
        lines.append("")

    lines += [
        "## 三、经验沉淀（本省专项）",
        "",
    ]
    lessons = [ev["lesson"] for ev in prov["timeline"] if ev["lesson"] and ev["lesson"] != "—"]
    if lessons:
        for les in dict.fromkeys(lessons):
            lines.append(f"- {les}")
    else:
        lines.append("- 本省暂无专项经验，见「共用背景」中的通用范式。")
    lines.append("")
    lines.append("> 通用环境、三类阻碍与站群正则说明见 `README.md`「共用背景」。")
    lines.append("")
    return "\n".join(lines)


def render_readme(crawl: dict, probe: dict, crawl_idx: dict) -> str:
    # 统计（按合并后的真实来源结果计算）
    all_entries = list(crawl_idx.values())
    total_articles = sum(e.get("saved", 0) for e in all_entries if e.get("ok"))
    ok_sources = sum(1 for e in all_entries if e.get("ok"))
    fail_sources = sum(1 for e in all_entries if not e.get("ok"))
    ok_count = sum(1 for p in PROVINCES if p["status"].startswith("✅"))
    part_count = sum(1 for p in PROVINCES if p["status"].startswith("⚠"))
    fail_count = sum(1 for p in PROVINCES if p["status"].startswith("❌"))

    lines = [
        "# 各省份电网新闻爬取 · 攻克过程日志",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}　"
        f"| 数据来源：`_crawl_report.json` + `_cdp_report.json` + `_probe_report.json`（真实可追溯）",
        "",
        "本项目目标是采集全国各省份电网及能源主管部门的新闻，按「省份 / 站点」"
        "两级目录归档。本文档**按省份独立归档**每一次攻关的：①卡点详述、"
        "②排查步骤与操作指令、③验证结果与可复用经验。",
        "",
        "## 总览",
        "",
        "- 来源清单：**45 条**（国网 27 + 南网 5 + 独立电网 1 + 港澳台 3 + 行业 3 + 政府 6）",
        f"- 已归档文章：**{total_articles} 篇**",
        f"- 成功来源：**{ok_sources} 个**；未成功来源：{fail_sources} 个",
        f"- 省份条目：**{len(PROVINCES)} 个**　→　🟢 已攻克 {ok_count}　🟡 部分攻克 {part_count}　🔴 未攻克 {fail_count}",
        "",
        "### 省份索引",
        "",
        "| 省份 | 状态 | 分组 | 日志 |",
        "|---|---|---|---|",
    ]
    for p in PROVINCES:
        icon = status_icon(p["status"])
        lines.append(
            f"| {icon} {p['name']} | {p['status']} | {p['group']} | "
            f"[`{p['name']}.md`](./{p['name']}.md) |"
        )
    lines += ["", SHARED_BACKGROUND, ""]
    return "\n".join(lines)


def apply_results(prov: dict, crawl_idx: dict, cdp_idx: dict) -> None:
    """把真实抓取结果并入省份状态与时间线（数据驱动，保证日志与事实一致）。

    Args:
        prov: 省份条目（原地修改 status / timeline）。
        crawl_idx: 合并后的来源结果索引（含 CDP）。
        cdp_idx: CDP 浏览器抓取结果索引。
    """
    srcs = PROV_SOURCES.get(prov["name"], [])

    # 1) 若本省有来源经 CDP 攻克成功，追加「阶段八」时间线
    gained = [(s, cdp_idx[s["id"]]) for s in srcs if cdp_idx.get(s["id"], {}).get("ok")]
    if gained:
        names = "、".join(f"{s['name']}（{e['saved']} 篇）" for s, e in gained)
        prov["timeline"] = prov["timeline"] + [{
            "phase": "阶段八 · CDP 真实浏览器攻克瑞数 412",
            "symptom": "本站点此前受**瑞数动态防护（RiverSecurity）**拦截：HTTP 返回 412，"
                       "响应体仅 2~6KB 混淆 JS（含 `$_ts` 全局变量与 `<script r='m'>`），"
                       "纯 httpx 请求无法拿到正文。",
            "actions": [
                "启动真实 Chrome：`--user-agent=<普通 Chrome UA>` + "
                "`--disable-blink-features=AutomationControlled` + `--remote-debugging-port=9222`；",
                "确认 UA **不含** `HeadlessChrome`（headless 默认 UA 会直接暴露），且 `navigator.webdriver=false`；",
                "用 CDP `Page.navigate` 打开列表页，轮询 `document.documentElement.outerHTML.length` "
                "直至挑战通过、DOM 渲染稳定（>2KB 且不再增长）；",
                "取渲染后 HTML 交回**既有适配器** `parse_list` / `parse_detail` 解析（解析逻辑零改动）；",
                "详情页 `<h1>` 为栏目名时，回退使用列表页标题；过滤 PDF/Office 附件链接。",
            ],
            "result": f"瑞数挑战通过，成功入库：{names}。",
            "lesson": "瑞数 412 只能靠真实浏览器执行 JS 挑战破解；两个关键开关——"
                      "UA 去掉 HeadlessChrome、关闭 navigator.webdriver。",
        }]

    # 2) 依据实际来源成功数，重算省份状态
    if not srcs:
        return
    oks = [s for s in srcs if (crawl_idx.get(s["id"]) or {}).get("ok")]
    n, t = len(oks), len(srcs)
    old = prov["status"]
    reason = old.split("（", 1)[1].rstrip("）") if "（" in old else ""
    if n == t:
        prov["status"] = old if old.startswith("✅") else "✅ 已攻克"
    elif n > 0:
        prov["status"] = f"⚠️ 部分攻克（{n}/{t} 个来源）"
    else:
        prov["status"] = f"❌ 未攻克（{reason}）" if reason else "❌ 未攻克"


def main() -> None:
    crawl, probe = load_reports()
    # 合并优先级：报告（含失败原因） → 磁盘归档（成功的权威结果）
    crawl_idx = index_by_id(crawl, "sources")
    crawl_idx.update(load_disk_entries())
    cdp_idx = load_cdp_entries()
    probe_idx = index_by_id(probe, "results")

    for prov in PROVINCES:
        apply_results(prov, crawl_idx, cdp_idx)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for prov in PROVINCES:
        (OUT_DIR / f"{prov['name']}.md").write_text(
            render_province(prov, crawl_idx, probe_idx), encoding="utf-8"
        )
    (OUT_DIR / "README.md").write_text(render_readme(crawl, probe, crawl_idx), encoding="utf-8")

    print(f"已生成 {len(PROVINCES)} 个省份日志 + README.md")
    print(f"输出目录: {OUT_DIR}")


if __name__ == "__main__":
    main()
