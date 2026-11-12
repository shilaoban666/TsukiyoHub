# Grafana 看板：LLM 路由网关 + 聊天/RAG 内核

一套「开箱即用」的 AI infra 观测面板：Prometheus 采集两个服务的 `/metrics`，Grafana 用
provisioning 自动装配数据源与看板，不需要在 UI 里点任何「Add data source / Import」。

```
gateway/deploy/grafana/
├── dashboard.json                          # 看板本体（5 行 / 42 个面板，Grafana 10/11 可导入）
├── provisioning/
│   ├── datasources/prometheus.yml          # 数据源（uid=prometheus，指向 http://prometheus:9090）
│   └── dashboards/dashboards.yml           # 看板 provider（扫 /var/lib/grafana/dashboards）
└── README.md
```

## 1. 快速开始

```bash
# 1) 起监控栈（prometheus + alertmanager + grafana）
docker compose --profile monitor up -d prometheus alertmanager grafana

# 2) 打开看板
#    http://127.0.0.1:3000  →  默认 admin / admin（首次登录会要求改密码）
#    首页已经被 GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH 指到这个看板
#    Prometheus:   http://127.0.0.1:9090
#    Alertmanager: http://127.0.0.1:9093
```

只想验证配置、不启动容器：

```bash
docker compose --profile monitor config          # 需要 .env（Copy-Item .env.example .env）
python -c "import json;print(len(json.load(open('gateway/deploy/grafana/dashboard.json'))['panels']),'panels')"
```

只看 Grafana（会通过 `depends_on` 顺手起 Prometheus）：

```bash
docker compose --profile monitor up -d grafana
# 数据源地址写死为 http://prometheus:9090；本机非 compose 调试时改
# provisioning/datasources/prometheus.yml 的 url 为 http://127.0.0.1:9090 后重启 grafana
```

Grafana 账号可以用环境变量覆盖：`GRAFANA_ADMIN_USER`、`GRAFANA_ADMIN_PASSWORD`（默认 admin/admin，
生产务必改）。看板里还挂了三个外链（Prometheus / Alertmanager / 网关管理概览），地址按本机
9090 / 9093 / 8082 写死，部署到别的机器时改 `dashboard.json` 的 `links` 即可。

看板也可以手工导入：Grafana → Dashboards → New → Import → 上传 `dashboard.json`，
在弹出的数据源下拉里选你的 Prometheus（JSON 用 `${datasource}` 变量，不绑定固定 uid）。

## 2. 自动装配是怎么回事

* `provisioning/datasources/prometheus.yml`：启动时按 `uid: prometheus` 建数据源。
  `jsonData.timeInterval: 15s` 必须和 `prometheus.yml` 的 `scrape_interval`（当前 15s）一致，
  否则 Grafana 算出来的 `$__rate_interval` 会小于一个采样间隔，`rate()` 曲线会周期性归零。
* `provisioning/dashboards/dashboards.yml`：每 30s 扫一次 `/var/lib/grafana/dashboards`，
  把 `dashboard.json` 作为看板加载；改完 JSON 不用重启 Grafana，也不用重新导入。
* compose 把 `dashboard.json` 单文件挂进容器（见根目录 `docker-compose.yml` 的 `grafana` 服务），
  所以**文件是唯一真源**：在 UI 上改的东西容器重建后会回到文件版本，长期修改请改 JSON。

## 3. 看板结构（5 行，42 个面板）

变量：`$datasource`（数据源）、`$route` / `$model`（网关路由与模型）、`$intent`（聊天意图），
都带 `All`（`allValue: ".*"`，所以 PromQL 里统一写 `route=~"$route"`）。

