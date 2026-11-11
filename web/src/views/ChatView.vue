<script setup lang="ts">
/**
 * 聊天页（核心）：三栏工作台。
 *
 * 布局：`会话列表 248px | 消息区 1fr | 来源面板 340px`
 * - ≥1280px 时三栏常驻；窄屏时来源面板变成覆盖式浮层，会话列表可折叠。
 *
 * 数据流：
 * - 会话列表由 store 从 `/api/sessions` 拉取；
 * - 点会话加载历史（`/api/sessions/{id}/messages`）；
 * - 发送走 `/api/chat/stream`（SSE，delta 是累积全文）；
 * - 流式结束后右侧面板可查看引用来源。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import {
  BookOutlined,
  BulbOutlined,
  FileSearchOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons-vue'
import SessionList from '@/components/chat/SessionList.vue'
import MessageList from '@/components/chat/MessageList.vue'
import Composer from '@/components/chat/Composer.vue'
import StreamStatus from '@/components/chat/StreamStatus.vue'
import SourcePanel from '@/components/chat/SourcePanel.vue'
import { useChatStore } from '@/stores/chat'
import type { Citation } from '@/types/api'

const chat = useChatStore()
const route = useRoute()
const router = useRouter()

/** 窄屏下会话列表是否收起（宽屏始终展开） */
const sessionsCollapsed = ref(false)

const messages = computed(() => chat.activeMessages)
const title = computed(() => chat.activeSession?.title || '新对话')
const isEmpty = computed(() => messages.value.length === 0)
const streamingId = computed(() =>
  chat.streaming.running ? chat.streaming.messageId : null,
)
const hasActiveCitations = computed(
  () => chat.panelCitations.length > 0 || chat.panelOpen,
)

/** 空状态引导：3 个示例问题（点击直接发送） */
const EXAMPLES = [
  { icon: BookOutlined, text: '知识库里目前收录了哪些资料？' },
  { icon: ThunderboltOutlined, text: '你有哪些可以调用的技能？' },
  { icon: BulbOutlined, text: '帮我用三句话说明你能做什么' },
]

/** 路由 ↔ store 双向同步（避免互相触发死循环，用值比较挡一层） */
watch(
  () => chat.activeId,
  (id) => {
    const routeId = (route.params.sessionId as string) || ''
    if (id && routeId !== id) void router.replace(`/chat/${id}`)
  },
)

watch(
  () => route.params.sessionId,
  async (id) => {
    const next = (id as string) || ''
    if (!next || next === chat.activeId) return
    if (chat.sessions.some((s) => s.id === next)) await chat.selectSession(next)
  },
)

/** 流式失败时给一次全局提示（store 里只记录状态，不弹 toast） */
watch(
  () => {
    const last = messages.value[messages.value.length - 1]
    return last ? `${last.id}:${last.status}` : ''
  },
  (key) => {
    if (!key) return
    const last = messages.value[messages.value.length - 1]
    if (last?.status === 'error' && last.error) message.error(last.error)
  },
)

onMounted(async () => {
  try {
    await chat.loadSessions()
  } catch {
    /* 错误已由请求层 toast，这里保持页面可用（顶部还有离线横幅） */
  }
  const routeId = (route.params.sessionId as string) || ''
  if (routeId && chat.sessions.some((s) => s.id === routeId)) {
    await chat.selectSession(routeId)
  } else if (chat.sessions.length) {
    await chat.selectSession(chat.sessions[0].id)
  }
  // 没有会话时不自动创建：等用户发第一条消息后再建，避免刷新页面就产生空会话
})

async function onSend(text: string) {
  await chat.send(text)
}

function onStop() {
  chat.stop()
}

function onRegenerate(id: string) {
  void chat.regenerate(id)
}

function openSources(id: string, citations: Citation[]) {
  chat.openCitationPanel(id, citations)
}

function useExample(text: string) {
  if (chat.busy) return
  void chat.send(text)
}

function toggleSources() {
  if (chat.panelOpen) {
    chat.closeCitationPanel()
    return
  }
  // 没有选中某条消息时，默认展示最后一条带引用的回答
  const last = [...messages.value].reverse().find((m) => m.citations.length)
  if (last) chat.openCitationPanel(last.id, last.citations)
  else message.info('当前会话还没有引用来源：先提问，回答命中知识库后会显示来源')
}
</script>

