<script setup>
/**
 * 站点概览表：选中省份后，列出该省涉及的站点与产出。
 *
 * 数据来自 `GET /stats/sources?province=&channel=`（跨域归并）。
 * 为什么不用 `/sources`：那个接口没有文章数，看不出「哪个站点在产、哪个停了」。
 */
import { computed } from 'vue'

const props = defineProps({
  sites: { type: Array, default: () => [] },
  loading: { type: Boolean, default: false },
})

const emit = defineEmits(['pick'])

const total = computed(() => props.sites.reduce((sum, s) => sum + (s.articleCount || 0), 0))

const CHANNEL_LABEL = { power: '电力', province: '省级' }
const CATEGORY_LABEL = {
  sgcc: '国网', csg: '南网', gov: '政府', industry: '行业媒体',
  independent: '发电集团', hmt: '港澳台', province_media: '党报/广电',
}

function channelLabel(v) {
  return CHANNEL_LABEL[v] || v || '—'
}

function categoryLabel(v) {
  return CATEGORY_LABEL[v] || v || '—'
}

function statusLabel(site) {
  if (!site.enabled) return '停用'
  if (site.status === 'candidate') return '候选'
  if (site.status === 'paused') return '暂停'
  return '启用'
}

function fmtDate(value) {
  if (!value) return '—'
  return String(value).replace('T', ' ').slice(0, 16)
}
</script>

<template>
  <section class="sites">
    <header class="sites-head">
      <h2>站点概览</h2>
      <span class="muted">
        {{ sites.length }} 个站点 · 合计 {{ total }} 篇
      </span>
    </header>

    <p v-if="loading" class="muted pad">加载中…</p>
    <p v-else-if="!sites.length" class="muted pad">该省暂无已配置的站点。</p>

    <table v-else class="table">
      <thead>
        <tr>
          <th>站点</th>
          <th>数据域</th>
          <th>类别</th>
          <th class="num">文章数</th>
          <th>最近发布</th>
          <th>状态</th>
          <th />
        </tr>
      </thead>
      <tbody>
        <tr v-for="site in sites" :key="`${site.channel}-${site.id}`">
          <td>{{ site.name }}</td>
          <td><span class="tag" :class="site.channel">{{ channelLabel(site.channel) }}</span></td>
          <td>{{ categoryLabel(site.category) }}</td>
          <td class="num">{{ site.articleCount }}</td>
          <td class="mono">{{ fmtDate(site.lastPublishedAt) }}</td>
          <td :class="{ muted: !site.enabled }">{{ statusLabel(site) }}</td>
          <td class="actions">
            <button class="link" @click="emit('pick', site)">只看该站点</button>
          </td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<style scoped>
.sites {
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}

.sites-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--c-border);
}

.sites-head h2 { margin: 0; font-size: 14px; font-weight: 500; }

.table { width: 100%; border-collapse: collapse; font-size: 13px; }

.table th,
.table td {
  padding: 7px 12px;
  text-align: left;
  border-bottom: 1px solid var(--c-border);
}

.table th { font-weight: 500; color: var(--c-muted, #666); white-space: nowrap; }
.table tbody tr:last-child td { border-bottom: none; }
.table tbody tr:hover { background: var(--c-hover, #f7f9fc); }

.num { text-align: right; font-variant-numeric: tabular-nums; }
.mono { font-variant-numeric: tabular-nums; }
.pad { padding: 16px 14px; }
.actions { text-align: right; }

.tag {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 12px;
  border: 1px solid var(--c-border);
}

.tag.power { color: #185fa5; background: #e6f1fb; border-color: #b5d4f4; }
.tag.province { color: #534ab7; background: #eeedfe; border-color: #cecbf6; }

.link {
  border: none;
  background: none;
  color: var(--c-primary, #185fa5);
  cursor: pointer;
  font-size: 12px;
  padding: 0;
}

.link:hover { text-decoration: underline; }
</style>
