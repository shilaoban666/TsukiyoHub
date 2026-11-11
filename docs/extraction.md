# 抽取对照：从 HQDingTalkrobot / HQDingtalkRobotUI 到 TsukiyoHub

这份文档记录「哪段代码从哪来、改了什么、为什么删掉」。目的是让后来接手的人知道：
不是重写了一套，而是把原有能力**去业务化**后搬过来了；也知道哪些东西是故意不要的。

## 一句话结论

| 维度 | 原项目 | TsukiyoHub |
|---|---|---|
| 定位 | 公司内部钉钉知识库机器人（含管理后台） | 与业务无关的聊天 / 技能 / RAG 内核服务 |
| 入口 | 钉钉 Stream / 千问办公 / 管理后台 | HTTP + SSE + OpenAI 兼容（任何前端） |
| 业务耦合 | 钉钉、组织架构、办公流程、部门洞察 | 无（全部丢弃，改为接入方自己实现） |
| 规模 | 后端 ~35k 行 / 前端 ~30k 行 | 内核 + API + 参考前端，去掉业务后约 1/3 |
| 数据 | PostgreSQL + Milvus + Redis + OSS | 相同技术栈，但**默认零依赖可跑**（SQLite + 内存检索） |

## 后端：模块级对照

| 原路径（`src/fancyqube_robot/`） | 去向（`server/tsukiyohub/`） | 处理方式 |
|---|---|---|
| `domain.py` | `domain.py` | 保留结构；删掉钉钉渠道、员工/部门字段、OA 意图枚举 |
| `config.py` | `config.py` | 重写：删钉钉/千问/OSS/MNS/组织配置，改为通用 LLM/Embedding/Vector/DB 配置 |
| `security.py`（密钥箱） | ✂️ | 删除：内核不做多租户密钥托管，密钥走环境变量 |
| `logging_setup.py` | `logging_setup.py` | 保留 trace_id 上下文与启动静音；去掉企业字段 |
| `banner.py` | `banner.py` | 重写：结论式启动横幅（不写日志文件） |
| `main.py` | `app.py` | 重写：只装配 HTTP；去掉钉钉 Stream / 后台任务 / 自动导入大段逻辑 |
| `service/container.py` | `container.py` | 重写：依赖装配改为「端口 + 适配器」，去掉组织/办公/文件发送装配 |
| `service/agent.py` | `kernel/chat/agent.py` | **核心移植**：模型工具循环、流式、主备降级、prefetch、引用挑选、逐句核对；删通讯录/部门简称/OA 动作/附件/FAQ/语义缓存 |
| `service/conversation.py` | `kernel/chat/conversation.py` | 移植会话编排、历史、落库；删群聊、@、卡片、评价推送、员工上下文拉取 |
| `service/citations.py` | `kernel/chat/citations.py` | 移植 `renumber / tidy_refs / merge_pages / related`；去掉钉盘链接签名 |
| `service/safety.py` | `kernel/chat/safety.py` | 精简为「脱敏占位符 + 禁止话题」 |
| `service/config_store.py` | `ports/memory.KVStore` + `adapters/store_sql.py` | 简化：运行期配置只保留人设/风格/检索参数/技能开关 |
| `agentic/*`（LangGraph 图） | `kernel/chat/agentic.py` | 保留「感知 → 路由 → direct/rag → 核对」的思想，重写为纯 Python 轻量管线，去掉目录/办公节点与灰度/影子 |
| `llm/router.py` | `adapters/llm_openai.py` | 重写：不再依赖数据库里的模型表，改为环境变量配置主/备模型；保留 `enable_thinking` 开关 |
| `llm/fake.py` | `adapters/llm_openai.FakeLLMClient` | 简化：能演示「工具调用 + 流式」的演示模型 |
| `harness/base.py` + `registry.py` | `kernel/skills/base.py` + `registry.py` | 移植协议与注册表；补 `all()` / `set_enabled()` / `describe()` |
| `harness/knowledge.py` | `kernel/skills/builtin/knowledge.py` | 移植 `knowledge_search`；去掉可见范围（kb_ids）改为 `SearchFilters` |
| `harness/web.py` | `kernel/skills/builtin/web.py` | 移植 `web_fetch` / `web_search`；去掉钉盘文档链接读取 |
| `harness/skills/__init__.py` | `kernel/skills/loader.py` | **补齐**：原实现只发现不启用；新实现支持 catalog 注入 + `use_skill` 工具 |
| `harness/mcp/__init__.py` | `kernel/skills/mcp.py` | **补齐**：原实现是骨架；新实现支持 stdio/sse server 与工具包装 |
| `harness/{directory,office,kb_files,send_file,doc_link}.py` | ✂️ | 删除：通讯录 / 打卡 / 钉盘文件 / 发文件 / 钉钉文档链接，全是业务 |
| `rag/*` | `kernel/rag/*` | 移植 tokenize / fusion / embeddings / embed_cache / rerank / grounding / ground_check / multi_query / semantic_cache / prompt / answer_style；删 kb_ids 可见范围 |
| `kb/store.py` | `kernel/rag/store.py` | 移植清单 / 增量 / 幂等 / 落盘；去掉「同步来源」与 Milvus 专属字段 |
| `kb/{links,actions,faq,lexicon,terms,release,summary,visibility}.py` | ✂️ | 删除：钉盘签名链接、操作目录、FAQ 直答、公司词典、可见范围 |
| `ingest/{parser,chunker,ocr,vision,sources}` | `kernel/rag/parser.py` + `chunker.py` | 移植解析与切块；OCR/图片理解属可选增强，先不移植（文档说明扩展点） |
| `kbsync/*`（22 文件） | ✂️ | 删除：钉盘/群文件/公告同步、OSS/MNS 事件、任务队列；通用版用「上传 + 目录导入 + 重建」代替 |
| `memory/*` | `kernel/memory/*` | 精简移植：事实抽取/召回/禁记/摘要；删热温冷三层、员工画像、离职钩子、治理与审计 |
| `admin/*`（21 文件） | `api/routes_system.py` 等 | 重写为少量通用接口；删权限/审计/组织管理后台 |
| `insight/*` | ✂️ | 删除：领导驾驶舱、部门洞察，纯业务 |
| `evals/*` | `tests/` + `scripts/smoke.py` | 不移植 RAGAS 评测集（与原语料绑定）；保留可跑的冒烟与单测 |
| `adapters/dingtalk/*`、`adapters/qwenwork/*` | ✂️ | 删除：渠道适配属于接入方；内核只暴露 HTTP |

