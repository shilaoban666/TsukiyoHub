/**
 * 取消类异常的判定：统一放一处，main.ts / 页面 / 请求层都用它。
 *
 * 为什么需要：用户点「停止生成」时 AbortController.abort() 会让 fetch 抛 DOMException，
 * axios 取消抛 CanceledError。它们不是 bug，不该弹 toast，也不该进 errorHandler。
 */
export function isCanceled(e: unknown): boolean {
  if (!e) return false
  const anyErr = e as { name?: string; code?: string; message?: string }
  return (
    anyErr.name === 'AbortError' ||
    anyErr.name === 'CanceledError' ||
    anyErr.code === 'ERR_CANCELED' ||
    /aborted|canceled/i.test(anyErr.message ?? '')
  )
}