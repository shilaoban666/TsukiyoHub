<script setup lang="ts">
/**
 * 知识库页：统计卡 + 文档列表 + 拖拽上传 + 删除 + 重建索引 + 检索测试。
 *
 * 所有数据都走真实接口（`/api/knowledge/*`），后端未连接时显示重试入口而不是假数据。
 */
import { computed, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  CloudUploadOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  FileTextOutlined,
  ReloadOutlined,
  SearchOutlined,
  SyncOutlined,
} from '@ant-design/icons-vue'
import {
  deleteDocument,
  getStats,
  listDocuments,
  reindex,
  search,
  uploadDocument,
} from '@/api/knowledge'
import { errorText } from '@/api/client'
import { useAppStore } from '@/stores/app'
import { fmtDateTime, fmtNumber, fmtScore, highlight } from '@/utils/format'
import type { DocumentInfo, KnowledgeStats, SearchResult } from '@/types/api'

const app = useAppStore()

// ---------------- 列表与统计 ----------------
const stats = ref<KnowledgeStats | null>(null)
const docs = ref<DocumentInfo[]>([])
const loading = ref(false)
const loadError = ref('')
const reindexing = ref(false)

async function refresh() {
  loading.value = true
  loadError.value = ''
  try {
    const [statsData, listData] = await Promise.all([getStats(), listDocuments()])
    stats.value = statsData
    docs.value = listData.items
  } catch (e) {
    loadError.value = errorText(e)
  } finally {
    loading.value = false
  }
}

// ---------------- 上传 ----------------
interface UploadTask {
  id: string
  name: string
  percent: number
  status: 'uploading' | 'done' | 'error'
  note?: string
}

const uploads = ref<UploadTask[]>([])
const dragOver = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

function pickFiles() {
  fileInput.value?.click()
}

async function handleFiles(files: FileList | File[] | null) {
  if (!files) return
  const arr = Array.from(files as File[])
  if (!arr.length) return

  for (const file of arr) {
    const task: UploadTask = {
      id: `${Date.now()}-${file.name}`,
      name: file.name,
      percent: 0,
      status: 'uploading',
    }
    uploads.value.push(task)
    try {
      const res = await uploadDocument(file, (p) => (task.percent = p))
      task.status = 'done'
      task.percent = 100
      // 后端返回 {ok, count, results:[{file_name, ok, doc_id, title, chunk_count, skipped, reason, error}]}
      const item = res?.results?.[0]
      if (item?.ok === false) {
        task.status = 'error'
        task.note = item.error || '入库失败'
        message.error(`${file.name} 入库失败：${task.note}`)
      } else if (item?.skipped) {
        task.note = item.reason || '内容未变化，已跳过'
        message.info(`${file.name}：${task.note}`)
      } else {
        task.note = item?.chunk_count != null ? `切块 ${item.chunk_count} 段` : ''
        message.success(`${file.name} 已入库`)
      }
    } catch (e) {
      task.status = 'error'
      task.note = errorText(e)
      message.error(`${file.name} 上传失败：${errorText(e)}`)
    }
  }
  await refresh()
  // 成功的任务 3 秒后自动清掉，失败保留给用户看原因
  window.setTimeout(() => {
    uploads.value = uploads.value.filter((t) => t.status !== 'done')
  }, 3000)
}

function onDrop(e: DragEvent) {
  dragOver.value = false
  void handleFiles(e.dataTransfer?.files ?? null)
}

function onFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  void handleFiles(input.files)
  input.value = ''
}

// ---------------- 文档操作 ----------------
const deletingId = ref('')

async function removeDoc(doc: DocumentInfo) {
  deletingId.value = doc.doc_id
  try {
    await deleteDocument(doc.doc_id)
    message.success(`已删除《${doc.title}》`)
    await refresh()
  } catch (e) {
    message.error(`删除失败：${errorText(e)}`)
  } finally {
    deletingId.value = ''
  }
}

