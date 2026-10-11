"""生成 ``sql/weather_schema.sql``：24 张气象数据表 + 站点表 + 回填进度表。

为什么用生成器而不是手写 SQL
----------------------------
24 张表除了「大区」「产品」两个字面量**完全相同**。手写等于把同一份列定义
抄 24 遍——改一个列宽就得改 24 处，漏一处就出现「某些表有这列、某些表没有」
的隐性不一致，且只在跑到那张表时才暴露。这里让列定义唯一来源于
:mod:`newsagg.weather.models`，SQL 只是它的投影，并用
``--check`` 在 CI 里断言产物与源码同步。

用法::

    python scripts/gen_weather_schema.py            # 写出 SQL
    python scripts/gen_weather_schema.py --check    # 只校验是否同步（不写文件）
    python scripts/gen_weather_schema.py --stdout   # 打到标准输出
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from newsagg.weather.models import (  # noqa: E402
    DAILY_AGG_LABEL,
    GRANULARITIES,
    HOURLY_VARS,
    PRODUCTS,
    REGIONS,
    table_name,
)

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "sql" / "weather_schema.sql"

#: 数值列宽。取值依据：ERA5 的实际量级 + 一点余量。
#: 例如 ``pressure_msl`` 约 870–1085 hPa，``DECIMAL(7,2)`` 足够；
#: 用 ``FLOAT`` 会引入二进制浮点误差，气象数据做日均/日累加时误差会被放大。
COLUMN_TYPES: dict[str, str] = {
    "temperature_2m": "DECIMAL(5,2)",
    "relative_humidity_2m": "DECIMAL(5,2)",
    "precipitation": "DECIMAL(7,2)",
    "wind_speed_10m": "DECIMAL(6,2)",
    "wind_direction_10m": "SMALLINT UNSIGNED",
    "shortwave_radiation": "DECIMAL(8,2)",
    "cloud_cover": "DECIMAL(5,2)",
    "pressure_msl": "DECIMAL(7,2)",
    "et0_fao_evapotranspiration": "DECIMAL(7,3)",
    "weather_code": "SMALLINT UNSIGNED",
}

HEADER = """\
-- =====================================================================
-- 气象数据表（24 张 = 8 大区 × 3 数据产品）
--
-- ⚠ 本文件由 `scripts/gen_weather_schema.py` 生成，请勿手改。
--   列定义唯一来源于 `newsagg/weather/models.py`；
--   改列请改源码后重新生成，并用 `--check` 验证同步。
--
-- 命名：wx_{大区}_{产品}
--   大区：huabei/dongbei/huadong/huazhong/huanan/xinan/xibei/gangat
--   产品：hist_rt(历史实时) / hist_fc(历史预测) / daily_fc(每日预测)
--
-- 时间类别：同表内由 `granularity` 列区分逐时/逐日，两类行的数值列同名但口径不同，
--   逐日口径见每个数值列注释里的「逐日=…」。
-- 时区：一律 Asia/Shanghai，逐时为整点、逐日为当日零点。
-- 幂等：主键 (station_id, granularity, ts)，重复采集覆盖而非追加。
-- =====================================================================

SET NAMES utf8mb4;

"""

STATIONS_TABLE = """\
-- ---------------------------------------------------------------------
-- 站点主表：全国行政区中心点（由 scripts/build_weather_stations.py 产出）
-- 大区口径与 newsagg/provinces.py 一致，供数据表按大区路由。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wx_stations (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键，CN + 行政区划代码',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码（唯一）',
    name           VARCHAR(64)   NOT NULL COMMENT '区县/市级名称',
    level          VARCHAR(16)   NOT NULL DEFAULT '' COMMENT 'district/city/province',
    lon            DECIMAL(9,6)  NOT NULL COMMENT '经度（官方质心，°E）',
    lat            DECIMAL(9,6)  NOT NULL COMMENT '纬度（官方质心，°N）',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    province       VARCHAR(32)   NOT NULL COMMENT '省级全称',
    region         VARCHAR(16)   NOT NULL COMMENT '大区：华北/东北/华东/华中/华南/西南/西北/港澳台',
    is_special     TINYINT(1)    NOT NULL DEFAULT 0 COMMENT '1=港澳台',
    created_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id),
    UNIQUE KEY uk_wx_stations_adcode (adcode),
    KEY idx_wx_stations_region (region)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象站点主表';

