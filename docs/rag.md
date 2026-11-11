# RAG 内核（tsukiyohub.kernel.rag）

与业务无关的「解析 → 切块 → 向量化 → 混合检索 → 证据闸门 → 逐句核对」链路。
任何前端（Web / 桌面 / IDE / IM / Agent 平台）都通过 HTTP 接入，内核本身不认渠道、不认用户。

设计上的三条硬约束（改代码前先读）：

1. **无外部依赖可跑**：`vector_backend=memory` + `embedding_provider=hash` + 不联网，
   入库 → 检索 → 证据闸门全链路可用（hash 是演示模式，检索走 BM25 + 关键词闸门；
   `tests/test_rag_*.py` 覆盖）。
2. **降级不阻断**：向量化关闭 → 纯关键词；检索后端失败 → 空结果 + 上层话术；重排失败 → 保留融合顺序。
   任何一环失败都只是**质量下降**，不是 500。
3. **索引期严格、查询期宽松**：入库时向量化失败要重试、仍失败就整篇返回 `error`（绝不写伪向量污染索引）；
   查询时失败才回退伪向量。这个不对称是整个降级设计的要点。

## 1. 模块与职责

| 文件 | 职责 | 关键点 |
|---|---|---|
| `tokenize.py` | 中文分词 | jieba 懒加载 + 预热 + 二元组兜底；`add_words()` 登记行业词 |
| `chunker.py` | 文本切块 | 递归按段落/句子边界切、标题面包屑织入正文、表格保护 |
| `parser.py` | 文档解析 | md/txt/json/csv/html 零依赖；pdf/docx/xlsx/pptx 缺失依赖给中文错误 |
| `embeddings.py` | 向量化缓存 | 磁盘缓存 + 索引严格 / 查询宽松 + 伪向量兜底 |
| `fusion.py` | 融合与轻量重排 | 加权 RRF、L1 词面加权、去冗余工具、本地过滤兜底 |
| `retriever.py` | 检索编排 | 一次 `backend.search` + 过滤 + 可选 L2 重排 + 每文档上限 |
| `store.py` | 知识库门面 | 幂等入库、先写后删、失败语义、清单持久化 |
| `prompt.py` | 提示词素材 | 无命中话术、生成约束规则、参考资料渲染、父块回填打包 |
| `answer.py` | 答案素材 | 来源行、编号证据块、兜底原文摘录、进度文案 |
| `grounding.py` | 证据闸门 | 原始余弦 / 重排分 / BM25 三档 + 闲聊 + 词面交集 |
| `ground_check.py` | 逐句核对 | `complete` 注入，strict 删句 / lenient 只记录 / auto 数字触发 |
| `multi_query.py` | 多路检索（可选） | 多说法并行召回 + 加权 RRF，默认不启用 |
| `semantic_cache.py` | 语义缓存（可选） | 默认关闭，阈值 0.95，按作用域隔离 |

端口（`server/tsukiyohub/ports/`）：`VectorStore` / `EmbeddingProvider` / `Reranker`。
内核只依赖这些协议，换后端不改内核。

## 2. 检索链路

```
问题
 ├─ 1. 查询向量化   CachedEmbeddings.embed_for_query（失败→伪向量，不缓存）
 ├─ 2. 混合召回     VectorStore.search(vector, query_text, top_k=recall, filters)
 │                   memory：余弦 + BM25 → 加权 RRF（k=60，0.6/0.4）
 │                   milvus：HNSW + COSINE（supports_hybrid=False）
 ├─ 3. 本地过滤     chunk_matches（doc_types / doc_ids / tags / active_only）
 ├─ 4. L1 词面加权  仅当后端不做词法融合时（纯稠密后端）
 ├─ 5. L2 模型重排  可选，rerank_enabled 才启用，失败保留原顺序
 ├─ 6. 阈值与配额   min_score 过滤 → 每文档上限 → top_k
 └─ 7. 证据闸门     evaluate_grounding(hits, grounding_min_score, question=...)
```

