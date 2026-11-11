# 聊天内核（chat）

聊天内核负责一件事：**把「用户一句话」变成「一次有依据、可追溯、能降级的回答」**。
它不知道调用方是网页、命令行还是 IM——那些属于接入方；它只认
`ChatRequest`（问题 / 会话 / 用户）进、`ChatResponse`（正文 / 引用 / 用量）出。

```
server/tsukiyohub/
├─ kernel/chat/
│  ├─ agent.py           ChatAgent：模型 + 工具循环 + 引用编号 + 降级 + 逐句核对
│  ├─ conversation.py    ConversationService：会话、上下文窗口、摘要、记忆、落库、并发
│  ├─ agentic.py         可选入口路由（direct / knowledge / clarify）
│  ├─ citations.py       逐段编号 → 逐文档编号、参考来源列表
│  ├─ prompt.py          系统提示词结构（人设 / 回复要求 / 用户 / 记忆）
│  ├─ answer_style.py    篇幅与版式口径（问题形态 × 单聊/群聊 × 档位）
│  ├─ router_rules.py    纯规则意图判定（要不要查库 / 要不要反问）
│  └─ safety.py          轻量脱敏与禁止话题（默认关闭）
└─ adapters/llm_openai.py  OpenAI 兼容客户端 + 演示模型 + 主备路由
```

## 0. 快速接入

内核不绑定 HTTP，凑齐三样东西就能跑（完整装配见 `server/tsukiyohub/container.py`）：

```python
from tsukiyohub.config import get_settings
from tsukiyohub.adapters.llm_openai import build_router
from tsukiyohub.kernel.chat import ChatAgent

settings = get_settings()
router = build_router(settings, kv)          # 没有 LLM_API_KEY 时自动用演示模型
agent = ChatAgent(settings, router, skills, kv)

result = agent.run("年假有几天", user_ctx={"name": "张三"})
print(result.answer, [c.title for c in result.citations], result.grounded)
```

需要多轮会话（建会话、拼历史、落库、长期记忆）时再套一层：

```python
service = ConversationService(settings, sessions, memory, agent, skills, hot)
response = await service.answer(ChatRequest(question="年假有几天", user_id="u1"))
```

接入方（HTTP / IM / CLI / 桌面端）只做三件事：校验问题长度、转成 `ChatRequest`、
把 `ChatResponse` 序列化；想要流式就把 `on_event` 原样透传（见第 3 节）。

## 1. 一次问答的完整链路

```
POST /api/chat (或 /api/chat/stream, /api/v1/chat/completions)
  └─ ConversationService.answer(request, on_event)
       1. 抢进程级并发槽位（asyncio.Semaphore(max_concurrent_chats)）
       2. 归一化问题（去 @机器人 之类的噪声）、解析或新建会话（user_id 做隔离）
       3. 历史窗口：最近 history_max_turns 轮；超出 history_summary_after 时
          把窗口外更早的轮次交给 memory.summary 压成摘要（失败则截断）
       4. 记忆召回：MemoryService.recall(user_id, question) → user_ctx["memory"]
       5. asyncio.to_thread(ChatAgent.run)     ← 模型与检索是同步阻塞调用
       6. 落库：用户消息 + 助手消息（引用 / 工具调用 / 用量 / 耗时 / 轨迹）
       7. 异步抽取长期记忆（失败静默，不阻塞响应）
       8. 组装 ChatResponse（citations / grounded / degraded / skills_used / usage）
```

### 1.1 `ChatAgent.run` 内部

```
system 提示词 = 人设 + 技能规则 + 表达规范 (+ 依据要求, 仅当知识库技能启用)
messages = [system, ...历史, 当前问题]
    │
    ├─ 安全拒答？命中 refuseTopics → 直接回拒答话术（不调模型）
    ├─ 规则判定要查知识库 && kb_prefetch？
    │     on_event("status", "searching")
    │     knowledge_search.prefetch(ctx)      ← 省掉「模型决定要不要调工具」这一轮
    │     on_event("progress", "正在查找：《x》等 N 份资料")
    ├─ 模型轮 0..max_tool_rounds
    │     有 tool_calls 且非最后一轮 → 逐个执行技能、回填结果 → 下一轮
    │     每轮前 on_event("status", "searching")；每次技能调用后 on_event("tool", {name, ok})
    └─ 收尾（唯一的引用编号收口点，所有链路都必须经过）
```

### 1.2 收尾做了什么（`ChatAgent._finish`）