async function doReindex() {
  reindexing.value = true
  try {
    await reindex()
    message.success('索引已重建')
    await refresh()
  } catch (e) {
    message.error(`重建失败：${errorText(e)}`)
  } finally {
    reindexing.value = false
  }
}

// ---------------- 检索测试 ----------------
const query = ref('')
const topK = ref(5)
const searching = ref(false)
const result = ref<SearchResult | null>(null)
const searchError = ref('')

async function doSearch() {
  const q = query.value.trim()
  if (!q) {
    message.warning('请输入检索内容')
    return
  }
  searching.value = true
  searchError.value = ''
  result.value = null
  try {
    result.value = await search(q, topK.value)
  } catch (e) {
    searchError.value = errorText(e)
  } finally {
    searching.value = false
  }
}

// ---------------- 表格列 ----------------
const columns = [
  { title: '文档', key: 'title', dataIndex: 'title' },
  { title: '类型', key: 'doc_type', dataIndex: 'doc_type', width: 90 },
  { title: '切块数', key: 'chunk_count', dataIndex: 'chunk_count', width: 90 },
  { title: '来源', key: 'source_type', dataIndex: 'source_type', width: 90 },
  { title: '更新时间', key: 'updated_at', dataIndex: 'updated_at', width: 170 },
  { title: '操作', key: 'action', width: 80 },
]

const statsCards = computed(() => {
  const s = stats.value
  return [
    { label: '文档数', value: fmtNumber(s?.document_count), icon: FileTextOutlined },
    { label: '切块数', value: fmtNumber(s?.chunk_count), icon: DatabaseOutlined },
    { label: '向量数', value: fmtNumber(s?.vector_count), icon: SyncOutlined },
    { label: '向量后端', value: s?.vector_store || '-', icon: CloudUploadOutlined },
  ]
})

onMounted(refresh)
</script>

