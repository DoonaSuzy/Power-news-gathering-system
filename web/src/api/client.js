/**
 * 统一请求层。
 *
 * 只做四件事，保持薄：
 * 1. 拼 URL 与 query（数组参数展开成重复键，中台的 ``province`` 只收单值）；
 * 2. 带 Bearer Token；
 * 3. 超时（AbortController）—— 中台在跑真库聚合时可能慢，但不能无限等；
 * 4. 解包中台的统一响应体 ``{code, data, request_id}``。
 *
 * **不**在这里做业务降级或缓存。降级属于 AI 能力层（``api/ai.js``），
 * 缓存属于调用方的 composable，混在一起会让「这个值是缓存来的还是新拉的」
 * 永远说不清。
 */

const BASE = import.meta.env.VITE_API_BASE || '/api/v1'
const DEFAULT_TIMEOUT = 20_000

/** Token 存 localStorage 而不是会话内存：刷新页面不该丢登录态。 */
const TOKEN_KEY = 'powernews.token'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || import.meta.env.VITE_API_TOKEN || ''
}

export function setToken(value) {
  if (value) localStorage.setItem(TOKEN_KEY, value)
  else localStorage.removeItem(TOKEN_KEY)
}

export class ApiError extends Error {
  constructor(message, { status = 0, code = -1, requestId = '', payload = null } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.requestId = requestId
    /**
     * 整包响应体。``extra`` 类信息（409 的 ``candidates``、422 的 ``errors``）
     * 都在 body 顶层，只留 message 会让调用方无从恢复。
     */
    this.payload = payload
  }

  /** 401/403：调用方据此提示「去填 API Key」而不是「加载失败」。 */
  get isAuthError() {
    return this.status === 401 || this.status === 403
  }
}

function buildQuery(params) {
  if (!params) return ''
  const usp = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) {
      // 中台多数筛选参数只接受单值；数组时按重复键传，由后端决定取哪个。
      for (const item of value) usp.append(key, String(item))
    } else {
      usp.append(key, String(value))
    }
  }
  const qs = usp.toString()
  return qs ? `?${qs}` : ''
}

/**
 * 发一次 GET 请求并返回解包后的 ``data``。
 *
 * @param {string} path 相对 ``VITE_API_BASE`` 的路径，如 ``/articles``。
 * @param {object} [options]
 * @param {object} [options.params] query 参数，空值会被跳过。
 * @param {number} [options.timeout] 超时毫秒数。
 * @param {AbortSignal} [options.signal] 外部取消信号（组件卸载时用）。
 * @returns {Promise<any>} 中台响应体里的 ``data``。
 * @throws {ApiError} 网络错误 / 超时 / 非 0 业务码 / HTTP 非 200。
 */
export async function apiGet(path, { params, timeout = DEFAULT_TIMEOUT, signal } = {}) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(new Error('timeout')), timeout)
  // 外部 signal 与超时 signal 需要并联：任一触发都取消
  if (signal) {
    if (signal.aborted) controller.abort(signal.reason)
    else signal.addEventListener('abort', () => controller.abort(signal.reason), { once: true })
  }

  let resp
  try {
    resp = await fetch(`${BASE}${path}${buildQuery(params)}`, {
      method: 'GET',
      headers: {
        Accept: 'application/json',
        ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
      },
      signal: controller.signal,
    })
  } catch (err) {
    if (controller.signal.aborted) {
      throw new ApiError(signal?.aborted ? '请求已取消' : `请求超时（${timeout}ms）`, { code: -2 })
    }
    // 走到这里通常是中台没起，或请求被本机代理拦了
    throw new ApiError(`无法连接中台（${BASE}）：${err.message}`, { code: -3 })
  } finally {
    clearTimeout(timer)
  }

  let body = null
  try {
    body = await resp.json()
  } catch {
    throw new ApiError(`响应不是 JSON（HTTP ${resp.status}）`, { status: resp.status })
  }

  if (!resp.ok) {
    throw new ApiError(body?.detail || `HTTP ${resp.status}`, {
      status: resp.status,
      code: body?.code ?? resp.status,
      requestId: body?.request_id,
      payload: body,
    })
  }
  if (body?.code !== 0) {
    throw new ApiError(body?.detail || `业务错误码 ${body?.code}`, {
      status: resp.status,
      code: body?.code,
      requestId: body?.request_id,
      payload: body,
    })
  }
  return body.data
}
