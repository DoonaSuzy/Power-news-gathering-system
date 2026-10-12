<script setup>
/**
 * 筛选栏：地区维度 + 主题维度 + 来源类别 + 时间档位 + 关键词。
 *
 * 组件**不持有筛选状态**，只发 ``patch`` 事件让父组件改。
 * 这样「当前筛选条件」只有一份（在 ``useNewsFeed`` 里），
 * 定时任务拉数据时用的和用户看到的一定是同一套条件 —— 否则会出现
 * 「手动刷新看到的是储能，2 小时自动刷新后变成全部」这类错乱。
 *
 * 地区维度分两级：大区（8 个）与省（34 个）。两级都映射到中台的同一个
 * ``province`` 参数 —— 中台接受「大区名 / 省代码 / 省全称 / 简称」，
 * 前端不做展开（展开必然与中台的区划定义漂移）。
 */
import { computed } from 'vue'
import { CATEGORY_PRESETS, RANGE_PRESETS, REGION_PRESETS, TOPIC_PRESETS } from '../utils/topics.js'

const props = defineProps({
  filters: { type: Object, required: true },
  provinces: { type: Array, default: () => [] },
  activeCount: { type: Number, default: 0 },
})

const emit = defineEmits(['patch', 'reset', 'search'])

/** 选了省就按省，没选省才按大区 —— 两者共用中台的 province 参数。 */
const provincesOfRegion = computed(() =>
  props.filters.region
    ? props.provinces.filter((p) => p.region === props.filters.region)
    : props.provinces,
)

function pickRegion(value) {
  // 切换大区时清掉已选省份：不清会出现「华南 + 北京市」这种空结果
  emit('patch', 'region', props.filters.region === value ? '' : value)
  emit('patch', 'province', '')
}

function pickTopic(value) {
  emit('patch', 'topic', props.filters.topic === value ? '' : value)
}
</script>

<template>
  <div class="filters">
    <div class="row">
      <span class="label">地区</span>
      <button
        v-for="r in REGION_PRESETS"
        :key="r"
        :class="{ active: filters.region === r }"
        @click="pickRegion(r)"
      >
        {{ r }}
      </button>
      <select
        id="filter-province"
        name="province"
        :value="filters.province"
        @change="emit('patch', 'province', $event.target.value)"
      >
        <option value="">全部省份</option>
        <option v-for="p in provincesOfRegion" :key="p.code" :value="p.code">
          {{ p.shortName }}
        </option>
      </select>
    </div>

    <div class="row">
      <span class="label">主题</span>
      <button
        v-for="t in TOPIC_PRESETS"
        :key="t.key"
        :class="{ active: filters.topic === t.key }"
        @click="pickTopic(t.key)"
      >
        {{ t.label }}
      </button>
    </div>

    <div class="row">
      <span class="label">来源</span>
      <button
        v-for="c in CATEGORY_PRESETS"
        :key="c.key"
        :class="{ active: filters.category === c.key }"
        @click="emit('patch', 'category', filters.category === c.key ? '' : c.key)"
      >
        {{ c.label }}
      </button>
    </div>

    <div class="row">
      <span class="label">时间</span>
      <button
        v-for="r in RANGE_PRESETS"
        :key="r.key"
        :class="{ active: filters.range === r.key }"
        @click="emit('patch', 'range', r.key)"
      >
        {{ r.label }}
      </button>
      <template v-if="filters.range === 'all'">
        <input
          id="filter-since"
          name="since"
          type="date"
          :value="filters.since"
          @change="emit('patch', 'since', $event.target.value)"
        />
        <span class="muted">~</span>
        <input
          id="filter-until"
          name="until"
          type="date"
          :value="filters.until"
          @change="emit('patch', 'until', $event.target.value)"
        />
      </template>
    </div>

    <div class="row">
      <span class="label">搜索</span>
      <input
        id="filter-keyword"
        name="keyword"
        class="search"
        placeholder="标题或正文关键词，回车搜索"
        :value="filters.keyword"
        @keyup.enter="emit('search', $event.target.value)"
        @change="emit('patch', 'keyword', $event.target.value)"
      />
      <button v-if="activeCount" @click="emit('reset')">重置（{{ activeCount }}）</button>
    </div>
  </div>
</template>

<style scoped>
.filters {
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.row {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.label {
  flex: none;
  width: 34px;
  color: var(--c-muted);
  font-size: 12px;
}

button {
  padding: 3px 10px;
  font-size: 13px;
}

.search {
  flex: 1;
  min-width: 180px;
}
</style>
