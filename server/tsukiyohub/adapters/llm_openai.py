"""OpenAI 兼容模型适配器：真实模型、演示模型、主备路由。

为什么这个文件在 ``adapters`` 而不是 ``kernel``
--------------------------------------------
内核只认 :class:`tsukiyohub.ports.llm.LLMClient`（消息进、回复出）。把 LangChain 的
``ChatOpenAI``、错误码解析、流式分片合并关在这里，换供应商 / 换 SDK 只改这一个文件；
而且内核不 import adapters，依赖方向不会反过来。

三个实现
--------
``OpenAIChatClient``   任何 OpenAI 兼容服务（OpenAI / DeepSeek / 通义千问 / vLLM / Ollama / 中转站），
                      支持 ``bind_tools`` 与流式；混合思考模型按需带 ``enable_thinking``；
                      供应商报「不支持 tool」时置 ``supports_tools=False`` 并抛
                      :class:`LLMToolUnsupported`（内核据此改走「先查再答」）。
``FakeLLMClient``      无密钥演示：识别「知识库 / 文档 / 资料」类提问并调用 ``knowledge_search``，
                      其余返回固定回复。保证「什么前端接上来都能看到效果」。
``ConfiguredLLMRouter`` 按 ``Settings`` 解析主 / 备模型，实现 ``LLMRouter`` 端口。
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any

from tsukiyohub.config import Settings
from tsukiyohub.logging_setup import get_logger
from tsukiyohub.ports.llm import LLMClient, LLMReply, LLMUnavailable, ToolCall
from tsukiyohub.ports.memory import KVStore

logger = get_logger(__name__)


class LLMToolUnsupported(LLMUnavailable):
    """模型 / 供应商不支持工具调用（400 + tool 关键字）。

    ``tool_unsupported`` 属性是给内核的识别标记——内核不 import 本模块（依赖方向），
    只按属性 / 类名 / 报错文本判断是否要降级为 ``plain`` 模式。
    """

    tool_unsupported = True


#: 供应商报错里出现这些词说明是「工具能力」问题，而不是网络 / 限流
_TOOL_MARKS = ("tool", "function", "tool_choice")
_BAD_REQUEST_MARKS = ("400", "bad request", "not support", "unsupported", "invalid")

#: LangChain 参数名映射（内核用 OpenAI 风格的驼峰 / 下划线混写）
_PARAM_MAP = {
    "temperature": "temperature",
    "top_p": "top_p",
    "topP": "top_p",
    "max_tokens": "max_tokens",
    "maxTokens": "max_tokens",
    "presence_penalty": "presence_penalty",
    "presencePenalty": "presence_penalty",
    "frequency_penalty": "frequency_penalty",
    "frequencyPenalty": "frequency_penalty",
}

#: 支持 ``enable_thinking`` 开关的模型族（按模型名前缀判断，不按地址猜）
_THINKING_PREFIXES = ("qwen", "deepseek-v4")


class OpenAIChatClient:
    """基于 ``langchain_openai.ChatOpenAI`` 的通用客户端（线程安全：底层模型可复用）。"""

    def __init__(
        self, *, model: str, base_url: str, api_key: str, provider: str, timeout: float,
        max_retries: int, thinking: bool, temperature: float, max_tokens: int,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self._api_key = api_key
        self.name = provider or "openai"
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        #: settings.llm_thinking：混合思考模型是否开启深度思考
        self.thinking = bool(thinking)
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        #: None = 未知（首次成功即视为支持）；False = 已确认不支持工具
        self.supports_tools: bool | None = None
        self._client: Any = None

    # ---------------------------------------------------------------- 构造
    def _chat_model(self) -> Any:
        """懒构造 ChatOpenAI（不在进程启动时就要求 langchain 可用）。"""
        if self._client is not None:
            return self._client
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {
            "model": self.model,
            "api_key": self._api_key,
            "base_url": self.base_url,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            # 流式也返回用量（OpenAI 兼容的 stream_options.include_usage），避免统计丢失
            "stream_usage": True,
        }
        if self._thinking_switch():
            kwargs["extra_body"] = {"enable_thinking": self.thinking}
        self._client = ChatOpenAI(**kwargs)
        return self._client

    def _thinking_switch(self) -> bool:
        """只有混合思考模型才接受 ``enable_thinking``（其它模型可能直接报错）。"""
        return (self.model or "").lower().startswith(_THINKING_PREFIXES)

    def _runnable(self, tools: list[dict[str, Any]] | None, params: dict[str, Any] | None) -> Any:
        """按本轮的工具与参数得到可调用对象（绑定参数不重建模型，保持缓存）。"""
        runnable = self._chat_model()
        mapped = _map_params(params)
        if mapped:
            runnable = runnable.bind(**mapped)
        if tools:
            runnable = runnable.bind_tools(tools)
        return runnable

    # ---------------------------------------------------------------- 调用
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        params: dict[str, Any] | None = None,
        on_delta: Any = None,
    ) -> LLMReply:
        """发一轮对话；``on_delta`` 非空时走流式，回调**当前累积全文**。"""
        runnable = self._runnable(tools, params)
        lc_messages = _to_lc_messages(messages)
        try:
            if on_delta is None:
                reply = runnable.invoke(lc_messages)
            else:
                reply = self._stream(runnable, lc_messages, on_delta)
        except Exception as exc:  # noqa: BLE001 - 先判断是否「工具能力」问题
            if tools and _looks_tool_unsupported(exc):
                self.supports_tools = False
                logger.warning("模型 %s 不支持工具调用，后续改走「先查再答」：%s", self.model, exc)
                raise LLMToolUnsupported(str(exc)[:300]) from exc
            raise
        return _to_reply(reply, model=self.model, provider=self.name)

    def _stream(self, runnable: Any, messages: list[Any], on_delta: Any) -> Any:
        """合并分片：内核要的是「当前全文」，所以每次回调都传累积后的文本。"""
        merged: Any = None
        for chunk in runnable.stream(messages):
            merged = chunk if merged is None else merged + chunk
            text = _text_of(merged)
            if not text or getattr(chunk, "tool_call_chunks", None):
                continue
            try:
                on_delta(text)
            except Exception as exc:  # noqa: BLE001 - 前端断开不该中断生成
                logger.debug("流式回调失败：%s", exc)
        if merged is None:
            raise RuntimeError("模型返回了空的流式结果")
        return merged


# ---------------------------------------------------------------------------
# 演示模型
# ---------------------------------------------------------------------------
#: 值得演示「查知识库」的提问特征
_KNOWLEDGE_WORDS = re.compile(r"知识库|文档|资料|文件|制度|规定|政策|流程|手册|规范|条款")
_SNIPPET = re.compile(r"\[(\d{1,3})\][^\n]*\n([^\n]{0,140})")


class FakeLLMClient:
    """无密钥演示模型：行为确定、不联网、支持一轮工具调用与流式。

    行为（与 docs/chat.md 一致）：

    * 问题含「知识库 / 文档 / 资料 …」且工具里有 ``knowledge_search`` → 发起一次工具调用；
    * 拿到工具结果 → 「根据资料：…（引用 [1]）」；结果为空 → 如实说没找到；
    * 其它问题 → 「我收到了你的问题：…（当前为演示模型，配置 LLM_API_KEY 后可接入真实模型）」。
    """

    name = "fake"
    supports_tools: bool | None = True

    def __init__(self, model: str = "fake-chat", *, thinking: bool = False) -> None:
        self.model = model
        self.thinking = thinking
        # 录制演示 GIF 用：按字符流式时人为放慢（默认 0 = 尽可能快）。
        # 只影响演示模型，真实模型不会读这个变量。
        self.stream_delay = max(0.0, float(os.environ.get("TSUKIYO_FAKE_DELAY_MS", "0") or 0)) / 1000.0

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        params: dict[str, Any] | None = None,
        on_delta: Any = None,
    ) -> LLMReply:
        content, tool_calls = self._respond(messages, tools)
        if on_delta is not None and content:
            # 按字符推送「当前全文」，与真实流式模型的行为一致
            for index in range(1, len(content) + 1):
                try:
                    on_delta(content[:index])
                except Exception:  # noqa: BLE001 - 演示模型同样不能因回调失败中断
                    break
                if self.stream_delay:
                    time.sleep(self.stream_delay)
        prompt = "\n".join(str(message.get("content") or "") for message in messages)
        prompt_tokens = _estimate_tokens(prompt)
        completion_tokens = _estimate_tokens(content) + (20 if tool_calls else 0)
        return LLMReply(
            content=content,
            tool_calls=tool_calls,
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            model=self.model,
            provider=self.name,
        )

    def _respond(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
    ) -> tuple[str, list[ToolCall]]:
        last = messages[-1] if messages else {}
        role = str(last.get("role") or "")

        # ---- 拿到工具结果：基于资料回答（引用 [n]）
        if role == "tool":
            result = str(last.get("content") or "")
            match = _SNIPPET.search(result)
            if match:
                snippet = match.group(2).strip()
                return f"根据资料：{snippet}（引用 [{match.group(1)}]）", []
            return ("知识库中暂时没有找到相关内容。你可以换个说法再问，或确认相关文档是否已上传到知识库。", [])

        question = _last_user_text(messages)
        names = _tool_names(tools)
        if "knowledge_search" in names and _KNOWLEDGE_WORDS.search(question):
            return "", [ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name="knowledge_search",
                                 arguments={"query": question[:100]})]
        if not question:
            return "你好，有什么可以帮你？", []
        return (f"我收到了你的问题：{question[:60]}"
                "（当前为演示模型，配置 LLM_API_KEY 后可接入真实模型）"), []


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
class ConfiguredLLMRouter:
    """按 ``Settings``（可被 KV 运行期覆盖）解析主 / 备模型。"""

    def __init__(self, settings: Settings, kv: KVStore | None = None) -> None:
        self.settings = settings
        self.kv = kv
        self._primary: LLMClient | None = None
        self._fallback: LLMClient | None = None

    # ---------------------------------------------------------------- 端口
    def primary(self) -> LLMClient:
        if self._primary is None:
            self._primary = self._build_primary()
        if self._primary is None:  # pragma: no cover - _build_primary 至少返回演示模型
            raise LLMUnavailable("没有可用的对话模型：请在 .env 配置 LLM_API_KEY")
        return self._primary

    def fallback(self) -> LLMClient | None:
        if self._fallback is None:
            self._fallback = self._build_fallback()
        return self._fallback

    def candidates(self) -> list[LLMClient]:
        """按尝试顺序：主 → 备。主模型不可用时也要能拿到备用模型。"""
        out: list[LLMClient] = []
        try:
            out.append(self.primary())
        except LLMUnavailable as exc:
            logger.warning("主模型不可用：%s", exc)
        fallback = self.fallback()
        if fallback is not None:
            out.append(fallback)
        return out

    def info(self) -> tuple[str, str, bool]:
        """(供应商, 模型, 是否可用)；给 /healthz 与 OpenAI 兼容接口的 ``model`` 字段用。"""
        try:
            client = self.primary()
            return client.name, client.model, True
        except LLMUnavailable:
            return self.settings.llm_provider, self.settings.llm_model, False

    def invalidate(self) -> None:
        """配置变更后重建客户端（接入方改完模型配置可调）。"""
        self._primary = None
        self._fallback = None

    # ---------------------------------------------------------------- 构造
    def _build_primary(self) -> LLMClient | None:
        settings = self.settings
        provider = str(settings.llm_provider)
        model = self._override("model.primary") or settings.llm_model
        api_key = _secret(settings.llm_api_key)
        if provider == "fake" or not api_key:
            if provider != "fake":
                logger.warning("未配置 LLM_API_KEY，使用演示模型（配置后可接入真实模型）")
            return FakeLLMClient(model=f"fake:{model or 'chat'}")
        return OpenAIChatClient(
            model=model,
            base_url=settings.llm_base_url,
            api_key=api_key,
            provider=provider,
            timeout=float(settings.llm_timeout_seconds),
            max_retries=int(settings.llm_max_retries),
            thinking=bool(settings.llm_thinking),
            temperature=float(settings.llm_temperature),
            max_tokens=int(settings.llm_max_tokens),
        )

    def _build_fallback(self) -> LLMClient | None:
        settings = self.settings
        model = self._override("model.fallback") or str(settings.llm_fallback_model or "").strip()
        if not model:
            return None
        if str(settings.llm_provider) == "fake":
            return None
        if model == str(settings.llm_model or "") and not _secret(settings.llm_fallback_api_key):
            return None   # 与主模型完全相同时没有意义
        api_key = _secret(settings.llm_fallback_api_key) or _secret(settings.llm_api_key)
        if not api_key:
            return None
        return OpenAIChatClient(
            model=model,
            base_url=str(settings.llm_fallback_base_url or settings.llm_base_url),
            api_key=api_key,
            provider=f"{settings.llm_provider}-fallback",
            timeout=float(settings.llm_timeout_seconds),
            max_retries=int(settings.llm_max_retries),
            thinking=bool(settings.llm_thinking),
            temperature=float(settings.llm_temperature),
            max_tokens=int(settings.llm_max_tokens),
        )

    def _override(self, key: str) -> str:
        """KV 运行期覆盖（``model.primary`` / ``model.fallback``），空串视为未设置。"""
        if self.kv is None:
            return ""
        try:
            value = self.kv.get(key)
        except Exception:  # noqa: BLE001 - 读不到就用配置文件
            return ""
        return str(value).strip() if value else ""


def build_router(settings: Settings, kv: KVStore | None = None) -> ConfiguredLLMRouter:
    """容器装配入口：``container.router = build_router(settings, container.kv)``。"""
    return ConfiguredLLMRouter(settings, kv)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _secret(value: Any) -> str:
    """``SecretStr | None`` → 去掉首尾空白的明文（空串表示未配置）。"""
    if value is None:
        return ""
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if callable(getter) else value).strip()


def _map_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """只放行 ChatOpenAI 认识的参数，未知键丢弃（供应商对它不认识的参数可能直接 400）。"""
    out: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if value is None or key not in _PARAM_MAP:
            continue
        name = _PARAM_MAP[key]
        out[name] = int(value) if name == "max_tokens" else float(value)
    return out


def _to_lc_messages(messages: list[dict[str, Any]]) -> list[Any]:
    """内部 OpenAI 格式 → LangChain 消息对象。"""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    out: list[Any] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = _text_of(message.get("content"))
        if role == "system":
            out.append(SystemMessage(content=content))
        elif role == "tool":
            out.append(ToolMessage(content=content, tool_call_id=str(message.get("tool_call_id") or "")))
        elif role == "assistant":
            out.append(AIMessage(content=content, tool_calls=_lc_tool_calls(message.get("tool_calls"))))
        else:
            out.append(HumanMessage(content=content))
    return out


def _lc_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """OpenAI 的 tool_calls（``function.arguments`` 是 JSON 字符串）→ LangChain 的 dict 列表。"""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for call in raw:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except ValueError:
                arguments = {}
        out.append({
            "name": str(function.get("name") or ""),
            "args": arguments if isinstance(arguments, dict) else {},
            "id": str(call.get("id") or ""),
            "type": "tool_call",
        })
    return out


def _to_reply(reply: Any, *, model: str, provider: str) -> LLMReply:
    """LangChain 的 AIMessage / AIMessageChunk → 内核的 :class:`LLMReply`。"""
    tool_calls: list[ToolCall] = []
    for call in getattr(reply, "tool_calls", None) or []:
        if not isinstance(call, dict):
            continue
        arguments = call.get("args")
        tool_calls.append(ToolCall(
            id=str(call.get("id") or ""),
            name=str(call.get("name") or ""),
            arguments=arguments if isinstance(arguments, dict) else {},
        ))
    return LLMReply(
        content=_text_of(reply),
        tool_calls=tool_calls,
        usage=_usage_of(reply),
        model=model,
        provider=provider,
        raw=reply,
    )


def _text_of(message: Any) -> str:
    """取消息正文（LangChain 的 content 可能是 str，也可能是分段列表）。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part if isinstance(part, str) else str(part.get("text", ""))
            for part in content if isinstance(part, (str, dict))
        )
    return str(content or "")


