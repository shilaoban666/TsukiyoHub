# TsukiyoHub 通用聊天工作台 · 前端设计文档

> 来源：`E:\Project\HQDingtalkRobotUI`（Vue 3.5 + TS 5.7 + Vite 6 + Ant Design Vue 4 管理后台）只读勘察。
> 目标：为 `E:\Project\TsukiyoHub`（FastAPI + 技能 + RAG 内核，`pyproject.toml` 已定 `app_port=8080`、`api_key` 可选 Bearer）产出一套**去后台化**的聊天前端资产清单与实现方案。
> 约定：下文 `src/...` 指 HQ 仓库路径；`web/...` 指 TsukiyoHub 新增前端目录（建议与 `server/` 平级）。

---

## 0. 结论先行

| 项 | 结论 |
| --- | --- |
| 可直接搬的硬资产 | `VirtualList.vue`、`markdown.ts`、`storage.ts`、`tokens.less`、`transitions.less`、`UserAvatar.vue`、`usePolling.ts`、`locales/index.ts`（结构）、`eslint.config.js`/`.prettierrc.json` |
| 值得改造的核心 | `request.ts`、`ChatBubble.vue`/`ChatBubbleList.vue`、`RetrievalProcess.vue`、`AgentTraceTree.vue`+`trace.ts`、`DebugDrawer.vue`、`knowledge/base.vue`、`PageContainer.vue`、`StatusTag.vue`、`ErrorBoundary.vue` |
| 必须丢弃 | `views/knowledge/sync.vue`、`router/routes.ts`、`router/guards.ts`、`utils/runtime.ts`、`utils/upload.ts`、`api/robot.ts`、`views/chat-records/index.vue`、`AgenticFlow.vue`、`layouts/*` |
| 关键纠正 | **SSE 实现不在 `src/api/chat.ts`，而在 `src/api/system.ts` 的 `debugStream()`**（第 60–103 行）；`api/chat.ts` 只有会话/轨迹/检索过程查询。新项目应搬 `system.ts` 的流式逻辑。 |
| 前端净规模 | 从 HQ 的 ~14.5k 行 src 收敛到 ~2.5k 行：4 页面 + 12 组件 + 3 store + 1 SSE composable |
| 估算 | 迁移 3 人日 + 新写 4 人日 ≈ **7 人日**（见 §6） |

---

## 1. 资产清单（逐文件裁决）

判定口径：**原样复用**＝复制后只改 import 路径；**改造**＝结构可用但需改实现/收敛 props；**丢弃**＝复用的改造成本 > 重写成本。

### 1.1 工程脚手架

| 文件 | 结论 | 改什么 / 为什么 | 工作量 |
| --- | --- | --- | --- |
| `package.json` | 改造 | 删 `echarts`/`vue-echarts`/`jsencrypt`/`@playwright/test`/`vitest`/`happy-dom`/`rollup-plugin-visualizer`/`vite-plugin-compression2`；`axios` 视 §4 决定（推荐 fetch，则删）；保留 `ant-design-vue`+`@ant-design/icons-vue`/`marked`/`dompurify`/`dayjs`/`pinia`+`pinia-plugin-persistedstate`/`@vueuse/core`/`less`；`name` 改 `tsukiyohub-web` | 0.5h |
| `vite.config.ts` | 改造 | 保留：`@` alias、`unplugin-vue-components`+`AntDesignVueResolver({importStyle:false})`（AntDV4 CSS-in-JS 无需引样式）、`css.preprocessorOptions.less`、`server.proxy`。删：preview 静态演示分支（`VITE_USE_MOCK`/`VITE_ROUTER_HISTORY`/`VITE_PUBLIC_BASE`）、`compression`、`visualizer`、`manualChunks` 里的 echarts 分支、`test` 段。代理改指 `http://127.0.0.1:8080` | 1h |
| `tsconfig.json` | 原样 | 仅 `include` 去掉 `tests/**`、`playwright*.ts`。`noImplicitAny:false` 建议改 `true`（新代码量小，能收住） | 10min |
| `index.html` | 改造 | 首帧主题脚本里 localStorage key `fancyqube-app` → `tsukiyohub-app`（与 §2 store key 对齐）、`lang` 默认 `zh-CN`；`title`/`description`/`favicon` 换品牌；**保留** boot spinner 防白屏 | 0.2h |
| `src/main.ts` | 改造 | 删 `loadRuntime()` 预取、`vPermission` 指令、mock 适配相关。保留 `app.config.errorHandler`（已过滤 `ERR_CANCELED`/`BizError`）+ `unhandledrejection` 静默 + `message.config`，`reportVitals` 可留可删 | 1h |
| `src/App.vue` | 改造 | 保留 `a-config-provider` + `themeConfig` + `a-app` + 路由进度条。删 `useLocaleSetup` 的 AntDV locale 动态加载可留；`app.viewportWidth` 监听保留（桌面/窄屏切换用） | 0.2h |
| `eslint.config.js` | 原样 | flat config，忽略 `src/types/components.d.ts` 很重要（unplugin 生成物） | 0 |
| `.prettierrc.json` | 原样 | `semi:false, singleQuote, printWidth:110, trailingComma:all`；与现有代码风格一致，别改 | 0 |

### 1.2 utils

| 文件 | 结论 | 改什么 / 为什么 | 工作量 |
| --- | --- | --- | --- |
| `src/utils/request.ts` | **改造（重点）** | 165 行里 4 处强耦合需解：① `import { message } from 'ant-design-vue'` 弹全局错误 → 抽 `setToast(fn)` 注入；② `errorText()` 读 `i18n.global` → 保留 i18n 或直接返回后端 `message`；③ 401 分支调 `useUserStore().refresh()` + `logout()` → TsukiyoHub 无账号体系，改成「清 apiKey + 提示重设」，仅保留**单次重试**逻辑；④ `dataAdapter` 演示数据适配器 + `isMockPath`/`hasBackend` → **整段删除**（约 -60 行）。保留：`BizError`、`pending` Set + `cancelPendingRequests()`（路由切换取消）、`RequestOptions{silent,keepOnRouteChange}`、`downloadFile()`、`isCanceled()`、`{code,message,data}` 拆包 | 2h |
| `src/utils/markdown.ts` | **原样** | 41 行，`marked`+`DOMPurify` 双保险，`renderMarkdown` 即插即用；`highlightHtml` 用 TreeWalker 只替换文本节点、不破坏标签，检索关键词高亮直接可用。唯一注意：流式期间别对半截 Markdown 反复调它（见 §4.5） | 0.1h |
| `src/utils/format.ts` | 改造 | `isZh()` 读 `i18n.global.locale.value` → 改成可选参数或保留 i18n。`fmtDateTime/fmtTime/fmtDuration/fmtNumber/fmtCompact/fmtBytes/fmtPercent/escapeHtml/highlight` 全部要；`fmtMoney`/`toCsv`/`downloadText` 删（无导出/计费需求） | 0.5h |
| `src/utils/runtime.ts` | **丢弃** | 114 行全是业务运行时开关：demo/live 双数据模式、`MOCK_ONLY_MODULES`、钉钉 `clientId/corpId`、`authRealm`。TsukiyoHub 只需 `/api/health` 一个 `online` 标志，重写 30 行即可 | 30min |
| `src/utils/storage.ts` | **原样** | 55 行，隐私模式/禁用存储下读写不抛错；`safeLocalStorage` 给 `pinia-plugin-persistedstate` 用，直接搬 | 0 |
| `src/utils/trace.ts` | 改造 | 瀑布条算法（`traceTree` 把 `rag.*` 子节点挂到 `rag`/`mixed` 父节点、按总耗时算 `offset/width`）值得保留；只需把 `TraceNode.node` 的命名约定换成 TsukiyoHub 的 `retrieval.*`/`skill.*`/`generate`/`judge` | 1h |
| `src/utils/upload.ts` | **丢弃** | 203 行分片上传 + OSS 直传 + 断点续传 + 签名 URL 回退，配套 `upload-session.ts`。TsukiyoHub `max_upload_mb=32`、`storage_backend=local`，一个 `FormData` POST + XHR `upload.onprogress` 约 40 行即可 | 0.5h |
| `src/utils/env.ts` | 原样 | 8 行，`API_BASE`/`APP_TITLE`/`ROUTER_HISTORY`，改默认标题 | 0 |
| `src/utils/crypto.ts` | 丢弃 | RSA 加密钉钉密码登录用，无账号体系不需要 | — |
| `src/utils/upload-session.ts` | 丢弃 | 只服务 `upload.ts` | — |

