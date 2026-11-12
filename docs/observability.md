# 可观测性：Prometheus 接入参考表

这份文档回答三个问题：

1. **接哪些指标**（表格：名字 / 类型 / 标签 / 含义 / 典型 PromQL）；
2. **怎么接**（抓取配置、记录规则、告警规则、Grafana 看板，一键起）；
3. **AI infra / agent infra 场景怎么讲**（TTFT / TPOT / tokens-s / 成本、agent 工具可观测、SLO 燃烧率）。

> 两个服务都暴露**标准 Prometheus 文本格式**（手写 exposition，零第三方依赖）：
> * 网关（Go）`http://127.0.0.1:8082/metrics`
> * 聊天内核（Python FastAPI）`http://127.0.0.1:8080/metrics`

---

## 0. 一键起监控栈

```powershell
# Prometheus(9090) + Alertmanager(9093) + Grafana(3000)
docker compose --profile monitor up -d

# 本机直接跑（不装 Docker）：
prometheus --config.file=gateway\deploy\prometheus\prometheus.yml
```

| 地址 | 用途 |
|---|---|
| <http://127.0.0.1:9090/targets> | 确认两个 job 都 `UP` |
| <http://127.0.0.1:9090/graph> | 临时跑 PromQL（本文下面的表达式都能直接贴） |
| <http://127.0.0.1:9090/alerts> | 看告警状态（pending / firing） |
| <http://127.0.0.1:3000> | Grafana，看板 `TsukiyoHub · AI Gateway & Agent Infra` |

配置文件：

| 文件 | 作用 |
|---|---|
| `gateway/deploy/prometheus/prometheus.yml` | 抓取两个服务 + 引用规则 |
| `gateway/deploy/prometheus/recording_rules.yml` | 记录规则：tokens/s、TPOT、P95、成本、SLO 燃烧率 |
| `gateway/deploy/prometheus/alerts.yml` | 告警规则（网关 + 内核 + SLO） |
| `gateway/deploy/alertmanager/alertmanager.yml` | 告警路由（critical / warning 分流） |
| `gateway/deploy/grafana/` | 看板 JSON + 自动装配 |

---

## 1. 网关指标（`job="tsukiyo-gateway"`）

### 1.1 指标清单

| 指标 | 类型 | 标签 | 含义 | 典型 PromQL |
|---|---|---|---|---|
| `tsukiyo_gateway_requests_total` | counter | `route, model, provider, status` | 请求数 | `sum by (route) (rate(tsukiyo_gateway_requests_total[1m]))` |
| `tsukiyo_gateway_request_duration_seconds` | histogram | `route, model, provider` | 端到端耗时（秒） | `histogram_quantile(0.95, sum by (le,route) (rate(..._bucket[5m])))` |
| `tsukiyo_gateway_ttft_seconds` | histogram | `route, model, provider` | **流式首字延迟** | `histogram_quantile(0.95, sum by (le,route) (rate(..._bucket[5m])))` |
| `tsukiyo_gateway_tokens_total` | counter | `model, direction` | token 用量（prompt/completion） | `sum by (direction) (rate(...[1m]))` |
| `tsukiyo_gateway_cost_usd_total` | counter | `model` | 费用（美元） | `3600 * sum(rate(...[5m]))`（USD/小时） |
| `tsukiyo_gateway_inflight` | gauge | — | 当前在途请求 | `tsukiyo_gateway_inflight` |
| `tsukiyo_gateway_breaker_state` | gauge | `model` | 熔断状态 0=closed 1=half_open 2=open | `max by (model) (tsukiyo_gateway_breaker_state)` |
| `tsukiyo_gateway_provider_up` | gauge | `provider` | 供应商探活 1/0 | `min by (provider) (tsukiyo_gateway_provider_up)` |
| `tsukiyo_gateway_provider_probe_seconds` | histogram | `provider` | 探活耗时 | `avg by (provider) (rate(_sum[5m])/rate(_count[5m]))` |
| `tsukiyo_gateway_ratelimit_rejected_total` | counter | `client` | 被限流数（429） | `sum(rate(...[5m]))` |
| `tsukiyo_gateway_budget_rejected_total` | counter | `client` | 被预算拒绝数（402） | `sum(increase(...[1h]))` |
| `tsukiyo_gateway_alerts_firing` | gauge | — | 内置告警引擎 firing 数 | `tsukiyo_gateway_alerts_firing` |
| `tsukiyo_gateway_uptime_seconds` | gauge | — | 进程运行时长 | `time() - tsukiyo_gateway_uptime_seconds`（启动时刻） |

