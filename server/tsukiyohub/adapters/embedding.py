"""向量化适配器：OpenAI 兼容 / DashScope 兼容 / 本地哈希（零依赖）。

三种实现的定位
--------------
===================  ==========================================================
``openai``           任何 OpenAI 兼容的 ``/embeddings`` 端点（OpenAI、DeepSeek、
                     vLLM、Ollama、各类中转站都行）——最通用
``dashscope``        阿里云百炼（OpenAI 兼容模式），默认模型 ``text-embedding-v3``
``hash``             本地特征哈希，**零网络**：按分词结果做 hashing trick。
                     工厂默认返回 ``enabled=False``（演示模式只用关键词检索），
                     显式 ``enabled=True`` 时才产生词面向量（仅供测试 / 调参）
===================  ==========================================================

约定（内核依赖这些不变量）
--------------------------
* 输出**必须 L2 归一化**：归一化后点积就是余弦，后端可以只做点积；
* 返回条数必须等于请求条数；空向量直接报错（空向量写进索引会永久污染检索）；
* ``hash`` 实现声明 ``semantic=False``（它没有真实语义，但对「词面重合」有效），
  因此检索层仍然会跑稠密通路；真正「关闭向量化」时 ``enabled=False``，索引写空向量。

为什么不用 LangChain 的 ``Embeddings`` 基类
-------------------------------------------
掉一个重依赖只为继承一个抽象类不划算；内核只认 :class:`EmbeddingProvider` 协议，
任何实现（包括接入方自研）都能插进来。
"""

from __future__ import annotations

import hashlib
import math

from tsukiyohub.config import Settings
from tsukiyohub.kernel.rag.tokenize import tokenize
from tsukiyohub.logging_setup import get_logger
from tsukiyohub.ports.embedding import EmbeddingProvider

logger = get_logger(__name__)

#: 特征哈希默认维度（与 ``settings.embedding_dimension`` 一致时可直接比对）
DEFAULT_HASH_DIMENSION = 1024


class EmbeddingError(RuntimeError):
    """向量化失败（提供给上层做「重试 / 降级」判断）。"""


def _normalize(vector: list[float]) -> list[float]:
    """L2 归一化；零向量原样返回（调用方会当作「无效向量」）。"""
    norm = math.sqrt(sum(value * value for value in vector))
    return vector if norm == 0 else [value / norm for value in vector]


def _lcg_pseudo(text: str, dimension: int) -> list[float]:
    """确定性伪向量：sha256 作种子 + 线性同余生成 + L2 归一化。"""
    state = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    vector: list[float] = []
    for _ in range(max(1, dimension)):
        state = (state * 6364136223846793005 + 1442695040888963407) % (2**64)
        vector.append((state / 2**64) * 2.0 - 1.0)
    return _normalize(vector)


