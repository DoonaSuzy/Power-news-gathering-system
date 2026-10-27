/**
 * 错误文案的映射规则。
 *
 * 最要紧的一条是「不许用通用话术覆盖后端原文」：中台的 detail 本身就是
 * 可执行的（例如「跨域查询最多翻到第 250 页」），换成「加载失败」等于把
 * 用户唯一的线索删掉。
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { formatApiError } from '../src/utils/errors.js'

test('401 引导去填 API Key，而不是显示「加载失败」', () => {
  const info = formatApiError({ status: 401, code: 2001, message: '缺少 Bearer Token' })
  assert.equal(info.action, 'token')
  assert.match(info.title, /未授权/)
})

test('连不上中台时给出网络/代理方向，并允许重试', () => {
  const info = formatApiError({ code: -3, message: '无法连接中台' })
  assert.match(info.title, /无法连接/)
  assert.equal(info.action, 'retry')
})

test('参数类错误保留后端原文（分页上限写了具体页数）', () => {
  const info = formatApiError({
    code: 1000,
    message: '跨域查询最多翻到第 250 页（可翻页扫描窗口 5000 条…）',
  })
  assert.match(info.hint, /250 页/)
})

test('409 冲突给出「回列表重开」的恢复路径', () => {
  const info = formatApiError({ status: 409, code: 4002, message: '编号冲突' })
  assert.match(info.title, /冲突/)
  assert.equal(info.action, 'retry')
})

test('5xx 带上 request_id 尾号，便于对日志', () => {
  const info = formatApiError({
    status: 500,
    code: 5000,
    message: '内部错误',
    requestId: 'abcdef0123456789',
  })
  assert.match(info.hint, /23456789/)
})