### 1.2 三个「AI 特有」的口径

| 指标 | 公式 | 为什么重要 |
|---|---|---|
| **tokens/s** | `sum(rate(tsukiyo_gateway_tokens_total{direction="completion"}[1m]))` | 吞吐的真实单位。RPS 相同不代表吞吐相同（长回答 vs 短回答差 100 倍） |
| **TPOT**（Time Per Output Token） | `sum(rate(duration_sum[5m])) / sum(rate(completion_tokens[5m]))` | 每个输出 token 的耗时，跨模型可比；首字延迟之外的「生成速度」 |
| **单位成本** | `cost / 1M output tokens` | FinOps 口径：便宜模型不一定省钱（质量差导致重试） |

记录规则里已固化为 `tsukiyo_gateway:output_tokens_per_second`、`...:tpot_seconds_avg`、
`...:cost_usd_per_1m_output_tokens`，看板直接用即可。

---

## 2. 聊天 / 技能 / RAG 内核指标（`job="tsukiyohub-core"`）

### 2.1 指标清单

| 指标 | 类型 | 标签 | 含义 | 典型 PromQL |
|---|---|---|---|---|
| `tsukiyohub_http_requests_total` | counter | `method, path, status` | HTTP 请求（path 用路由模板，基数可控） | `sum by (path,status) (rate(...[5m]))` |
| `tsukiyohub_http_request_duration_seconds` | histogram | `path` | HTTP 耗时 | `histogram_quantile(0.95, sum by (le,path) (rate(...)))` |
| `tsukiyohub_chat_requests_total` | counter | `intent, grounded, degraded` | 问答数（是否「有据」/「降级」） | `sum by (grounded) (rate(...[5m]))` |
| `tsukiyohub_chat_duration_seconds` | histogram | `intent` | 一次问答总耗时 | `histogram_quantile(0.95, sum by (le,intent) (...))` |
| `tsukiyohub_sse_first_token_seconds` | histogram | — | SSE 首字延迟（流式体验） | `histogram_quantile(0.95, sum by (le) (...))` |
| `tsukiyohub_llm_duration_seconds` | histogram | `model` | 模型调用累计耗时 | `sum by (model) (rate(_sum[5m])) / sum by (model) (rate(_count[5m]))` |
| `tsukiyohub_retrieval_duration_seconds` | histogram | — | 检索耗时 | P95 直接看 |
| `tsukiyohub_retrieval_hits` | histogram | — | 单次检索命中片段数 | 命中 <1 的占比 = 检索「空手而归」比例 |
| `tsukiyohub_tokens_total` | counter | `model, direction` | token 用量 | `sum(rate(...[5m]))` |
| `tsukiyohub_citations_per_answer` | histogram | — | 每次回答的引用数 | `avg(..._sum/_count)` |
| `tsukiyohub_skill_calls_total` | counter | `skill, ok` | **技能调用（agent 工具）** | `sum by (skill) (rate(...[5m]))` |
| `tsukiyohub_knowledge_ingest_total` | counter | `status` | 入库成功/跳过/失败 | `sum by (status) (increase(...[1h]))` |
| `tsukiyohub_knowledge_documents` | gauge | — | 文档数 | 直接看 |
| `tsukiyohub_knowledge_chunks` | gauge | — | 切块数 | 直接看 |
| `tsukiyohub_sessions` / `tsukiyohub_messages` | gauge | — | 会话数 / 消息数 | 直接看 |
| `tsukiyohub_memory_facts` | gauge | — | 长期记忆条数 | 直接看 |
| `tsukiyohub_skills_enabled` | gauge | — | 启用中的技能数 | 直接看 |
| `tsukiyohub_llm_ready` / `tsukiyohub_embedding_ready` | gauge | — | 依赖就绪 1/0 | `min(...)` |
| `tsukiyohub_uptime_seconds` | gauge | — | 运行时长 | 直接看 |

