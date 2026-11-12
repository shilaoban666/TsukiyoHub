"""路由网关冒烟测试：验证「路由 / 降级 / 熔断 / 限流 / 预算 / 指标 / 追踪 / 告警 / 热更新」。

前置：起一个假上游与网关（见 docs/gateway.md「快速开始」）：

    python scripts/mock_upstream.py --port 18080
    gateway\\bin\\tsukiyo-gateway.exe -config gateway\\config.test.json

然后：

    python scripts/gateway_smoke.py --base http://127.0.0.1:18081 --admin http://127.0.0.1:18082 \
        --key sk-demo-local --limited-key sk-limited --budget-key sk-budget \
        --sink http://127.0.0.1:18080/sink

退出码 0 = 全部通过。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx

OK = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"


def _delta_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _items(payload: Any, *keys: str) -> list:
    """兼容 ``{items}`` / ``{traces}`` / ``{events}`` / ``{rules}`` / 裸数组五种返回形状。"""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("items", *keys):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


class GatewaySmoke:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.client = httpx.Client(timeout=60.0)
        self.failures: list[str] = []
        self.fired_alerts: list[dict] = []

    # ------------------------------------------------------------------ 工具
    def _headers(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _admin(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self.client.request(method, f"{self.args.admin}{path}", **kwargs)

    def chat(self, model: str, content: str = "你好", *, key: str | None = None,
             stream: bool = False) -> httpx.Response:
        return self.client.post(
            f"{self.args.base}/v1/chat/completions",
            headers=self._headers(key or self.args.key),
            json={"model": model, "messages": [{"role": "user", "content": content}], "stream": stream},
        )

    def check(self, name: str, fn) -> None:
        started = time.perf_counter()
        try:
            detail = fn()
            print(f"  {OK} {name}  ({int((time.perf_counter() - started) * 1000)}ms)  {detail or ''}")
        except Exception as exc:  # noqa: BLE001
            self.failures.append(f"{name}: {exc}")
            print(f"  {FAIL} {name}  {exc}")

    # ------------------------------------------------------------------ 各项
    def healthz(self) -> str:
        data = self.client.get(f"{self.args.base}/healthz").raise_for_status().json()
        return f"status={data.get('status')}"

    def models(self) -> str:
        data = self.client.get(f"{self.args.base}/v1/models", headers=self._headers(self.args.key)) \
            .raise_for_status().json()
        names = [item["id"] for item in data.get("data") or []]
        if "chat" not in names:
            raise RuntimeError(f"/v1/models 缺少路由别名：{names}")
        return f"路由别名 {len(names)} 个"

    def chat_non_stream(self) -> str:
        response = self.chat("chat")
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        body = response.json()
        text = _delta_text(body["choices"][0]["message"]["content"])
        usage = body.get("usage") or {}
        if "[mock:mock-chat]" not in text:
            raise RuntimeError(f"回答不是假上游的：{text[:80]}")
        if int(usage.get("total_tokens") or 0) <= 0:
            raise RuntimeError("usage 为空（网关没有解析用量）")
        return f"{len(text)} 字，tokens={usage.get('total_tokens')}"

    def chat_stream(self) -> str:
        chunks: list[str] = []
        usage = 0
        saw_done = False
        with self.client.stream(
            "POST", f"{self.args.base}/v1/chat/completions", headers=self._headers(self.args.key),
            json={"model": "chat", "messages": [{"role": "user", "content": "流式你好"}], "stream": True},
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    saw_done = True
                    break
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                for choice in event.get("choices") or []:
                    chunks.append(_delta_text(choice.get("delta", {}).get("content")))
                if event.get("usage"):
                    usage = int(event["usage"].get("total_tokens") or 0)
        text = "".join(chunks)
        if not saw_done:
            raise RuntimeError("没有收到 [DONE]")
        if "[mock:mock-chat]" not in text:
            raise RuntimeError(f"流式内容异常：{text[:80]}")
        if usage <= 0:
            raise RuntimeError("流式响应没有解析到 usage")
        return f"{len(chunks)} 个 chunk，{len(text)} 字，tokens={usage}"

    def failover(self) -> str:
        response = self.chat("failover")
        if response.status_code != 200:
            raise RuntimeError(f"降级后仍失败 HTTP {response.status_code}: {response.text[:200]}")
        text = _delta_text(response.json()["choices"][0]["message"]["content"])
        if "[mock:mock-chat]" not in text:
            raise RuntimeError(f"没有切到 fallback 模型：{text[:80]}")
        return "主模型失败 → 自动切到 fallback"

    def breaker(self) -> str:
        # 先打一批必失败的请求，触发熔断（failure_threshold=2）
        for _ in range(4):
            self.chat("failover")
        time.sleep(0.5)
        overview = self._admin("GET", "/admin/api/overview").raise_for_status().json()
        breakers = overview.get("breakers") or {}
        if not breakers:
            raise RuntimeError("overview 里没有熔断状态")
        if not any(state in ("open", "half_open") for state in breakers.values()):
            raise RuntimeError(f"熔断器没有打开：{breakers}")
        return f"熔断状态 {breakers}"

    def rate_limit(self) -> str:
        statuses = [self.chat("chat", key=self.args.limited_key).status_code for _ in range(5)]
        if 429 not in statuses:
            raise RuntimeError(f"没有触发 429（实际：{statuses}）")
        return f"状态序列 {statuses}"

    def budget(self) -> str:
        first = self.chat("chat", key=self.args.budget_key)
        second = self.chat("chat", key=self.args.budget_key)
        statuses = (first.status_code, second.status_code)
        if 402 not in statuses:
            raise RuntimeError(f"没有触发预算拒绝（实际：{statuses}）")
        return f"状态序列 {statuses}"

    def metrics(self) -> str:
        time.sleep(0.2)  # 给「刚结束的流式请求」一点写指标的时间，避免偶发抢跑
        text = self.client.get(f"{self.args.admin}/metrics").raise_for_status().text
        required = [
            "tsukiyo_gateway_requests_total",
            "tsukiyo_gateway_request_duration_seconds_bucket",
            "tsukiyo_gateway_ttft_seconds",
            "tsukiyo_gateway_tokens_total",
            "tsukiyo_gateway_cost_usd_total",
            "tsukiyo_gateway_breaker_state",
            "tsukiyo_gateway_provider_up",
        ]
        missing = [name for name in required if name not in text]
        if missing:
            raise RuntimeError(f"缺少指标：{missing}")
        return f"{len(text.splitlines())} 行指标文本"

    def tracing(self) -> str:
        data = self._admin("GET", "/admin/api/traces", params={"limit": 20}).raise_for_status().json()
        items = _items(data, "traces")
        if not items:
            raise RuntimeError("追踪为空")
        detail = self._admin("GET", f"/admin/api/traces/{items[0]['id']}").raise_for_status().json()
        if not detail.get("id"):
            raise RuntimeError("追踪详情缺失")
        return f"{len(items)} 条记录，最近：{items[0].get('route')} → {items[0].get('model')}"

    def stats(self) -> str:
        data = self._admin("GET", "/admin/api/stats", params={"window": 300}).raise_for_status().json()
        if int(data.get("requests") or 0) <= 0:
            raise RuntimeError(f"窗口统计为空：{data}")
        return f"requests={data['requests']} error_rate={data.get('error_rate'):.3f} p95={data.get('p95_ms')}ms"

    def alerts(self) -> str:
        if not self.args.sink:
            return "跳过（未提供 --sink）"
        time.sleep(3.5)  # 等一个评估周期（网关 config.test.json 里 1.5s）
        events = self._admin("GET", "/admin/api/alerts/events", params={"limit": 50}).raise_for_status().json()
        items = _items(events, "events")
        firing = [item for item in items if item.get("status") in ("firing", "pending")]
        if not firing:
            raise RuntimeError(f"没有产生告警事件：{items[:2]}")
        sink = self.client.get(self.args.sink).raise_for_status().json()
        if int(sink.get("count") or 0) <= 0:
            raise RuntimeError("告警事件产生了，但 webhook 没收到通知")
        return f"{len(items)} 条事件，{sink['count']} 条通知已送达 webhook"

    def ack_alert(self) -> str:
        events = self._admin("GET", "/admin/api/alerts/events", params={"limit": 20}).raise_for_status().json()
        firing = [item for item in _items(events, "events") if item.get("status") == "firing"]
        if not firing:
            return "跳过（没有 firing 事件）"
        alert_id = firing[0]["id"]
        self._admin("POST", f"/admin/api/alerts/{alert_id}/ack",
                    json={"by": "smoke", "minutes": 15}).raise_for_status()
        return f"已确认 {alert_id}"

    def hot_reload(self) -> str:
        current = self._admin("GET", "/admin/api/config").raise_for_status().json()
        config = current.get("config") or {}
        routes = list(config.get("routes") or [])
        if any(route.get("alias") == "hot-route" for route in routes):
            routes = [route for route in routes if route.get("alias") != "hot-route"]
        routes.append({
            "alias": "hot-route",
            "strategy": "priority",
            "targets": [{"model": "mock-chat", "priority": 1}],
            "enabled": True,
        })
        config["routes"] = routes
        self._admin("PUT", "/admin/api/config", json=config).raise_for_status()
        time.sleep(0.5)
        response = self.chat("hot-route")
        if response.status_code != 200:
            raise RuntimeError(f"热更新后新路由不可用：HTTP {response.status_code} {response.text[:200]}")
        return "PUT 配置后新路由立即可用"

    def config_redacted(self) -> str:
        data = self._admin("GET", "/admin/api/config").raise_for_status().json()
        config = data.get("config") or {}
        keys = [item.get("key", "") for item in config.get("clients") or []]
        if any(key and "****" not in key and len(key) > 12 for key in keys):
            raise RuntimeError("配置里的 client key 没有脱敏")
        return f"client keys 已脱敏：{keys[:2]}"

    def run(self) -> int:
        print(f"\n网关冒烟 → {self.args.base}（admin {self.args.admin}）\n")
        self.check("健康检查 /healthz", self.healthz)
        self.check("模型列表 /v1/models", self.models)
        self.check("非流式问答", self.chat_non_stream)
        self.check("流式问答（SSE）", self.chat_stream)
        self.check("失败降级（主模型 500 → fallback）", self.failover)
        self.check("熔断器打开", self.breaker)
        self.check("限流 429", self.rate_limit)
        self.check("预算 402", self.budget)
        self.check("Prometheus 指标", self.metrics)
        self.check("请求追踪", self.tracing)
        self.check("窗口统计", self.stats)
        self.check("配置脱敏", self.config_redacted)
        self.check("告警事件 + webhook 通知", self.alerts)
        self.check("告警确认 Ack", self.ack_alert)
        self.check("配置热更新", self.hot_reload)

        print()
        if self.failures:
            print(f"{FAIL} {len(self.failures)} 项失败：")
            for item in self.failures:
                print(f"   - {item}")
            return 1
        print(f"{OK} 全部通过")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="路由网关冒烟测试")
    parser.add_argument("--base", default="http://127.0.0.1:18081", help="网关入口")
    parser.add_argument("--admin", default="http://127.0.0.1:18082", help="网关管理接口")
    parser.add_argument("--key", default="sk-demo-local", help="主测试 key")
    parser.add_argument("--limited-key", default="sk-limited", help="低配额 key（验证 429）")
    parser.add_argument("--budget-key", default="sk-budget", help="低预算 key（验证 402）")
    parser.add_argument("--sink", default="", help="告警 webhook 接收端 URL（验证通知链路）")
    args = parser.parse_args()
    sys.exit(GatewaySmoke(args).run())


if __name__ == "__main__":
    main()