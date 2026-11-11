"""冒烟脚本：服务启动后验证「聊天 / 流式 / 知识库 / 技能 / 会话」全链路。

用法::

    python scripts/smoke.py                       # 默认 http://127.0.0.1:8080
    python scripts/smoke.py --base http://host:port --api-key xxx

退出码 0 = 全部通过；非 0 = 有失败项（同时打印失败原因）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

OK = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"


class Checker:
    def __init__(self, base: str, api_key: str = "") -> None:
        self.base = base.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base,
            timeout=120.0,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        )
        self.failures: list[str] = []

    def check(self, name: str, fn) -> None:
        started = time.perf_counter()
        try:
            detail = fn()
            cost = int((time.perf_counter() - started) * 1000)
            print(f"  {OK} {name}  ({cost}ms)  {detail or ''}")
        except Exception as exc:  # noqa: BLE001
            self.failures.append(f"{name}: {exc}")
            print(f"  {FAIL} {name}  {exc}")

    # ---------------------------------------------------------------- 各项
    def health(self) -> str:
        data = self.client.get("/healthz").raise_for_status().json()
        return f"status={data['status']} llm_ready={data['llm_ready']} docs={data['document_count']}"

    def info(self) -> str:
        data = self.client.get("/api/system/info").raise_for_status().json()
        return f"{data['app']} v{data['version']} vector={data['vector_store']} skills={data['skills']}"

    def knowledge_stats(self) -> str:
        data = self.client.get("/api/knowledge/stats").raise_for_status().json()
        return f"documents={data['document_count']} chunks={data['chunk_count']} backend={data['vector_store']}"

    def upload_demo(self) -> str:
        """把 examples/knowledge 里的示例文档传上去（幂等：内容不变会跳过）。"""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "examples" / "knowledge"
        files = sorted(root.glob("*.md"))
        if not files:
            raise RuntimeError(f"{root} 下没有示例文档")
        uploaded = 0
        for path in files:
            with path.open("rb") as handle:
                response = self.client.post(
                    "/api/knowledge/upload", files={"file": (path.name, handle, "text/markdown")}
                )
            response.raise_for_status()
            body = response.json()
            uploaded += int(body.get("count") or 0)
        return f"上传 {len(files)} 个文件，入库 {uploaded} 篇"

    def search(self) -> str:
        data = self.client.post(
            "/api/knowledge/search", json={"query": "检索是怎么工作的", "top_k": 3}
        ).raise_for_status().json()
        hits = data.get("hits") or []
        if not hits:
            raise RuntimeError("检索没有任何命中（知识库可能为空）")
        return f"命中 {len(hits)} 条，top1={hits[0]['title']} score={hits[0]['score']:.4f}"

    def chat(self) -> str:
        data = self.client.post(
            "/api/chat",
            json={"question": "TsukiyoHub 的检索是怎么工作的？", "user_id": "smoke"},
        ).raise_for_status().json()
        if not data.get("answer"):
            raise RuntimeError("answer 为空")
        return f"session={data['session_id'][:8]} grounded={data['grounded']} citations={len(data['citations'])}"

    def stream(self) -> str:
        events: list[str] = []
        answer = ""
        with self.client.stream(
            "POST",
            "/api/chat/stream",
            json={"question": "支持哪些文档格式？", "user_id": "smoke"},
            headers={"Accept": "text/event-stream"},
        ) as response:
            response.raise_for_status()
            event = "message"
            data_lines: list[str] = []
            for line in response.iter_lines():
                if line.startswith(":"):
                    continue
                if not line:
                    if event == "delta":
                        answer = "\n".join(data_lines)
                        events.append("delta")
                    elif event in ("meta", "done", "error"):
                        events.append(event)
                    event, data_lines = "message", []
                    continue
                field, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "event":
                    event = value
                elif field == "data":
                    data_lines.append(value)
        if "done" not in events:
            raise RuntimeError(f"没有收到 done 事件（收到：{events[:8]}）")
        if not answer.strip():
            raise RuntimeError("流式回答为空")
        return f"事件序列={events[:6]}… 回答 {len(answer)} 字"

    def sessions(self) -> str:
        data = self.client.get("/api/sessions", params={"limit": 5}).raise_for_status().json()
        if not data.get("items"):
            raise RuntimeError("会话列表为空（聊天没落库？）")
        session = data["items"][0]
        detail = self.client.get(f"/api/sessions/{session['id']}/messages").raise_for_status().json()
        return f"{data['total']} 个会话，最近一个有 {len(detail['messages'])} 条消息"

    def skills(self) -> str:
        data = self.client.get("/api/skills").raise_for_status().json()
        names = [item["name"] for item in data.get("items") or []]
        if "knowledge_search" not in names:
            raise RuntimeError(f"缺少 knowledge_search 技能（当前：{names}）")
        return f"{len(names)} 个技能：{', '.join(names[:6])}"

    def config(self) -> str:
        data = self.client.get("/api/system/config").raise_for_status().json()
        return f"model={data['model']} vector={data['vector_backend']} tone={data['style']['tone']}"

    def openai_compatible(self) -> str:
        data = self.client.post(
            "/api/v1/chat/completions",
            json={"model": "any", "messages": [{"role": "user", "content": "你好"}]},
        ).raise_for_status().json()
        content = data["choices"][0]["message"]["content"]
        if not content:
            raise RuntimeError("OpenAI 兼容接口返回空内容")
        return f"返回 {len(content)} 字"

    def run(self) -> int:
        print(f"\n冒烟测试 → {self.base}\n")
        self.check("健康检查 /healthz", self.health)
        self.check("服务信息 /api/system/info", self.info)
        self.check("上传示例文档 /api/knowledge/upload", self.upload_demo)
        self.check("知识库统计 /api/knowledge/stats", self.knowledge_stats)
        self.check("检索调试 /api/knowledge/search", self.search)
        self.check("技能列表 /api/skills", self.skills)
        self.check("问答 /api/chat", self.chat)
        self.check("流式问答 /api/chat/stream", self.stream)
        self.check("会话历史 /api/sessions", self.sessions)
        self.check("运行时配置 /api/system/config", self.config)
        self.check("OpenAI 兼容 /api/v1/chat/completions", self.openai_compatible)

        print()
        if self.failures:
            print(f"{FAIL} {len(self.failures)} 项失败：")
            for item in self.failures:
                print(f"   - {item}")
            return 1
        print(f"{OK} 全部通过")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="TsukiyoHub 冒烟测试")
    parser.add_argument("--base", default="http://127.0.0.1:8080", help="服务地址")
    parser.add_argument("--api-key", default="", help="API_KEY（服务端配置了才需要）")
    args = parser.parse_args()
    sys.exit(Checker(args.base, args.api_key).run())


if __name__ == "__main__":
    main()