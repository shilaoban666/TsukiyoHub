<script setup lang="ts">
/**
 * 路由编辑对话框：别名 / 策略 / 目标（可增删）/ 降级链 / 能力要求 / options JSON。
 *
 * 前端做**最小校验**（别名、至少一个目标、options 必须是 JSON 对象），
 * 真正的语义校验由网关 `Reload` 负责；保存失败时对话框保持打开，不丢用户输入。
 */
import { computed, reactive, ref, watch } from 'vue'
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons-vue'
import { parseList, STRATEGY_OPTIONS } from './meta'
import type { RouteConfig, RouteTarget } from '@/types/gateway'

const props = defineProps<{
  open: boolean
  /** null = 新增 */
  route: RouteConfig | null
  saving: boolean
  modelOptions: string[]
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  save: [route: RouteConfig]
}>()

interface TargetRow {
  model: string
  weight: number | null
  priority: number | null
}

const formState = reactive({
  alias: '',
  strategy: 'priority',
  enabled: true,
  fallbacks: '',
  required: '',
  optionsText: '{}',
})

const targets = ref<TargetRow[]>([])
const formError = ref('')
const optionsError = ref('')

const modelHint = computed(() => {
  const list = props.modelOptions.slice(0, 12).join('、')
  return props.modelOptions.length > 12 ? `${list} …` : list
})

function reset() {
  const r = props.route
  formState.alias = r?.alias || ''
  formState.strategy = r?.strategy || 'priority'
  formState.enabled = r?.enabled ?? true
  formState.fallbacks = (r?.fallbacks || []).join(', ')
  formState.required = (r?.required_capabilities || []).join(', ')
  formState.optionsText = JSON.stringify(r?.options || {}, null, 2)
  targets.value = r?.targets?.length
    ? r.targets.map((t: RouteTarget) => ({
        model: t.model,
        weight: t.weight ?? 1,
        priority: t.priority ?? 1,
      }))
    : [{ model: '', weight: 1, priority: 1 }]
  formError.value = ''
  optionsError.value = ''
}

watch(
  () => [props.open, props.route] as const,
  () => {
    if (props.open) reset()
  },
)

function addTarget() {
  targets.value.push({
    model: '',
    weight: 1,
    priority: targets.value.length + 1,
  })
}

function removeTarget(index: number) {
  if (targets.value.length <= 1) return
  targets.value.splice(index, 1)
}

function close() {
  emit('update:open', false)
}

function submit() {
  formError.value = ''
  optionsError.value = ''

  const alias = formState.alias.trim()
  if (!alias) {
    formError.value = '路由别名（alias）不能为空'
    return
  }

  const validTargets: RouteTarget[] = targets.value
    .map((t) => ({
      model: t.model.trim(),
      weight: Number(t.weight) || 1,
      priority: Number(t.priority) || 1,
    }))
    .filter((t) => t.model)
  if (!validTargets.length) {
    formError.value = '至少需要一个目标模型'
    return
  }

  let options: Record<string, unknown>
  try {
    const parsed: unknown = JSON.parse(formState.optionsText.trim() || '{}')
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('必须是 JSON 对象（不能是数组或标量）')
    }
    options = parsed as Record<string, unknown>
  } catch (e) {
    optionsError.value = `options 不是合法 JSON 对象：${(e as Error).message}`
    return
  }

  emit('save', {
    alias,
    strategy: formState.strategy,
    options,
    targets: validTargets,
    fallbacks: parseList(formState.fallbacks),
    required_capabilities: parseList(formState.required),
    enabled: formState.enabled,
  })
}
</script>

<template>
  <a-modal
    :open="open"
    :title="route ? '编辑路由' : '新增路由'"
    width="680px"
    ok-text="保存"
    cancel-text="取消"
    :confirm-loading="saving"
    :mask-closable="false"
    @ok="submit"
    @cancel="close"
  >
    <a-form layout="vertical" class="route-form">
      <a-form-item label="路由别名（alias）" required>
        <a-input v-model:value="formState.alias" placeholder="客户端请求的 model 字段，如 fast" />
      </a-form-item>

      <div class="form-row">
        <a-form-item label="选路策略" class="grow">
          <a-select v-model:value="formState.strategy" :options="STRATEGY_OPTIONS" />
        </a-form-item>
        <a-form-item label="启用">
          <a-switch v-model:checked="formState.enabled" />
        </a-form-item>
      </div>

      <a-form-item label="目标模型（按 priority 升序尝试）">
        <div class="targets">
          <div v-for="(t, i) in targets" :key="i" class="target-row">
            <a-input v-model:value="t.model" class="grow" placeholder="模型 ID，如 gpt-4o-mini" />
            <a-input-number
              v-model:value="t.weight"
              class="num"
              :min="1"
              :max="100"
              placeholder="权重"
            />
            <a-input-number
              v-model:value="t.priority"
              class="num"
              :min="1"
              :max="99"
              placeholder="优先级"
            />
            <a-button type="text" danger :disabled="targets.length <= 1" @click="removeTarget(i)">
              <template #icon><DeleteOutlined /></template>
            </a-button>
          </div>
          <a-button type="dashed" size="small" @click="addTarget">
            <template #icon><PlusOutlined /></template>
            添加目标
          </a-button>
        </div>
        <div v-if="modelHint" class="hint">可选模型：{{ modelHint }}</div>
      </a-form-item>

      <div class="form-row">
        <a-form-item label="降级链（fallbacks，逗号分隔）" class="grow">
          <a-input v-model:value="formState.fallbacks" placeholder="如 gpt-4o-mini, qwen-plus" />
        </a-form-item>
        <a-form-item label="能力要求（逗号分隔）" class="grow">
          <a-input v-model:value="formState.required" placeholder="如 tools, vision, context:32000" />
        </a-form-item>
      </div>

      <a-form-item label="策略参数（options JSON）">
        <a-textarea
          v-model:value="formState.optionsText"
          class="mono"
          :rows="5"
          spellcheck="false"
        />
        <div v-if="optionsError" class="err">{{ optionsError }}</div>
        <div v-else class="hint">
          必须是 JSON 对象，例如 canary 策略用 {"percent": 10}；留空即 {}。
        </div>
      </a-form-item>

      <a-alert v-if="formError" type="error" show-icon :message="formError" class="form-alert" />
    </a-form>
  </a-modal>
</template>

<style scoped lang="less">
.form-row {
  display: flex;
  gap: var(--space-4);
}

.grow {
  flex: 1;
  min-width: 0;
}

.num {
  width: 92px;
  flex: 0 0 92px;
}

.targets {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  align-items: flex-start;
}

.target-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
}

.hint {
  margin-top: 4px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--color-text-3);
  word-break: break-word;
}

.err {
  margin-top: 4px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--color-error);
}

.form-alert {
  margin-top: var(--space-2);
}
</style>