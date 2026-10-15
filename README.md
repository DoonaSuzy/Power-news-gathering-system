# Power News Aggregator · 电力新闻聚合与数据中台

> 采集全国 34 个省级行政区的电力新闻与市场行情，并提供统一查询 API 与 AI 摘要能力。
> 覆盖「采集 → 调度 → 存储 → 监控」全流程。

[![tests](https://img.shields.io/badge/tests-620%20passed-brightgreen)](#-开发)
[![python](https://img.shields.io/badge/python-%3E%3D3.11-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-lightgrey)](#-许可)

---

## 📖 项目概述

本项目把「分散在几十个站点的电力新闻与行情数据」聚合成一个可查询、可监控、可扩展的数据中台。

**现状一览**（实测）：

| 指标 | 值 |
| --- | ---: |
| 采集来源 | **60 个**（57 个适配器，59 个启用） |
| 已入库文章 | **1,707 篇**（84 生产库；本机 dev 库 1,274，口径见下注） |
| Python 代码 | **15,149 行**（`newsplatform/` + `powersdk/` 非空行） |
| 自动化测试 | **620 个（全通过）**（口径见下注） |
| 中台 API 端点 | **40 个**（口径见下注） |
| 配图资源 | **120 张**（跨文章公共元素过滤后；治理前 680 张 / 265 MB） |
| 气象数据表 | **26 张**（24 业务表 = 8 大区 × 3 数据产品，+ `wx_stations` / `wx_ingest_progress` 2 张辅助表） |

> **端点口径**（2026-10-15 实测，可复现）：`app.routes` 共 46 个 route object =
> 4 个框架路由（`/docs`、`/docs/oauth2-redirect`、`/redoc`、`/api/v1/openapi.json`）+
> 2 个非业务 APIRoute（`/metrics`、前端指路牌 `/`）+ **40 个业务端点**（`newsplatform/api/v1/`，10 个路由模块）。
> 唯一路径 **36 条**（`/sources`、`/sources/{source_id}`、`/admin/api-keys` 各有多个方法）。
> 复现：`python -c "from newsplatform.main import app; print(len(app.routes))"`。
>
> **测试口径**（2026-10-15 实测，可复现）：`pyproject.toml:63` 的 `testpaths` 含四个目录 ——
> `tests/` **540** + `powersdk/tests` 49 + `newsagg/finance/tests` 22 + `newsplatform/tests` 9 = **620**。
> 旧口径 580 只算了前两个目录（`cf877e9`「收回仅存在于虚拟机上的三批成果」把金融爬虫与监控体系并入仓库后未同步）；
> 611 → 620 是 2026-10-15 新增 `tests/test_sources_seed.py`（9 例，来源清单守卫回归）。
> 复现：`python -m pytest --junitxml=.pytest.xml`（`-q` 的 summary 行抓不到计数，必须用 junitxml）。
>
> **文章数口径**（2026-10-16 实测，可复现）：上表 **1,707 篇**取 **84 生产库**；
> 本机开发库为 **1,274 篇**（`python scripts/site_inventory.py`，脚本末尾自动闭合验算）。
> **演示对外数字前先确认连的是哪个库**，否则当场对不上。
>
> **代码量口径**：`newsplatform/` 7,898 + `powersdk/` 4,833 = 15,149 非空行；
> 若含采集层 `newsagg/` 与 `scripts/` 则为 **38,773 行**（全仓 `.py` 总行 52,732）。

### 核心能力

- **多源采集**：Scrapy 框架 + 57 个站点适配器，含反爬中间件、robots 合规、限速与租约调度
- **分布式调度**：Redis Stream 任务队列 + 租约心跳 + 三重预算熔断
- **数据中台**：FastAPI 提供 40 个端点（文章 / 来源 / 统计 / 摘要 / 行情 / 气象 / 公告）
- **AI 摘要**：服务端预生成 + 缓存 + 定时刷新（LLM 调用与访客量解耦）
- **市场行情**：6 个数据集（LNG 现货 / 挂牌 / 信号、ceci 进口煤 / PMMI、电力现货）
- **气象数据**：Open-Meteo 2,875 个站点，含配额核算与限流退避
- **数据交付**：CSV 双输出（带版本标识 `v<日期>-<毫秒>-<序号>` + 索引映射）
- **监控体系**：Prometheus + Grafana（含预置看板）
- **前端**：Vue 3 + Vite（新闻列表 / 详情 / AI 公告栏 / 实时检索）

---

## 🛠 技术栈（定型版本）

> 版本号取自 `requirements.txt` / `newsplatform/requirements.txt` / `web/package.json`，
> **不是大概齐**；基础设施版本取自 `docker-compose.yml` 的镜像标签。

### 后端 · 数据中台

| 组件 | 版本 | 用途 |
| --- | --- | --- |
| **Python** | **≥ 3.11** | 运行环境（生产用 3.12） |
| **FastAPI** | **0.115.6** | REST API 框架（40 个端点） |
| **Uvicorn[standard]** | **0.34.0** | ASGI 服务器 |
| **Pydantic** | **2.10.4** | 请求/响应校验（v2 风格） |
| **pydantic-settings** | **2.7.0** | 配置管理（12-factor） |
| **SQLAlchemy[asyncio]** | **2.0.43** | ORM（异步） |
| **aiomysql** | **0.2.0** | MySQL 异步驱动 |
| **structlog** | **24.4.0** | 结构化日志 |
| **prometheus-client** | **0.26.0** | 指标暴露（`/metrics`） |
| **httpx** | **0.28.1** | 异步 HTTP 客户端 |

### 采集 · 爬虫引擎

| 组件 | 版本 | 用途 |
| --- | --- | --- |
| **Scrapy** | **2.19.0** | 爬虫框架（57 个适配器） |
| **parsel** | **1.12.1** | HTML/XML 选择器（XPath/CSS） |
| **GNE** | **0.3.1** | 通用正文抽取（兜底） |
| **PyMySQL** | **1.2.3** | 同步 MySQL 驱动（采集侧） |
| **redis** | **8.1.0** | Redis 客户端（Stream 任务队列） |
| **python-dotenv** | **1.2.4** | `.env` 加载 |
| **tzdata** | **2026.5** | 时区数据库（CST 换算） |
| **Playwright** | **1.49.0** | 浏览器自动化（**仅客户端库**，连远端 CDP） |
| **boto3** | **≥ 1.34** | S3 客户端（配图上传 RustFS） |

### 前端 · Web UI

| 组件 | 版本 | 用途 |
| --- | --- | --- |
| **Vue** | **3.5.13** | 组合式 API（`<script setup>`） |
| **Vite** | **6.0.7** | 构建工具（dev server + 打包） |
| **@vitejs/plugin-vue** | **5.2.1** | SFC 编译 |

> 无 UI 组件库、无状态管理库 —— 单页轻量应用，**刻意不引**（多一个依赖多一份维护成本）。
> 生产由中台 `StaticFiles` 托管构建产物（同域免 CORS）。

### 基础设施（Docker 镜像）

| 组件 | 镜像标签 | 用途 |
| --- | --- | --- |
| **MySQL** | **8.4** | 采集库 `power_news` + 中台库 `powernews_platform` |
| **Redis** | **7-alpine** | 任务队列（Redis Stream + 租约） |
| **RustFS** | **rustfs/rustfs:1.0.1** | 对象存储（配图，S3 兼容） |
| **Prometheus** | **prom/prometheus:latest** | 指标采集 + 告警规则 |
| **Grafana** | **grafana/grafana:latest** | 可视化看板（已中文化 `zh-Hans`） |
| **Playwright** | **mcr.microsoft.com/playwright:v1.49.0-jammy** | CDP 浏览器通道（过瑞数 412） |

> **为什么对象存储用 RustFS 而不是 MinIO**：MinIO 社区版 2026-04-25 仓库永久归档、
> Docker Hub 镜像下线、CVE 不再修 —— 详见 `docs/对象存储选型-MinIO与RustFS对比.md`。

### 工具链

| 工具 | 版本 | 用途 |
| --- | --- | --- |
| **Ruff** | **0.17.0** | Lint + Format（替代 black/isort/flake8） |
| **pytest** | **9.1.1** | 单元测试（**620 个**） |
| **pytest-asyncio** | **1.4.0** | 异步测试支持 |
| **fakeredis** | **2.39.0** | Redis 测试替身（测试**不需要真 Redis**） |
| **freezegun** | **1.5.1** | 时间冻结（时间窗口逻辑测试） |
| **Docker / Compose** | ≥ 20.10 / v2 | 编排（10 个服务 + profile 分层） |

### 架构一句话

```
Scrapy 采集 → Redis Stream 队列 → Worker 消费 → MySQL 落库
                                      ↓
                          图片异步下载 → RustFS
                                      ↓
                    FastAPI 中台（40 端点）→ Vue3 前端（同域托管）
                                      ↓
                        Prometheus + Grafana 监控
```

**两个库职责不混**：`powernews`（采集库，`newsagg` 独占写）与
`powernews_platform`（中台库，存 `api_keys`/`audit_logs`/`stats_snapshots`）。

---


## 📁 目录结构

```
Power News Aggregator/
├── newsagg/                      # 采集层（Scrapy 框架 + 适配器 + 数据清洗）
│   ├── adapters/                 # 57 个站点适配器（按省份/类型分目录）
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
│   ├── weather/                  # 气象数据采集（26 张表）
│   ├── cleaning.py               # 正文清洗 / 时间解析 / 质量标记
│   ├── rawstore.py               # 原始响应落盘层（重洗的基础）
│   ├── export.py                 # CSV 导出（含版本标识）
│   ├── queue.py                  # Redis Stream 任务队列
│   ├── repo.py                   # 数据访问层
│   ├── schedule.py               # 调度中枢
│   └── cli.py                    # 命令行入口
│
├── newsplatform/                 # 数据中台（FastAPI）
│   ├── api/v1/                   # 40 个端点（10 个路由模块）
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
│   ├── tests/                    #   测试（49 个）
│   └── README.md                 #   SDK 独立文档
│
├── web/                          # 前端（Vue 3 + Vite）
│   └── src/{components,composables,api,utils,styles}/
│
├── scripts/                      # 43 个辅助脚本（41 .py + 2 .sh）
│   ├── site_inventory.py         #   站点采集清单 + 口径验算
│   ├── crawl_report.py           #   采集报告 + 结构化导出
│   ├── sync_sources.py           #   来源清单同步到库
│   ├── import_collected.py       #   浏览器采集结果导入
│   ├── rewash.py                 #   重洗：用当前规则重跑已存原文
│   ├── verify_market.py          #   行情数据校验
│   └── e2e_smoke.py              #   中台接口冒烟
│
├── sql/                          # 建表 SQL（schema.sql 等）
├── tests/                        # 测试（531 个）
├── docs/                         # 文档（75 份，含 34 份省级攻克日志；按 .gitignore 不入库）
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
├── docker-compose.yml            # 10 个服务编排（mysql/redis/fastapi/scheduler/worker/backfill/prometheus/grafana/rustfs/browser）
├── pyproject.toml                # 项目元信息 + 依赖 + Ruff/pytest 配置
├── requirements.txt              # 采集层依赖
└── .env.example                  # 环境变量模板
```

---

## 🌐 访问地址（实测可用）

> 全部地址于 **2026-10-13 21:10** 实测；状态列为当时的 HTTP 状态码。
> 主机：`192.168.101.84`（Ubuntu 22.04）—— 9 个容器全部 `Up`（第 10 个服务 `backfill` 属 `backfill` profile，按需启动，非常驻）

### 生产环境（192.168.101.84）

| 服务 | 地址 | 状态 | 说明 |
| --- | --- | --- | --- |
| **数据中台 API** | http://192.168.101.84:8080 | ✅ 200 | 40 个端点，除健康检查外需 Bearer Token |
| **Swagger 文档** | http://192.168.101.84:8080/docs | ✅ 200 | 可交互调试（已修 Authorize 按钮） |
| **ReDoc 文档** | http://192.168.101.84:8080/redoc | ✅ 200 | 阅读版 API 文档 |
| **健康检查** | http://192.168.101.84:8080/api/v1/health | ✅ 200 | **免鉴权**，探活首选 |
| **Prometheus 指标** | http://192.168.101.84:8080/metrics | ✅ 200 | 应用侧指标（scrape 目标） |
| **Grafana 看板** | http://192.168.101.84:3000 | ✅ 200 | 已中文化（`zh-Hans`），11 个面板 |
| **Prometheus** | http://192.168.101.84:9090 | ✅ 200 | 指标存储 + 4 条告警规则 |
| **RustFS 管理台** | http://192.168.101.84:9001 | 🔒 需登录 | 对象存储控制台（配图存储） |
| **RustFS S3 端点** | http://192.168.101.84:9000 | 🔒 403 | S3 兼容 API（根路径无权限属正常） |
| **浏览器 CDP 通道** | ws://192.168.101.84:3010 | ✅ 200 | Playwright CDP（过瑞数 412 用） |
| MySQL | 192.168.101.84:3306 | — | 采集库 `power_news` + 中台库 `powernews_platform` |
| SSH | ssh://zhu@192.168.101.84:22 | — | 运维入口 |

**配图直链格式**：`http://192.168.101.84:9000/powernews-images/<object_key>`
（如 `articles/a2/72/a2720e62....png`）—— 桶已设公开读策略，可直接访问。

### 前端

**生产环境：已由中台托管** —— 直接访问 `http://192.168.101.84:8080/` 即为前端页面。

| 场景 | 地址 | 说明 |
| --- | --- | --- |
| **生产（84）** | **http://192.168.101.84:8080/** | 中台 `StaticFiles` 托管 `web/dist`，**同域免 CORS** |
| 开发服务器 | http://127.0.0.1:5173 | `cd web && VITE_API_TARGET=http://192.168.101.84:8080 npx vite` |
| 构建产物预览 | http://127.0.0.1:4173 | `cd web && npx vite preview` |
| 本地中台 | http://127.0.0.1:8123 | 独立开发用 |

**为什么由中台托管而不是单独起 Nginx**：
1. **同域** → 前端 `/api/v1/...` 直接命中同源路由，天然免 CORS；
2. **不引新镜像** → 84 拉 Docker Hub 极慢（实测 `nginx:alpine` 三分钟没下来）；
3. **一个入口** → 用户只记 8080 一个端口。

**SPA 路由**由 `newsplatform/main.py` 的 `_SPAStaticFiles` 处理：
访问 `/some/route` 时磁盘上不存在该文件 → **回退到 `index.html`** 交给前端路由。

> ⚠️ `web/dist` 是构建产物、**不入版本库** → 部署时需**单独传输**
> （`git archive` 打不出来）。

> 前端开发时中台必须可达，否则页面顶部会显示「中台不可达」。

### 关键 API 端点

| 端点 | 用途 | 鉴权 |
| --- | --- | --- |
| `GET /api/v1/health` | 健康检查（探活首选） | **免** |
| `GET /api/v1/articles` | 文章列表（分页 / 时间窗 / 省份 / 类别筛选） | 需 |
| `GET /api/v1/articles/{id}` | 文章详情（正文 + **配图 `images[]`**） | 需 |
| `GET /api/v1/articles/count` | 命中条数（导出前预览） | 需 |
| `GET /api/v1/articles/export` | 导出 CSV（UTF-8 BOM） | 需 |
| `GET /api/v1/sources` | 来源清单（含生效配置） | 需 |
| `GET /api/v1/stats/*` | 统计（覆盖 / 时序 / 快照） | 需 |
| `GET /api/v1/news/summary` | AI 摘要（服务端缓存） | 需 |
| `GET /metrics` | Prometheus 指标 | **免** |
| `GET /docs` · `/redoc` | API 文档 | **免** |

### ⚠️ 访问两个坑

1. **本机有 HTTP 代理** → `curl 127.0.0.1` / `curl 192.168.101.84` 默认走代理，
   返回 **502**（看着像服务挂了，实际是代理拦的）。
   - 命令行：必须加 `--noproxy '*'`
   - 浏览器：在代理设置里排除 `127.0.0.1` / `localhost` / `192.168.101.84`
   - Python（httpx）：`trust_env=False` 或清空 `HTTP_PROXY`
2. **除健康检查与文档外都需 Bearer Token**：
   ```
   Authorization: Bearer pn_xxxxxxxx
   ```
   令牌从 `.env` 的引导配置获取，或查中台库 `api_keys` 表。

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

# ② 单元测试：仅 tests/ 目录期望 540 passed
python -m pytest tests -q

#    四个 testpaths 全量期望 620 passed（tests/ 540 + powersdk/tests 49
#    + newsagg/finance/tests 22 + newsplatform/tests 9）
python -m pytest --junitxml=.pytest.xml

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
| 采集框架（Scrapy + 租约调度 + 去重） | ✅ 57 个适配器 |
| **原始响应落盘层**（`newsagg/rawstore.py`） | ✅ **已实现**（`POWERNEWS_KEEP_RAW=true` 开启） |
| **重洗工具**（`scripts/rewash.py`） | ✅ **已实现**（改规则重跑解析，不重抓） |
| 数据中台（FastAPI） | ✅ 40 个端点 |
| 市场行情（6 个数据集） | ✅ |
| 气象数据（26 张表 / 2,875 站点） | ✅ |
| AI 公告栏（服务端预生成 + 缓存 + 定时刷新） | ✅ |
| CSV 双输出（含版本标识 + 索引） | ✅ |
| 前端（Vue 3） | ✅ |
| 部署（Docker Compose 10 服务） | ✅ |
| 监控（Prometheus + Grafana + 预置看板） | ✅ |
| 图片对象存储（RustFS，S3 兼容；配图经四层过滤） | ✅ |
| 港澳台 3 源（中华电力 / 澳电 / 台电） | ✅ |
| 测试（620 个） | ✅ 全通过 |
| 文档（75 份） | ✅ |

### ⏳ 待开发

| 优先级 | 项 | 说明 |
| --- | --- | --- |
| P2 | 中国华能列表页 | Liferay 门户，列表 JS 渲染（静态抓取 0 条），待走浏览器通道或定位数据接口 |
| P2 | 节点注册表 | 有租约 + 心跳续租，无节点清单 / 容量视图 |

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
| `docs/采集器全量清单.md` | 57 个适配器 + 源文件路径 |
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
