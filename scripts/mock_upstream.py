"""假上游：OpenAI 兼容的最小服务，用于验证路由网关（不需要任何 API Key、不联网）。

用法::

    python scripts/mock_upstream.py --port 18080

支持：
* ``GET  /v1/models``
* ``POST /v1/chat/completions``（``stream=true`` 时按 SSE 逐字返回）
* 故障注入（验证熔断 / 降级）：
  - 请求体 ``model`` 为 ``mock-fail``：固定返回 500；
  - 请求体 ``model`` 为 ``mock-slow``：延迟 ``--slow-ms``（默认 3000ms）后返回；
  - 请求头 ``X-Mock-Fail-First: N`` 或环境变量 ``MOCK_FAIL_FIRST``：前 N 次请求返回 500；
  - 请求头 ``X-Mock-No-Usage: 1``：不返回 usage（验证网关的 token 估算兜底）。
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_FAIL_LOCK = threading.Lock()
_ATTEMPTS = {"n": 0}
_STATE = {"slow_ms": 3000, "version": "mock-1", "sink": []}


def _reply_text(model: str, prompt: str) -> str:
    return f"[mock:{model}] 收到 {len(prompt)} 字提问。这是假上游的固定回答，用于验证路由与流式链路。"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------------- 工具
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - 覆盖基类
        if os.environ.get("MOCK_VERBOSE"):
            super().log_message(fmt, *args)

    # ---------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self.path.startswith("/v1/models"):
            self._send_json(200, {
                "object": "list",
                "data": [
                    {"id": "mock-chat", "object": "model", "owned_by": "mock"},
                    {"id": "mock-fail", "object": "model", "owned_by": "mock"},
                    {"id": "mock-slow", "object": "model", "owned_by": "mock"},
                ],
            })
            return
        if self.path in ("/health", "/healthz"):
            self._send_json(200, {"status": "ok"})
            return
        if self.path == "/sink":
            # 告警 webhook 接收端：GET 取回最近收到的通知（验证通知链路用）
            self._send_json(200, {"count": len(_STATE["sink"]), "items": _STATE["sink"][-20:]})
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/sink":
            payload = self._read_json()
            _STATE["sink"].append(payload)
            self._send_json(200, {"ok": True})
            return
        if not self.path.startswith("/v1/chat/completions"):
            self._send_json(404, {"error": {"message": "not found"}})
            return

        payload = self._read_json()
        model = str(payload.get("model") or "mock-chat")
        messages = payload.get("messages") or []
        prompt = "".join(
            (m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content"), ensure_ascii=False))
            for m in messages
            if isinstance(m, dict)
        )
        stream = bool(payload.get("stream"))

        # ---- 故障注入 ----
        fail_first = int(self.headers.get("X-Mock-Fail-First") or os.environ.get("MOCK_FAIL_FIRST") or "0")
        with _FAIL_LOCK:
            _ATTEMPTS["n"] += 1
            attempt = _ATTEMPTS["n"]
        if model == "mock-fail" or (fail_first and attempt <= fail_first):
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": f"mock failure (attempt {attempt})"}})
            return
        if model == "mock-slow":
            time.sleep(_STATE["slow_ms"] / 1000)

        text = _reply_text(model, prompt)
        prompt_tokens = max(1, len(prompt) // 2)
        completion_tokens = len(text)

        if not stream:
            body: dict = {
                "id": f"chatcmpl-mock-{attempt}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }],
            }
            if not self.headers.get("X-Mock-No-Usage"):
                body["usage"] = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                }
            self._send_json(200, body)
            return

        # ---- SSE 流式 ----
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        chunk_id = f"chatcmpl-mock-{attempt}"
        try:
            for ch in text:
                event = {
                    "id": chunk_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "delta": {"content": ch}, "finish_reason": None}],
                }
                self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.005)
            if not self.headers.get("X-Mock-No-Usage"):
                usage_event = {
                    "id": chunk_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                }
                self.wfile.write(f"data: {json.dumps(usage_event, ensure_ascii=False)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return  # 客户端提前断开（验证网关的中断透传）


def main() -> None:
    parser = argparse.ArgumentParser(description="假 OpenAI 上游（验证路由网关用）")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--slow-ms", type=int, default=3000, help="model=mock-slow 时的延迟")
    args = parser.parse_args()
    _STATE["slow_ms"] = args.slow_ms

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"假上游已启动：http://{args.host}:{args.port}/v1（Ctrl+C 退出）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()