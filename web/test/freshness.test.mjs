/**
 * 「数据有多旧」的判定与措辞。
 *
 * 省级域是历史快照（收录停在 2026-10-10），但界面一直按「最新新闻」呈现。
 * 这里的三个用例分别守住：会报警、不会误报、措辞不能说成「坏了」。
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { describeFreshness } from '../src/utils/topics.js'

const NOW = new Date('2026-10-27T22:00:00+08:00')

test('冻结 17 天要判定为 stale，且写明截至日期', () => {
  const info = describeFreshness('2026-10-10T00:00:00', NOW)
  assert.equal(info.stale, true)
  assert.equal(info.days, 17)
  assert.equal(info.date, '2026-10-10')
  assert.match(info.text, /历史快照/)
  assert.match(info.text, /2026-10-10/)
})

test('措辞不能出现「故障/失败/错误」——这是数据边界，不是系统坏了', () => {
  const { text } = describeFreshness('2026-10-10T00:00:00', NOW)
  for (const word of ['故障', '失败', '错误', '异常']) {
    assert.equal(text.includes(word), false, `文案不应包含「${word}」`)
  }
})

test('1 天内的数据不报警（阈值默认 3 天）', () => {
  assert.equal(describeFreshness('2026-10-26T22:00:00', NOW).stale, false)
})

test('没有最新时间时保持沉默，不误报', () => {
  const info = describeFreshness(null, NOW)
  assert.equal(info.stale, false)
  assert.equal(info.days, null)
  assert.equal(info.text, '')
})