**分数刻度（契约）**

* `ScoredChunk.score`：融合分（RRF），只反映名次，**跨问题不可比**，不要拿它跟阈值比；
* `ScoredChunk.vector_score`：**原始余弦**（不是 `1/(3-2cos)`），跨问题可比，闸门用它；
* `ScoredChunk.bm25_score`：BM25 原始分（无上界），只有「> 0」有语义；
* `ScoredChunk.rerank_score`：模型重排分 0~1，跨问题可比，存在时优先级最高。

`grounding_min_score`（默认 `0.35`）是**通用初值**：阈值与 embedding 模型绑定，换模型必须重新标定，
不同模型的余弦分布差异很大（同一份语料，相关对可能从 0.5 漂到 0.3）。

`embedding_provider=hash` 是**零依赖演示模式**，工厂 `build_embeddings` 对它返回
`enabled=False`：**不产生语义向量，检索只走关键词（BM25）**，闸门自动落到 BM25 分支
（分数 > 0 且问题实词有交集）。原因是实测过：同一批文档相关问题哈希余弦 0.30~0.51、
无关问题（量子计算机制冷原理）0.298，分布完全重叠，任何阈值都不可标定——
会让「知识库里没有」被判成「有」。想验证哈希向量本身（确定性 / 词面相似）时，
测试里显式构造 `HashEmbeddingProvider(..., enabled=True)` 即可，但**不要**拿它标定生产阈值。

## 3. 参数（都在 `Settings`）

| 参数 | 默认 | 说明 / 调参建议 |
|---|---|---|
| `chunk_size` / `chunk_overlap` | 700 / 120 | 制度类长文档 700~900 合适；问答型短文档降到 300~400 |
| `retrieval_top_k` | 8 | 送进证据块的片段数；越大越全但越吵 |
| `retrieval_vector_k` / `retrieval_bm25_k` | 20 / 20 | 两路各召回多少；后端融合用的候选池 |
| `retrieval_fusion_k` | 60 | RRF 平滑常数，越大越平权 |
| `retrieval_vector_weight` / `retrieval_bm25_weight` | 0.6 / 0.4 | 权重；编号/型号类问题可以把词法调高 |
| `retrieval_min_score` | 0.0 | 融合分下限，默认关闭（融合分尺度不适合当阈值） |
| `rerank_enabled` / `rerank_max_chunks` | False / 4 | L2 重排开关；开启后同时限制每文档片段数 |
| `grounding_min_score` | 0.35 | 证据闸门余弦阈值（**需按语料标定**） |
| `answer_judge` | strict | 逐句核对：strict 删句 / lenient 只记录 / off 关闭 |
| `embedding_*` | openai / 1536 | `hash` = 演示模式（`enabled=False`，只走关键词）；`dimension` 必须与后端一致 |
| `vector_backend` | memory | memory = 零依赖本地；milvus = 生产 |

## 4. 落盘格式（`<data>/index/`）

| 文件 | 内容 | 容错 |
|---|---|---|
| `kb_index_docs.json` | 清单：`version` / `updated_at` / `last_ingested_at` / `backend` / `embedding_*` / `documents[]` | 版本过高告警但尽力读；坏记录跳过 |
| `kb_index_chunks.jsonl` | 每行一个 `Chunk`（含正文与 extra） | 坏行跳过，其余照常恢复 |
| `kb_index_vectors.jsonl` | 内存后端的向量副本（`{"version", "chunk", "dense"}`） | 坏行 / 版本不符跳过 |
| `embed_cache.jsonl` | 向量化缓存（键 = `sha256(provider\|model\|dim\|text)`） | 坏行跳过；换模型自动失效 |

`KnowledgeStore.load()` 的行为：

1. 读清单 + 切块副本；
2. 后端为空（换后端 / 向量文件丢失）时**按切块重建索引**（走向量缓存，通常不产生外部调用）；
3. 后端读失败不回滚清单——清单是元数据真相，Milvus 丢了也能据此重建。