# ---------------------------------------------------------------------------
# 本地哈希实现（零依赖 / 零网络）
# ---------------------------------------------------------------------------
class HashEmbeddingProvider:
    """特征哈希向量化：分词结果按 ``sha256`` 落桶，同词重合度决定余弦。

    它**不是**语义模型（``semantic=False``）：同一个意思换个说法就命中不了。
    更关键的是**分数不可标定**——实测相关问题余弦 0.30~0.51、无关问题 0.298，
    分布完全重叠，任何阈值都区分不开。所以 :func:`build_embeddings` 对 ``hash``
    返回 ``enabled=False``（检索只走 BM25，命中判定交给词面交集）；这里保留
    ``enabled=True`` 只是为了让测试 / 调参能验证「哈希向量确定性 + 词面相似」这条性质。

    Args:
        dimension: 向量维度。
        enabled: 是否参与稠密检索；``False`` 时索引写空向量、检索只走关键词。
    """

    provider = "hash"
    model = "hash-feature-v1"
    #: 没有真实语义能力（但仍是有效的词面向量，检索层不会因此关闭稠密通路）
    semantic = False
    batch_size = 64

    def __init__(self, dimension: int = DEFAULT_HASH_DIMENSION, *, enabled: bool = True) -> None:
        self.dimension = max(8, int(dimension or DEFAULT_HASH_DIMENSION))
        self.enabled = bool(enabled)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量特征哈希向量化（永不失败、永不联网）。"""
        return [self._embed_one(text) for text in texts]

    def pseudo_vector(self, text: str) -> list[float]:
        """占位向量（与 :meth:`embed` 不同的算法：只保证「同文本同向量」）。"""
        return _lcg_pseudo(text, self.dimension)

    def _embed_one(self, text: str) -> list[float]:
        tokens = tokenize(text)
        if not tokens:
            # 没有任何可切分的词（纯符号 / emoji）：退化成字符级哈希，至少保证可区分
            tokens = list((text or "").strip()[:64]) or ["_empty"]
        vector = [0.0] * self.dimension
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        return _normalize(vector)


# ---------------------------------------------------------------------------
# OpenAI 兼容实现（含 DashScope 兼容模式）
# ---------------------------------------------------------------------------
class OpenAIEmbeddingProvider:
    """任何 OpenAI 兼容 ``POST {base_url}/embeddings`` 端点。

    Args:
        model: 模型名，例如 ``text-embedding-3-small``。
        api_key: 密钥；为空时 ``enabled=False``（检索退化为纯关键词）。
        base_url: 接口根地址，例如 ``https://api.openai.com/v1``。
        dimension: 期望维度；``text-embedding-3-*`` / ``text-embedding-v*`` 会带上
            ``dimensions`` 参数，其它模型交给服务端默认值并在返回时校验。
        batch_size: 单次请求条数上限。
        timeout: 单次请求超时（秒）。
    """

    provider = "openai"
    semantic = True

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        dimension: int,
        batch_size: int = 10,
        timeout: float = 30.0,
        provider_name: str | None = None,
    ) -> None:
        self.model = (model or "").strip()
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "").rstrip("/")
        self.dimension = int(dimension or 0)
        self.batch_size = max(1, int(batch_size or 10))
        self.timeout = float(timeout or 30.0)
        if provider_name:
            self.provider = provider_name

    @property
    def enabled(self) -> bool:
        """有密钥且配置了接口地址才启用（否则检索退化为纯关键词）。"""
        return bool(self.api_key and self.base_url)

    # ------------------------------------------------------------------ 公开
    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化；失败抛 :class:`EmbeddingError`（由上层决定重试 / 降级）。"""
        if not texts:
            return []
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - 依赖缺失分支
            raise EmbeddingError("未安装 httpx，无法调用远程向量化接口；请执行 pip install httpx") from exc

        payload: dict[str, object] = {"model": self.model, "input": list(texts)}
        if self.dimension and self._supports_dimensions():
            payload["dimensions"] = self.dimension
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(f"{self.base_url}/embeddings", json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
        except Exception as exc:  # noqa: BLE001 - 统一成内核异常类型
            raise EmbeddingError(f"向量化接口调用失败（{self.model}）：{exc}") from exc

        try:
            rows = sorted(body["data"], key=lambda item: int(item.get("index", 0)))
            vectors = [_normalize([float(value) for value in row["embedding"]]) for row in rows]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError(f"向量化接口返回格式异常：{exc}") from exc
        if len(vectors) != len(texts):
            raise EmbeddingError(f"向量化返回条数({len(vectors)})与请求条数({len(texts)})不一致")
        for vector in vectors:
            if not vector:
                raise EmbeddingError("向量化返回了空向量（可能模型名不受支持或网关异常）")
        return vectors

    def pseudo_vector(self, text: str) -> list[float]:
        """查询失败时的占位向量（与真实向量空间无关，只用于让流程继续）。"""
        return _lcg_pseudo(text, self.dimension or DEFAULT_HASH_DIMENSION)

    def _supports_dimensions(self) -> bool:
        """只有已知支持 ``dimensions`` 参数的模型族才带它（带上不支持的参数会被 400）。"""
        model = self.model.lower()
        return model.startswith("text-embedding-3") or model.startswith("text-embedding-v")


class DashScopeEmbeddingProvider(OpenAIEmbeddingProvider):
    """阿里云百炼（DashScope）OpenAI 兼容模式。

    直接用兼容端点而不是厂商 SDK：少一个依赖，且换模型只改配置。
    默认模型 ``text-embedding-v3``（1024 维，单次最多 10 条）。
    """

    provider = "dashscope"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        dimension: int,
        batch_size: int = 10,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url,
            dimension=dimension,
            batch_size=batch_size,
            timeout=timeout,
            provider_name="dashscope",
        )


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def build_embeddings(settings: Settings) -> EmbeddingProvider:
    """按配置构造向量化器（**永不抛错**，最差情况返回不启用的哈希实现）。

    降级顺序：配置的 provider → 无密钥/依赖缺失 → 哈希实现（``enabled=False``，纯关键词）。
    服务可用性优先于检索质量：宁可降级，也不要启动失败。
    """
    dimension = max(8, int(settings.embedding_dimension or DEFAULT_HASH_DIMENSION))

    if not settings.embedding_enabled:
        logger.info("向量化已关闭（embedding_enabled=false），检索只用关键词")
        return HashEmbeddingProvider(dimension, enabled=False)

    provider = str(settings.embedding_provider or "").strip().lower()
    if provider == "hash":
        # hash 是「零依赖演示」：它是**词面哈希**，不是语义向量。
        # 实测同一批文档，相关问题余弦 0.30/0.51/0.37，无关问题 0.298——分布完全重叠，
        # 任何阈值都不可标定，会把「知识库里没有」判成「有」。
        # 所以工厂直接声明为不可用 → 检索走纯关键词（BM25），
        # 命中判定交给 grounding 的 BM25 分支（分数 > 0 且问题实词有交集），干净可分。
        # 想验证 hash 向量本身（确定性 / 词面相似）时，直接构造 HashEmbeddingProvider(enabled=True)。
        logger.info("向量化器：hash 演示模式 → 只用关键词检索（不产生语义向量）")
        return HashEmbeddingProvider(dimension, enabled=False)

    api_key = _embedding_key(settings, provider)
    if not api_key:
        logger.warning("未配置向量化密钥（provider=%s），检索降级为纯关键词", provider or "?")
        return HashEmbeddingProvider(dimension, enabled=False)

    if provider == "dashscope":
        base_url = _dashscope_base_url(settings)
        instance = DashScopeEmbeddingProvider(
            model=settings.embedding_model,
            api_key=api_key,
            base_url=base_url,
            dimension=dimension,
            batch_size=min(10, int(settings.embedding_batch_size or 10)),
            timeout=float(settings.embedding_timeout_seconds or 30),
        )
    elif provider == "openai":
        instance = OpenAIEmbeddingProvider(
            model=settings.embedding_model,
            api_key=api_key,
            base_url=settings.embedding_base_url,
            dimension=dimension,
            batch_size=int(settings.embedding_batch_size or 10),
            timeout=float(settings.embedding_timeout_seconds or 30),
        )
    else:
        logger.warning("未知的 embedding_provider=%r，检索降级为纯关键词", provider)
        return HashEmbeddingProvider(dimension, enabled=False)

    logger.info("向量化器就绪：provider=%s model=%s dim=%d", instance.provider, instance.model, dimension)
    return instance


