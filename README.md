# Power News Aggregator · 电力新闻聚合与数据中台

> 采集全国 34 个省级行政区的电力新闻与市场行情，并提供统一查询 API 与 AI 摘要能力。
> 覆盖「采集 → 调度 → 存储 → 监控」全流程。

[![tests](https://img.shields.io/badge/tests-431%20passed-brightgreen)](#-开发)
[![python](https://img.shields.io/badge/python-%3E%3D3.11-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-lightgrey)](#-许可)

---

## 📖 项目概述

本项目把「分散在几十个站点的电力新闻与行情数据」聚合成一个可查询、可监控、可扩展的数据中台。

**现状一览**（实测）：

| 指标 | 值 |
| --- | ---: |
| 采集来源 | **54 个**（51 个适配器） |
| 已入库文章 | **1,274 篇** |
| Python 代码 | **15,298 行** |
| 自动化测试 | **431 个（全通过）** |
| 中台 API 端点 | **39 个** |
| 气象数据表 | **24 张**（8 大区 × 3 数据产品） |

### 核心能力

- **多源采集**：Scrapy 框架 + 51 个站点适配器，含反爬中间件、robots 合规、限速与租约调度
- **分布式调度**：Redis Stream 任务队列 + 租约心跳 + 三重预算熔断
- **数据中台**：FastAPI 提供 39 个端点（文章 / 来源 / 统计 / 摘要 / 行情 / 气象 / 公告）
- **AI 摘要**：服务端预生成 + 缓存 + 定时刷新（LLM 调用与访客量解耦）
- **市场行情**：6 个数据集（LNG 现货 / 挂牌 / 信号、ceci 进口煤 / PMMI、电力现货）
- **气象数据**：Open-Meteo 2,875 个站点，含配额核算与限流退避
- **数据交付**：CSV 双输出（带版本标识 `v<日期>-<毫秒>-<序号>` + 索引映射）
- **监控体系**：Prometheus + Grafana（含预置看板）
- **前端**：Vue 3 + Vite（新闻列表 / 详情 / AI 公告栏 / 实时检索）

---

## 📁 目录结构

```
Power News Aggregator/
├── newsagg/                      # 采集层（Scrapy 框架 + 适配器 + 数据清洗）
│   ├── adapters/                 # 51 个站点适配器（按省份/类型分目录）
│   │   ├── base.py               #   BaseAdapter 抽象契约
│   │   ├── _common.py            #   HeuristicAdapter 通用启发式
│   │   ├── sgcc_generic.py       #   国网站群模板 + build_sgcc_adapter()
│   │   ├── gov_generic.py        #   政府主管部门模板
│   │   ├── registry.py           #   注册中心 + ADAPTER_MODULES
│   │   ├── national/             #   全国性来源（行业媒体 + 央企）
│   │   ├── gov/                  #   政府能源主管部门
│   │   └── <省份拼音>/            #   各省国网/南网公司
│   ├── crawl/                    # Scrapy 蜘蛛、引擎、中间件、租约、去重
│   ├── market/                   # 市场行情采集（6 个数据集）
│   ├── weather/                  # 气象数据采集（24 张表）
│   ├── cleaning.py               # 正文清洗 / 时间解析 / 质量标记
│   ├── rawstore.py               # 原始响应落盘层（重洗的基础）
│   ├── export.py                 # CSV 导出（含版本标识）
│   ├── queue.py                  # Redis Stream 任务队列
│   ├── repo.py                   # 数据访问层
│   ├── schedule.py               # 调度中枢
│   └── cli.py                    # 命令行入口
│
├── newsplatform/                 # 数据中台（FastAPI）
│   ├── api/v1/                   # 39 个端点（12 个模块）
│   ├── core/                     # 配置 / 鉴权 / 限流 / 审计 / 缓存 / 指标
│   ├── models/                   # ORM 模型
│   ├── repositories/             # 数据访问层
│   ├── schemas/                  # Pydantic 请求/响应模型
│   ├── services/                 # 业务逻辑（含 AI 公告生成）
│   ├── tasks/                    # 后台任务
│   ├── main.py                   # 应用入口（含 lifespan 预热）
│   └── Dockerfile
│
├── powersdk/                     # 电力数据中台统一接口 SDK（独立 pip 包）
│   ├── client.py                 #   PowerDataClient（12 类时序数据）
│   ├── interpolation.py          #   52 个气象参数插值方法映射表
│   ├── parsing.py                #   JSON 解析 + 时间戳补全
│   ├── pinyin.py                 #   地区索引（中文/拼音/缩写/区划码）
│   ├── http_source.py            #   数据源抽象（内存/MySQL/fake）
│   └── README.md                 #   SDK 独立文档
│
├── web/                          # 前端（Vue 3 + Vite）
│   └── src/{components,composables,api,utils,styles}/
│
├── scripts/                      # 31 个辅助脚本
│   ├── site_inventory.py         #   站点采集清单 + 口径验算
│   ├── crawl_report.py           #   采集报告 + 结构化导出
│   ├── sync_sources.py           #   来源清单同步到库
│   ├── import_collected.py       #   浏览器采集结果导入
│   ├── rewash.py                 #   重洗：用当前规则重跑已存原文
│   ├── verify_market.py          #   行情数据校验
│   └── e2e_smoke.py              #   中台接口冒烟
│
├── sql/                          # 建表 SQL（schema.sql 等）
├── tests/                        # 测试（431 个）
├── docs/                         # 文档（20+ 篇）
│   ├── 数据中台运行手册.md
│   ├── 省份攻克日志/              #   按省份归档的采集攻克过程
│   ├── 采集器全量清单.md
│   └── 逆向报告-*.md              #   站点接口逆向分析
├── deploy/                       # 部署配置
│   ├── prometheus/               #   Prometheus 采集配置
│   └── grafana/                  #   Grafana 数据源 + 看板
│
├── data/                         # 运行期数据（不入版本库）
│   ├── csv/                      #   CSV 双输出产物
│   └── collected/                #   浏览器采集中间产物
│
├── docker-compose.yml            # 8 个服务编排（mysql/redis/fastapi/scheduler/worker/backfill/prometheus/grafana）
├── pyproject.toml                # 项目元信息 + 依赖 + Ruff/pytest 配置
├── requirements.txt              # 采集层依赖
└── .env.example                  # 环境变量模板
```

---

## 🚀 环境配置

### 前置要求

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Python | **≥ 3.11** | `pyproject.toml` 声明 |
| MySQL | 8.0+ | 字符集 `utf8mb4` |
| Redis | 6.0+ | **必须支持 Redis Stream** |
| Node.js | 20+ | 仅前端需要 |
| Docker | 可选 | 用 compose 一键起全栈 |

> **Redis 注意**：项目用 Redis Stream 做任务队列，**6.0 以下不支持**。
> 若本机已有其他 Redis 占用默认端口，可另起一个（见下方启动命令）。

### 步骤 1 · 获取代码与依赖

```bash
git clone <repo-url>
cd "Power News Aggregator"

# 推荐用 uv（`pip` 亦可）
python -m venv .venv
source .venv/Scripts/activate      # Windows Git Bash
# .venv/bin/activate               # Linux / macOS

pip install -r requirements.txt -r newsplatform/requirements.txt
pip install -e ./powersdk          # 可选：数据 SDK
```

### 步骤 2 · 配置环境变量

```bash
cp .env.example .env
```

**必填项**（`.env`）：

```bash
# ---- MySQL（两个库，职责不混）----
POWERNEWS_MYSQL_HOST=127.0.0.1
POWERNEWS_MYSQL_PORT=3306
POWERNEWS_MYSQL_USER=powernews
POWERNEWS_MYSQL_PASSWORD=<your-password>
POWERNEWS_MYSQL_DATABASE=powernews                    # 采集库
POWERNEWS_PLATFORM_COLLECTOR_DATABASE_URL=mysql://... # 中台读采集库（**必须显式配**）

# ---- Redis ----
POWERNEWS_PLATFORM_REDIS_URL=redis://127.0.0.1:6380/1

# ---- 中台 ----
POWERNEWS_PLATFORM_SECRET_KEY=<random-string>         # 生产必须覆盖默认值

# ---- LLM（可选，用于 AI 公告栏 / 摘要）----
POWERNEWS_PLATFORM_LLM_BASE_URL=https://openrouter.ai/api/v1/chat/completions
POWERNEWS_PLATFORM_LLM_API_KEY=<your-key>
POWERNEWS_PLATFORM_LLM_MODEL=<model-id>
POWERNEWS_PLATFORM_LLM_MAX_TOKENS=16000
POWERNEWS_NOTICE_INTERVAL_SECONDS=7200               # 公告刷新间隔（秒）
```

> ⚠️ **两个易踩的坑**：
> 1. `POWERNEWS_PLATFORM_COLLECTOR_DATABASE_URL` **漏配会回落到源码里的占位口令**，
>    表现为「中台能起但查不到文章」。
> 2. 源码中**不写真实口令**，只用 `CHANGE_ME` 占位 + 环境变量注入。

### 步骤 3 · 初始化数据库

```bash
# 建库建表 + 灌入省份与来源清单
python -m newsagg init-db

# 校验来源清单与适配器一致性
python -m newsagg validate
python -m newsagg adapters
```

---

## ▶️ 运行指南

### 启动全栈（Docker，推荐）

```bash
docker compose up -d
# 服务：mysql / redis / fastapi / scheduler / worker / backfill / prometheus / grafana
```

| 服务 | 地址 |
| --- | --- |
| 中台 API | http://127.0.0.1:8123 |
| Swagger 文档 | http://127.0.0.1:8123/docs |
| Prometheus | http://127.0.0.1:9090 |
| Grafana | http://127.0.0.1:3000 |

### 本地开发启动

```bash
# ① Redis（若默认端口被占用，另起一个；**必须与 uvicorn 放同一后台任务**）
./.redis/redis-server.exe --port 6380 --dir .redis --save "" --appendonly no &

# ② 中台
python -m uvicorn newsplatform.main:app --host 127.0.0.1 --port 8123 --reload

# ③ 前端
cd web && npm install && npm run dev     # http://127.0.0.1:5173
```

> ⚠️ **本机有 HTTP 代理时**：`curl 127.0.0.1:8123` 会走代理返回 **502**（看着像服务挂了）。
> 必须加 `--noproxy '*'`；浏览器需在代理设置里排除 `127.0.0.1` / `localhost`。

### 采集操作

```bash
# 跑一轮调度（按各源间隔建批 + 投递）
python -m newsagg schedule

# 手工建批并立即执行（指定来源 / 近 30 天）
python -m newsagg crawl-once --source cpnn --days 30 --exec

# 起 worker
python -m newsagg worker
python -m newsagg supervisor --workers 4     # 多进程

# 查看统计
python -m newsagg stats
python -m newsagg sources
```

### 数据查询与导出

```bash
# 分页检索
python -m newsagg query --range week --limit 20

# 查看单篇全文
python -m newsagg show 63

# 导出 CSV（UTF-8 BOM，Excel 直开）
python -m newsagg export --since 2026-10-01 --out exports/oct.csv

# 站点采集清单 + 口径验算
python scripts/site_inventory.py --csv data/reports/site-inventory.csv

# 采集报告 + 结构化数据集
python scripts/crawl_report.py --days 30
```

### 中台接口示例

```bash
TOKEN=<your-api-key>

# 文章检索
curl --noproxy '*' -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8123/api/v1/articles?range=week&limit=10"

# AI 摘要（服务端缓存）
curl --noproxy '*' -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8123/api/v1/news/summary?range=week"

# 公告栏（读缓存，不触发 LLM）
curl --noproxy '*' -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8123/api/v1/notices"
```

### 数据 SDK

```python
from powersdk import PowerDataClient

client = PowerDataClient()
df = client.query("load", region="广东", start="2025-01-01",
                  end="2025-01-02", granularity="hourly")
print(df.head())
```

---

## 🧪 开发

### 质量门禁

```bash
# ① 静态检查（期望 All checks passed!）
ruff check .

# ② 单元测试（期望 431 passed；**必须用 python -m pytest**）
python -m pytest tests -q

# ③ 来源清单与适配器一致性
python -m newsagg validate

# ④ 行情数据校验（需真实网络 + 采集库）
python scripts/verify_market.py --periods 3

# ⑤ 中台接口冒烟（需 Redis + uvicorn）
NO_PROXY=127.0.0.1,localhost HTTP_PROXY= HTTPS_PROXY= \
  python scripts/e2e_smoke.py --write
```

> ⚠️ **必须用 `python -m pytest`**：直接调 `pytest` 会解析到系统 Python
> 且 cwd 不进 `sys.path` → 报 `No module named 'newsagg'`。

### 新增一个采集站点

1. 在 `newsagg/sources_seed.py` 的 `ADDITIONAL_SOURCES` 加一条来源定义
2. 在 `newsagg/adapters/<分组>/` 建 `<source_id>.py`，声明 `list_url` / `article_pattern` / `body_selectors`
3. 在 `newsagg/adapters/registry.py` 的 `ADAPTER_MODULES` 登记模块名
4. 同步到库：`python scripts/sync_sources.py`
5. 验证：`python -m newsagg adapters` + 单测
6. 采集：`python -m newsagg crawl-once --source <id> --days 30 --exec`
7. **按约定更新 `docs/省份攻克日志/`**（详见该目录的 README）

---

## 📊 项目当前状态

**版本**：`0.1.0`

### ✅ 已完成

| 模块 | 状态 |
| --- | --- |
| 采集框架（Scrapy + 租约调度 + 去重） | ✅ 51 个适配器 |
| **原始响应落盘层**（`newsagg/rawstore.py`） | ✅ **已实现**（`POWERNEWS_KEEP_RAW=true` 开启） |
| **重洗工具**（`scripts/rewash.py`） | ✅ **已实现**（改规则重跑解析，不重抓） |
| 数据中台（FastAPI） | ✅ 39 个端点 |
| 市场行情（6 个数据集） | ✅ |
| 气象数据（24 张表 / 2,875 站点） | ✅ |
| AI 公告栏（服务端预生成 + 缓存 + 定时刷新） | ✅ |
| CSV 双输出（含版本标识 + 索引） | ✅ |
| 前端（Vue 3） | ✅ |
| 部署（Docker Compose 8 服务） | ✅ |
| 监控（Prometheus + Grafana + 预置看板） | ✅ |
| 测试（431 个） | ✅ 全通过 |
| 文档（20+ 篇） | ✅ |

### ⏳ 待开发

| 优先级 | 项 | 说明 |
| --- | --- | --- |
| P2 | MinIO 图片存储 | 目前只做文本存储 |
| P2 | 节点注册表 | 有租约 + 心跳，无节点清单视图 |
| P2 | 港澳台 3 源适配 | 澳门 / 香港 / 台湾电网公司 |
| P2 | 中国华能适配器 | 该站列表为 JS 渲染 |

### 📌 已知风险

- **原始层默认关闭** → 不开 `POWERNEWS_KEEP_RAW=true` 就没有重洗能力
- **日均采集量未持续验证** → 需调度器长期运行

---

## 📚 文档索引

| 文档 | 内容 |
| --- | --- |
| `docs/数据中台运行手册.md` | 部署 / 运维 / 排查 |
| `docs/数据中台表设计.md` | 数据库表结构 |
| `docs/项目进度总结报告.md` | 各模块进展与待办（对照简历） |
| `docs/采集器全量清单.md` | 51 个适配器 + 源文件路径 |
| `docs/爬虫数据存储方案对比.md` | 先落盘 vs 先入库的取舍 |
| `docs/分表存储方案设计与可行性评估.md` | 分表方案设计与实测评估 |
| `docs/采集CSV双输出说明.md` | CSV 双输出 + 版本标识机制 |
| `docs/省份攻克日志/` | 按省份归档的采集攻克过程 |
| `docs/逆向报告-*.md` | 站点接口逆向分析 |
| `docs/浏览器采集通道操作手册.md` | Scrapy 抓不到时的备用通道 |
| `powersdk/README.md` | 数据 SDK 使用说明 |

---

## 🤝 贡献指南

- 提交前跑通「质量门禁」四项（ruff / pytest / validate / adapters）
- 适配器变更后**同步更新** `docs/省份攻克日志/` 对应分册
- 涉及数值边界的接口（如 `Field(le=...)`）必须与 DDL 列容量对齐
- 统计类结论用 `scripts/` 下的脚本产出，**不口述数字**

---

## 📄 许可

[MIT](https://opensource.org/licenses/MIT)