### 2.2 agent infra 视角：「一次 agent 运行」该看什么

| 问题 | 指标 / 记录 | 看板/告警 |
|---|---|---|
| agent 调了哪些工具、调了多少次 | `tsukiyohub_skill_calls_total{skill}` | 技能调用 TopN |
| 工具是否稳定 | `ok="false"` 的比率 | `TsukiyoCoreSkillFailures` |
| 检索是否有效 | `retrieval_hits` 分布 + `grounded` 比例 | `TsukiyoCoreGroundedRatioLow` |
| 回答是否真的用了资料 | `citations_per_answer` + grounded | 引用数分布面板 |
| 一次问答贵不贵 | `tokens_total` + `llm_duration_seconds` | tokens/s、P95 耗时 |
| 模型挂了会不会降级 | `degraded` 标签 + `llm_ready` | `TsukiyoCoreDegradedHigh` |
| 用户等多久看到第一个字 | `sse_first_token_seconds` | `TsukiyoCoreSSEFirstTokenSlow` |

---

## 3. 与业界约定的对照（面试可迁移的部分）

| 本项目 | OpenTelemetry GenAI 语义约定 | vLLM / 推理引擎 | 说明 |
|---|---|---|---|
| `tsukiyo_gateway_tokens_total{direction}` | `gen_ai.usage.input_tokens` / `output_tokens` | `vllm:prompt_tokens_total` / `generation_tokens_total` | 名字不同，口径一致：输入/输出分开计 |
| `tsukiyo_gateway_ttft_seconds` | `gen_ai.server.time_to_first_token` | `vllm:time_to_first_token_seconds` | 首字延迟，流式体验核心 |
| `tsukiyo_gateway_request_duration_seconds` | `gen_ai.server.request.duration` | `vllm:e2e_request_latency_seconds` | 端到端耗时 |
| `tsukiyo_gateway_tokens_total/moons`（推导） | `gen_ai.usage.output_tokens` 速率 | `vllm:avg_generation_throughput_toks_per_s` | 吞吐 tokens/s |
| `tsukiyo_gateway_breaker_state` | —（非标准） | — | 熔断状态，SRE 通用模式 |
| `tsukiyohub_skill_calls_total` | `gen_ai.tool.name` 维度 | — | agent 工具调用 |
| `tsukiyohub_grounded_ratio` | —（RAG 评估指标） | — | 有据回答比例，防幻觉 |

> 结论：**指标名可以换，口径（tokens 分输入/输出、TTFT、TPOT、成本、工具调用成功率）是行业通用语言。**
> 接 OTel 时只需在 `tsukiyohub/metrics.py` 与 `gateway/internal/metrics` 里加一层 exporter，不需要改业务。

---

## 4. 告警规则参考表（`alerts.yml`）

### 4.1 网关

| 告警 | 表达式（简写） | for | 级别 | 含义 / 处置 |
|---|---|---|---|---|
| `TsukiyoGatewayErrorRateHigh` | 5xx 率 > 10% | 2m | critical | 上游故障；看 overview 定位 provider，确认 fallback |
| `TsukiyoGatewayP95LatencyHigh` | P95 > 5s | 5m | warning | 上游变慢或网络抖动；对照 TTFT |
| `TsukiyoGatewayTTFTHigh` | TTFT P95 > 8s | 5m | warning | 首字慢（用户最敏感） |
| `TsukiyoGatewayBreakerOpen` | `breaker_state==2` | 1m | critical | 某模型被熔断，流量已切换；查上游 |
| `TsukiyoGatewayProviderDown` | `provider_up==0` | 2m | critical | 探活失败；查 key/网络/配额 |
| `TsukiyoGatewayBudgetRejected` | 10m 内 402 > 0 | 5m | critical | 调用方预算用尽 |
| `TsukiyoGatewayCostSpike` | 15m 花费 > 3× 历史均值 | 15m | warning | 大 prompt / 价格配错 |

