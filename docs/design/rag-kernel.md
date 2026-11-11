# RAG 内核抽取设计（源：fancyqube_robot → 目标：TsukiyoHub）

> 侦察对象：`E:\Project\HQDingtalkrobot\src\fancyqube_robot\`（只读，未改任何源码）。
> 目标：把「入库 → 检索 → 生成约束 → 缓存」四块抽成与钉钉/公司业务无关的通用服务。
> 本文所有签名与参数均来自实读代码；标 ⚠️ 的是源码里已有的矛盾或必须由 Lead 拍板的点。

## 0. 结论摘要（给实现者的 8 行）

1. 真正**零业务耦合**的只有 `rag/`（除 `answer_style.VOICE/base.py` 的人设文案）+ `ingest/chunker.py` + `tokenize.py`；`kb/store.py`、`kbsync/*`、`kb/visibility.py`、`kb/links.py` 是钉钉/组织语义的重灾区。
2. 检索核心是 **两路召回（Milvus 稠密 + Milvus 内置 BM25）→ 加权 RRF → L1 特征重排 → 可选 L2 模型重排 → 每文档上限**，融合逻辑在进程内、`k=60`、权重 `0.6/0.4`。
3. 防幻觉是**三段式**：入库前 `grounding.grade` 闸门（hit/fuzzy/miss）→ 生成时 `answer_style.GROUNDING_RULES` → 生成后 `ground_check` 逐句删无依据句。
4. 引用有严格不变式：**片段编号 = 正文 `[n]` = `Citation.index`**，`service/citations.renumber` 把逐段编号收敛成逐文档编号（正文顺序、去重、页码后缀、补足到 5 篇）。
5. 降级是硬要求：无 Key → 伪向量 + 仅 BM25；向量化失败 → 查询回退伪向量、检索回退 BM25；Milvus 熔断 30s 期间检索返回空。
6. ⚠️ **索引期伪向量是自相矛盾的**：`embed_cache` 注释说「索引必须严格、伪向量会永久污染」，但 `kb/store._write` 捕获异常后照样按伪向量写入。新内核必须二选一（本文建议：`enabled=False` 才允许伪向量入索引，异常一律让任务失败重试）。
7. ⚠️ **分数刻度未钉死**：`backend.to_score = 1/(1+sqL2)`、`cosine_to_score = 1/(3-2cos)`，而 `config.knowledge_grounding_min_score=0.48` 是按**原始余弦**标定的（0.5392 vs 0.4749）。用 0.48 去比转换后的分数会误命中（0.4749 → 0.4877）。必须一次 conformance test 钉死刻度。
8. 建议交付顺序：契约 + 内存实现 + 切块（可 demo）→ Milvus + 入库幂等 → 融合/重排/闸门/引用 → contextualize + 语义缓存。

## 1. 职责边界与源文件 → 目标文件对照表

### 1.1 四块职责边界

| 块 | 输入 | 输出 | 不允许做的事 |
|---|---|---|---|
| 入库 | 字节 / 本地路径 | `Document` → `Chunk[]` + `IndexRow[]` 写入 `VectorStore` + 元数据落库 | 不读钉钉、不判可见范围、不发消息 |
| 检索 | `query` + `SearchFilters` | `RetrievalResult{items, timings, rerank, partial}` | 不生成答案、不拼引用文案、不认识用户 |
| 生成约束 | `question` + `ScoredChunk[]` | `grade`（hit/fuzzy/miss）+ 编号上下文 + `Citation[]` | 不调用 LLM 生成正文（除 `ground_check` 的核对调用） |
| 缓存 | 文本 / 问题 | 向量、答案 | 不跨作用域复用、不缓存降级结果 |

### 1.2 对照表（status：keep=近乎照搬 / port=改签名为端口 / rewrite=重写 / drop=删除）

| 源文件（`src/fancyqube_robot/`） | 目标文件（`src/tsukiyohub/`） | status | 说明 |
|---|---|---|---|
| `rag/backend.py` | `rag/contracts.py` + `rag/ports/vector_store.py` | port | `SearchBackend` 拆成 `VectorStore` 端口与 `IndexRow/SearchFilters` 数据类；`to_score/cosine_to_score` 保留但重新钉刻度 |
| `rag/fusion.py` | `rag/fusion.py` | keep | 单路 `HybridRetriever` 保留（无 Milvus 的简单部署用）；去掉 `service.metrics` 依赖，改注入 `Metrics` 端口 |
| `rag/multi_query.py` | `rag/multi_query.py` | keep | 主链路。删掉 `self.store` 门面依赖，改成 `(store: VectorStore, embedder: EmbeddingProvider, reranker: Reranker)` 三参构造 |
| `rag/rerank.py` | `rag/rerank.py` + `rag/adapters/rerank_dashscope.py` | port | `RerankProvider.scores` 升级为 `Reranker` 端口；`FakeRerank` 保留为测试替身 |
| `rag/grounding.py` | `rag/grounding.py` | keep | 纯函数，零依赖；`GENERIC_TERMS/_CHITCHAT_PATTERNS` 改可配置（默认值原样保留） |
| `rag/ground_check.py` | `rag/ground_check.py` | keep | 纯函数 + 注入 `complete(prompt) -> str`；`agentic.rules.numbers_of` 需一起搬成 `rag/numbers.py` |
| `rag/prompt.py` | `rag/prompt.py` | keep | `format_context/pack_context/build_citations/extractive_answer` 全留；`NO_HIT_ANSWER` 文案外置为资源文件 |
| `rag/answer_style.py` | `rag/style.py` | rewrite | 只留 `ANSWER_TEMPLATE/GROUNDING_RULES/BANNED_PHRASES/length_*`；`VOICE`（公司资深同事人设）与钉钉 Markdown 版式删掉 |
| `rag/citations.py`（源在 `service/citations.py`） | `rag/citations.py` | keep | `renumber/tidy_refs/merge_pages/related/unique_sources` 是通用算法，整体搬运 |
| `rag/embeddings.py` | `rag/adapters/embedding_dashscope.py` | port | 去掉 `langchain_core.embeddings.Embeddings` 继承（省一个重依赖），实现 `EmbeddingProvider` |
| `rag/embed_cache.py` | `rag/caches/embed_cache.py` | port | DB 依赖改 `KvStore` 端口（`get_many/put_many/delete_older_than`）；键格式 `sha256(model|dim|text)` 保留 |
| `rag/semantic_cache.py` | `rag/caches/semantic_cache.py` | keep | 作用域键改通用 `scope` 字符串（见 3.7），`personal()` 判据与业务相关，改成注入 `is_cacheable(user_ctx)` |
| `rag/tokenize.py` | `rag/tokenize.py` | keep | jieba/二元组双档 + `token_set` lru_cache 全留；停用词表可外置 |
| `rag/pipeline.py` | `rag/pipeline.py` | keep | 参数默认值（recallK=100/candidates=150/finalK=8/maxPerDoc=5）原样；`deptBoost` 删掉 |
| `rag/base.py` | — | drop | `Retriever`/`LexicalReranker` 两个占位实现被正式端口取代（`LexicalReranker` 并入 `rerank.l1`） |
| `kb/store.py` | `rag/store.py`（可选门面） | rewrite | 清单 JSON 落盘 + 内存切块副本 → 换 `MetadataStore` 端口（SQLite/PG）；「钉钉同步由 kbsync 直接写 Milvus」的分叉删掉 |
| `kb/contextual.py` | `ingest/contextual.py` | keep | 提示词与 `PROMPT_VERSION="ctx1"` 保留；`terms` 参数改注入 `TermExpander` |
| `kb/links.py` | `rag/ports/linker.py`（`CitationLinker`） | port | 钉盘 URL 模板、签名令牌、`public_base` 全删；内核只留 `hydrate(citations, ctx)` 钩子 |
| `kb/visibility.py` | — | drop | 部门/群/保密库可见范围属业务；内核只认 `SearchFilters` |
| `kb/{faq,lexicon,terms,actions,release,summary}.py` | — | drop | 术语表、FAQ 对、快捷操作、发布版本号，均为公司业务；`terms` 以端口形式保留可注入 |
| `ingest/parser.py` | `ingest/parser.py` | keep | 编码探测 + Loader 降级表 + `ParsedDocument` 全留；删 `_raw_root_of`（`data/raw` 约定）改 `infer_metadata(root=)` 参数 |
| `ingest/chunker.py` | `ingest/chunker.py` | keep | 四种 mode + 表格保护 + 面包屑织入 + `ChunkRule.signature()` 全留 |
| `ingest/ocr.py` | `ingest/ocr.py` | port | `kbsync.keys/ratelimit` 依赖改为 `BlobStore` + 通用 `TokenBucket`；`QwenOcrClient` 保留为默认实现 |
| `ingest/vision.py` | `ingest/vision.py` | keep | 提示词/KINDS/CONFIDENT=0.75 保留；`kbsync.keys` 依赖同上替换 |
| `ingest/cleaner.py` | `ingest/cleaner.py` | keep | 页眉页脚/乱码清洗；`RULES_VERSION` 要进切块产物签名 |
| `ingest/pipeline.py` | `index/pipeline.py` | rewrite | 本地目录扫描 + `_prune_missing` 语义保留，来源改 `DocumentSource` 端口列表 |
| `ingest/sources.py` | `index/sources.py` | port | `KnowledgeSource` 抽象保留（`list_documents/load`），`DingTalkSource` 删除，`UploadSource` 保留 |
| `ingest/convert.py` | `ingest/convert.py` | keep | LibreOffice 无界面转换，纯通用 |
| `kbsync/*`（22 文件） | `index/jobs.py` | drop→rewrite | 只保留 `queue.py` 的**语义**（SKIP LOCKED 领取、僵死 10 分钟回收、退避 30/120/480s、去重入队），重写为 `JobQueue` 端口；钉盘/OSS/事件/公告全部删除 |
| `domain.py` | `rag/contracts.py` | rewrite | 只搬 `Document/DocumentMeta/Chunk/ScoredChunk/Citation/KnowledgeStats/IngestResult`；`Channel/Intent/ChatRequest/ChatResponse/HealthResponse` 归 chat 内核 |
| `api/routes_knowledge.py` | `api/routes_kb.py` | rewrite | 见第 5 节 |

### 1.3 必须删除或抽象掉的耦合（逐项）

| 耦合 | 出现位置 | 处理 |
|---|---|---|
| 钉盘预览链接、`spaceId/dentryId`、签名跳转 | `kb/links.py:28,79-95` | 删；留 `CitationLinker` 端口（返回 `url/folder_url/folder_name`） |
| 群 / 部门 / 保密库可见范围 | `kb/visibility.py:115-160`、`group_kb.scoped` | 删；调用方构造 `SearchFilters(kb_ids, allow_restricted)` |
| `kb://` / `att:` / `upload://` URI 约定 | `kb/links.py:74`、`kbsync/worker.py:475` | 换成不透明 `uri` + 可选 `BlobRef` |
| OSS 对象存储、MNS 事件、`incoming/override/chunks` 缓存键 | `kbsync/{oss_store,objstore,events,keys}.py` | 换成 `BlobStore` 端口 + `KvStore` 端口 |
| 钉钉任务表 `kb_task`、发布版本 `release` | `kbsync/{queue,release}.py` | 换成 `JobQueue` 端口；`release` 删除 |
| OCR 月度页数配额 / `hot`（Redis）计数 | `ingest/ocr.py:94-110` | 保留概念，改 `Quota` 端口；默认实现进程内计数 |
| 部门加权 `deptBoost`、`boost_kbs` | `rag/rerank.py:29,51`、`rag/pipeline.py:30` | 删；`RerankHints` 只留 `categories/history/boost_docs` |
| 人设 `VOICE`、钉钉 Markdown 版式 | `rag/answer_style.py:70-73,156-157` | 删；版式由调用方传入 |
| `Channel.DINGTALK` 等枚举、ChatRequest 长度策略 | `domain.py:19-42,247-291` | 删 |
| 公司泛词表 `GENERIC_TERMS`（公司/员工/制度…） | `rag/grounding.py:37-41` | 保留为默认值，暴露为配置：`grounding.generic_terms: list[str]` |

## 2. 端口（Port/Protocol）设计

### 2.1 数据契约（`rag/contracts.py`，全部 pydantic v2 `BaseModel`）

```python
class DocType(str, Enum): POLICY="policy"; SOP="sop"; NOTICE="notice"; FAQ="faq"; FORM="form"; OTHER="other"

class DocumentMeta(BaseModel):
    doc_id: str                      # 稳定 ID：调用方可给；默认 sha1(相对路径|来源 key)[:16]
    title: str
    source_type: SourceType = SourceType.UPLOAD   # LOCAL/UPLOAD/API（删 DINGTALK）
    doc_type: DocType = DocType.OTHER
    uri: str = ""                    # 不透明来源标识，内核不解析
    file_name: str = ""
    department: str = ""             # 通用化为 owner/namespace 标签，仅用于展示与加权
    version: str = ""
    effective_date: str = ""
    tags: list[str] = []
    updated_at: str = ""             # ISO8601
    extra: dict[str, Any] = {}       # loader/warnings/ocr 等

class Document(BaseModel):
    meta: DocumentMeta
    content: str
    content_hash: str = ""           # sha256(content)，空则入库时算

class Chunk(BaseModel):
    chunk_id: str                    # f"{doc_id}#{version_tag}.{ordinal}"
    doc_id: str
    text: str                        # 展示 + 送模型的正文（含面包屑前缀）
    ordinal: int = 0
    title: str = ""
    uri: str = ""
    source_type: SourceType = SourceType.UPLOAD
    doc_type: DocType = DocType.OTHER
    department: str = ""
    version: str = ""
    updated_at: str = ""
    extra: dict[str, Any] = {}       # 检索属性，见 2.3

class ScoredChunk(BaseModel):
    chunk: Chunk
    score: float = 0.0               # 融合/重排后的最终分
    vector_score: float | None = None    # 稠密刻度，见 3.3
    bm25_score: float | None = None
    rerank_score: float | None = None    # L2 模型分（0~1，跨问题可比）
    retriever: Literal["vector","bm25","hybrid","fusion","none"] = "fusion"

class Citation(BaseModel):
    index: int; doc_id: str; title: str; uri: str = ""; snippet: str = ""
    department: str = ""; version: str = ""; updated_at: str = ""
    score: float = 0.0; label: str = ""; version_id: str = ""
    url: str = ""; folder_url: str = ""; folder_name: str = ""
    cited: bool = True               # False = 仅为补足篇数而列出

class KnowledgeStats(BaseModel):
    document_count: int = 0; chunk_count: int = 0; vector_count: int = 0
    embedding_enabled: bool = False; embedding_model: str | None = None
    vector_store: str = "memory"; dimension: int = 0
    sources: dict[str,int] = {}; doc_types: dict[str,int] = {}
    last_ingested_at: str | None = None; degraded: list[str] = []
```

### 2.2 EmbeddingProvider

```python
class EmbeddingProvider(Protocol):
    name: str                 # "dashscope" | "pseudo" | "openai-compat"
    model: str                # "text-embedding-v3"
    dimension: int            # 1024
    semantic: bool            # False = 伪向量，检索层据此关闭稠密通路
    batch_size: int           # 单次上限（DashScope v3 = 10）

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...  # 严格：失败抛 EmbeddingError
    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]: ...    # 宽松：可回退伪向量

    def pseudo_vector(self, text: str) -> list[float]: ...                    # sha256 确定性哈希，L2 归一化
```

**约束（照做）**
- 输出必须 **L2 归一化**；`len(vector) != dimension` 或空向量 → 抛 `EmbeddingError`（源实现明确：空向量入库会污染整个索引）。
- 批量必须按 `batch_size` 切片；返回条数不等即报错。
- `pseudo` 实现：`state=int(sha256(text)[:8])`，`x_{n+1}=(6364136223846793005·x_n+1442695040888963407) mod 2^64`，取 `x/2^64*2-1`，再归一化。**`semantic=False`**。
- `FallbackEmbeddings` 语义保留：`_embed` 失败**逐批**回退伪向量；查询向量进程内 LRU（1 小时 / 4096 条），**回退的伪向量不缓存**。

### 2.3 VectorStore

```python
@dataclass
class IndexRow:
    chunk: Chunk
    vector: list[float]          # 空列表 = 不写稠密（仅 BM25 可检索）
    search_text: str = ""        # 送 BM25/向量化的文本 = 《标题》版本 > 章节 \n 上下文 \n 正文；空则用 chunk.text

@dataclass(frozen=True)
class SearchFilters:
    kb_ids: frozenset[str] | None = None      # None = 不限；frozenset() = 空集（务必区分！）
    current_only: bool = True                 # 只取 is_current == true
    as_of: int | None = None                  # yyyymmdd；给定时取 version_date <= as_of（忽略 current_only）
    doc_types: frozenset[str] | None = None
    allow_restricted: frozenset[str] = frozenset()   # 允许放行的 restricted=True 的 doc_id
    def expr(self) -> str: ...      # Milvus 布尔表达式，见 2.7
    def match(self, chunk: Chunk) -> bool: ...       # 返回后兜底校验（下推表达式 + 本地二次过滤）

class VectorStore(Protocol):
    name: str
    def ensure_ready(self) -> None: ...                       # 建集合/建索引/加载；幂等
    def upsert(self, rows: Sequence[IndexRow]) -> int: ...
    def remove(self, *, doc_id: str | None = None, version_id: str | None = None,
               kb_id: str | None = None, chunk_ids: Sequence[str] | None = None) -> int: ...
    def set_flags(self, *, doc_id: str | None = None, version_id: str | None = None,
                  kb_id: str | None = None, **flags: Any) -> int: ...   # enabled/is_current/restricted
    def search_dense(self, vector: Sequence[float], filters: SearchFilters, k: int) -> list[tuple[Chunk, float]]: ...
    def search_lexical(self, text: str, filters: SearchFilters, k: int) -> list[tuple[Chunk, float]]: ...
    def count(self, kb_id: str | None = None) -> int: ...
    def count_by_kb(self) -> dict[str, int]: ...
    def get(self, chunk_ids: Sequence[str]) -> list[Chunk]: ...
    def ping(self) -> bool: ...
    def stats(self) -> dict[str, Any]: ...
```

**语义要点**
- 两条召回**分开**暴露（不合并成 `search()`），因为融合必须发生在调用方的 RRF 里，且需保留 `vector_score/bm25_score` 供闸门与调试。
- `remove()` 且所有键都为 `None` → **返回 0，绝不清库**（源实现 `_target_expr` 为空即返回，防误删）。
- `set_flags()` 是「查整行 → 改标量 → upsert 回」，不得重新向量化（BM25 稀疏向量由函数重算）。
- `search_*` **失败返回空列表**而非抛错（上层要能降级）；写操作必须抛错（由重试兜住）。
- `Chunk.extra` 里的检索属性固定为：`kb_id / version_id / is_current / enabled / restricted / version_date(int) / parent_id / source / breadcrumb`。

### 2.4 BlobStore

```python
class BlobStore(Protocol):
    def put(self, key: str, data: bytes, *, content_type: str = "") -> str: ...  # 返回 key
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def signed_url(self, key: str, *, ttl_s: int = 300) -> str | None: ...       # 不支持签名实现返回 None
```
用途：原文件、OCR/vision 结果缓存（`ocr/{sha256}` gzip）、解析后纯文本缓存（`text/{sha256}/{sig}`）。本机实现 `LocalBlobStore(root: Path)`、测试 `MemoryBlobStore`。

### 2.5 Reranker

```python
@dataclass(frozen=True)
class RerankHints:
    categories: frozenset[str] = frozenset()
    history: bool = False              # 有时间意图时不惩罚历史版本
    boost_docs: frozenset[str] = frozenset()

@dataclass
class RerankResult:
    items: list[ScoredChunk]
    level: Literal["L1","L2"]
    degraded: bool = False             # L2 请求了但失败/熔断

class Reranker(Protocol):
    name: str
    def rerank(self, query: str, items: Sequence[ScoredChunk], *, hints: RerankHints | None = None,
               mode: Literal["on","auto","off"] = "auto", candidates: int = 20,
               extra_terms: str = "", multi_hop: bool = False,
               second_round: bool = False) -> RerankResult: ...

class ScoreModel(Protocol):            # Reranker 的底层模型能力（可独立替换）
    model: str
    def scores(self, query: str, documents: Sequence[str], timeout: float) -> list[float]: ...
```
默认实现：`L1L2Reranker(provider: ScoreModel | None, candidates=20, timeout_ms=800)`。

### 2.6 Chunker

```python
@dataclass(frozen=True)
class ChunkRule:
    mode: Literal["article","parent_child","fixed","faq"] = "article"
    size: int = 700
    overlap: int = 120                 # 强制 <= size // 2
    def signature(self, cleaner_version: str = "") -> str: ...   # sha1(f"{mode}|{size}|{overlap}|{cleaner_version}|{CHUNKER_VERSION}")[:12]

@dataclass
class ChunkResult:
    chunks: list[Chunk]
    parents: list[tuple[str, str]]     # (parent_id, 正文)，parent_child 模式回填用

class Chunker(Protocol):
    name: str
    def chunk(self, doc: Document, rule: ChunkRule, *, id_tag: str = "") -> ChunkResult: ...
```

### 2.7 Milvus 实现约束（`adapters/milvus_store.py`）

单集合装全部知识库，`kb_id` 为**分区键**（源实现如此，按库过滤直接裁剪分区）：

| 字段 | 类型 | 备注 |
|---|---|---|
| `chunk_id` | VARCHAR(96) PK | `auto_id=False, enable_dynamic_field=False` |
| `kb_id` | VARCHAR(32) | `is_partition_key=True` |
| `doc_id` | VARCHAR(64) / `version_id` VARCHAR(32) | 建 INVERTED 索引 |
| `is_current` / `enabled` / `restricted` | BOOL | `is_current/enabled` 建 INVERTED |
| `doc_type` | VARCHAR(16) | |
| `source` | VARCHAR(16) | `text`/`ocr` |
| `version_date` | INT64 | `yyyymmdd` |
| `parent_id` | VARCHAR(96) | 父块 ID（回填正文用） |
| `title` | VARCHAR(512) / `breadcrumb` VARCHAR(1024) | 按**字节**截断（中文 3 字节，留 16 字节余量） |
| `text` | VARCHAR(8192) | `enable_analyzer=True, analyzer_params={"type":"chinese"}`，**BM25 输入** |
| `content` | VARCHAR(8192) | 展示正文（送模型） |
| `meta` | JSON | `{ordinal, uri, source_type, department, version, updated_at}` |
| `dense` | FLOAT_VECTOR(dim) | HNSW + COSINE，`M=16, efConstruction=200` |
| `sparse` | SPARSE_FLOAT_VECTOR | 由 `FunctionType.BM25` 从 `text` 自动生成，**写入不得提供** |

索引：`sparse` 用 `SPARSE_INVERTED_INDEX` + `metric_type="BM25"` + `bm25_k1=1.2, bm25_b=0.75, inverted_index_algo="DAAT_MAXSCORE"`。

过滤表达式（`SearchFilters.expr()`，字符串两侧引号/反斜杠需剥离）：
```
enabled == true
[and kb_id in ["a","b"]]                 # kb_ids 非 None；空集写 in [""]（匹配不到）
[and version_date <= 20260101]           # as_of 给定时
[and is_current == true]                 # 仅当 as_of 为 None 且 current_only
[and doc_type in ["policy","sop"]]
```
`restricted` **不进程表达式**（避免 `allow_restricted` 过长导致表达式爆炸），在 `match()` 里兜底。

运维约束（源实现踩过，照做）：
- `load_collection` 必须带超时（60s），否则段损坏会让启动/请求永久卡死；加载失败立即熔断 30s。
- 集合加载用单飞锁；**检索路径 `wait=False`**，别人正在加载就直接降级返回空，不让并发提问一起排队。
- 连续失败 5 次熔断 30s（期间检索返回空、写入抛错）。
- `count()` 用 `query(filter, output_fields=["count(*)"])`，**不要用** `get_collection_stats`（只统计已 flush 段）。
- 全量遍历用 `query_iterator`（单次查询上限 16384）。
- 一致性：检索 `Bounded`，`strong=True` 时 `Strong`。

### 2.8 零外部依赖的内存实现（`adapters/memory_store.py`）

要求：**只用标准库**（`math/re/hashlib/json`），供本地 demo 与单测；与 Milvus 实现必须能跑同一套 conformance test。

```python
class MemoryVectorStore:
    name = "memory"
    def __init__(self, dimension: int = 1024, *, persist_path: Path | None = None,
                 stopwords: frozenset[str] | None = None): ...
    # 内部：self._rows: dict[str, _Row]   _Row(chunk, dense: list[float], search_text, tokens: list[str], flags)
    #       self._df: dict[str, int]; self._n: int; self._avgdl: float   # BM25 统计，可增量维护

    def search_dense(self, vector, filters, k):
        # 归一化后点积 == 余弦；空向量直接返回 []
        # for row in self._rows.values(): if filters.match(row.chunk): sim = dot(q, row.dense)
        # ⚠️ 必须转成与 Milvus 同一刻度：score = cosine_to_score(sim)，见 3.3
    def search_lexical(self, text, filters, k):
        # q = tokenize(text)（与索引期同一函数）；BM25(k1=1.2, b=0.75)：
        # idf(t) = ln(1 + (N - df + 0.5) / (df + 0.5));  score += idf * f*(k1+1) / (f + k1*(1-b+b*dl/avgdl))
```
实现细节：
- `upsert` 全量覆盖同 `chunk_id`，同时维护 `df/n/avgdl`（删除时 `df -= 1`，`df<=0` 时剔除）。
- `set_flags` 只改 flags 并重建 `chunk.extra`，不动向量。
- `remove` 语义与 Milvus 一致（全 None → 0）。
- 可选 `persist_path`：每行一条 JSONL（`{"chunk": ..., "dense": [...], "search_text": ...}`），启动时加载，损坏行跳过。
- `search_dense` 用纯 Python 点积：10k 块 × 1024 维约 10ms/查询，够 demo；提供 `use_numpy: bool = False` 开关。

## 3. 关键算法细节

### 3.1 切块策略与参数

```
CHUNKER_VERSION = "3"
MIN_CHUNK_CHARS = 10                 # 丢弃「第 3 页」「附则」碎片
PARENT_MAX_CHARS = 1600              # 父块上限
CHILD_SIZE, CHILD_OVERLAP = 420, 60  # parent_child 子块
CAPTION_MAX_CHARS = 60               # 表名 = 表格上一行短句
SEPARATORS = ["\n\n","\n","。","；","！","？","，"," ",""]   # 优先级从高到低
splitter(keep_separator=True, strip_whitespace=True, length_function=len)
```

流程（`chunk()` 伪代码）：
```
text = doc.content.strip(); if not text: return ChunkResult([], [])
if rule.mode == "faq":
    for (q, a) in faq_pairs(text): pieces += [(q+"\n"+a, breadcrumb=q, parent="")]
elif rule.mode == "fixed":
    pieces = [(p, "", "") for p in split_blocks(text, rule.size, splitter(rule).split_text)]
else:
    sections = split_by_headings(text) or [Section(path=[], text)]
    for sec in sections:
        if mode == "parent_child":
            for body in parent_blocks(sec.text):                  # 按空行累积到 ≤1600
                pid = f"{doc_id}#p{tag}.{len(parents)}"
                parents.append((pid, f"（{crumb}）{body}"))
                pieces += [(p, crumb, pid) for p in split_blocks(body, CHILD_SIZE, child_splitter)]
        else:
            pieces += [(p, crumb, "") for p in split_blocks(sec.text, rule.size, splitter)]
for (body, crumb, pid) in pieces:
    clean = body.strip()
    if len(clean) < MIN_CHUNK_CHARS: continue
    ordinal = len(chunks)
    text_out = clean if (not crumb or mode == "faq") else f"（{crumb}）{clean}"    # 面包屑织入正文
    chunk_id = f"{doc_id}#{tag}.{ordinal}"  (tag 为空时 f"{doc_id}#{ordinal}")
```

要点：
- **面包屑必须织进正文**：LangChain 的 `MarkdownHeaderTextSplitter` 只写 metadata，而 BM25 只看正文，`（4. 请假管理 > 4.2 审批权限）` 前缀能让「审批权限」这类只在标题出现的问法召回得到。
- 表格保护：连续 ≥2 行含 `" | "` 视为表格；超长按**行组**切，每块重复表名 + 表头；拆成 ≥2 块时**额外生成一个表摘要块**（`表格摘要：{caption}，共 N 行，列：a、b、c`）；表名从正文块里移走，别重复。
- 标题解析用代码围栏外的 `^#{1,6} `；`stack` 按 level 剪枝得到 `breadcrumb = " > ".join(path)`。

### 3.2 RRF 融合与权重

```
score(d) = Σ_r  w_r / (k + rank_r(d))          k = retrieval.fusion_k = 60
```

- 单路（`HybridRetriever`）：`w_dense = vector_weight = 0.6`，`w_bm25 = bm25_weight = 0.4`，各取 `vector_k=20 / bm25_k=20`。
- 多路（`MultiQueryRetriever`）：每路权重 = `subquery.weight × route_weight`，其中
  - `route_weight = (0.6, 0.4)`；
  - **关键词型问题**（命中 `[A-Za-z]{2,}|\d{3,}|[￥$]\s*\d|\d+(\.\d+)?\s*(元|万|%)|[「“"].+?[」”"]`）改 `(0.4, 0.6)`；
  - `bm25_only=True` 的子查询只走词法，权重 `q.weight × 1.0`；`kind="hyde"` 的子查询**不走向量**（HyDE 文本只用于词法）。
- `w <= 0` 的路跳过；`rank` 从 1 开始；同 `chunk_id` 累加；`registry.setdefault` 保留首次出现的 chunk 对象。
- 融合后把 `vector_score = max(该 chunk 在所有稠密路的分数)`、`bm25_score = max(...)` 挂回（调试与闸门用）。
- 结果按 `round(score, 6)` 降序。

### 3.3 稠密分数刻度（必须先钉死）

```
to_score(sq_l2)      = 1 / (1 + max(0, sq_l2))
cosine_to_score(cos) = to_score(2 - 2*cos) = 1 / (3 - 2*cos)
```
- 归一化向量下 `‖a-b‖² = 2-2cos`，两者等价，统一到**同一刻度**才能跨实现比较。
- ⚠️ 阈值语义：`config.knowledge_grounding_min_score = 0.48` 是按**原始余弦**标定的（相关最低 0.5392 / 无关最高 0.4749）。若 `vector_score` 已是 `1/(3-2cos)`，则 0.4749 → 0.4877 > 0.48，出现误命中。
  **建议**：`ScoredChunk.vector_score` 统一存 **`1/(3-2cos)`**（与 `to_score` 一致），阈值改为在该刻度上重新标定，并写一个 conformance test：
  `assert abs(cosine_to_score(0.5392) - 0.5204) < 1e-3`；`memory_store` 与 `milvus_store` 对同一组成对向量必须返回相同的 `vector_score`（±1e-6）。

### 3.4 重排：L1 因子与 L2 触发

L1（零成本、**始终执行**，在融合分上做**乘法**）：
```
tokens = {t for t in tokenize(query) if len(t) >= 2} or set(tokenize(query))
factor  = 1 + 0.3 * |tokens ∩ token_set(chunk.text)| / |tokens|
factor *= 1.15   if any(t in (chunk.title + " " + breadcrumb) for t in tokens)
factor *= 0.70   if chunk.extra.is_current is False and not hints.history
factor *= 1.10   if hints.categories and chunk.doc_type in hints.categories
factor *= 1.20   if chunk.doc_id in hints.boost_docs
factor *= 0.80   if len(chunk.text.strip()) < 40
score = round(score * factor, 6)；按 score 降序
```
L2 触发（`mode`：`on` 总是 / `off` 从不 / `auto` 看条件）：
```
if mode == "on": True
if mode != "auto" or len(items) < 2: False
if multi_hop or second_round: True
top, fifth = items[0].score, items[min(4, len(items)-1)].score
return top > 0 and (top - fifth) / top < 0.15        # 前五名挤在一起 → 交给模型
```
L2 合成：只重排前 `candidates` 个（管线 150 / 重排器默认 20），送 `chunk.text[:1500]`，`timeout=0.8s`。
```
merged_score = 0.7 * model_score + 0.3 * (l1_score / max(l1_scores))
```
失败/超时 → 返回 L1（`degraded=True`）；连续失败 10 次熔断 300s。
`extra_terms`（术语表书面说法）**只进 L1 的词面计算**，模型重排仍只用原问。

### 3.5 grounding 判定（闸门）

```
grade(question, chunks, gate) -> "hit" | "fuzzy" | "miss"

if not chunks or looks_like_chitchat(question): return MISS
score, mode = grounding_conf(chunks, rerank=gate.rerank_high is not None)
   # mode 优先级：任一 rerank_score → "rerank"; 否则 vector_score → "vector"; 否则 bm25 → "bm25"; 否则 "none"
if mode == "bm25":  return HIT if score > 0 and has_lexical_overlap(question, chunks) else MISS
if mode == "none":  return MISS
high, low = (rerank_high, rerank_low) if mode == "rerank" else (gate.high, gate.low)
low = high if low is None else min(low, high)
if score < low or not has_lexical_overlap(question, chunks): return MISS
return HIT if score >= high else FUZZY
```

- `grounding_conf` 取**各片段对应分数字段的最大值**（不是 RRF 分——RRF 分只反映名次，跨问题不可比）。
- `looks_like_chitchat`：去标点后**精确匹配**短名单（「你好，请问年假有几天」不算闲聊）。
- `has_lexical_overlap(question, chunks, top_n=3)`：
  `q = tokenize(question) - GENERIC_TERMS`（全被减光时用原词集）；分词切偏时补二字片段 `grams = {t[i:i+2] for t in q if len(t)>2}`；
  任一前 3 片段满足 `q ∩ token_set(text)` 非空，或某 gram 作为子串出现在原文 → True。
- 阈值来源：`Gate(high, low, rerank_high, rerank_low)`，未校准时 `low = high`（退化单阈值）；默认 `high = 0.48`（刻度见 3.3）。
- `FUZZY` 档仍作答，但首句声明「按 X 理解」，数字必须有出处。

### 3.6 引用编号与去重（`rag/citations.py`）

不变式：**`format_context` 里的 `[n]` = `Citation.index` = 模型该写的编号**。

1. `build_citations(chunks)`：`index` 从 1 连续、`snippet = text[:160]`、`label = doc_label()`。
2. `tidy_refs(answer, doc_of)`（逐段编号阶段）：
   - 否定句（`NEGATIVE` 正则：未提及/没有找到/无法确认…）**去掉引用**（没有的东西不该有出处）；
   - 一句话最多 `MAX_REFS_PER_SENTENCE = 2` 篇文档的引用（按出现顺序保留；同文档多段都留）；
   - 「引用文档 / 可以联系」之后的版式原样保留。
3. `renumber(answer, citations, texts, pages)`：逐段编号 → **逐文档编号**
   - 文档顺序 = 正文里首次出现的顺序；未出现在正文的排在最后；
   - 同一文档的多个段号映射到同一新号；`[1][1]` → `[1]`（`_DUP` 合并）；
   - 面包屑里有「第 N 页」时 `[n → [新号·第N页]`；
   - 返回 `(新正文, 每文档一条 Citation, 新号 → 该文档被引用各段原文)`。
4. `merge_pages`：`[2·第2页][2·第37页]` → `[2·第2、37页]`；循环直到收敛。
5. `related(cited, ranked, limit=5)`：正文引用不足 5 篇时按检索排名补，补上的标 `cited=False`；**按 `doc_id`/`title` 去重**（同名多库/新旧版只列一次）。
6. `unique_sources`：同文档只留一条，`limit = max(limit, cited 数)`（正文引用了的必须全列）。
7. `source_line`：`[《标题》](url) · 目录：X · 版本：v2 · 更新：2026-10-14`（无链接则纯文本）。
8. `pack_context(numbered, parents, budget_tokens=3000)`：预算按 `tokens / 0.7`（中文约 0.7 token/字）折算成字符；父块回填（同父块只出现一次），装不下退回子块正文；按分数降序装入。

### 3.7 多路查询与同义词扩展

```
queries = dedupe(req.queries)[:6]
  key = re.sub(r"[\s，。？！?!,.、]+", "", text).lower() + ("#bm25" if bm25_only else "")
  同 key 合并：权重相加，保留首个的 text/kind
vectors = req.vectors or {}
missing = [q for q in queries if not q.bm25_only and q.text not in vectors]   # 一次批量 embed_queries
jobs = {}   # Future -> (subquery, "dense"|"lexical", weight)
for q in queries:
    heavy = q.bm25_only or keyword_heavy(q.text)
    qvw, qbw = (0.4, 0.6) if heavy and not q.bm25_only else (vector_weight, bm25_weight)
    if use_dense and not q.bm25_only and vectors.get(q.text):
        jobs[pool.submit(store.search_dense, vectors[q.text], filters, per_query_k)] = (q, "dense", q.weight*qvw)
    if q.kind != "hyde":
        jobs[pool.submit(store.search_lexical, q.text, filters, per_query_k)] = (q, "lexical", q.weight*qbw)
done, pending = wait(jobs, timeout=deadline - monotonic()); [f.cancel() for f in pending]; partial = bool(pending)
fused = weighted_rrf([(ids, w) for f,(q,kind,w) in done], k=fusion_k)
```
- `filters` 处理：给了 `as_of` 时**先不按日期截断**（`as_of=None, current_only=False`），召回全部版本后再按文档选「当时有效的那一版」（`_effective_versions`：取 `version_date <= as_of` 的最大值；全都晚于它时留最早一版）。
- `_dedupe_versions`（无历史意图）：同文档只留 `(is_current, version_date)` 最大者。
- `_dedupe_parents`：同 `parent_id` 只留最高分，其余分数 `× 0.3` 加给它。
- `_cap_per_doc(max_per_doc, top_k)`：每文档最多 N 段，**超出的不丢**，别的文档凑不满 `top_k` 时按原顺序补。
- 线程池：`ThreadPoolExecutor(max_workers=16)`，模块级单例。
- **同义词扩展**（`TermExpander` 端口）：`expand(query) -> str`，把口语换成书面说法（「酒店一晚能报多少」→「住宿…」），作为**第二路 `SubQuery(kind="expand", weight=0.8)`** 加入；术语表为空则不扩展。术语表本身（`kb/terms.py`）属业务，**不进内核**。

### 3.8 上下文化切块（contextualize）

```
PROMPT_VERSION = "ctx1"; BATCH = 40; WHOLE_DOC_CHARS = 40_000
WINDOW_CHARS = 12_000; MAX_CONTEXT_CHARS = 160
windows: len(text) <= 40k → 每 40 块一批带全文；否则每批带「这批块首次出现位置 -3000」起 12000 字窗口
prompt: 传入 <文档 标题> + 文档窗口 + [i] 片段（各截 600 字）+ 期望长度 n，要求只输出 JSON 字符串数组
parse: 长度不足补空串；解析失败/缺项 → 该批全部空串（等同未开启）
注入：IndexRow.search_text = 《标题》版本 > 章节 \n {context} \n {正文}
     chunk.text 保持原文（展示与送模型都用原文）
```
失败不影响入库；`PROMPT_VERSION` 必须进切块产物签名（提示词一改，下次处理自动重建）。

## 4. 入库流水线

### 4.1 步骤（单文档）

```
1. discover   DocumentSource.list_documents() → SourceItem{key, name, size, mtime, source_type}
2. dedupe     查元数据：同 (kb_id, key) 且 content_hash 未变 且 pipeline_sig 未变 → 跳过
3. fetch      blob.get(key) 或本地路径 → bytes
4. parse      parser.parse_bytes(bytes, name) → ParsedDocument{text,title,doc_type,warnings,loader,pages,ocr}
                编码探测 utf-8 → utf-8-sig → gbk → gb18030；失败回退 utf-8(errors="replace")
                扫描件页（文本 < 50 字/页）→ OCR（可选，有配额与令牌桶）
5. clean      clean_text(text, title) → (text, report{removed, suspicious})，RULES_VERSION 进签名
6. chunk      chunker.chunk(doc, rule, id_tag=version_tag) → chunks + parents
7. context    （可选）contextualizer(title, text, [c.text]) → 每块 1-2 句
8. embed      embed_for_index([search_text...])     ← 严格，失败抛错（见 4.4）
9. materialize chunks[i].extra = {kb_id, version_id, is_current:True, enabled, restricted,
                                  version_date:int, parent_id, source, breadcrumb}
10. index     store.upsert([IndexRow(chunk, vec, search_text)])     ← 先写新
11. retire    旧版本：keep_history ? set_flags(is_current=False) : remove(version_id=...)
12. meta      元数据落库：doc{current_version_id, content_hash}、version{pipeline_sig, status}
```

### 4.2 幂等与增量

- `doc_id` 必须**稳定**：`sha1(相对路径 | 来源 key)[:16]`（源实现 `parser.build_document:831`）。
- `chunk_id = f"{doc_id}#{version_tag}.{ordinal}"`，`version_tag` 让新旧版本共存于同一索引。
- 跳过判据 = `content_hash 相同` **且** `pipeline_sig 相同`。`pipeline_sig` 由
  `ChunkRule.signature(cleaner_version) + options_tag（contextual PROMPT_VERSION、imageText）+ embedding_model + dimension` 拼成 —— 任一变化自动重建。
- 写顺序：**先 upsert 新切块，再删/改旧版本**（不出现检索空窗）；同文档旧切块差集 `set(old_ids) - set(new_ids)` 用 `remove(chunk_ids=...)`。
- 源里消失的文档：**只有全部来源都是「本地目录」时才 prune**（钉钉/上传「本次没看到」≠「已删除」）。

### 4.3 失败降级（关键，别写成源实现那样自相矛盾）

| 场景 | 行为 |
|---|---|
| 解析失败 | 单文档 `failed` + `ParseError.code`，不中断整批；`TaskError.retryable` 仅对 `convert_failed/ocr_failed/ocr_budget` 为 True |
| 解析为空 | 跳过，记 reason「解析后无有效文本」 |
| OCR 未配置 | 图片类标记 `ocr_unavailable`，跳过该文件（不是失败） |
| **向量化失败（索引期）** | **让任务失败并按退避重试**；绝不写伪向量（写入等于永久污染检索） |
| `embedding.provider = "pseudo"` | 允许写伪向量（`semantic=False`），检索层自动关闭稠密通路 |
| 查询向量化失败 | 逐条回退伪向量（**不缓存**），检索仍跑 BM25 |
| Milvus 写失败 | 抛错 → 任务重试；检索失败 → 返回空、上层走「知识库暂无」话术 |
| 元数据落库失败 | 记日志，不阻断在线检索（索引已是真相） |

### 4.4 并发与重试

- `JobQueue` 端口（源 `kbsync/queue.py` 语义）：
  - 领取 `FOR UPDATE SKIP LOCKED`，`ORDER BY priority DESC, id`，`LIMIT n`；
  - `running` 超 **10 分钟**无心跳视为僵死可重领；
  - 失败退避 `(30, 120, 480)` 秒，`max_attempts=3`，超限置 `dead`；
  - 入队去重：同 `(target, stage)` 已有 pending/running 则不重复入队。
- 阶段化：`download`（已有暂存空间检查/背压）→ `process`（解析+切块+向量化，产物缓存到 BlobStore）→ `index`（写索引，纯 upsert/flags/delete，可重放）。
- 批内并发：解析/OCR 单文档串行（外部 API 有限流），跨文档用 job worker 并行（默认 2~4 worker）；检索侧 `max_workers=16`。
- 大文档：按 `BATCH=40` 块分批调用上下文与向量化，避免单次超限。

### 4.5 配置项替换表

| 原配置（钉钉/公司） | 新配置（通用） | 默认 |
|---|---|---|
| `KNOWLEDGE_SOURCES=local,dingtalk` | `ingest.sources: [{type:"local",root:...},{type:"upload"}]` | `[{"type":"upload"}]` |
| `DASHSCOPE_API_KEY` | `embedding.api_key` / `rerank.api_key`（可同源） | 空 → pseudo |
| `EMBEDDING_ENABLED/PROVIDER/MODEL/DIMENSION/BATCH_SIZE` | `embedding.{enabled,provider,model,dimension,batch_size}` | `true/dashscope/text-embedding-v3/1024/10` |
| `MILVUS_URI/TOKEN/COLLECTION/TIMEOUT` | `vector_store.{backend,url,token,collection,dimension,timeout_s}` | `memory`（本地）/`http://127.0.0.1:19530` |
| `CHUNK_SIZE/CHUNK_OVERLAP` | `chunk.{size,overlap,mode}` | `700/120/article` |
| `RETRIEVAL_TOP_K/VECTOR_K/BM25_K/FUSION_K/*_WEIGHT/MIN_SCORE` | `retrieval.{top_k,recall_k,fusion_k,vector_weight,bm25_weight,min_score}` | `8/100/60/0.6/0.4/0.0` |
| `RERANK_ENABLED/RERANK_MAX_CHUNKS` | `rerank.{mode,candidates,timeout_ms,max_per_doc}` | `auto/150/800/5` |
| `KNOWLEDGE_GROUNDING_MIN_SCORE=0.48` | `grounding.{high,low,rerank_high,rerank_low,generic_terms}` | `0.48/None/None/None/默认表` |
| `INDEX_PATH`（清单 JSON） | `metadata.dsn`（sqlite:///data/rag.db） | 同左 |
| `PUBLIC_BASE_URL` / 钉盘链接 | `links.mode`（none/signed）+ `CitationLinker` | `none` |
| `max_upload_mb` | `ingest.max_upload_mb` | 32 |
| OCR 月度配额 / Redis | `ocr.{enabled,model,monthly_pages,qps,provider}` | `false/-/3000/2.0/-` |

## 5. 对外 API 建议（REST，`/v1`，所有写操作幂等键可选）

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/v1/kbs` | 建库 `{name, description?, chunk_rule?}` → `{kb_id, ...}` |
| `GET` | `/v1/kbs` | 列表（含 `document_count/chunk_count/enabled`） |
| `PATCH` | `/v1/kbs/{kb_id}` | 改 `name/enabled/chunk_rule/keep_history`；`chunk_rule` 变更触发后台重建 |
| `DELETE` | `/v1/kbs/{kb_id}?purge=true` | 删库（`purge=false` 仅停用） |
| `POST` | `/v1/kbs/{kb_id}/documents` | `multipart/form-data`：`file` + `doc_type/department/version/tags` → `202 {doc_id, version_id, job_id}` |
| `GET` | `/v1/kbs/{kb_id}/documents?cursor=&limit=50&status=` | 文档列表 `{total, next_cursor, documents:[{doc_id,title,uri,doc_type,version,chunk_count,content_hash,status,ingested_at,error}]}` |
| `GET` | `/v1/documents/{doc_id}` | 文档详情 + 版本列表 + 切块预览（`?chunks=1&limit=20`） |
| `DELETE` | `/v1/documents/{doc_id}` | 删文档（异步 `{job_id}`） |
| `POST` | `/v1/kbs/{kb_id}/reindex` | `{force?: bool, scope?: "all"|"changed", doc_ids?: []}` → `{job_id}` |
| `GET` | `/v1/kbs/{kb_id}/stats` | `KnowledgeStats` |
| `GET` | `/v1/jobs/{job_id}` | `{id, stage, status: pending/running/done/dead, attempts, error, progress:{docs,chunks}}` |
| `POST` | `/v1/search` | 检索调试（唯一读接口） |
| `GET` | `/v1/health` | `{status, vector_store:{connected,entities}, embedding:{enabled,model,dimension}, degraded:[]}` |

`POST /v1/search` 请求 / 响应：

```jsonc
// 请求
{ "query": "年假有几天", "queries": ["带薪年休假天数"],   // 可选额外路，max 4
  "kb_ids": ["kb_1"], "doc_types": ["policy"], "as_of": 20260101,
  "top_k": 8, "max_per_doc": 5, "rerank": "auto", "debug": true }
// 响应
{ "query": "...", "grade": "hit", "confidence": 0.62, "confidence_source": "rerank",  // rerank|vector|bm25|none
  "partial": false, "rerankLevel": "L2", "degraded": [],
  "timings": {"embed": 12, "search": 38, "rerank": 90, "total": 145},
  "perQuery": {"original:年假有几天": 20, "expand:带薪年休假天数": 11},
  "items": [{ "chunkId": "...", "docId": "...", "kbId": "kb_1", "title": "...",
              "breadcrumb": "4. 请假管理 > 4.2 审批权限", "version": "v2", "versionId": "...",
              "score": 0.0312, "vectorScore": 0.5431, "bm25Score": 12.4, "rerankScore": 0.88,
              "text": "（4. 请假管理 > …）…" }],
  "citations": [{ "index": 1, "docId": "...", "title": "...", "snippet": "...", "cited": true }] }
```
约定：`/v1/search` 不生成答案，但**必须返回 `grade`**（chat 内核据此决定是否走通用模型）；`debug=false` 时不返回 `vectorScore/bm25Score/perQuery`。

## 6. 依赖与风险

### 6.1 与 chat 内核的接口（三个函数，别多）

```python
# 1) 检索工具（agent tool / 单轮直调共用）
def search(question: str, filters: SearchFilters, *, top_k: int | None = None,
           hints: RerankHints | None = None) -> SearchResult
# SearchResult{ items: list[ScoredChunk], grade: "hit"|"fuzzy"|"miss", confidence: float,
#               confidence_source: str, numbered_context: str, citations: list[Citation],
#               partial: bool, degraded: list[str], timings: dict[str,int] }

# 2) 证据判断（可单独调，用于「要不要调用工具」之外的事后判定）
def grade(question: str, items: Sequence[ScoredChunk], gate: Gate | float) -> str

# 3) 引用拼装（生成之后）
def finalize(answer: str, numbered: list[tuple[int, ScoredChunk]], *,
             snippet_length: int = 160, max_sources: int = 5,
             linker: CitationLinker | None = None, ctx: LinkContext | None = None
             ) -> tuple[str, list[Citation]]      # tidy_refs → renumber → merge_pages → related
```

chat 内核负责：提示词拼装、LLM 调用、`ground_check` 触发时机、无命中话术、渠道渲染。
RAG 内核负责：编号语义、`grounded` 判定、`Citation` 结构与去重。

### 6.2 必须先由 Lead 定义的契约（阻塞项）

1. **领域模型归属**：`Chunk/ScoredChunk/Citation` 放 `tsukiyohub.contracts`（独立包）还是 RAG 包内被 chat 依赖？建议独立 `contracts` 包，双方只依赖它。
2. **分数刻度与阈值语义**：`vector_score` 存原始余弦还是 `1/(3-2cos)`；阈值 0.48 在哪个刻度上标定（见 3.3 ⚠️）。
3. **`SearchFilters.kb_ids` 的三态**：`None` = 不限、`frozenset()` = 空集、非空集 = 白名单。这个语义必须写进契约文档，否则一次误用就是越权或全空。
4. **`restricted/allow_restricted` 是否保留**：通用化后建议改名为 `acl_doc_ids`，或者干脆删除、由调用方在 `kb_ids` 里表达（更简单，但失去「文档级保密」能力）。
5. **同步 vs 异步**：源实现是同步 + 线程池；FastAPI 侧用 `anyio.to_thread.run_sync` 包。建议内核保持**全同步**（简单、可测），API 层负责线程化。
6. **引用编号权责**：内核输出「逐段编号 + 文档级 `Citation[]`」，chat 只做渲染 —— 还是 chat 自己 renumber？建议前者（源实现已是前者）。
7. **降级契约**：`degraded: list[str]`（`embedding_disabled` / `vector_store_down` / `rerank_failed` / `partial_search`）由内核出，chat 决定话术。
8. **多租户**：内核不认用户，只认 `SearchFilters`；`kb_id` 由调用方传。是否要内置 `tenant_id` 前缀需 Lead 定。

### 6.3 风险清单

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | 索引期伪向量污染（源实现既有「严格」注释又降级写入） | 检索质量静默劣化，极难发现 | 索引严格、只允许 `semantic=False` 时写伪向量；加启动自检「索引向量与 provider 维度/模型不一致则告警」 |
| R2 | 分数刻度不一致（3.3） | 闸门误命中/漏命中 | conformance test + 用同一刻度重标阈值 |
| R3 | Milvus 与内存实现行为漂移 | 本地 demo 通过、生产错 | 同一套 `conformance_test`（upsert/remove/set_flags/filters/两路召回/刻度）跑两个实现 |
| R4 | 切块参数变更未触发重建 | 新旧切块混在同一索引 | `pipeline_sig` 必须含 `ChunkRule.signature + cleaner_version + CHUNKER_VERSION + contextual PROMPT_VERSION + embedding model/dim` |
| R5 | 先删后写导致检索空窗 | 用户提问命中 0 条 | 固定「先 upsert 新、后删/改旧」 |
| R6 | `as_of` 历史检索的版本选择歧义 | 答错版本 | 保留 `_effective_versions` 的两条规则（≤as_of 取最大；全晚于则取最早）并写单测 |
| R7 | 引用编号与正文不一致（`[3]` 但只列 2 条） | 用户看到假引用 | `unique_sources(limit=max(limit, cited 数))` + `has_refs` 断言 |
| R8 | `ground_check` 误删（模型把「**总结**」判为无依据） | 答案被削 | 保留 `_framing` 豁免 + `usable` 检查（删完无引用则回退原答案） |
| R9 | 语义缓存误命中 | 答非所问 | 阈值 0.95、作用域一致、不个性化、引用版本变化即失效、👎 即删；默认关闭 |
| R10 | OCR/vision 成本失控 | 账单 | 月度页数配额 + 令牌桶 + sha256 结果缓存 + 默认关闭 |
| R11 | 依赖过重（LangChain 全家桶） | 抽取后仍难装 | 只保留 `langchain-text-splitters`（切块）；`Embeddings` 继承去掉；`DocumentLoader` 可选用 |
| R12 | 解析器覆盖面（老格式需 LibreOffice） | 部分文件无法入库 | `parser.can_parse()` 前置判断 + `ParseError.code="convert_failed"` 明确提示 |

### 6.4 建议实现顺序

| 里程碑 | 内容 | 验收 |
|---|---|---|
| M0（1~2 天） | `contracts.py` + 端口 Protocol + `MemoryVectorStore` + `PseudoEmbedding` + `tokenize` + `chunker` + `parser`（md/txt/pdf/docx） | 无外部依赖跑通「解析→切块→内存索引→检索」，并有 conformance 骨架 |
| M1（2~3 天） | `MilvusStore`（字段/索引/过滤/熔断）+ `EmbeddingProvider`（dashscope + fallback）+ `embed_cache`（SQLite KvStore）+ 入库流水线 + `JobQueue` | 10 篇文档增量入库；改文件只重建变更文档；杀掉 Milvus 时检索降级不 500 |
| M2（2~3 天） | 融合 + L1/L2 重排 + `grounding` + `citations` + `/v1/search` | 检索调试接口给出 grade/confidence/分数拆解；引用编号与正文一一对应 |
| M3（2 天） | `contextualize` + `multi_query` 完整链路（多路/截止时间/父块回填） | `search_text` 含上下文；父块回填生效；`partial=True` 可复现 |
| M4（1~2 天） | `semantic_cache` + `ground_check` + 指标 + `/v1/kbs/{id}/stats` | 缓存命中率/误命中抽检页；核对删除可复现 |