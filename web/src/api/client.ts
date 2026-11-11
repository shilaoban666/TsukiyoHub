/**
 * 统一 HTTP 客户端：axios 实例 + 错误归一化 + 全局 toast。
 *
 * 设计要点：
 * 1. **错误只归一化一次**：所有调用方拿到的都是 `ApiError`，带 `isNetwork` 与可读 `message`，
 *    不用每个页面自己判断 `err.response?.data?.detail`。
 * 2. **网络不通要说清楚**：后端没起时提示「无法连接后端（http://127.0.0.1:8080）」而不是
 *    干巴巴的 `Network Error`，并让页面显示重试按钮。
 * 3. **API Key 直接读 localStorage**：避免 store ↔ client 循环依赖；设置页写入后下一次请求立即生效。
 * 4. 只有非 `silent` 的请求才弹全局 toast，页面级错误自己处理时可以关掉。
 */
import axios, { type AxiosInstance, type AxiosRequestConfig, type AxiosProgressEvent } from 'axios'
import { message } from 'ant-design-vue'
import { local } from '@/utils/storage'

/** API 前缀：与 vite proxy / 生产网关约定一致 */
export const API_BASE = '/api'

/** 后端默认地址，仅用于错误文案（浏览器里真实请求走同源代理） */
export const BACKEND_HINT = 'http://127.0.0.1:8080'

const API_KEY_STORAGE = 'tsukiyohub-api-key'

export function getApiKey(): string {
  return (local.get<string>(API_KEY_STORAGE, '') as string) || ''
}

export function setApiKey(key: string): void {
  if (key) local.set(API_KEY_STORAGE, key)
  else local.remove(API_KEY_STORAGE)
}

/** 归一化后的接口错误：所有 api/* 抛出的都是它 */
export class ApiError extends Error {
  /** HTTP 状态码；网络错误时为 undefined */
  status?: number
  /** 后端 detail 原文 */
  detail: string
  /** true = 请求根本没到后端（后端未启动 / 断网 / 被 CORS 拦） */
  isNetwork: boolean

  constructor(msg: string, opt: { status?: number; detail?: string; isNetwork?: boolean } = {}) {
    super(msg)
    this.name = 'ApiError'
    this.status = opt.status
    this.detail = opt.detail ?? ''
    this.isNetwork = opt.isNetwork ?? false
  }
}

/** 后端错误体 → 可读文案（FastAPI 的 detail 可能是字符串也可能是校验错误数组） */
function readDetail(body: unknown): string {
  if (!body) return ''
  if (typeof body === 'string') return body
  const data = body as { detail?: unknown; message?: unknown }
  if (typeof data.detail === 'string') return data.detail
  if (typeof data.message === 'string') return data.message
  if (Array.isArray(data.detail)) {
    // pydantic 422：[{loc: [...], msg: "..."}]
    return data.detail
      .map((item) => {
        const it = item as { loc?: unknown[]; msg?: string }
        const where = Array.isArray(it.loc) ? it.loc.slice(-1).join('.') : ''
        return where ? `${where}: ${it.msg ?? ''}` : (it.msg ?? '')
      })
      .filter(Boolean)
      .join('；')
  }
  return ''
}

/** HTTP 状态码兜底文案 */
function statusText(status: number, detail: string): string {
  if (detail) return detail
  const map: Record<number, string> = {
    400: '请求参数有误',
    401: 'API Key 无效或缺失，请到「设置」页填写',
    403: '没有权限访问该接口',
    404: '接口不存在（后端版本可能较旧）',
    413: '上传文件超过大小限制',
    422: '请求参数校验失败',
    429: '请求太频繁，请稍后再试',
    500: '后端内部错误',
    502: '上游模型服务异常',
    503: '后端服务暂不可用',
  }
  return map[status] || `请求失败（HTTP ${status}）`
}

const instance: AxiosInstance = axios.create({
  baseURL: API_BASE,
  timeout: 60000,
  headers: { Accept: 'application/json' },
})

instance.interceptors.request.use((config) => {
  const key = getApiKey()
  if (key) config.headers.set('Authorization', `Bearer ${key}`)
  return config
})

instance.interceptors.response.use(
  (res) => res,
  (error) => {
    // 主动取消：交给调用方判断（不弹 toast）
    if (axios.isCancel(error)) throw error

    const response = error?.response
    if (!response) {
      throw new ApiError(`无法连接后端（${BACKEND_HINT}），请确认服务已启动`, {
        isNetwork: true,
      })
    }
    const status = response.status as number
    const detail = readDetail(response.data)
    throw new ApiError(statusText(status, detail), { status, detail })
  },
)

export interface RequestOptions {
  /** 不弹全局错误提示，由调用方自己展示 */
  silent?: boolean
  signal?: AbortSignal
  timeout?: number
}

/** 统一的调用入口：成功返回 data，失败抛 ApiError（并按需弹 toast） */
export async function request<T>(config: AxiosRequestConfig, opt: RequestOptions = {}): Promise<T> {
  try {
    const res = await instance.request<T>({ ...config, signal: opt.signal, timeout: opt.timeout })
    return res.data
  } catch (e) {
    const err = e instanceof ApiError ? e : new ApiError((e as Error)?.message || '请求失败', {})
    if (!opt.silent && !axios.isCancel(e)) {
      if (err.status === 401 || err.status === 403) message.warning(err.message)
      else message.error(err.message)
    }
    throw err
  }
}

/** 便捷方法：语义清晰，参数顺序固定 */
export const http = {
  get: <T>(url: string, params?: Record<string, unknown>, opt?: RequestOptions) =>
    request<T>({ url, method: 'GET', params }, opt),

  post: <T>(url: string, data?: unknown, opt?: RequestOptions) =>
    request<T>({ url, method: 'POST', data }, opt),

  put: <T>(url: string, data?: unknown, opt?: RequestOptions) =>
    request<T>({ url, method: 'PUT', data }, opt),

  patch: <T>(url: string, data?: unknown, opt?: RequestOptions) =>
    request<T>({ url, method: 'PATCH', data }, opt),

  del: <T>(url: string, params?: Record<string, unknown>, opt?: RequestOptions) =>
    request<T>({ url, method: 'DELETE', params }, opt),

  /**
   * 上传：multipart/form-data + 进度回调。
   * 旧前端那套分片/OSS 直传（200 行）对本项目是过度设计：后端 max_upload_mb=32、本地存储，
   * 一个 FormData POST 足够。
   */
  upload: <T>(
    url: string,
    form: FormData,
    onProgress?: (percent: number) => void,
    opt?: RequestOptions,
  ) =>
    request<T>(
      {
        url,
        method: 'POST',
        data: form,
        // 不手动设置 Content-Type：让浏览器带上 boundary
        onUploadProgress: (e: AxiosProgressEvent) => {
          if (!onProgress) return
          const total = e.total ?? 0
          onProgress(total > 0 ? Math.round((e.loaded / total) * 100) : 0)
        },
      },
      opt,
    ),
}

/** 从任意异常里取出可读文案（流式请求不走 axios，需要单独归一化） */
export function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.message
  if (e instanceof Error) return e.message
  if (typeof e === 'string') return e
  return '未知错误'
}

/** 是否为「后端不可达」类错误（决定是否显示离线横幅） */
export function isNetworkError(e: unknown): boolean {
  return e instanceof ApiError && e.isNetwork
}