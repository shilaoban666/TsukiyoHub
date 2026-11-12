# 告警指南：内置引擎 与 Prometheus/Alertmanager

网关提供**两条互相独立**的告警链路，按团队情况选一条或都开：

| 链路 | 实现 | 适合 | 数据留存 | 额外依赖 |
|---|---|---|---|---|
| 内置告警引擎 | `internal/alert`，跑在网关进程里 | 单机 / 内网 / 想要「一个二进制就能告警」 | 内存环形，最近 200 条事件 | **零**（只用标准库） |
| Prometheus + Alertmanager | `deploy/prometheus`、`deploy/alertmanager` | 多网关、多服务聚合、要长期留存与值班流程 | Prometheus TSDB（按 retention） | Prometheus + Alertmanager |

两条链路的规则**各自独立**：内置规则写在 `config.json` 的 `alerts.rules`，外部规则写在 `deploy/prometheus/alerts.yml`。
建议阈值只在一处调，否则会出现「面板正常、告警还在响」的经典问题。

## 一、内置告警引擎

### 1.1 怎么开

```json
{
  "public_url": "http://127.0.0.1:8081",
  "alerts": {
    "enabled": true,
    "interval_ms": 15000, "default_cooldown_seconds": 300,
    "rules": [ { "id": "error_rate_high", "metric": "error_rate", "op": ">", "threshold": 0.1,
                 "window": "1m", "for": "1m", "severity": "critical", "enabled": true } ],
    "notifiers": [ { "type": "log", "name": "log", "enabled": true } ]
  }
}
```

* `interval_ms`：评估间隔，默认 `15000`，实现下限 100ms（防止把 CPU 打满）。
* `default_cooldown_seconds`：规则没写 `cooldown` 时的默认冷却，默认 `300`。
* `enabled: false` 时引擎不评估（`Overview.Enabled=false`），但接口仍然可用。
* 改完整的配置走 `PUT /admin/api/config`；只改规则可以走 `PUT /admin/api/alerts/rules`（调 `ReplaceRules`）。

### 1.2 规则字段

| 字段 | 含义 | 说明 |
|---|---|---|
| `id` | 规则唯一标识 | 必填；事件与通知里都用它 |
| `name` | 展示名 | 空则用 `id` |
| `metric` | 指标名 | 见 1.3；写错不会崩，规则被跳过并记一次 warning |
| `op` | 比较运算符 | `> >= < <= == !=`，空 = `>` |
| `threshold` | 阈值 | 浮点 |
| `window` | 统计窗口 | `30s` `1m` `5m` `10m` `1h`；空 = `1m`；最小 1s，**最大 15m** |
| `for` | 持续满足多久才 firing | `30s` `1m`…；空 = 满足即 firing |
| `cooldown` | 重复通知冷却 | 空或 `0` = 用 `default_cooldown_seconds`；想更频繁提醒就写小值（如 `1m`） |
| `severity` | 级别 | `info` / `warning` / `critical`，空 = `warning`（也接受 `warn`/`crit`/`fatal`/`notice` 别名） |
| `scope` | 维度过滤 | `{"route":"fast"}` / `{"model":"gpt-4o-mini"}` / `{"provider":"openai"}`，键只支持 `route/model/provider/client`，空 = 全局 |
| `message` | 通知里的补充说明 | 可选 |
| `enabled` | 是否启用 | false 的规则保留在列表里但不评估 |

`window` 上限 15m 是因为 `metrics.Aggregator` 只保留最近 900 秒的请求记录；
写 `2h` 会在 `ReplaceRules` 时直接报错，而不是悄悄按 5 分钟统计。

### 1.3 指标语义

**窗口指标**取自 `Aggregator.Query(window, scope)`（即最近 `window` 秒的请求记录聚合，高 QPS 下是近似值）：

| 指标 | 含义 | 单位 |
|---|---|---|
| `error_rate` | 窗口内 `!ok \|\| status>=400` 的占比 | 0~1 |
| `p95_latency_ms` / `p50_latency_ms` | 窗口内总延迟分位数（线性插值） | 毫秒 |
| `ttft_ms` | 窗口内首字延迟（TTFT）P95 | 毫秒 |
| `rps` | `requests / window` | 请求/秒 |
| `requests` / `error_count` | 窗口内请求数 / 错误数 | 次 |
| `tokens` | 窗口内 `usage.total_tokens` 之和 | token |
| `cost_usd` | 窗口内累计费用 | 美元 |
| `rate_limited` / `budget_rejected` | 窗口内 429 / 402 的请求数 | 次 |

