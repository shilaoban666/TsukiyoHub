"""入库链路测试：解析 / 切块 / 幂等 / 失败语义 / 删除重建 / 落盘恢复。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from test_rag_support import (  # noqa: E402
    SAMPLE_LEAVE,
    build_settings,
    build_stack,
    document_of,
    ingest_text,
)
from tsukiyohub.adapters.vector_memory import build_memory_store  # noqa: E402
from tsukiyohub.domain import DocType  # noqa: E402
from tsukiyohub.kernel.rag.chunker import TextChunker  # noqa: E402
from tsukiyohub.kernel.rag.embeddings import CachedEmbeddings  # noqa: E402
from tsukiyohub.kernel.rag.parser import SUPPORTED_SUFFIXES, DocumentParser, ParseError  # noqa: E402
from tsukiyohub.kernel.rag.retriever import HybridRetriever  # noqa: E402
from tsukiyohub.kernel.rag.store import KnowledgeStore  # noqa: E402


class _FailingProvider:
    """可控的向量化失败替身（用于验证索引期的严格语义）。"""

    def __init__(self, *, semantic: bool = True) -> None:
        self.provider = "boom"
        self.model = "boom-v1"
        self.dimension = 256
        self.enabled = True
        self.batch_size = 10
        self.semantic = semantic
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        raise RuntimeError("网络不可用")

    def pseudo_vector(self, text: str) -> list[float]:
        return [0.01 * (index + 1) for index in range(self.dimension)]


def _stack_with_provider(tmp_path: Path, provider: object):
    """用自定义向量化器装配一套（其余与生产一致）。"""
    settings = build_settings(tmp_path)
    embeddings = CachedEmbeddings(provider, cache_path=None)  # type: ignore[arg-type]
    backend = build_memory_store(settings)
    retriever = HybridRetriever(settings, backend, embeddings)
    store = KnowledgeStore(settings, backend, embeddings, TextChunker(settings), retriever)
    return settings, store, backend


# ---------------------------------------------------------------------------
# 解析与切块
# ---------------------------------------------------------------------------
def test_ingest_markdown_breadcrumb_and_stable_chunk_ids(tmp_path: Path) -> None:
    """Markdown 零依赖解析；标题面包屑织进正文；chunk_id 稳定为 ``doc_id#ordinal``。"""
    stack = build_stack(build_settings(tmp_path))
    result = ingest_text(stack.store, "leave.md", SAMPLE_LEAVE)

    assert result.error is None
    assert result.chunk_count >= 3
    chunks = [stack.store.get_chunk(chunk_id) for chunk_id in stack.store.documents[result.doc_id].chunk_ids]
    assert all(chunk is not None for chunk in chunks)
    for ordinal, chunk in enumerate(chunks):
        assert chunk.chunk_id == f"{result.doc_id}#{ordinal}"
        assert chunk.ordinal == ordinal
    # 面包屑必须出现在正文里（BM25 只认正文，否则只在标题出现的问法召回不到）
    assert any("（请假管理规范 > 4. 请假管理 > 4.2 审批权限）" in chunk.text for chunk in chunks)
    combined = "\n".join(chunk.text for chunk in chunks)
    assert "500 元" in combined  # 表格被保留
    assert "| 一线城市 | 500 元 |" in combined
    # 标题取 Markdown 一级标题，而不是文件名
    assert stack.store.documents[result.doc_id].title == "请假管理规范"


def test_ingest_idempotent_by_content_hash(tmp_path: Path) -> None:
    """同一内容重复入库按 content_hash 跳过，索引不重复写。"""
    stack = build_stack(build_settings(tmp_path))
    first = ingest_text(stack.store, "leave.md", SAMPLE_LEAVE)
    count_after_first = stack.backend.count()
    second = ingest_text(stack.store, "leave.md", SAMPLE_LEAVE)

    assert first.skipped is False
    assert second.skipped is True
    assert second.reason and "未变化" in second.reason
    assert stack.backend.count() == count_after_first
    assert stack.store.stats().document_count == 1


def test_ingest_force_rebuilds_and_removes_stale_chunks(tmp_path: Path) -> None:
    """force=True 时重建：切块变少后，旧的多余切块必须从索引里删掉。"""
    stack = build_stack(build_settings(tmp_path, chunk_size=120, chunk_overlap=20))
    long_doc = document_of("d1", "长文档", "年假制度。" * 200)
    stack.store.upsert_document(long_doc)
    before = stack.backend.count()
    assert before > 1

    short_doc = document_of("d1", "长文档", "年假制度只有一句话。")
    result = stack.store.upsert_document(short_doc, force=True)

    assert result.chunk_count == 1
    assert stack.backend.count() == 1


