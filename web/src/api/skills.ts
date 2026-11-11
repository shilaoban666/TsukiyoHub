/**
 * 技能接口。
 *
 * - `GET /api/skills` → `{items: SkillInfo[], total}`
 * - `PATCH /api/skills/{name}` body `{"enabled": bool}` → 更新后的 SkillInfo
 */
import { http } from './client'
import { normalizeList } from './sessions'
import type { ListResult, SkillInfo } from '@/types/api'

/** 技能列表（内置工具 / SKILL.md 技能 / MCP 工具） */
export async function listSkills(): Promise<ListResult<SkillInfo>> {
  const data = await http.get<unknown>('/skills')
  return normalizeList<SkillInfo>(data)
}

/** 启用 / 停用某个技能 */
export async function setSkillEnabled(name: string, enabled: boolean): Promise<SkillInfo> {
  return http.patch<SkillInfo>(`/skills/${encodeURIComponent(name)}`, { enabled })
}