| 行 | 面板 | 回答什么问题 / 怎么看 |
|---|---|---|
| ① 总览 | 服务存活（up）、RPS、错误率、P95 延迟、P95 TTFT、tokens/s、USD/h、在途请求、延迟分位趋势 | 30 秒内判断「服务活着吗、快不快、贵不贵」。up=0 先修抓取，不要先怀疑业务 |
| ② 网关路由 | 各 route / 各 model RPS、各 route P95、各 model P95、TTFT 分位、错误率 TopN（route×model）、熔断状态矩阵、供应商 up、限流/预算拒绝速率、探活耗时 P95 | 定位「谁在吃流量、谁在报错、要不要熔断」。熔断矩阵 0=closed（绿）1=half_open（橙）2=open（红） |
| ③ 聊天/RAG | 聊天 RPS（按 intent）、Grounded 比例、P95 聊天耗时、SSE 首字延迟、检索耗时、检索命中数、引用数分布、技能调用 TopN、技能失败率 TopN | 回答质量视角：grounded 掉 = 检索/知识库问题；首字变慢 = 上游排队；技能失败率 = 工具链坏了 |
| ④ 知识与会话 | 文档数/切块数、入库速率（按 ok/skipped/failed）、入库累计、会话数、消息数、记忆条数、启用技能数 | 「知识库在长吗、入库有没有失败、用户规模多大」。skipped 多为内容去重（正常），failed 要查日志 |
| ⑤ 资源与 SLO | SLO 可用性（1-错误率，1h）、错误预算燃烧率（1h/6h）、内置告警数、运行时长、最近 1h 请求数、LLM/Embedding 就绪 | 距离违背 SLO 还有多远。burn rate 1x = 30 天刚好用完预算；1h 曲线 >14.4x 属于该 page 的级别 |

关键约定（写 PromQL 时照着抄就不会错）：

```promql
# 分位数：只按 le + 分组标签聚合，其它标签必须丢掉
histogram_quantile(0.95, sum by (le, route) (rate(tsukiyo_gateway_request_duration_seconds_bucket{route=~"$route"}[5m])))
# 比率：分母 clamp_min 防 0 除
sum(rate(tsukiyo_gateway_requests_total{status=~"5.."}[5m])) / clamp_min(sum(rate(tsukiyo_gateway_requests_total[5m])), 1e-9)
# 计数类直方图（命中数 / 引用数）桶是 0,1,2,3,5,8,13,21，同样用 histogram_quantile
histogram_quantile(0.95, sum by (le) (rate(tsukiyohub_retrieval_hits_bucket[5m])))
```

## 4. 指标来源（改面板前先确认标签）

| 来源 | 指标 | 标签 |
|---|---|---|
| 网关 `:8082/metrics` | `tsukiyo_gateway_requests_total` | `route, model, provider, status` |
| 网关 | `request_duration_seconds` / `ttft_seconds` / `provider_probe_seconds` | 直方图（`_bucket/_sum/_count`），标签分别是 `route,model,provider` / `model` / `provider` |
| 网关 | `tokens_total` / `cost_usd_total` / `inflight` / `breaker_state` / `provider_up` / `ratelimit_rejected_total` / `budget_rejected_total` / `alerts_firing` / `uptime_seconds` | `model,direction` / `model` / 无 / `model` / `provider` / `client` / `client` / 无 / 无 |
| 内核 `:8080/metrics` | `tsukiyohub_http_requests_total`、`chat_requests_total`、`skill_calls_total`、`tokens_total`、`knowledge_ingest_total` | `method,path,status` / `intent,grounded,degraded` / `skill,ok` / `model,direction` / `status` |
| 内核 | `sse_first_token_seconds`、`retrieval_duration_seconds`、`retrieval_hits`、`citations_per_answer`、`llm_duration_seconds`、`chat_duration_seconds`、`http_request_duration_seconds` | 直方图，只有 `llm_duration_seconds{model}` / `chat_duration_seconds{intent}` 带标签 |
| 内核 | `knowledge_documents`、`knowledge_chunks`、`sessions`、`messages`、`memory_facts`、`skills_enabled`、`llm_ready`、`embedding_ready` | 都是无标签 gauge |

## 5. 怎么加一个面板

1. 在 `dashboard.json` 的 `panels` 数组里加一个对象（`id` 不能重复；`gridPos.y` 决定它落在哪一行）；
2. 必填：`datasource: {"type":"prometheus","uid":"${datasource}"}`、`targets[].expr`、
   `targets[].interval: "30s"`、`description`（写清「这面板回答什么问题」）、`fieldConfig.defaults.unit`；
3. `timeseries` 用区间查询（不要 `instant: true`）；`stat/gauge/bargauge` 用 `instant: true`；
   表格加 `"format": "table"`；
