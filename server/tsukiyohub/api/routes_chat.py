"""统一问答入口。

* ``POST /api/chat``：一次返回完整回答（脚本 / 后端调用）
* ``POST /api/chat/stream``：SSE 逐字输出（自带聊天前端）
* ``POST /api/v1/chat/completions``：OpenAI 兼容（Dify / FastGPT / IDE / 任何 Agent 平台零改造接入）
* ``GET /api/v1/models``

三种入口共用同一套内核：请求归一化成 :class:`ChatRequest`，回答统一由
:class:`~tsukiyohub.kernel.chat.conversation.ConversationService` 产出。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from tsukiyohub.api.deps import ApiKeyDep, ContainerDep, RateLimitDep
from tsukiyohub.domain import Channel, ChatMessage, ChatRequest, ChatResponse, Role
from tsukiyohub.logging_setup import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["chat"])

#: SSE 心跳间隔：网关（Nginx / 云负载均衡）常在 60s 空闲后掐断连接
_HEARTBEAT_SECONDS = 15.0
#: ``delta`` 推送节流（秒）：模型逐 token 回调，按这个间隔合并成一次「当前全文」
_DELTA_INTERVAL_SECONDS = 0.04
#: 单次请求的问题长度上限
_MAX_QUESTION_CHARS = 8000


# ---------------------------------------------------------------------------
# 校验与协议转换
# ---------------------------------------------------------------------------
def _normalize(payload: ChatRequest) -> ChatRequest:
    """校验并归一化：空问题 422、超长 422、消息列表里的最后一条用户消息兜底成 question。"""
    question = (payload.question or "").strip()
    if not question and payload.messages:
        last_user = [m for m in payload.messages if m.role == Role.USER]
        if last_user:
            question = (last_user[-1].content or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="question 不能为空")
    if len(question) > _MAX_QUESTION_CHARS:
        raise HTTPException(status_code=422, detail=f"question 过长（上限 {_MAX_QUESTION_CHARS} 字符），请精简后重试")
    payload.question = question
    return payload


def _openai_to_chat_request(payload: dict[str, Any]) -> ChatRequest:
    """把 OpenAI Chat Completions 请求转成内部契约。"""
    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=422, detail="messages 不能为空")

    parsed: list[ChatMessage] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content", "")
        if isinstance(content, list):  # 多模态分段：只取文本部分
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        role = str(message.get("role", "user"))
        if role == "system":  # 外部平台的 system prompt 由本服务自己的提示词接管
            continue
        parsed.append(ChatMessage(role=Role.ASSISTANT if role == "assistant" else Role.USER, content=str(content)))

    question = next((m.content for m in reversed(parsed) if m.role == Role.USER), "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="未找到用户提问内容")

    # 前面的 assistant / user 轮次当作历史，最后一条 user 是本次问题
    history = [m for m in parsed[:-1]] if parsed and parsed[-1].role == Role.USER else parsed
    last_user_index = max((i for i, m in enumerate(history) if m.role == Role.USER), default=-1)
    trimmed = history[: last_user_index + 1] if last_user_index >= 0 else []

    return ChatRequest(
        question=question,
        session_id=payload.get("user") or payload.get("session_id"),
        user_id=payload.get("user"),
        messages=trimmed,
        channel=Channel.OPENAI,
        top_k=payload.get("top_k"),
        stream=bool(payload.get("stream")),
        extra={"source_protocol": "openai-compatible"},
    )


def _openai_chunks(response: ChatResponse, model: str) -> dict[str, Any]:
    """把内部响应转成 OpenAI 非流式响应体。"""
    return {
        "id": f"chatcmpl-{response.trace_id or response.session_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": response.answer_with_citations()},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": int(response.usage.get("prompt_tokens") or response.usage.get("input_tokens") or 0),
            "completion_tokens": int(response.usage.get("completion_tokens") or response.usage.get("output_tokens") or 0),
            "total_tokens": int(response.usage.get("total_tokens") or 0),
        },
        # 非标准但无害的附加字段：调用方可直接渲染引用
        "citations": [c.model_dump() for c in response.citations],
        "session_id": response.session_id,
        "grounded": response.grounded,
        "degraded": response.degraded,
    }


# ---------------------------------------------------------------------------
# 同步入口
# ---------------------------------------------------------------------------
@router.post("/chat", response_model=ChatResponse, summary="问答（一次性返回）")
async def chat(
    payload: ChatRequest,
    container: ContainerDep,
    _caller: ApiKeyDep,
    _rl: RateLimitDep,
) -> ChatResponse:
    """一次问答。任何前端 / 脚本 / 自动化平台都可以直接调。"""
    request = _normalize(payload)
    try:
        response = await container.conversation.answer(request)
    except Exception as exc:  # noqa: BLE001 - 对外只给可读信息，细节进日志
        logger.exception("问答失败：%s", exc)
        raise HTTPException(status_code=500, detail=f"问答失败：{exc}") from exc
    logger.info(
        "问答完成：channel=%s grounded=%s citations=%d skills=%s 耗时=%dms 降级=%s",
        request.channel.value, response.grounded, len(response.citations),
        ",".join(response.skills_used) or "-", response.latency_ms, response.degraded,
    )
    return response


# ---------------------------------------------------------------------------
# 流式入口（SSE）
# ---------------------------------------------------------------------------
async def _event_stream(container: ContainerDep, request: ChatRequest) -> AsyncIterator[str]:
    """把内核回调（可能来自工作线程）转成 SSE 帧。

    事件：``status`` / ``progress`` / ``tool`` / ``delta`` / ``reset`` / ``meta`` / ``error`` / ``done``。
    ``delta`` 推的是**当前累积全文**，前端整段替换即可，不需要自己拼接。
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def on_event(kind: str, value: Any) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (kind, value))

    async def run() -> None:
        try:
            response = await container.conversation.answer(request, on_event=on_event)
        except Exception as exc:  # noqa: BLE001
            logger.exception("流式问答失败：%s", exc)
            response = exc
        await queue.put(("final", response))

    task = asyncio.create_task(run())
    sent = ""
    last_push = 0.0
    try:
        yield "event: status\ndata: thinking\n\n"
        while True:
            try:
                kind, value = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"          # 注释帧：保持连接，前端解析器忽略
                continue

            if kind == "delta":
                text = str(value)
                if not text.startswith(sent):   # 换了一轮生成（例如查完知识库重新作答）
                    yield "event: reset\ndata: \n\n"
                    sent = ""
                # 推「当前累积全文」而不是增量：前端整段替换即可，天然幂等、不会因丢帧错位。
                # 节流：模型逐 token 回调，按 40ms 合并一次，避免长回答把带宽打满。
                now = time.monotonic()
                if text != sent and (now - last_push >= _DELTA_INTERVAL_SECONDS
                                     or text.endswith(("。", "！", "？", "\n"))):
                    sent = text
                    last_push = now
                    yield f"event: delta\ndata: {json.dumps(text, ensure_ascii=False)}\n\n"
            elif kind == "final":
                if isinstance(value, Exception):
                    message = str(value).strip() or "服务内部错误"
                    yield f"event: error\ndata: {json.dumps({'message': message}, ensure_ascii=False)}\n\n"
                    yield "event: done\ndata: [DONE]\n\n"
                    return
                final: ChatResponse = value
                tail = final.answer_with_citations()
                if not tail.startswith(sent):
                    yield "event: reset\ndata: \n\n"
                    sent = ""
                if tail != sent:
                    # 补最后一帧「累积全文」：节流可能吞掉了末尾几个 token，这里兜住
                    sent = tail
                    yield f"event: delta\ndata: {json.dumps(tail, ensure_ascii=False)}\n\n"
                meta = {
                    "session_id": final.session_id,
                    "message_id": final.message_id,
                    "citations": [c.model_dump() for c in final.citations],
                    "grounded": final.grounded,
                    "grounding_score": final.grounding_score,
                    "usage": final.usage,
                    "tool_calls": final.tool_calls,
                    "skills_used": final.skills_used,
                    "suggestions": final.suggestions,
                    "trace_id": final.trace_id,
                    "degraded": final.degraded,
                    "intent": final.intent.value,
                }
                yield f"event: meta\ndata: {json.dumps(meta, ensure_ascii=False)}\n\n"
                yield "event: done\ndata: [DONE]\n\n"
                return
            elif kind in ("status", "progress", "tool"):
                data = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
                yield f"event: {kind}\ndata: {data}\n\n"
            else:
                logger.debug("忽略未知的流式事件：%s", kind)
    finally:
        if not task.done():
            task.cancel()