### 1.3 composables / stores

| 文件 | 结论 | 改什么 / 为什么 | 工作量 |
| --- | --- | --- | --- |
| `src/composables/useTheme.ts` | 改造 | `LIGHT`/`DARK`/`COMMON` token + `mql` 监听 + `data-theme` 同步 + AntDV `darkAlgorithm` 全留（70 行里 60 行可用）；`chartPalette()` 删（无图表）。**额外要加**：把 `themeMode` 存进 settings store 并响应 `prefers-color-scheme` | 0.5h |
| `src/composables/usePolling.ts` | **原样** | 38 行，绑定 `useDocumentVisibility` + `onActivated/onDeactivated`，标签页不可见自动停表、切回立即刷一次。会话列表轮询/文档状态轮询直接用 | 0 |
| `src/composables/useTable.ts` | 改造或丢弃 | 强耦合 `TablePaginationConfig` + `i18n` 的 `showTotal`。工作台会话列表是 **cursor 分页**（无限滚动），不需要 a-table；`useTable` 只在「知识库文档列表」用得上，可简化为 `usePagedList()`（去掉 sorter） | 1h |
| `src/stores/app.ts` | 改造 | 保留 `themeMode`/`locale`/`systemDark`/`isDark`/`setTheme`/`persist`（key 改 `tsukiyohub-app`，`pick` 去掉 `tabs`）；**删**多标签栏 `tabs` + `addTab/closeTab/closeOthers/closeSide/closeAll/moveTab` + `collapsed/sidebarCollapsed/toggleSidebar`（-50 行） | 0.5h |
| `src/stores/user.ts` | 改造 | 保留模式（state/getter/action + sessionStorage + 过期时间）但语义换成 **apiKey**：`apiKey`/`expiresAt`/`setKey`/`clear`/`isConfigured`；删 `refresh/scheduleRefresh/restore/logout` 与 `router`/`authRealm` 耦合（-40 行） | 0.5h |

### 1.4 components

| 文件 | 结论 | 改什么 / 为什么 | 工作量 |
| --- | --- | --- | --- |
| `src/components/VirtualList.vue` | **原样** | 133 行不定高虚拟列表：预估高度 + `ResizeObserver` 实测 + 二分查找可见区间 + `scrollToIndex`（实测后再校正一次）。纯泛型、零业务、零 AntDV 依赖。长会话（>200 条）必备 | 0 |
| `src/components/ChatBubble.vue` | 改造 | 气泡骨架/`role` 布局/`max-width:78%`/Markdown 排版 `:deep()` 样式全留。**删** 6 个后台动作：`rating` 赞踩回显、`addPending`、`addEval`、`hit===false` 的「未命中」tag、`latencyMs/tokens` meta（可留、建议留）。**改** 为 `<slot name="actions">` + `openReference` 事件，引用折叠区保留（含 `folderUrl/folderName` 可剥） | 1.5h |
| `src/components/ChatBubbleList.vue` | **改造（性能关键）** | 现实现 `computed` 里对**所有消息**每次变化重跑 `renderMarkdown` + `highlightHtml`（`messages` 任一字段变 → O(n) 次 `marked.parse`）。流式场景下每秒几十次 → **O(n²)** 直接卡死。必须改成：`Map<id+contentLength, html>` 缓存 + 流式中的那条走纯文本分支（详见 §4.5）。其余（虚拟列表阈值 200、`scrollToFirstHit`、`prevQuestion`）可保留 | 1h |
| `src/components/RetrievalProcess.vue` | 改造 | 100 行，信息密度极好：问题 → 模式/命中/耗时 → 感知意图 → 每轮 query → 候选表（doc/score/vector/bm25/是否引用+未引用原因）→ 最终引用列表。**保留结构与表格**；`a-descriptions`/`a-tag`/`a-alert` 可继续用 AntDV（已引入），i18n key 整体重写；`data.kbCalled`/`noSnapshot` 这类 RAG 专有分支按 TsukiyoHub 的 `grounded` 语义替换 | 1.5h |
| `src/components/AgentTraceTree.vue` | 改造 | 129 行：`route/verdict/cached/latencyMs/ttftMs/llmCalls/tokens` 头部 + `a-timeline` 节点 + **瀑布条**（`offset/width` 百分比，健康色 green/orange/blue/red）+ 子节点缩进。保留骨架与瀑布条，节点文案键换新；`trace.rag.perception` 之类的业务字段删 | 1.5h |
| `src/components/AgenticFlow.vue` | **丢弃** | 163 行，完全绑定 `AgenticConfig` + `@/views/agents/agenticFields` 的 `AgenticGroup` + 18 个 `agentic.flow.*` i18n key。TsukiyoHub 没有这套配置面；如需「流水线示意」，用静态 SVG/步骤条 20 行 | — |
| `src/components/PageContainer.vue` | 改造 | 88 行。`title/description/#extra/#title-extra` 插槽 + 内边距值得留；**删** 权限过滤 `user.has(t.permission)`、`a-tabs` 页内跳转、`router.replace` 污染 query 逻辑 → 简化为 `PageShell.vue`（~40 行） | 0.5h |
| `src/components/StatusTag.vue` | 改造 | 51 行 `MAP`（running/parsing/chunking/embedding/indexing/syncing/failed + `spin` + `progress%`）语义可直接映射到 TsukiyoHub 文档状态机；把 `$t()` 换直出文案、`a-tag` 换 `<span class="tag">` 即可（也可保留 a-tooltip） | 0.5h |
| `src/components/ErrorBoundary.vue` | 改造 | 35 行，`onErrorCaptured` 里**过滤 `isCanceled` 与 `BizError`**（避免把已提示过的接口错误当页面崩溃）这个判断很有价值；`a-result` 换自绘 + `route.fullPath` watch 重置保留 | 0.5h |
| `src/components/UserAvatar.vue` | **原样** | 23 行，取名字后 2 字 + 背景色，零依赖 | 0 |

### 1.5 api / views / locales / styles / router

