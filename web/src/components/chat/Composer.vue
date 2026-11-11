<script setup lang="ts">
/**
 * 输入框：Enter 发送 / Shift+Enter 换行 / 字数限制 / 发送中禁用。
 *
 * 细节：
 * - 中文输入法组合态（compositionstart/end）下按 Enter 不该发送，否则选词时会被截断；
 * - textarea 高度随内容自适应，最高 200px 后内部滚动；
 * - 流式生成中把「发送」换成「停止」。
 */
import { computed, nextTick, ref, watch } from 'vue'
import { SendOutlined, StopOutlined } from '@ant-design/icons-vue'

const props = withDefaults(
  defineProps<{
    /** 生成中：禁用发送，显示停止按钮 */
    busy?: boolean
    disabled?: boolean
    maxlength?: number
    placeholder?: string
  }>(),
  {
    busy: false,
    disabled: false,
    maxlength: 2000,
    placeholder: '输入问题，Enter 发送，Shift + Enter 换行',
  },
)

const emit = defineEmits<{ send: [text: string]; stop: [] }>()

const text = ref('')
const composing = ref(false)
const textareaRef = ref<HTMLTextAreaElement | null>(null)

const trimmed = computed(() => text.value.trim())
const canSend = computed(
  () => !props.disabled && !props.busy && trimmed.value.length > 0,
)
const length = computed(() => text.value.length)

/** 自适应高度（1 行起，最高 200px） */
function resize() {
  const el = textareaRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 200)}px`
}

watch(text, () => nextTick(resize))

function submit() {
  if (!canSend.value) return
  const value = trimmed.value
  text.value = ''
  nextTick(resize)
  emit('send', value)
}

function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Enter') return
  // 输入法组合态：交给输入法处理
  if (composing.value || e.isComposing) return
  if (e.shiftKey) return // 换行
  if (e.ctrlKey || e.metaKey) return // 允许 Ctrl+Enter 也发送？这里保持与 Enter 一致
  e.preventDefault()
  submit()
}

/** 外部调用（例如空状态里的示例问题） */
function setText(value: string) {
  text.value = value
  nextTick(() => {
    resize()
    textareaRef.value?.focus()
  })
}

defineExpose({ setText, focus: () => textareaRef.value?.focus() })
</script>

<template>
  <div class="composer">
    <div class="box" :class="{ disabled }">
      <textarea
        ref="textareaRef"
        v-model="text"
        class="input"
        rows="1"
        :maxlength="maxlength"
        :placeholder="placeholder"
        :disabled="disabled"
        @keydown="onKeydown"
        @compositionstart="composing = true"
        @compositionend="composing = false"
        @input="resize"
      />

      <div class="bar">
        <span class="hint">
          <span class="count" :class="{ near: length > maxlength * 0.9 }">{{ length }}/{{ maxlength }}</span>
          <span class="tip">Enter 发送 · Shift+Enter 换行</span>
        </span>

        <a-button v-if="busy" danger size="small" @click="emit('stop')">
          <template #icon><StopOutlined /></template>
          停止生成
        </a-button>
        <a-button v-else type="primary" size="small" :disabled="!canSend" @click="submit">
          <template #icon><SendOutlined /></template>
          发送
        </a-button>
      </div>
    </div>
    <p class="disclaimer">回答由模型生成，涉及关键信息请核对来源。</p>
  </div>
</template>

<style scoped lang="less">
.composer {
  padding: var(--space-2) var(--space-4) var(--space-3);
  background: var(--color-bg-card);
  border-top: 1px solid var(--color-border-light);
}

.box {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: 8px 10px 6px;
  background: var(--color-bg-card);
  transition: border-color var(--dur-fast) var(--ease);

  &:focus-within {
    border-color: var(--color-primary);
  }
  &.disabled {
    background: var(--color-bg-sunken);
  }
}

.input {
  display: block;
  width: 100%;
  border: 0;
  outline: 0;
  resize: none;
  background: transparent;
  color: var(--color-text-1);
  font-family: var(--font-family);
  font-size: var(--fs-sm);
  line-height: 1.6;
  max-height: 200px;

  &::placeholder {
    color: var(--color-text-3);
  }
  &:disabled {
    cursor: not-allowed;
  }
}

.bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding-top: 4px;
}

.hint {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: 11px;
  color: var(--color-text-3);
  min-width: 0;
}
.count.near {
  color: var(--color-warning);
}
.tip {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.disclaimer {
  margin: 6px 2px 0;
  font-size: 11px;
  color: var(--color-text-3);
  text-align: center;
}

@media (max-width: 700px) {
  .tip {
    display: none;
  }
}
</style>