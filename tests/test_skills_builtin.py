"""内置技能单测：计算器安全性、网页抓取拒内网、可选联网搜索、知识库工具写上下文。"""

from __future__ import annotations

import types
from typing import Any

import pytest

from tsukiyohub.domain import Chunk, DocType, ScoredChunk, SourceType
from tsukiyohub.kernel.skills import ToolContext
from tsukiyohub.kernel.skills.builtin.knowledge import KnowledgeTool, format_evidence
from tsukiyohub.kernel.skills.builtin.math import CalculatorTool, evaluate
from tsukiyohub.kernel.skills.builtin.web import WebError, WebFetchTool, WebSearchTool, check_url


def make_ctx(question: str = "年假有几天", cfg: dict[str, Any] | None = None) -> ToolContext:
    return ToolContext(question=question, user_ctx={"user_id": "u1"}, cfg=cfg or {}, pref={})


# ---------------------------------------------------------------------------
# calculator
# ---------------------------------------------------------------------------
def test_calculator_arithmetic() -> None:
    assert evaluate("1+2*3") == 7
    assert evaluate("(1+2)*3") == 9
    assert evaluate("10/4") == 2.5
    assert evaluate("10//4") == 2
    assert evaluate("10%3") == 1
    assert evaluate("2**10") == 1024
    assert evaluate("-3+5") == 2
    assert evaluate("1.5*2") == 3


def test_calculator_call_output() -> None:
    ctx = make_ctx()
    assert CalculatorTool().call({"expression": "1+2*3"}, ctx) == "1+2*3 = 7"
    assert CalculatorTool().call({"expression": "(1+2)*3"}, ctx) == "(1+2)*3 = 9"
    assert CalculatorTool().call({"expression": "10/4"}, ctx) == "10/4 = 2.5"
    assert ctx.records[-1]["result"] == "2.5"


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('dir')",
        "open('/etc/passwd').read()",
        "x + 1",
        "().__class__",
        "abs(-1)",
        "[1,2,3]",
        "'a'+'b'",
        "1 if True else 2",
    ],
)
def test_calculator_rejects_non_arithmetic(expression: str) -> None:
    text = CalculatorTool().call({"expression": expression}, make_ctx())
    assert text.startswith("【计算器】"), text
    assert "= " not in text


@pytest.mark.parametrize(
    ("expression", "keyword"),
    [
        ("1/0", "除数不能为 0"),
        ("2**10000", "指数太大"),
        ("1e400", "超出可计算范围"),
        ("", "表达式为空"),
        ("1+", "语法不正确"),
        ("1" * 201, "太长"),
    ],
)
def test_calculator_error_messages(expression: str, keyword: str) -> None:
    text = CalculatorTool().call({"expression": expression}, make_ctx())
    assert keyword in text, text


# ---------------------------------------------------------------------------
# web_fetch（不访问网络：内网地址在发起请求前就被拒绝）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/admin",
        "http://10.1.2.3/",
        "http://192.168.1.5/",
        "http://172.16.0.1/",
        "http://172.31.255.254/",
        "http://localhost/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/x",
        "http://0.0.0.0/",
        "http://100.100.100.200/",
        "ftp://example.com/file",
        "http://user:pass@example.com/",
    ],
)
def test_web_fetch_blocks_internal_targets(url: str) -> None:
    text = WebFetchTool().call({"url": url}, make_ctx())
    assert "读不了" in text, text
    assert "内网" in text or "只能读取" in text or "账号密码" in text


def test_check_url_rejects_domain_resolving_to_private() -> None:
    with pytest.raises(WebError) as error:
        check_url("http://example.com/page", resolver=lambda host: ["10.0.0.8"])
    assert "内网" in str(error.value)
    assert check_url("http://example.com/page", resolver=lambda host: ["93.184.216.34"]) == (
        "http://example.com/page"
    )


def test_check_url_accepts_public_ip_literal() -> None:
    assert check_url("https://93.184.216.34/x") == "https://93.184.216.34/x"


def test_web_fetch_reports_missing_url() -> None:
    text = WebFetchTool().call({}, make_ctx())
    assert "请给出要读取的网页链接" in text


def test_web_fetch_prefetch_ignores_question_without_url() -> None:
    assert WebFetchTool().prefetch(make_ctx("没有链接的问题")) is None


def test_web_fetch_prefetch_ignores_internal_url() -> None:
    ctx = make_ctx("看看 http://192.168.0.10/secret 这个")
    text = WebFetchTool().prefetch(ctx)
    assert text is not None and "读不了" in text


# ---------------------------------------------------------------------------
# web_search（未配置时必须默认关闭）
# ---------------------------------------------------------------------------
def test_web_search_disabled_without_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_SEARCH_ENDPOINT", raising=False)
    monkeypatch.delenv("WEB_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    tool = WebSearchTool(types.SimpleNamespace(skill_timeout_seconds=5.0))
    assert tool.configured is False
    assert tool.default_enabled is False
    text = tool.call({"query": "最新政策"}, make_ctx("网上查一下最新政策"))
    assert "没有配置联网搜索服务" in text
    assert tool.prefetch(make_ctx("网上查一下最新政策")) is None


def test_web_search_configured_parses_tavily_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_SEARCH_ENDPOINT", "https://search.example.com/search")

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "results": [
                    {"title": "示例一", "url": "https://example.com/a", "content": "摘要一"},
                    {"title": "示例二", "url": "https://example.com/b", "content": "摘要二"},
                    {"title": "坏链接", "url": "not-a-url", "content": "忽略"},
                ]
            }

    monkeypatch.setattr("httpx.post", lambda *args, **kwargs: FakeResponse())
    tool = WebSearchTool(types.SimpleNamespace(skill_timeout_seconds=5.0))
    assert tool.configured is True
    assert tool.default_enabled is True
    ctx = make_ctx("网上查一下最新政策")
    text = tool.call({"query": "最新政策"}, ctx)
    assert "[网1] 示例一" in text
    assert "[网2] 示例二" in text
    assert ctx.extra["web_sources"] == [
        ("示例一", "https://example.com/a", "example.com"),
        ("示例二", "https://example.com/b", "example.com"),
    ]


