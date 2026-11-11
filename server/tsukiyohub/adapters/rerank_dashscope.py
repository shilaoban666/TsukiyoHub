"""可选重排适配器：阿里云百炼（DashScope）``gte-rerank``。

它解决什么问题
--------------
混合检索是「召回」逻辑：粗排会把语义相近但答非所问的片段带进来。重排模型对
（问题, 片段）逐对打分，分数是 0~1 **跨问题可比**的，因此：

* 能显著提升 Top-3 的准确率；
* 可以用重排分代替余弦做证据闸门（:func:`evaluate_grounding` 优先取 ``rerank_score``）。

为什么默认关闭
--------------
每次重排是一次额外的外部调用（延迟 + 费用）。没有密钥、或测评显示收益不明显时，
``NoopReranker`` 更划算——所以 :func:`build_reranker` 未配置时返回 ``NoopReranker``
（**不返回 None**，调用方不用到处判空）。

配置方式（不改 ``Settings``，避免内核为可选能力涨配置项）
--------------------------------------------------------
==============================  ==========================================
``TSUKIYO_RERANK_API_KEY``      专用密钥（优先）
``DASHSCOPE_API_KEY``           复用百炼密钥
``TSUKIYO_RERANK_MODEL``        默认 ``gte-rerank-v2``
``TSUKIYO_RERANK_BASE_URL``     默认为百炼文本重排端点
``settings.rerank_enabled``     必须为 ``True`` 才会启用（默认关闭）
==============================  ==========================================
"""

from __future__ import annotations

import os
from typing import Any

from tsukiyohub.config import Settings
from tsukiyohub.domain import ScoredChunk
from tsukiyohub.logging_setup import get_logger
from tsukiyohub.ports.rerank import NoopReranker, Reranker

logger = get_logger(__name__)

#: 百炼文本重排端点（与 embedding 的 OpenAI 兼容端点不同，这是 DashScope 原生协议）
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
DEFAULT_MODEL = "gte-rerank-v2"
#: 单条片段送模型的字符上限（再长对判断没有帮助，只是浪费 token）
DOC_CHARS = 1500
#: 重排分与 L1 分的合成权重（沿用源项目实测：模型分主导，原有序号做微调）
MODEL_WEIGHT = 0.7
BASE_WEIGHT = 0.3


class DashScopeReranker:
    """百炼重排（实现 :class:`~tsukiyohub.ports.rerank.Reranker`）。

    Args:
        api_key: 百炼密钥。
        model: 重排模型名。
        base_url: 重排端点。
        timeout: 单次请求超时（秒）；超时按失败处理（保留检索原顺序）。
    """

    name = "dashscope-rerank"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 8.0,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.model = (model or DEFAULT_MODEL).strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).strip()
        self.timeout = float(timeout or 8.0)
        #: 最近一次失败原因（便于健康检查 / 排障；不影响返回）
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        """有密钥才启用。"""
        return bool(self.api_key)

    # ------------------------------------------------------------------ 公开
    def rerank(self, query: str, chunks: list[ScoredChunk], *, top_n: int | None = None) -> list[ScoredChunk]:
        """按相关度重排并截断。

        失败 / 超时 / 解析不出分数时**原样返回**（保留融合顺序），绝不抛错——
        重排是「锦上添花」，不能成为检索链路的单点故障。
        """
        if not chunks or not self.enabled:
            return chunks[:top_n] if top_n else chunks
        try:
            scores = self._scores(query, [item.chunk.text[:DOC_CHARS] for item in chunks])
        except Exception as exc:  # noqa: BLE001 - 重排失败降级
            self.last_error = str(exc)
            logger.warning("重排失败，保留融合顺序：%s", exc)
            return chunks[:top_n] if top_n else chunks
        if not scores or len(scores) != len(chunks):
            self.last_error = "重排返回条数不匹配"
            logger.warning("重排返回条数不匹配（%d != %d），保留融合顺序", len(scores or []), len(chunks))
            return chunks[:top_n] if top_n else chunks

        base = max((item.score for item in chunks), default=0.0) or 1.0
        for item, score in zip(chunks, scores, strict=True):
            item.rerank_score = round(float(score), 6)
            item.score = round(MODEL_WEIGHT * float(score) + BASE_WEIGHT * (item.score / base), 6)
        ranked = sorted(chunks, key=lambda item: item.score, reverse=True)
        return ranked[:top_n] if top_n else ranked

    # ---------------------------------------------------------------- 内部
    def _scores(self, query: str, documents: list[str]) -> list[float]:
        """调用重排接口，返回与 ``documents`` 同序的 0~1 分数。"""
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise RuntimeError("未安装 httpx，无法调用重排接口") from exc

        payload: dict[str, Any] = {
            "model": self.model,
            "input": {"query": query, "documents": documents},
            "parameters": {"return_documents": False, "top_n": len(documents)},
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(self.base_url, json=payload, headers=headers)
            response.raise_for_status()
            body = response.json()
        results = ((body or {}).get("output") or {}).get("results") or []
        scores = [0.0] * len(documents)
        for row in results:
            try:
                index = int(row.get("index", -1))
                if 0 <= index < len(scores):
                    scores[index] = float(row.get("relevance_score", 0.0))
            except (TypeError, ValueError):
                continue
        self.last_error = None
        return scores


def build_reranker(settings: Settings) -> Reranker:
    """按配置构造重排器；未启用 / 无密钥时返回 :class:`NoopReranker`（**不返回 None**）。"""
    if not bool(getattr(settings, "rerank_enabled", False)):
        return NoopReranker()

    api_key = (
        os.environ.get("TSUKIYO_RERANK_API_KEY", "").strip()
        or os.environ.get("DASHSCOPE_API_KEY", "").strip()
        or _settings_dashscope_key(settings)
    )
    if not api_key:
        logger.info("rerank_enabled=true 但未配置百炼密钥，重排保持关闭")
        return NoopReranker()

    reranker = DashScopeReranker(
        api_key=api_key,
        model=os.environ.get("TSUKIYO_RERANK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        base_url=os.environ.get("TSUKIYO_RERANK_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
    )
    logger.info("重排器就绪：%s（%s）", reranker.name, reranker.model)
    return reranker


def _settings_dashscope_key(settings: Settings) -> str:
    """从 Settings 里找可复用的百炼密钥（embedding / llm 走百炼时通常同一个）。"""
    for provider_name, secret in (
        (settings.embedding_provider, settings.embedding_api_key),
        (settings.llm_provider, settings.llm_api_key),
    ):
        if str(provider_name) == "dashscope" and secret is not None:
            value = secret.get_secret_value().strip()
            if value:
                return value
    return ""


__all__ = ["DEFAULT_BASE_URL", "DEFAULT_MODEL", "DashScopeReranker", "build_reranker"]