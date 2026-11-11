<script setup lang="ts">
/**
 * 一条消息气泡。
 *
 * 性能关键（旧前端踩过的坑）：
 * - `html` 用 computed 缓存：只有 content 变化才重新跑 marked/DOMPurify；
 * - 流式中的消息走 `renderStreaming`（稳定前缀 Markdown + 尾部纯文本），避免半截
 *   ``` 或 ** 导致整段跳变；流结束后（status !== 'streaming'）再一次性全量渲染；
 * - 父级用 `v-memo` 限制重渲染范围。
 */
import { computed, ref } from 'vue'
import { message as toast } from 'ant-design-vue'
import {
  CopyOutlined,
  RedoOutlined,
  RobotOutlined,
  UserOutlined,
  DownOutlined,
  RightOutlined,
  WarningOutlined,
} from '@ant-design/icons-vue'
import CitationList from './CitationList.vue'
import { renderMarkdown, renderStreaming } from '@/utils/markdown'
import { fmtLatency, fmtRelative } from '@/utils/format'
import type { Citation, UiMessage } from '@/types/api'

const props = defineProps<{
  message: UiMessage
  /** 正在流式输出的这一条（父级据此决定是否显示光标 / 停止按钮） */
  streaming?: boolean
}>()

const emit = defineEmits<{
  regenerate: [id: string]
  openSources: [id: string, citations: Citation[]]
}>()

const openRefs = ref(false)

const isUser = computed(() => props.message.role === 'user')
const isStreaming = computed(() => props.streaming || props.message.status === 'streaming')
const hasContent = computed(() => !!props.message.content.trim())

/** 渲染用的 HTML（流式与完成后走不同策略） */
const html = computed(() => {
  const raw = props.message.content
  if (!raw) return ''
  return isStreaming.value ? renderStreaming(raw) : renderMarkdown(raw)
})

const citations = computed<Citation[]>(() => props.message.citations ?? [])
const showAnswerMeta = computed(() => !isUser.value && !isStreaming.value)

async function copy() {
  const text = props.message.content
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    toast.success('已复制')
  } catch {
    // 非 https / 权限被拒时的兜底
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    ta.remove()
    toast.success('已复制')
  }
}

function openPanel() {
  emit('openSources', props.message.id, citations.value)
}

const statusText = computed(() => {
  const m = props.message
  if (m.status === 'aborted') return '已停止生成'
  if (m.status === 'error') return m.error || '生成失败'
  return ''
})
</script>

<template>
  <div class="row" :class="isUser ? 'user' : 'bot'">
    <span class="avatar" :class="isUser ? 'me' : 'bot-avatar'">
      <UserOutlined v-if="isUser" />
      <RobotOutlined v-else />
    </span>

    <div class="col">
      <!-- 正文 -->
      <div class="bubble">
        <div v-if="hasContent" class="md" v-html="html" />
        <!-- 流式但还没有正文：三个点的等待态 -->
        <div v-else-if="isStreaming" class="typing" aria-label="正在思考">
          <span /><span /><span />
        </div>
        <div v-else class="empty-reply">（空回答）</div>
        <span v-if="isStreaming && hasContent" class="cursor" />
      </div>

      <!-- 中止 / 失败提示 -->
      <div v-if="statusText" class="status-line" :class="message.status">
        <WarningOutlined v-if="message.status === 'error'" />
        <span>{{ statusText }}</span>
      </div>

      <!-- 助手消息的引用折叠区 -->
      <div v-if="!isUser && citations.length" class="refs">
        <button class="refs-toggle" type="button" @click="openRefs = !openRefs">
          <DownOutlined v-if="openRefs" />
          <RightOutlined v-else />
          <span>参考来源 {{ citations.length }} 条</span>
        </button>
        <button class="refs-panel-link" type="button" @click="openPanel">在右侧查看</button>
        <Transition name="slide-up">
          <div v-if="openRefs" class="refs-box">
            <CitationList :citations="citations" compact @select="openPanel" />
          </div>
        </Transition>
      </div>

      <!-- 操作栏：复制 / 重新生成 / 时间与耗时 -->
      <div class="actions">
        <span class="time">{{ fmtRelative(message.created_at) }}</span>

        <template v-if="!isUser">
          <a v-if="showAnswerMeta" class="act" title="重新生成" @click="emit('regenerate', message.id)">
            <RedoOutlined /> 重新生成
          </a>
          <a v-if="message.latency_ms" class="act muted">{{ fmtLatency(message.latency_ms) }}</a>
          <span v-if="message.grounded === false" class="act muted">未命中知识库</span>
          <span v-if="message.degraded" class="act muted">降级回答</span>
        </template>

        <a v-if="hasContent && !isStreaming" class="act" title="复制" @click="copy">
          <CopyOutlined /> 复制
        </a>
      </div>
    </div>
  </div>