def _usage_of(message: Any) -> dict[str, int]:
    """用量：优先 ``usage_metadata``，其次 ``response_metadata.token_usage``。"""
    usage = getattr(message, "usage_metadata", None) or {}
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or 0)
    if not (prompt or completion):
        meta = getattr(message, "response_metadata", None) or {}
        tokens = meta.get("token_usage") or {}
        prompt = int(tokens.get("prompt_tokens") or 0)
        completion = int(tokens.get("completion_tokens") or 0)
        total = int(tokens.get("total_tokens") or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": total or prompt + completion}


def _looks_tool_unsupported(exc: BaseException) -> bool:
    """400 / bad request + tool 字样 = 供应商不接受工具参数。"""
    text = str(exc).lower()
    if not any(mark in text for mark in _TOOL_MARKS):
        return False
    return any(mark in text for mark in _BAD_REQUEST_MARKS)


def _tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    names: set[str] = set()
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and function.get("name"):
            names.add(str(function["name"]))
    return names


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if str(message.get("role") or "") == "user":
            return _text_of(message.get("content")).strip()
    return ""


def _estimate_tokens(text: str) -> int:
    """中文约 0.7 token / 字（演示模型只需要量级正确）。"""
    return max(1, int(len(text or "") * 0.7))


__all__ = [
    "ConfiguredLLMRouter", "FakeLLMClient", "LLMToolUnsupported", "OpenAIChatClient", "build_router",
]