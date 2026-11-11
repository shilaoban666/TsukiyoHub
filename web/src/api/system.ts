/**
 * 系统接口：健康检查、生效配置、人设回显。
 *
 * - `GET /healthz` / `GET /readyz`：探活（**不在 /api 前缀下**）
 * - `GET /api/system/config` → SystemConfig（脱敏，只读部分 + 可写字段的当前值）
 * - `GET /api/system/persona` → PersonaConfig（人设长文本的回显专用，避免塞进 config）
 * - `PUT /api/system/config` → SystemConfig（只接受 9 个可写字段，部分覆盖）
 * - `GET /api/system/info` → 后端自述信息
 */
import { http, setApiKey } from './client'
import type { HealthInfo, PersonaConfig, SystemConfig, SystemConfigPatch } from '@/types/api'

/**
 * 探活。用 fetch 而不是 axios：
 * 1) `/healthz` 不在 `/api` 前缀下，axios 实例的 baseURL 是 `/api`；
 * 2) 探活需要「失败也不弹 toast、超时短」的语义，fetch + AbortSignal.timeout 最干净。
 */
export async function checkHealth(timeoutMs = 4000): Promise<HealthInfo> {
  const ctrl = new AbortController()
  const timer = window.setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const res = await fetch('/healthz', { signal: ctrl.signal, headers: { Accept: 'application/json' } })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    return (await res.json()) as HealthInfo
  } finally {
    window.clearTimeout(timer)
  }
}

/** 生效配置（模型 / 向量后端 / 检索参数 / 风格 / 功能开关） */
export async function getSystemConfig(): Promise<SystemConfig> {
  return http.get<SystemConfig>('/system/config')
}

/** 人设等可写配置的回显 */
export async function getPersona(): Promise<PersonaConfig> {
  return http.get<PersonaConfig>('/system/persona', undefined, { silent: true })
}

/** 部分覆盖保存（只提交用户真正改过的字段） */
export async function saveSystemConfig(patch: SystemConfigPatch): Promise<SystemConfig> {
  return http.put<SystemConfig>('/system/config', patch)
}

/** 后端自述信息（设置页「关于」区块） */
export async function getSystemInfo(): Promise<Record<string, unknown>> {
  return http.get<Record<string, unknown>>('/system/info', undefined, { silent: true })
}

/** 设置页保存 API Key（写入 localStorage，下一次请求立即生效） */
export function saveApiKey(key: string): void {
  setApiKey(key.trim())
}