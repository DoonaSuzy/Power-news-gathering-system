# 电力新闻资讯前端

Vue3 组合式 API + Vite。对接本仓库的中台（`newsplatform`，默认 `127.0.0.1:8123`）。

## 快速开始

```bash
cd web
cp .env.example .env.local     # 按需填 VITE_API_TOKEN

npm install
npm run dev                    # http://127.0.0.1:5173
```

中台要同时起着（否则页面顶部会显示「中台不可达」）：

```bash
cd ..
./.redis/redis-server.exe --port 6380 --dir .redis --save "" --appendonly no &
NO_PROXY=127.0.0.1,localhost \
  C:/Users/10911/.workbuddy/binaries/python/envs/powernews/Scripts/python.exe \
  -m uvicorn newsplatform.main:app --host 127.0.0.1 --port 8123
wait   # 末尾必须加，否则命令一结束子进程被回收
```

首次打开点右上角**设置**填 API Key。本机可直接用 `.workbuddy/e2e_token` 里缓存的值。

## 需求对照

| # | 需求 | 落点 |
|---|---|---|
| 1 | 按地区 / 主题筛选 | `components/FilterBar.vue` + `utils/topics.js`（12 个主题 × 8 大区 × 34 省） |
| 2 | AI 数据公告栏（滚动 + 置顶） | `components/NoticeBoard.vue`（置顶区固定 + 无缝滚动 + 悬停暂停） |
| 3 | AI 辅助功能 | 三项，**全部手动触发**（见下） |
| 3.1 | ~~AI 新闻朗读~~ | **已移除**（见「变更记录」） |
| 3.2 | AI 智能解析 | `ai.explain()` |
| 3.3 | AI 要点总结 | `ai.summarize()` |
| 4 | 精细化多维度自动分类 | 两级：列表用 `utils/lexicon.js` 就地打标；详情用 `ai.tag()` |
| 5 | 每 2 小时自动拉取 | `composables/usePolling.js` |
| 6 | 组合式 API / 轻量 / AI 预留对接位 | `api/ai.js` 的 Provider 契约 + `api/ai-llm.js` 标准大模型接口 |

## 目录结构

```
web/
├── src/
│   ├── api/
│   │   ├── client.js       统一请求（token / 超时 / 解包 / 错误）
│   │   ├── news.js         新闻接口（字段名转 camelCase）
│   │   ├── ai.js           ★ AI 能力标准接口层（唯一知道「AI 从哪来」的地方）
│   │   └── ai-local.js     本地规则降级实现
│   ├── composables/
│   │   ├── usePolling.js   ★ 周期任务调度
│   │   ├── useNewsFeed.js  列表 + 筛选 + 分页（含竞态令牌）
│   │   └── useAiNotices.js 公告数据
│   ├── components/         FilterBar / NoticeBoard / NewsList / ArticleDetail
│   ├── utils/
│   │   ├── lexicon.js      领域词表（主题 / 主体 / 属性 / 术语）
│   │   ├── topics.js       筛选维度预设
│   │   └── notices.js      AI 结果 → 公告条目映射
│   └── styles/main.css     CSS 变量设计基座
```

依赖只有 **vue / vite / @vitejs/plugin-vue** 三个。没有 UI 框架、没有状态库、
没有 axios、没有 router —— 交互只有筛选、列表、抽屉、公告栏四件事，
引框架的收益低于它的体积与学习成本。

## 一、AI 接口对接（需求 6 的核心）

`src/api/ai.js` 是全项目**唯一**知道「AI 从哪来」的地方。组件只调
`ai.summarize()` / `ai.explain()` / `ai.tag()` / `ai.notices()`。

### 统一契约

| 方法 | 用途 | 输入 | 输出关键字段 |
|---|---|---|---|
| `summarize(ctx)` | 要点总结 | `{articles, window, filters, maxPoints}` | `headline` / `keyPoints[]` / `highlights[]` / `keywords[]` / `method` / `caveats[]` |
| `explain(ctx)` | 智能解析 | `{article}` | `paragraphs[{role,text}]` / `entities[]` / `terms[{term,gloss}]` |
| `tag(ctx)` | 多维度标签 | `{article}` 或 `{articles}` | `dimensions{维度:[词]}` / `labels[]` |
| `notices(ctx)` | 公告栏数据 | 同上 | `notices[{pinned,kind,text,reasons}]` |
| `available()` | 能力探活 | — | `boolean` |
| `supports(method, ctx)` | **按上下文**判断某次调用能否交给本 provider | — | `boolean`（可选，缺省退回 `capabilities`） |

## AI 触发规则（需求 4）

**详情页的 AI 功能全部由用户点击按钮触发，没有任何自动触发**：

