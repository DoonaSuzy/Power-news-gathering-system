<script setup>
/**
 * 省份选择器（可搜索单选）。
 *
 * 为什么不用原生 <select>：34 个省份平铺成一个下拉，找「广东」要滚很久；
 * 也不方便以后扩到「站点级」筛选。这里做成输入框 + 过滤列表，
 * 支持键盘上下键与回车，交互成本最低。
 *
 * 为什么不用 <datalist>：datalist 的 value 必须是最终值（代码），
 * 用户看到的是代码而不是「广东」，而且各浏览器过滤行为不一致。
 */
import { computed, nextTick, ref } from 'vue'
import { NATIONWIDE } from '../utils/topics.js'

const props = defineProps({
  provinces: { type: Array, default: () => [] },
  modelValue: { type: String, default: '' },
  /** 是否提供「全国 / 不限」项（不过滤省份） */
  allowAll: { type: Boolean, default: false },
  /**
   * 「全国」项的选中值。默认取后端同款字面量 ``全国``：
   * 空串已被「未选择」占用，选全国若也用空串，两者在状态上无法区分。
   */
  allValue: { type: String, default: NATIONWIDE },
})
const emit = defineEmits(['update:modelValue', 'change'])

const query = ref('')
const open = ref(false)
const activeIndex = ref(-1)
const inputEl = ref(null)

const selectedName = computed(() => {
  // 「全国」不在省份字典里，单独认一下，否则选完输入框会退回「搜索省份…」
  if (props.modelValue === props.allValue) return '全国 / 不限'
  const found = props.provinces.find((p) => p.code === props.modelValue)
  return found ? found.name : ''
})

/** 过滤后的候选。输入为空时展示全部（含「全国」项）。 */
const options = computed(() => {
  const q = query.value.trim().toLowerCase()
  const list = props.provinces.filter((p) => {
    if (!q) return true
    return (
      p.name.toLowerCase().includes(q) ||
      (p.shortName || '').toLowerCase().includes(q) ||
      p.code.includes(q)
    )
  })
  if (props.allowAll && (!q || '全国不限'.includes(q))) {
    return [{ code: props.allValue, name: '全国 / 不限', shortName: '全国' }, ...list]
  }
  return list
})

function choose(item) {
  emit('update:modelValue', item.code)
  emit('change', item)
  query.value = ''
  open.value = false
  activeIndex.value = -1
}

function onInput() {
  open.value = true
  activeIndex.value = options.value.length ? 0 : -1
}

function onFocus() {
  open.value = true
  nextTick(() => inputEl.value?.select?.())
}

function onBlur() {
  // 延迟关闭，否则点选项时 blur 先触发、选项被卸载
  setTimeout(() => {
    open.value = false
    query.value = ''
  }, 120)
}

function onKeydown(event) {
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    open.value = true
    activeIndex.value = Math.min(activeIndex.value + 1, options.value.length - 1)
  } else if (event.key === 'ArrowUp') {
    event.preventDefault()
    activeIndex.value = Math.max(activeIndex.value - 1, 0)
  } else if (event.key === 'Enter') {
    if (open.value && options.value[activeIndex.value]) {
      event.preventDefault()
      choose(options.value[activeIndex.value])
    }
  } else if (event.key === 'Escape') {
    open.value = false
    query.value = ''
  }
}
</script>

<template>
  <div class="picker">
    <input
      ref="inputEl"
      class="picker-input"
      type="text"
      role="combobox"
      :aria-expanded="open"
      :placeholder="selectedName ? `${selectedName}（可重新搜索）` : '搜索省份，如 广东 / 江苏'"
      :value="query"
      @input="query = $event.target.value; onInput()"
      @focus="onFocus"
      @blur="onBlur"
      @keydown="onKeydown"
    />
    <ul v-if="open" class="picker-list" role="listbox">
      <li
        v-for="(item, index) in options"
        :key="item.code || '__all__'"
        class="picker-item"
        :class="{ active: index === activeIndex, selected: item.code === modelValue }"
        role="option"
        :aria-selected="item.code === modelValue"
        @mousedown.prevent="choose(item)"
        @mouseenter="activeIndex = index"
      >
        <span>{{ item.name }}</span>
        <span class="code">{{ item.code || '—' }}</span>
      </li>
      <li v-if="!options.length" class="picker-empty">没有匹配的省份</li>
    </ul>
  </div>
</template>

<style scoped>
.picker { position: relative; min-width: 240px; }

.picker-input {
  width: 100%;
  padding: 6px 10px;
  border: 1px solid var(--c-border);
  border-radius: var(--radius-md);
  background: var(--c-surface);
  font-size: 13px;
}

.picker-input:focus { outline: 2px solid var(--c-primary, #185fa5); outline-offset: -1px; }

.picker-list {
  position: absolute;
  z-index: 20;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  max-height: 280px;
  overflow-y: auto;
  margin: 0;
  padding: 4px;
  list-style: none;
  background: var(--c-surface);
  border: 1px solid var(--c-border);
  border-radius: var(--radius-md);
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.08);
}

.picker-item {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 8px;
  border-radius: 4px;
  font-size: 13px;
  cursor: pointer;
}

.picker-item.active { background: var(--c-hover, #eef4fb); }
.picker-item.selected { font-weight: 500; }
.picker-item .code { color: var(--c-muted, #888); font-variant-numeric: tabular-nums; }
.picker-empty { padding: 10px; color: var(--c-muted, #888); font-size: 13px; text-align: center; }
</style>
