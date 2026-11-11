/**
 * 本地存储的安全封装：隐私模式 / 禁用存储时读写都不抛错。
 * 直接从旧前端 `utils/storage.ts` 复用（这是纯工具，零业务耦合）。
 */

function wrap(getStore: () => Storage) {
  return {
    get<T = string>(key: string, fallback: T | null = null): T | null {
      try {
        const raw = getStore().getItem(key)
        if (raw == null) return fallback
        try {
          return JSON.parse(raw) as T
        } catch {
          return raw as unknown as T
        }
      } catch {
        return fallback
      }
    },
    set(key: string, value: unknown) {
      try {
        getStore().setItem(key, typeof value === 'string' ? value : JSON.stringify(value))
      } catch {
        /* 隐私模式：忽略写入失败 */
      }
    },
    remove(key: string) {
      try {
        getStore().removeItem(key)
      } catch {
        /* ignore */
      }
    },
  }
}

export const local = wrap(() => window.localStorage)
export const session = wrap(() => window.sessionStorage)

/** 供 pinia-plugin-persistedstate 使用的安全 storage */
export const safeLocalStorage: Pick<Storage, 'getItem' | 'setItem'> = {
  getItem(key) {
    try {
      return window.localStorage.getItem(key)
    } catch {
      return null
    }
  },
  setItem(key, value) {
    try {
      window.localStorage.setItem(key, value)
    } catch {
      /* ignore */
    }
  },
}