def _embedding_key(settings: Settings, provider: str) -> str:
    """取向量化密钥：优先专用 key，其次复用对话模型 key（多数中转站 / 百炼是同一个）。"""
    for value in (settings.embedding_api_key, settings.llm_api_key):
        if value is not None:
            secret = value.get_secret_value().strip()
            if secret:
                return secret
    return ""


def _dashscope_base_url(settings: Settings) -> str:
    """DashScope 走 OpenAI 兼容端点；配置里显式给了非 OpenAI 地址时尊重配置。"""
    base_url = (settings.embedding_base_url or "").strip().rstrip("/")
    if base_url and "openai.com" not in base_url:
        return base_url
    return "https://dashscope.aliyuncs.com/compatible-mode/v1"


def describe_embeddings(provider: EmbeddingProvider) -> dict[str, object]:
    """健康检查 / 管理端用的向量化描述。"""
    return {
        "provider": getattr(provider, "provider", "none"),
        "model": getattr(provider, "model", ""),
        "dimension": getattr(provider, "dimension", 0),
        "enabled": bool(getattr(provider, "enabled", False)),
        "semantic": bool(getattr(provider, "semantic", True)),
    }


__all__ = [
    "DEFAULT_HASH_DIMENSION", "DashScopeEmbeddingProvider", "EmbeddingError",
    "HashEmbeddingProvider", "OpenAIEmbeddingProvider", "build_embeddings", "describe_embeddings",
]