| 文件 | 结论 | 改什么 / 为什么 | 工作量 |
| --- | --- | --- | --- |
| `src/api/system.ts`（`debugStream`） | **改造后复用（SSE 原型）** | 见 §4。要改 4 点：① `data: (.*)` 单行正则 → 支持多行 `data:`；② 增加 `id:`/`retry:` 与心跳；③ `abort` 后区分「用户中止」与「网络断开」；④ `delta` 语义显式化（现为**累积全文**，见 §3.4） | 2h |
| `src/api/chat.ts` | 改造 | 16 行，是**查询侧**契约：`/chat/sessions`（分页）、`/chat/stats`、`/chat/sessions/{id}`（含 messages）、`/chat/messages/{id}/trace`、`/chat/messages/{id}/retrieval`、`/chat/export`、`/audit/log`。路径与类型全换，但「列表 / 详情 / 轨迹 / 检索过程 四分离」的切分可直接沿用 | 1h |
| `src/api/knowledge.ts` | 重写 | 147 行里约 85% 是钉钉专属：`driveFolders`、`runs/runItems/reconcile/claimEvent/budget/releases`、`contributions`、`versions/pinVersion/relabel`、`createUpload/uploadParts/completeUpload`。TsukiyoHub 只需 12 个：docs 列表/上传/删除/重建、chunks、hit-test、stats、faq 可选 | 1.5h |
| `src/api/robot.ts` | 丢弃 | 87 行 provider/model-config/token 计费/记忆治理，全是运营后台 | — |
| `src/views/robot/DebugDrawer.vue` | 改造（**流式 UI 原型**） | 145 行，价值点：① 用户消息 + 占位 `'…'` 先入列表再流式填充；② `streamed` 标志——正文开始后不再被 `progress` 过程文案覆盖；③ `status==='searching'` → 「正在查询公司资料…」；④ `panes.value = {...panes.value}` 强制刷新的**反面教材**（应直接改数组元素，靠 reactive 更新）；⑤ **缺** 停止按钮/重连。迁移时把双栏 A/B 对比保留为可选「对比模式」 | 2h |
| `src/views/chat-records/index.vue` | 丢弃 | 361 行审计后台：水印 `a-watermark`、CSV 导出、部门/员工筛选、`usePermission`、URL 同步筛选。业务专属 | — |
| `src/views/chat-records/SessionDrawer.vue` | 改造 | 144 行，可借鉴：`a-drawer` 会话详情 + **二级抽屉**展示检索过程 + `scrollToFirstHit` 延时 320ms 等抽屉动画结束。新项目改成右侧 `CitationPanel`（非抽屉，常驻分栏） | 1h |
| `src/views/knowledge/base.vue` | 改造 | 350 行，可用部分：卡片/列表双视图（`local` 记忆偏好）、启停 switch、`TransitionGroup name="list"`、同步中 5s 轮询且仅 `visibilityState==='visible'` 时请求、`CAT_COLOR`/`SOURCE_ICON` 映射。删：`kb:edit` 权限、钉盘来源、`syncAll` | 2h |
| `src/views/knowledge/sync.vue` | **丢弃** | 660 行钉盘同步编排（调度/预算/事件认领/发布记录/体检卡），与 TsukiyoHub 无关 | — |
| `src/locales/index.ts` | **原样** | 47 行，`createI18n({legacy:false, globalInjection:true})` + 非默认语言 `import()` 按需加载 + `loadLocale` 同步 `<html lang>`。这套结构直接搬 | 0 |
| `src/locales/zh-CN/*.ts` | 重写 | 只保留 `common`/`chat`/`knowledge`/`settings` 四组，其余 11 组丢弃 | 1h |
| `src/styles/tokens.less` | **原样** | 91 行，已为聊天设计好 `--color-bubble-self/-text/-bot`、`--color-bg-sunken`、`--shadow-card/-float`、`--space-*`、`--fs-*`、`--ease/--dur-*`，亮/暗双份。**这是最划算的资产** | 0 |
| `src/styles/transitions.less` | **原样** | 87 行，`page/fade/slide-up/list` + `shake` + `prefers-reduced-motion` 全局降级 | 0 |
| `src/styles/global.less` | 改造 | 保留 `box-sizing` 重置、`html.theme-transition *` 主题过渡、滚动条、`.card/.grid-*/.text-2/.text-3/.mono/.ellipsis/.flex/.flex-between`、`mark.hl`；删 `.ant-btn:active{scale}`、`.ant-table-thead` 等钉钉拟态覆盖（可选保留） | 0.3h |
| `src/router/index.ts` | 原样 | 10 行，`createWebHistory(import.meta.env.BASE_URL)` + `scrollBehavior` | 0 |
| `src/router/routes.ts` | **丢弃** | 270 行 `MENU`/`menuShell`/`LEGACY_PATHS`/12 个菜单 + 权限声明 | — |
| `src/router/guards.ts` | 丢弃 | 96 行权限守卫/多标签迁移/`firstAccessiblePath`。仅保留 `router.onError` 里「发版后旧 chunk 404 自动刷新一次」的 15 行（很实用） | 0.3h |
| `src/directives/permission.ts` | 丢弃 | 无权限体系 | — |
| `src/env.d.ts` | 改造 | 删 `VITE_DATA_MODE`/`VITE_USE_MOCK`/`VITE_DD_REDIRECT_URI`/`Window.DTFrameLogin`；留 `VITE_API_BASE`/`VITE_APP_TITLE`/`VITE_ROUTER_HISTORY` + `*.vue` 声明 | 0.1h |
| `src/layouts/*` | 丢弃 | `BasicLayout/SideBar/HeaderBar/TabsBar` 是 AntDV 后台三件套（含菜单权限、多标签、KeepAlive `include`）；工作台用两栏自绘外壳（~120 行） | — |

---

## 2. 新前端形态建议

### 2.1 页面（4 条路由，无权限系统）

| 路由 | 页面 | 职责 |
| --- | --- | --- |
| `/chat`、`/chat/:sessionId` | `ChatView` | 主工作台：会话列表 + 消息流 + 引用/过程面板 + 技能面板（右栏 tab） |
| `/knowledge` | `KnowledgeView` | 文档上传/列表/状态、切块预览、命中测试 |
| `/skills` | `SkillsView` | 技能/MCP 开关、超时与启用清单（也可作为右栏面板，页面仅做批量管理） |
| `/settings` | `SettingsView` | 模型与检索参数、外观、API Key、后端连通性 |

### 2.2 目录结构

