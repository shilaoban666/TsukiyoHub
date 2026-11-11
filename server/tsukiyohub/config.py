"""全局配置：pydantic-settings，从环境变量 / .env 读取。

设计原则
--------
* **配置即契约**：所有环境变量只在 :func:`get_settings` 一处读取，其它模块一律注入 ``Settings``。
* **零业务耦合**：不出现钉钉、部门、员工、公司等业务字段——这些属于接入方，内核只认「用户、会话、技能、知识」。
* **默认即可跑**：不填任何密钥也能启动（模型与向量化自动降级），方便任何前端先接上来。
* **相对路径锚定项目根**：``data/`` 默认在项目根下，从任何工作目录启动都得到同一份数据。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 项目根目录（server/tsukiyohub/config.py -> tsukiyohub -> server -> <root>）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

Environment = Literal["local", "dev", "test", "staging", "prod"]
LLMProviderName = Literal["openai", "dashscope", "deepseek", "fake"]
EmbeddingProviderName = Literal["openai", "dashscope", "hash"]
VectorBackendName = Literal["milvus", "memory"]
StorageBackendName = Literal["local", "oss"]


class Settings(BaseSettings):
    """内核服务的全部可配置项。字段顺序即 ``.env.example`` 的顺序。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", PROJECT_ROOT / "local.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ 基础
    app_name: str = "TsukiyoHub"
    app_env: Environment = "local"
    app_host: str = "0.0.0.0"
    app_port: int = 8080
    debug: bool = True
    log_level: str = "INFO"
    log_json: bool = False
    public_base_url: str = "http://127.0.0.1:8080"
    cors_origins: str = "*"

    #: 数据根目录：索引、上传的原文件、SQLite 数据库都在这里
    data_root: Path = Field(
        default=PROJECT_ROOT / "data",
        validation_alias=AliasChoices("TSUKIYO_DATA_ROOT", "DATA_ROOT"),
    )

    #: 对外 API 鉴权：留空 = 不鉴权（本地开发 / 内网）；填了则要求 ``Authorization: Bearer <key>``
    api_key: SecretStr | None = None
    #: 每个客户端 IP 每分钟请求上限（0 = 不限）
    rate_limit_per_minute: int = 0

    # -------------------------------------------------------------- 大模型
    # 对话模型走 OpenAI 兼容协议：OpenAI / DeepSeek / 通义千问 / vLLM / Ollama / 任何中转站均可
    llm_provider: LLMProviderName = "openai"
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr | None = None
    llm_fallback_model: str = ""
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: SecretStr | None = None
    llm_temperature: float = 0.3
    llm_max_tokens: int = 1500
    llm_timeout_seconds: int = 60
    llm_max_retries: int = 2
    #: 混合思考型模型（通义千问 / 百炼 DeepSeek V4）默认关闭深度思考，降低首字延迟
    llm_thinking: bool = False

    # -------------------------------------------------------------- 向量化
    embedding_enabled: bool = True
    embedding_provider: EmbeddingProviderName = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: SecretStr | None = None
    embedding_dimension: int = 1536
    embedding_batch_size: int = 10
    embedding_timeout_seconds: int = 30

    # ---------------------------------------------------------- 知识库 / 检索
    #: 启动时自动扫描 ``knowledge_dir`` 下的文件并入库（增量，按内容哈希去重）
    kb_auto_load_index: bool = True
    chunk_size: int = 700
    chunk_overlap: int = 120
    max_upload_mb: int = 32

    retrieval_top_k: int = 8
    retrieval_vector_k: int = 20
    retrieval_bm25_k: int = 20
    retrieval_fusion_k: int = 60
    retrieval_vector_weight: float = 0.6
    retrieval_bm25_weight: float = 0.4
    retrieval_min_score: float = 0.0
    rerank_enabled: bool = False
    rerank_max_chunks: int = 4

    # -------------------------------------------------------------- 向量库
    #: milvus = 生产检索后端；memory = 进程内混合检索（零依赖，本地演示 / 单测）
    vector_backend: VectorBackendName = "memory"
    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_token: SecretStr | None = None
    milvus_collection: str = "tsukiyo_chunks"
    milvus_timeout_seconds: float = 5.0

    #: 原文件存储：local = 本机目录（默认）；oss = 阿里云 OSS（S3 兼容）
    storage_backend: StorageBackendName = "local"
    oss_endpoint: str = ""
    oss_region: str = ""
    oss_bucket: str = ""
    oss_prefix: str = "kb/"
    oss_access_key_id: str = ""
    oss_access_key_secret: SecretStr | None = None

    # ------------------------------------------------------- 命中判定 / 防幻觉
    #: 检索结果最高向量余弦低于该值即视为「知识库里没有」
    grounding_min_score: float = 0.35
    #: 逐句核对（温度 0 的模型判定每句是否被资料支持）：strict=删掉无依据句；lenient=只告警；off=关闭
    answer_judge: Literal["strict", "lenient", "off"] = "strict"
    judge_timeout_seconds: float = 12.0

    # -------------------------------------------------------------- 数据库
    #: 会话 / 消息 / 模型配置 / 记忆。留空 = 自动使用 ``data/tsukiyohub.db``（SQLite）
    database_url: str = ""
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    # -------------------------------------------------------------- 热存储
    #: Redis 可选：会话状态、近期消息缓存。留空 = 全部读写数据库（单副本足够）
    redis_url: str = ""
    redis_prefix: str = "tsukiyo:"
    redis_timeout_ms: int = 50

    # ---------------------------------------------------------------- 对话
    #: 两条消息间隔超过该分钟数即视为一段新对话
    segment_idle_minutes: int = 30
    #: 一条消息内最多几轮工具调用
    max_tool_rounds: int = 3
    #: 规则判定「要查知识库」时先检索，把片段直接放进上下文，省掉一轮模型决策
    kb_prefetch: bool = True
    #: 单次请求最多注入多少条历史消息
    history_max_turns: int = 10
    #: 长对话摘要阈值（超过该轮数后把更早的对话压缩成摘要，0 = 不压缩）
    history_summary_after: int = 12
    #: 同时处理的问答请求上限
    max_concurrent_chats: int = 32
    #: 默认回复风格：tone = formal / friendly / concise
    default_tone: str = "friendly"
    default_length: str = "medium"
    default_markdown: bool = True
    #: 人设（系统提示词主干的补充），可用占位符 {bot_name} {user_name} {current_date}
    persona_prompt: str = ""
    #: 机器人名称：用于提示词与自我介绍
    bot_name: str = "Tsukiyo"
    #: 未命中知识库时的兜底话术
    fallback_text: str = "抱歉，我暂时无法回答这个问题。"
    #: 后台定时任务（会话归档 / 长对话摘要）
    scheduler_enabled: bool = True
    scheduler_interval_seconds: int = 60

    # ---------------------------------------------------------------- 技能
    #: SKILL.md 技能目录（每个技能一个子目录，内含 SKILL.md）
    skills_dir: str = ""
    #: 内置技能开关，形如 ``web_search,web_fetch,knowledge_search``
    skills_enabled: str = "knowledge_search,web_fetch"
    #: MCP server 配置文件（JSON），结构见 docs/skills.md；留空 = 不启用 MCP
    mcp_config_file: str = ""
    #: 技能工具默认超时（秒）
    skill_timeout_seconds: float = 20.0
    #: 联网搜索端点（Tavily 兼容的 POST 接口）。留空 = 不启用 ``web_search`` 技能
    web_search_endpoint: str = ""
    web_search_api_key: SecretStr | None = None
    #: web_fetch 单页正文上限（字符）与超时
    web_fetch_max_chars: int = 8000
    web_fetch_timeout_seconds: float = 15.0

    # ---------------------------------------------------------------- 记忆
    memory_enabled: bool = True
    #: 每个用户最多保留的事实条数
    memory_max_facts: int = 50
    #: 抽取记忆用的模型调用温度（0 = 稳定）
    memory_extract_temperature: float = 0.0

    # ---------------------------------------------------------------- 其它
    app_timezone: str = "Asia/Shanghai"
    #: 用于加密存库密钥的种子；留空时自动生成并保存在 data/.secrets/
    app_secret_key: SecretStr | None = None

    # ================================================================ 校验器
    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        return v.upper()

    @field_validator("chunk_size")
    @classmethod
    def _chunk_size_sane(cls, v: int) -> int:
        if v < 100:
            raise ValueError("CHUNK_SIZE 至少为 100，否则切块过碎会显著降低检索质量")
        return v

    @field_validator("grounding_min_score")
    @classmethod
    def _grounding_threshold_sane(cls, v: float) -> float:
        if v < 0:
            raise ValueError("GROUNDING_MIN_SCORE 不能为负（余弦相似度值域为 [-1, 1]）")
        return v

    # ============================================================ 派生属性
    @computed_field  # type: ignore[prop-decorator]
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def skills_enabled_list(self) -> list[str]:
        return [s.strip() for s in self.skills_enabled.split(",") if s.strip()]

    @property
    def data_dir(self) -> Path:
        return self.data_root

    @property
    def knowledge_dir(self) -> Path:
        """本地待入库文档目录（启动时自动增量导入）。"""
        return self.data_dir / "knowledge"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    @property
    def index_path(self) -> Path:
        return self.index_dir / "kb_index"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def skills_path(self) -> Path:
        return Path(self.skills_dir) if self.skills_dir.strip() else self.data_dir / "skills"

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / ".secrets"

    @property
    def logs_dir(self) -> Path:
        return PROJECT_ROOT / "logs"

    @property
    def resolved_database_url(self) -> str:
        """未配置时回落到项目内的 SQLite，保证「不装数据库也能跑」。"""
        url = (self.database_url or "").strip()
        if url:
            return url
        return f"sqlite:///{(self.data_dir / 'tsukiyohub.db').as_posix()}"

    @property
    def db_enabled(self) -> bool:
        return True

    @property
    def embedding_ready(self) -> bool:
        if not self.embedding_enabled:
            return False
        # ``hash`` 是零依赖演示模式：词面哈希，没有语义，检索退化为纯关键词
        if self.embedding_provider == "hash":
            return False
        return bool(self._secret(self.embedding_api_key) or self._secret(self.llm_api_key))

    @property
    def llm_ready(self) -> bool:
        if self.llm_provider == "fake":
            return True
        return bool(self._secret(self.llm_api_key))

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.app_env in ("staging", "prod")

    def ensure_dirs(self) -> None:
        """创建运行期必需目录（幂等）。"""
        for directory in (self.data_dir, self.knowledge_dir, self.index_dir, self.upload_dir, self.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _secret(value: SecretStr | None) -> str:
        return value.get_secret_value().strip() if value is not None else ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试中可用 ``get_settings.cache_clear()`` 覆盖。"""
    # 允许 Windows 控制台按 UTF-8 输出中文日志/横幅
    if os.name == "nt":  # pragma: no cover - 平台相关
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    return Settings()


__all__ = ["PROJECT_ROOT", "Settings", "get_settings"]