def test_ingest_txt_with_gbk_encoding(tmp_path: Path) -> None:
    """GBK 编码的 txt 也能解析（编码探测 utf-8 → gbk 回退）。"""
    stack = build_stack(build_settings(tmp_path))
    result = stack.store.ingest_bytes("gbk.txt", "住宿上限 500 元。".encode("gbk"))
    assert result.error is None
    chunk = stack.store.get_chunk(f"{result.doc_id}#0")
    assert chunk is not None and "住宿上限" in chunk.text


def test_empty_document_is_skipped(tmp_path: Path) -> None:
    """解析后没有有效文本 → 跳过（不是失败）。"""
    stack = build_stack(build_settings(tmp_path))
    result = stack.store.upsert_document(document_of("d1", "空文档", "   "))
    assert result.skipped is True
    assert result.reason and "无有效文本" in result.reason
    assert stack.backend.count() == 0


def test_parser_markdown_and_txt_need_no_optional_deps() -> None:
    """零依赖格式必须在 SUPPORTED_SUFFIXES 里（API 上传按它校验）。"""
    required = {".md", ".markdown", ".txt", ".text", ".json", ".csv", ".pdf", ".docx", ".xlsx", ".pptx"}
    assert required <= SUPPORTED_SUFFIXES
    assert DocumentParser.can_parse(".md") and DocumentParser.can_parse(".TXT")
    assert not DocumentParser.can_parse(".exe")


def test_missing_optional_dependency_has_clear_chinese_error(tmp_path: Path) -> None:
    """缺 python-pptx 时给清晰中文错误，而不是 ImportError 崩掉整批。"""
    try:
        import pptx  # noqa: F401
    except ImportError:
        pass
    else:  # pragma: no cover - 装了可选依赖的环境不适用
        pytest.skip("当前环境已安装 python-pptx，无法验证缺失分支")

    stack = build_stack(build_settings(tmp_path))
    result = stack.store.ingest_bytes("deck.pptx", b"PK\x03\x04 not a real pptx")
    assert result.error is not None
    assert "未安装 python-pptx" in result.error


# ---------------------------------------------------------------------------
# 失败语义：索引期严格、显式降级才写伪向量
# ---------------------------------------------------------------------------
def test_index_embedding_failure_returns_error_and_writes_nothing(tmp_path: Path) -> None:
    """真实向量化失败 → 重试一次 → 仍失败就返回 error，绝不写伪向量污染索引。"""
    provider = _FailingProvider(semantic=True)
    settings, store, backend = _stack_with_provider(tmp_path, provider)
    result = ingest_text(store, "leave.md", SAMPLE_LEAVE)

    assert result.error is not None and "向量化失败" in result.error
    assert provider.calls == 2, "应按要求重试一次"
    assert backend.count() == 0
    assert store.documents == {}
    assert store.loaded is False
    assert settings.embedding_provider == "hash"  # 装配用的配置未被破坏


def test_non_semantic_provider_falls_back_to_pseudo_vectors(tmp_path: Path) -> None:
    """``semantic=False``（明确的降级模式）允许写伪向量，文档仍可被关键词命中。"""
    provider = _FailingProvider(semantic=False)
    _, store, backend = _stack_with_provider(tmp_path, provider)
    result = ingest_text(store, "leave.md", SAMPLE_LEAVE)

    assert result.error is None
    assert backend.count() == result.chunk_count > 0
    hits = store.retrieve("年假由谁审批")
    assert hits, "写伪向量后关键词通路仍应命中"


# ---------------------------------------------------------------------------
# 删除 / 重建 / 持久化
# ---------------------------------------------------------------------------
def test_delete_document_then_reingest(tmp_path: Path) -> None:
    """删除文档会同时清掉切块与索引，之后可以重新入库。"""
    stack = build_stack(build_settings(tmp_path))
    result = ingest_text(stack.store, "leave.md", SAMPLE_LEAVE)
    assert stack.backend.count() > 0

    removed = stack.store.delete_document(result.doc_id)
    assert removed > 0
    assert stack.backend.count() == 0
    assert stack.store.documents == {}
    assert stack.store.stats().document_count == 0
    assert stack.store.delete_document(result.doc_id) == 0  # 幂等

    again = ingest_text(stack.store, "leave.md", SAMPLE_LEAVE)
    assert again.chunk_count > 0
    assert stack.store.retrieve("补卡期限")


def test_persist_and_load_roundtrip(tmp_path: Path) -> None:
    """清单 + 切块 + 向量落盘后，新进程装配能直接恢复并检索。"""
    settings = build_settings(tmp_path)
    first = build_stack(settings)
    result = ingest_text(first.store, "leave.md", SAMPLE_LEAVE)
    assert first.store.manifest_path.exists()
    assert first.store.chunks_path.exists()
    assert settings.index_path.with_name(settings.index_path.name + "_vectors.jsonl").exists()

    second = build_stack(settings)
    assert second.store.loaded is False  # load() 之前还没读清单
    assert second.store.load() is True
    assert second.store.stats().document_count == 1
    assert second.store.get_chunk(f"{result.doc_id}#0") is not None
    hits = second.store.retrieve("年假由谁审批")
    assert hits and "年假" in hits[0].chunk.text