"""

PROGRESS_TABLE = """\
-- ---------------------------------------------------------------------
-- 回填进度表：让「2024.1.1 → 今天」这种跨数小时的长回填可中断续跑。
-- 主键即一片工作单元（产品 × 大区 × 时间窗）；status='ok' 的片会被跳过。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wx_ingest_progress (
    product       VARCHAR(16)   NOT NULL COMMENT '数据产品',
    region        VARCHAR(16)   NOT NULL COMMENT '大区',
    period_start  DATE          NOT NULL COMMENT '窗口起（闭）',
    period_end    DATE          NOT NULL COMMENT '窗口止（闭）',
    hourly_rows   INT UNSIGNED  NOT NULL DEFAULT 0 COMMENT '写入的逐时行数',
    daily_rows    INT UNSIGNED  NOT NULL DEFAULT 0 COMMENT '写入的逐日行数',
    stations      INT UNSIGNED  NOT NULL DEFAULT 0 COMMENT '本片站点数',
    status        VARCHAR(16)   NOT NULL DEFAULT 'ok' COMMENT 'ok / partial / failed',
    note          VARCHAR(255)  NOT NULL DEFAULT '' COMMENT '失败原因摘要',
    updated_at    TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (product, region, period_start, period_end),
    KEY idx_wx_ingest_progress_status (status)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '气象数据回填进度（可续跑）';

"""


def _value_columns() -> str:
    lines: list[str] = []
    for col, unit, cn in HOURLY_VARS:
        typ = COLUMN_TYPES[col]
        comment = f"{cn}（{unit}）；逐日={DAILY_AGG_LABEL[col]}"
        lines.append(
            f"    {col:<28} {typ:<20} NULL COMMENT '{comment}',"
        )
    return "\n".join(lines)


def data_table(region: str, product: str) -> str:
    name = table_name(region, product)
    comment = f"气象数据·{region}·{PRODUCTS[product]}"
    granularity = "/".join(GRANULARITIES)
    return f"""\
-- {region} × {PRODUCTS[product]}
CREATE TABLE IF NOT EXISTS {name} (
    station_id     VARCHAR(16)   NOT NULL COMMENT '站点主键',
    adcode         INT UNSIGNED  NOT NULL COMMENT '行政区划代码',
    province_code  CHAR(6)       NOT NULL COMMENT '省级行政区划代码',
    granularity    ENUM('hourly','daily') NOT NULL COMMENT '时间类别：{granularity}',
    ts             DATETIME      NOT NULL COMMENT '逐时=整点；逐日=当日零点（Asia/Shanghai）',
{_value_columns()}
    source         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '来源标识',
    fetched_at     TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (station_id, granularity, ts),
    KEY idx_{name}_adcode_ts (adcode, ts),
    KEY idx_{name}_ts (ts)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '{comment}';

"""


def render() -> str:
    parts = [HEADER, STATIONS_TABLE, PROGRESS_TABLE]
    parts.append(
        "-- =====================================================================\n"
        "-- 数据表 24 张："
        + "；".join(f"{r}={table_name(r, 'hist_rt').split('_')[1]}" for r in REGIONS)
        + "\n"
        "-- =====================================================================\n\n"
    )
    for region in REGIONS:
        for product in PRODUCTS:
            parts.append(data_table(region, product))
    return "".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 24 张气象数据表的建表 SQL")
    ap.add_argument("--check", action="store_true", help="只校验产物是否与源码同步")
    ap.add_argument("--stdout", action="store_true", help="打到标准输出，不写文件")
    args = ap.parse_args(argv)

    sql = render()

    if args.stdout:
        print(sql)
        return 0

    if args.check:
        if not TARGET.exists():
            print(f"缺少产物：{TARGET}", file=sys.stderr)
            return 1
        if TARGET.read_text(encoding="utf-8") != sql:
            print(
                f"{TARGET} 与 models.py 不同步——请重新执行不带 --check 的生成命令",
                file=sys.stderr,
            )
            return 1
        print(f"同步：{TARGET}")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(sql, encoding="utf-8")
    print(f"已写出 {TARGET}")
    print(f"数据表 {len(REGIONS) * len(PRODUCTS)} 张 = {len(REGIONS)} 大区 × {len(PRODUCTS)} 产品")
    return 0


if __name__ == "__main__":
    sys.exit(main())