写顺序固定「先 upsert 新切块、再删旧切块」，避免重建瞬间的检索空窗。

## 5. 防幻觉（三段）

1. **检索后闸门** `evaluate_grounding`：命中 / 未命中两态。判定优先级
   `rerank_score → vector_score(余弦) → bm25_score`，再加两条否决：
   闲聊短语（「你好」余弦很高也不作答）、问题实词一个都没出现（泛化相似不是命中）。
2. **生成约束** `prompt.grounding_block()`：逐条写进系统提示词的「有据可查」规则 + 禁用词表。
3. **逐句核对** `ground_check.check(answer, evidence, complete, mode=..., question=...)`：
   用一次温度 0 的模型调用判每句是否有出处；`strict` 直接删无依据句，删完没有引用内容时
   保留原答案（`usable=False`）；解析失败 / 超时按原答案返回并记录 `error`。

一次完整调用示例：

```python
hits = store.retrieve("年假由谁审批", top_k=6)
gate = evaluate_grounding(hits, settings.grounding_min_score, question="年假由谁审批")
if gate.grounded:
    evidence = build_evidence_block(hits)          # 已编号 [1]..[n]
    answer = llm(...)                              # chat 内核负责调用模型
    checked = check(answer, [(i, h.chunk.text) for i, h in enumerate(hits, 1)],
                    complete, mode=settings.answer_judge, question="年假由谁审批")
```

## 6. 换后端

### 6.1 memory → milvus

```python
container.vector = build_milvus_store(settings)   # 换这一行即可，其它不动
```

约束：

* `MilvusVectorStore` **懒加载** `pymilvus`：构造不连接，首次使用才导入；未安装 / 连不上时
  抛 `MilvusUnavailable`（中文原因 + `pip install pymilvus` 建议），不影响其它后端；
* 当前实现只做稠密召回（`supports_hybrid=False`），词法信号由 `HybridRetriever` 的 L1 词面重排补；
* 升级成原生混合检索需要：`text` 字段（`enable_analyzer=True`）+ `sparse` 字段 +
  `FunctionType.BM25` + `SPARSE_INVERTED_INDEX(metric_type="BM25")` + `hybrid_search`，
  然后把 `supports_hybrid` 改成 `True`（详见 `adapters/vector_milvus.py` 模块 docstring）；
* Milvus 的 `COSINE` 返回的就是余弦，**不要**再做刻度变换。

### 6.2 自研后端（ES / pgvector / 向量数据库）

实现 `tsukiyohub.ports.vector.VectorStore` 的五个方法即可：`upsert` / `remove` / `search` /
`count` / `get`（`persist` / `set_flags` / `clear` 可空实现）。必须满足同一套 conformance：

| 项 | 要求 |
|---|---|
| 分数 | `search` 返回的 `vector_score` 是**原始余弦**；没有稠密分时给 `None` |
| 融合 | 后端内部自己融合两路；`supports_hybrid=False` 时内核会补 L1 词面重排 |
| 过滤 | `doc_types` / `doc_ids` 下推；`tags` 与 `active_only` 允许本地兜底 |
| 删除 | 所有条件为空时 **返回 0，绝不清库** |
| 失败 | `search` 失败返回空列表；写操作抛错（由上层重试） |
| 空向量 | `vector=[]` 的行只能被关键词检索到，不能因此写坏索引 |

`tests/test_rag_retrieval.py::test_vector_score_is_raw_cosine` 就是刻度基准，新后端必须能过。

### 6.3 换向量化

`build_embeddings(settings)` 已内置三种：`openai`（任何 OpenAI 兼容 `/embeddings`）、
`dashscope`（百炼兼容模式）、`hash`（本地特征哈希，零网络）。
`hash` 是**演示模式**：工厂对它返回 `enabled=False`，检索只走关键词、闸门走 BM25 分支
（哈希余弦与语义无关，阈值不可标定，见第 2 节）。
没有密钥 / 依赖缺失时**不启动失败**，同样降级为 `hash(enabled=False)`（纯关键词检索）。

