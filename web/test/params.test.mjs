/**
 * 参数拼装的守卫：`province` 的三种状态不能混。
 *
 * 这三个用例都对应真实缺陷：
 * - 「全国」若被写成空串，`buildQuery` 会把它丢掉，请求上和「什么都没选」
 *   完全一样——而前者应该发请求、后者不应该；
 * - 「未选择」若被当成「全国」，首屏就会在用户选省之前把全库拉一遍。
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { NATIONWIDE, toQueryParams } from '../src/utils/topics.js'

test('「全国」原样透传，不能被吞成 undefined', () => {
  const params = toQueryParams({ province: NATIONWIDE, range: 'week', channel: 'all' })
  assert.equal(params.province, '全国')
})

test('空字符串表示「未选择」，不能当成全国', () => {
  const params = toQueryParams({ province: '', region: '', range: 'week' })
  assert.equal(params.province, undefined)
})

test('选了省份时以省份为准，大区只在没有省份时生效', () => {
  assert.equal(toQueryParams({ province: '440000', region: '华南' }).province, '440000')
  assert.equal(toQueryParams({ province: '', region: '华南' }).province, '华南')
})

test('时间参数只发一种：预设档位优先，自定义区间只在「不限时间」时生效', () => {
  assert.equal(toQueryParams({ range: 'week', since: '2026-10-01' }).since, undefined)
  assert.equal(toQueryParams({ range: 'all' }).range, undefined)
  // 「不限时间」档下才发自定义区间，否则后端会因为 range 与 since 同时给而 400
  assert.equal(toQueryParams({ range: 'all', since: '2026-10-01' }).since, '2026-10-01')
})
