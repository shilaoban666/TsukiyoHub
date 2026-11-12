# 路由网关 / 告警 · 前端管理页说明

> 路由网关是独立的 Go 服务（`gateway/`），本前端**不直连网关**，只请求 TsukiyoHub 后端的
> `/api/gateway/*`，由后端代理并统一鉴权 / 报错。契约见
> [gateway-contract.md](design/gateway-contract.md) 的「admin」一节。

---

## 1. 页面与入口

| 路由 | 组件 | 导航 | 作用 |
|---|---|---|---|
| `/routing` | `web/src/views/RoutingView.vue` | 顶栏「路由网关」（`ApiOutlined`） | 网关健康、指标、供应商、路由、请求追踪 |
| `/alerts` | `web/src/views/AlertsView.vue` | 顶栏「告警」（`AlertOutlined`） | 告警概况、规则增删改、事件与确认（Ack） |

两个页面都是懒加载路由（`router/index.ts`），`meta.title` 会写进浏览器标题。
数据层在 `web/src/api/gateway.ts`，类型在 `web/src/types/gateway.ts`，可复用子组件在
`web/src/components/gateway/`。

## 2. 路由网关页（`/routing`）

- **状态条**：管理地址、健康状态、版本、uptime、配置指纹（`config_hash` 前 12 位），
  右侧「刷新」与自动刷新下拉（关闭 / 5s / 15s）。自动刷新在标签页不可见时跳过。
- **指标卡**：RPS、错误率、P95 延迟、P95 首字、在途请求、今日 token、今日成本。
  - 前 5 个来自 `Overview.metrics`（`StatsSnapshot`，默认 300s 窗口），失败时退回 `/stats`；
  - 「今日」两项没有专用接口：前端取最近 500 条追踪、按本地 0 点过滤后累加
    `usage.total_tokens` 与 `cost_usd`，因此是**近似值**，卡片 hint 会注明。
- **供应商表**：健康 / 延迟 / 最近探活来自 `/providers`，类型与 `base_url` 来自 `/config`
  （按 `name` 关联）。行内「探活」调用 `POST /providers/{name}/probe`，成功/失败都有 toast。
- **路由表**：别名、策略、目标（priority/weight）、降级链、能力要求、启用状态；展开行看
  `options` JSON。表格与「编辑路由」对话框都只负责收集数据，保存时**整体替换**：
  `PUT /routes` body `{items: RouteConfig[]}`。
  - 对话框支持增删目标、8 种策略、`fallbacks` / `required_capabilities`（逗号分隔）、
    `options` JSON 校验（必须是 JSON 对象）、启用开关；
  - 保存失败时对话框不关闭，用户输入不丢。
- **追踪表**：时间 / 路由 / 模型 / 状态码 / 耗时 / TTFT / tokens / 成本 / 尝试次数 / 客户端；
  支持按 route / model / 成功失败过滤（透传给 `/traces` 的 `route/model/ok`），
  分页是前端切片（一次取 200 条）。点击行打开抽屉，再请求
  `GET /traces/{id}` 展示完整 `attempt_log`、错误与原始 JSON。

## 3. 告警页（`/alerts`）

- **概况卡**：规则数、firing、pending、最近评估时间（`AlertOverview`）。
- **规则表**：名称、指标、作用域（`route=... / model=... / provider=...`）、条件
  （`metric op threshold`）、窗口、持续（`for`）、级别、启用、操作。
  - 「新增规则 / 编辑」对话框的指标下拉是网关 14 个内置指标：`error_rate`、`p95_latency_ms`、
    `p50_latency_ms`、`ttft_ms`、`rps`、`inflight`、`breaker_open`、`rate_limited`、
    `budget_rejected`、`provider_down`、`requests`、`error_count`、`tokens`、`cost_usd`；
  - 作用域 = 维度下拉（route/model/provider）+ 关键词输入，留空为全局；
  - 启用/停用与删除同样是**整体保存**：`PUT /alerts/rules` body `{items: AlertRule[]}`。
- **事件表**：时间、规则、级别、指标值、阈值、状态（pending/firing/resolved/acked）、
  持续时间；`firing` 行高亮。行内「确认」弹窗选择静默 5/15/30/60 分钟，
  调 `POST /alerts/{id}/ack` body `{by, minutes}`。