切模型要点：`embedding_model` 与 `embedding_dimension` 变了，缓存键与索引向量全部失效，
必须清 `embed_cache.jsonl`、重建索引（`/api/knowledge/reindex`），否则新旧向量混在一个空间里。

## 7. 如何评估

没有测评集就没有调参。建议三步：

**① 构造测评集**（20~100 条就够起步）：`{"question": "...", "relevant_doc_ids": ["..."], "answerable": true}`，
其中要有 20% 是**故意答不了**的问题（用来标定闸门，防止全都判命中）。

**② 跑检索指标**（只依赖内核，不需要大模型）：

```python
import json
from tsukiyohub.kernel.rag.grounding import evaluate_grounding

rows = [json.loads(line) for line in open("eval.jsonl", encoding="utf-8")]
hits_at = {5: 0, 8: 0}
grounded_ok = 0
for row in rows:
    hits = store.retrieve(row["question"], top_k=8)
    docs = [hit.chunk.doc_id for hit in hits]
    for k in hits_at:
        hits_at[k] += bool(set(docs[:k]) & set(row["relevant_doc_ids"]))
    gate = evaluate_grounding(hits, settings.grounding_min_score, question=row["question"])
    grounded_ok += gate.grounded == row["answerable"]

print("recall@5", hits_at[5] / len(rows), "recall@8", hits_at[8] / len(rows))
print("gate 准确率", grounded_ok / len(rows))
```

**③ 标定阈值**：把「能答的问题」与「答不了的问题」的最高余弦各统计一遍，
取两者之间（例如相关最低 0.52、无关最高 0.44 → 阈值 0.48 或 0.46）。
`grounding_min_score` 只在该刻度上调；换了 embedding 模型必须重标。

再往下才是生成质量：正确率（人工/模型判）、引用正确率（正文 `[n]` 是否真的支持该句）、
**幻觉率**（`ground_check` 删掉的句数 / 答案句数）。这三项建议每次改提示词都跑一遍。

## 8. 可选模块

* **多路查询**（`multi_query.py`）：口语与书面词差异大时开；子查询 ≤ 6 条，`deadline` 到了就
  丢弃未完成的路并标记 `partial=True`。默认主链路不用它。
* **语义缓存**（`semantic_cache.py`）：默认关闭。阈值 0.95、按作用域隔离、只缓存有引用的答案、
  引用文档版本变化即失效、用户 👎 可 `forget`。没标定过阈值不要开。
* **模型重排**（`rerank_dashscope.py`）：`rerank_enabled=true` + 百炼密钥才生效，
  未配置时返回 `NoopReranker`（`enabled=False`），调用方不用判空。
* **parent_child 切块**：`TextChunker.split_parent_child(document)` 返回
  `(子块, {parent_id: 父块正文})`，配合 `prompt.pack_context(numbered, parents)` 做父块回填。

## 9. 后续扩展（未实现，按需再做）

1. **Milvus 原生混合检索**（BM25 稀疏向量 + 稠密 + `hybrid_search`）；
2. **上下文化切块**（contextualize：把每块在那篇文档里的上下文补进 `search_text`）；
3. **任务队列**（大目录导入、失败退避重试、进度上报）；
4. **扫描件 OCR**（PDF/图片的文本层缺失时）；
5. **引用链接化**（`Citation.url`，由接入方提供签名链接）。

## 10. 已知限制

* 表格只做「不切断 + 重复表头」，不做结构化解析；
* 扫描件 PDF 直接报 `scanned_pdf`，不做 OCR；
* 目录导入不删除「目录里消失」的文档（「没扫到」不等于「已删除」，删除请显式调用）；
* 内存后端适合 ≤ 数万切块；再大请换 Milvus；
* 每次入库都会整库重写 JSONL（单文档级别可接受，十万级文档请换元数据库）。