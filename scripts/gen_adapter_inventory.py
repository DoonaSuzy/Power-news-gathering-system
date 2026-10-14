"""生成《采集器（适配器）全量清单》（``docs/采集器全量清单.md``）。

为什么单独做一个脚本
--------------------
这份清单是**代码的镜像**：适配器增删后若手工维护文档，必然与 ``all_adapters()``
的实际枚举脱节 —— 本项目已踩过（文档停在 51 个，实际 57 个，README 也跟着错）。
把口径固化成脚本后，``--check`` 可以在交付前断言文档没有过期。

**口径**：

- 适配器清单 = ``registry.all_adapters()`` 实时枚举（**不是**目录下的 ``*.py`` 文件数，
  因为通用基类 / 注册中心也在同目录）；
- 源文件路径 = 用 ``registry.ADAPTER_MODULES`` 反查模块 ``__file__``
  （动态生成的类 ``__module__`` 不可靠，见文档附 D）；
- 站点名称 / 类别 = ``newsagg.sources_seed.ALL_SOURCES``；
- 风格 = 类自己 ``__dict__`` 里是否定义了 ``parse_*`` 等方法（覆写 → 专属，否则 → 配置型）。

用法::

    python scripts/gen_adapter_inventory.py            # 写入 docs/采集器全量清单.md
    python scripts/gen_adapter_inventory.py --check    # 只校验文档是否与代码一致
    python scripts/gen_adapter_inventory.py --stdout   # 打印到标准输出
"""

from __future__ import annotations

import argparse
import importlib
import pathlib
import re
import sys
from datetime import date

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from newsagg import sources_seed  # noqa: E402
from newsagg.adapters import registry  # noqa: E402

OUTPUT = PROJECT_ROOT / "docs" / "采集器全量清单.md"

#: 生成时间行。``--check`` 比对前先抹掉它 —— 否则文档昨天生成、今天校验，
#: 只因为日期不同就报「文档已过期」，把真正的代码漂移淹没在假警报里。
_GENERATED_AT_RE = re.compile(r"^> 生成时间：\d{4}-\d{2}-\d{2}", re.M)


def canonical(text: str) -> str:
    """抹掉生成时间后的正文，用于 ``--check`` 比对。"""
    return _GENERATED_AT_RE.sub("> 生成时间：", text).strip()

#: 类别展示顺序与中文名（键与 sources_seed.SOURCE_BY_ID[*]["category"] 一致）
CATEGORY_ORDER: tuple[tuple[str, str], ...] = (
    ("sgcc", "国家电网省级公司"),
    ("csg", "南方电网省级公司"),
    ("independent", "独立电网 / 电力企业"),
    ("industry", "行业媒体"),
    ("gov", "政府能源主管部门"),
    ("hmt", "港澳台电力公司"),
    ("other", "其他"),
)

#: 判定「专属适配器」时检查的方法名
OVERRIDE_METHODS: tuple[str, ...] = (
    "parse_list",
    "parse_detail",
    "is_article",
    "allows",
    "robots_allowed",
    "_extract_paragraphs",
)


def _adapter_file(module_name: str) -> pathlib.Path:
    """按模块全名取源文件路径。

    不直接用 ``type(adapter).__module__``：动态生成的适配器类拿不到真实文件。
    """
    module = importlib.import_module(module_name)
    return pathlib.Path(getattr(module, "__file__", "?"))


def _relative(path: pathlib.Path) -> str:
    """转成从 ``docs/`` 出发的相对路径（文档在 docs/ 下，链接要能点开）。"""
    try:
        return "../../" + path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def collect() -> list[dict[str, object]]:
    """把全部适配器枚举成行记录。"""
    registry.ensure_loaded()
    adapters = registry.all_adapters()
    by_id = sources_seed.SOURCE_BY_ID

    # 模块名尾段 == source_id，据此建立 source_id → 模块文件 的映射
    module_by_id: dict[str, str] = {
        name.rsplit(".", 1)[-1]: name for name in registry.ADAPTER_MODULES
    }

    rows: list[dict[str, object]] = []
    for source_id in sorted(adapters):
        adapter = adapters[source_id]
        cls = type(adapter)
        source = by_id.get(source_id, {})
        overridden = [n for n in OVERRIDE_METHODS if n in cls.__dict__]
        module_name = module_by_id.get(source_id)
        path = _adapter_file(module_name) if module_name else pathlib.Path("?")
        rows.append(
            {
                "id": source_id,
                "name": source.get("name", source_id),
                "category": source.get("category", "other"),
                "enabled": bool(source.get("enabled", False)),
                "cls": cls.__name__,
                "overridden": overridden,
                "style": "专属" if overridden else "配置型",
                "path": path,
                "lines": len(path.read_text(encoding="utf-8").splitlines())
                if path.is_file()
                else 0,
            }
        )
    return rows