</template>

<style scoped lang="less">
.row {
  display: flex;
  gap: var(--space-2);
  align-items: flex-start;
  padding: 6px 0;

  &.user {
    flex-direction: row-reverse;

    .bubble {
      background: var(--color-bubble-self);
      color: var(--color-bubble-self-text);
      border-color: transparent;
      border-radius: 12px 2px 12px 12px;

      :deep(a) {
        color: inherit;
        text-decoration: underline;
      }
      :deep(code) {
        background: rgba(255, 255, 255, 0.18);
      }
    }
    .col {
      align-items: flex-end;
    }
    .actions {
      justify-content: flex-end;
    }
  }
}

.avatar {
  flex: 0 0 auto;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;

  &.me {
    background: var(--color-bg-hover);
    color: var(--color-text-2);
  }
  &.bot-avatar {
    background: var(--color-primary-bg);
    color: var(--color-primary);
  }
}

.col {
  display: flex;
  flex-direction: column;
  min-width: 0;
  max-width: 78%;
}

.bubble {
  position: relative;
  padding: 9px 13px;
  border: 1px solid var(--color-border-light);
  border-radius: 2px 12px 12px 12px;
  background: var(--color-bubble-bot);
  color: var(--color-text-1);
  box-shadow: var(--shadow-card);
  font-size: var(--fs-sm);
}

.empty-reply {
  color: var(--color-text-3);
  font-size: var(--fs-xs);
}

// 打字光标
.cursor {
  display: inline-block;
  width: 2px;
  height: 14px;
  margin-left: 2px;
  vertical-align: -2px;
  background: var(--color-primary);
  animation: blink 1s steps(2, start) infinite;
}
@keyframes blink {
  to {
    visibility: hidden;
  }
}

// 等待中的三个点
.typing {
  display: inline-flex;
  gap: 4px;
  padding: 3px 0;

  span {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--color-text-3);
    animation: bounce 1.2s var(--ease) infinite;

    &:nth-child(2) {
      animation-delay: 0.15s;
    }
    &:nth-child(3) {
      animation-delay: 0.3s;
    }
  }
}
@keyframes bounce {
  0%,
  60%,
  100% {
    opacity: 0.35;
    transform: translateY(0);
  }
  30% {
    opacity: 1;
    transform: translateY(-3px);
  }
}

.status-line {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-top: 4px;
  font-size: var(--fs-xs);
  color: var(--color-text-3);

  &.error {
    color: var(--color-error);
  }
}

.refs {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin-top: 6px;
}

.refs-toggle,
.refs-panel-link,
.act {
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
.refs-panel-link {
  color: var(--color-primary);
}

.refs-box {
  flex: 1 0 100%;
  margin-top: 2px;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
  margin-top: 4px;
  min-height: 18px;
  font-size: var(--fs-xs);
  color: var(--color-text-3);
}

.time {
  color: var(--color-text-3);
}
.muted {
  color: var(--color-text-3);
}

.rate {
  display: none;
}
</style>