- **立即评估提示**：评估周期由网关 `alerts.interval_ms` 决定（默认 15s），
  前端「刷新」只重新拉取规则 / 事件，不会触发立即评估。

## 4. 接口对应关系

| 前端方法（`api/gateway.ts`） | TsukiyoHub 接口 | 返回（前端归一化后） |
|---|---|---|
| `getGatewayHealth` | `GET /api/gateway/health` | `{enabled, reachable, admin_url, error?, ...}` |
| `getOverview` | `GET /api/gateway/overview` | `Overview` |
| `getStats` | `GET /api/gateway/stats?window=300` | `StatsSnapshot`（window 后端限制 10~900） |
| `getGatewayConfig` | `GET /api/gateway/config` | `GatewayConfig`（密钥已脱敏） |
| `listProviders` / `probeProvider` | `GET /providers`、`POST /providers/{name}/probe` | `ProviderHealth[]` / `ProviderHealth` |
| `listRoutes` / `putRoutes` | `GET /routes`、`PUT /routes` | `RouteConfig[]` / `{ok, reloaded}` |
| `listTraces` / `getTrace` | `GET /traces?...`、`GET /traces/{id}` | `TraceRecord[]` / `TraceRecord` |
| `getAlertOverview` | `GET /alerts/overview` | `AlertOverview` |
| `listAlertRules` / `putAlertRules` | `GET/PUT /alerts/rules` | `AlertRule[]` / `{ok}` |
| `listAlertEvents` / `ackAlert` | `GET /alerts/events`、`POST /alerts/{id}/ack` | `AlertEvent[]` / `{ok}` |

列表响应统一按 `{items: [...]}` 处理，同时兼容裸数组与按资源名包裹
（`{routes: [...]}` 等），后端版本差异不会导致白屏。

## 5. 空态、错误与安全

- 网关不可达时 `/health` 仍返回 200 + `reachable:false`：页面显示醒目提示 + 空态，
  并给出管理地址和配置指引，**不弹错误、不白屏**。
- 其余接口失败：保留旧数据，手动刷新弹一次 toast；自动刷新只显示内联错误，避免刷屏。
- 页面不接收 / 不缓存网关密钥；`config` 里的 `api_key`、`admin_token` 由网关脱敏后返回。

## 6. 对接真实网关

1. 启动 Go 网关（管理接口默认只允许本机访问）：

   ```powershell
   cd E:\Project\TsukiyoHub\gateway
   go run ./cmd/tsukiyo-gateway -config ./config.json
   ```

   默认 `listen=:8081`（OpenAI 兼容入口）、`admin_listen=:8082`（管理接口）。

2. 配置 TsukiyoHub 后端 `.env`：

   ```ini
   GATEWAY_ENABLED=true
   GATEWAY_ADMIN_URL=http://127.0.0.1:8082
   GATEWAY_PUBLIC_URL=http://127.0.0.1:8081
   GATEWAY_ADMIN_TOKEN=          # 与网关 config.json 的 admin_token 一致；留空则后端不带凭证
   GATEWAY_TIMEOUT_SECONDS=10
   ```

3. 重启后端，打开 `/routing`：状态条变绿即接通；再看 `/alerts` 是否读到规则。
   若网关配置了 `admin_token`，务必让后端使用同一令牌，否则页面会显示网关返回的 401 文案。

4. 验证顺序：供应商探活 → 通过网关发一次 `/v1/chat/completions` → `/routing` 追踪表出现记录
   → 把某条规则阈值改成 0 等待一个评估周期 → `/alerts` 出现 firing 事件 → 确认（Ack）。

## 7. 已知限制

- 「今日 token / 今日成本」是最近 500 条追踪内的近似累计，不是网关的按天账本；
  追踪环形缓冲满后更早记录会被淘汰。
- 追踪分页在浏览器侧完成，只拉最近 200 条（后端 `limit` 上限 500）。
- 页面不提供网关完整配置编辑（`PUT /config` 接口已在 `api/gateway.ts` 封装，暂无 UI），
  供应商 / 模型 / 熔断 / 预算仍在网关配置文件里改。
- 「立即评估」只是提示：告警评估周期完全由后端配置决定。
- 规则 ID 由前端为新增规则生成 `web_<时间戳>`；已存在规则保持原 ID。