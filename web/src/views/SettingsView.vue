<script setup lang="ts">
/**
 * 设置页：系统配置（只读展示 + 可写字段编辑）、人设、主题、API Key、连通性。
 *
 * 关键设计：**可写字段只有后端允许的 9 个**，其余（模型、向量后端、检索权重）只读展示。
 * 原因：这些属于部署期配置，前端改了不起作用会让人误以为保存失败（旧前端踩过的坑）。
 * 保存时只提交「用户真正改过的字段」，避免把后端默认值覆盖成前端旧快照。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  ApiOutlined,
  BgColorsOutlined,
  CheckCircleOutlined,
  ReloadOutlined,
  SaveOutlined,
  SettingOutlined,
  UserOutlined,
} from '@ant-design/icons-vue'
import {
  getPersona,
  getSystemConfig,
  getSystemInfo,
  saveSystemConfig,
} from '@/api/system'
import { errorText } from '@/api/client'
import { useAppStore, type ThemeMode } from '@/stores/app'
import { fmtNumber } from '@/utils/format'
import type { SystemConfig, SystemConfigPatch } from '@/types/api'

const app = useAppStore()

// ---------------- 配置加载 ----------------
const config = ref<SystemConfig | null>(null)
const info = ref<Record<string, unknown> | null>(null)
const loading = ref(false)
const loadError = ref('')
const saving = ref(false)
const testing = ref(false)

/** 表单（= 可写字段） */
const form = reactive({
  bot_name: '',
  persona_prompt: '',
  default_tone: 'friendly',
  default_length: 'medium',
  default_markdown: true,
  fallback_text: '',
  retrieval_top_k: 8,
  memory_enabled: true,
})

/** 载入时的快照，用于算 diff（只提交改动过的字段） */
let snapshot: SystemConfigPatch = {}

const TONES = [
  { value: 'formal', label: '正式' },
  { value: 'friendly', label: '友好' },
  { value: 'concise', label: '简洁' },
]
const LENGTHS = [
  { value: 'short', label: '简短' },
  { value: 'medium', label: '适中' },
  { value: 'long', label: '详细' },
]

async function loadAll() {
  loading.value = true
  loadError.value = ''
  try {
    const cfg = await getSystemConfig()
    config.value = cfg

    // 人设等可写字段优先从 /system/persona 回显（config 里不回显人设长文本）
    let persona: Record<string, unknown> = {}
    try {
      persona = (await getPersona()) as unknown as Record<string, unknown>
    } catch {
      /* 后端可能没有该接口：降级用 config 里能看到的字段 */
    }

    form.bot_name = String(persona.bot_name ?? cfg.bot_name ?? 'Tsukiyo')
    form.persona_prompt = String(persona.persona_prompt ?? '')
    form.default_tone = String(persona.default_tone ?? cfg.style?.tone ?? 'friendly')
    form.default_length = String(persona.default_length ?? cfg.style?.length ?? 'medium')
    form.default_markdown = Boolean(persona.default_markdown ?? cfg.style?.markdown ?? true)
    form.fallback_text = String(
      persona.fallback_text ?? '抱歉，我暂时无法回答这个问题。',
    )
    form.retrieval_top_k = Number(
      persona.retrieval_top_k ?? cfg.retrieval?.top_k ?? 8,
    )
    form.memory_enabled = Boolean(persona.memory_enabled ?? cfg.features?.memory ?? true)

    snapshot = { ...form }
  } catch (e) {
    loadError.value = errorText(e)
  } finally {
    loading.value = false
  }

  // 自述信息是可选接口，失败不影响页面
  try {
    info.value = await getSystemInfo()
  } catch {
    info.value = null
  }
}