### 4.2 聊天内核 / agent

| 告警 | 表达式（简写） | for | 级别 | 含义 / 处置 |
|---|---|---|---|---|
| `TsukiyoCoreGroundedRatioLow` | grounded < 50% 且 QPS>0.02 | 15m | warning | 知识库覆盖不足 / 检索退化 / 阈值错 |
| `TsukiyoCoreDegradedHigh` | degraded > 20% | 10m | critical | 模型不可用或全部降级 |
| `TsukiyoCoreRetrievalSlow` | 检索 P95 > 1.5s | 10m | warning | embedding / 向量库慢 |
| `TsukiyoCoreSkillFailures` | 技能失败率 > 10% | 10m | warning | 工具坏了（agent infra 核心） |
| `TsukiyoCoreSSEFirstTokenSlow` | 首字 P95 > 8s | 10m | warning | 用户等待过久 |
| `TsukiyoCoreChatErrorRateHigh` | `/api/chat*` 5xx > 5% | 5m | critical | 数据库/适配器异常 |

### 4.3 SLO 燃烧率（最该讲给面试官的一页）

SLO：网关可用性 **99.5%** → 错误预算 **0.5%**（30 天）。

| 告警 | 窗口 | 阈值 | 处置 |
|---|---|---|---|
| `TsukiyoGatewaySLOBurnFast` | 1h | 错误率 > 7.2%（14.4× 预算） | 立即处理：停止发布、回滚、查上游 |
| `TsukiyoGatewaySLOBurnSlow` | 6h | 错误率 > 3%（6× 预算） | 当天跟进，不叫醒 |

> 14.4× / 6× 来自 Google SRE Workbook 的多窗口多燃烧率方法；面试里能讲清「为什么不是简单阈值 5%」
> 就已经赢一半了。

---

## 5. 排障速查表

| 症状 | 先看 | 判断 |
|---|---|---|
| 用户说「很慢」 | `tsukiyohub:sse_first_token_p95_ms` vs `tsukiyo_gateway:ttft_p95_ms_by_route` | 都慢 → 上游/网络；只有内核慢 → 检索或本服务 |
| 错误率升高 | `tsukiyo_gateway:error_ratio_by_route` + `status` 标签 | 429/402 是**配额**问题，不是故障 |
| 回答总说「没找到」 | `tsukiyohub:grounded_ratio` + `retrieval_hits` 分布 | 命中少 → 知识库缺文档；命中多但 grounded 低 → 阈值/核对过严 |
| 成本突然涨 | `tsukiyo_gateway:cost_usd_per_hour` + `tokens_total` | token 涨 → prompt/输出变长；token 没涨 → 价格配置或模型切错 |
| 某个模型经常失败 | `tsukiyo_gateway_breaker_state` + `requests_total{model=...}` | 熔断反复开合 → 上游不稳，考虑提高权重给 fallback |
| agent 答非所问 | `tsukiyohub_skill_calls_total` + `citations_per_answer` | 没调检索 → 意图/技能开关问题；调了但没引用 → 生成层问题 |
| 服务「活着但不干活」 | `up` + `tsukiyohub:chat_requests_per_second == 0` | 流量入口/网关/鉴权问题 |

---

## 6. 看板规划（`gateway/deploy/grafana/dashboard.json`）