<template>
  <div class="page">
    <div class="page-inner">
      <!-- 头部 -->
      <header class="page-head">
        <div>
          <h1 class="page-title">知识库</h1>
          <p class="page-desc">上传文档 → 自动解析切块 → 向量化入库，聊天时按语义检索并给出引用。</p>
        </div>
        <div class="head-actions">
          <a-button :loading="loading" @click="refresh">
            <template #icon><ReloadOutlined /></template>
            刷新
          </a-button>
          <a-button :loading="reindexing" @click="doReindex">
            <template #icon><SyncOutlined /></template>
            重建索引
          </a-button>
        </div>
      </header>

      <!-- 统计卡 -->
      <div class="stats">
        <div v-for="card in statsCards" :key="card.label" class="stat-card">
          <component :is="card.icon" class="stat-icon" />
          <div class="stat-body">
            <div class="stat-value">{{ card.value }}</div>
            <div class="stat-label">{{ card.label }}</div>
          </div>
        </div>
      </div>

      <a-alert
        v-if="loadError"
        class="block"
        type="error"
        show-icon
        :message="loadError"
        description="请确认后端已启动（默认 http://127.0.0.1:8080）。"
      >
        <template #action>
          <a-button size="small" @click="refresh">重试</a-button>
        </template>
      </a-alert>

      <!-- 上传区 -->
      <section class="block">
        <div
          class="dropzone"
          :class="{ over: dragOver }"
          @click="pickFiles"
          @dragover.prevent="dragOver = true"
          @dragleave.prevent="dragOver = false"
          @drop.prevent="onDrop"
        >
          <CloudUploadOutlined class="drop-icon" />
          <p class="drop-title">把文件拖到这里，或点击选择文件</p>
          <p class="drop-hint">支持 .md / .txt / .docx / .pdf / .xlsx 等（单文件上限以后端配置为准）</p>
          <input
            ref="fileInput"
            class="file-input"
            type="file"
            multiple
            @change="onFileChange"
          />
        </div>

        <div v-if="uploads.length" class="uploads">
          <div v-for="t in uploads" :key="t.id" class="upload-item">
            <span class="upload-name ellipsis">{{ t.name }}</span>
            <a-progress
              :percent="t.percent"
              :status="t.status === 'error' ? 'exception' : t.status === 'done' ? 'success' : 'active'"
              size="small"
              class="upload-bar"
            />
            <span class="upload-note text-3">{{ t.note || '' }}</span>
          </div>
        </div>
      </section>

      <!-- 文档列表 -->
      <section class="block">
        <div class="block-head">
          <h2 class="block-title">文档（{{ docs.length }}）</h2>
        </div>
        <a-table
          :columns="columns"
          :data-source="docs"
          :loading="loading"
          :pagination="docs.length > 10 ? { pageSize: 10 } : false"
          row-key="doc_id"
          size="middle"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.key === 'title'">
              <div class="doc-title">{{ record.title || record.file_name || record.doc_id }}</div>
              <div class="doc-id mono text-3">{{ record.doc_id }}</div>
            </template>
            <template v-else-if="column.key === 'doc_type'">
              <a-tag>{{ record.doc_type || 'doc' }}</a-tag>
            </template>
            <template v-else-if="column.key === 'chunk_count'">
              {{ fmtNumber(record.chunk_count) }}
            </template>
            <template v-else-if="column.key === 'source_type'">
              <span class="text-2">{{ record.source_type || 'local' }}</span>
            </template>
            <template v-else-if="column.key === 'updated_at'">
              <span class="text-2">{{ fmtDateTime(record.updated_at || record.ingested_at) }}</span>
            </template>
            <template v-else-if="column.key === 'action'">
              <a-popconfirm
                :title="`删除《${record.title}》及其全部切块？`"
                ok-text="删除"
                cancel-text="取消"
                @confirm="removeDoc(record)"
              >
                <a-button type="text" size="small" danger :loading="deletingId === record.doc_id">
                  <template #icon><DeleteOutlined /></template>
                </a-button>
              </a-popconfirm>
            </template>
          </template>
          <template #emptyText>
            <div class="table-empty">还没有文档，先上传一个试试。</div>
          </template>
        </a-table>
      </section>

      <!-- 检索测试 -->
      <section class="block">
        <div class="block-head">
          <h2 class="block-title">检索测试</h2>
          <span class="text-3 hint">直接调用 /api/knowledge/search，验证检索效果</span>
        </div>

        <div class="search-bar">
          <a-input
            v-model:value="query"
            placeholder="输入一句话，看命中的文档片段与分数"
            allow-clear
            @press-enter="doSearch"
          />
          <a-select v-model:value="topK" class="topk">
            <a-select-option :value="3">top 3</a-select-option>
            <a-select-option :value="5">top 5</a-select-option>
            <a-select-option :value="8">top 8</a-select-option>
          </a-select>
          <a-button type="primary" :loading="searching" @click="doSearch">
            <template #icon><SearchOutlined /></template>
            检索
          </a-button>
        </div>

        <a-alert v-if="searchError" type="error" show-icon :message="searchError" class="block" />

        <div v-if="result" class="search-result">
          <div class="search-meta text-2">
            命中 {{ result.hits.length }} 条 · 耗时 {{ fmtNumber(result.latency_ms) }}ms ·
            {{ result.grounded ? '达到命中阈值' : '低于命中阈值（会被判定为未命中）' }}
            <span v-if="result.grounding_score != null">
              · 最高分 {{ fmtScore(result.grounding_score) }}
            </span>
          </div>

          <div v-if="!result.hits.length" class="table-empty">没有命中任何片段。</div>

          <div v-for="(hit, i) in result.hits" :key="hit.chunk_id || i" class="hit">
            <div class="hit-head">
              <span class="idx">{{ i + 1 }}</span>
              <span class="hit-title">{{ hit.title || '未命名文档' }}</span>
              <span class="hit-score">score {{ fmtScore(hit.score) }}</span>
              <span v-if="hit.vector_score != null" class="text-3">向量 {{ fmtScore(hit.vector_score) }}</span>
              <span v-if="hit.bm25_score != null" class="text-3">BM25 {{ fmtScore(hit.bm25_score) }}</span>
              <span v-if="hit.rerank_score != null" class="text-3">重排 {{ fmtScore(hit.rerank_score) }}</span>
            </div>
            <p class="hit-text" v-html="highlight(hit.text, result.query)" />
          </div>
        </div>
      </section>

      <p v-if="app.offline" class="offline-note">
        后端未连接，页面数据为空。启动命令：
        <code>uvicorn tsukiyohub.app:app --port 8080</code>
      </p>
    </div>
  </div>