// ---------------- 保存（只提交改动字段） ----------------
function buildPatch(): SystemConfigPatch {
  const patch: SystemConfigPatch = {}
  if (form.bot_name !== snapshot.bot_name) patch.bot_name = form.bot_name
  if (form.persona_prompt !== snapshot.persona_prompt) patch.persona_prompt = form.persona_prompt
  if (form.default_tone !== snapshot.default_tone) patch.default_tone = form.default_tone
  if (form.default_length !== snapshot.default_length) patch.default_length = form.default_length
  if (form.default_markdown !== snapshot.default_markdown) patch.default_markdown = form.default_markdown
  if (form.fallback_text !== snapshot.fallback_text) patch.fallback_text = form.fallback_text
  if (form.retrieval_top_k !== snapshot.retrieval_top_k) patch.retrieval_top_k = form.retrieval_top_k
  if (form.memory_enabled !== snapshot.memory_enabled) patch.memory_enabled = form.memory_enabled
  return patch
}

const dirty = computed(() => Object.keys(buildPatch()).length > 0)

async function save() {
  const patch = buildPatch()
  if (!Object.keys(patch).length) {
    message.info('没有修改需要保存')
    return
  }
  saving.value = true
  try {
    const updated = await saveSystemConfig(patch)
    if (updated) config.value = updated
    snapshot = { ...form }
    message.success('已保存，下一次对话立即生效')
  } catch (e) {
    message.error(`保存失败：${errorText(e)}`)
  } finally {
    saving.value = false
  }
}

function resetForm() {
  Object.assign(form, snapshot)
  message.info('已还原为上次载入的值')
}

// ---------------- 主题 / API Key / 连通性 ----------------
const themeMode = computed({
  get: () => app.themeMode,
  set: (v: ThemeMode) => app.setTheme(v),
})

const apiKey = ref(app.apiKey)
function persistApiKey() {
  app.saveApiKey(apiKey.value)
  message.success(apiKey.value ? '已保存 API Key' : '已清空 API Key')
}

async function testConnection() {
  testing.value = true
  try {
    const ok = await app.refreshHealth()
    if (ok) message.success('后端连接正常')
    else message.error(app.backend.error || '后端未连接')
  } finally {
    testing.value = false
  }
}

// ---------------- 只读信息 ----------------
const readonlyRows = computed(() => {
  const c = config.value
  if (!c) return []
  return [
    { label: '应用', value: `${c.app_name || 'TsukiyoHub'} ${c.version || ''} (${c.env || '-'})` },
    { label: '对话模型', value: `${c.model || '-'}${c.provider ? ` · ${c.provider}` : ''}` },
    { label: '模型就绪', value: c.llm_ready ? '是' : '否（未配置 API Key）' },
    { label: '向量后端', value: c.vector_backend || '-' },
    { label: '向量化模型', value: c.embedding_model || '-' },
    { label: '数据库', value: c.database || '-' },
    {
      label: '检索参数',
      value: c.retrieval
        ? `top_k=${c.retrieval.top_k} · 向量权重 ${c.retrieval.vector_weight} · BM25 权重 ${c.retrieval.bm25_weight} · 命中阈值 ${c.retrieval.grounding_min_score}`
        : '-',
    },
    {
      label: '功能开关',
      value: c.features
        ? Object.entries(c.features)
            .map(([k, v]) => `${k}=${v ? '开' : '关'}`)
            .join(' · ')
        : '-',
    },
  ]
})

const backendVersion = computed(() => {
  const i = info.value
  if (!i) return ''
  const version = i.version ?? i.app_version
  const env = i.env
  return [version ? `v${String(version)}` : '', env ? String(env) : ''].filter(Boolean).join(' · ')
})

onMounted(loadAll)
</script>

