"""文本向量化端口。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """把文本变成向量。

    约束：
    * ``embed`` 必须保持输入顺序，且对同一文本返回稳定结果（适配器内部应有缓存）；
    * 不可用时 ``enabled`` 为 ``False``，检索自动降级为纯关键词（BM25）；
    * 向量化失败时内核会调用 :meth:`pseudo_vector` 写入伪向量——文档仍可被关键词命中。
    """

    #: 供应商标识，例如 ``openai`` / ``dashscope`` / ``hash``
    provider: str
    #: 模型名，例如 ``text-embedding-3-small``
    model: str
    #: 向量维度
    dimension: int
    #: 是否具备真实向量化条件
    enabled: bool

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化（返回顺序与输入一致）。"""
        ...

    def pseudo_vector(self, text: str) -> list[float]:
        """确定性伪向量：向量化失败时占位，保证文档仍能被关键词检索到。"""
        ...


__all__ = ["EmbeddingProvider"]