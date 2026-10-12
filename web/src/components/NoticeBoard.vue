<script setup>
/**
 * AI 数据公告栏。
 *
 * 三种展示效果叠加，各自解决不同问题：
 * 1. **置顶区**：带量化数据的亮点固定在顶部不滚动 —— 这类条目最该先被看到，
 *    让它随波逐流地滚过去等于没置顶；
 * 2. **无缝滚动**：其余条目匀速上滚。实现方式是渲染两份内容 + 位移 -50%，
 *    比 JS 定时改 scrollTop 更省 CPU，也不会在条目高度不一致时抖动；
 * 3. **悬停暂停**：鼠标移入暂停滚动，否则用户想细看时内容一直在跑。
 *
 * 来源标注（趋势/中台 AI/本地规则）如实显示，不把本地规则的结果说成 AI。
 */
import { computed } from 'vue'

const props = defineProps({
  notices: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
  method: { type: String, default: '' },
  caveats: { type: Array, default: () => [] },
  updatedAt: { type: Number, default: 0 },
  /** 服务端给出的缓存生成时刻（字符串，优先于本地 updatedAt）。 */
  generatedAt: { type: String, default: '' },
  /** 下次服务端刷新时刻。 */
  nextRefreshAt: { type: String, default: '' },
  /** 数据来源构成摘要。 */
  sourceSummary: { type: String, default: '' },
  /** 缓存是否已就绪（服务刚启动时可能为 false）。 */
  ready: { type: Boolean, default: true },
})

const emit = defineEmits(['open-articles'])

const pinned = computed(() => props.notices.filter((n) => n.pinned))
const scrolling = computed(() => props.notices.filter((n) => !n.pinned))

/** 滚动时长随条目数增加，保证速度恒定（条目少时不至于一闪而过）。 */
const scrollDuration = computed(() => `${Math.max(16, scrolling.value.length * 5)}s`)

const METHOD_LABEL = {
  llm: 'AI 模型',
  'title-only': '仅标题',
  none: '不可用',
}

const methodLabel = computed(() => METHOD_LABEL[props.method] || props.method || '未知')

/** 首次就绪时刻：优先用服务端给的生成时刻。 */
const updatedText = computed(() => {
  if (props.generatedAt) return props.generatedAt
  if (!props.updatedAt) return ''
  return new Date(props.updatedAt).toLocaleTimeString('zh-CN', { hour12: false })
})

const KIND_LABEL = { highlight: '亮点', keypoint: '要点', keyword: '焦点', title: '标题' }

function open(notice) {
  // 优先用后端给的原站链接（搜索结果直接可点），没有才回落到库内文章跳转
  const url = notice.urls?.[0]
  if (url) {
    window.open(url, '_blank', 'noopener,noreferrer')
    return
  }
  if (notice.articleIds?.length) emit('open-articles', notice.articleIds)
}
</script>

<template>
  <section class="board">
    <header class="board-head">
      <h2>
        AI 数据公告栏
        <span class="tag primary">服务端预生成</span>
        <span class="tag" :class="{ accent: method === 'title-only' }">{{ methodLabel }}</span>
      </h2>
      <div class="meta">
        <span v-if="updatedText" class="muted">生成于 {{ updatedText }}</span>
        <span v-if="nextRefreshAt" class="muted">下次 {{ nextRefreshAt.slice(11, 16) }}</span>
        <span class="muted">置顶 {{ pinned.length }} · 滚动 {{ scrolling.length }}</span>
      </div>
    </header>

    <p v-if="sourceSummary" class="src-line muted">数据来源：{{ sourceSummary }}</p>

    <div v-if="loading && !notices.length" class="empty">
      <span class="spinner" /> 正在读取公告…
    </div>

    <div v-else-if="!ready && !notices.length" class="empty">
      服务端公告正在生成中（首次启动需一次 AI 总结，约 1 分钟）。稍后刷新即可。
    </div>

    <div v-else-if="!notices.length" class="empty">暂无公告数据。</div>

    <template v-else>
      <!-- 置顶区：固定不滚动 -->
      <ul v-if="pinned.length" class="pinned">
        <li v-for="n in pinned" :key="n.id" class="notice pinned-item" @click="open(n)">
          <span class="pin" title="置顶">📌</span>
          <span class="tag accent">{{ n.pinReason || '置顶' }}</span>
          <span class="text">{{ n.text }}</span>
          <span v-if="n.sources?.length" class="src">{{ n.sources.slice(0, 2).join(' / ') }}</span>
        </li>
      </ul>

      <!-- 滚动区：内容渲染两份实现无缝循环 -->
      <div v-if="scrolling.length" class="viewport">
        <div class="track" :style="{ animationDuration: scrollDuration }">
          <ul v-for="copy in 2" :key="copy" class="row" :aria-hidden="copy === 2">
            <li v-for="n in scrolling" :key="`${copy}-${n.id}`" class="notice" @click="open(n)">
              <span class="tag">{{ KIND_LABEL[n.kind] || n.kind }}</span>
              <span class="text">{{ n.text }}</span>
              <span v-if="n.reasons?.length" class="reason">{{ n.reasons.join(' · ') }}</span>
            </li>
          </ul>
        </div>
      </div>
    </template>

    <p v-for="(c, i) in caveats" :key="i" class="banner warn caveat">{{ c }}</p>
  </section>
</template>

<style scoped>
.board {
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.board-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.board-head h2 {
  font-size: 15px;
  margin: 0;
  display: flex;
  align-items: center;
  gap: 6px;
}

.meta {
  display: flex;
  gap: 12px;
  font-size: 12px;
  flex-wrap: wrap;
}

.src-line {
  margin: 0 0 8px;
  font-size: 12px;
}

.pinned {
  list-style: none;
  margin: 0 0 8px;
  padding: 8px 10px;
  background: var(--c-accent-soft);
  border: 1px solid #fed7aa;
  border-radius: var(--radius);
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.pinned-item {
  cursor: pointer;
  align-items: flex-start;
}

.pin { flex: none; }

.viewport {
  overflow: hidden;
  flex: 1;
  /* 高度固定，滚动才有稳定的可见窗口；条目少时也不至于压扁 */
  max-height: 190px;
  position: relative;
}

/* 上下淡出，让滚动看起来是「流过去」而不是被硬切 */
.viewport::before,
.viewport::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  height: 14px;
  pointer-events: none;
  z-index: 1;
}

.viewport::before { top: 0; background: linear-gradient(var(--c-surface), transparent); }
.viewport::after { bottom: 0; background: linear-gradient(transparent, var(--c-surface)); }

.track {
  animation-name: scroll-up;
  animation-timing-function: linear;
  animation-iteration-count: infinite;
}

.viewport:hover .track { animation-play-state: paused; }

@keyframes scroll-up {
  from { transform: translateY(0); }
  to { transform: translateY(-50%); }
}

.row {
  list-style: none;
  margin: 0;
  padding: 0;
}

.notice {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 5px 0;
  font-size: 13px;
  line-height: 1.55;
  border-bottom: 1px dashed var(--c-border);
  cursor: pointer;
}

.notice:hover .text { color: var(--c-primary); }

.text { flex: 1; }

.src,
.reason {
  flex: none;
  font-size: 11px;
  color: var(--c-muted);
  white-space: nowrap;
}

.caveat { margin: 6px 0 0; }
</style>