<template>
  <div class="chat-view">
    <!-- 左：会话列表 -->
    <SessionList v-show="!sessionsCollapsed" class="col-sessions" />

    <!-- 中：消息 + 输入 -->
    <section class="pane">
      <header class="pane-head">
        <button
          class="icon-btn"
          type="button"
          :title="sessionsCollapsed ? '展开会话列表' : '收起会话列表'"
          @click="sessionsCollapsed = !sessionsCollapsed"
        >
          <MenuUnfoldOutlined v-if="sessionsCollapsed" />
          <MenuFoldOutlined v-else />
        </button>

        <div class="heading">
          <h2 class="pane-title ellipsis">{{ title }}</h2>
          <span v-if="chat.activeSession" class="pane-sub">
            {{ chat.activeSession.message_count || messages.length }} 条消息
          </span>
        </div>

        <a-tooltip title="查看引用来源">
          <a-button size="small" :type="chat.panelOpen ? 'primary' : 'default'" @click="toggleSources">
            <template #icon><FileSearchOutlined /></template>
            来源
            <template v-if="chat.panelCitations.length">({{ chat.panelCitations.length }})</template>
          </a-button>
        </a-tooltip>
      </header>

      <!-- 空状态：3 个示例问题 -->
      <div v-if="isEmpty" class="empty">
        <div class="empty-inner">
          <div class="empty-logo">月</div>
          <h1 class="empty-title">今天想了解点什么？</h1>
          <p class="empty-desc">
            提问后会自动检索知识库并调用已启用的技能；回答下方会列出参考来源。
          </p>
          <div class="examples">
            <button
              v-for="ex in EXAMPLES"
              :key="ex.text"
              class="example"
              type="button"
              :disabled="chat.busy"
              @click="useExample(ex.text)"
            >
              <component :is="ex.icon" class="example-icon" />
              <span>{{ ex.text }}</span>
            </button>
          </div>
          <p v-if="chat.messagesError" class="empty-error">{{ chat.messagesError }}</p>
        </div>
      </div>

      <!-- 消息区 -->
      <MessageList
        v-else
        :messages="messages"
        :streaming-id="streamingId"
        @regenerate="onRegenerate"
        @open-sources="openSources"
      />

      <!-- 状态条 + 输入框 -->
      <div class="footer">
        <div v-if="chat.streaming.running" class="status-row">
          <StreamStatus
            :phase="chat.streaming.phase"
            :progress="chat.streaming.progress"
            :tool="chat.streaming.tool"
          />
        </div>

        <Composer :busy="chat.busy" @send="onSend" @stop="onStop" />
      </div>
    </section>

    <!-- 右：来源面板 -->
    <SourcePanel
      v-if="hasActiveCitations"
      :citations="chat.panelCitations"
      :open="chat.panelOpen"
      @close="chat.closeCitationPanel()"
    />
  </div>
</template>

<style scoped lang="less">
.chat-view {
  position: relative;
  display: flex;
  flex: 1;
  min-height: 0;
  overflow: hidden;
  background: var(--color-bg-page);
}

.col-sessions {
  flex: 0 0 var(--session-col);
}

.pane {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--color-bg-page);
}

.pane-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  height: 46px;
  flex: 0 0 46px;
  padding: 0 var(--space-4);
  background: var(--color-bg-page);
  border-bottom: 1px solid var(--color-border-light);
}

.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-2);
  cursor: pointer;

  &:hover {
    background: var(--color-bg-hover);
    color: var(--color-text-1);
  }
}

.heading {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  min-width: 0;
  flex: 1;
}
.pane-title {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--color-text-1);
  max-width: 60%;
}
.pane-sub {
  font-size: var(--fs-xs);
  color: var(--color-text-3);
  white-space: nowrap;
}

.footer {
  flex: 0 0 auto;
  background: var(--color-bg-card);
  border-top: 1px solid var(--color-border-light);
}
.status-row {
  padding: var(--space-2) var(--space-4) 0;
}

// ---------------- 空状态 ----------------
.empty {
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-6);
  overflow-y: auto;
}
.empty-inner {
  max-width: 640px;
  text-align: center;
}
.empty-logo {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  margin-bottom: var(--space-3);
  border-radius: 14px;
  background: var(--color-primary-bg);
  color: var(--color-primary);
  font-size: 18px;
  font-weight: 600;
}
.empty-title {
  margin: 0 0 var(--space-2);
  font-size: var(--fs-lg);
  font-weight: 600;
  color: var(--color-text-1);
}
.empty-desc {
  margin: 0 0 var(--space-6);
  font-size: var(--fs-sm);
  color: var(--color-text-2);
  line-height: 1.7;
}
.examples {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  text-align: left;
}
.example {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  width: 100%;
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-card);
  color: var(--color-text-1);
  font-size: var(--fs-sm);
  text-align: left;
  cursor: pointer;
  transition:
    border-color var(--dur-fast) var(--ease),
    background-color var(--dur-fast) var(--ease);

  &:hover:not(:disabled) {
    border-color: var(--color-primary);
    background: var(--color-bg-hover);
  }
  &:disabled {
    cursor: not-allowed;
    opacity: 0.6;
  }
}
.example-icon {
  color: var(--color-primary);
  flex: 0 0 auto;
}
.empty-error {
  margin-top: var(--space-4);
  font-size: var(--fs-xs);
  color: var(--color-error);
}

// 窄屏：会话列表变覆盖层，来源面板浮层（SourcePanel 内部处理）
@media (max-width: 900px) {
  .col-sessions {
    position: absolute;
    top: 46px;
    bottom: 0;
    left: 0;
    z-index: 30;
    width: min(280px, 86vw);
    flex: none;
    box-shadow: var(--shadow-float);
  }
  .pane-title {
    max-width: 45%;
  }
}
</style>