- 进入详情页只加载正文，**不**预跑任何 AI；
- 「AI 智能解析」「AI 要点总结」「AI 多维标签」三个按钮各自独立调用；
- 组件没有 `watch` 监听会触发 AI（切换文章只重置状态）。

**唯一例外是公告栏**：它按需求 2 与需求 5 在定时任务与筛选变化时自动刷新。
那是「列表级」的数据展示，不是「解析某篇新闻」—— 两者刻意分开，
别把公告栏的自动刷新误判成「AI 自动解析」。

### 为什么需要 `supports(method, ctx)` 而不只是静态能力表

静态表只能表达「这个 provider 有没有某能力」，表达不了「**这次**调用适不适合它」。
实测踩到的两个坑：

1. **中台只有窗口级总结端点**。详情页要总结**一篇**，若仍交给中台，传该篇日期当窗口，
   返回的是那一天**全部**新闻的要点 —— 标题写着「AI 要点总结」，内容是
   「近1周共 307 篇电力新闻」。形状合法、语义全错。
2. **中台没有单篇解析 / 打标端点**。早先用「当天窗口」近似，返回一堆与这篇无关的要点。

所以 `HttpAiProvider.supports()` 会在 `articles.length <= 1` 时拒绝 `summarize`，
并把 `explain` / `tag` 一律交给本地。**降级原因会写进 `caveats` 并在 UI 显示**，
例如「中台 AI不适用于本次「summarize」调用（输入是单篇（1 篇），中台只有窗口级总结端点），
已由本地规则完成」。

### 接一个新 AI 服务要改什么

**只改一个文件**：

```js
// src/api/ai.js
export class MyVendorProvider {
  constructor() { this.name = '某云 AI' }

  async available() { /* 探活，返回 boolean */ }

  async summarize(ctx) {
    const raw = await myVendor.call(...)
    return { headline, keyPoints, highlights, keywords, method: 'llm', caveats: [], provider: this.name }
  }
  async explain(ctx) { /* ...同形状... */ }
  async tag(ctx)      { /* ... */ }
  async notices(ctx)  { /* ... */ }
}

const PROVIDERS = {
  http:  () => new HttpAiProvider(),      // 中台
  local: () => new LocalAiProvider(),     // 本地规则
  myvendor: () => new MyVendorProvider(), // ← 新增一行
}
```

再把 `.env.local` 的 `VITE_AI_PROVIDER` 改成 `myvendor` 即可。
**组件、composable、模板一行都不用动。**

### 为什么默认是 `auto` 而不是直连 AI

「AI 不可用」在实践中一定发生：没配 Key、后端没起、额度用尽、上游 5xx。
只认 HTTP 的实现在这四种情况下 AI 功能会**整体消失**。所以 `auto` 模式：
中台优先 → 失败降级本地规则 → 再退到「仅统计」。三层保证功能不消失，
且结果里的 `method` 字段会如实写明走的是哪条路（`llm` / `extractive` /
`local-rule`），UI 直接展示 —— **不把本地规则的结果说成 AI**。

## 二、定时任务机制（需求 5）

裸 `setInterval(task, 7200000)` 在真实页面里有三个必然失效场景，
`usePolling.js` 逐个解决：

| 场景 | 现象 | 本实现的处理 |
|---|---|---|
| 后台标签页被节流/冻结 | 定时器不触发，任务漏跑且不会补 | 用 **1 秒心跳 + 绝对时刻 `nextRunAt`** 驱动；恢复后第一次心跳就发现 `now >= nextRunAt` 并立即补跑 |
| 页面重开即失忆 | 关页 3 小时后打开，还要再等 2 小时 | `lastRunAt` 存 localStorage，重开时按「上次 + 周期」算，已过期立即拉 |
| 一次失败就断链 | 网络抖动后要等满整个周期 | 失败按 30s → 1m → 2m → … 指数退避，成功清零，上限 30 分钟 |

另外：卸载时 `clearInterval` + 移除 `visibilitychange` 监听
（HMR 下不清理会叠加出多个心跳）；`running` 标志防重入。

界面左下角的「定时任务」面板实时显示：已执行次数、上次执行时刻、
连续失败次数、下次执行倒计时 —— 调度是否真的在跑，一眼可见，不用猜。

## 三、两个容易踩的前后端约定

**1. `range` 与 `since`/`until` 在中台是互斥的**，同时传会 400。
`utils/topics.js` 的 `toQueryParams` 只发一种；「不限时间」档两者都不发。

**2. 地区维度不做前端展开**。中台的 `province` 参数本身就接受
「省代码 / 全称 / 简称 / 大区名」，传「华南」它会解析成该大区下全部省份。
前端自己映射一遍必然会与中台的区划定义漂移。

## 四、开发态为什么要走 proxy

`vite.config.js` 把 `/api` 代理到 `127.0.0.1:8123`，而不是让前端直连：