```
web/
├─ index.html  package.json  vite.config.ts  tsconfig.json
├─ .prettierrc.json  eslint.config.js  env.d.ts
└─ src/
   ├─ main.ts  App.vue
   ├─ api/
   │  ├─ http.ts            # request.ts 瘦身版（fetch 或 axios）
   │  ├─ sse.ts             # createSSE()：POST + ReadableStream 解析器
   │  ├─ types.ts           # §3 的类型定义（唯一真源）
   │  ├─ chat.ts  sessions.ts  skills.ts  knowledge.ts  config.ts
   ├─ composables/
   │  ├─ useChatStream.ts   # 状态机：idle→connecting→streaming→done/error/aborted
   │  ├─ useMarkdown.ts     # 节流渲染 + 增量边界切分
   │  ├─ useTheme.ts  usePolling.ts  useToast.ts
   ├─ stores/
   │  ├─ app.ts             # theme/locale/apiKey（持久化）
   │  ├─ session.ts         # 会话列表 + 消息 map + 流式状态
   │  └─ skills.ts
   ├─ components/
   │  ├─ layout/AppShell.vue  PageShell.vue  ErrorBoundary.vue
   │  ├─ chat/ChatWorkbench.vue  SessionList.vue  MessageList.vue
   │  ├─ chat/ChatBubble.vue  StreamingBubble.vue  Composer.vue
   │  ├─ chat/CitationPanel.vue  RetrievalProcess.vue  TraceTimeline.vue
   │  ├─ chat/SkillPanel.vue  SkillBadge.vue
   │  ├─ base/VirtualList.vue  StatusTag.vue  UserAvatar.vue  EmptyState.vue
   │  └─ knowledge/DocTable.vue  UploadDropzone.vue  ChunkDrawer.vue  HitTestPanel.vue
   ├─ views/ ChatView.vue  KnowledgeView.vue  SkillsView.vue  SettingsView.vue
   ├─ router/index.ts
   ├─ locales/ index.ts  zh-CN.ts  en-US.ts
   └─ styles/ tokens.less  global.less  transitions.less
```

### 2.3 组件树

```
App.vue (a-config-provider: themeConfig + antdLocale)
└─ AppShell.vue                       ← 自绘两栏，替代 BasicLayout
   ├─ TopBar.vue                     ← 品牌 / 主题切换 / 语言 / 设置入口 / 连接状态
   └─ RouterView (无 KeepAlive，聊天态由 store 保活)

ChatView.vue
└─ ChatWorkbench.vue                 ← 三栏 grid：260px | 1fr | 380px（可折叠）
   ├─ SessionList.vue                ← cursor 分页 + 删除/重命名 + 「新对话」
   │  └─ VirtualList.vue             ← >50 条启用
   ├─ ChatPane.vue
   │  ├─ MessageList.vue             ← 自动贴底 / 上滑暂停 / 「回到底部」
   │  │  ├─ VirtualList.vue          ← >200 条启用（复用 HQ 实现）
   │  │  ├─ ChatBubble.vue           ← 已完成消息（v-html 无 DOM 直接更新：需注意见 §4)
   │  │  └─ StreamingBubble.vue      ← 流式中：纯文本+光标，结束才切 Markdown
   │  ├─ ProcessRail.vue             ← 横向步骤条：检索中→调用技能→生成→校对
   │  └─ Composer.vue                ← textarea 自适应行高 / Enter 发送 / Shift+Enter 换行
   │                                    / 「停止」按钮（streaming 时替换发送）
   └─ SidePanel.vue                  ← a-tabs：引用 | 过程 | 技能
      ├─ CitationPanel.vue           ← Citation[] 卡片，点击回填 [n] 高亮
      ├─ RetrievalProcess.vue        ← 改造自 HQ 同名组件
      └─ SkillPanel.vue              ← SkillInfo[] 开关 + 本次调用记录
```

### 2.4 状态设计

| store | state | 说明 |
| --- | --- | --- |
| `app` | `themeMode` / `locale` / `systemDark` / `apiKey` / `baseUrl` | `persist: {key:'tsukiyohub-app', pick:['themeMode','locale','apiKey']}`；apiKey 也可只放 sessionStorage |
| `session` | `sessions: SessionInfo[]`、`cursor`、`activeId`、`messages: Record<string, ChatMessage[]>`、`streaming: {sid, mid, phase, text, citations, skills} \| null` | 会话懒加载：进入会话才 `GET /sessions/{id}`；新建会话先本地乐观插入（`id = 'tmp-...'`），`meta` 事件回来再替换真实 id |
| `skills` | `list: SkillInfo[]`、`enabled: Set<string>`、`lastRun: Record<string,{ms,status}>` | 5s 轮询只在面板展开时开（`usePolling` 自动处理不可见暂停） |
| 流式运行时 | 模块级单例（非 store） | `AbortController`、`seq`、`lastEventAt` 放模块作用域，避免被 Vue 响应式代理（controller 不能被 proxy） |

**关键约束**：`messages` 里的消息对象**必须整体替换或只改标量字段**，不要 `messages.value = {...}` 全量拷贝（HQ `DebugDrawer.vue` 第 38/44 行的写法会触发整列重渲染）。

---

## 3. API 契约

### 3.1 端点清单

统一前缀 `/api`（Vite 代理同名），REST 统一返回 `{code:0,message:"ok",data}`，错误用 HTTP 状态 + `{code,message}` 体。标注 **现有** = TsukiyoHub 代码已有配置面；**需新增** = 前端需要但后端需实现。

| 方法 | 路径 | 状态 | 用途 / 要点 |
| --- | --- | --- | --- |
| GET | `/api/health` | 需新增 | `{ok, version, llmReady, embeddingReady, vectorBackend}`，顶栏连接灯 |
| POST | `/api/chat/stream` | **需新增（核心）** | 请求 `ChatRequest`，响应 `text/event-stream`。**必须 POST**（问题体可能大、要带 Authorization） |
| POST | `/api/chat` | 需新增 | 非流式兜底，返回 `ChatResponse`（SSR 降级/不支持流的环境） |
| GET | `/api/sessions?cursor&limit&q` | 需新增 | `PageResult<SessionInfo>`，游标分页（`cursor` 为空即到头） |
| GET | `/api/sessions/{id}` | 需新增 | `SessionInfo & {messages: ChatMessage[]}` |
| PATCH | `/api/sessions/{id}` | 需新增 | `{title?, pinned?}` |
| DELETE | `/api/sessions/{id}` | 需新增 | 硬/软删 |
| POST | `/api/sessions/{id}/compact` | 可选 | 触发长对话摘要（对应 `history_summary_after`） |
| GET | `/api/messages/{id}/citations` | 可选 | 消息详情补拉引用（刷新页面后） |
| GET | `/api/messages/{id}/trace` | 可选 | 复用 HQ `chat.ts` 的「轨迹单独查」切分 |
| POST | `/api/messages/{id}/feedback` | 需新增 | `{rating:'up'\|'down'}` |
| GET | `/api/skills` | 需新增 | `SkillInfo[]`（内置 + `SKILL.md` 目录 + MCP） |
| PATCH | `/api/skills/{name}` | 需新增 | `{enabled:boolean}` |
| GET | `/api/knowledge/docs?cursor&limit&status` | 需新增 | `PageResult<KnowledgeDoc>` |
| POST | `/api/knowledge/docs` | 需新增 | `multipart/form-data`，字段 `file`；超 `max_upload_mb` 返回 413 |
| DELETE | `/api/knowledge/docs/{id}` | 需新增 | |
| POST | `/api/knowledge/docs/{id}/reindex` | 需新增 | 重新解析+切块+向量化 |
| GET | `/api/knowledge/docs/{id}/chunks` | 需新增 | `{doc, chunks:[{id,index,content,tokens}]}` |
| POST | `/api/knowledge/search` | 需新增 | `{query, topK}` → `Citation[]`，对应 HQ 的 `hit-test` |
| GET | `/api/knowledge/stats` | 可选 | 文档数/切块数/库大小 |
| GET | `/api/config` | 需新增 | 生效配置（**密钥一律脱敏**）：`llm_model`、`retrieval_top_k`、`grounding_min_score`、`skills_enabled`、`default_tone` 等 |
| PUT | `/api/config` | 需新增 | 部分覆盖（落库或写 `.env.local`）；FastAPI 侧注意 `get_settings()` 是 `lru_cache`，需 `cache_clear()` 或改为可热更的 overlay |