def _table_header() -> list[str]:
    return [
        "| # | 站点名称 | 采集器标识 | 适配器类 | 风格 | 覆写方法 | 源文件路径（可点击） |",
        "|---:|---|---|---|---|---|---|",
    ]


def _row(index: int, r: dict[str, object]) -> str:
    overridden = r["overridden"]
    method_text = "、".join(f"`{m}`" for m in overridden) if overridden else "—"
    link = f"[`{_relative(r['path'])}`]({_relative(r['path'])})"
    return (
        f"| {index} | {r['name']} | `{r['id']}` | `{r['cls']}` | "
        f"{r['style']} | {method_text} | {link} |"
    )


def render(rows: list[dict[str, object]]) -> str:
    """渲染整份文档。"""
    total = len(rows)
    missing = [r for r in rows if not pathlib.Path(r["path"]).is_file()]
    dedicated = [r for r in rows if r["style"] == "专属"]
    configured = [r for r in rows if r["style"] == "配置型"]

    dirs: dict[str, int] = {}
    for r in rows:
        parent = pathlib.Path(r["path"]).parent
        try:
            rel = parent.resolve().relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            rel = parent.as_posix()
        dirs[rel] = dirs.get(rel, 0) + 1

    grouped: dict[str, list[dict[str, object]]] = {key: [] for key, _ in CATEGORY_ORDER}
    for r in rows:
        grouped.setdefault(str(r["category"]), []).append(r)

    out: list[str] = []
    out.append("# 采集器（适配器）全量清单")
    out.append("")
    out.append(
        f"> 生成时间：{date.today().isoformat()}　| "
        "数据来源：`all_adapters()` 实时枚举 + `ADAPTER_MODULES` 反查源文件"
    )
    out.append(
        f"> **全部 {total} 个适配器的源文件路径均已逐一验证存在**"
        f"（校验结果：**{total - len(missing)}/{total}**"
        f"{'，0 缺失' if not missing else '，缺失 ' + str(len(missing))}），链接可直接点击。"
    )
    out.append("")
    out.append("## 0. 总览")
    out.append("")
    out.append("| 项 | 值 |")
    out.append("|---|---|")
    out.append(f"| 适配器总数 | **{total}** |")
    out.append(
        f"| 源文件路径校验 | **{total - len(missing)}/{total} 存在** "
        f"{'✅' if not missing else '❌'} |"
    )
    out.append("| 源文件根目录 | `newsagg/adapters/` |")
    out.append("| 注册中心 | `newsagg/adapters/registry.py` |")
    out.append(
        "| 枚举 API | `newsagg.adapters.all_adapters()`（返回 `dict[str, BaseAdapter]`） |"
    )
    out.append("| 契约校验命令 | `python -m newsagg adapters` |")
    out.append("")
    out.append("### 按采集器风格")
    out.append("")
    out.append("| 风格 | 数量 | 判据 |")
    out.append("|---|---:|---|")
    out.append(
        f"| 专属适配器 | {len(dedicated)} | "
        "覆写了 `parse_list` / `parse_detail` / `is_article` 等解析方法 |"
    )
    out.append(
        f"| 配置型 | {len(configured)} | 有独立源文件，仅声明站点元信息（列表页、域名、栏目） |"
    )
    out.append("")
    out.append("### 按类别")
    out.append("")
    out.append("| 类别 | 数量 |")
    out.append("|---|---:|")
    for key, label in CATEGORY_ORDER:
        count = len(grouped.get(key, []))
        if count:
            out.append(f"| {label} | {count} |")
    out.append("")
    out.append("### 源文件目录分布")
    out.append("")
    out.append("| 目录 | 文件数 |")
    out.append("|---|---:|")
    for rel in sorted(dirs):
        out.append(f"| `{rel}/` | {dirs[rel]} |")
    out.append("")
    out.append("---")
    out.append("")

    for key, label in CATEGORY_ORDER:
        items = grouped.get(key, [])
        if not items:
            continue
        out.append(f"## {label}（{len(items)} 个）")
        out.append("")
        out.extend(_table_header())
        for i, r in enumerate(items, 1):
            out.append(_row(i, r))
        out.append("")

    out.append("## 附 A：专属适配器明细（站点改版时优先检查这些）")
    out.append("")
    out.append(
        f"这 {len(dedicated)} 个**不是纯继承通用逻辑**，而是针对站点真实 DOM 结构写了专属规则 ——"
    )
    out.append("**站点改版时最需要复查的就是它们**。")
    out.append("")
    out.append("| 采集器标识 | 站点 | 覆写的方法 | 源文件 | 行数 |")
    out.append("|---|---|---|---|---:|")
    for r in sorted(dedicated, key=lambda x: -int(x["lines"])):
        rel = _relative(r["path"])
        methods = "、".join(f"`{m}`" for m in r["overridden"])
        out.append(
            f"| `{r['id']}` | {r['name']} | {methods} | [`{rel}`]({rel}) | {r['lines']} |"
        )
    out.append("")
    out.append(f"## 附 B：通用基类（不在 {total} 个之内，供改适配器时参考）")
    out.append("")
    out.append("| 文件 | 作用 |")
    out.append("|---|---|")
    for name, desc in (
        ("base.py", "`BaseAdapter` 抽象契约（`parse_list` / `parse_detail` / `is_article` / `allows`）"),
        ("_common.py", "`HeuristicAdapter` 通用启发式（正文三级降级、标题黑名单）"),
        ("sgcc_generic.py", "`SgccGenericAdapter` 国网站群模板 + `build_sgcc_adapter()`"),
        ("csg_generic.py", "`CsgGenericAdapter` 南网站群模板"),
        ("gov_generic.py", "`GovGenericAdapter` 政府能源主管部门模板 + `build_gov_adapter()`"),
        ("_gne.py", "通用取数工具（列表/详情共用逻辑）"),
        ("registry.py", "注册中心 + `ADAPTER_MODULES`（新增适配器要在这里登记）"),
        ("listing.py", "列表页工具（`extract_links` / `find_next_page`）"),
    ):
        rel = f"../../newsagg/adapters/{name}"
        out.append(f"| [`newsagg/adapters/{name}`]({rel}) | {desc} |")
    out.append("")
    out.append("## 附 C：复现命令")
    out.append("")
    out.append("```bash")
    out.append("# ① 列出所有适配器（含契约自检）")
    out.append("python -m newsagg adapters")
    out.append("")
    out.append("# ② 重新生成本清单")
    out.append("python scripts/gen_adapter_inventory.py")
    out.append("")
    out.append("# ③ 校验本清单是否已过期（交付前跑，不一致则返回非零）")
    out.append("python scripts/gen_adapter_inventory.py --check")
    out.append("")
    out.append("# ④ 单独验证某个适配器能否解析")
    out.append("python -m pytest tests/test_adapters.py -q --override-ini=addopts= -k <source_id>")
    out.append("```")
    out.append("")
    out.append("## 附 D：取源文件路径的两个坑")
    out.append("")
    out.append("**① `all_adapters()` 返回 `dict` 而不是 `list`**")
    out.append("")
    out.append("直接 `for a in all_adapters()` 会遍历到 **key（字符串）**，")
    out.append("拿它调 `inspect.getfile` 会报 `TypeError: <class 'str'> is a built-in class`。")
    out.append("正确写法：`for sid, a in all_adapters().items()`。")
    out.append("")
    out.append("**② 动态创建的类拿不到真实源文件**")
    out.append("")
    out.append("部分适配器（`sgcc_*` / `gov_*`）的类是")
    out.append("`type(class_name, (XxxGenericAdapter,), attrs)` **动态生成**的，")
    out.append("它们的 `cls.__module__` 可能是 `abc`，")
    out.append("`inspect.getfile` 会返回 **Python 标准库的 `abc.py`** —— 而不是项目里的文件。")
    out.append("")
    out.append("正确做法：用 `ADAPTER_MODULES` 反查「模块名尾段 == `source_id`」的模块，")
    out.append("再取该模块的 `__file__`。本清单即按此法解析。")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成采集器全量清单")
    parser.add_argument("--check", action="store_true", help="只校验文档是否与代码一致")
    parser.add_argument("--stdout", action="store_true", help="打印到标准输出，不写文件")
    args = parser.parse_args(argv)

    content = render(collect())

    if args.stdout:
        print(content)
        return 0

    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.is_file() else ""
        if canonical(current) != canonical(content):
            print(f"文档已过期：{OUTPUT}")
            print("请运行 python scripts/gen_adapter_inventory.py 重新生成。")
            return 1
        print(f"文档与代码一致：{OUTPUT}")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(content, encoding="utf-8")
    rows = collect()
    missing = [r for r in rows if not pathlib.Path(r["path"]).is_file()]
    print(f"已写入 {OUTPUT}")
    print(f"适配器总数 {len(rows)}，源文件校验 {len(rows) - len(missing)}/{len(rows)}")
    if missing:
        for r in missing:
            print(f"  缺失源文件：{r['id']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