**瞬时指标**取自 `Aggregator.Instant()`（`window` 对它无意义、`scope` 也不生效）：
`inflight` 当前在途请求数、`breaker_open` 处于 open/half-open 的模型数、`provider_down` 探活失败的供应商数。

### 1.4 评估语义（严格按契约）

```
            条件满足                    for 到期                  条件恢复
   ──────────────────> pending ──────────────────> firing ──────────────────> resolved
   (新建事件, StartsAt)  (每轮刷新 Value)          (发通知)                 (发「已恢复」)
                          │                         │
                          │ Ack                     │ Ack
                          v                         v
                       acked  ──── SilencedUntil 到期且条件仍满足 ────> firing（再通知）
```

* **pending**：条件首次满足时建事件、记 `StartsAt`，此阶段不发通知。
* **firing / cooldown**：`now - StartsAt >= for` 时转 firing 并发通知（`for` 空/0 即当轮 firing）；
  仍是 firing 且距上次通知超过 cooldown → 再发一条（周期提醒），冷却内不重复。
* **resolved**：条件恢复 → 事件置 resolved、记 `EndsAt`。**pending 阶段就恢复的事件不发通知**（用户从没被告知过），只有 firing/acked 过的事件才发「已恢复」。
* **Ack**：`Ack(id, by, duration)` 把事件置 `acked`、`SilencedUntil = now+duration`；
  静默期内该规则不发任何通知、状态保持不变；到期后若条件仍满足则回到 firing 再通知一次。
  已 resolved 的事件不允许 Ack（返回错误）。
* **规则热替换**：`ReplaceRules` 先整体编译，**任何一条非法 → 整体拒绝**（原子替换）；
  定义完全相同的规则保留运行状态与历史事件；变更/新增规则重建状态，旧的活动事件就地收尾为
  resolved（不通知）；被删除规则的历史事件从事件环里移除。
* **事件环**：固定 200 条，满了覆盖最旧的；`ListEvents` 最新在前，`limit<=0` 时返回 50 条。

### 1.5 内置规则（`config.example.json` 里开箱即用）

| id | 指标 | 条件 | 持续 | 级别 |
|---|---|---|---|---|
| `error_rate_high` | `error_rate` | `> 0.1` | 1m | critical |
| `latency_p95_high` | `p95_latency_ms` | `> 5000` | 2m | warning |
| `ttft_slow` | `ttft_ms` | `> 8000` | 2m | warning |
| `breaker_open` | `breaker_open` | `> 0` | 30s | critical |
| `provider_down` | `provider_down` | `> 0` | 1m | critical |
| `budget_rejected` | `budget_rejected` | `> 0` | 1m | critical |
| `rate_limited_spike` | `rate_limited` | `> 20` | 1m | warning |

### 1.6 管理接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/api/alerts/rules` | 当前规则（配置顺序） |
| PUT | `/admin/api/alerts/rules` | 整体替换规则（原子；非法时 400） |
| GET | `/admin/api/alerts/events?limit=&status=` | 最近事件，`status` = `pending/firing/resolved/acked` |
| POST | `/admin/api/alerts/{id}/ack` | body `{"by":"web","minutes":30}`，静默期内不再通知 |
| GET | `/admin/api/alerts/overview` | 规则数 / firing / pending / 最近事件 |

## 二、通知渠道

每个渠道是 `Notifier` 接口的实现，注册在 `internal/alert/notify.go` 的注册表里；
引擎对每个渠道**并发**发送，单渠道超时 **5s**，失败只记日志、不影响其它渠道。

```json
"notifiers": [
  { "type": "log",      "name": "log",    "enabled": true },
  { "type": "webhook",  "name": "hook",   "url": "http://127.0.0.1:9099/alert", "headers": { "X-Token": "xxx" },
    "min_severity": "warning", "enabled": true },
  { "type": "dingtalk", "name": "oncall", "url": "https://oapi.dingtalk.com/robot/send?access_token=xxx",
    "secret": "SECxxxx", "min_severity": "critical", "enabled": true },
  { "type": "slack",    "name": "slack",  "url": "https://hooks.slack.com/services/xxx", "enabled": false }
]
```

