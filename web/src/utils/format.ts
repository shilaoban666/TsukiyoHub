/**
 * 格式化工具：日期 / 数字 / 字节 / 百分比 / HTML 转义。
 * 从旧前端 `utils/format.ts` 复用，去掉了 i18n 依赖（本项目单语中文）。
 */
import dayjs from 'dayjs'

/** 日期：2026-09-28 */
export function fmtDate(v?: string | number | Date | null): string {
  if (!v) return '-'
  const d = dayjs(v)
  return d.isValid() ? d.format('YYYY-MM-DD') : '-'
}

/** 日期时间：2026-09-28 14:03 */
export function fmtDateTime(v?: string | number | Date | null): string {
  if (!v) return '-'
  const d = dayjs(v)
  return d.isValid() ? d.format('YYYY-MM-DD HH:mm') : '-'
}

/** 时间：14:03:22 */
export function fmtTime(v?: string | number | Date | null): string {
  if (!v) return '-'
  const d = dayjs(v)
  return d.isValid() ? d.format('HH:mm:ss') : '-'
}

/**
 * 相对时间：刚刚 / 5 分钟前 / 昨天 14:03 / 2026-09-28
 * 会话列表用它排序后展示，比绝对时间更符合聊天场景。
 */
export function fmtRelative(v?: string | number | Date | null): string {
  if (!v) return '-'
  const d = dayjs(v)
  if (!d.isValid()) return '-'
  const now = dayjs()
  const diffMin = now.diff(d, 'minute')
  if (diffMin < 1) return '刚刚'
  if (diffMin < 60) return `${diffMin} 分钟前`
  if (d.isSame(now, 'day')) return d.format('HH:mm')
  if (d.isSame(now.subtract(1, 'day'), 'day')) return `昨天 ${d.format('HH:mm')}`
  if (d.isSame(now, 'year')) return d.format('MM-DD')
  return d.format('YYYY-MM-DD')
}

/** 数字千分位 */
export function fmtNumber(n?: number | null, digits = 0): string {
  if (n == null || Number.isNaN(n)) return '-'
  return new Intl.NumberFormat('zh-CN', {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  }).format(n)
}

/** 分数保留 3 位（检索相似度用） */
export function fmtScore(n?: number | null): string {
  if (n == null || Number.isNaN(n)) return '-'
  return n.toFixed(3)
}

/** 百分比：0.8234 → 82.3% */
export function fmtPercent(n?: number | null, digits = 1): string {
  if (n == null || Number.isNaN(n)) return '-'
  return `${(n * 100).toFixed(digits)}%`
}

/** 字节：1536 → 1.5 KB */
export function fmtBytes(bytes?: number | null): string {
  if (bytes == null) return '-'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = bytes
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

/** 毫秒耗时：1.8s / 320ms */
export function fmtLatency(ms?: number | null): string {
  if (ms == null || Number.isNaN(ms)) return '-'
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`
}

/** 转义 HTML，用于把纯文本安全地塞进 v-html */
export function escapeHtml(s: string): string {
  return s.replace(
    /[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!,
  )
}

/** 关键词高亮：先转义再包 <mark>，结果可安全 v-html */
export function highlight(text: string, keyword?: string): string {
  const safe = escapeHtml(text || '')
  const words = (keyword || '')
    .split(/\s+/)
    .map((w) => w.trim())
    .filter(Boolean)
  if (!words.length) return safe
  const re = new RegExp(
    `(${words.map((w) => escapeHtml(w).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`,
    'gi',
  )
  return safe.replace(re, '<mark class="hl">$1</mark>')
}

/** 截断长文本 */
export function truncate(text: string, max = 120): string {
  const s = text || ''
  return s.length > max ? `${s.slice(0, max)}…` : s
}