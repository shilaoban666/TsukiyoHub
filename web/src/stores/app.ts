/**
 * 全局应用状态：主题、API Key、后端连通性。
 *
 * 持久化用 `pinia-plugin-persistedstate` + `safeLocalStorage`（隐私模式下不抛错）。
 * key 固定为 `tsukiyohub-app`——`index.html` 的首帧脚本按这个 key 读主题，别改。
 */
import { defineStore } from 'pinia'
import { checkHealth } from '@/api/system'
import { getApiKey, setApiKey } from '@/api/client'
import { safeLocalStorage } from '@/utils/storage'
import type { HealthInfo } from '@/types/api'

export type ThemeMode = 'light' | 'dark' | 'system'

/** 后端连通状态：null = 还没探测过 */
export interface BackendState {
  online: boolean | null
  checking: boolean
  version?: string
  llmReady?: boolean
  embeddingReady?: boolean
  vectorStore?: string
  documents?: number
  chunks?: number
  error?: string
  checkedAt?: number
}

export const useAppStore = defineStore('app', {
  state: () => ({
    themeMode: 'system' as ThemeMode,
    systemDark: false,
    apiKey: getApiKey(),
    backend: {
      online: null,
      checking: false,
    } as BackendState,
  }),

  getters: {
    isDark(state): boolean {
      return state.themeMode === 'dark' || (state.themeMode === 'system' && state.systemDark)
    },
    /** 后端是否确认不可达（用于顶部离线横幅；尚未探测时不要吓用户） */
    offline(state): boolean {
      return state.backend.online === false
    },
  },

  actions: {
    setTheme(mode: ThemeMode) {
      this.themeMode = mode
    },
    /** 浅色 ↔ 深色一键切换（当前是 system 时，按实际显示效果切到相反的一侧） */
    toggleTheme() {
      this.themeMode = this.isDark ? 'light' : 'dark'
    },
    setSystemDark(dark: boolean) {
      this.systemDark = dark
    },
    saveApiKey(key: string) {
      this.apiKey = key.trim()
      setApiKey(this.apiKey)
    },

    /** 探活：失败时记录原因，不抛错（首页不该因为后端没起而白屏） */
    async refreshHealth(): Promise<boolean> {
      if (this.backend.checking) return this.backend.online === true
      this.backend.checking = true
      try {
        const info: HealthInfo = await checkHealth()
        this.backend = {
          online: true,
          checking: false,
          version: info.version,
          llmReady: info.llm_ready,
          embeddingReady: info.embedding_ready,
          vectorStore: info.vector_store,
          documents: info.document_count,
          chunks: info.chunk_count,
          checkedAt: Date.now(),
        }
        return true
      } catch (e) {
        this.backend = {
          online: false,
          checking: false,
          error: (e as Error)?.message || '无法连接后端',
          checkedAt: Date.now(),
        }
        return false
      }
    },
  },

  persist: {
    key: 'tsukiyohub-app',
    pick: ['themeMode', 'apiKey'],
    storage: safeLocalStorage,
  },
})