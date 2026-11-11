# 通用聊天工作台 · 前端实现说明

> 本文是 `web/` 的维护手册：怎么跑起来、怎么接自己的后端、怎么改主题和组件、踩过哪些坑。
> 快速上手看 [README](../README.md)；后端契约看 [kernel-contract.md](../../docs/design/kernel-contract.md)。

---

## 1. 启动与构建

```bash
cd web
npm install

npm run dev        # 开发，http://127.0.0.1:5173 ，/api 代理到 127.0.0.1:8080
npm run type-check # 只跑 vue-tsc（严格模式，noUnusedLocals/Parameters 都开着）
npm run build      # vue-tsc --noEmit && vite build → dist/
npm run preview    # 预览 dist（不会代理 /api，联调请用 dev 或 nginx）
```

后端：

```powershell
$env:PYTHONPATH="E:\Project\TsukiyoHub\server"
& "E:\Project\HQDingTalkrobot\.venv\Scripts\python.exe" -m uvicorn tsukiyohub.app:app --port 8080
```

> `GET /healthz` 返回 `{"status":"ok",...}` 就说明前端能连上。前端顶栏的圆点会变绿，
> 并且每 30 秒探活一次（标签页不可见时跳过）。

端口不是 8080 时不要改代码：

```bash
# web/.env.local
VITE_API_PROXY=http://127.0.0.1:8099
```

---

## 2. 接自己的后端

前端只依赖 HTTP + SSE，不依赖任何框架细节。三种接法：

### 2.1 同域反代（推荐）

把 `dist/` 交给 nginx，把 `/api`、`/healthz`、`/readyz` 反代到后端：

```nginx
location /api/ {
  proxy_pass http://127.0.0.1:8080;
  proxy_http_version 1.1;
  proxy_set_header Connection '';
  proxy_buffering off;        # SSE 必须关缓冲，否则会「一次性吐出」
  proxy_read_timeout 3600s;
}
location = /healthz { proxy_pass http://127.0.0.1:8080; }
location = /readyz  { proxy_pass http://127.0.0.1:8080; }
```

前端代码里请求的是相对路径（`/api/...`），所以不需要构建期注入后端地址。

### 2.2 跨域直连

后端已带 CORS（`CORS_ORIGINS`）。若要把前端放到别的域名：

1. `src/api/client.ts` 里 `API_BASE` 改成完整地址（如 `https://api.example.com/api`）；
2. `src/api/system.ts` 的探活路径同步改（`/healthz` → 完整地址）；
3. `src/api/chat.ts` 的 `fetch` 也走 `API_BASE`，无需改。

### 2.3 自定义前端框架

只要满足下面 3 条，任何前端都能接：

- `POST /api/chat/stream`：body 是 `ChatRequest`，响应 `text/event-stream`；
- 解析事件：`status` / `progress` / `delta` / `reset` / `tool` / `meta` / `error` / `done`；
- `delta.data` 是 **JSON 字符串（当前累积全文）**，整段替换即可；`reset` 表示换轮清空。

---

## 3. 目录与数据流

```
ChatView
  ├─ SessionList ── store.loadSessions() ──► GET /api/sessions
  │                  store.createSession() ─► POST /api/sessions
  │                  store.renameSession() ─► PATCH /api/sessions/{id}
  │                  store.removeSession() ─► DELETE /api/sessions/{id}
  ├─ MessageList ── store.loadMessages(id) ─► GET /api/sessions/{id}/messages
  ├─ Composer    ── store.send(text) ──────► POST /api/chat/stream (SSE)
  └─ SourcePanel ── store.openCitationPanel(messageId, citations)
```

关键约定：

| 层 | 职责 | 不许做的事 |
| --- | --- | --- |
| `api/*` | 拼请求、归一化错误、SSE 解析 | 不碰 Vue 响应式、不弹组件级 UI |
| `stores/*` | 会话/消息/流式状态机 | 不发请求（除自己 action 里调 api） |
| `utils/*` | 纯函数（markdown、sse、format） | 不依赖 store |
| `components/*` | 渲染 + 交互事件 | 不直接调 axios |

---

## 4. 流式实现要点（最容易踩的地方）

1. **AbortController 放模块作用域**（`stores/chat.ts` 的 `runtime`），不要放进 `state`：
   被 Proxy 包住的 controller 行为不可预期。
2. **`delta` 两种语义都要吃下**：契约是「累积全文」，但历史实现与 OpenAI 兼容接口推增量。
   `api/chat.ts` 用「新文本是否以已有全文为前缀且更长」区分，对外统一成累积全文。
   `reset` 一定要清空缓冲，否则换轮后会出现两段文字粘在一起。
3. **50ms 节流**：后端 40ms 一批，如果每帧都写响应式会触发过多渲染；
   `stores/chat.ts` 用 `setTimeout(50)` 合并，流结束时再兜底 flush 一次。
4. **半截 Markdown 不要直接 `marked.parse`**：`utils/markdown.ts` 的 `renderStreaming()`
   只渲染「最后一个空行之前」的稳定前缀，尾部按纯文本显示；未闭合的代码块整体降级成 `<pre>`。
5. **必须保留 DOMPurify**：`v-html` 注入模型输出，不过滤就是 XSS。
6. **不要整数组替换消息**：只改「正在流式那条」的 `content` 字段，列表用 `v-memo`。
   旧前端 `panes.value = {...panes.value}` 的写法会触发整列 diff，流式下直接卡死。
