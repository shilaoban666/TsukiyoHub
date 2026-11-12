# 路由网关与告警

`gateway/` 是一个**独立的 Go 服务**（零第三方依赖），放在所有 LLM 上游前面：

```
你的前端 / TsukiyoHub / 任何 OpenAI 客户端
        │  OpenAI 兼容协议（含 SSE 流式）
        ▼
┌────────────────────────────———─┐
│         tsukiyo-gateway         │
│  鉴权 → 限流 → 预算 → 选路 → 透传  │
│  ├─ 供应商：OpenAI / DeepSeek /   │
│  │   DashScope / Ollama / vLLM / │
│  │   任何 OpenAI 兼容中转站        │
│  ├─ 策略：优先级/加权/轮转/最低延迟 │
│  │   /最便宜/灰度/粘性/能力路由    │
│  ├─ 可靠性：重试+降级、熔断、超时   │
│  └─ 可观测：Prometheus / 追踪 / 告警│
└────────────────────────────────┘
        │                    │
        ▼                    ▼
   /metrics（Prometheus）   /admin/api（TsukiyoHub 前端）
```

为什么用 Go：单二进制、低内存、天然高并发，做流式转发最省心；**不用 Rust** 是因为
单语言运维成本更低、瓶颈在上游模型而不在网关；**不用 eBPF** 是因为网关是应用层（HTTP/SSE）
转发，eBPF 只能做 L4 观测且仅限 Linux——需要时按本文末「可选扩展」接一个 Linux sidecar 即可。

## 1. 快速开始

```powershell
# 1) 起一个假上游（不需要任何 API Key，离线可用）
python scripts\mock_upstream.py --port 18080

# 2) 准备网关配置：把 mock 供应商打开
Copy-Item gateway\config.example.json gateway\config.json
#    编辑 gateway\config.json：
#    - providers 里 mock: enabled=true
#    - routes 保留 "mock-chat"（其它路由可先不管）
#    - admin_token 可留空（只允许本机访问）

# 3) 构建并启动（Windows 下 Go 在工具链目录里）
$env:GOCACHE="$PWD\.gocache"; $env:GOTMPDIR="$PWD\.gotmp"
$env:TEMP="$env:GOTMPDIR"; $env:TMP="$env:GOTMPDIR"
cd gateway
go build -o bin\tsukiyo-gateway.exe .\cmd\tsukiyo-gateway
.\bin\tsukiyo-gateway.exe -config config.json
```

验证：

```powershell
# OpenAI 兼容入口
curl.exe http://127.0.0.1:8081/v1/chat/completions `
  -H "Authorization: Bearer sk-demo-local" -H "Content-Type: application/json" `
  -d '{"model":"mock-chat","messages":[{"role":"user","content":"你好"}],"stream":true}'

# 指标
curl.exe http://127.0.0.1:8082/metrics

# 管理概况
curl.exe http://127.0.0.1:8082/admin/api/overview
```

## 2. 配置说明（`gateway/config.json`）

完整示例见 [`gateway/config.example.json`](../gateway/config.example.json)。关键字段：

| 字段 | 说明 |
|---|---|
| `listen` | 客户端入口，默认 `:8081` |
| `admin_listen` | 管理接口，默认 `:8082`（TsukiyoHub 的 `/api/gateway/*` 代理它） |
| `admin_token` | 管理接口令牌；留空 = 只允许本机访问 |
| `clients[]` | 调用方 Key、允许的路由、RPM/TPM、月预算（USD） |
| `providers[]` | OpenAI 兼容上游：`base_url` / `api_key`（支持 `${ENV}`）/ 超时 / 探活路径 |
| `models[]` | 可路由模型：能力（tools/vision/context）、每 1K token 价格 |
| `routes[]` | 对外模型别名 → 候选 + 策略 + 降级链 |
| `breaker` | 熔断：连续失败次数、打开时长、错误率阈值 |
| `budget` | 预算窗口与超限行为 |
| `tracing` | 请求追踪环形缓冲容量 |
| `alerts` | 告警规则与通知渠道（见第 5 节） |

