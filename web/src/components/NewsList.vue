<script setup>
/**
 * 新闻列表。
 *
 * 每条卡片的标签由 ``matchDimensions`` **就地**打（纯字符串匹配，微秒级），
 * 不走 AI 接口 —— 列表里几十条各调一次 AI 既慢又无必要。
 * 精细化的多维度标签在详情页由 AI 层产出（需求 4 的两级实现）。
 */
import { computed } from 'vue'
import { matchDimensions } from '../utils/lexicon.js'
import { CHANNEL_LABELS } from '../utils/topics.js'

const props = defineProps({
  items: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  loadingMore: { type: Boolean, default: false },
  hasMore: { type: Boolean, default: false },
  total: { type: Number, default: 0 },
  /** 空结果时的说明文案（由上层按「有站点但没稿 / 完全没站点」分别给出） */
  emptyHint: { type: String, default: '' },
  /** 标题由上层给：它还带着「当前数据域」的信息，组件不该自己猜 */
  title: { type: String, default: '新闻' },
  /** 查询失败原文。非空时**必须**走错误态，不能落进「没有新闻」 */
  error: { type: String, default: '' },
  /** 是否因扫描窗口封顶而截断了可翻页数 */
  truncated: { type: Boolean, default: false },
  /** 后端给的可翻页数（封顶后即扫描窗口内的页数） */
  pages: { type: Number, default: 0 },
})

const emit = defineEmits(['open', 'load-more', 'retry'])

/** 预计算每条卡片的标签，避免模板里反复计算。 */
const decorated = computed(() =>
  props.items.map((a) => {
    const dims = matchDimensions(`${a.title} ${a.excerpt}`)
    return {
      ...a,
      chips: [...dims.主题.slice(0, 3), ...dims.属性.slice(0, 1)],
      hasBody: Boolean(a.excerpt && a.excerpt.length > 40),
    }
  }),
)

function fmtTime(value) {
  if (!value) return '时间未知'
  const d = new Date(String(value).replace(' ', 'T'))
  if (Number.isNaN(d.getTime())) return value
  const now = Date.now()
  const diff = now - d.getTime()
  const day = 86_400_000
  if (diff < 3600_000) return `${Math.max(1, Math.floor(diff / 60000))} 分钟前`
  if (diff < day) return `${Math.floor(diff / 3600_000)} 小时前`
  if (diff < 3 * day) return `${Math.floor(diff / day)} 天前`
  return d.toLocaleDateString('zh-CN')
}

/** 时间精度为 day 的来源只能标到日期，这里如实标注，不假装有精确时刻。 */
function precisionHint(a) {
  return a.timePrecision === 'day' ? '仅日期' : ''
}
</script>

<template>
  <section class="list-wrap">
    <header class="list-head">
      <h2>
        {{ title }}
        <span class="muted">命中 {{ total.toLocaleString() }} 条</span>
      </h2>
      <span v-if="loading" class="muted"><span class="spinner" /> 加载中</span>
    </header>

    <ul v-if="decorated.length" class="list">
      <!-- key 必须带数据域：两库 id 各自自增，全国视图下 1–2735 会重号，
           只用 id 会让 Vue 复用错误的 DOM 节点（表现为点开是另一条）。 -->
      <li
        v-for="a in decorated"
        :key="`${a.channel}:${a.id}`"
        class="card"
        @click="emit('open', a)"
      >
        <div class="card-main">
          <h3>{{ a.title }}</h3>
          <p class="excerpt">{{ a.excerpt || '（无摘要，点开查看详情）' }}</p>
          <div class="chips">
            <span v-for="c in a.chips" :key="c" class="tag">{{ c }}</span>
          </div>
        </div>
        <div class="card-aside">
          <!-- 混显时必须能一眼分辨「电网稿」和「党报稿」 -->
          <span class="tag" :class="a.channel">{{ CHANNEL_LABELS[a.channel] || a.channel }}</span>
          <span class="src">{{ a.sourceName }}</span>
          <span class="muted">{{ a.provinceName || a.provinceCode || '全国' }}</span>
          <span class="muted">
            {{ fmtTime(a.publishedAt) }}
            <em v-if="precisionHint(a)" class="hint">{{ precisionHint(a) }}</em>
          </span>
          <span v-if="!a.hasBody" class="tag danger" title="正文为空，AI 解析质量会受影响">正文缺失</span>
        </div>
      </li>
    </ul>

    <div v-else-if="loading" class="skeleton-list">
      <div v-for="i in 5" :key="i" class="skeleton" style="height: 62px; margin-bottom: 8px" />
    </div>

    <!-- 错误态必须排在空态之前：Key 失效时若显示「没有新闻」，
         用户会以为是这个省没数据，而真正的原因是没权限。 -->
    <div v-else-if="error" class="empty">
      <p class="empty-title">加载失败：{{ error }}</p>
      <p class="muted">这不是「该省没有数据」，而是请求本身失败了。</p>
      <button @click="emit('retry')">重试</button>
    </div>

    <div v-else class="empty">
      <p class="empty-title">{{ emptyHint || '当前条件下没有新闻。' }}</p>
      <p class="muted">
        可放宽时间范围、清除关键词，或到上方站点表确认该站点是否仍在产出。
      </p>
    </div>

    <div v-if="hasMore" class="more">
      <button :disabled="loadingMore" @click="emit('load-more')">
        {{ loadingMore ? '加载中…' : '加载更多' }}
      </button>
    </div>

    <!-- 跨域查询的可翻页数被扫描窗口封顶，到这里就翻不动了，得说清楚 -->
    <p v-else-if="truncated" class="muted truncate-hint">
      已到分页上限：共 {{ total.toLocaleString() }} 条，只能翻到前 {{ pages }} 页。
      其余请用「导出 CSV」或缩小时间范围。
    </p>
  </section>