| 行 | 面板 | 关键 PromQL |
|---|---|---|
| 总览 | Up / RPS / 错误率 / P95 / TTFT P95 / tokens/s / 成本速率 / 在途 | 见记录规则 |
| 网关路由 | 按 route 的 RPS、P95、TTFT；错误率 TopN；熔断矩阵；供应商 up | `...:requests_per_second_by_route` 等 |
| 聊天内核 | chat RPS（按 intent）、grounded 比例、P95、SSE 首字、检索耗时/命中、引用数分布 | `tsukiyohub:*` |
| 技能/知识 | 技能调用 TopN + 失败率；文档数/切块数；入库速率；会话/记忆 | `tsukiyohub_skill_calls_total` 等 |
| SLO | 可用性、快速/慢速燃烧率、firing 告警数 | `TsukiyoGatewaySLOBurn*` 表达式 |

模板变量：`$datasource`（Prometheus 数据源）、`$route`、`$model`，全部带 `All`。

---

## 7. 怎么改到你的场景

1. **改名对齐公司规范**：只改 `server/tsukiyohub/metrics.py` 里的字符串常量与
   `gateway/internal/proxy/record.go` 的 `Metric*` 常量，看板/PromQL 同步替换即可（口径不变）。
2. **换成 OTel**：加一个 `OTLPMetricExporter`，把 `render()` 的数值转成 OTLP（或直接用
   `prometheus` exporter 走 OTLP→Prometheus），业务代码不动。
3. **多副本**：`requests_total` 用 `sum by (...)` 天然可聚合；但**限流/预算/熔断是进程内状态**，
   要全局口径需前置 Redis 或把网关做成无状态 + 共享配额（`docs/gateway.md` 已写清边界）。
4. **长期存储**：Prometheus 本地保留 7d（compose 里已设 `--storage.tsdb.retention.time=7d`），
   长期用 remote_write 到 Thanos/VictoriaMetrics/Mimir。
5. **高基数**：不要给 `user_id` / `session_id` / `trace_id` 打标签——它们放**追踪**（`/admin/api/traces`）
   而不是指标；`path` 用路由模板而非原始 URL。
6. **exemplar（可选）**：给 trace_id 加 exemplar，就能从「P95 变慢」一键跳到具体请求。

---

## 8. 这个项目在 AI infra / agent infra 面试里能讲什么

| 主题 | 这个项目的证据 |
|---|---|
| 推理网关 / 流量治理 | 多供应商路由、降级、熔断、限流、预算，8 种策略可插拔 |
| 推理性能指标 | TTFT、TPOT、tokens/s、P95/P99、成本/1M tokens（含记录规则） |
| 可观测性体系 | 两个服务统一 exposition、记录规则、告警、Grafana、SLO 燃烧率 |
| RAG 质量 | 检索召回、命中数、grounded 比例、引用数、防幻觉三道关 |
| Agent 可观测 | 技能调用数与失败率、工具结果、agent 循环轮数（tool_calls） |
| 成本治理 | 按客户端/模型计价、预算拒绝（402）、成本突增告警 |
| 工程取舍 | 为什么手写 exposition、为什么内存态限流、指标基数怎么控 |

> 建议面试时按这个顺序讲：**问题（多模型/多租户/成本/稳定性）→ 方案（网关 + 内核）→ 指标口径
> （TTFT/TPOT/tokens-s）→ 告警与 SLO → 踩过的坑（首字 0ms、伪向量、同秒排序）**。

---

## 9. 校验清单（改完规则必做）

```bash
# PromQL / 规则语法
docker run --rm -v ${PWD}/gateway/deploy/prometheus:/cfg --entrypoint promtool \
  prom/prometheus:latest check rules /cfg/alerts.yml /cfg/recording_rules.yml

# 抓取目标是否 UP
curl -s http://127.0.0.1:9090/api/v1/targets | grep -o '"health":"[a-z]*"'

# 关键指标是否真的有数据
curl -s "http://127.0.0.1:9090/api/v1/query?query=tsukiyo_gateway:tokens_per_second_by_model" | head
```