密钥全部支持 `${ENV_VAR}` 注入，配置文件可以进版本库：

```json
{"providers":[{"name":"openai","base_url":"https://api.openai.com/v1","api_key":"${OPENAI_API_KEY}"}]}
```

### 2.1 加一个供应商（不用改代码）

```json
{
  "providers": [
    {"name": "my-proxy", "type": "openai", "base_url": "https://llm.internal/v1",
     "api_key": "${MY_PROXY_KEY}", "timeout_ms": 60000, "enabled": true}
  ],
  "models": [
    {"id": "internal-70b", "provider": "my-proxy", "upstream": "qwen2.5-72b-instruct",
     "context": 32000, "supports_tools": true, "input_price": 0, "output_price": 0}
  ]
}
```

然后把 `internal-70b` 挂到某个 `routes[].targets` 即可。**改完 PUT 一次配置热生效，不用重启。**

## 3. 选路策略（可插拔）

| 策略 | 用途 | 关键参数 |
|---|---|---|
| `priority` | 主备降级（最常用） | `targets[].priority` 越小越优先 |
| `weighted` | 按比例分流（容量/成本组合） | `targets[].weight` |
| `round_robin` | 均摊 | — |
| `least_latency` | 自动避开慢模型 | 基于最近延迟 EWMA |
| `cost` | 优先便宜模型 | 用 `models[].input_price/output_price` |
| `canary` | 灰度：x% 流量给新模型 | `options.percent` |
| `sticky` | 同一用户/会话固定模型（缓存友好） | 按 `user` 或首条消息哈希 |
| `capability` | 需要 tools / vision / 长上下文时自动筛选 | `required_capabilities` |

新增策略只要实现 `strategy.Strategy` 接口并在注册表登记：

```go
type Strategy interface {
    Name() string
    Order(ctx *PickContext) ([]model.Target, error)
}
```

同一个路由下的候选会逐个尝试：第一个失败（5xx/超时/熔断）→ 自动换下一个，
最多 `max_attempts` 次；全部失败返回 502 并在追踪里记下每次尝试。

## 4. 可观测性

### 4.1 Prometheus

`GET /metrics`（默认在管理端口）暴露标准文本格式：

```
tsukiyo_gateway_requests_total{route,model,provider,status}
tsukiyo_gateway_request_duration_seconds_bucket{route,model,provider,le}
tsukiyo_gateway_ttft_seconds_bucket{model}
tsukiyo_gateway_tokens_total{model,direction}
tsukiyo_gateway_cost_usd_total{model}
tsukiyo_gateway_inflight
tsukiyo_gateway_breaker_state{model}      # 0=closed 1=half_open 2=open
tsukiyo_gateway_provider_up{provider}
tsukiyo_gateway_ratelimit_rejected_total{client}
tsukiyo_gateway_budget_rejected_total{client}
tsukiyo_gateway_alerts_firing
```

配套部署物在 `gateway/deploy/prometheus/` 与 `gateway/deploy/alertmanager/`：

```powershell
# 本机验证
prometheus --config.file=gateway\deploy\prometheus\prometheus.yml
alertmanager --config.file=gateway\deploy\alertmanager\alertmanager.yml
```

### 4.2 请求追踪

每个请求结束都会记录：路由、命中模型、每次尝试、状态码、总耗时、首字延迟、
token、成本、客户端、错误摘要。通过管理接口查询：

```
GET /admin/api/traces?limit=50&route=chat&ok=false
GET /admin/api/traces/{id}
```

内存环形缓冲（默认 2000 条），不落盘、不含请求正文与密钥，可安全用于排障。

## 5. 告警（两条链路，任选或同时用）

### 5.1 网关内置告警引擎（零依赖，开箱即用）

规则写在 `config.json` 的 `alerts.rules`，每 `interval_ms`（默认 15s）评估一次：

```json
{
  "id": "error_rate_high",
  "name": "错误率过高",
  "metric": "error_rate",
  "scope": {"route": "chat"},
  "op": ">",
  "threshold": 0.1,
  "window": "1m",
  "for": "1m",
  "severity": "critical",
  "message": "最近 1 分钟错误率超过 10%",
  "cooldown": "5m",
  "enabled": true
}
```