## 前端：资产级对照

| 原路径（`src/`） | 去向（`web/src/`） | 处理方式 |
|---|---|---|
| `utils/request.ts` | `api/client.ts` | 重写：去掉 mock、401 刷新、antd message 耦合；改为纯 axios + 错误归一化 |
| `utils/markdown.ts` | `utils/markdown.ts` | 移植并加缓存（避免每次渲染都 `marked.parse`） |
| `utils/format.ts`、`utils/storage.ts` | 同名 | 移植精简 |
| `composables/useTheme.ts` | 同名 | 移植：亮/暗两套 token |
| `styles/tokens.less`、`transitions.less` | `styles/*` | 移植：保留气泡/间距变量 |
| `components/ChatBubble.vue`、`ChatBubbleList.vue` | `components/chat/ChatBubble.vue` 等 | 改造：流式「稳定前缀」渲染，避免整列表 O(n²) 重解析 |
| `components/RetrievalProcess.vue` | `components/chat/RetrievalProcess` 思路并入状态条 | 改造：用新 SSE 的 `status/progress/tool` 事件 |
| `src/api/system.ts::debugStream()` | `utils/sse.ts` + `api/chat.ts` | **重写**：原实现 `/^data: (.*)$/m` 只取第一行 data，多行会截断；新实现按 SSE 规范拼接 |
| `views/robot/DebugDrawer.vue` | `views/...`（会话页） | 重写：拆成会话列表 / 消息区 / 引用面板 |
| `views/knowledge/base.vue` | `views/KnowledgeView.vue` | 改造：上传 + 列表 + 删除 + 重建 |
| `views/harness/capabilities.vue` | `views/SkillsView.vue` | 改造：技能列表 + 启停 |
| `layouts/*`、`router/routes.ts`（权限菜单）、`guards.ts` | `components/layout/AppShell.vue` + `router/index.ts` | 重写：三栏布局，无权限体系 |
| `stores/*`（user/org/app） | `stores/chat.ts`、`stores/skills.ts`、`stores/app.ts` | 重写：没有登录态与组织树 |
| `api/{robot,analysis,insight,eval,org,routing,vector,ops}.ts` | ✂️ | 删除：对应后台业务页面已删 |
| `echarts` / `vue-echarts` / `jsencrypt` / `playwright` | ✂️ | 删除：聊天工作台不需要图表、加密登录、E2E 框架 |
| `mock/*`（27 文件） | ✂️ | 删除：假数据会掩盖后端问题，改为「后端未连接」提示 |

## 有意保留但升级了的部分

| 能力 | 原实现 | 新实现 |
|---|---|---|
| 引用编号 | 段编号 → 文档编号，规则散在 agent + citations | 收敛到 `citations.renumber`，有单测 |
| 防幻觉 | 检索闸门 + 规则核对 + 逐句核对 | 保留三道关，阈值配置化（换模型要重标定） |
| 流式 | 卡片/SSE 各自实现 | 统一 `delta=累积全文` + `reset`，三种入口共用 |
| 技能 | 只有发现，没有启用链路 | 完整：注册 / 启停 / REST / `use_skill` / MCP |
| 存储 | 必须 PostgreSQL + Milvus | 默认 SQLite + 内存；生产可切回原栈 |
| 检索调试 | 仅内部评测 | 开放 `POST /api/knowledge/search`，返回分数明细 |

## 抽取时踩过的坑（留给后来人）

1. **`delta` 的语义**：原项目有的地方推增量、有的地方推全文，前端各自拼接。
   新实现统一为「当前累积全文」，前端整段替换，丢帧也不会错位。
2. **SSE 多行 data**：旧前端用 `/^data: (.*)$/m` 只取第一行，多行 JSON 会被静默截断。
   新前端按规范把多行 `data:` 用 `\n` 拼接。
3. **向量分数量纲**：`grounding_min_score` 是在特定 embedding 模型上标定的。
   换模型必须重新标定，否则会出现「该拒答的没拒」或「该答的拒了」。
4. **伪向量污染**：原实现在向量化失败时仍写伪向量，导致文档看起来「已入库」却检索不到。
   新实现：索引期失败返回错误让调用方重试，只有在明确降级模式才写伪向量。
5. **知识库编号**：`ctx.hits` 是全局编号总线（prefetch / 工具调用 / 核对共用），
   编号不得重排；唯一收口点是 `_finish` 与 `citations.renumber`。
6. **进程内单例**：原项目的熔断器/并发池是进程内状态，多副本不共享。
   新实现把并发限制放在 `ConversationService` 的 Semaphore（单副本语义），文档里写明多副本需外部限流。