# ---------------------------------------------------------------------------
# knowledge_search
# ---------------------------------------------------------------------------
def _chunk(ordinal: int, text: str, *, title: str = "员工手册") -> Chunk:
    return Chunk(
        chunk_id=f"doc#chunk{ordinal}",
        doc_id="doc",
        text=text,
        ordinal=ordinal,
        title=title,
        uri="knowledge/employee.md",
        source_type=SourceType.LOCAL,
        doc_type=DocType.SPEC,
        version="v2.1",
        updated_at="2026-10-14",
    )


def _scored(chunks: list[Chunk], score: float = 0.82) -> list[ScoredChunk]:
    return [ScoredChunk(chunk=chunk, score=score, vector_score=score) for chunk in chunks]


class FakeKnowledgeStore:
    """只实现 KnowledgeTool 用到的 retrieve；rag 内核未就绪时也能测。"""

    def __init__(self, chunks: list[ScoredChunk]) -> None:
        self.chunks = chunks
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def retrieve(self, query: str, **kwargs: Any) -> list[ScoredChunk]:
        self.calls.append((query, kwargs))
        if isinstance(self.chunks, Exception):
            raise self.chunks
        return list(self.chunks)


def _knowledge_settings() -> types.SimpleNamespace:
    return types.SimpleNamespace(grounding_min_score=0.0)


def test_knowledge_search_numbers_hits_continuously() -> None:
    chunks = _scored([
        _chunk(0, "员工入职满一年可享受 5 天年假。"),
        _chunk(1, "年假需提前三天在系统里申请。"),
    ])
    store = FakeKnowledgeStore(chunks)
    tool = KnowledgeTool(_knowledge_settings(), store)
    ctx = make_ctx("年假有几天")

    text = tool.call({"query": "年假怎么算"}, ctx)
    assert "以下是与「年假怎么算」相关的知识库片段" in text
    assert "[1]" in text and "[2]" in text
    assert "《员工手册》" in text
    assert "v2.1" in text
    assert ctx.next_index == 3
    assert set(ctx.hits) == {1, 2}
    assert ctx.kb_called is True
    assert ctx.timings["retrieve"] >= 0
    assert len(ctx.retrieved) == 2
    assert ctx.retrieved[0]["title"] == "员工手册"
    assert ctx.records[-1]["hits"] == 2

    second = tool.call({"query": "再查一次年假"}, ctx)
    assert "[3]" in second and "[4]" in second
    assert ctx.next_index == 5


def test_knowledge_search_prefetch_fills_context() -> None:
    store = FakeKnowledgeStore(_scored([_chunk(0, "年假规定：满一年 5 天。")]))
    tool = KnowledgeTool(_knowledge_settings(), store)
    ctx = make_ctx("年假有几天")
    text = tool.prefetch(ctx)
    assert text is not None
    assert text.startswith("## 参考资料")
    assert "[1]" in text
    assert ctx.hits
    assert ctx.kb_called is True
    assert ctx.records[-1]["prefetch"] is True


def test_knowledge_search_empty_result() -> None:
    tool = KnowledgeTool(_knowledge_settings(), FakeKnowledgeStore([]))
    ctx = make_ctx("年假有几天")
    text = tool.call({"query": "年假"}, ctx)
    assert "没有找到" in text
    assert ctx.hits == {}
    assert ctx.next_index == 1
    assert tool.prefetch(ctx) is None


def test_knowledge_search_handles_store_failure() -> None:
    tool = KnowledgeTool(_knowledge_settings(), FakeKnowledgeStore(RuntimeError("索引损坏")))  # type: ignore[arg-type]
    ctx = make_ctx("年假有几天")
    text = tool.call({"query": "年假"}, ctx)
    assert "暂时不可用" in text
    assert ctx.kb_called is True


def test_knowledge_search_passes_filters_and_top_k() -> None:
    store = FakeKnowledgeStore(_scored([_chunk(0, "年假规定。")]))
    tool = KnowledgeTool(_knowledge_settings(), store)
    ctx = make_ctx("年假", cfg={"doc_types": ["faq"]})
    tool.call({"query": "年假", "top_k": 3}, ctx)
    _query, kwargs = store.calls[-1]
    assert kwargs["top_k"] == 3
    assert kwargs["filters"].doc_types == ["faq"]


def test_knowledge_search_uses_question_when_query_missing() -> None:
    store = FakeKnowledgeStore(_scored([_chunk(0, "年假规定。")]))
    tool = KnowledgeTool(_knowledge_settings(), store)
    tool.call({}, make_ctx("年假有几天"))
    assert store.calls[-1][0] == "年假有几天"


def test_format_evidence_includes_source_line_and_text() -> None:
    numbered = [(1, _scored([_chunk(0, "正文内容")])[0])]
    text = format_evidence(numbered)
    assert text.startswith("[1] 《员工手册》")
    assert "正文内容" in text