# power-data-sdk（电力数据中台统一接口 SDK）

面向电力数据中台的统一查询 SDK：**业务人员 3 行代码即可拿到标准化时序数据**。

```python
from powersdk import PowerDataClient

client = PowerDataClient()
df = client.query("load", region="广东", start="2025-01-01",
                  end="2025-01-02", granularity="hourly")
print(df.head())
#                    ts region      value unit
# 0 2025-01-01 00:00:00     广东  15447.747   MW
# 1 2025-01-01 01:00:00     广东  17549.725   MW
```

## 能力一览

| 能力 | 入口 |
| --- | --- |
| 11 类时序数据（天气 / 电价 / 负荷 / 新能源出力 / 风电 / 光伏 / 水电 / 煤价 / 用电量 / 电力缺口 / 预测） | `powersdk.categories()`、`Category` |
| 多时间粒度（raw / hourly / daily / monthly，含 `h`、`日`、`月` 等别名） | `client.query(..., granularity=...)`、`powersdk.resample()` |
| JSON 解析 + 时间戳自动补全 + 时区归一 | `powersdk.parse_json_records()`、`powersdk.complete_timestamps()` |
| 20+ 气象参数 → 插值方法映射表（配置与逻辑解耦） | `powersdk.INTERP_METHODS`、`powersdk.resolve_method()` |
| 向量化处理（10 万条/批，无行级循环） | `powersdk.vectorized_transform()` |
| 通用工具（重采样 / 补缺 / 透视 / 清洗 / 单位归一 / 记录导出） | `powersdk.utils` |
| 多环境配置（dev / test / prod） | `POWERSDK_ENV`、`powersdk.Settings` |
| 数据校验与告警（准确率阈值 0.998，可插拔 AlertSink） | `powersdk.validate()`、`powersdk.validate_and_alert()` |
| 地区索引（中文名 / 拼音 / 缩写 / 区划码互通） | `powersdk.default_region_index()` |
| 数据源抽象（内存源 / MySQL 源 / 自研 fake） | `powersdk.TimeSeriesSource` |

## 安装

```bash
pip install ./powersdk            # 或 pip install power-data-sdk
pip install ./powersdk[dev]       # 带 pytest
```

依赖：`pandas`、`numpy`、`PyMySQL`、`pypinyin`、`loguru`。
PyMySQL 只在真正连接 MySQL 时才导入，未安装时内存源照常可用。

## 常用片段

```python
# 1) 多粒度 + 多地区（拼音 / 缩写 / 区划码都能写）
df = client.query("solar_power", region=["广东", "jiangsu", "京"],
                  start="2025-01-01", end="2025-01-07", granularity="daily")

# 2) 气象参数（单位与插值方法来自配置表）
temp = client.query("weather", region="北京", param="temperature_2m", granularity="hourly")
rain = client.query("weather", region="北京", param="precipitation", granularity="daily")

# 3) 直接消费上游 JSON
records = client.query_json({"data": [{"时间": "05:00", "地区": "广东", "数值": 30000}]},
                            category="load")

# 4) 校验与告警
report = client.last_report
print(report.accuracy, [v.code for v in report.violations])
for alert in client.alerts:
    print(alert.level, alert.code, alert.message)

# 5) 换成 MySQL 数据源（离线环境自动给出可读报错）
from powersdk import MySQLTimeSeriesSource
mysql_client = PowerDataClient(source=MySQLTimeSeriesSource(dsn="mysql+pymysql://..."), env="prod")
print(mysql_client.healthcheck())
```

## 环境配置

`POWERSDK_ENV` 选择 `dev` / `test` / `prod`（未知值回退 `dev`），三套画像在
`powersdk.config.ENV_PROFILES` 中定义（库名、日志级别、批大小、准确率阈值）。
连接参数可用 `POWERSDK_MYSQL_HOST` / `POWERSDK_MYSQL_PORT` / `POWERSDK_MYSQL_USER` /
`POWERSDK_MYSQL_PASSWORD` / `POWERSDK_MYSQL_DATABASE` 逐项覆盖。

## 测试

```bash
cd powersdk && python -m pytest -q      # 25 个用例，全离线、确定性
```