<template>
  <div class="page">
    <div class="page-inner">
      <header class="page-head">
        <div>
          <h1 class="page-title">设置</h1>
          <p class="page-desc">
            人设、回复风格与检索参数可在此调整；模型与向量后端属于部署期配置，仅供查看。
          </p>
        </div>
        <div class="head-actions">
          <a-button :loading="loading" @click="loadAll">
            <template #icon><ReloadOutlined /></template>
            重新载入
          </a-button>
          <a-button type="primary" :loading="saving" :disabled="!dirty" @click="save">
            <template #icon><SaveOutlined /></template>
            保存
          </a-button>
        </div>
      </header>

      <a-alert
        v-if="loadError"
        type="error"
        show-icon
        :message="loadError"
        description="请确认后端已启动（默认 http://127.0.0.1:8080）。"
      >
        <template #action>
          <a-button size="small" @click="loadAll">重试</a-button>
        </template>
      </a-alert>

      <a-skeleton v-if="loading && !config" active :paragraph="{ rows: 10 }" />

      <template v-else>
        <!-- 人设与风格 -->
        <section class="card">
          <div class="card-head">
            <UserOutlined class="card-icon" />
            <div>
              <h2 class="card-title">人设与回复风格</h2>
              <p class="card-sub">保存后对后续对话生效（历史消息不受影响）。</p>
            </div>
          </div>

          <div class="form">
            <label class="field">
              <span class="field-label">助手名称</span>
              <a-input v-model:value="form.bot_name" :maxlength="40" placeholder="例如：Tsukiyo" />
            </label>

            <label class="field">
              <span class="field-label">人设提示词</span>
              <a-textarea
                v-model:value="form.persona_prompt"
                :rows="5"
                :maxlength="4000"
                show-count
                placeholder="描述助手的身份、语气与边界。可用占位符 {bot_name} {user_name} {current_date}。"
              />
            </label>

            <div class="field-row">
              <label class="field">
                <span class="field-label">语气</span>
                <a-select v-model:value="form.default_tone">
                  <a-select-option v-for="t in TONES" :key="t.value" :value="t.value">
                    {{ t.label }}（{{ t.value }}）
                  </a-select-option>
                </a-select>
              </label>

              <label class="field">
                <span class="field-label">回答长度</span>
                <a-select v-model:value="form.default_length">
                  <a-select-option v-for="l in LENGTHS" :key="l.value" :value="l.value">
                    {{ l.label }}（{{ l.value }}）
                  </a-select-option>
                </a-select>
              </label>

              <label class="field">
                <span class="field-label">Markdown 输出</span>
                <a-switch v-model:checked="form.default_markdown" />
              </label>
            </div>

            <label class="field">
              <span class="field-label">未命中知识库时的兜底话术</span>
              <a-input v-model:value="form.fallback_text" :maxlength="200" />
            </label>
          </div>
        </section>

        <!-- 检索与记忆 -->
        <section class="card">
          <div class="card-head">
            <SettingOutlined class="card-icon" />
            <div>
              <h2 class="card-title">检索与记忆</h2>
              <p class="card-sub">影响每次对话召回的片段数量与是否记住用户偏好。</p>
            </div>
          </div>

          <div class="form">
            <div class="field-row">
              <label class="field">
                <span class="field-label">召回条数 top_k</span>
                <a-input-number v-model:value="form.retrieval_top_k" :min="1" :max="30" class="num" />
              </label>

              <label class="field">
                <span class="field-label">事实记忆</span>
                <a-switch v-model:checked="form.memory_enabled" />
              </label>
            </div>
          </div>
        </section>

        <!-- 部署信息（只读） -->
        <section class="card">
          <div class="card-head">
            <ApiOutlined class="card-icon" />
            <div>
              <h2 class="card-title">后端信息（只读）</h2>
              <p class="card-sub">
                这些属于部署期配置，改完需要重启后端或改环境变量，因此页面上不提供编辑。
              </p>
            </div>
          </div>

          <div class="rows">
            <div v-for="row in readonlyRows" :key="row.label" class="row">
              <span class="row-label">{{ row.label }}</span>
              <span class="row-value">{{ row.value }}</span>
            </div>
          </div>

          <div v-if="config?.skills?.length" class="skills-line">
            启用技能：<a-tag v-for="s in config.skills" :key="s" class="skill-tag">{{ s }}</a-tag>
          </div>
        </section>

        <!-- 外观 -->
        <section class="card">
          <div class="card-head">
            <BgColorsOutlined class="card-icon" />
            <div>
              <h2 class="card-title">外观</h2>
              <p class="card-sub">主题仅保存在本机浏览器，不影响后端。</p>
            </div>
          </div>

          <a-radio-group v-model:value="themeMode" button-style="solid">
            <a-radio-button value="light">浅色</a-radio-button>
            <a-radio-button value="dark">深色</a-radio-button>
            <a-radio-button value="system">跟随系统</a-radio-button>
          </a-radio-group>
        </section>

        <!-- 连接与鉴权 -->
        <section class="card">
          <div class="card-head">
            <CheckCircleOutlined class="card-icon" />
            <div>
              <h2 class="card-title">连接与鉴权</h2>
              <p class="card-sub">
                后端配置了 API_KEY 时需要填同一个值；请求会带
                <code>Authorization: Bearer ...</code>。
              </p>
            </div>
          </div>

          <div class="form">
            <label class="field">
              <span class="field-label">API Key（本地保存）</span>
              <div class="field-inline">
                <a-input-password
                  v-model:value="apiKey"
                  placeholder="留空表示后端未开启鉴权"
                  @press-enter="persistApiKey"
                />
                <a-button @click="persistApiKey">保存</a-button>
              </div>
            </label>

            <div class="conn">
              <span class="conn-status" :class="app.backend.online ? 'ok' : 'down'">
                {{
                  app.backend.online === true
                    ? '后端已连接'
                    : app.backend.online === false
                      ? '后端未连接'
                      : '尚未检测'
                }}
              </span>
              <span v-if="backendVersion" class="text-3 conn-version">{{ backendVersion }}</span>
              <span v-if="app.backend.online" class="text-3 conn-version">
                文档 {{ fmtNumber(app.backend.documents) }} · 切块
                {{ fmtNumber(app.backend.chunks) }}
              </span>
              <a-button size="small" :loading="testing" class="conn-btn" @click="testConnection">
                测试连接
              </a-button>
            </div>
          </div>
        </section>

        <p v-if="dirty" class="dirty-hint">有未保存的修改</p>
        <p v-else class="dirty-hint text-3">
          与上次载入一致。需要还原改动时可 <a @click="resetForm">放弃修改</a>。
        </p>
      </template>
    </div>
  </div>