4. 保存后等 30s（`updateIntervalSeconds`）刷新页面即可看到，不用重启 Grafana；
5. 改完先跑一遍静态校验：`python -c "import json;json.load(open('.../dashboard.json'))"`。

常用单位：`reqps`（req/s）、`s`（秒）、`percentunit`（0~1 比例）、`short`（计数）、
`currencyUSD`、`dtdurations`（秒转人类可读时长）。

### 5.1 几个刻意的设计决定（面试可讲）

* **分位数一定先 `sum by (le, ...)` 再 `histogram_quantile`**：直方图是“每标签组合一套桶”，
  不先聚合会把桶拆散、分位数算错；反过来聚合维度留多了又会把 `le` 之外的标签带进来。
* **`stat` 用即时查询、`timeseries` 用区间查询**：`stat` 只关心“现在是几”，区间查询会白算
  一堆点；而 `gauge`（inflight / 会话数）在 `stat` 里用区间查询才有 sparkline。
* **token 速率用 `[1m]`、其它速率用 `[5m]`**：token 是成本敏感指标，宁可抖一点也要快；
  错误率 / P95 用 5m 是为了不被单次抖动带偏（15s scrape × 5m = 20 个采样点）。
* **`grounded` 比例用“分子分母各自 rate 再相除”**，不是 `avg()` 单个样本：前者天然抗流量波动，
  后者在低流量时会被一两条请求带飞。
* **所有标签都是有限集合**（route/model/provider/status/intent/skill/client）：`path` 用路由模板
  而不是原始 URL，避免标签基数爆炸——这是 Prometheus 里最容易踩的性能坑。
* 燃烧率除的是 **`(1 - SLO)`** 而不是 100：SLO=99% 时错误预算就是 1%，所以 `0.01` 这个数字
  和标题里的 SLO 必须同步改。

## 6. 没数据怎么查（按顺序）

1. **up=0**：`docker compose logs prometheus`；确认 `prometheus.yml` 的 targets 端口对不对
   （网关 `/metrics` 挂在管理端口 8082，内核在 8080）。
2. **只有网关有数、内核全空**：`prometheus.yml` 里应有 `job_name: tsukiyohub-core`，
   targets 为 `app:8080`（本仓库已配置）。用自定义 Prometheus 时补一个
   `- job_name: tsukiyohub-core` + `metrics_path: /metrics` + `targets: ["app:8080"]`；
   `$intent` 变量也会跟着出现。
3. **变量下拉是空的**：数据源 uid 不是 `prometheus`，或 Prometheus 里还没有任何
   `tsukiyo_gateway_requests_total` / `tsukiyohub_chat_requests_total` 样本。
4. **面板显示 No data 但 up=1**：面板的标签过滤写错了（比如给 `ttft_seconds` 加了 `route=`，
   它只有 `model` 标签），或时间窗内确实没有该样本（计数器第一次出现要等一个 scrape 周期）。

## 7. 已知限制

* 没采集 `process_*`（进程 CPU/内存）：需要 node_exporter / process-exporter（或另加一个主机指标源）。
  看板第 5 行因此只看 SLO 与就绪状态，不看 CPU/内存。
* SLO 在面板里按 **99%** 写死（错误预算比例 `0.01`）。改 SLO 需要同时改「可用性」的阈值和
  「燃烧率」里的 `/ 0.01`，并把标题里的 SLO 一起改掉。
* 燃烧率用的是 1h / 6h 窗口的**瞬时** burn rate，没有做多窗口 SLO 告警的 for 判定；
  真正的告警规则在 `gateway/deploy/prometheus/alerts.yml`（由 Prometheus 评估，看板只做可视化）。
* `up` 面板用 `job=~".*tsukiyo.*"` 匹配两个 job，换 job 名后要同步改这一处正则。
* 成本按 `models[].input_price/output_price`（每 1K token 美元）计算，价格配错时面板会一起错；
  预算/限流计数是进程内的，多副本部署时每个副本各自计数（看板趋势会按副本叠加）。
* 熔断状态矩阵、供应商探活是「当前值」，没有历史回放能力；要做复盘请给这两个指标加 recording rule。