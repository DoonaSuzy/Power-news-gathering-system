/**
 * 把 ``ApiError`` 翻译成「用户能照着做」的提示。
 *
 * 为什么单独一个模块而不是塞进 ``client.js``
 * -------------------------------------------
 * 1. ``client.js`` 顶层读 ``import.meta.env``，Node 的 ``node --test`` 无法直接
 *    import，而这段映射逻辑是最需要单测的部分；
 * 2. 文案与「点哪个按钮」属于展示层，请求层只该负责把错误**原样**抛出来。
 *
 * 设计原则
 * --------
 * - **不要用通用话术覆盖后端原文**。中台的 ``detail`` 是写给人看的
 *   （「跨域查询最多翻到第 250 页」这种），换成「加载失败」等于把线索删掉。
 * - 只有「怎么恢复」需要前端补：401 要引导去填 Key，网络错误要能重试。
 *
 * @module utils/errors
 */

/** 后端业务码（与 newsplatform/core/exceptions.py 的 ERROR_CODES 对齐）。 */
const CODE_CONNECT = -3
const CODE_TIMEOUT = -2
const CODE_INVALID = 1000
const CODE_VALIDATION = 1001
const CODE_OUT_OF_RANGE = 1002
const CODE_AMBIGUOUS = 4002

/**
 * @typedef {object} ErrorInfo
 * @property {'error'|'notice'} tone 视觉语气
 * @property {string} title 一句话结论
 * @property {string} hint  可执行的下一步 / 后端原文
 * @property {'retry'|'token'|''} action 需要哪个按钮
 */

/**
 * @param {any} err 通常是 ApiError，但网络异常也可能漏进来
 * @returns {ErrorInfo}
 */
export function formatApiError(err) {
  const message = err?.message || String(err || '未知错误')
  const code = Number(err?.code ?? -1)
  const status = Number(err?.status ?? 0)
  const isAuth = Boolean(err?.isAuthError) || status === 401 || status === 403

  if (isAuth) {
    return {
      tone: 'error',
      title: `未授权（HTTP ${status || 401}）`,
      hint: 'API Key 为空或已失效。在「设置」里填入有效的 pn_ 开头的 Key 后重试。',
      action: 'token',
    }
  }

  if (code === CODE_CONNECT) {
    return {
      tone: 'error',
      title: '无法连接中台',
      hint: `${message}。确认中台已启动、地址正确（VITE_API_BASE），公司网络下访问内网地址可能需要关掉 HTTP 代理。`,
      action: 'retry',
    }
  }

  if (code === CODE_TIMEOUT) {
    return {
      tone: 'error',
      title: '请求超时',
      hint: '中台在跑跨库聚合时可能变慢。可以重试，或把时间范围缩小到「今日 / 近 1 周」。',
      action: 'retry',
    }
  }

  if (code === CODE_AMBIGUOUS) {
    return {
      tone: 'error',
      title: '编号冲突（HTTP 409）',
      hint: '该编号在电力与省级两个数据域都有记录，请从列表里点开，或指定数据域后重试。',
      action: 'retry',
    }
  }

  if (code === CODE_INVALID || code === CODE_VALIDATION || code === CODE_OUT_OF_RANGE) {
    return {
      tone: 'error',
      title: '请求未被接受',
      hint: message, // 后端 detail 本身就是可执行的（例如分页上限写明了具体页数）
      action: '',
    }
  }

  if (status >= 500 || code >= 5000) {
    const tail = err?.requestId ? `（request_id …${String(err.requestId).slice(-8)}）` : ''
    return {
      tone: 'error',
      title: `中台异常（HTTP ${status || code}）`,
      hint: `${message}${tail}。稍后重试；持续失败请把 request_id 一起报给运维。`,
      action: 'retry',
    }
  }

  return { tone: 'error', title: '查询失败', hint: message, action: 'retry' }
}
