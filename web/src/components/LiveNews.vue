<script setup>
/**
 * 最新收录 —— **本地采集库**的最新 N 条。
 *
 * ## 为什么数据源必须是本地库（而不是外部检索）
 *
 * 本区块此前调 `/news/live`，那是**外部 anysearch 检索服务**，有两个硬伤：
 *
 * 1. **有每日免费额度** —— 用尽后返回 `HTTP 402`，界面永远显示"检索失败"
 *    （实测 `error_code: daily_free_quota...`）。外部依赖不可控。
 * 2. **只有标题 + 摘要，没有正文** —— 点进去是站外链接，体验与主列表割裂。
 *
 * 按需求「检索走本地模式」改为读**本地采集库**：数据完整（有正文）、
 * 可点开详情抽屉、无外部依赖与额度限制。
 *
 * ## anysearch 去哪了
 *
 * 它不再作为**前端功能**，改为**采集侧的数据源** ——
 * 见 `scripts/anysearch_backfill.py`：检索结果入库，走与其它来源相同的
 * 审核管线（去重 / 图片过滤 / 质量标记），前端自然看不到来源差异。
 *
 * ## 默认折叠
 *
 * 多数时候看主列表就够，只有想快速确认"最近有没有新稿"时才展开。
 */
import { ref } from 'vue'
import { apiGet } from '../api/client.js'

const props = defineProps({
  /** 主列表最新一条的日期，用于提示「本地库最新到几号」。 */
  latestLocalDate: { type: String, default: '' },
  /** 点击某条时打开详情抽屉（与主列表共用同一交互）。 */
  onOpen: { type: Function, default: null },
})

const open = ref(false)
const loading = ref(false)
const error = ref('')
const items = ref([])
const loaded = ref(false)

/** 拉本地库最新 N 条（按发布时间倒序）。 */
async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await apiGet('/articles', {
      params: { page_size: 20, order: 'published_desc' },
    })
    items.value = data?.items || []
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

function pick(it) {
  if (props.onOpen) props.onOpen(it)
}
</script>

<template>
  <section class="live">
    <header class="live-head" @click="toggle">
      <span class="caret">{{ open ? '▾' : '▸' }}</span>
      <strong>最新收录</strong>
      <span class="tag">本地库</span>
      <span v-if="latestLocalDate" class="muted">
        最新到 {{ latestLocalDate }}
      </span>
      <span v-if="loaded" class="muted">· {{ items.length }} 条</span>
      <span v-if="loading" class="spinner" />
    </header>

    <div v-if="open" class="live-body">
      <p v-if="error" class="banner error">{{ error }}</p>

      <template v-else>
        <ul v-if="items.length" class="rows">
          <li v-for="it in items" :key="it.id">
            <a href="#" @click.prevent="pick(it)">{{ it.title }}</a>
            <div class="sub muted">
              <span>{{ it.source_name_full || it.source_name || '未知来源' }}</span>
              <span v-if="it.published_at_cst">{{ (it.published_at_cst || '').slice(0, 16) }}</span>
              <span v-else class="nodate">无日期</span>
            </div>
            <p v-if="it.excerpt" class="snip">{{ it.excerpt.slice(0, 120) }}</p>
          </li>
        </ul>
        <div v-else-if="!loading" class="empty">本地库暂无数据。</div>
      </template>

      <div class="foot">
        <button :disabled="loading" @click="load">{{ loading ? '加载中…' : '刷新' }}</button>
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

.caret { color: var(--c-muted); font-size: 11px; width: 10px; }

.live-body { padding: 0 14px 12px; }

.rows { list-style: none; margin: 0; padding: 0; }

.rows li { padding: 8px 0; border-top: 1px solid var(--c-border); }

.rows a {
  color: var(--c-link, #2563eb);
  text-decoration: none;
  font-size: 13px;
  line-height: 1.5;
}

.rows a:hover { text-decoration: underline; }

.sub {
  display: flex;
  gap: 10px;
  font-size: 12px;
  margin-top: 3px;
}

.snip { font-size: 12px; color: var(--c-text-2); margin: 4px 0 0; line-height: 1.5; }

.nodate { opacity: 0.6; }

.empty { font-size: 13px; color: var(--c-muted); padding: 12px 0; }

.foot {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--c-border);
}

.foot button {
  font-size: 12px;
  padding: 4px 12px;
  cursor: pointer;
}
</style>