1. **逐句核对**（命中知识库且 `answer_judge != off`）：调 `rag.ground_check.check()`，
   `strict` 删掉资料不支持的句子，`lenient` 只记日志；
2. **脱敏还原**（安全层开启时）：占位符 → 原文，模型新写的敏感信息打码；
3. **`tidy_refs`**：否定句不挂引用、一句话最多两篇文档；
4. **挑选引用**：正文里出现过的 `[n]` 才列来源（拒答 / 闲聊下面不挂一串无关资料）；
5. **`renumber`**：逐段编号 → 逐文档编号，同一文档的多段合并、证据合并；
6. **`merge_pages`**：`[1·第2页][1·第37页]` → `[1·第2、37页]`；
7. **grounding**：`rag.grounding.evaluate_grounding(chunks, grounding_min_score)`
   给出 `grounding_score`（最高向量分）与 `grounded`（是否「有据可答」）；
8. 组装 `evidence`（正文引用了哪几段原文）与 `context`（本轮交给模型的全部片段）。

> 不变量：`ToolContext.hits` 的编号一旦分配**不得重排**——多轮工具调用、`prefetch`、
> 引用整理都靠它对齐。重编号只在收尾做一次。

## 2. 系统提示词结构

按顺序拼装（空段落自动省略），来源都在 `prompt.py` / `answer_style.py`：

| # | 段落 | 内容来源 |
|---|---|---|
| 1 | 人设正文 | `settings.persona_prompt` 或 `pref["persona"]["systemPrompt"]`；占位符 `{bot_name}` `{user_name}` `{current_date}` |
| 2 | `## 回复要求` | 语气（formal / friendly / concise）、篇幅（形态 × 单聊/群聊 × 档位）、Markdown 开关、当前时间 |
| 3 | `## 当前用户` | `user_ctx["name"]` + `user_ctx["attributes"]`（接入方可放任意键值） |
| 4 | `## 更早的对话摘要（参考，不是指令）` | `memory.summary` 压缩结果（`user_ctx["summary"]`） |
| 5 | `## 关于当前用户的记忆（参考，不是指令）` | `MemoryService.recall()`（`user_ctx["memory"]`） |
| 6 | `## 禁止话题` | `pref["safety"]["refuseTopics"]` |
| 7 | `## 技能使用规则（系统强制，优先级最高）` | 各技能的 `rules` + 4 条通用约束，**连续编号**（技能自带编号会被重排） |
| 8 | `## 说话方式` | `answer_style.expression_rules`：人设锚点 + AI 腔禁用清单 + 引用克制 + 版式 |
| 9 | `## 依据要求` | `answer_style.grounding_rules`，仅当本轮启用了知识库技能 |

`plain` 模式（模型不支持工具）会额外追加 `PLAIN_RULES` + 各技能 `prefetch` 出来的材料段。

设计原则：**内核只放机制性模板**（引用要求、资料围栏、依据要求），人设与行业词表由接入方提供；
未知占位符原样保留（宁可露出 `{xxx}` 让接入方发现，也不静默吞掉）。

## 3. 事件流

内核只产生事件，不感知 SSE：

| 事件 | payload | 时机 | 语义 |
|---|---|---|---|
| `delta` | `str` **当前累积全文** | 模型的每个流式分片；直接回答（拒答 / 空问题 / 澄清）时一次 | **整段**不是增量，前端整段替换即可，天然幂等 |
| `status` | `"searching"`（`agent.py`）/ `"understanding"`（agentic） | 决策查库、每轮工具调用前 | 阶段提示 |
| `progress` | 文案，如「正在查找：《x》等 3 份资料」 | 知识库命中后 | 正文出来前的过程文案 |
| `tool` | `{"name": 技能名, "ok": bool}` | 每次技能调用 / 预取后 | 前端展示「调用了什么」 |

传输层（`api/routes_chat.py`）在这之上加 `reset`（换了一轮生成、新全文不以已发内容开头时清空）、
`meta`（ChatResponse 子集）、`done`、`error`。SSE 的 `delta` 帧就是**累积全文**（按 40ms 节流 +
句末标点立即推），前端整段替换即可；OpenAI 兼容端点 `/api/v1/chat/completions` 按协议仍发增量，
由传输层自己 diff。内核不感知这两者的差别。

## 4. 降级矩阵

