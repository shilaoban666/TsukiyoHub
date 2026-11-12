<script setup lang="ts">
/**
 * 告警规则编辑对话框：指标（14 个内置）/ 作用域 / 运算符 + 阈值 / 窗口 / 持续 / 级别。
 *
 * 前端只做必填与数值校验；规则语义（指标名、窗口格式）由网关校验，
 * 保存失败会保留对话框内容，方便用户改完重试。
 */
import { computed, reactive, ref, watch } from 'vue'
import {
  COOLDOWN_OPTIONS,
  FOR_OPTIONS,
  METRIC_OPTIONS,
  OP_OPTIONS,
  SCOPE_OPTIONS,
  SEVERITY_OPTIONS,
  WINDOW_OPTIONS,
} from './meta'
import type { AlertRule, AlertSeverity } from '@/types/gateway'

const props = defineProps<{
  open: boolean
  /** null = 新增 */
  rule: AlertRule | null
  saving: boolean
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  save: [rule: AlertRule]
}>()

/** 指标下拉把口径提示拼进 label，省得用户去翻文档 */
const metricOptions = computed(() =>
  METRIC_OPTIONS.map((m) => ({ value: m.value, label: `${m.label} · ${m.hint}` })),
)

const formState = reactive({
  name: '',
  metric: 'error_rate',
  scopeKey: '',
  scopeValue: '',
  op: '>',
  threshold: 0 as number | null,
  window: '1m',
  forDur: '1m',
  severity: 'warning' as AlertSeverity,
  message: '',
  cooldown: '5m',
  enabled: true,
})

const formError = ref('')

function reset() {
  const r = props.rule
  const scope = r?.scope || {}
  const scopeKey = Object.keys(scope)[0] || ''
  formState.name = r?.name || ''
  formState.metric = r?.metric || 'error_rate'
  formState.scopeKey = scopeKey
  formState.scopeValue = scopeKey ? String(scope[scopeKey] ?? '') : ''
  formState.op = r?.op || '>'
  formState.threshold = r?.threshold ?? 0
  formState.window = r?.window || '1m'
  formState.forDur = r?.for || ''
  formState.severity = r?.severity || 'warning'
  formState.message = r?.message || ''
  formState.cooldown = r?.cooldown || '5m'
  formState.enabled = r?.enabled ?? true
  formError.value = ''
}

watch(
  () => [props.open, props.rule] as const,
  () => {
    if (props.open) reset()
  },
)

function close() {
  emit('update:open', false)
}

function submit() {
  formError.value = ''
  const name = formState.name.trim()
  if (!name) {
    formError.value = '规则名称不能为空'
    return
  }
  if (formState.threshold == null || !Number.isFinite(formState.threshold)) {
    formError.value = '阈值必须是数字'
    return
  }

  const scope: Record<string, string> = {}
  const value = formState.scopeValue.trim()
  if (formState.scopeKey && value) scope[formState.scopeKey] = value

  emit('save', {
    id: props.rule?.id || `web_${Date.now().toString(36)}`,
    name,
    metric: formState.metric,
    scope,
    op: formState.op,
    threshold: formState.threshold,
    window: formState.window,
    for: formState.forDur,
    severity: formState.severity,
    message: formState.message.trim(),
    cooldown: formState.cooldown,
    enabled: formState.enabled,
  })
}
</script>

<template>
  <a-modal
    :open="open"
    :title="rule ? '编辑告警规则' : '新增告警规则'"
    width="680px"
    ok-text="保存"
    cancel-text="取消"
    :confirm-loading="saving"
    :mask-closable="false"
    @ok="submit"
    @cancel="close"
  >
    <a-form layout="vertical" class="rule-form">
      <div class="form-row">
        <a-form-item label="规则名称" required class="grow">
          <a-input v-model:value="formState.name" placeholder="如 错误率过高" />
        </a-form-item>
        <a-form-item label="级别" class="severity">
          <a-select v-model:value="formState.severity" :options="SEVERITY_OPTIONS" />
        </a-form-item>
      </div>

      <a-form-item label="指标">
        <a-select v-model:value="formState.metric" :options="metricOptions" show-search />
      </a-form-item>

      <div class="form-row">
        <a-form-item label="作用域维度" class="scope-key">
          <a-select v-model:value="formState.scopeKey" :options="SCOPE_OPTIONS" />
        </a-form-item>
        <a-form-item label="作用域关键词" class="grow">
          <a-input
            v-model:value="formState.scopeValue"
            :disabled="!formState.scopeKey"
            :placeholder="
              formState.scopeKey
                ? `如 ${formState.scopeKey === 'route' ? 'fast' : formState.scopeKey === 'model' ? 'gpt-4o-mini' : 'openai'}`
                : '留空 = 全局'
            "
          />
        </a-form-item>
      </div>

      <div class="form-row">
        <a-form-item label="运算符" class="op">
          <a-select v-model:value="formState.op" :options="OP_OPTIONS" />
        </a-form-item>
        <a-form-item label="阈值" class="grow">
          <a-input-number v-model:value="formState.threshold" class="full" :step="0.1" />
        </a-form-item>
        <a-form-item label="统计窗口" class="grow">
          <a-select v-model:value="formState.window" :options="WINDOW_OPTIONS" />
        </a-form-item>
        <a-form-item label="持续（for）" class="grow">
          <a-select v-model:value="formState.forDur" :options="FOR_OPTIONS" />
        </a-form-item>
      </div>

      <div class="form-row">
        <a-form-item label="通知冷却（cooldown）" class="grow">
          <a-select v-model:value="formState.cooldown" :options="COOLDOWN_OPTIONS" />
        </a-form-item>
        <a-form-item label="启用" class="switch-item">
          <a-switch v-model:checked="formState.enabled" />
        </a-form-item>
      </div>

      <a-form-item label="通知文案（可选）">
        <a-textarea
          v-model:value="formState.message"
          :rows="2"
          placeholder="告警通知里附带的说明，例如「错误率超过 10%，检查上游供应商」"
        />
      </a-form-item>

      <a-alert v-if="formError" type="error" show-icon :message="formError" class="form-alert" />
    </a-form>
  </a-modal>
</template>

<style scoped lang="less">
.form-row {
  display: flex;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.grow {
  flex: 1;
  min-width: 150px;
}

.severity {
  width: 190px;
  flex: 0 0 190px;
}

.scope-key {
  width: 200px;
  flex: 0 0 200px;
}

.op {
  width: 96px;
  flex: 0 0 96px;
}

.switch-item {
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  padding-bottom: 6px;
}

.full {
  width: 100%;
}

.form-alert {
  margin-top: var(--space-2);
}

@media (max-width: 700px) {
  .severity,
  .scope-key {
    width: 100%;
    flex: 1 1 100%;
  }
}
</style>