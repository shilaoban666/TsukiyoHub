"""对话模型端口。

内核只认「消息进、回复出」，不关心背后是 OpenAI / DeepSeek / 通义千问 / vLLM / Ollama。
适配器实现见 ``tsukiyohub.adapters.llm_openai``。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ToolCall:
    """模型请求调用某个技能。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMReply:
    """一次模型调用的结果。"""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: prompt_tokens / completion_tokens / total_tokens（拿不到时为 0）
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    provider: str = ""
    #: 供应商原始返回（调试用，不序列化给前端）
    raw: Any = None

    @property
    def prompt_tokens(self) -> int:
        return int(self.usage.get("prompt_tokens") or 0)

    @property
    def completion_tokens(self) -> int:
        return int(self.usage.get("completion_tokens") or 0)

    @property
    def total_tokens(self) -> int:
        return int(self.usage.get("total_tokens") or self.prompt_tokens + self.completion_tokens)


#: 增量文字回调：流式输出时内核把「已经生成到哪」推给前端
DeltaSink = Callable[[str], None]


@runtime_checkable
class LLMClient(Protocol):
    """一个可对话的模型。"""

    name: str
    model: str
    #: None = 未知（首次调用后再回写）；False = 不支持工具调用，内核改走「先查再答」
    supports_tools: bool | None

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        params: dict[str, Any] | None = None,
        on_delta: DeltaSink | None = None,
    ) -> LLMReply:
        """发一轮对话。

        * ``messages``：OpenAI 格式（``role`` / ``content`` / ``tool_calls`` / ``tool_call_id``）；
        * ``tools``：OpenAI function 规格列表；
        * ``on_delta``：传入时走流式，边生成边回调**当前完整文本**（不是增量片段，
          这样前端可以整段替换，天然幂等）；
        * 返回的 :class:`LLMReply` 是这一轮的完整结果（含工具调用请求）。
        """
        ...


@runtime_checkable
class LLMRouter(Protocol):
    """按配置解析主 / 备模型，并暴露降级能力。"""

    def primary(self) -> LLMClient:
        """主模型（不可用时抛 :class:`LLMUnavailable`）。"""
        ...

    def fallback(self) -> LLMClient | None:
        """备用模型；未配置返回 None。"""
        ...

    def candidates(self) -> list[LLMClient]:
        """按尝试顺序返回可用模型（主 → 备）。"""
        ...

    def info(self) -> tuple[str, str, bool]:
        """(供应商名, 模型名, 是否可用)。"""
        ...


class LLMUnavailable(RuntimeError):
    """模型未配置 / 密钥缺失 / 供应商报错且不重试。"""


__all__ = ["LLMClient", "LLMRouter", "LLMReply", "ToolCall", "DeltaSink", "LLMUnavailable"]