@router.post("/chat/stream", summary="问答（SSE 逐字输出）", response_class=StreamingResponse)
async def chat_stream(
    payload: ChatRequest,
    container: ContainerDep,
    _caller: ApiKeyDep,
    _rl: RateLimitDep,
) -> StreamingResponse:
    request = _normalize(payload)
    return StreamingResponse(
        _event_stream(container, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # Nginx 不缓冲，否则 SSE 会被攒着一次性下发
        },
    )


# ---------------------------------------------------------------------------
# OpenAI 兼容
# ---------------------------------------------------------------------------
@router.post("/v1/chat/completions", summary="OpenAI 兼容接口（任何 Agent 平台可直接接入）")
async def chat_completions(
    container: ContainerDep,
    _caller: ApiKeyDep,
    _rl: RateLimitDep,
    payload: dict[str, Any] = Body(...),
) -> Any:
    request = _openai_to_chat_request(payload)
    model = payload.get("model") or container.model_name()
    if not payload.get("stream"):
        response = await container.conversation.answer(request)
        return _openai_chunks(response, model)

    chunk_id = f"chatcmpl-{int(time.time() * 1000):x}"

    def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
        body = {
            "id": chunk_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"

    async def event_stream() -> AsyncIterator[str]:
        yield chunk({"role": "assistant", "content": ""})
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        def on_event(kind: str, value: Any) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, (kind, value))

        async def run() -> None:
            try:
                response = await container.conversation.answer(request, on_event=on_event)
            except Exception as exc:  # noqa: BLE001
                response = exc
            await queue.put(("final", response))

        task = asyncio.create_task(run())
        sent = ""
        try:
            while True:
                try:
                    kind, value = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if kind == "delta":
                    text = str(value)
                    if not text.startswith(sent):
                        sent = ""
                    piece, sent = text[len(sent):], text
                    if piece:
                        yield chunk({"content": piece})
                elif kind == "final":
                    if isinstance(value, Exception):
                        yield chunk({"content": f"\n\n[错误] {value}"}, "stop")
                    break
        finally:
            if not task.done():
                task.cancel()
        yield chunk({}, "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/v1/models", summary="OpenAI 兼容模型列表")
async def list_models(container: ContainerDep) -> dict[str, Any]:
    model_id = container.model_name()
    return {
        "object": "list",
        "data": [{"id": model_id, "object": "model", "created": int(time.time()), "owned_by": "tsukiyohub"}],
    }


class FeedbackRequest(BaseModel):
    session_id: str = ""
    message_id: str = ""
    rating: int = 0
    comment: str = ""


@router.post("/feedback", summary="对一条回答打分（👍/👎）")
async def feedback(payload: FeedbackRequest, container: ContainerDep, _caller: ApiKeyDep) -> dict[str, Any]:
    """把评价写进热存储（没有 Redis 时返回成功但不持久化），接入方可自行落库。"""
    entry = {"session_id": payload.session_id, "message_id": payload.message_id,
             "rating": payload.rating, "comment": payload.comment, "at": int(time.time())}
    try:
        container.hot.set(f"feedback:{payload.message_id or payload.session_id}", entry, ttl_seconds=86400)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True}


__all__ = ["router"]