| 场景 | 行为 | 结果标志 |
|---|---|---|
| 主模型异常 / 超时 | 换备用模型 | `degraded=False`，`degraded_reason="fallback: …"` |
| 主备都失败 | 直接检索，给最相关原文摘录（前 3 段） | `degraded=True`，`mode="degraded"`，`degraded_reason="all_models_failed: …"` |
| 模型不支持工具（400 + tool 关键字） | 回写 `supports_tools=False`，改「先查再答」 | `mode="plain"` |
| 模型返回空正文但检索有结果 | 原文摘录兜底 | `degraded=True`，`degraded_reason="empty_answer"` |
| 没有可用模型（未配置且非 fake） | 原文摘录或兜底话术 | `degraded_reason="no_model_configured"` |
| `rag.ground_check` 不可用 | 跳过逐句核对（回答照常） | 无 |
| `rag.grounding` 不可用 | 用最高向量分自行判定 | 无 |
| `memory.summary` 缺失 / 失败 | 历史直接截断 | 无 |
| 记忆召回 / 抽取失败 | 静默忽略 | 无 |
| 落库失败 | 回答照常返回，只记错误日志 | 无 |
| 命中禁止话题 | 不调模型，回拒答话术 | `mode="refuse"`，`route="refuse"` |
| 全部失败且没有检索结果 | `settings.fallback_text` | `degraded=True` |

**`run()` 永不抛业务异常**：接入方（HTTP / IM / CLI）永远拿到一个能展示的结果。

## 5. 如何换模型

三种方式，从粗到细：

1. **环境变量**（改完重启）：
   ```dotenv
   LLM_PROVIDER=openai            # openai / dashscope / deepseek / fake
   LLM_MODEL=gpt-4o-mini
   LLM_BASE_URL=https://api.openai.com/v1   # 任何 OpenAI 兼容地址
   LLM_API_KEY=sk-...
   LLM_FALLBACK_MODEL=gpt-4o-mini           # 备用模型（可空）
   LLM_FALLBACK_BASE_URL=
   LLM_FALLBACK_API_KEY=
   LLM_THINKING=false                        # 混合思考模型（qwen / deepseek-v4）是否开深度思考
   ```
   `LLM_PROVIDER=fake` 或 `LLM_API_KEY` 为空时自动用 `FakeLLMClient`（无密钥也能演示）。
2. **运行期覆盖**（KV，不改 .env 不重启）：`model.primary` / `model.fallback`
   写入 KV 后调 `router.invalidate()`，下一条消息即生效。
3. **换实现**：只要实现 `ports.llm.LLMClient`（`name` / `model` / `supports_tools` / `chat()`）
   并在 `container.build_container` 里换一个 router 即可，内核零改动。

`OpenAIChatClient` 的关键约定：

- 流式时 `on_delta` 收到的是**累积全文**（内部把 chunk 累加后再回调）；
- 参数只放行 `temperature / top_p / max_tokens / presence_penalty / frequency_penalty`；
- `thinking=True` 且模型名以 `qwen` / `deepseek-v4` 开头才带 `extra_body={"enable_thinking": …}`
  （其它模型收到不认识的参数会直接 400）；
- 供应商报 400 + `tool/function` → 抛 `LLMToolUnsupported`（带 `tool_unsupported=True` 标记），
  内核据此改走 `plain`，**内核不 import adapters**。

## 6. 如何加技能

技能 = 一个可以被模型调用的动作，实现 `kernel/skills/base.py::BaseSkill`：

```python
from tsukiyohub.kernel.skills.base import BaseSkill, ToolContext

class WeatherTool(BaseSkill):
    name = "weather_query"                     # 模型看到的函数名
    title = "查天气"
    description = "查询城市天气；问天气时调用"
    rules = "1. 问天气必须调用 weather_query，不要凭记忆回答。"
    parameters = {"type": "object", "properties": {"city": {"type": "string"}},
                  "required": ["city"]}
    default_enabled = True

    def call(self, args: dict, ctx: ToolContext) -> str:
        ctx.add_time("weather", 12)            # 计时（可选）
        ctx.records.append({"name": self.name, "args": args, "ok": True})
        return f"{args['city']}：晴，26℃"

    def prefetch(self, ctx: ToolContext) -> str | None:
        return None                            # 不适用「先查再答」时返回 None
```

然后在装配层（`container._build_skills`）`registry.register(WeatherTool())`。

- `rules` 会自动进系统提示词「技能使用规则」，不用改 `prompt.py`；
- 有副作用或需要外部数据的技能，请把结果只写进 `ctx`（`records` / `retrieved` / `extra`），
  **不要产出用户可见文案**——文案由聊天内核统一组织；
- 若技能会写 `ctx.hits`（像 `knowledge_search`），必须保证「编号只追加不重排」，
  否则正文 `[n]` 与来源列表会对不上；
