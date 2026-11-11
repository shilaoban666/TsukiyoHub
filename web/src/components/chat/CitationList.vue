<script setup lang="ts">
/**
 * 引用来源列表（气泡内折叠区 / 右侧面板共用）。
 *
 * 展示：序号 [n]、标题、相似度、版本、片段。
 * 点击条目触发 `select`，由父组件决定是展开右侧面板还是滚动定位。
 */
import { computed } from 'vue'
import { FileTextOutlined } from '@ant-design/icons-vue'
import { fmtScore } from '@/utils/format'
import type { Citation } from '@/types/api'

const props = withDefaults(
  defineProps<{
    citations: Citation[]
    /** 紧凑模式：气泡内使用，片段只显示两行 */
    compact?: boolean
  }>(),
  { compact: false },
)

const emit = defineEmits<{ select: [citation: Citation] }>()

/** 统一排序：按 index 升序，保证 [1][2][3] 与正文一致 */
const list = computed(() =>
  [...props.citations].sort((a, b) => (a.index ?? 0) - (b.index ?? 0)),
)

function subtitle(c: Citation): string {
  const parts: string[] = []
  if (typeof c.score === 'number') parts.push(`相似度 ${fmtScore(c.score)}`)
  if (c.version) parts.push(c.version)
  if (c.cited === false) parts.push('未直接引用')
  return parts.join(' · ')
}
</script>

<template>
  <div class="citations" :class="{ compact }">
    <button
      v-for="c in list"
      :key="`${c.chunk_id || c.doc_id}-${c.index}`"
      class="citation"
      type="button"
      @click="emit('select', c)"
    >
      <span class="idx">{{ c.index }}</span>
      <div class="body">
        <div class="head">
          <FileTextOutlined class="icon" />
          <span class="title ellipsis" :title="c.title">{{ c.title || '未命名文档' }}</span>
          <span class="meta">{{ subtitle(c) }}</span>
        </div>
        <p v-if="c.snippet" class="snippet">{{ c.snippet }}</p>
      </div>
    </button>
  </div>
</template>

<style scoped lang="less">
.citations {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.citation {
  display: flex;
  gap: var(--space-2);
  align-items: flex-start;
  width: 100%;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-sunken);
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition:
    border-color var(--dur-fast) var(--ease),
    background-color var(--dur-fast) var(--ease);

  &:hover {
    border-color: var(--color-primary);
    background: var(--color-bg-hover);
  }
}

.idx {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 4px;
  border-radius: 4px;
  background: var(--color-primary-bg);
  color: var(--color-primary);
  font-size: 11px;
  font-weight: 600;
}

.body {
  min-width: 0;
  flex: 1;
}

.head {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.icon {
  color: var(--color-text-3);
  flex: 0 0 auto;
}
.title {
  font-size: var(--fs-sm);
  color: var(--color-text-1);
  font-weight: 500;
  flex: 0 1 auto;
  min-width: 0;
}
.meta {
  flex: 0 0 auto;
  font-size: var(--fs-xs);
  color: var(--color-text-3);
  margin-left: auto;
}

.snippet {
  margin: 4px 0 0;
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--color-text-2);
  white-space: pre-wrap;
  word-break: break-word;
}

// 紧凑模式：片段最多两行
.compact .snippet {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>