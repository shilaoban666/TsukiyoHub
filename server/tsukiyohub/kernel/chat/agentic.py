"""轻量 Agentic 管线：先判「要不要查资料」，再决定直接答、查库答、还是反问澄清。

为什么不用 LangGraph
--------------------
原项目的 Agentic 图有十几个节点（感知 / 计划 / 检索 / 生成 / 核对 / 修订 / 兜底），
但真正决定用户体验的只有**入口路由**这一步：闲聊别去查库（慢且答非所问），
知识问题别直接答（容易编），指代不明先问清楚（比猜错省事）。
所以这里用纯 Python 实现「路由 + 复用 ChatAgent」的最小闭环：

    route(question, history)
      ├─ direct    → 不查库、不挂工具，直接让模型回答（闲聊 / 改写 / 计算）
      ├─ knowledge → 走 ChatAgent 的工具循环 + 检索 + 逐句核对（默认链路）
      └─ clarify   → 不调模型，反问一句补齐信息（省一次调用，也避免瞎猜）

**默认关闭**（``pref["agentic"] = True`` 才启用）：默认链路已经带规则预取与核对，
Agentic 的价值在于「明确不该查库时省掉一次检索」，这属于体验调优，不该默认改变行为。
"""

from __future__ import annotations

from typing import Any

from tsukiyohub.kernel.chat import router_rules
from tsukiyohub.kernel.chat.agent import AgentResult, ChatAgent, EventSink
from tsukiyohub.logging_setup import get_logger

logger = get_logger(__name__)

#: 指代不明时的反问话术（不含任何业务词）
CLARIFY_ANSWER = (
    "你想问的是哪一件事？可以再补充一点细节（比如相关的文档名、时间，或者你想解决的具体场景），"
    "我再帮你查。"
)

#: 「直接回答」时压制的偏好：不预取、不给工具（避免闲聊也去检索）
_DIRECT_PREF: dict[str, Any] = {
    "kb_prefetch": False,
    "max_tool_rounds": 0,
    "allow_skills": [],
}


class AgenticPipeline:
    """一次问答的轻量编排器（无状态，可直接多线程复用）。"""

    def __init__(self, agent: ChatAgent) -> None:
        self.agent = agent

    def run(
        self,
        question: str,
        history: list[dict[str, str]] | None = None,
        user_ctx: dict[str, Any] | None = None,
        *,
        preference: dict[str, Any] | None = None,
        on_event: EventSink | None = None,
    ) -> AgentResult:
        """按路由执行一次问答，结果在 ``route`` / ``mode`` 上标注走了哪条路。"""
        history = list(history or [])
        pref = dict(preference or {})
        route, reason = router_rules.describe(question, history)
        logger.debug("Agentic 路由：%s（%s）", route, reason)
        _emit(on_event, "status", "understanding")

        if route == "clarify":
            result = AgentResult(answer=CLARIFY_ANSWER, mode="agentic", route="clarify",
                                 grounded=False, degraded=False)
            _emit(on_event, "delta", CLARIFY_ANSWER)
            return result

        if route == "direct":
            result = self.agent.run(
                question, history, user_ctx, preference={**pref, **_DIRECT_PREF}, on_event=on_event,
            )
        else:
            result = self.agent.run(question, history, user_ctx, preference=pref, on_event=on_event)
        result.route = route
        result.mode = "agentic"
        return result


def _emit(on_event: EventSink | None, kind: str, value: Any) -> None:
    if on_event is None:
        return
    try:
        on_event(kind, value)
    except Exception as exc:  # noqa: BLE001 - 推送失败不影响回答
        logger.debug("事件推送失败（%s）：%s", kind, exc)


__all__ = ["CLARIFY_ANSWER", "AgenticPipeline"]