1. 跨端口会触发 CORS 预检，中台的 CORS 白名单要额外加 dev 端口；
2. 本机常驻 HTTP 代理，`127.0.0.1` 不排除的话请求会被代理拦成 502；
3. 生产同域部署时 `/api` 由网关转发，前端代码一行都不用改。

## 大模型联调（需求 2）

**已于 2026-10-12 联调通过**，接入方为 OpenRouter（OpenAI 兼容协议）。
配置在 `web/.env.local`（已在 .gitignore，不入库）：

```bash
VITE_AI_PROVIDER=llm
VITE_LLM_API_URL=https://openrouter.ai/api/v1/chat/completions
VITE_LLM_API_KEY=sk-or-v1-...
VITE_LLM_MODEL=nvidia/nemotron-3-super-120b-a12b:free
VITE_LLM_TIMEOUT_MS=60000
VITE_LLM_MAX_TOKENS=8000
```

### 实测结果

| 项 | 结果 |
|---|---|
| HTTP | 200，`cost: 0`（免费模型） |
| 耗时 | 单篇约 4–15 秒（推理型模型，`reasoning_tokens` 占大头） |
| 要点总结 | headline + 3–4 条要点，均为模型凝练的陈述句 |
| 智能解析 | 逐段角色（导语/正文）+ **模型生成的术语词典** |
| 多维度标签 | 主题/主体/属性三维 |
| 公告栏 | 置顶 2 条 + 滚动 7 条，全部为模型表述 |

换供应商只需改 `.env.local` 三个变量，**不改代码**。

### 联调中修掉的三个 bug

1. **`resolveChain()` 漏了 `llm` 分支** —— `VITE_AI_PROVIDER=llm` 会落到 auto 链，
   表现为「明明配了大模型，界面却显示中台 AI / 本地规则」，而 env 注入看着完全正常。
2. **`raw.slice(cap)` 应为 `slice(0, cap)`** —— 前者是「从第 cap 个取到末尾」，
   对 4 条要点、cap=6 得到**空数组**：headline 有、要点全没，而模型明明返回了 4 条。
3. **`REQUIRED` 是全局的** —— `explain` 返回 `paragraphs/entities/terms`、
   `tag` 返回 `dimensions`，都被判「缺少 headline/key_points」而降级到本地规则，
   表现是「总结走大模型，解析和打标偷偷走本地」。已改为按能力分别声明。

另外两处口径修正：
- `max_tokens` 必须显式给（默认 8000）：实测推理型模型一次总结
  `completion_tokens 3987`，其中 `reasoning_tokens` 3641 —— 思考过程计入输出长度，
  不给上限会在文章多时被截断，且报错看不出是 token 不够。
- 置顶判定**不能只信 `reasons`**：LLM 路径的要点不带 reasons（那是本地算法的产物），
  只信它会「配了大模型反而没有置顶」。已把量化数据判断下沉到 `utils/notices.js`，
  两条路径口径一致。

## 数据修复：bjx 空正文（需求 1）

`scripts/repair_bjx_bodies.py` 修复北极星电力网 172 条空正文。

**根因**（实测，非推测）：bjx 对**详情页**启用了阿里云 WAF 的 JS 挑战
（`appkey: "CF_APP_WAF"`）。普通 HTTP 请求返回的 HTTP 200 页面只有 16,010 字节，
内容全是挑战脚本，正文关键词零命中、`<title>` 为空。所以标题来自列表页（有），
正文来自详情页（被拦），时间与来源也一并丢失。

**修法**：驱动真实 Chrome（CDP）跑完 JS 挑战再解析。同一条 URL 前后对比：

| 指标 | HTTP 抓取 | CDP 浏览器 |
|---|---|---|
| 页面字节 | 16,010 | 58,523 |
| 含 `CF_APP_WAF` | 是 | 否 |
| 标题关键词命中 | 0 | 3 |
| 去标签纯文本 | 388 字 | 8,898 字 |

**结果**：1,201 条新闻正文为空 **0 条（0.00%）**；bjx 正文最短 630 字 /
平均 5,047 字 / 最长 23,710 字。169 条修复耗时 370 秒（2.2 秒/条）。

```bash
python scripts/repair_bjx_bodies.py --limit 5   # 先试 5 条
python scripts/repair_bjx_bodies.py             # 全量（幂等，已修跳过）
```

## 变更记录

- **2026-10-12**：移除朗读功能（`useSpeech.js` 删除、`ArticleDetail` 的语音按钮/进度/
  语速/CSS 全部清除）；AI 功能改为纯手动触发；新增 `ai-llm.js` 大模型标准对接位；
  修复 bjx 172 条空正文。
- **2026-10-12**：新增 Vue3 前端（本目录全部）。

```bash
npm run build     # 产物在 dist/
npm run preview   # 本地预览构建产物（4173）
```
