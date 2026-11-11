<script setup lang="ts">
/**
 * 技能页：列出全部技能（内置工具 / SKILL.md 技能 / MCP 工具），支持启用停用与查看参数 schema。
 *
 * 开关走 `PATCH /api/skills/{name}`（乐观更新，失败回滚），调用次数来自后端统计。
 */
import { computed, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  CodeOutlined,
  DownOutlined,
  ReloadOutlined,
  RightOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons-vue'
import { useSkillsStore } from '@/stores/skills'
import { errorText } from '@/api/client'
import { fmtNumber } from '@/utils/format'
import type { SkillInfo } from '@/types/api'

const store = useSkillsStore()
const expanded = ref<Record<string, boolean>>({})

const KIND_LABEL: Record<SkillInfo['kind'], string> = {
  tool: '内置工具',
  skill: '技能文档',
  mcp: 'MCP 工具',
}

const groups = computed(() => {
  const order: SkillInfo['kind'][] = ['tool', 'skill', 'mcp']
  return order
    .map((kind) => ({ kind, label: KIND_LABEL[kind], items: store.byKind(kind) }))
    .filter((g) => g.items.length > 0)
})

function toggleExpand(name: string) {
  expanded.value = { ...expanded.value, [name]: !expanded.value[name] }
}

async function toggle(skill: SkillInfo) {
  try {
    await store.toggle(skill.name)
  } catch (e) {
    message.error(`切换失败：${errorText(e)}`)
  }
}

async function refresh() {
  await store.load(true)
}

/** JSON Schema 格式化展示 */
function schemaText(skill: SkillInfo): string {
  try {
    return JSON.stringify(skill.parameters ?? {}, null, 2)
  } catch {
    return '（无法解析参数结构）'
  }
}

onMounted(() => {
  void store.load()
})
</script>

<template>
  <div class="page">
    <div class="page-inner">
      <header class="page-head">
        <div>
          <h1 class="page-title">技能</h1>
          <p class="page-desc">
            技能是模型可以主动调用的能力单元（知识库检索、网页抓取、计算器等）。启用后模型才能使用。
          </p>
        </div>
        <div class="head-actions">
          <span class="summary text-2">
            已启用 {{ store.enabledCount }} / {{ store.items.length }}
          </span>
          <a-button :loading="store.loading" @click="refresh">
            <template #icon><ReloadOutlined /></template>
            刷新
          </a-button>
        </div>
      </header>

      <a-alert
        v-if="store.error"
        type="error"
        show-icon
        :message="store.error"
        description="请确认后端已启动（默认 http://127.0.0.1:8080）。"
      >
        <template #action>
          <a-button size="small" @click="refresh">重试</a-button>
        </template>
      </a-alert>

      <a-skeleton v-if="store.loading && !store.items.length" active :paragraph="{ rows: 8 }" />

      <div v-else-if="!store.items.length && !store.error" class="empty">
        <ThunderboltOutlined class="empty-icon" />
        <p>后端没有注册任何技能</p>
        <p class="text-3">
          内置技能可通过环境变量 <code>SKILLS_ENABLED</code> 开启，SKILL.md 技能放到
          <code>data/skills/</code> 下。
        </p>
      </div>

      <!-- 按类型分组 -->
      <section v-for="group in groups" :key="group.kind" class="group">
        <div class="block-head">
          <h2 class="block-title">{{ group.label }}</h2>
          <span class="text-3 count">{{ group.items.length }} 个</span>
        </div>

        <div class="cards">
          <article
            v-for="skill in group.items"
            :key="skill.name"
            class="card"
            :class="{ disabled: !skill.enabled }"
          >
            <div class="card-main">
              <div class="card-info">
                <div class="card-title-row">
                  <span class="card-title">{{ skill.title || skill.name }}</span>
                  <span class="card-name mono text-3">{{ skill.name }}</span>
                  <a-tag v-if="!skill.enabled" class="off-tag">已停用</a-tag>
                </div>
                <p class="card-desc">{{ skill.description || '（无描述）' }}</p>
                <div class="card-meta text-3">
                  <span>来源：{{ skill.source || 'builtin' }}</span>
                  <span>调用 {{ fmtNumber(skill.calls) }} 次</span>
                </div>
              </div>

              <div class="card-actions">
                <a-switch
                  :checked="skill.enabled"
                  :loading="store.pending === skill.name"
                  size="small"
                  @change="toggle(skill)"
                />
                <button class="expand" type="button" @click="toggleExpand(skill.name)">
                  <DownOutlined v-if="expanded[skill.name]" />
                  <RightOutlined v-else />
                  参数
                </button>
              </div>
            </div>

            <Transition name="slide-up">
              <div v-if="expanded[skill.name]" class="detail">
                <div class="detail-block">
                  <div class="detail-label">
                    <CodeOutlined /> 参数结构（JSON Schema）
                  </div>
                  <pre class="detail-pre mono">{{ schemaText(skill) }}</pre>
                </div>
                <div v-if="skill.rules" class="detail-block">
                  <div class="detail-label">使用规则</div>
                  <pre class="detail-pre rules">{{ skill.rules }}</pre>
                </div>
              </div>
            </Transition>
          </article>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped lang="less">
.summary {
  font-size: var(--fs-xs);
  white-space: nowrap;
}

.group {
  display: flex;
  flex-direction: column;
}
.count {
  font-size: var(--fs-xs);
}

.cards {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.card {
  background: var(--color-bg-card);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);
  padding: var(--space-3) var(--space-4);
  transition: border-color var(--dur-fast) var(--ease);

  &:hover {
    border-color: var(--color-border);
  }
  &.disabled {
    background: var(--color-bg-sunken);
  }
}

.card-main {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
}

.card-info {
  flex: 1;
  min-width: 0;
}

.card-title-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.card-title {
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--color-text-1);
}
.card-name {
  font-size: 11px;
}
.off-tag {
  margin: 0;
  font-size: 11px;
}

.card-desc {
  margin: 4px 0 0;
  font-size: var(--fs-xs);
  line-height: 1.7;
  color: var(--color-text-2);
}

.card-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  margin-top: 6px;
  font-size: 11px;
}

.card-actions {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--space-2);
  flex: 0 0 auto;
}

.expand {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 0;
  background: transparent;
  padding: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-2);
  cursor: pointer;

  &:hover {
    color: var(--color-primary);
  }
}

.detail {
  margin-top: var(--space-3);
  padding-top: var(--space-3);
  border-top: 1px dashed var(--color-border);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.detail-label {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: var(--fs-xs);
  color: var(--color-text-2);
  margin-bottom: 4px;
}

.detail-pre {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  background: var(--color-bg-sunken);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-sm);
  font-size: 12px;
  line-height: 1.6;
  color: var(--color-text-1);
  max-height: 260px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;

  &.rules {
    font-family: var(--font-family);
  }
}

.empty {
  padding: 48px 0;
  text-align: center;
  color: var(--color-text-2);
  font-size: var(--fs-sm);

  p {
    margin: 4px 0;
  }
  code {
    font-family: var(--font-mono);
    background: var(--color-bg-hover);
    padding: 1px 5px;
    border-radius: 4px;
  }
}
.empty-icon {
  font-size: 26px;
  color: var(--color-text-3);
  margin-bottom: var(--space-2);
}
</style>