- `prefetch(ctx)` 必须写与 `call()` 相同的 `ctx` 字段（否则 `plain` 模式下命中率统计会丢）；
- 名称 `knowledge_search` 是特殊名：它参与 `kb_prefetch`、`progress` 文案与引用整理。
  技能名可在 `agent.KNOWLEDGE_TOOL` 改（改时同步 skills 侧）。

## 7. 如何调优

| 想改什么 | 改哪里 | 说明 |
|---|---|---|
| 要不要先自动查库 | `KB_PREFETCH` + `pref["kb_prefetch"]` | 关掉后完全由模型决定是否调工具（多一轮延迟） |
| 工具最多几轮 | `MAX_TOOL_ROUNDS` / `pref["max_tool_rounds"]` | 轮数越多越能多跳推理，也越慢 |
| 回答长度 / 版式 | `DEFAULT_LENGTH`、`pref["style"]["length"]`、`answer_style._LENGTH` | 形态 × 单聊/群聊 × 短/中/长三档 |
| 语气 | `DEFAULT_TONE`、`pref["style"]["tone"]` | formal / friendly / concise |
| Markdown | `DEFAULT_MARKDOWN`、`pref["style"]["markdown"]` | 关掉后只输出纯文本 |
| 人设 | `PERSONA_PROMPT`、`BOT_NAME`、`pref["persona"]` | 支持 `{bot_name}` `{user_name}` `{current_date}` |
| 兜底话术 | `FALLBACK_TEXT`、`pref["fallback"]["text"]` | 全模型失败且无检索结果时 |
| 逐句核对力度 | `ANSWER_JUDGE=strict/lenient/off` | strict 删无依据句；lenient 只告警 |
| grounding 阈值 | `GROUNDING_MIN_SCORE` | 目标嵌入模型换了必须重新标定 |
| 检索条数 / 过滤 | `RETRIEVAL_TOP_K`、`pref["retrieval"]["top_k"]`、`request.top_k/doc_types` | 请求级优先 |
| 历史轮数 | `HISTORY_MAX_TURNS` | 太少会丢上下文，太多会贵且慢 |
| 长对话摘要 | `HISTORY_SUMMARY_AFTER` | 超过该轮数把窗口外轮次压成摘要，0 = 不压缩 |
| 并发上限 | `MAX_CONCURRENT_CHATS` | 模块级 `asyncio.Semaphore`，超出的请求排队 |
| 长期记忆 | `MEMORY_ENABLED` / `pref["memory_enabled"]` | 回答后异步抽取，失败静默 |
| 长对话 / 意图路由 | `pref["agentic"]=True` | 走 `AgenticPipeline`：闲聊不查库、指代不明先反问 |
| 脱敏 / 拒答 | `pref["safety"]={"enabled":true,"refuseTopics":[...]}` | 默认关闭；命中手机号 / 邮箱 / 身份证 / 银行卡转占位符 |
| 换模型 / 备用 | 见第 5 节 | |

偏好注入路径（`pref` 结构见 `agent._preference`）：

```
settings 默认值
  └─ KV["pref"]（扁平键 bot_name/persona_prompt/tone/length/markdown/fallback_text/memory_enabled）
       └─ ChatRequest.extra["preference"] / skills / top_k / doc_types
            └─ ChatAgent.run(preference=...)
```

## 8. 自检

```powershell
$env:PYTHONPATH="E:\Project\TsukiyoHub\server"
# 本机沙箱下 python 子进程不能往项目目录写 __pycache__，指向可写位置（否则 compileall 报权限错）
$env:PYTHONPYCACHEPREFIX="E:\Project\HQDingTalkrobot\.pycache_tsukiyo"
& "E:\Project\HQDingTalkrobot\.venv\Scripts\python.exe" -m compileall -q E:\Project\TsukiyoHub\server\tsukiyohub
& "E:\Project\HQDingTalkrobot\.venv\Scripts\python.exe" -m pytest E:\Project\TsukiyoHub\tests -q -p no:cacheprovider
```

chat 相关单测（全部用假实现，不连外部服务）：`tests/test_chat_agent.py`（主路径 / 降级 / 核对）、
`tests/test_chat_conversation.py`（会话 / 历史 / 摘要 / 记忆 / 落库）、
`tests/test_chat_citations.py`（编号语义）、`tests/test_chat_rules.py`（路由 / 表达 / 脱敏 / 适配器）。

## 9. 排查清单