7. **空闲看门狗**：后端 15s 一次 `: ping`，60s 没有字节就判定连接已死并取消 reader，
   否则网关掐流时 `reader.read()` 会永远挂着。

SSE 帧样例（`data` 是 JSON 字符串，注意引号）：

```
event: status
data: thinking

event: delta
data: "知识库检索使用的是混合召回"

event: reset
data: 

event: meta
data: {"session_id":"s1","message_id":"m1","citations":[...],"grounded":true}

event: done
data: [DONE]
```

---

## 5. 定制主题

三层，从粗到细：

1. **色板**：`src/styles/tokens.less`。亮/暗两份 CSS 变量，组件里只允许用 `var(--xxx)`。
   换品牌色只需改 `--color-primary*` 与 `--color-bubble-self`。
2. **AntDV token**：`src/composables/useTheme.ts` 的 `LIGHT` / `DARK` / `COMMON`。
   这里改的是 Ant Design Vue 组件内部颜色与圆角，要和 tokens 保持一致。
3. **布局尺寸**：`--header-height`、`--session-col`（会话列宽）、`--source-col`（来源面板宽），
   响应式断点在各自组件的 `@media` 里（1280 三栏 → 900 会话列浮层）。

切换方式：`stores/app.ts` 的 `themeMode`（`light | dark | system`），持久化到
`localStorage['tsukiyohub-app']`；`index.html` 首帧脚本读同一个 key，避免深色模式闪白。

---

## 6. 定制组件 / 加页面

- **换气泡样式**：`components/chat/ChatBubble.vue` 的 `.bubble` 与 `.row.user .bubble`。
  结构是 `avatar + col(bubble, refs, actions)`，复制/重新生成按钮在 `actions` 里。
- **来源展示换成抽屉**：`ChatView.vue` 里把 `<SourcePanel>` 换成 `<a-drawer>` 即可，
  数据仍在 `store.panelCitations` / `panelOpen`。
- **加一个页面**：`views/XxxView.vue` + `router/index.ts` 加一条路由 + `AppShell.vue` 的 `NAV` 加一项。
  长页面直接复用全局样式 `.page / .page-inner / .page-head / .page-title`。
- **加 AntDV 组件**：`main.ts` 底部数组里补一行 `import { Xxx } from 'ant-design-vue'`，
  否则模板里的 `<a-xxx>` 会渲染成未知标签（Vue 只在控制台给 warning，页面看起来「没样式」）。
- **加接口**：`src/types/api.ts` 加类型 → 对应 `api/xxx.ts` 加函数（用 `http.get/post/...`，
  错误会自动 toast）→ 页面里调用。视频/图表等重依赖建议 `defineAsyncComponent` 懒加载。

---

## 7. 错误与状态约定

| 场景 | 表现 |
| --- | --- |
| 后端未启动 | 顶栏「后端未连接」+ 红色横幅（含启动命令）+ 各页面重试按钮 |
| 401/403 | `message.warning` 提示「API Key 无效或缺失」，**不跳登录页**（本前端无账号体系） |
| 422 | 显示后端 `detail`（把 pydantic 校验数组拼成中文） |
| 流式中途失败 | 保留已收到正文，气泡下方显示原因 + 「重新生成」 |
| 用户点停止 | 消息标记 `已停止生成`，保留正文，不算错误、不弹 toast |
| 列表为空 | 各页面自己的空状态（聊天页是 3 个示例问题） |

---

## 8. 已知限制 / 后续可做

- **长会话未上虚拟列表**：>300 条消息时滚动会变重。接 `VirtualList.vue`（旧项目有 133 行实现）
  需要注意「自动贴底」与虚拟滚动的冲突，建议先做分页加载。
- **引用点击没有正文定位**：现在点击引用只打开右侧面板；可加「回到正文 `[n]` 高亮」。
- **「重新生成」会在后端留下两条回答**：前端会删掉本地的旧回答再重发，
  但后端没有删除消息的接口，所以刷新后历史里会看到重复的一轮。彻底修需要后端支持
  「重生成指定 message_id」。
- **没有消息级反馈（赞/踩）**：后端有 `POST /api/feedback`，需要产品定义展示位置再补。
- **附件上传未接入聊天**：知识库上传已可用，但「把某文档作为本轮上下文」还没做（需要后端支持 `attachments`）。
- **i18n**：当前只有中文，文案散落在组件里；要国际化建议先抽 `locales/zh-CN.ts` 再逐步替换。
- **打包体积**：AntDV 仍是最大头（~220KB gzip）。若极致优化，可把 `a-table`、`a-select`
  换成自绘（这两个贡献最多），或用 `unplugin-vue-components` 做编译期按需引入。

---

## 9. 自检记录（实现时实测）

| 项 | 结果 |
| --- | --- |
| `vue-tsc --noEmit` | 通过（strict + noUnusedLocals + noUnusedParameters） |
| `vite build` | 通过；index.html 1.8KB，antd chunk 736KB(220KB gz)，业务代码 ~300KB |
| SSE 解析器单测 | 17 项全过（真实后端帧、跨 chunk 分片、多行 data、CRLF、心跳、残帧、emoji） |
| 真实后端联调 | `/healthz`、`/api/system/config`、`/api/system/persona`、`/api/sessions*`、`/api/skills`、`/api/knowledge/{stats,documents,upload,search}`、`/api/chat/stream` 全部按契约返回 |
| 前端代理 | dev server 下 `/healthz`、`/api/*` 均正确转发，中文 UTF-8 无乱码 |