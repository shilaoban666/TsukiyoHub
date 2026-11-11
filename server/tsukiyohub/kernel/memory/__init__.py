"""记忆内核：长期事实的抽取 / 召回 / 守门，以及长对话摘要。

对上层只有两个入口：

* :class:`~tsukiyohub.kernel.memory.service.MemoryService`
  —— ``recall`` 注入「参考数据」块，``extract`` 后台提炼事实，``list/delete/clear`` 供 API 使用；
* :func:`~tsukiyohub.kernel.memory.summary.summarize`
  —— 历史超长时把更早的轮次压成摘要（失败退化为截断，绝不抛异常）。
"""

from __future__ import annotations

from tsukiyohub.kernel.memory import policy
from tsukiyohub.kernel.memory.extract import CandidateFact, FactExtractor, parse_facts
from tsukiyohub.kernel.memory.recall import render, select_facts
from tsukiyohub.kernel.memory.service import MemoryService, to_memory_fact
from tsukiyohub.kernel.memory.summary import due, summarize

__all__ = [
    "CandidateFact",
    "FactExtractor",
    "MemoryService",
    "due",
    "parse_facts",
    "policy",
    "render",
    "select_facts",
    "summarize",
    "to_memory_fact",
]