def test_load_rebuilds_index_when_backend_lost(tmp_path: Path) -> None:
    """清单在、向量文件丢了：load() 按切块重建索引，而不是「有文档但搜不到」。"""
    settings = build_settings(tmp_path)
    first = build_stack(settings)
    ingest_text(first.store, "leave.md", SAMPLE_LEAVE)
    settings.index_path.with_name(settings.index_path.name + "_vectors.jsonl").unlink()

    second = build_stack(settings)
    assert second.backend.count() == 0
    assert second.store.load() is True
    assert second.backend.count() > 0
    assert second.store.retrieve("年假由谁审批")


def test_load_tolerates_corrupt_chunk_lines(tmp_path: Path) -> None:
    """切块 JSONL 里的坏行跳过，其余照常恢复。"""
    settings = build_settings(tmp_path)
    first = build_stack(settings)
    ingest_text(first.store, "leave.md", SAMPLE_LEAVE)
    with first.store.chunks_path.open("a", encoding="utf-8") as handle:
        handle.write("{不是合法 JSON}\n")
        handle.write("\n")

    second = build_stack(settings)
    assert second.store.load() is True
    assert second.store.chunk_count > 0
    assert second.store.retrieve("补卡期限")


def test_ingest_directory_scans_supported_files(tmp_path: Path) -> None:
    """目录导入只收支持的格式，跳过隐藏与不支持的；返回每篇结果。"""
    knowledge = tmp_path / "knowledge"
    (knowledge / "sub").mkdir(parents=True)
    (knowledge / "leave.md").write_text(SAMPLE_LEAVE, encoding="utf-8")
    (knowledge / "sub" / "note.txt").write_text("差旅住宿上限 500 元。", encoding="utf-8")
    (knowledge / "skip.exe").write_text("binary", encoding="utf-8")
    (knowledge / ".hidden").mkdir()
    (knowledge / ".hidden" / "x.md").write_text("不该被扫描", encoding="utf-8")

    stack = build_stack(build_settings(tmp_path))
    results = stack.store.ingest_directory(knowledge)

    assert len(results) == 2
    assert all(result.error is None for result in results)
    assert stack.store.stats().document_count == 2
    # 再扫一次：全部按内容哈希跳过
    again = stack.store.ingest_directory(knowledge)
    assert all(result.skipped for result in again)


def test_list_documents_and_stats(tmp_path: Path) -> None:
    """stats/list_documents 字段与 domain 契约一致（API 直接用）。"""
    stack = build_stack(build_settings(tmp_path))
    result = ingest_text(stack.store, "leave.md", SAMPLE_LEAVE, doc_type=DocType.SPEC)

    stats = stack.store.stats()
    assert stats.document_count == 1
    assert stats.chunk_count == result.chunk_count
    assert stats.vector_count == result.chunk_count
    # provider=hash 演示模式 → 向量化器声明不可用（检索只走关键词）
    assert stats.embedding_enabled is False
    assert stats.embedding_model is None
    assert stats.vector_store == "memory"
    assert stats.doc_types == {"spec": 1}

    documents = stack.store.list_documents()
    assert len(documents) == 1
    item = documents[0]
    assert item.doc_id == result.doc_id
    assert item.title == "请假管理规范"
    assert item.doc_type == "spec"
    assert item.chunk_count == result.chunk_count
    assert item.ingested_at


def test_parse_error_does_not_abort_batch(tmp_path: Path) -> None:
    """单篇解析失败只影响这一篇（目录导入继续跑）。"""
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "ok.md").write_text(SAMPLE_LEAVE, encoding="utf-8")
    (knowledge / "bad.pdf").write_bytes(b"not a pdf at all")

    stack = build_stack(build_settings(tmp_path))
    results = stack.store.ingest_directory(knowledge)

    assert len(results) == 2
    errors = [result for result in results if result.error]
    assert len(errors) == 1
    assert any(result.error is None for result in results)
    assert stack.store.stats().document_count == 1


def test_parser_error_type_is_parse_error(tmp_path: Path) -> None:
    """解析器统一抛中文 ParseError（带 code），调用方好分类重试。"""
    stack = build_stack(build_settings(tmp_path))
    parser = DocumentParser(stack.settings)
    with pytest.raises(ParseError) as info:
        parser.parse_bytes("weird.bin", b"\x00\x01")
    assert "不支持的格式" in str(info.value)
    assert info.value.code == "unsupported_type"