> 注意：`server/tsukiyohub/config.py` 目前是纯 pydantic-settings（env 驱动）。UI 里的「模型设置」要么只读展示（推荐第一版），要么后端加一层 **DB 覆盖表**，不要直接改 env —— 否则 `lru_cache` 单例不同步。

### 3.2 鉴权

- 后端：`Settings.api_key` 留空 = 不鉴权（本地）；填了则要求 `Authorization: Bearer <key>`。
- 前端：`SettingsView` 里填 key → `sessionStorage`（`app.apiKey`）；`http.ts` 与 `sse.ts` **统一从 store 取**并加头。
- **不要**搬 HQ 的钉钉扫码/密码登录（`api/auth.ts` 22 行、`jsencrypt`、`/auth/qrcode`、refresh token 轮转）——TsukiyoHub 无用户体系，业务方接入时应自行在前置网关做身份。
- 401/403 处理：清空 apiKey + 顶栏提示「API Key 无效或缺失」，**不跳转登录页**。

### 3.3 TypeScript 类型草案（`web/src/api/types.ts`）

```ts
export interface ApiEnvelope<T> { code: number; message: string; data: T }
export interface PageResult<T> { list: T[]; total: number; cursor?: string | null }

export interface Usage { promptTokens: number; completionTokens: number; totalTokens: number }

export interface Citation {
  index: number          // 正文里的 [1][2]
  docId: string
  docName: string
  chunkId: string
  snippet: string        // 已截断的片段，200~400 字
  score: number          // 融合分（0~1）
  vectorScore?: number | null
  bm25Score?: number | null
  page?: number | null
  url?: string
  cited?: boolean        // false = 召回但未被引用（配合 reason 解释）
  reason?: string
}

export interface SkillInfo {
  name: string
  title: string
  description: string
  kind: 'builtin' | 'skill_md' | 'mcp'
  enabled: boolean
  source?: string          // 目录名 / MCP server 名
  paramsSchema?: Record<string, unknown>
  timeoutSec?: number
  calls?: number
}

export type DocStatus = 'pending' | 'parsing' | 'chunking' | 'embedding' | 'ready' | 'failed'

export interface KnowledgeDoc {
  id: string
  name: string
  size: number
  mime: string
  status: DocStatus
  progress?: number        // 0~100，仅处理中有意义
  chunkCount: number
  hash?: string
  error?: string | null
  createdAt: string
  updatedAt: string
}

export interface SessionInfo {
  id: string
  title: string
  preview?: string
  messageCount: number
  createdAt: string
  lastMessageAt: string
  pinned?: boolean
}

export interface TraceNode {
  node: string             // 'retrieval.vector' | 'skill.web_search' | 'generate' | 'judge'
  ms: number
  status: 'ok' | 'degraded' | 'skipped' | 'error'
  summary: string
  [k: string]: unknown
}

export interface ChatMessage {
  id: string
  sessionId: string
  role: 'user' | 'assistant' | 'tool'
  content: string
  createdAt: string
  status?: 'streaming' | 'done' | 'error' | 'aborted'
  citations?: Citation[]
  skillsUsed?: string[]
  trace?: TraceNode[]
  usage?: Usage
  latencyMs?: number
  feedback?: 'up' | 'down' | null
}

// ---------- 请求 / 响应 ----------
export interface ChatRequest {
  sessionId?: string | null       // 为空 = 新建会话
  message: string
  stream?: boolean
  clientRequestId: string         // 幂等键：网络重试不会重复落库
  skillNames?: string[]           // 本次强制启用的技能；缺省用全局开关
  attachments?: string[]          // 已上传文档 id
  overrides?: {                   // 单次覆盖，不落配置
    model?: string
    temperature?: number
    maxTokens?: number
    topK?: number
    groundingMinScore?: number
  }
}

export interface ChatResponse {
  sessionId: string
  messageId: string
  content: string
  citations: Citation[]
  skillsUsed: string[]
  trace: TraceNode[]
  usage: Usage
  latencyMs: number
  ttftMs?: number
  grounded: boolean                // false = 未命中知识库，走了兜底话术
  finishReason: 'stop' | 'length' | 'error' | 'aborted'
}

// ---------- SSE ----------
export type SSEEvent =
  | { event: 'meta';   data: { sessionId: string; messageId: string; model: string } }
  | { event: 'status'; data: { phase: 'retrieving' | 'skill' | 'generating' | 'judging'; text: string } }
  | { event: 'citation'; data: Citation }
  | { event: 'skill';  data: { name: string; status: 'start' | 'ok' | 'error'; ms?: number; error?: string } }
  | { event: 'delta';  data: { seq: number; text: string } }   // 增量，见 §3.4
  | { event: 'usage';  data: Usage }
  | { event: 'done';   data: ChatResponse }
  | { event: 'error';  data: { code: number; message: string; retryable: boolean } }
  | { event: 'ping';   data: { t: number } }

export type SSEEventName = SSEEvent['event']
export type SSEPayload<K extends SSEEventName> = Extract<SSEEvent, { event: K }>['data']
```

### 3.4 SSE 帧格式（与后端逐字节约定）

```
event: meta
data: {"sessionId":"s_01","messageId":"m_07","model":"gpt-4o-mini"}

event: status
data: {"phase":"retrieving","text":"正在检索知识库…"}

event: citation
data: {"index":1,"docId":"d_3","docName":"policy.md","chunkId":"c_12","snippet":"…","score":0.82,"cited":true}

event: skill
data: {"name":"web_search","status":"start"}

event: delta
data: {"seq":1,"text":"报销"}

event: usage
data: {"promptTokens":812,"completionTokens":96,"totalTokens":908}

event: done
data: {"sessionId":"s_01","messageId":"m_07","content":"…完整正文…","citations":[…],"skillsUsed":["web_search"],"trace":[…],"usage":{…},"latencyMs":1830,"ttftMs":420,"grounded":true,"finishReason":"stop"}

event: error
data: {"code":502,"message":"上游模型超时","retryable":true}
```

规则（**必须写进后端**）：

