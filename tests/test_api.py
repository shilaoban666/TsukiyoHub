"""端到端接口测试：不连任何外部服务，覆盖「任何前端接入」需要的全部端点。"""

from __future__ import annotations

import json


def test_healthz(client):
    data = client.get("/healthz").json()
    assert data["app"]
    assert data["status"] in ("ok", "degraded")
    assert isinstance(data["document_count"], int)


def test_readyz_allows_empty_knowledge(client):
    response = client.get("/readyz")
    assert response.status_code in (200, 503)
    assert "ready" in response.json()


def test_system_info(client):
    data = client.get("/api/system/info").json()
    assert data["app"]
    assert data["vector_store"] == "memory"
    assert "knowledge" in data


def test_system_config_roundtrip(client):
    before = client.get("/api/system/config").json()
    assert before["style"]["tone"]

    updated = client.put("/api/system/config", json={"default_tone": "formal", "retrieval_top_k": 6}).json()
    assert updated["style"]["tone"] == "formal"
    assert updated["retrieval"]["top_k"] == 6

    # 还原，避免影响其它测试
    client.put("/api/system/config", json={"default_tone": before["style"]["tone"],
                                           "retrieval_top_k": before["retrieval"]["top_k"]})


def test_persona_endpoint(client):
    data = client.get("/api/system/persona").json()
    assert "persona_prompt" in data and "bot_name" in data


def test_upload_and_stats(client, sample_docs):
    stats = client.get("/api/knowledge/stats").json()
    assert stats["document_count"] >= 2
    assert stats["chunk_count"] >= 2
    assert stats["vector_store"] == "memory"


def test_upload_via_api(client):
    files = {"file": ("测试上传.md", "# 测试上传\n\n这是一篇通过接口上传的文档，用于验证上传链路。".encode(), "text/markdown")}
    data = client.post("/api/knowledge/upload", files=files).json()
    assert data["count"] == 1
    assert data["results"][0]["ok"] is True


def test_document_list_and_delete(client, sample_docs):
    listing = client.get("/api/knowledge/documents").json()
    assert listing["total"] >= 2
    doc_id = listing["items"][0]["doc_id"]

    removed = client.delete(f"/api/knowledge/documents/{doc_id}").json()
    assert removed["removed_chunks"] >= 1

    listing = client.get("/api/knowledge/documents").json()
    assert all(item["doc_id"] != doc_id for item in listing["items"])


def test_search(client, sample_docs):
    data = client.post("/api/knowledge/search", json={"query": "RRF 融合", "top_k": 3}).json()
    assert data["query"] == "RRF 融合"
    assert data["latency_ms"] >= 0
    assert isinstance(data["hits"], list)


def test_skills_list(client):
    data = client.get("/api/skills").json()
    names = [item["name"] for item in data["items"]]
    assert "knowledge_search" in names
    assert all("description" in item for item in data["items"])


def test_skill_toggle(client):
    data = client.patch("/api/skills/web_fetch", json={"enabled": False}).json()
    assert data["enabled"] is False
    data = client.patch("/api/skills/web_fetch", json={"enabled": True}).json()
    assert data["enabled"] is True


def test_chat_basic(client):
    data = client.post("/api/chat", json={"question": "你好", "user_id": "t-chat"}).json()
    assert data["answer"]
    assert data["session_id"]


def test_chat_knowledge_and_session(client, sample_docs):
    response = client.post("/api/chat", json={
        "question": "TsukiyoHub 的检索是怎么融合的？", "user_id": "t-kb",
    }).json()
    assert response["answer"]
    session_id = response["session_id"]

    history = client.get(f"/api/sessions/{session_id}/messages").json()
    assert history["session"]["id"] == session_id
    assert len(history["messages"]) >= 2
    assert history["messages"][0]["role"] == "user"

    sessions = client.get("/api/sessions", params={"user_id": "t-kb"}).json()
    assert sessions["total"] >= 1
    assert sessions["items"][0]["id"] == session_id


def test_session_rename_and_delete(client):
    created = client.post("/api/sessions", json={"user_id": "t-sess", "channel": "web"}).json()
    session_id = created["id"]

    renamed = client.patch(f"/api/sessions/{session_id}", json={"title": "新标题"}).json()
    assert renamed["title"] == "新标题"

    assert client.delete(f"/api/sessions/{session_id}").json()["ok"] is True
    assert client.get(f"/api/sessions/{session_id}").status_code == 404


def test_chat_stream_sse(client):
    """SSE 必须能拿到 delta / meta / done，且 delta 是累积全文。"""
    events: list[tuple[str, str]] = []
    with client.stream("POST", "/api/chat/stream", json={"question": "你好", "user_id": "t-stream"}) as response:
        assert response.status_code == 200
        event, data_lines = "message", []
        for line in response.iter_lines():
            if line.startswith(":"):
                continue
            if not line:
                if event != "message":
                    events.append((event, "\n".join(data_lines)))
                event, data_lines = "message", []
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "event":
                event = value
            elif field == "data":
                data_lines.append(value)

    kinds = [kind for kind, _ in events]
    assert "delta" in kinds
    assert "meta" in kinds
    assert kinds[-1] == "done"

    meta = json.loads(next(value for kind, value in events if kind == "meta"))
    assert meta["session_id"]
    assert "citations" in meta

    # delta 是「当前累积全文」：每帧都是 JSON 字符串，先解码再比较
    deltas = [json.loads(value) for kind, value in events if kind == "delta"]
    assert deltas, "没有收到任何 delta"
    assert deltas[-1].startswith(deltas[0])
    assert deltas[-1] == deltas[-1].strip() or deltas[-1].strip()


def test_openai_compatible(client):
    data = client.post("/api/v1/chat/completions", json={
        "model": "ignored", "messages": [{"role": "user", "content": "你好"}],
    }).json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"]

    models = client.get("/api/v1/models").json()
    assert models["data"][0]["id"]


def test_memory_endpoints(client):
    data = client.get("/api/memory/t-memory").json()
    assert data["user_id"] == "t-memory"
    assert isinstance(data["items"], list)

    cleared = client.delete("/api/memory/t-memory").json()
    assert cleared["ok"] is True


def test_feedback(client):
    data = client.post("/api/feedback", json={"session_id": "s", "message_id": "m", "rating": 1}).json()
    assert data["ok"] is True