| 现象 | 先看什么 |
|---|---|
| 回答总是「演示模型…」 | `LLM_API_KEY` 是否为空 / `LLM_PROVIDER=fake`；`router.info()` 的 ready |
| 回答完全没有引用 | 知识库是否有文档（`/api/knowledge/stats`）、检索是否命中、模型是否写了 `[n]` |
| 正文编号与来源对不上 | 是否有技能自己重排了 `ctx.hits` 的编号（第 6 节不变量） |
| 一直 `searching` 没有正文 | 模型不支持工具且 `plain` 也失败；看 `usages[*].error` 与 `degraded_reason` |
| `grounded=false` 但内容看着正常 | `grounding_min_score` 偏高（换嵌入模型后必须重新标定） |
| 历史像丢了 | `history_max_turns` 太小，或已被 `history_summary_after` 压成摘要 |
| SSE 长时间只有 ping | 模型首字慢；对比 `latency_ms` / `llm_ms` / `retrieve_ms` 定位是模型还是检索慢 |
| 日志中文乱码 | 控制台编码：`PYTHONIOENCODING=utf-8`（`get_settings()` 已在 Windows 上设置） |
| 并发高了回答变慢 | `max_concurrent_chats` 排队中；看请求排队耗时（`curl -w` 或接入方埋点） |

排查手段：`ChatRequest(debug=true)`（非生产环境）会在 `ChatResponse.debug` 里返回
`mode / route / tool_calls / retrieved / llm_ms / retrieve_ms / history_turns`。

## 10. 与契约的对应关系

`docs/design/kernel-contract.md` 的 chat 小节逐个签名实现，没有改名改签名：

| 契约 | 实现位置 | 备注 |
|---|---|---|
| `EventSink` | `agent.py` | `("delta", 全文)` / `("status", …)` / `("progress", 文案)`；另加 `("tool", {"name","ok"})` |
| `UsageRecord` / `AgentResult` | `agent.py` | 额外补了 `grounded` / `trace_id` 两个带默认值字段 |
| `ChatAgent.__init__/run/build_system_prompt/simple_completion` | `agent.py` | `run` 永不抛业务异常 |
| `ConversationService.__init__/answer/resolve_session/history` | `conversation.py` | `answer` 是 `async`，内部 `asyncio.to_thread` |
| `with_citations / renumber / tidy_refs` | `citations.py` | 编号不变量见第 1.2 节 |
| `build_persona_prompt / build_tool_rules` | `prompt.py` | 提示词结构见第 2 节 |
| `guess_shape / length_instruction / expression_rules` | `answer_style.py` | 篇幅口径与测评共用 |
| `OpenAIChatClient / FakeLLMClient / ConfiguredLLMRouter / build_router` | `adapters/llm_openai.py` | 实现 `ports.llm.LLMClient` / `LLMRouter` |

## 11. 已知边界

- **`delta` 是全文**：SSE 层按 40ms 节流下发累积全文、换轮发 `reset`，OpenAI 兼容端点自己 diff 成增量；
  内核不感知传输协议的差别，也不做字符级节流。
- **同步内核**：`ChatAgent.run` 是同步的，`ConversationService` 用 `asyncio.to_thread` 包一层；
  接入方若在纯同步环境调用，直接用 `ChatAgent` 即可。
- **滚动摘要是近似判断**：只取尾部 `max(history_max_turns*2, (history_summary_after+1)*2)` 条消息，
  据此判断是否超阈值；超长会话不需要精确统计。
- **Agentic 只做到入口路由**：没有原项目的多轮检索 / 充分性判定 / 修订节点，保持简单可读；
  真正的质量提升主要来自检索参数与逐句核对（见第 7 节）。

## 12. 后续增强（本版未做）

- **「网络来源」独立列表**：`web_fetch` / `web_search` 已经把来源写进**返回给模型的文本**，
  前端也可以通过 `tool_calls` 展示「调用了什么」，所以本版不单独渲染「网络来源」区块。
  独立列表需要同时改引用编号体系（`[网n]` 与 `[n]` 两套编号、来源补足、去重），
  收益不抵风险，留到下一版；届时可在 `_finish` 里读 `ctx.extra["web_sources"]`
  追加一段来源列表，再补一条渲染单测。
- **Agentic 深化**：把原项目的「多轮检索 → 充分性判定 → 修订」节点按需加回来（当前只有入口路由）。
- **语义缓存 / FAQ 直答**：原项目有，本版按「内核保持简单」的取舍未实现；需要时作为
  `ChatAgent.run` 入口前的一层可选钩子接入（接口位已在 `run()` 的降级链之外预留）。