内置指标：`error_rate` / `p95_latency_ms` / `p50_latency_ms` / `ttft_ms` / `rps` /
`requests` / `error_count` / `tokens` / `cost_usd` / `rate_limited` / `budget_rejected` /
`inflight` / `breaker_open` / `provider_down`。

状态机：`pending`（条件满足但未到 `for`）→ `firing`（发通知）→ `resolved`（恢复，也发通知）。
同一规则在 `cooldown` 内不重复发；`ack` 后可静默一段时间。

通知渠道（可插拔，实现 `Notifier` 接口即可加新的）：

| 类型 | 说明 |
|---|---|
| `log` | 写网关日志（默认，零配置） |
| `webhook` | POST JSON（可自定义 headers），接飞书/企微/自建平台 |
| `dingtalk` | 钉钉机器人 markdown，支持加签 |
| `slack` | Slack Incoming Webhook |

配置示例：

```json
{"notifiers":[
  {"type":"log","name":"console","enabled":true,"min_severity":"warning"},
  {"type":"dingtalk","name":"dingtalk","url":"${DINGTALK_ROBOT_WEBHOOK}",
   "secret":"${DINGTALK_ROBOT_SECRET}","enabled":true,"min_severity":"critical"}
]}
```

管理接口：

```
GET /admin/api/alerts/overview
GET /admin/api/alerts/rules
PUT /admin/api/alerts/rules          # 热更新
GET /admin/api/alerts/events?status=firing
POST /admin/api/alerts/{id}/ack      # body {"by":"web","minutes":30}
```

### 5.2 外部 Prometheus + Alertmanager

`gateway/deploy/prometheus/alerts.yml` 是等价的 PromQL 规则（错误率、P95、熔断、供应商不可用等），
适合已有 Prometheus/Alertmanager 的团队统一告警。详见 [`gateway/docs/alerts.md`](../gateway/docs/alerts.md)。

## 6. 与 TsukiyoHub 的关系

两种用法：

1. **只用网关**：把 `LLM_BASE_URL` 指向 `http://127.0.0.1:8081/v1`，`LLM_API_KEY` 填 `clients[]` 里的 key，
   TsukiyoHub 的聊天内核自动获得路由/降级/熔断/限流能力。
2. **带管理界面**：TsukiyoHub 后端提供 `/api/gateway/*` 代理（读 `GATEWAY_ADMIN_URL`/`GATEWAY_ADMIN_TOKEN`），
   前端「路由网关」「告警」两个页面直接管理网关。

即使网关没启动，TsukiyoHub 也能正常工作（`/api/gateway/health` 返回 `reachable:false`，页面显示空态）。

## 7. 可扩展点清单

| 想加什么 | 改哪里 | 要不要改核心代码 |
|---|---|---|
| 新供应商 | `config.json` 的 `providers/models` | 不要 |
| 新路由策略 | 新文件实现 `strategy.Strategy` + 注册 | 只加文件 |
| 新告警指标 | `internal/metrics.Aggregator` 增加派生 + `internal/alert` 映射 | 小改 |
| 新通知渠道 | 实现 `alert.Notifier` + 注册 | 只加文件 |
| 新中间件（如审计/脱敏） | 实现 `middleware.Guard` + 挂进 Chain | 只加文件 |
| 换向量/超时/HTTP 客户端 | `internal/provider` | 只改一个包 |
| L4 观测 | Linux sidecar（eBPF/BCC）读 socket 指标，写 Prometheus remote-write | 不需改网关 |

## 8. 已知边界

- 追踪在内存里（默认 2000 条），高 QPS 下只看最近一段；长期分析请接 Prometheus。
- 预算/限流计数是**进程内**的；多副本部署时每个副本各自计数（要全局配额请用 Redis 或网关前置）。
- 流式转发是「边收边发」；客户端提前断开时网关会立即关闭上游连接，不继续计费。
- 网关只做协议透传与路由，不做内容审核；需要的话用 `middleware.Guard` 加一个审核 Guard。