"""知识库接口：上传、导入、列表、删除、重建、检索调试。

知识库是「RAG 内核」对外的唯一入口——任何前端 / 脚本都通过这几个端点管理语料，
再通过 ``/api/chat`` 让模型基于语料回答。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from tsukiyohub.api.deps import ApiKeyDep, ContainerDep
from tsukiyohub.api.schemas import (
    DeleteDocumentResponse,
    DocumentListResponse,
    IngestRequest,
    IngestResponse,
    ReindexResponse,
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from tsukiyohub.domain import DocType
from tsukiyohub.kernel.rag.parser import SUPPORTED_SUFFIXES
from tsukiyohub.logging_setup import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get("/stats", summary="知识库统计")
async def stats(container: ContainerDep, _caller: ApiKeyDep) -> Any:
    return container.knowledge.stats()


@router.get("/documents", response_model=DocumentListResponse, summary="文档列表")
async def list_documents(container: ContainerDep, _caller: ApiKeyDep) -> DocumentListResponse:
    items = container.knowledge.list_documents()
    return DocumentListResponse(items=items, total=len(items))


@router.post("/upload", summary="上传文档（multipart，可多文件）")
async def upload(
    container: ContainerDep,
    _caller: ApiKeyDep,
    file: list[UploadFile] = File(..., description="一个或多个文档"),
    doc_type: str = Query("doc"),
) -> dict[str, Any]:
    """上传即入库：解析 → 切块 → 向量化 → 写索引，一步到位。

    原文件同时落一份到 ``data/files/``（方便回溯），再交给知识库解析。
    """
    max_bytes = container.settings.max_upload_bytes
    results: list[dict[str, Any]] = []
    for item in file:
        name = Path(item.filename or "unnamed").name
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            results.append({"file_name": name, "ok": False,
                            "error": f"不支持的文件类型 {suffix or '(无扩展名)'}；支持：{', '.join(sorted(SUPPORTED_SUFFIXES))}"})
            continue
        data = await item.read()
        if len(data) > max_bytes:
            results.append({"file_name": name, "ok": False,
                            "error": f"文件过大（{len(data) / 1024 / 1024:.1f} MB），上限 {container.settings.max_upload_mb} MB"})
            continue
        if not data:
            results.append({"file_name": name, "ok": False, "error": "文件内容为空"})
            continue
        try:
            key = f"kb/{int(time.time())}-{name}"
            container.blobs.put(key, data)
        except Exception as exc:  # noqa: BLE001 - 原文件存档失败不阻碍入库
            logger.warning("原文件存档失败（继续入库）：%s", exc)
            key = ""
        try:
            result = container.knowledge.ingest_bytes(name, data, doc_type=DocType.parse(doc_type))
            results.append({
                "file_name": name, "ok": not result.error, "doc_id": result.doc_id,
                "title": result.title, "chunk_count": result.chunk_count,
                "skipped": result.skipped, "reason": result.reason, "error": result.error,
            })
            from tsukiyohub.metrics import record_ingest

            record_ingest("failed" if result.error else ("skipped" if result.skipped else "ok"))
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不影响其它文件
            logger.exception("文档入库失败：%s", name)
            results.append({"file_name": name, "ok": False, "error": str(exc)})
            from tsukiyohub.metrics import record_ingest

            record_ingest("failed")
    ok_count = sum(1 for r in results if r.get("ok"))
    return {"ok": ok_count == len(results), "count": ok_count, "results": results}


@router.post("/ingest", response_model=IngestResponse, summary="导入服务器上已有的文件 / 目录")
async def ingest(payload: IngestRequest, container: ContainerDep, _caller: ApiKeyDep) -> IngestResponse:
    """把服务器上的目录（递归）或单个文件导入知识库。

    ``path`` 可以是绝对路径，也可以是相对 ``data/`` 的路径（例如 ``knowledge``）。
    """
    raw = Path(payload.path)
    path = raw if raw.is_absolute() else container.settings.data_dir / raw
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在：{path}")
    try:
        if path.is_dir():
            results = container.knowledge.ingest_directory(path, force=payload.force)
            payloads = [r.model_dump() for r in results]
        else:
            result = container.knowledge.ingest_path(path, doc_type=DocType.parse(payload.doc_type))
            payloads = [result.model_dump()]
    except Exception as exc:  # noqa: BLE001
        logger.exception("导入失败：%s", path)
        raise HTTPException(status_code=500, detail=f"导入失败：{exc}") from exc

    added = sum(1 for r in payloads if not r.get("skipped") and not r.get("error"))
    skipped = sum(1 for r in payloads if r.get("skipped"))
    failed = sum(1 for r in payloads if r.get("error"))
    return IngestResponse(results=payloads, added=added, skipped=skipped, failed=failed)


@router.delete("/documents/{doc_id}", response_model=DeleteDocumentResponse, summary="删除文档")
async def delete_document(doc_id: str, container: ContainerDep, _caller: ApiKeyDep) -> DeleteDocumentResponse:
    removed = container.knowledge.delete_document(doc_id)
    if removed == 0 and doc_id not in container.knowledge.documents:
        raise HTTPException(status_code=404, detail=f"文档不存在：{doc_id}")
    return DeleteDocumentResponse(doc_id=doc_id, removed_chunks=removed)


@router.post("/search", response_model=SearchResponse, summary="检索调试（不调模型）")
async def search(payload: SearchRequest, container: ContainerDep, _caller: ApiKeyDep) -> SearchResponse:
    """只跑检索，返回每条命中的向量分 / 关键词分 / 融合分——调参时看这个。"""
    started = time.perf_counter()
    doc_types = payload.doc_types
    chunks = container.knowledge.retrieve(payload.query, top_k=payload.top_k, doc_types=doc_types)
    from tsukiyohub.kernel.rag.grounding import evaluate_grounding

    grounding = evaluate_grounding(chunks, container.settings.grounding_min_score)
    return SearchResponse(
        query=payload.query,
        hits=[
            SearchHit(
                chunk_id=c.chunk.chunk_id, doc_id=c.chunk.doc_id, title=c.chunk.title, text=c.chunk.text,
                score=round(c.score, 6), vector_score=None if c.vector_score is None else round(c.vector_score, 6),
                bm25_score=None if c.bm25_score is None else round(c.bm25_score, 6),
                rerank_score=None if c.rerank_score is None else round(c.rerank_score, 6),
                retriever=c.retriever, uri=c.chunk.uri,
            )
            for c in chunks
        ],
        latency_ms=int((time.perf_counter() - started) * 1000),
        grounded=grounding.grounded,
        grounding_score=grounding.score,
    )


@router.post("/reindex", response_model=ReindexResponse, summary="重建索引")
async def reindex(container: ContainerDep, _caller: ApiKeyDep) -> ReindexResponse:
    """清空索引并重新导入 ``data/knowledge``。

    向量化接口偶发故障后、或更换 embedding 模型（维度变了）时用。
    """
    container.knowledge.clear()
    results = container.knowledge.ingest_directory(container.settings.knowledge_dir, force=True)
    stats = container.knowledge.stats()
    failed = [r for r in results if r.error]
    return ReindexResponse(
        documents=stats.document_count, chunks=stats.chunk_count,
        message=f"重建完成：{stats.document_count} 篇 / {stats.chunk_count} 个切块"
                + (f"，{len(failed)} 篇失败" if failed else ""),
    )


__all__ = ["router"]