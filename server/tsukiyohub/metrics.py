"""Prometheus 指标（零第三方依赖）。

为什么自己写：和网关保持同一风格——**不引入 prometheus_client**，只实现
counter / gauge / histogram 三种类型，输出标准 exposition 文本，Prometheus 直接可抓。
这样部署时不需要额外依赖，也方便面试时讲「指标怎么来的、标签怎么设计的」。

指标设计原则
------------
* **USE + RED**：服务侧看资源（会话/文档/记忆条数），请求侧看 Rate / Errors / Duration；
* **AI 特有**：token 用量、SSE 首字延迟、检索耗时与命中数、引用数、grounded 比例、技能调用；
* **标签基数可控**：`path` 用路由模板而不是原始 URL，`model` / `skill` / `intent` 都是有限集合；
* **命名与网关一致**：前缀 ``tsukiyohub_``，直方图统一 ``*_seconds``。

指标清单见 ``docs/observability.md``。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

#: 延迟直方图桶（秒）：5ms ~ 60s，覆盖检索、模型、SSE 首字
SECONDS_BUCKETS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
#: 计数类直方图桶（检索命中数、引用数）
COUNT_BUCKETS: tuple[float, ...] = (0, 1, 2, 3, 5, 8, 13, 21)

STARTED_AT = time.time()

_HELP: dict[str, str] = {
    "tsukiyohub_http_requests_total": "HTTP 请求总数（method/path/status）",
    "tsukiyohub_http_request_duration_seconds": "HTTP 请求耗时（秒）",
    "tsukiyohub_chat_requests_total": "问答请求数（按意图/是否有据/是否降级）",
    "tsukiyohub_chat_duration_seconds": "一次问答总耗时（秒）",
    "tsukiyohub_sse_first_token_seconds": "SSE 流式首字延迟（秒）",
    "tsukiyohub_llm_duration_seconds": "模型调用累计耗时（秒）",
    "tsukiyohub_retrieval_duration_seconds": "知识库检索耗时（秒）",
    "tsukiyohub_retrieval_hits": "单次检索命中的片段数",
    "tsukiyohub_tokens_total": "消耗的 token 数（prompt/completion）",
    "tsukiyohub_citations_per_answer": "一次回答附带的引用数",
    "tsukiyohub_skill_calls_total": "技能调用次数（name/ok）",
    "tsukiyohub_knowledge_ingest_total": "知识入库次数（ok/skipped/failed）",
    "tsukiyohub_knowledge_documents": "知识库文档数",
    "tsukiyohub_knowledge_chunks": "知识库切块数",
    "tsukiyohub_sessions": "会话总数",
    "tsukiyohub_messages": "消息总数",
    "tsukiyohub_memory_facts": "长期记忆条数",
    "tsukiyohub_skills_enabled": "启用中的技能数",
    "tsukiyohub_uptime_seconds": "进程运行时长（秒）",
    "tsukiyohub_llm_ready": "对话模型是否就绪（1/0）",
    "tsukiyohub_embedding_ready": "向量化是否就绪（1/0）",
}


def _labels_key(labels: dict[str, str] | None) -> str:
    if not labels:
        return ""
    parts = []
    for key in sorted(labels):
        value = str(labels[key]).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        parts.append(f'{key}="{value}"')
    return ",".join(parts)


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


class Registry:
    """线程安全的指标注册表。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[str, float]] = {}
        self._gauges: dict[str, dict[str, float]] = {}
        # histogram: name -> labelsKey -> (buckets, counts[], sum, count)
        self._histograms: dict[str, dict[str, list[Any]]] = {}
        self._buckets: dict[str, tuple[float, ...]] = {}

    # ---------------------------------------------------------------- 写入
    def inc(self, name: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
        key = _labels_key(labels)
        with self._lock:
            series = self._counters.setdefault(name, {})
            series[key] = series.get(key, 0.0) + float(value)

    def set(self, name: str, labels: dict[str, str] | None = None, value: float = 0.0) -> None:
        key = _labels_key(labels)
        with self._lock:
            series = self._gauges.setdefault(name, {})
            series[key] = float(value)

    def observe(self, name: str, labels: dict[str, str] | None, value: float,
                *, buckets: tuple[float, ...] | None = None) -> None:
        key = _labels_key(labels)
        with self._lock:
            if name not in self._buckets:
                self._buckets[name] = buckets or SECONDS_BUCKETS
            bounds = self._buckets[name]
            series = self._histograms.setdefault(name, {})
            entry = series.get(key)
            if entry is None:
                entry = [bounds, [0] * (len(bounds) + 1), 0.0, 0]
                series[key] = entry
            entry[2] += float(value)
            entry[3] += 1
            for index, bound in enumerate(bounds):
                if value <= bound:
                    entry[1][index] += 1
                    break
            else:
                entry[1][len(bounds)] += 1

    # ---------------------------------------------------------------- 读取
    def render(self) -> str:
        """输出 Prometheus 文本格式（线程安全快照）。"""
        with self._lock:
            counters = {k: dict(v) for k, v in self._counters.items()}
            gauges = {k: dict(v) for k, v in self._gauges.items()}
            histograms = {
                name: {key: (list(entry[0]), list(entry[1]), entry[2], entry[3]) for key, entry in series.items()}
                for name, series in self._histograms.items()
            }

        lines: list[str] = []

        def header(name: str, kind: str) -> None:
            help_text = _HELP.get(name)
            if help_text:
                lines.append(f"# HELP {name} {_escape_help(help_text)}")
            lines.append(f"# TYPE {name} {kind}")

        for name in sorted(counters):
            header(name, "counter")
            for key in sorted(counters[name]):
                labels = f"{{{key}}}" if key else ""
                lines.append(f"{name}{labels} {counters[name][key]:g}")

        for name in sorted(gauges):
            header(name, "gauge")
            for key in sorted(gauges[name]):
                labels = f"{{{key}}}" if key else ""
                lines.append(f"{name}{labels} {gauges[name][key]:g}")

        for name in sorted(histograms):
            header(name, "histogram")
            for key in sorted(histograms[name]):
                bounds, counts, total, count = histograms[name][key]
                cumulative = 0
                for index, bound in enumerate(bounds):
                    cumulative += counts[index]
                    label = f'{key},le="{bound:g}"' if key else f'le="{bound:g}"'
                    lines.append(f"{name}_bucket{{{label}}} {cumulative}")
                cumulative += counts[len(bounds)]
                label = f'{key},le="+Inf"' if key else 'le="+Inf"'
                lines.append(f"{name}_bucket{{{label}}} {cumulative}")
                labels = f"{{{key}}}" if key else ""
                lines.append(f"{name}_sum{labels} {total:g}")
                lines.append(f"{name}_count{labels} {count}")

        # 自观测
        header("tsukiyohub_uptime_seconds", "gauge")
        lines.append(f"tsukiyohub_uptime_seconds {time.time() - STARTED_AT:g}")
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """清空（仅测试用）。"""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()


#: 进程级注册表
REGISTRY = Registry()


# ---------------------------------------------------------------------------
# 便捷函数（带异常保护：打点失败绝不能影响业务）
# ---------------------------------------------------------------------------
def inc(name: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
    try:
        REGISTRY.inc(name, labels, value)
    except Exception:  # noqa: BLE001
        pass


def gauge(name: str, value: float, labels: dict[str, str] | None = None) -> None:
    try:
        REGISTRY.set(name, labels, value)
    except Exception:  # noqa: BLE001
        pass


def observe(name: str, value: float, labels: dict[str, str] | None = None,
            *, buckets: tuple[float, ...] | None = None) -> None:
    try:
        REGISTRY.observe(name, labels, value, buckets=buckets)
    except Exception:  # noqa: BLE001
        pass


def render() -> str:
    try:
        return REGISTRY.render()
    except Exception:  # noqa: BLE001
        return "# metrics render failed\n"


@contextmanager
def timed(name: str, labels: dict[str, str] | None = None) -> Iterator[None]:
    """``with timed("x_seconds"): ...`` —— 异常也会记录耗时。"""
    started = time.perf_counter()
    try:
        yield
    finally:
        observe(name, time.perf_counter() - started, labels)


# ---------------------------------------------------------------------------
# 业务打点
# ---------------------------------------------------------------------------
def record_http(method: str, path: str, status: int, seconds: float) -> None:
    labels = {"method": method, "path": path, "status": str(status)}
    inc("tsukiyohub_http_requests_total", labels)
    observe("tsukiyohub_http_request_duration_seconds", seconds, {"path": path})


def record_answer(result: Any, response: Any) -> None:
    """一次问答结束：请求量 / 耗时 / token / 引用 / 技能 / 检索 / 模型耗时。

    ``result`` 是内核的 AgentResult，``response`` 是 ChatResponse。
    """
    intent = getattr(getattr(response, "intent", None), "value", "unknown")
    grounded = "true" if getattr(response, "grounded", False) else "false"
    degraded = "true" if getattr(response, "degraded", False) else "false"
    inc("tsukiyohub_chat_requests_total", {"intent": intent, "grounded": grounded, "degraded": degraded})
    observe("tsukiyohub_chat_duration_seconds", float(getattr(response, "latency_ms", 0)) / 1000.0,
            {"intent": intent})

    usage = getattr(response, "usage", None) or {}
    model = str(getattr(response, "model", "") or "unknown")
    # ``usage_dict`` 用 OpenAI 风格键（input/output），也兼容 prompt/completion 命名
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    if prompt:
        inc("tsukiyohub_tokens_total", {"model": model, "direction": "prompt"}, prompt)
    if completion:
        inc("tsukiyohub_tokens_total", {"model": model, "direction": "completion"}, completion)

    citations = getattr(response, "citations", None) or []
    observe("tsukiyohub_citations_per_answer", float(len(citations)), buckets=COUNT_BUCKETS)

    for call in getattr(response, "tool_calls", None) or []:
        # 技能记录里可能是 name（OpenAI 风格）也可能是 tool（内置技能用的键）
        name = str(call.get("name") or call.get("tool") or "unknown")
        ok = "false" if call.get("ok") is False else "true"
        inc("tsukiyohub_skill_calls_total", {"skill": name, "ok": ok})

    # 内核侧耗时拆分：模型调用 / 检索。
    # 注意：本地假模型 / 内存检索常在 1ms 内完成，``int(ms)`` 会截断成 0；
    # 只要「确实发生过」就记录一次观测（0 秒也是合法样本），否则看板永远是空的。
    llm_ms = int(getattr(result, "llm_ms", 0) or 0)
    if llm_ms > 0 or getattr(result, "usages", None):
        observe("tsukiyohub_llm_duration_seconds", llm_ms / 1000.0, {"model": model})
    retrieve_ms = int(getattr(result, "retrieve_ms", 0) or 0)
    retrieved = getattr(result, "retrieved", None) or []
    if retrieve_ms > 0 or retrieved:
        observe("tsukiyohub_retrieval_duration_seconds", retrieve_ms / 1000.0)
    if retrieved:
        observe("tsukiyohub_retrieval_hits", float(len(retrieved)), buckets=COUNT_BUCKETS)


def record_sse_first_token(seconds: float) -> None:
    observe("tsukiyohub_sse_first_token_seconds", seconds)


def record_ingest(status: str) -> None:
    inc("tsukiyohub_knowledge_ingest_total", {"status": status})


def record_skill_call(skill: str, ok: bool) -> None:
    inc("tsukiyohub_skill_calls_total", {"skill": skill, "ok": "true" if ok else "false"})


def refresh_gauges(container: Any) -> None:
    """周期刷新「资源类」指标（知识库规模、会话数、记忆条数、就绪状态）。"""
    try:
        stats = container.knowledge.stats()
        gauge("tsukiyohub_knowledge_documents", float(stats.document_count))
        gauge("tsukiyohub_knowledge_chunks", float(stats.chunk_count))
    except Exception:  # noqa: BLE001
        pass
    try:
        session_stats = container.sessions.stats()
        gauge("tsukiyohub_sessions", float(session_stats.get("sessions", 0)))
        gauge("tsukiyohub_messages", float(session_stats.get("messages", 0)))
        gauge("tsukiyohub_memory_facts", float(session_stats.get("facts", 0)))
    except Exception:  # noqa: BLE001
        pass
    try:
        gauge("tsukiyohub_skills_enabled", float(len(container.skills.enabled(container.runtime_cfg()))))
    except Exception:  # noqa: BLE001
        pass
    try:
        _provider, _model, ready = container.router.info()
        gauge("tsukiyohub_llm_ready", 1.0 if ready else 0.0)
        gauge("tsukiyohub_embedding_ready", 1.0 if container.settings.embedding_ready else 0.0)
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "REGISTRY", "Registry", "inc", "gauge", "observe", "render", "timed",
    "record_http", "record_answer", "record_sse_first_token", "record_ingest",
    "record_skill_call", "refresh_gauges", "SECONDS_BUCKETS", "COUNT_BUCKETS",
]