1. 帧分隔符固定 `\n\n`；每帧 `event:` 一行 + `data:` **单行紧凑 JSON**（`json.dumps(payload, ensure_ascii=False, separators=(',',':'))`，天然不含裸换行）。若将来允许多行 `data:`，前端按 SSE 规范拼接后再 parse（§4 已实现）。
2. 无消息时每 **15s** 发 `: ping\n\n`（注释帧，前端忽略），防止 Nginx/网关 60s 空闲掐流。
3. 响应头：`Content-Type: text/event-stream; charset=utf-8`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`、`Connection: keep-alive`。
4. **`delta.data.text` 是增量**（append 语义），带自增 `seq`。⚠️ HQ 现实现是**累积全文**（`DebugDrawer.vue` 第 34 行 `placeholder.content = e.value` 直接赋值），那也是合法方案——好处是断线重连可幂等，坏处是 `O(n²)` 带宽。**新契约选增量 + `seq` 去重**，前端同时兼容两种：`if (seq === lastSeq + 1) append else if (seq > lastSeq) replace`。
5. `done` 一定发（含正文全文，便于前端校正累积误差）；`error` 之后关闭流。**不要**发 `data: [DONE]`（HQ 也没有）。
6. `event: error` 与 HTTP 非 2xx 是两条路径：流已开始（200）后的失败只能走 `error` 帧。

---

## 4. 流式实现方案

### 4.1 为什么不用 EventSource

| | EventSource | fetch + ReadableStream（推荐） |
| --- | --- | --- |
| POST body | ❌ 只能 GET | ✅ |
| 自定义头（Bearer） | ❌ | ✅ |
| 主动中止 | `close()`（够用） | `AbortController`（更细） |
| 自动重连 | ✅ 内置（带 `Last-Event-ID`） | 需自己写（但重连策略本来就该定制） |

结论：**用 fetch + ReadableStream**，照抄 HQ `src/api/system.ts:72-103` 的骨架，补 §4.2 的健壮性。

### 4.2 SSE 解析器（`web/src/api/sse.ts`）

```ts
export interface SSEFrame { event: string; data: string; id?: string }

export function createSSEParser(onFrame: (f: SSEFrame) => void) {
  let buf = ''
  return (chunk: string, flush = false) => {
    buf += chunk.replace(/\r\n/g, '\n')          // 兼容 CRLF
    for (;;) {
      const i = buf.indexOf('\n\n')
      if (i < 0) break
      const raw = buf.slice(0, i)
      buf = buf.slice(i + 2)
      let event = 'message', id: string | undefined
      const data: string[] = []
      for (const line of raw.split('\n')) {
        if (!line || line.startsWith(':')) continue   // 注释/心跳
        const c = line.indexOf(':')
        const field = c < 0 ? line : line.slice(0, c)
        const value = c < 0 ? '' : line.slice(c + 1).replace(/^ /, '')
        if (field === 'event') event = value
        else if (field === 'data') data.push(value)   // 多行 data 拼接
        else if (field === 'id') id = value
        // retry 字段前端不用（重连策略见 4.4）
      }
      if (data.length) onFrame({ event, data: data.join('\n'), id })
    }
    if (flush && buf.trim()) { /* 流结束仍残留半帧：丢弃并告警，避免半截 JSON 抛错 */ buf = '' }
  }
}
```

### 4.3 `useChatStream` 主流程

```ts
// web/src/composables/useChatStream.ts
export async function send(req: ChatRequest) {
  const ctrl = new AbortController()
  state.value = 'connecting'
  const res = await fetch(`${API_BASE}/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream', ...authHeader() },
    body: JSON.stringify({ ...req, stream: true }),
    signal: ctrl.signal,
    credentials: 'include',
  })
  if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`)   // 401/413/429 在这里
  state.value = 'streaming'

  const feed = createSSEParser(({ event, data }) => {
    const payload = JSON.parse(data)          // 单行 JSON，parse 失败说明后端违约 → 抛
    switch (event) {
      case 'meta':   /* 用真实 sessionId/messageId 替换临时 id */ break
      case 'status': phase.value = payload; break
      case 'citation': citations.value.push(payload); break
      case 'skill':  markSkillRun(payload); break
      case 'delta':  appendDelta(payload.seq, payload.text); break   // seq 去重
      case 'usage':  usage.value = payload; break
      case 'done':   finalize(payload); break
      case 'error':  throw Object.assign(new Error(payload.message), payload)
    }
  })

  const reader = res.body.getReader()
  const dec = new TextDecoder('utf-8')
  let lastByteAt = Date.now()
  const watchdog = setInterval(() => {
    if (Date.now() - lastByteAt > 45_000) ctrl.abort(new DOMException('idle timeout', 'TimeoutError'))
  }, 5_000)

  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      lastByteAt = Date.now()
      feed(dec.decode(value, { stream: true }))
    }
    feed(dec.decode(), true)                   // flush 尾部
    if (!finalized.value) throw new Error('stream ended without done')
    state.value = 'done'
  } finally {
    clearInterval(watchdog)
    reader.releaseLock?.()
  }
}
```

**错误分类**（决定 UI 文案与是否给「重试」）：

| 情况 | 识别 | 处理 |
| --- | --- | --- |
| 用户点停止 | `ctrl.abort()` 且 `reason` 非 Timeout | 保留已收到文本，消息 `status='aborted'`，显示「已停止 · 继续生成」 |
| 空闲超时 | watchdog abort | 同上，文案「响应超时」 |
| 网络断 | `reader.read()` reject `TypeError` | 保留已收文本 + 「重试」；**不自动续写**（避免重复半句） |
| 后端 `error` 帧 | `retryable` | 可重试则按钮亮起 |
| HTTP 非 2xx | `res.ok === false` | 4xx 直接展示 body.message；5xx 提示稍后重试 |
| 流正常结束但无 `done` | 循环结束未 finalize | 视为失败（HQ 同款判断），但保留文本 |

### 4.4 重连策略

**不做自动续写。** 理由：半句正文 + 重连续写极易出现重复/错位。折中：

1. **零 token 时失败**（`seq` 尚未收到）→ 自动重试 2 次，间隔 500ms / 1500ms，UI 显示「正在重试 (1/2)」。
2. **已有 token 时失败** → 不自动重试，给「重新生成」；若后端支持 `Last-Event-ID` 且 `meta` 帧带 `id:`，可选做「续传」，需后端按 `id` 回放增量（当前 `config.py` 无此机制，**第一版不做**）。
3. 服务端侧建议：`ChatRequest.clientRequestId` 做幂等，重试不产生两条消息。

### 4.5 逐字渲染与 Markdown 增量节流

三层，从便宜到贵：

**① chunk 到达即渲染（默认，推荐）**
SSE 天然按 token/chunk 到达，`appendDelta` 里只改 `streaming.text`（一个 ref 字符串）→ 单节点文本更新，成本极低。**不要**为了「打字机动画」再拆成定时器逐字 push（长回答会积压、CPU 抖动）。

**② 用 rAF 合并多次 delta**
后端一个 TCP 包可能带 20 个 delta 帧，逐帧赋值会触发 20 次渲染：

```ts
let pending = '', raf = 0
function appendDelta(seq: number, text: string) {
  if (seq <= lastSeq) return           // 去重/乱序保护
  lastSeq = seq
  pending += text
  if (!raf) raf = requestAnimationFrame(() => { streaming.text += pending; pending = ''; raf = 0 })
}
```

**③ Markdown 只对「已完成段落」增量渲染，尾部保持纯文本**

流式中直接 `v-html` 半截 Markdown 会闪烁（未闭合的 ``` / ** / 表格会整段跳变）。策略：

