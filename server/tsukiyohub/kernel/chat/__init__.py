"""聊天内核的公开入口。

一次问答涉及的四块能力：
=============================  ==========================================
:mod:`agent`                  模型 + 工具循环 + 引用编号 + 三级降级
:mod:`conversation`           会话解析、上下文窗口、记忆注入、落库、并发保护
:mod:`agentic`                可选的入口路由（direct / knowledge / clarify）
:mod:`citations`              逐段编号 → 逐文档编号、「参考来源」列表
=============================  ==========================================

其余模块是这两块的支撑：``prompt``（系统提示词结构）、``answer_style``（篇幅/版式口径）、
``router_rules``（纯规则意图判定）、``safety``（轻量脱敏与拒答）。

接入方通常只需要::

    from tsukiyohub.kernel.chat import ChatAgent, ConversationService

两者都由 ``tsukiyohub.container`` 装配，前端只通过 HTTP / SSE 访问，不直接依赖这里的类。
"""

from __future__ import annotations

from tsukiyohub.kernel.chat.agent import AgentResult, ChatAgent, EventSink, UsageRecord
from tsukiyohub.kernel.chat.agentic import AgenticPipeline
from tsukiyohub.kernel.chat.citations import renumber, tidy_refs, with_citations
from tsukiyohub.kernel.chat.conversation import ConversationService

__all__ = [
    "AgentResult", "AgenticPipeline", "ChatAgent", "ConversationService", "EventSink", "UsageRecord",
    "renumber", "tidy_refs", "with_citations",
]