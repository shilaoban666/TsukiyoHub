<script setup lang="ts">
/**
 * 消息流容器：自动贴底 + 上滑暂停 + 「回到底部」。
 *
 * 滚动策略（旧前端验证过的方案）：
 * - 用户停留在底部附近（<60px）时，新内容到达自动滚到底；
 * - 用户主动上滑后暂停自动滚动，右下角出现「回到底部」，避免「想往上翻却被拽回来」。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { DownOutlined } from '@ant-design/icons-vue'
import ChatBubble from './ChatBubble.vue'
import type { Citation, UiMessage } from '@/types/api'

const props = defineProps<{
  messages: UiMessage[]
  /** 正在流式输出的消息 id（用于判断光标/禁用重新生成） */
  streamingId?: string | null
}>()

const emit = defineEmits<{
  regenerate: [id: string]
  openSources: [id: string, citations: Citation[]]
}>()

const scroller = ref<HTMLElement | null>(null)
const sticky = ref(true)

/** 最后一条消息的内容长度：流式时随 delta 增长，用来触发贴底 */
const tailLength = computed(() => {
  const last = props.messages[props.messages.length - 1]
  return last ? last.content.length : 0
})

function nearBottom(): boolean {
  const el = scroller.value
  if (!el) return true
  return el.scrollHeight - el.scrollTop - el.clientHeight < 60
}

function onScroll() {
  sticky.value = nearBottom()
}

function scrollToBottom(behavior: ScrollBehavior = 'auto') {
  const el = scroller.value
  if (!el) return
  el.scrollTo({ top: el.scrollHeight, behavior })
}

watch(
  () => [props.messages.length, tailLength.value],
  async () => {
    if (!sticky.value) return
    await nextTick()
    scrollToBottom()
  },
)

// 首次挂载 / 切换会话后定位到底部
watch(
  () => props.messages,
  async () => {
    sticky.value = true
    await nextTick()
    scrollToBottom()
  },
)

onMounted(() => {
  scrollToBottom()
})

defineExpose({ scrollToBottom })
</script>

<template>
  <div class="messages">
    <div ref="scroller" class="scroll" @scroll.passive="onScroll">
      <div class="inner">
        <!-- v-memo：只在这条消息的 id/长度/状态变化时重渲染，流式期间不会拖动整列 diff -->
        <template v-for="m in messages" :key="m.id">
          <ChatBubble
            v-memo="[m.id, m.content.length, m.status, m.citations.length]"
            :message="m"
            :streaming="m.id === streamingId"
            @regenerate="emit('regenerate', $event)"
            @open-sources="(id, cites) => emit('openSources', id, cites)"
          />
        </template>
        <div class="bottom-anchor" />
      </div>
    </div>

    <Transition name="fade">
      <button v-if="!sticky" class="to-bottom" type="button" title="回到底部" @click="scrollToBottom('smooth')">
        <DownOutlined />
      </button>
    </Transition>
  </div>
</template>

<style scoped lang="less">
.messages {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
}

.scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  scroll-behavior: auto;
}

.inner {
  max-width: 860px;
  margin: 0 auto;
  padding: var(--space-4) var(--space-6) var(--space-2);
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.bottom-anchor {
  height: 8px;
}

.to-bottom {
  position: absolute;
  right: 20px;
  bottom: 16px;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  border: 1px solid var(--color-border);
  background: var(--color-bg-card);
  color: var(--color-text-2);
  cursor: pointer;
  box-shadow: var(--shadow-float);

  &:hover {
    color: var(--color-primary);
    border-color: var(--color-primary);
  }
}

@media (max-width: 700px) {
  .inner {
    padding: var(--space-3);
  }
}
</style>