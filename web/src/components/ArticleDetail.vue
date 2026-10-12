<script setup>
/**
 * 文章详情抽屉 —— AI 辅助功能（需求 3）与精细化标签（需求 4）的落点。
 *
 * ## AI 触发规则（需求 4，硬约束）
 *
 * 本组件内的 AI 能力**全部由用户点击按钮触发，没有任何自动触发**：
 * - 进入详情页时只加载正文（``loadDetail``），**不**预跑任何 AI；
 * - 三个 AI 按钮各自独立调用，点一次跑一次；
 * - 组件没有 ``watch`` 监听会触发 AI（切换文章时只重置状态）。
 *
 * 与之相对的是**公告栏**（``NoticeBoard.vue``）：它按需求 2 与需求 5 在定时任务
 * 与筛选变化时自动刷新，那是「列表级」的数据展示，不是「解析某篇新闻」。
 * 两者刻意分开，别把公告栏的自动刷新误判成「AI 自动解析」。
 *
 * 每项 AI 结果都显示 ``provider`` 与 ``method``：用户必须知道这条是模型算的、
 * 抽取式选的，还是本地规则匹配的 —— 三者的可信度并不相同。
 */
import { computed, ref, watch } from 'vue'
import { ai } from '../api/ai.js'
import { fetchArticleDetail } from '../api/news.js'
import { matchDimensions } from '../utils/lexicon.js'

const props = defineProps({
  article: { type: Object, required: true },
})

const emit = defineEmits(['close'])

const detail = ref(null)
const loadingDetail = ref(false)
const detailError = ref('')

const explain = ref(null)
const explainLoading = ref(false)
const summary = ref(null)
const summaryLoading = ref(false)
const tags = ref(null)
const tagsLoading = ref(false)
const aiError = ref('')

/** 本地即时标签（不耗 AI 调用），与 AI 标签并列展示，便于对照。 */
const localChips = computed(() => {
  const text = `${props.article.title} ${props.article.excerpt}`
  return matchDimensions(text)
})

const METHOD_LABEL = {
  llm: 'AI 模型',
  extractive: '抽取式',
  'local-rule': '本地规则',
  none: '不可用',
}

/** 正文拆段。
 *
 * 为什么不用 ``white-space: pre-wrap``：那样换行是有了，但**段与段之间没有间距**，
 * 实测这篇正文 73 段、无空行，长文本会糊成一片。拆成 ``<p>`` 才能加 ``margin``，
 * 也让首行元信息可以被单独弱化。
 *
 * 首段若是「来源：… 时间：…」这种网页元信息，标 ``srcMeta`` ——
 * 它是溯源信息，删掉用户就没法核对来源，但不该当作正文第一段读。
 */
const bodyParagraphs = computed(() => {
  const raw = detail.value?.bodyText || props.article.excerpt || ''
  if (!raw) return []
  return raw
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((text, i) => ({
      text,
      srcMeta: i === 0 && /^(来源|时间)[:：]|[:：]\s*\d{4}-\d{2}-\d{2}/.test(text),
    }))
})

/** 阅读字号（可调，持久化到 localStorage）。 */
const SIZE_KEY = 'powernews.reading-size'
const SIZE_STEPS = [14, 15, 16, 17, 18, 20]
const sizeIndex = ref(Math.max(0, SIZE_STEPS.indexOf(Number(localStorage.getItem(SIZE_KEY)) || 16)))

function applyReadingSize() {
  const px = SIZE_STEPS[sizeIndex.value]
  document.documentElement.style.setProperty('--reading-size', `${px}px`)
  try {
    localStorage.setItem(SIZE_KEY, String(px))
  } catch { /* 隐私模式下写不进去，不影响本次 */ }
}

function bumpSize(delta) {
  sizeIndex.value = Math.max(0, Math.min(SIZE_STEPS.length - 1, sizeIndex.value + delta))
  applyReadingSize()
}

watch(sizeIndex, applyReadingSize, { immediate: true })

function methodLabel(m) {
  return METHOD_LABEL[m] || m || '未知'
}

async function loadDetail() {
  loadingDetail.value = true
  detailError.value = ''
  try {
    detail.value = await fetchArticleDetail(props.article.id)
  } catch (err) {
    detailError.value = err?.message || String(err)
  } finally {
    loadingDetail.value = false
  }
}

async function runExplain() {
  explainLoading.value = true
  aiError.value = ''
  try {
    explain.value = await ai.explain({ article: { ...props.article, ...detail.value } })
  } catch (err) {
    aiError.value = err?.message || String(err)
  } finally {
    explainLoading.value = false
  }
}

async function runSummary() {
  summaryLoading.value = true
  aiError.value = ''
  try {
    const article = { ...props.article, ...detail.value }
    summary.value = await ai.summarize({
      articles: [article],
      window: { label: '本篇' },
      maxPoints: 6,
      maxHighlights: 3,
    })
  } catch (err) {
    aiError.value = err?.message || String(err)
  } finally {
    summaryLoading.value = false
  }
}

