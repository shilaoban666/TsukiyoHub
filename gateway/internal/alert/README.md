# internal/alert

网关的**内置告警引擎**与**通知渠道**。只依赖 `internal/model`、`internal/metrics` 和标准库，
不导入 `proxy` / `provider`（避免包循环，也让告警可以脱离真实流量单测）。

## 文件

| 文件 | 职责 |
|---|---|
| `engine.go` | `Engine` 与 `model.AlertEngine` 实现：生命周期、规则安装、事件查询、Ack、渠道视图 |
| `rule.go` | 规则编译：window/for/cooldown/severity/scope 解析，指标名 → 取值映射，运算符比较 |
| `evaluate.go` | 评估循环与状态机：pending → firing → resolved、for 持续、cooldown、Ack 静默 |
| `event.go` | 事件环形缓冲（200 条）、状态常量、深拷贝 |
| `notify.go` | `Notifier` 接口 + 注册表、severity 过滤、并发发送 + 5s 超时、通知文案 |
| `notify_log.go` / `notify_webhook.go` / `notify_dingtalk.go` / `notify_slack.go` | 四种渠道 |
| `engine_test.go` / `notify_test.go` | 假时钟覆盖状态机；`httptest.Server` 断言载荷/加签/超时/过滤 |

完整使用说明（规则字段、指标语义、Prometheus 链路、排查）见 [`gateway/docs/alerts.md`](../../docs/alerts.md)。

## 必须保持不变的不变量

1. **时间只来自 `Engine.now`**（生产 = `time.Now`）：`for` / `cooldown` / 静默的测试都靠替换它，
   不要在别处直接调 `time.Now()` 做状态判断。
2. **通知在锁外发送**：`EvaluateOnce` 在锁内只收集 `outgoing`，`dispatch` 在锁外并发发送，
   否则一个慢 webhook 会把管理接口的读请求一起堵住。
3. **事件指针只在引擎锁内修改**：`eventRing` 本身无锁；对外返回的都是 `cloneEvent` 的副本。
4. **未知指标不致命**：规则保留、评估跳过、只 warning 一次（`e.warned`）。
5. **规则热替换是原子的**：`ReplaceRules` 有任何非法规则就整体拒绝；未变规则保留状态。

## 扩展点

* 加渠道：实现 `alert.Notifier`（`Name/Type/Send`），在 `init` 里 `alert.Register("feishu", factory)`；
  可用导出的 `PostJSON` / `HTTPClient` / `NotificationText` / `NotificationMarkdown`。
* 加指标：在 `rule.go` 的 `windowMetrics`（窗口）或 `instantMetrics`（瞬时）里登记，
  同时更新 `SupportedMetrics` 的文档与 `docs/alerts.md` 的指标表。
* 规则持久化：`Engine.SetPersist(func([]model.AlertRule) error)`，由 `cmd` 注入 `config.Save` 的包装。
* 配置热更新：`Engine.ApplyConfig(cfg)` 是 `ReplaceRules` 的超集（同时刷新 notifier/间隔/冷却）。

## 测试

```bash
go vet ./internal/alert/...
go test ./internal/alert/...
```

`engine_test.go` 用假时钟把「等 30 秒 for / 等 5 分钟 cooldown / 静默 10 分钟」压到毫秒级；
`notify_test.go` 用 `httptest.Server` 验证 webhook 载荷与自定义头、钉钉 markdown + HmacSHA256 加签、
slack text、log 单行格式、severity 过滤与渠道超时。
