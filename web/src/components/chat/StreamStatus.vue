<script setup lang="ts">
/**
 * 状态条：思考中 / 检索中 / 正在查找《xx》等 N 份资料 / 正在调用技能。
 *
 * 优先级：progress（后端下发的过程文案）> tool（技能调用）> phase（阶段）。
 * progress 只在还没开始吐正文时显示，正文一开始就收起，避免和内容抢注意力。
 */
import { computed } from 'vue'
import { LoadingOutlined } from '@ant-design/icons-vue'

const props = withDefaults(
  defineProps<{
    phase?: string
    progress?: string
    tool?: string
  }>(),
  { phase: '', progress: '', tool: '' },
)

const PHASE_TEXT: Record<string, string> = {
  thinking: '思考中…',
  searching: '检索知识库中…',
  generating: '正在生成回答…',
}

const text = computed(() => {
  if (props.progress) return props.progress
  if (props.tool) return props.tool
  const phase = props.phase || 'thinking'
  return PHASE_TEXT[phase] ?? '处理中…'
})

/** 生成阶段用「正在生成」但不要和光标同时抢视觉：父组件控制显隐 */
const visible = computed(() => !!text.value)
</script>

<template>
  <Transition name="fade">
    <div v-if="visible" class="status">
      <LoadingOutlined spin class="spin" />
      <span class="text">{{ text }}</span>
    </div>
  </Transition>
</template>

<style scoped lang="less">
.status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  padding: 3px 10px;
  border-radius: 12px;
  background: var(--color-bg-sunken);
  border: 1px solid var(--color-border-light);
  font-size: var(--fs-xs);
  color: var(--color-text-2);
}
.spin {
  color: var(--color-primary);
  font-size: 11px;
}
.text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>