| 类型 | 请求 | 成功判定 |
|---|---|---|
| `log` | 标准库 logger 输出一行：`[alert] status= severity= rule= metric= value= threshold= window= scope= at= gateway=` | 总是成功 |
| `webhook` | POST JSON `{"event":{...},"rule":{...},"gateway":"..."}`，支持自定义 `headers` | 2xx |
| `dingtalk` | POST `{"msgtype":"markdown","markdown":{"title":..,"text":..}}` | 2xx 且 `errcode=0` |
| `slack` | POST `{"text":"..."}` | 2xx |

* `min_severity`：`info < warning < critical`，只有事件级别 ≥ 它才发；空 = 全发。
* `enabled:false`：渠道保留在引擎的 `Notifiers()` 视图里（管理接口展示用）但不发送。
* **钉钉加签**：`secret` 非空时自动追加 `&timestamp=<毫秒>&sign=<base64>`，
  签名算法 `base64(HmacSHA256(secret, timestamp+"\n"+secret))`；签名串会被 URL 转义。
* **脱敏 / 兜底**：`Notifiers()` 只返回 `name/type/enabled/min_severity`，不含 url/secret；
  一个渠道都没配时引擎启动会打一条 warning，事件仍可在管理接口看到。

## 三、加规则 / 加渠道

### 3.1 加一条内置规则

方式 A（推荐）：`PUT /admin/api/alerts/rules`，body 是**完整规则数组**（整体替换），例如把 402 按 client 拆开：

```json
[ { "id": "budget_rejected_client_a", "name": "client-a 预算被拒", "metric": "budget_rejected",
    "scope": { "client": "client-a" }, "op": ">", "threshold": 0, "window": "5m", "for": "1m",
    "severity": "critical", "cooldown": "10m", "enabled": true } ]
```

方式 B：直接改 `config.json` 的 `alerts.rules` 后 `PUT /admin/api/config`；两种方式都走 `ReplaceRules` 原子生效（`SetPersist` 注入了回调时还会写回配置文件）。

### 3.2 加一个通知渠道

实现接口 + 注册，**不用改引擎**：

```go
type feishuNotifier struct{ name, url string }

func (f *feishuNotifier) Name() string { return f.name }
func (f *feishuNotifier) Type() string { return "feishu" }
func (f *feishuNotifier) Send(ctx context.Context, n alert.Notification) error {
	_, _, err := alert.PostJSON(ctx, alert.HTTPClient, f.url, nil, map[string]any{
		"msg_type": "text", "content": map[string]string{"text": alert.NotificationText(n)}})
	return err
}

func init() {
	alert.Register("feishu", func(cfg model.NotifierConfig, _ *log.Logger) (alert.Notifier, error) {
		return &feishuNotifier{name: cfg.Name, url: cfg.URL}, nil
	})
}
```

> `PostJSON` / `HTTPClient` / `NotificationText` 都是导出的辅助函数，
> 所以自定义渠道可以放在网关仓外的独立包里实现、再 `alert.Register` 注册。
> 注册表是进程级全局的，必须在构造引擎（`alert.New`）之前完成注册（`init` 或 `main` 里）。

## 四、Prometheus + Alertmanager 链路

### 4.1 部署

```bash
docker network create tsukiyo || true    # 网关容器也加入该网络（服务名 gateway，端口 8081）
docker run -d --name prometheus --network tsukiyo -p 9090:9090 \
  -v ${PWD}/gateway/deploy/prometheus:/etc/prometheus:ro prom/prometheus:latest
docker run -d --name alertmanager --network tsukiyo -p 9093:9093 \
  -v ${PWD}/gateway/deploy/alertmanager:/etc/alertmanager:ro \
  prom/alertmanager:latest --config.file=/etc/alertmanager/alertmanager.yml
```

