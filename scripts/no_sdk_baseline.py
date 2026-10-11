"""给「业务人员 3 行代码即可获取标准化数据，数据获取效率提升 80%」一个可复现的分母。

为什么要这个脚本
----------------
「3 行代码」是可验证的：``import`` 一行 + ``PowerDataClient()`` 一行 + ``query`` 一行。
但「效率提升 80%」没有分母就是空话。这里把分母钉成一件可量的事——
**同一个需求（取一份标准化时序长表）业务侧要写多少行生产代码**：

* SDK 侧：``scripts/three_lines_demo.py``（本脚本会打印它的行数，实测 3 行）；
* 基线侧：本脚本里的 :func:`baseline_query`——不用 SDK，直连中台 HTTP，
  自己翻页、拆响应信封、处理业务错误码、组装 DataFrame、时间戳解析、
  去重排序、贴单位列、算空值/越界准确率。

两条硬约束，防止对比注水：

1. **基线必须和 SDK 产出同一张表**（``fill=False`` 口径：原始逐时行 + 去重 + 排序），
   不一致就是基线写错了，不能把它当成"效率提升更多"的证据；
2. **行数只数 :func:`baseline_query` 的函数体**，用 ``inspect`` 现算，
   排除空行、注释与 docstring——不复制粘贴一个凭记忆里的数字。

用法::

    python scripts/no_sdk_baseline.py                 # 需要 POWERSDK_API_BASE_URL + TOKEN
    python scripts/no_sdk_baseline.py --start 2024-01-10 --end 2024-01-12

"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import os
import textwrap
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import pandas as pd

#: 中台气象按「大区 × 产品」分表，省份落在哪个大区不由调用方猜——不知道就八张都问，
#: 不匹配的那几张返回空行。SDK 里这一手藏在 HttpTimeSeriesSource._fetch_weather 里。
BIG_REGIONS = ("华北", "东北", "华东", "华中", "华南", "西南", "西北", "港澳台")


def baseline_query(
    base_url: str,
    token: str,
    province_code: str,
    province_name: str,
    column: str,
    unit: str,
    start: str,
    end: str,
    page_size: int = 5000,
) -> tuple[pd.DataFrame, float]:
    """不用 SDK 取一份标准化气象长表，返回 (表, 准确率)。"""
    rows: list[tuple] = []
    for big_region in BIG_REGIONS:
        offset = 0
        while True:
            query = urlencode(
                {
                    "granularity": "hourly",
                    "params": column,
                    "level": "province",
                    "province": province_code,
                    "since": start,
                    "until": end,
                    "limit": page_size,
                    "offset": offset,
                }
            )
            url = f"{base_url}/weather/{quote(big_region)}/hist_rt?{query}"
            request = Request(
                url,
                headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            )
            try:
                with urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                raise RuntimeError(f"中台 HTTP {exc.code} {url}：{exc.read().decode('utf-8', 'replace')[:200]}") from exc
            except (URLError, OSError) as exc:
                raise RuntimeError(f"中台不可访问 {url}：{exc}") from exc
            if int(payload.get("code", 0) or 0) != 0:
                raise RuntimeError(f"中台业务错误：{payload}")
            batch = (payload.get("data") or {}).get("items") or []
            rows.extend((item["ts"], province_name, item[column], unit) for item in batch)
            if len(batch) < page_size:
                break
            offset += len(batch)

    frame = pd.DataFrame(rows, columns=["ts", "region", "value", "unit"])
    if frame.empty:
        return frame, float("nan")
    # 中台把 decimal 序列化成字符串（保住小数位），调用方自己转数值——
    # 忘了这一步，后面的比较运算会直接 TypeError。
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame["ts"] = pd.to_datetime(frame["ts"])
    frame = frame.drop_duplicates(["ts", "region"]).sort_values(["region", "ts"], kind="stable")
    frame = frame.reset_index(drop=True)
    bad = frame["value"].isna() | (frame["value"] < -40.0) | (frame["value"] > 60.0)
    return frame, 1.0 - float(bad.mean())


def sdk_query(start: str, end: str, column: str) -> pd.DataFrame:
    """业务同学的写法：配好环境变量后，取数就这三行。"""
    from powersdk import PowerDataClient

    return PowerDataClient().query(
        "weather", region="广东", start=start, end=end, granularity="hourly",
        param=column, fill=False,
    )


def code_lines(func: object) -> int:
    """函数体里的**生产代码行数**：去掉空行、整行注释与 docstring。"""
    source = textwrap.dedent(inspect.getsource(func))  # type: ignore[arg-type]
    tree = ast.parse(source)
    body = tree.body[0].body  # FunctionDef
    docstring_lines = 0
    first = body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        if isinstance(first.value.value, str):
            docstring_lines = first.end_lineno - first.lineno + 1
    counted = 0
    for node in body:
        start_line, end_line = node.lineno, getattr(node, "end_lineno", node.lineno)
        for line_no in range(start_line, end_line + 1):
            text = source.splitlines()[line_no - 1].strip()
            if not text or text.startswith("#"):
                continue
            counted += 1
    return counted - docstring_lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default="2024-01-10")
    parser.add_argument("--end", default="2024-01-12")
    parser.add_argument("--column", default="temperature_2m")
    args = parser.parse_args(argv)

    base_url = os.environ.get("POWERSDK_API_BASE_URL", "").rstrip("/")
    token = os.environ.get("POWERSDK_API_TOKEN", "")
    if not base_url or not token:
        raise SystemExit("需要 POWERSDK_API_BASE_URL 与 POWERSDK_API_TOKEN（测量对象是中台真实数据）")

    mine = code_lines(baseline_query)
    theirs = code_lines(sdk_query)
    baseline, accuracy = baseline_query(
        base_url, token, "440000", "广东", args.column, "°C", args.start, args.end
    )
    via_sdk = sdk_query(args.start, args.end, args.column)

    print(f"基线（不用 SDK，直连中台）：{len(baseline)} 行 / {baseline['region'].nunique()} 地区，准确率 {accuracy:.6f}")
    print(f"SDK（{theirs} 行函数体，业务侧写法 3 行）：{len(via_sdk)} 行，"
          f"准确率 {1.0 - float((via_sdk['value'].isna() | (via_sdk['value'] < -40) | (via_sdk['value'] > 60)).mean()):.6f}")
    same_shape = list(baseline.columns) == list(via_sdk.columns)
    same_rows = len(baseline) == len(via_sdk)
    align = baseline.reset_index(drop=True).compare(via_sdk.reset_index(drop=True))
    print(f"两张表一致：列={same_shape} 行数={same_rows} 差异={len(align)} 处")
    if not (same_shape and same_rows and len(align) == 0):
        print("SDK 侧行数/取值：")
        print(via_sdk.head())
        print(baseline.head())
        return 1
    print(f"同一件事的代码量：{mine} 行 → 3 行，降幅 {1 - 3 / mine:.1%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
