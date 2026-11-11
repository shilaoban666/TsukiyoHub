<script setup lang="ts">
/**
 * 右侧「来源」面板：展示当前选中回答的全部引用，含片段、分数、版本、更新时间。
 *
 * 为什么常驻分栏而不是抽屉：聊天时「看答案 + 核对来源」是高频连续动作，
 * 抽屉每次开合都会打断节奏。窄屏（<1280）下自动变成浮层，由 ChatView 控制显隐。
 */
import { computed } from 'vue'
import { CloseOutlined, FileTextOutlined, LinkOutlined } from '@ant-design/icons-vue'
import { fmtDateTime, fmtScore } from '@/utils/format'
import type { Citation } from '@/types/api'

const props = defineProps<{
  citations: Citation[]
  open: boolean
}>()

const emit = defineEmits<{ close: [] }>()

const list = computed(() =>
  [...props.citations].sort((a, b) => (a.index ?? 0) - (b.index ?? 0)),
)

const count = computed(() => list.value.length)
</script>

<template>
  <aside class="source-panel" :class="{ open }">
    <div class="head">
      <span class="title">来源</span>
      <span class="count">{{ count }} 条</span>
      <a-button class="close" type="text" size="small" @click="emit('close')">
        <template #icon><CloseOutlined /></template>
      </a-button>
    </div>

    <div class="scroll">
      <div v-if="!count" class="empty">
        <FileTextOutlined class="icon" />
        <p>点击回答下方的「参考来源」查看文档片段</p>
      </div>

      <div v-for="c in list" :key="`${c.chunk_id || c.doc_id}-${c.index}`" class="card">
        <div class="card-head">
          <span class="idx">{{ c.index }}</span>
          <span class="doc-title" :title="c.title">{{ c.title || '未命名文档' }}</span>
        </div>

        <div class="meta">
          <span>相似度 {{ fmtScore(c.score) }}</span>
          <span v-if="c.version">{{ c.version }}</span>
          <span v-if="c.updated_at">{{ fmtDateTime(c.updated_at) }}</span>
          <span v-if="c.cited === false" class="warn">未直接引用</span>
        </div>

        <p class="snippet">{{ c.snippet || '（无片段内容）' }}</p>

        <div class="foot">
          <a v-if="c.url" :href="c.url" target="_blank" rel="noopener">
            <LinkOutlined /> 打开原文
          </a>
          <span v-else-if="c.uri" class="uri" :title="c.uri">{{ c.uri }}</span>
          <span v-if="c.chunk_id" class="chunk mono">{{ c.chunk_id }}</span>
        </div>
      </div>
    </div>
  </aside>
</template>

<style scoped lang="less">
.source-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  width: var(--source-col);
  flex: 0 0 var(--source-col);
  background: var(--color-bg-card);
  border-left: 1px solid var(--color-border-light);
}

.head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3);
  border-bottom: 1px solid var(--color-border-light);
}
.title {
  font-size: var(--fs-sm);
  font-weight: 600;
}
.count {
  font-size: var(--fs-xs);
  color: var(--color-text-3);
}
.close {
  margin-left: auto;
}

.scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.empty {
  padding: 40px 12px;
  text-align: center;
  color: var(--color-text-3);
  font-size: var(--fs-xs);

  .icon {
    font-size: 22px;
    margin-bottom: var(--space-2);
  }
  p {
    margin: 0;
    line-height: 1.6;
  }
}

.card {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-3);
  background: var(--color-bg-sunken);
}

.card-head {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
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
.doc-title {
  font-size: var(--fs-sm);
  font-weight: 500;
  color: var(--color-text-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-top: 6px;
  font-size: 11px;
  color: var(--color-text-3);

  .warn {
    color: var(--color-warning);
  }
}

.snippet {
  margin: var(--space-2) 0 0;
  font-size: var(--fs-xs);
  line-height: 1.7;
  color: var(--color-text-2);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 260px;
  overflow-y: auto;
}

.foot {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-2);
  font-size: 11px;
  color: var(--color-text-3);

  a {
    display: inline-flex;
    align-items: center;
    gap: 3px;
  }
}
.uri,
.chunk {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}

// 窄屏：面板变成覆盖式浮层
@media (max-width: 1279px) {
  .source-panel {
    position: absolute;
    top: 0;
    right: 0;
    bottom: 0;
    z-index: 20;
    width: min(380px, 92vw);
    flex: none;
    box-shadow: var(--shadow-float);
  }
}
</style>