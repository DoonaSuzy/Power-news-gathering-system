"""电力新闻来源种子数据（39 条），严格对应《国家电网覆盖省份（27个）》§8。

来源清单文档给出的 ``SOURCES`` 列表共 39 条，分五类：

| 类别 | 条数 | 说明 |
|---|---|---|
| ``sgcc`` | 27 | 国家电网省级公司官网新闻中心 |
| ``csg`` | 5 | 南方电网省级公司（广东/广西/云南/贵州/海南） |
| ``independent`` | 1 | 内蒙古电力（集团）—— 独立电网，与国网蒙东并存 |
| ``hmt`` | 3 | 香港/澳门/台湾电力公司官网 |
| ``industry`` | 3 | 全国性行业媒体（按省份标签聚合） |

**关键覆盖说明（来自来源清单文档）**：

1. 内蒙古自治区的电力新闻**必须同时**配置 ``sgcc_md``（国网蒙东，东部四盟市）与
   ``impc``（内蒙古电力集团，中西部盟市）两个来源才能完整覆盖全区。
2. 冀北（``sgcc_jb``）与河北（``sgcc_he``）行政代码同为 ``130000``，
   是两个独立采集主体，不可互相替代。
3. 西藏、新疆、青海等省份仅配置国网省级公司即可。
4. 港澳台采用电力公司官网直接采集，注意繁体中文编码与反爬差异。

本模块不做网络请求，仅提供数据结构；实际入库走 ``scripts/seed_power_sources.py``
或 ``python -m newsagg init-db``（会调用 :func:`as_rows`）。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "SOURCES",
    "ADDITIONAL_SOURCES",
    "ALL_SOURCES",
    "CATEGORY_LABELS",
    "SOURCE_BY_ID",
    "ids",
    "by_category",
    "count_by_category",
    "coverage_codes",
    "as_rows",
    "validate",
]

#: 类别 → 中文标签
CATEGORY_LABELS: dict[str, str] = {
    "sgcc": "国家电网省级公司",
    "csg": "南方电网省级公司",
    "independent": "独立电网",
    "hmt": "港澳台地区",
    "industry": "行业补充来源",
}


def _s(
    sid: str,
    name: str,
    province_code: str | None,
    base_url: str,
    news_path: str,
    priority: int,
    interval_hours: int,
    category: str,
    columns: tuple[str, ...],
    note: str = "",
) -> dict[str, Any]:
    """构造一条来源记录（字段名与来源清单文档 §8 完全一致，另加扩展字段）。"""
    return {
        "id": sid,
        "name": name,
        "province_code": province_code,
        "base_url": base_url.rstrip("/"),
        "news_path": news_path,
        "priority": priority,
        "interval_hours": interval_hours,
        # ---- 以下为改造方案 §3.5 要求的 sources 表扩展字段 ----
        "category": category,
        "columns": list(columns),
        "enabled": True,
        "note": note,
    }


SOURCES: list[dict[str, Any]] = [
    # ==================== 一、国家电网省级公司（27 条）====================
    _s("sgcc_bj", "国网北京市电力公司", "110000", "https://www.bj.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_tj", "国网天津市电力公司", "120000", "https://www.tj.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_he", "国网河北省电力有限公司", "130000", "https://www.he.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_jb", "国网冀北电力有限公司", "130000", "https://www.jb.sgcc.com.cn",
       "/p1/", 1, 2, "sgcc", ("公司要闻", "一线风采"),
       note="冀北（承德/张家口/秦皇岛/唐山/廊坊）与河北是两个独立采集主体，均需配置"),
    _s("sgcc_sx", "国网山西省电力公司", "140000", "https://www.sx.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_sd", "国网山东省电力公司", "370000", "https://www.sd.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_sh", "国网上海市电力公司", "310000", "https://www.sh.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_js", "国网江苏省电力有限公司", "320000", "https://www.js.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_zj", "国网浙江省电力有限公司", "330000", "https://www.zj.sgcc.com.cn",
       "/p1/spxw.html", 0, 1, "sgcc", ("公司要闻", "媒体聚焦", "视频新闻"),
       note="来源清单中唯一指定非 /p1/ 路径的来源（视频新闻栏目）"),
    _s("sgcc_ah", "国网安徽省电力有限公司", "340000", "https://www.ah.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_fj", "国网福建省电力有限公司", "350000", "https://www.fj.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_hb", "国网湖北省电力有限公司", "420000", "https://www.hb.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_hn", "国网湖南省电力有限公司", "430000", "https://www.hn.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_ha", "国网河南省电力公司", "410000", "https://www.ha.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_jx", "国网江西省电力有限公司", "360000", "https://www.jx.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_ln", "国网辽宁省电力有限公司", "210000", "https://www.ln.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_jl", "国网吉林省电力有限公司", "220000", "https://www.jl.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_hl", "国网黑龙江省电力有限公司", "230000", "https://www.hl.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_sn", "国网陕西省电力有限公司", "610000", "https://www.sn.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_gs", "国网甘肃省电力公司", "620000", "https://www.gs.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_qh", "国网青海省电力公司", "630000", "https://www.qh.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_nx", "国网宁夏电力有限公司", "640000", "https://www.nx.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_xj", "国网新疆电力有限公司", "650000", "https://www.xj.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_sc", "国网四川省电力公司", "510000", "https://www.sc.sgcc.com.cn",
       "/html/main/col81/", 0, 1, "sgcc", ("公司要闻", "媒体聚焦"),
       note="来源清单中唯一指定 /html/main/colXX/ 栏目路径的来源"),
    _s("sgcc_cq", "国网重庆市电力公司", "500000", "https://www.cq.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_xz", "国网西藏电力有限公司", "540000", "https://www.xz.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采")),
    _s("sgcc_md", "国网内蒙古东部电力有限公司", "150000", "https://www.md.sgcc.com.cn",
       "/p1/", 0, 1, "sgcc", ("公司要闻", "一线风采"),
       note="覆盖呼伦贝尔/兴安/通辽/赤峰；与 impc 并存才能覆盖全内蒙古"),

    # ==================== 二、南方电网省级公司（5 条）====================
    _s("csg_gd", "广东电网有限责任公司", "440000", "https://www.gd.csg.cn",
       "/", 0, 1, "csg", ("公司新闻", "一线传真"),
       note="广州/深圳供电局为独立法人，可作为广东省二级补充来源"),
    _s("csg_gx", "广西电网有限责任公司", "450000", "https://www.gx.csg.cn",
       "/", 0, 1, "csg", ("公司新闻", "一线传真")),
    _s("csg_yn", "云南电网有限责任公司", "530000", "https://www.yn.csg.cn",
       "/", 0, 1, "csg", ("公司新闻", "媒体聚焦", "一线传真")),
    _s("csg_gz", "贵州电网有限责任公司", "520000", "https://www.gz.csg.cn",
       "/", 0, 1, "csg", ("公司新闻", "一线传真")),
    _s("csg_hn", "海南电网有限责任公司", "460000", "https://www.hn.csg.cn",
       "/", 0, 1, "csg", ("公司新闻", "一线传真")),

    # ==================== 三、独立电网（1 条）===========================
    _s("impc", "内蒙古电力（集团）有限责任公司", "150000", "https://www.impc.com.cn",
       "/", 0, 1, "independent", ("集团动态",),
       note="独立于国家电网运营，覆盖呼和浩特、包头等中西部盟市，不可遗漏"),

    # ==================== 四、港澳台地区（3 条）=========================
    _s("clp_hk", "中华电力有限公司", "810000", "https://www.clp.com.hk",
       "/", 1, 4, "hmt", ("新闻稿", "公司动态"),
       note="繁体中文/英文；香港主要电力供应商（中电 CLP）"),
    _s("cem_macau", "澳门电力股份有限公司", "820000", "https://www.cem-macau.com",
       "/", 1, 4, "hmt", ("新闻", "公司动态"),
       note="繁体中文/葡文/英文；澳门唯一电力专营公司（澳电 CEM）"),
    _s("taipower", "台湾电力股份有限公司", "710000", "https://www.taipower.com.tw",
       "/", 1, 4, "hmt", ("电力脉动", "公司消息"),
       note="繁体中文/英文；台电官网另设期刊栏目定期报道"),

    # ==================== 五、行业补充来源（3 条）=======================
    _s("cpnn", "中国电力新闻网", None, "https://www.cpnn.com.cn",
       "/", 1, 2, "industry", ("地方频道",),
       note="设省/市/县三级地方频道，覆盖全国电力系统，可作官网 fallback"),
    _s("bjx", "北极星电力网", None, "https://www.bjx.com.cn",
       "/", 2, 4, "industry", ("省级标签页",),
       note="按省份/电网公司标签聚合，可检索「XX电网」相关报道，用于交叉验证"),
    _s("in_en", "国际能源网", None, "https://www.in-en.com",
       "/news/", 2, 4, "industry", ("交易中心动态", "省级聚合"),
       note="设有交易中心动态与省级电力新闻聚合栏目，文章形态 /article/html/energy-XXX.shtml"),
]

#: 补充来源（gov）—— 面向南方电网五省与部分国网省份的政府能源主管部门。
#:
#: **背景**：南方电网省级公司官网（``*.csg.cn``）与国网部分站点
#: （``*.sgcc.com.cn``）为 SPA/WAF 或当前网络不可达，无法直接采集。
#: 各级能源局/发改委官网同为国家站群 CMS，栏目结构规范、可静态抓取，
#: 且发布的电力能源政策、供需形势、项目核准等信息权威性更高，
#: 因此作为电网官网的**权威补充来源**接入，保证每省都有真实数据落地。
ADDITIONAL_SOURCES: list[dict[str, Any]] = [
    # ---------- 南方电网五省（政府能源主管部门）----------
    _s("gov_gz_nyj", "贵州省能源局", "520000", "http://nyj.guizhou.gov.cn",
       "/xwzx/gzdt/", 1, 2, "gov", ("要闻动态", "行业动态"),
       note="贵州省能源局要闻动态，电力/新能源政策一手发布"),
    _s("gov_gx_fgw", "广西壮族自治区发展和改革委员会", "450000", "http://fgw.gxzf.gov.cn",
       "/xwzx/fgdt/", 1, 2, "gov", ("发改动态",),
       note="广西发改委发改动态栏目，含能源电力政策与项目动态"),
    _s("gov_yn_nyj", "云南省能源局", "530000", "http://nyj.yn.gov.cn",
       "/xwzx/", 1, 2, "gov", ("能源要闻",),
       note="云南省能源局新闻，水电/新能源大省电力动态"),
    # ---------- 北方/其他省份补充 ----------
    _s("gov_sd_nyj", "山东省能源局", "370000", "http://nyj.shandong.gov.cn",
       "/col/col59966/index.html", 1, 2, "gov", ("动态要闻",),
       note="山东省能源局动态要闻（栏目页为 JS 渲染，当前采集为空，待适配）"),
    _s("gov_jx_nyj", "江西省能源局", "360000", "http://nyj.jiangxi.gov.cn",
       "/xwzx/", 1, 2, "gov", ("能源动态",),
       note="江西省能源局，华中电网覆盖省（域名当前不可达）"),
    _s("gov_hb_nyj", "湖北省能源局", "420000", "http://nyj.hubei.gov.cn",
       "/xwzx/", 1, 2, "gov", ("能源要闻",),
       note="湖北省能源局，三峡/水电与电网动态（域名当前不可达）"),

    # ---------- anysearch 检索发现的全国性补充来源（2026-10-12 新增）----------
    #
    # **背景**：现有 45 个源里，国网/南网省级站点有相当比例不可达
    # （TCP 443 拒绝 / WAF / DNS 不解析），北极星详情页被阿里云 WAF 拦，
    # 导致「最近几天常常没有新稿」。以下 5 个站点是独立检索服务发现的、
    # 本仓库原先没有覆盖的全国性能源媒体与主管部门，作为时效性与覆盖面补充。
    #
    # 全部站点已实测：HTTP 可达、robots 允许抓取新闻页（禁的都是 CMS 管理路径）。
    _s("nea", "国家能源局", None, "https://www.nea.gov.cn",
       "/", 1, 2, "gov", ("新闻发布", "政策解读"),
       note="能源主管部门，政策原文一手来源；详情页形态 /20250605/<hash>/c.html"),
    _s("nengyuanjie", "能源界", None, "https://www.nengyuanjie.net",
       "/", 2, 4, "industry", ("全球能源要闻",),
       note="全球中文能源门户；文章形态 /article/<数字>.html，列表页结构稳定"),
    _s("chinapower", "电力网", None, "http://mm.chinapower.com.cn",
       "/", 2, 4, "industry", ("国内新闻", "地方报道"),
       note="电力行业门户；栏目分区清晰 /xw/gnxw/、/dww/dfbd/，含 swiper 轮播"),
    _s("china5e", "中国能源网", None, "https://www.china5e.com",
       "/", 2, 4, "industry", ("能源要闻",),
       note="robots 禁止 /news/08* 与 /news/2014* 前缀，适配器必须避开这两段"),
    _s("ceic", "国家能源集团", None, "https://www.ceic.com",
       "/", 2, 6, "independent", ("集团要闻",),
       note="央企官网；列表链接为相对路径 ../gjnyjtww/newCEN/，需规范化"),

    # ---------- 电力央企官网（2026-10-12 新增）----------
    #
    # **背景**：现有来源中「省级电网公司官网」与「行业媒体」已覆盖较好，
    # 但**发电央企集团总部**的官方新闻一直缺位。这 4 家是实测可达、
    # robots 宽松（均无 robots.txt 或明确允许）的候选，与已有来源互补不重复。
    # 站点发现过程见 docs/电力能源企业官网清单.md。
    _s("cr_power", "华润电力", None, "https://www.cr-power.com",
       "/zhxw/index.html", 1, 4, "independent", ("新闻资讯", "下属企业动态"),
       note="文章 URL 自带日期 /zhxw/YYYY-MM-DD/N.html，可直接从 URL 取发布时间"),
    _s("china_cdt", "中国大唐集团", None, "https://www.china-cdt.com",
       "/dtwz/xwzx/jtxw/A024004002Gone1.html", 1, 4, "independent", ("集团新闻",),
       note="正文容器 div.xwzx-content；列表页含日期"),
    _s("ctg", "中国长江三峡集团", None, "https://www.ctg.com.cn",
       "/sxjt/xwzx55/zhxw23/index.html", 1, 4, "independent", ("集团要闻", "综合新闻"),
       note="正文容器 div#content_dsh；标题可能含 document.write 包装需清洗"),
    _s("chng", "中国华能集团", None, "https://www.chng.com.cn",
       "/list_jtyw/-/article/vUfd4jOBhajJ/list/23204/", 1, 6, "independent", ("集团要闻",),
       note="Liferay 门户，列表为 JS 渲染（静态抓取 0 条），待走浏览器通道或定位数据接口"),
]

#: 对外统一全量来源 = 原 39 条 + 补充来源
ALL_SOURCES: list[dict[str, Any]] = SOURCES + ADDITIONAL_SOURCES

CATEGORY_LABELS["gov"] = "政府能源主管部门"

SOURCE_BY_ID: dict[str, dict[str, Any]] = {s["id"]: s for s in ALL_SOURCES}


def ids() -> list[str]:
    """全部来源 id（保持清单顺序）。"""
    return [s["id"] for s in ALL_SOURCES]


def by_category(category: str) -> list[dict[str, Any]]:
    """按类别筛选来源。"""
    return [s for s in ALL_SOURCES if s["category"] == category]


def count_by_category() -> dict[str, int]:
    """各类别来源数量统计。"""
    out: dict[str, int] = {k: 0 for k in CATEGORY_LABELS}
    for s in ALL_SOURCES:
        out[s["category"]] = out.get(s["category"], 0) + 1
    return out


def coverage_codes() -> set[str]:
    """来源实际覆盖的省级行政区代码集合。"""
    return {s["province_code"] for s in ALL_SOURCES if s["province_code"]}


def list_url(source: dict[str, Any]) -> str:
    """拼出可直接抓取的栏目页 URL。"""
    base = source["base_url"].rstrip("/")
    path = source.get("news_path") or "/"
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def as_rows() -> list[dict[str, Any]]:
    """转换为可直接写库的行（补充 ``list_url`` 与 ``category`` 中文字段）。"""
    rows: list[dict[str, Any]] = []
    for s in ALL_SOURCES:
        row = dict(s)
        row["list_url"] = list_url(s)
        row["category_label"] = CATEGORY_LABELS.get(s["category"], s["category"])
        rows.append(row)
    return rows


def validate() -> list[str]:
    """自检：返回问题列表（空列表表示通过）。

    校验项：
    1. 来源 id 唯一；
    2. ``province_code`` 若存在，必须是 34 省字典中的合法代码；
    3. 原始清单保持 39 条（``SOURCES``），补充来源不占用此额度；
    4. 五类别的条数与文档 §九 汇总表一致。
    """
    from newsagg.provinces import PROVINCE_BY_CODE

    problems: list[str] = []

    seen: set[str] = set()
    for s in ALL_SOURCES:
        if s["id"] in seen:
            problems.append(f"重复的来源 id: {s['id']}")
        seen.add(s["id"])

    for s in ALL_SOURCES:
        code = s["province_code"]
        if code is not None and code not in PROVINCE_BY_CODE:
            problems.append(f"{s['id']} 的 province_code={code} 不在 34 省字典中")

    if len(SOURCES) != 39:
        problems.append(f"原始来源总数应为 39，实际 {len(SOURCES)}")

    expected = {"sgcc": 27, "csg": 5, "independent": 1, "hmt": 3, "industry": 3}
    base_counts: dict[str, int] = {}
    for s in SOURCES:
        base_counts[s["category"]] = base_counts.get(s["category"], 0) + 1
    for key, want in expected.items():
        if base_counts.get(key) != want:
            problems.append(f"类别 {key} 应有 {want} 条，实际 {base_counts.get(key)}")

    return problems