* 本机直接跑网关：把 `prometheus.yml` 的 `targets` 改成 `["127.0.0.1:8081"]`；Prometheus 在容器、网关在宿主机则用 `["host.docker.internal:8081"]`。
* 校验：`promtool check rules /etc/prometheus/alerts.yml`（容器 `--entrypoint promtool`）；再访问 `http://127.0.0.1:9090/targets` 确认 `tsukiyo-gateway` 是 UP。

### 4.2 指标清单

网关 `/metrics` 暴露（手写 exposition，无需 client_golang）：
`tsukiyo_gateway_requests_total{route,model,provider,status}`、`..._request_duration_seconds_bucket/sum/count`、
`..._ttft_seconds_bucket{model}`、`..._tokens_total{model,direction}`、`..._cost_usd_total{model}`、
`..._inflight`、`..._breaker_state{model}`（0=closed 1=half_open 2=open）、`..._provider_up{provider}`、
`..._ratelimit_rejected_total{client}`、`..._budget_rejected_total{client}`、`..._alerts_firing`、`..._uptime_seconds`。

### 4.3 规则与路由

`deploy/prometheus/alerts.yml` 有 8 条：错误率、P95 延迟、熔断、供应商探活、抓取失败（`up==0`）、
限流突增、预算拒绝、费用突增（最近 15 分钟 > 之前 3 小时均值的 3 倍）。
`deploy/alertmanager/alertmanager.yml` 把 `severity=critical` 单独路由（10s group_wait、1h repeat），
其余走默认 webhook，并用 `inhibit_rules` 在「网关抓不到」时压掉指标类告警。

**换成钉钉**：Alertmanager 没有内置钉钉 receiver，标准做法是用 `timonwong/prometheus-webhook-dingtalk`
中转（配置片段见 `alertmanager.yml` 注释），或者干脆只用网关内置引擎的 dingtalk 渠道。
**换成 Slack** 则用 `slack_configs`，同样在注释里有示例。

## 五、排查

| 现象 | 先看什么 |
|---|---|
| 告警从不触发 / 一直 pending | `overview` 的 `last_check` 是否在动（引擎没启动？`enabled=false`？）；`ListRules` 里规则是否 `enabled`；`metric` 是否拼错（日志有「未知指标」，对照 1.3）；`for` 比评估间隔大得多时 pending 是正常的 |
| firing 但没收到通知 | `Notifiers()` 里渠道是否 `enabled`；`min_severity` 是否把事件过滤掉了；日志里找「发送失败」（每渠道 5s 超时） |
| 钉钉 310000 / sign not match | `secret` 是否就是这个机器人的加签密钥；容器时间是否准确（timestamp 偏差过大会被拒） |
| 通知太吵 | 调大 `cooldown`/`default_cooldown_seconds`；用 `min_severity` 把 warning 降到只进日志；对已确认的事件 `Ack` |
| 告警恢复后没有「已恢复」 | pending 阶段就恢复的事件按设计不发通知（见 1.4） |
| `/metrics` 抓不到 | `curl http://127.0.0.1:8081/metrics`；确认 `admin_listen` 与 `prometheus.yml` 的端口/路径一致 |
| Prometheus 规则报 `many-to-many` | 规则里按 `le`/`severity` 等显式 `sum by` 聚合，别直接对多 series 做二元运算 |

自测命令：`go test ./internal/alert/...`（`engine_test.go` 覆盖状态机，`notify_test.go` 用
`httptest.Server` 断言四种渠道的载荷/加签/超时/severity 过滤）。

## 六、已知限制

1. 内置引擎的事件与规则状态都在内存：**重启即清空**，需要长期留存请开 Prometheus 链路。
2. 窗口统计基于 `Aggregator` 的最近记录（环形最多 3 万条 / 900 秒），高 QPS 下是**近似值**；
   `cost_usd` / `tokens` 在上游不返回 usage 时按字符数估算，阈值要留余量。
3. 瞬时指标的 `scope` 不生效（`Instant()` 只有全局 gauge）；`==` / `!=` 是浮点精确比较，
   只建议用于计数类指标。
4. 通知发送在评估 goroutine 内同步等待（每渠道 5s），渠道全挂时单轮最坏慢 5s；内网、渠道少时可忽略。
5. Alertmanager 的 webhook 载荷是它自己的 JSON 格式，与内置引擎的 `{event,rule,gateway}` 不同，
   接同一个接收端时要分别解析。
