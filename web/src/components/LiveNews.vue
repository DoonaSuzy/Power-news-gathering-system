<script setup>
/**
 * 实时检索区块 —— 补本地采集库的时效缺口。
 *
 * 为什么单独一块而不混进主列表：两者性质不同。
 * - 主列表 = 本地采集库，有正文、有精确时间戳、可回溯；
 * - 本区块 = 实时检索（anysearch），覆盖广、时效新，但**只有标题与摘要**。
 *
 * 混在一起会让「这条能不能点开看全文」变得不可预测。所以分开呈现，
 * 并在界面上直接标明「实时检索」来源与「无正文」的事实。
 *
 * 默认折叠：多数时候用户看主列表就够，只有「最近是不是没更新」时才展开。
 */
import { ref } from 'vue'
import { apiGet } from '../api/client.js'

const props = defineProps({
  /** 主列表最新一条的日期，用于提示「本地库最新到几号」。 */
  latestLocalDate: { type: String, default: '' },
})

const open = ref(false)
const loading = ref(false)
const error = ref('')
const items = ref([])
const meta = ref({})
const loaded = ref(false)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await apiGet('/news/live', { params: { limit: 30 } })
    items.value = data?.items || []
    meta.value = data || {}
    loaded.value = true
  } catch (err) {
    error.value = err?.message || String(err)
  } finally {
    loading.value = false
  }
}

function toggle() {
  open.value = !open.value
  if (open.value && !loaded.value) load()
}
</script>

<template>
  <section class="live">
    <header class="live-head" @click="toggle">
      <span class="caret">{{ open ? '▾' : '▸' }}</span>
      <strong>实时检索</strong>
      <span class="tag">anysearch</span>
      <span v-if="latestLocalDate" class="muted">
        本地采集库最新到 {{ latestLocalDate }}
      </span>
      <span v-if="loaded" class="muted">· 检索到 {{ items.length }} 条</span>
      <span v-if="loading" class="spinner" />
    </header>

    <div v-if="open" class="live-body">
      <p v-if="error" class="banner error">{{ error }}</p>

      <template v-else>
        <p class="banner warn">
          检索结果只有标题与摘要（无正文），点标题跳原文；日期为从摘要文本提取，可能缺失。
        </p>
        <ul v-if="items.length" class="rows">
          <li v-for="(it, i) in items" :key="i">
            <a :href="it.url" target="_blank" rel="noreferrer noopener">{{ it.title }}</a>
            <div class="sub muted">
              <span>{{ it.source || '未知来源' }}</span>
              <span v-if="it.published">{{ it.published }}</span>
              <span v-else class="nodate">无日期</span>
            </div>
            <p v-if="it.excerpt" class="snip">{{ it.excerpt.slice(0, 120) }}</p>
          </li>
        </ul>
        <div v-else-if="!loading" class="empty">未检索到相关结果。</div>
      </template>

      <div class="foot">
        <button :disabled="loading" @click="load">{{ loading ? '检索中…' : '重新检索' }}</button>
        <span v-if="meta.fetched_at" class="muted">检索于 {{ meta.fetched_at }}</span>
      </div>
    </div>
  </section>
</template>

<style scoped>
.live {
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  margin-bottom: 10px;
}

.live-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 14px;
  cursor: pointer;
  font-size: 13px;
}

.live-head:hover { background: var(--c-surface-2); }

.caret { color: var(--c-muted); font-size: 11px; }

.live-body {
  padding: 0 14px 12px;
  border-top: 1px solid var(--c-border);
}

.rows {
  list-style: none;
  margin: 8px 0;
  padding: 0;
  max-height: 320px;
  overflow-y: auto;
}

.rows li {
  padding: 7px 0;
  border-bottom: 1px dashed var(--c-border);
}

.rows a {
  font-size: 13px;
  font-weight: 600;
  line-height: 1.5;
}

.sub {
  display: flex;
  gap: 10px;
  font-size: 12px;
  margin-top: 2px;
}

.nodate { color: var(--c-muted); }

.snip {
  margin: 3px 0 0;
  font-size: 12px;
  color: var(--c-text-2);
  line-height: 1.6;
}

.foot {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-top: 8px;
}
</style>