async function runTags() {
  tagsLoading.value = true
  aiError.value = ''
  try {
    tags.value = await ai.tag({ article: { ...props.article, ...detail.value } })
  } catch (err) {
    aiError.value = err?.message || String(err)
  } finally {
    tagsLoading.value = false
  }
}

/** 切换文章时重置所有派生状态，否则会短暂显示上一篇的解析结果。 */
watch(
  () => props.article?.id,
  () => {
    detail.value = null
    explain.value = null
    summary.value = null
    tags.value = null
    aiError.value = ''
    detailError.value = ''
    loadDetail()
  },
  { immediate: true },
)

function onKeydown(e) {
  if (e.key === 'Escape') emit('close')
}
</script>

<template>
  <div class="mask" @click.self="emit('close')" @keydown="onKeydown" tabindex="0">
    <aside class="drawer">
      <header class="head">
        <div class="head-main">
          <h2>{{ article.title }}</h2>
          <div class="meta muted">
            <span>{{ article.sourceName }}</span>
            <span>{{ article.provinceName || '全国' }}</span>
            <span>{{ article.publishedAt || '时间未知' }}</span>
            <a v-if="article.url" :href="article.url" target="_blank" rel="noreferrer noopener">
              原文 ↗
            </a>
          </div>
        </div>
        <button @click="emit('close')">关闭</button>
      </header>

      <!-- AI 工具条：全部按钮手动触发，无任何自动调用 -->
      <div class="tools">
        <div class="ai">
          <button :disabled="explainLoading" @click="runExplain">
            {{ explainLoading ? '解析中…' : 'AI 智能解析' }}
          </button>
          <button :disabled="summaryLoading" @click="runSummary">
            {{ summaryLoading ? '总结中…' : 'AI 要点总结' }}
          </button>
          <button :disabled="tagsLoading" @click="runTags">
            {{ tagsLoading ? '打标中…' : 'AI 多维标签' }}
          </button>
        </div>
        <div class="reading-size" title="正文字号">
          <span>字号</span>
          <button :disabled="sizeIndex === 0" @click="bumpSize(-1)">A−</button>
          <span class="level">{{ SIZE_STEPS[sizeIndex] }}px</span>
          <button :disabled="sizeIndex === SIZE_STEPS.length - 1" @click="bumpSize(1)">A+</button>
        </div>
      </div>

      <p v-if="aiError" class="banner error">AI 调用失败：{{ aiError }}</p>

      <div class="body">
        <!-- AI 结果区 -->
        <section v-if="summary" class="panel">
          <h3>
            AI 要点总结
            <span class="tag primary">{{ summary.provider }}</span>
            <span class="tag">{{ methodLabel(summary.method) }}</span>
          </h3>
          <p class="headline">{{ summary.headline }}</p>
          <ul v-if="summary.keyPoints?.length" class="points">
            <li v-for="(p, i) in summary.keyPoints" :key="i">
              {{ p.text }}
              <em v-if="p.reasons?.length" class="reasons">{{ p.reasons.join(' · ') }}</em>
            </li>
          </ul>
          <ul v-if="summary.highlights?.length" class="points highlights">
            <li v-for="(p, i) in summary.highlights" :key="`h${i}`">
              <strong>亮点</strong> {{ p.text }}
            </li>
          </ul>
          <p v-for="(c, i) in summary.caveats" :key="i" class="banner warn">{{ c }}</p>
        </section>

        <section v-if="explain" class="panel">
          <h3>
            AI 智能解析
            <span class="tag primary">{{ explain.provider }}</span>
            <span class="tag">{{ methodLabel(explain.method) }}</span>
          </h3>
          <ul v-if="explain.paragraphs?.length" class="points">
            <li v-for="(p, i) in explain.paragraphs" :key="i">
              <span class="tag">{{ p.role }}</span> {{ p.text }}
            </li>
          </ul>
          <div v-if="explain.entities?.length" class="chips">
            <span v-for="e in explain.entities" :key="e.text" class="tag">{{ e.text }}</span>
          </div>
          <dl v-if="explain.terms?.length" class="terms">
            <template v-for="t in explain.terms" :key="t.term">
              <dt>{{ t.term }}</dt>
              <dd>{{ t.gloss }}</dd>
            </template>
          </dl>
          <p v-for="(c, i) in explain.caveats" :key="i" class="banner warn">{{ c }}</p>
        </section>

        <section v-if="tags" class="panel">
          <h3>
            AI 多维度标签
            <span class="tag primary">{{ tags.provider }}</span>
            <span class="tag">{{ methodLabel(tags.method) }}</span>
          </h3>
          <div v-for="(words, dim) in tags.dimensions" :key="dim" class="dim-row">
            <span class="dim-name">{{ dim }}</span>
            <span v-for="w in words" :key="w" class="tag primary">{{ w }}</span>
          </div>
        </section>

        <!-- 本地即时标签：与 AI 标签对照，也保证 AI 不可用时仍有标签可看 -->
        <section class="panel">
          <h3>本地即时标签 <span class="tag">免调用</span></h3>
          <div v-for="(words, dim) in localChips" :key="dim" class="dim-row">
            <span class="dim-name">{{ dim }}</span>
            <span v-for="w in words" :key="w" class="tag">{{ w }}</span>
            <span v-if="!words.length" class="muted">—</span>
          </div>
        </section>

        <!-- 原文 -->
        <section class="panel">
          <h3>正文</h3>
          <p v-if="loadingDetail" class="muted"><span class="spinner" /> 加载正文…</p>
          <p v-else-if="detailError" class="banner error">{{ detailError }}</p>
          <div v-else-if="bodyParagraphs.length" class="article-body">
            <p v-for="(p, i) in bodyParagraphs" :key="i" :class="{ 'src-meta': p.srcMeta }">
              {{ p.text }}
            </p>
          </div>
          <div v-else class="article-body muted">（正文为空）</div>
        </section>
      </div>
    </aside>
  </div>