</template>

<style scoped lang="less">
.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--space-3);
}

.stat-card {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  background: var(--color-bg-card);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);
}
.stat-icon {
  font-size: 18px;
  color: var(--color-primary);
}
.stat-value {
  font-size: var(--fs-lg);
  font-weight: 600;
  color: var(--color-text-1);
  line-height: 1.2;
}
.stat-label {
  font-size: var(--fs-xs);
  color: var(--color-text-3);
}

.dropzone {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 28px var(--space-4);
  border: 1.5px dashed var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-bg-card);
  cursor: pointer;
  transition:
    border-color var(--dur-fast) var(--ease),
    background-color var(--dur-fast) var(--ease);

  &:hover,
  &.over {
    border-color: var(--color-primary);
    background: var(--color-primary-bg);
  }
}
.drop-icon {
  font-size: 26px;
  color: var(--color-primary);
  margin-bottom: 4px;
}
.drop-title {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--color-text-1);
}
.drop-hint {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-3);
}
.file-input {
  display: none;
}

.uploads {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-top: var(--space-3);
}
.upload-item {
  display: grid;
  grid-template-columns: minmax(120px, 1fr) 2fr minmax(80px, 1fr);
  align-items: center;
  gap: var(--space-3);
  font-size: var(--fs-xs);
}
.upload-name {
  color: var(--color-text-1);
}
.upload-bar {
  margin: 0;
}
.upload-note {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.doc-title {
  font-size: var(--fs-sm);
  color: var(--color-text-1);
}
.doc-id {
  font-size: 11px;
  margin-top: 2px;
}
.table-empty {
  padding: var(--space-6) 0;
  text-align: center;
  color: var(--color-text-3);
  font-size: var(--fs-xs);
}

.search-bar {
  display: flex;
  gap: var(--space-2);
  align-items: center;
}
.topk {
  width: 96px;
  flex: 0 0 96px;
}

.search-result {
  margin-top: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.search-meta {
  font-size: var(--fs-xs);
}

.hit {
  padding: var(--space-3);
  border: 1px solid var(--color-border-light);
  border-radius: var(--radius-md);
  background: var(--color-bg-card);
}
.hit-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
  font-size: var(--fs-xs);
}
.idx {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 4px;
  background: var(--color-primary-bg);
  color: var(--color-primary);
  font-weight: 600;
}
.hit-title {
  font-size: var(--fs-sm);
  font-weight: 500;
  color: var(--color-text-1);
}
.hit-score {
  margin-left: auto;
  color: var(--color-primary);
  font-family: var(--font-mono);
}
.hit-text {
  margin: var(--space-2) 0 0;
  font-size: var(--fs-xs);
  line-height: 1.7;
  color: var(--color-text-2);
  white-space: pre-wrap;
  word-break: break-word;
}

.offline-note {
  font-size: var(--fs-xs);
  color: var(--color-text-3);
  text-align: center;
  code {
    font-family: var(--font-mono);
    background: var(--color-bg-hover);
    padding: 1px 6px;
    border-radius: 4px;
  }
}

@media (max-width: 700px) {
  .search-bar {
    flex-wrap: wrap;
  }
  .upload-item {
    grid-template-columns: 1fr;
  }
}
</style>