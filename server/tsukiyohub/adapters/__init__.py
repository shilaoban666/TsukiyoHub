"""适配器层：端口的可替换实现（模型、向量化、向量库、存储、重排）。

刻意**不在本文件 import 任何子模块**：各适配器依赖不同的第三方库（langchain / pymilvus /
redis …），全量导入会让「只用一个适配器」的场景也被迫装齐所有依赖，也会让尚未实现或
导入失败的适配器连累别人。需要哪个就 ``from tsukiyohub.adapters.xxx import ...``。

现有实现：

======================== ============================================
``store_sql``            会话 / 消息 / 记忆 / 键值（SQLAlchemy）
``llm_openai``           OpenAI 兼容模型 + fake 模型 + 路由器
``embedding``            OpenAI / DashScope / hash 向量化
``vector_memory``        进程内混合检索（BM25 + 余弦，落盘 JSON）
``vector_milvus``        Milvus 后端
``rerank_dashscope``     可选重排
======================== ============================================
"""

from __future__ import annotations

__all__: list[str] = []