</template>

<style scoped>
.mask {
  position: fixed;
  inset: 0;
  background: rgb(15 23 42 / 42%);
  display: flex;
  justify-content: flex-end;
  z-index: 50;
}

.drawer {
  width: min(760px, 94vw);
  height: 100%;
  background: var(--c-surface);
  box-shadow: var(--shadow-lg);
  display: flex;
  flex-direction: column;
  animation: slide-in 0.18s ease-out;
}

@keyframes slide-in {
  from { transform: translateX(24px); opacity: 0.6; }
  to { transform: translateX(0); opacity: 1; }
}

.head {
  display: flex;
  gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--c-border);
}

.head-main { flex: 1; min-width: 0; }

.head h2 {
  margin: 0 0 6px;
  /* 22px/1.4：与正文 16px 拉开 6px 级差，标题才是「标题」。
     原来 16px 与正文 13px 只差 3px，层级几乎看不出来。 */
  font-size: 22px;
  line-height: 1.4;
  letter-spacing: -0.01em;
  font-weight: 700;
}

.meta {
  display: flex;
  gap: 12px;
  font-size: 12px;
  flex-wrap: wrap;
  /* #64748b 对比度 4.6:1，过 WCAG AA 的 4.5:1。
     原来的 #7c8798 只有 3.64:1 —— 「来源/时间」是重要元信息，不该那么淡。 */
  color: #64748b;
}

.tools {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  padding: 9px 16px;
  background: var(--c-surface-2);
  border-bottom: 1px solid var(--c-border);
}

.ai {
  display: flex;
  gap: 6px;
  align-items: center;
}

.body {
  flex: 1;
  overflow-y: auto;
  padding: 12px 16px 24px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.panel {
  border: 1px solid var(--c-border);
  border-radius: var(--radius);
  padding: 10px 12px;
  background: var(--c-surface);
}

.panel h3 {
  margin: 0 0 6px;
  font-size: 14px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.headline {
  margin: 0 0 8px;
  color: var(--c-text-2);
  font-size: 13px;
}

.points {
  margin: 0;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 13px;
}

.points.highlights { margin-top: 8px; }

.reasons {
  font-style: normal;
  font-size: 11px;
  color: var(--c-muted);
  margin-left: 4px;
}

.chips,
.dim-row {
  display: flex;
  gap: 5px;
  flex-wrap: wrap;
  align-items: center;
}

.dim-row { margin-bottom: 5px; }

.dim-name {
  flex: none;
  width: 44px;
  font-size: 12px;
  color: var(--c-muted);
}

.terms {
  margin: 8px 0 0;
  font-size: 12px;
}

.terms dt {
  font-weight: 600;
  margin-top: 5px;
}

.terms dd {
  margin: 0 0 0 12px;
  color: var(--c-text-2);
}

.article-body {
  /* 阅读参数全部来自 CSS 变量，字号控件只需改变量 */
  font-size: var(--reading-size);
  line-height: var(--reading-lh);
  letter-spacing: var(--reading-tracking);
  color: var(--reading-color);
  /* 单行宽度上限：无限宽的行长会让视线横扫过远，读几行就累 */
  max-width: var(--reading-max);
  max-height: 52vh;
  overflow-y: auto;
  /* 长文本滚动容器：加一点点内边距，避免滚动条贴字 */
  padding-right: 4px;
}

.article-body p {
  margin: 0 0 var(--reading-gap);
}

/* 首段的网页元信息（「来源：… 时间：…」）不是正文：
   弱化显示而不是删掉 —— 它是溯源信息，删了用户没法核对来源。 */
.article-body p.src-meta {
  font-size: 0.8em;
  color: var(--c-muted);
  letter-spacing: normal;
  padding-bottom: 0.6em;
  margin-bottom: 1em;
  border-bottom: 1px dashed var(--c-border);
}

/* 字号调节控件 */
.reading-size {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--c-muted);
}

.reading-size button {
  padding: 2px 8px;
  font-size: 12px;
  min-width: 30px;
}

.reading-size .level {
  min-width: 34px;
  text-align: center;
  font-family: var(--mono);
}

@media (max-width: 720px) {
  /* 窄屏不需要限制行宽（屏宽本身就是约束），字号略降避免一行字太少 */
  .article-body { max-width: none; font-size: 15px; }
}

.banner { margin: 0; }
</style>
