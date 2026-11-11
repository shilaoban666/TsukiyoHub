/**
 * 知识库接口。
 *
 * 后端契约：
 * - `GET /api/knowledge/stats` → KnowledgeStats
 * - `GET /api/knowledge/documents` → `{items: DocumentInfo[], total}`
 * - `POST /api/knowledge/upload` multipart 字段名 `file` → `{ok, doc_id, title, chunk_count, skipped, reason}`
 * - `POST /api/knowledge/ingest` body `{path}` → 扫描服务端目录入库
 * - `DELETE /api/knowledge/documents/{doc_id}` → `{ok: true}`
 * - `POST /api/knowledge/reindex` → 重建索引
 * - `POST /api/knowledge/search` body `{query, top_k}` → `{query, hits, latency_ms, grounded, grounding_score}`
 */
import { http } from './client'
import { normalizeList } from './sessions'
import type {
  DocumentInfo,
  KnowledgeStats,
  ListResult,
  SearchResult,
  UploadResult,
} from '@/types/api'

/** 文档列表 */
export async function listDocuments(): Promise<ListResult<DocumentInfo>> {
  const data = await http.get<unknown>('/knowledge/documents')
  return normalizeList<DocumentInfo>(data)
}

/** 知识库统计（文档数 / 切块数 / 向量数 / 向量后端） */
export async function getStats(): Promise<KnowledgeStats> {
  return http.get<KnowledgeStats>('/knowledge/stats')
}

/**
 * 上传文档（multipart）。进度回调用于页面的进度条。
 * 不传 Content-Type，让浏览器自动带 boundary。
 */
export async function uploadDocument(
  file: File,
  onProgress?: (percent: number) => void,
): Promise<UploadResult> {
  const form = new FormData()
  form.append('file', file, file.name)
  return http.upload<UploadResult>('/knowledge/upload', form, onProgress)
}

/** 让服务端扫描某个目录并入库（可选能力，界面上放在「高级」里） */
export async function ingestPath(path: string): Promise<unknown> {
  return http.post<unknown>('/knowledge/ingest', { path })
}

/** 删除文档（同时删除其切块与向量） */
export async function deleteDocument(docId: string): Promise<void> {
  await http.del<unknown>(`/knowledge/documents/${encodeURIComponent(docId)}`)
}

/** 重建索引（重新解析 + 切块 + 向量化） */
export async function reindex(): Promise<unknown> {
  return http.post<unknown>('/knowledge/reindex', {})
}

/** 检索测试：拿到 top-k 命中与分数 */
export async function search(query: string, topK = 8): Promise<SearchResult> {
  const data = await http.post<unknown>('/knowledge/search', { query, top_k: topK })
  const obj = (data ?? {}) as Partial<SearchResult>
  return {
    query: obj.query ?? query,
    hits: Array.isArray(obj.hits) ? obj.hits : [],
    latency_ms: obj.latency_ms ?? 0,
    grounded: obj.grounded ?? false,
    grounding_score: obj.grounding_score ?? null,
  }
}