```ts
// web/src/composables/useMarkdown.ts
const RENDER_INTERVAL = 100   // ms

export function useStreamingMarkdown(text: Ref<string>) {
  const html = ref('')
  let last = 0, timer: number | undefined

  watch(text, (t) => {
    const now = performance.now()
    const run = () => { html.value = renderStable(t) }
    if (now - last >= RENDER_INTERVAL) { last = now; run() }
    else if (!timer) timer = window.setTimeout(() => { timer = undefined; last = performance.now(); run() }, RENDER_INTERVAL)
  })

  // 稳定前缀 = 最后一个空行之前（块级边界），尾部走纯文本，避免半截语法
  function renderStable(t: string) {
    const cut = t.lastIndexOf('\n\n')
    if (cut < 0) return `<p>${escapeHtml(t)}</p>`          // 只有一个段落：纯文本更稳
    return renderMarkdown(t.slice(0, cut)) + `<p class="tail">${escapeHtml(t.slice(cut))}</p>`
  }
  return { html }
}
```

- `renderMarkdown` 直接用 HQ `src/utils/markdown.ts`（marked + DOMPurify），**必须**保留 DOMPurify：模型输出可含 `<script>`。
- 流结束后（`done`）用 `finalize` 里**完整正文**跑一次 `renderMarkdown` 覆盖，保证代码块/表格正确。
- 代码块流式体验：检测尾部未闭合 ``` 时，把该段包成 `<pre class="code streaming">` 直接显示纯文本，闭合后交给 marked。

**④ 列表性能**：`MessageList` 里已完成的 `ChatBubble` 用 `v-memo="[m.id, m.content.length]"`，避免流式期间整列 diff；>200 条切 `VirtualList.vue`。注意虚拟列表 + 自动贴底会打架：贴底用「滚动事件里判断 `scrollHeight - scrollTop - clientHeight < 40`」的 sticky 开关，用户上滑即暂停自动滚动。

---

## 5. 工程配置

### 5.1 `vite.config.ts`（精简后）

```ts
import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { AntDesignVueResolver } from 'unplugin-vue-components/resolvers'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  return {
    base: env.VITE_PUBLIC_BASE || '/',
    plugins: [
      vue(),
      Components({ dts: 'src/types/components.d.ts', dirs: [], resolvers: [AntDesignVueResolver({ importStyle: false })] }),
    ],
    resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
    css: { preprocessorOptions: { less: { javascriptEnabled: true } } },
    server: {
      port: 5173,
      host: true,
      proxy: {
        // SSE 也能过 vite 代理；关键是后端别开 gzip、要带 X-Accel-Buffering: no
        '/api': { target: env.VITE_API_PROXY || 'http://127.0.0.1:8080', changeOrigin: true },
      },
    },
    build: { target: 'es2020', cssCodeSplit: true, chunkSizeWarningLimit: 900 },
  }
})
```

### 5.2 `tsconfig.json`

```json
{
  "compilerOptions": {
    "target": "ES2020", "module": "ESNext", "moduleResolution": "Bundler",
    "lib": ["ES2021", "DOM", "DOM.Iterable"], "jsx": "preserve",
    "strict": true, "noImplicitAny": true, "resolveJsonModule": true,
    "isolatedModules": true, "esModuleInterop": true, "skipLibCheck": true,
    "useDefineForClassFields": true, "noEmit": true,
    "types": ["vite/client", "node"],
    "baseUrl": ".", "paths": { "@/*": ["src/*"] }
  },
  "include": ["src/**/*.ts", "src/**/*.d.ts", "src/**/*.vue", "vite.config.ts", "env.d.ts"]
}
```

### 5.3 eslint / prettier

`eslint.config.js` 与 `.prettierrc.json` **原样复制**（§1.1）。唯一建议增强：加 `@typescript-eslint/no-floating-promises: error`（流式代码里漏 `await` 会静默吞异常）。

### 5.4 依赖清单

| 类型 | 包 | 版本 | 说明 |
| --- | --- | --- | --- |
| runtime | `vue` | ^3.5 | |
| | `vue-router` | ^4.5 | 4 条路由，`createWebHistory` |
| | `pinia` + `pinia-plugin-persistedstate` | ^2.3 / 4.2.0 | 固定 4.2.0（4.3+ 改了 API） |
| | `ant-design-vue` + `@ant-design/icons-vue` | ^4.2 / ^7.0 | 若想彻底去 AntDV，见风险 R2 |
| | `marked` + `dompurify` | ^15.0 / ^3.2 | Markdown 渲染 + XSS 过滤，**不可省** |
| | `dayjs` | ^1.11 | 相对时间/格式化 |
| | `@vueuse/core` | ^13.0 | 只为 `useDocumentVisibility`（`usePolling`）；也可删掉自写 5 行 |
| | `less` | ^4.2 | 样式（devDep 亦可） |
| dev | `vite` `@vitejs/plugin-vue` `typescript` `vue-tsc` | ^6.2 / ^5.2 / ~5.7 / ^2.2 | |
| | `unplugin-vue-components` | ^28.4 | AntDV 按需自动引入 |
| | `eslint` `eslint-plugin-vue` `typescript-eslint` `vue-eslint-parser` `prettier` | 同 HQ | |
| **删除** | `echarts` `vue-echarts` `jsencrypt` `axios` `@playwright/test` `vitest` `happy-dom` `@vue/test-utils` `rollup-plugin-visualizer` `vite-plugin-compression2` | | 见下 |

**不要 echarts 等图表依赖怎么办**：工作台本身零图表需求（会话、消息、引用、技能都是列表/文本）。若 `KnowledgeView` 想要「文档类型分布」或 `SettingsView` 想要「token 趋势」：

1. **优先不画** —— 用 HQ 的 `.stat` 文本块（`chat-records/index.vue` 顶部那排数字）即可，零依赖。
2. 真要图：`const { default: ECharts } = await import('echarts/core')` 放在组件 `onMounted` 里动态导入，配合 `manualChunks` 只在 `/knowledge` 路由命中时加载；`vue-echarts` 可完全不用（直接 `echarts/core` + `use()` 按需注册）。
3. 轻量替代：纯 CSS 柱状条 / 内联 SVG 环形图（< 60 行），首屏零成本。

**移除 axios 的收益**：`request.ts` 里 60 行都在给 axios 打补丁（adapter、interceptor、cancel token）。用 fetch 重写后约 100 行，且天然与 `sse.ts` 统一 `Authorization` 处理。若想少改，保留 axios 也可，但**流式请求仍必须走原生 fetch**。

---

## 6. 依赖与风险

### 6.1 深度耦合清单与最小改造

| 耦合点 | 涉事文件 | 最小改造 | 工作量 |
| --- | --- | --- | --- |
| **权限路由** `user.has(perm)` + `MENU` + `menuShell` + `meta.permission` | `router/routes.ts`(270) `router/guards.ts`(96) `PageContainer.vue` `directives/permission.ts` `layouts/*` | 整层删除，写 4 条扁平路由 + 一个 `beforeEach`（仅设 `document.title`）。**不要**保留 `MENU` 结构再裁剪——它和「一个菜单多个 tab」的壳机制绑死 | 1d |
| **Ant 布局** `BasicLayout` 三件套（侧栏/顶栏/多标签） | `layouts/BasicLayout.vue` `SideBar.vue` `HeaderBar.vue` `TabsBar.vue` | 自绘 `AppShell.vue`：`display:grid; grid-template-columns: 260px 1fr`，顶栏 48px。多标签栏直接不做（聊天是单页面应用形态，标签无意义） | 0.5d |
| **i18n** `$t()` 遍布组件 + `errorText()` 动态 key | 所有 `.vue` + `format.ts` + `request.ts` + `locales/*` | 两条路：① 保留 `vue-i18n`（成本 1d，但 en 文案要重写）；② **推荐** 第一版只做 zh-CN，把 `$t('x')` 直接内联中文，保留 `locales/index.ts` 结构待后续接入（成本 0.2d）。注意 `StatusTag`/`TraceTimeline` 的 `MAP` 里存的是 key，改直出文案 | 0.2~1d |
| **`useTheme` + AntDV `ConfigProvider`** | `App.vue` `useTheme.ts` `tokens.less` | 保留（这是资产不是负债）：`tokens.less` 提供 CSS 变量，`ConfigProvider` 只喂 primary/borderRadius 等 8 个 token | 0.3d |
| **`useTable` + `a-table`** | `useTable.ts` | 会话列表用游标 + 无限滚动，不用表格；仅文档列表保留（可换 `a-list`） | 0.3d |
| **`request.ts` 的提示/401/i18n 三处** | `request.ts` | 注入 `setToast()`、删 mock adapter、401 改「清 key」 | 0.3d |
| **`ChatBubble` 的 6 个后台动作** | `ChatBubble.vue` `ChatBubbleList.vue` | props 收敛到 `{message, html}` + `#actions` 插槽 + `openReference` 事件 | 0.3d |
| **`ChatBubbleList` 全量 Markdown 重算** | `ChatBubbleList.vue` 第 26–33 行 | 按 `id + content.length` 缓存 html；流式消息不进这个 map。**这是流式能否跑顺的分水岭** | 0.5d |
| **`upload.ts` 分片/OSS** | `upload.ts`(203) `upload-session.ts` | 删，改 40 行 `FormData` + XHR 进度 | 0.3d |
| **mock 数据层** | `src/mock/**`(≈20 文件) `runtime.ts` | 整目录不复制 | 0 |
| **钉钉登录** | `api/auth.ts` `views/login/*` `utils/crypto.ts` `utils/dingtalk.ts` | 不复制；换 API Key 输入框 | 0 |

### 6.2 需新写（无对应资产）

| 模块 | 说明 | 工作量 |
| --- | --- | --- |
| `api/sse.ts` + `useChatStream` | 解析器/状态机/中止/超时/重试/seq 去重（§4） | 1.5d |
| `StreamingBubble.vue` | 流式气泡：纯文本 + 闪烁光标 + 停止/重试按钮 | 0.3d |
| `useStreamingMarkdown` | 稳定前缀切分 + 100ms 节流 + done 后全量覆盖 | 0.5d |
| `SessionList` + `session` store | 游标分页、乐观新建、标题自动生成（取首条消息前 20 字） | 1d |
| `Composer` | 自适应高度、Enter/Shift+Enter、快捷键、附件入口 | 0.3d |
| `CitationPanel` | 引用卡片 + 点击回填 `[n]` 高亮 + 未引用原因 | 0.5d |
| `SkillPanel` + `skills` store | 开关 + 本次调用耗时/失败展示 | 0.5d |
| 知识库上传/续传状态 | 上传队列、进度、失败重试（单请求版） | 0.5d |
| `SettingsView` | 模型/检索参数表单 + 连通性测试 + Key 管理 | 0.5d |
| `AppShell` + 4 路由 + `api/http.ts` | | 0.7d |

### 6.3 风险登记

| # | 风险 | 影响 | 缓解 |
| --- | --- | --- | --- |
| R1 | **后端 SSE 尚未实现**（`server/tsukiyohub/` 目前只有 `config.py`） | 前端无法联调，只能对着 mock | 先写 30 行 FastAPI `StreamingResponse` 桩：按 §3.4 逐字吐固定文本 + 一个 `done`，前端即可完整开发；`api/sse.ts` 里保留 `VITE_MOCK_STREAM=1` 开关 |
| R2 | AntDV 体积（~350KB gz）+ CSS-in-JS 运行时 | 首屏慢 | 按需引入已在（`unplugin-vue-components`）；若极致优化，用 `a-input`/`a-button`/`a-drawer`/`a-tag` 之外自绘（约 5 个组件可去掉 AntDV）。**第一版不建议**，节省 2 人日 |
| R3 | 长会话渲染卡顿（>500 条） | 滚动掉帧 | `VirtualList` + `v-memo` + `content-visibility:auto`；单条气泡 `max-width:78%` 已由 HQ 验证 |
| R4 | Markdown 半截渲染闪烁 | 观感差 | §4.5 的稳定前缀策略；代码块专判；`done` 后全量重渲染 |
| R5 | 网关/Nginx 缓冲导致「假流式」（一次性吐出） | 体验退化 | 后端 `X-Accel-Buffering: no` + `no-cache`；前端有 45s 空闲 watchdog，能识别「一直没字节」与「一次来一堆」 |
| R6 | `config.py` 的 `lru_cache` 单例 + env 读取 | UI 改配置不生效 | 第一版「模型设置」**只读展示**；写操作走独立 `overrides` 表，请求时随 `ChatRequest.overrides` 传入（无状态，立即可用） |
| R7 | `delta` 语义两套（累积 vs 增量） | 正文重复/丢失 | 契约固定增量 + `seq`；前端兼容累积（`seq` 不递增时按全文替换）；`done.content` 兜底校正 |
| R8 | i18n 半途引入导致返工 | 返工 1d | 第一版单语；所有用户可见文案集中到 `locales/zh-CN.ts`（哪怕只有一个语言），后续加 en 只改这一处 |

### 6.4 建议实施顺序

1. **骨架**（0.5d）：`web/` 脚手架 + `tokens.less`/`transitions.less`/`global.less` + `AppShell` + 4 路由 + `http.ts`。
2. **假流式打通**（1d）：后端 `StreamingResponse` 桩 → `sse.ts` + `useChatStream` + `StreamingBubble` → 能看到逐字输出。
3. **真聊天**（1.5d）：`session` store + `SessionList` + `MessageList` + `ChatBubble`（改造）+ `Composer`。
4. **过程可视化**（1.5d）：`CitationPanel` + `RetrievalProcess`（改造）+ `TraceTimeline`（改造自 `AgentTraceTree`）+ `ProcessRail`。
5. **知识与技能**（1.5d）：`KnowledgeView`（改造 `knowledge/base.vue`）+ 上传 + `SkillPanel`。
6. **设置与打磨**（1d）：`SettingsView`、错误态/空态、暗色校验、移动端窄屏（<768px 时右栏变抽屉）。

---

## 附：三条最容易踩的坑（来自源码实证）

1. **`DebugDrawer.vue:38` 的 `panes.value = { ...panes.value }`** —— 为了让模板更新而整对象替换，等价于强制全量 diff。新代码里消息用 `reactive` 数组 + 只改元素字段，靠 Vue 3 的 Proxy 追踪即可。
2. **`ChatBubbleList.vue:26-33` 的 computed 对全列表跑 `marked.parse`** —— 静态回放没问题，接上流式就是性能悬崖。务必先改这里再上流式。
3. **`system.ts:93` 的 `/^data: (.*)$/m`** —— 只取第一行 `data:`，多行 data 会被静默截断；`event:` 与 `done` 缺失时抛 `stream ended without result` 的兜底是对的，要保留。