"""端口（Port）定义：内核只依赖这里的 Protocol，具体实现放在 ``tsukiyohub.adapters``。

这么做的好处：
* **换模型 / 换向量库 / 换存储不改内核**，也不影响任何前端；
* 测试可以注入内存实现，不需要任何外部服务；
* 第三方接入方可以自己写适配器（例如把知识库换成自家 ES）。

端口清单
--------
==========================  ==========================================
``LLMClient`` / ``LLMRouter`` 对话模型（OpenAI 兼容即可，含备用模型）
``EmbeddingProvider``        文本向量化
``VectorStore``              向量 + 关键词混合检索后端
``Reranker``                 检索结果重排（可选）
``BlobStore``               原文件存储（本地 / OSS）
``HotStore``                 热存储（Redis，可选）
``SessionStore``             会话与消息持久化
``MemoryStore``              长期记忆持久化
==========================  ==========================================
"""

from tsukiyohub.ports.blob import BlobStore, LocalBlobStore
from tsukiyohub.ports.embedding import EmbeddingProvider
from tsukiyohub.ports.hot import HotStore, NoopHotStore
from tsukiyohub.ports.llm import LLMClient, LLMReply, LLMRouter, ToolCall
from tsukiyohub.ports.memory import FactRecord, MemoryStore, MessageRecord, SessionRecord, SessionStore
from tsukiyohub.ports.rerank import Reranker
from tsukiyohub.ports.vector import IndexRow, SearchFilters, VectorStore

__all__ = [
    "LLMClient", "LLMReply", "LLMRouter", "ToolCall",
    "EmbeddingProvider",
    "VectorStore", "IndexRow", "SearchFilters",
    "Reranker",
    "BlobStore", "LocalBlobStore",
    "HotStore", "NoopHotStore",
    "SessionStore", "MemoryStore", "SessionRecord", "MessageRecord", "FactRecord",
]