</template>

<style scoped lang="less">
.card {
  background: var(--color-bg-card);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);
  padding: var(--space-4);
}

.card-head {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}
.card-icon {
  font-size: 16px;
  color: var(--color-primary);
  margin-top: 3px;
}
.card-title {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--color-text-1);
}
.card-sub {
  margin: 2px 0 0;
  font-size: var(--fs-xs);
  color: var(--color-text-3);
  line-height: 1.6;

  code {
    font-family: var(--font-mono);
    background: var(--color-bg-hover);
    padding: 1px 5px;
    border-radius: 4px;
  }
}

.form {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.field-label {
  font-size: var(--fs-xs);
  color: var(--color-text-2);
}
.field-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--space-3);
  align-items: end;
}
.field-inline {
  display: flex;
  gap: var(--space-2);
}
.num {
  width: 120px;
}

.rows {
  display: flex;
  flex-direction: column;
}
.row {
  display: grid;
  grid-template-columns: 120px 1fr;
  gap: var(--space-3);
  padding: 7px 0;
  border-bottom: 1px solid var(--color-border-light);
  font-size: var(--fs-xs);

  &:last-child {
    border-bottom: 0;
  }
}
.row-label {
  color: var(--color-text-3);
}
.row-value {
  color: var(--color-text-1);
  word-break: break-word;
}

.skills-line {
  margin-top: var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-text-2);
}
.skill-tag {
  margin: 2px 4px 2px 0;
}

.conn {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
  font-size: var(--fs-xs);
}
.conn-status {
  display: inline-flex;
  align-items: center;
  gap: 5px;

  &::before {
    content: '';
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--color-text-3);
  }
  &.ok::before {
    background: var(--color-success);
  }
  &.down::before {
    background: var(--color-error);
  }
}
.conn-version {
  font-size: 11px;
}
.conn-btn {
  margin-left: auto;
}

.dirty-hint {
  text-align: center;
  font-size: var(--fs-xs);
  color: var(--color-warning);
  margin: 0;

  a {
    cursor: pointer;
    text-decoration: underline;
  }
}
</style>