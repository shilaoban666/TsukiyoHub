/**
 * 技能 store：列表 + 启停开关。
 *
 * 技能页和聊天右栏共用同一份数据，所以放 store 而不是页内 state。
 * 启停用「乐观更新 + 失败回滚」：开关点击立刻响应，接口失败再翻回去并 toast。
 */
import { defineStore } from 'pinia'
import { listSkills, setSkillEnabled } from '@/api/skills'
import { errorText } from '@/api/client'
import type { SkillInfo } from '@/types/api'

export const useSkillsStore = defineStore('skills', {
  state: () => ({
    items: [] as SkillInfo[],
    loading: false,
    error: '',
    loaded: false,
    /** 正在切换的技能名（用于只禁用那一行的开关） */
    pending: '' as string,
  }),

  getters: {
    enabledCount(state): number {
      return state.items.filter((s) => s.enabled).length
    },
    byKind(state) {
      return (kind: SkillInfo['kind']) => state.items.filter((s) => s.kind === kind)
    },
  },

  actions: {
    async load(force = false) {
      if (this.loading) return
      if (this.loaded && !force) return
      this.loading = true
      this.error = ''
      try {
        const { items } = await listSkills()
        this.items = items
        this.loaded = true
      } catch (e) {
        this.error = errorText(e)
      } finally {
        this.loading = false
      }
    },

    /** 切换启用状态：先改本地，失败回滚 */
    async toggle(name: string) {
      const target = this.items.find((s) => s.name === name)
      if (!target || this.pending) return
      const prev = target.enabled
      target.enabled = !prev
      this.pending = name
      try {
        const updated = await setSkillEnabled(name, target.enabled)
        // 后端返回可能是空体：有内容才覆盖，避免把本地状态清掉
        if (updated && typeof updated.enabled === 'boolean') target.enabled = updated.enabled
        if (updated && updated.calls != null) target.calls = updated.calls
      } catch (e) {
        target.enabled = prev
        throw e
      } finally {
        this.pending = ''
      }
    },
  },
})