</template>

<style scoped>
.list-wrap {
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.list-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding: 10px 14px;
  border-bottom: 1px solid var(--c-border);
}

.list-head h2 {
  font-size: 15px;
  margin: 0;
  display: flex;
  gap: 8px;
  align-items: baseline;
}

.list-head .muted { font-size: 12px; font-weight: 400; }

.list {
  list-style: none;
  margin: 0;
  padding: 0;
  overflow-y: auto;
  flex: 1;
}

.card {
  display: flex;
  gap: 14px;
  /* 14px 20px：比原来的 10px 14px 松一档。列表是「扫读」场景，
     行距太紧会让眼睛找不到落点，反而降低扫读速度。 */
  padding: 14px 16px;
  border-bottom: 1px solid var(--c-border);
  cursor: pointer;
  transition: background 0.12s;
}

.card:hover { background: var(--c-surface-2); }

.card-main { flex: 1; min-width: 0; }

.card-main h3 {
  margin: 0 0 5px;
  /* 15px/1.5：列表标题要比正文大一号，行高从 1.45 放到 1.5，
     两行标题不再挤在一起。 */
  font-size: 15px;
  font-weight: 600;
  line-height: 1.5;
  letter-spacing: -0.005em;
}

.card:hover h3 { color: var(--c-primary); }

.excerpt {
  margin: 0 0 6px;
  font-size: 13px;
  /* 1.7 而不是 1.6：摘要是连续小字，行高偏紧会明显降低可读性 */
  line-height: 1.7;
  color: var(--c-text-2);
  display: -webkit-box;
  /* 3 行而不是 2 行：13px 下 2 行只有约 60 字，扫读时信息量不足；
     3 行能多覆盖一半内容，而列表高度只增加约 20px。 */
  -webkit-line-clamp: 3;
  line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.chips { display: flex; gap: 4px; flex-wrap: wrap; }

.card-aside {
  flex: none;
  width: 130px;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
  font-size: 12px;
  text-align: right;
}

.src { color: var(--c-text-2); }

.hint {
  font-style: normal;
  border-bottom: 1px dotted var(--c-muted);
}

.more {
  padding: 10px;
  text-align: center;
  border-top: 1px solid var(--c-border);
}

.skeleton-list { padding: 12px 14px; }

.empty {
  padding: 28px 16px;
  text-align: center;
  background: var(--c-surface);
  border: 1px dashed var(--c-border);
  border-radius: var(--radius-lg);
  font-size: 13px;
}

.empty-title { margin: 0 0 4px; font-weight: 500; }

/* 错误态里的重试按钮：与「加载更多」同款，但在空面板里要留点间距 */
.empty button {
  margin-top: 10px;
  padding: 4px 14px;
  border: 1px solid var(--c-border);
  border-radius: var(--radius-md);
  background: var(--c-surface);
  cursor: pointer;
}

/* 达分页上限的说明：它不是错误，不该抢视觉焦点 */
.truncate-hint {
  margin: 0;
  padding: 8px 14px 0;
  text-align: center;
  font-size: 12px;
}
</style>
