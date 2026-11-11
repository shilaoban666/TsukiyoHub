# TsukiyoHub 通用聊天工作台（Web 参考前端）

一个与业务无关的「聊天 + 技能 + 知识库」前端工作台，对接 [TsukiyoHub](../server) 内核服务的 HTTP + SSE 接口。
任何团队都可以拿它当起点：换掉品牌、接上自己的后端地址、按需增删页面即可。

- 技术栈：Vue 3 (`<script setup lang="ts">`) + Pinia + Vue Router + Ant Design Vue 4 + marked + DOMPurify + axios
- 无 mock 数据：所有数据都来自真实接口；后端没启动时页面会明确提示「后端未连接」并给重试入口
- 深色模式、窄屏折叠、流式逐字输出、停止生成、引用来源面板

## 快速开始

```bash
cd web
npm install

# 开发（默认把 /api 代理到 http://127.0.0.1:8080）
npm run dev            # http://127.0.0.1:5173

# 类型检查 + 生产构建（产物在 dist/）
npm run build

# 预览构建产物
npm run preview
```

### 后端怎么起

```powershell
$env:PYTHONPATH="E:\Project\TsukiyoHub\server"
& "E:\Project\HQDingtalkRobot\.venv\Scripts\python.exe" -m uvicorn tsukiyohub.app:app --port 8080
```

冒烟：

```bash
curl http://127.0.0.1:8080/healthz
```

后端默认地址可以通过环境变量覆盖（`vite.config.ts` 里读的是 `VITE_API_PROXY`）：

```bash
# .env.local
VITE_API_PROXY=http://127.0.0.1:8099
```

生产环境不需要改代码：把 `dist/` 交给 nginx，并把 `/api`、`/healthz`、`/readyz` 反代到内核服务即可
（SSE 要关掉缓冲：`proxy_buffering off;` 或后端已返回 `X-Accel-Buffering: no`）。

## 页面与功能

| 路由 | 页面 | 能力 |
| --- | --- | --- |
| `/chat`、`/chat/:sessionId` | 聊天（核心） | 会话列表（新建/重命名/删除/按时间排序）、Markdown 气泡、流式逐字、停止生成、重新生成、复制、引用来源折叠、右侧来源面板、状态条、空状态示例问题 |
| `/knowledge` | 知识库 | 统计卡（文档/切块/向量/向量后端）、文档列表、拖拽上传（带进度）、删除、重建索引、检索测试（top-k + 分数） |
| `/skills` | 技能 | 按类型分组、启用/停用开关（乐观更新 + 失败回滚）、展开看参数 JSON Schema 与使用规则、调用次数 |
| `/settings` | 设置 | 人设/语气/长度/Markdown/兜底话术/top_k/记忆开关（只提交改动字段）、模型与向量后端只读展示、主题、API Key、连通性测试 |

## 目录结构

```
web/
├─ index.html            首帧主题脚本（防深色闪白）
├─ vite.config.ts        /api、/healthz、/readyz 代理到后端
├─ src/
│  ├─ main.ts            Pinia + Router + 按需注册的 AntDV 组件
│  ├─ App.vue            ConfigProvider（主题/中文语言）+ 外壳
│  ├─ api/               client(axios+错误归一化) / chat(SSE) / sessions / knowledge / skills / system
│  ├─ components/
│  │  ├─ layout/AppShell.vue        顶栏 + 导航 + 后端状态 + 离线横幅
│  │  ├─ chat/SessionList.vue       会话列表
│  │  ├─ chat/MessageList.vue       自动贴底 / 上滑暂停
│  │  ├─ chat/ChatBubble.vue        Markdown / 复制 / 重新生成 / 引用折叠
│  │  ├─ chat/CitationList.vue      引用卡片
│  │  ├─ chat/SourcePanel.vue       右侧来源面板
│  │  ├─ chat/Composer.vue          输入框（Enter 发送 / Shift+Enter 换行）
│  │  └─ chat/StreamStatus.vue      思考中 / 检索中 / 过程文案
│  ├─ composables/useTheme.ts       浅色/深色/跟随系统（CSS 变量 + AntDV token）
│  ├─ router/index.ts   4 条扁平路由（无权限系统）
│  ├─ stores/           app(主题/Key/连通性) · chat(会话+消息+流式) · skills
│  ├─ styles/           tokens.less（设计变量）· global.less · transitions.less
│  ├─ types/api.ts      与后端 Pydantic 对齐的类型（唯一真源）
│  └─ utils/            sse / markdown / format / storage / cancel
└─ docs/web.md          更详细的设计与定制说明
```

## 关键设计

- **SSE 用 fetch 而不是 EventSource**：需要 POST body、`Authorization` 头、`AbortController` 中止。
  `utils/sse.ts` 按规范解析（多行 `data:` 拼 `\n`、忽略 `: ping` 心跳、残帧丢弃），
  `api/chat.ts` 把 `delta` 归一化成「累积全文」后再交给上层，兼容增量与累积两种后端实现。
- **流式渲染只打一个节点**：只有正在生成的那条消息会变；`delta` 到达后按 50ms 节流写入，
  Markdown 渲染用「稳定前缀 + 尾部纯文本」避免半截 ``` 造成整段闪烁。
- **列表不全量重算 Markdown**：已完成消息用 `computed` 缓存 + `v-memo`，
  只有 `content.length` 变化的那条才重新渲染。
- **按需注册 AntDV 组件**：全量 `app.use(Antd)` 会打进来 ~1.45MB JS；
  现在只注册用到的 13 个（见 `main.ts`），产物减少约 50%。新增组件记得在 `main.ts` 补一行。

## 构建产物（参考值）

| 文件 | 体积 | gzip |
| --- | --- | --- |
| antd chunk | ~736 KB | ~220 KB |
| 业务 + 组件 | ~300 KB | ~110 KB |
| CSS 合计 | ~36 KB | ~10 KB |

## 常见问题

**页面显示「后端未连接」？** 后端没起、端口不是 8080、或 API Key 不对。启动命令见上，
端口不同时设置 `VITE_API_PROXY` 后重启 `npm run dev`。

**回答不逐字出现，一次性冒出？** 中间有代理缓冲。nginx 需 `proxy_buffering off;`，
后端已返回 `X-Accel-Buffering: no`；也可看后端是否真的分片 `yield`。

**改了配置没生效？** 模型/向量后端属于部署期配置，页面只读；可写字段是 9 个
（`bot_name / persona_prompt / default_tone / default_length / default_markdown / fallback_text /
retrieval_top_k / skills_enabled / memory_enabled`）。

**上传后检索不到？** 后端 `grounding_min_score` 默认 0.35，哈希向量在小语料上可能低于阈值，
表现为「未命中知识库」。可换真实 embedding（DashScope/OpenAI）或在知识库页用「检索测试」看分数。