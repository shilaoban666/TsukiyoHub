# 聊天内核抽取设计（HQDingTalkrobot → TsukiyoHub）

> 侦察对象：`E:\Project\HQDingTalkrobot\src\fancyqube_robot\`（Python / FastAPI / LangChain / LangGraph / PostgreSQL）。
> 目标：抽出与钉钉、公司组织、办公业务无关的**通用对话内核**，落到 `E:\Project\TsukiyoHub`。只做设计与对照，不改源码。
> 依据：`service/{agent,conversation,config_store,safety,citations,container}.py`、`agentic/*`、`llm/*`、`harness/*`、`domain.py`、`api/routes_chat.py`、`db/*`。

**结论**：可抽。真正的工作量不在"拆代码"，而在**先定两份契约**（`domain` 数据 + `ports` 依赖倒置）。内核资产是四块：`ChatAgent`（模型+工具循环+引用编号）、`ConversationService`（会话段/窗口/准入/落库）、`agentic` 图（感知→计划→检索→生成→核对）、`ModelRouter`（多供应商+主备+用量）。业务耦合集中在"人、部门、群、文件、办公查询"五类工具及其话术/表字段。

## 0. 目标目录（建议）

```
src/tsukiyohub/
  domain/   chat.py knowledge.py results.py events.py      # 纯数据，仅 pydantic
  ports/    llm.py knowledge.py store.py config.py safety.py identity.py
  kernel/   agent.py conversation.py citations.py safety.py config.py prompt.py style.py grounding.py ground_check.py context.py
  agentic/  state.py graph.py node.py runner.py rules.py limits.py trace.py focus.py
            nodes/{perceive,plan,rag,synthesize,verify}.py
  llm/ router.py fake.py          harness/ base.py registry.py knowledge.py
  retrieval/ store/               adapters/http/ routes_chat.py deps.py   app/bootstrap.py
  plugins/  directory/ web/ files/ memory/ lexicon/        # 可选，注册即生效
```
依赖方向（强约束）：`domain ← ports ← kernel/agentic/llm/harness ← adapters ← plugins`。`domain`/`ports` 不得 import 上层；`kernel` 不得 import 任何 `plugins`。

## 1. 职责边界与「源文件 → 目标文件」对照表

### 1.1 判定口径
| 判据 | 归属 |
|---|---|
| 只依赖"问题/历史/用户上下文/检索结果/模型"，不假设组织形态 | **纯内核（保留）** |
| 需要"人、部门、群、文件、办公系统"才能工作 | **插件（可选，默认不装）** |
| 表字段、话术、提示词里写死钉钉/公司/部门/办公 | **去业务化（删或提为配置）** |

### 1.2 对照表（覆盖全部相关文件）
| # | 源文件（`fancyqube_robot/`） | 处置 | 目标（`tsukiyohub/`） | 说明 |
|---|---|---|---|---|
| 1 | `domain.py`（ChatRequest/ChatResponse/Citation/Intent） | 保留·拆分 | `domain/chat.py` | 删 `department`；`conversation_id`→`channel_thread_id`；`Intent` 只留 `knowledge_qa/smalltalk/unknown` |
| 2 | `domain.py`（Document/Chunk/ScoredChunk/DocumentMeta） | 保留 | `domain/knowledge.py` | `department/version/doc_type/source_type` 并入 `metadata: dict` |
| 3 | `service/agent.py` | 保留·去业务 | `kernel/agent.py` | 删 `_dept_names/_contacts/_contact_names/_PEOPLE/_web_sources`；`BOT_NAME` 改配置 |
| 4 | `service/conversation.py` | 保留·拆薄 | `kernel/conversation.py` | 留去重、段窗口、准入、并发槽、上下文、摘要、落库钩子、SSE relay；删评价/群KB/发文件/附件投稿/部门范围/CATEGORY_BY_DEPT |
| 5 | `service/config_store.py` | 保留·泛化 | `kernel/config.py` + `ports/config.py` | 键名空间化；`model_config/preference/quota/robot_state` 并为 `KernelConfig` 子树 |
| 6 | `service/safety.py` | **原样保留** | `kernel/safety.py` | 脱敏+占位符还原+逐字暂缓（holdback），内核里最干净的一块 |
| 7 | `service/citations.py` | 保留 | `kernel/citations.py` | 逐段→逐文档编号、页码合并、否定句不挂引用、补足 N 篇，全通用 |
| 8 | `agentic/graph.py` | 保留·参数化 | `agentic/graph.py` | `MiniGraph` 保底（无 LangGraph 也能跑）；`route_directory`→通用插件路由 |
| 9 | `agentic/state.py` | 保留 | `agentic/state.py` | `Perception.intent` 取值可配置；`RagPolicy/Budget/Verdict` 原样 |
| 10 | `agentic/node.py` | 保留 | `agentic/node.py` | `RunCtx.agent` 改 `KernelAgentPort`；守卫/竞速/预算通用 |
| 11 | `agentic/runner.py` | 保留·去业务 | `agentic/runner.py` | 删 `add_pending/parents/has_knowledge(LOCAL_KB)/release_id` |
| 12 | `agentic/nodes/perceive.py` | 保留·去业务 | `agentic/nodes/perceive.py` | `from_llm/enrich/focus_text` 通用；`lexicon` 插件注入 |
| 13 | `agentic/nodes/plan.py` | 保留·去业务 | `agentic/nodes/plan.py` | 删 `DIRECTORY/FILES/OFFICE/SEND_TOOL`、`known_name_in` |
| 14 | `agentic/nodes/rag.py` | 保留·去业务 | `agentic/nodes/rag.py` | 删 `LOOKUP_TOOLS/directory_lookup/dept_kbs`；`mixed` 改"通用并行插件查询" |
| 15 | `agentic/nodes/synthesize.py` | 保留·去业务 | `agentic/nodes/synthesize.py` | `_tool_question` 删业务判定；`web_on_miss` 移插件；`release`→`knowledge_version` |
| 16 | `agentic/nodes/verify.py` | 保留 | `agentic/nodes/verify.py` | V1–V7 规则通用（数字/版本/拒答一致/完整性） |
| 17 | `agentic/prompts.py` | 保留·模板化 | `kernel/prompt.py` | 中文提示词外置可覆盖（含 `EVIDENCE_OPEN/CLOSE` 围栏） |
| 18 | `agentic/config.py` | 保留·合并 | `kernel/config.py`（`agentic` 段） | `companyTerms/smalltalkTerms/synonyms` 默认值清空，示例进文档 |
| 19 | `agentic/limits.py` | **原样保留** | `agentic/limits.py` | `StreamGuard/Breaker/Triggers/cut_at_sentence/repeated` 全通用 |
| 20 | `agentic/rules.py` | 保留·去业务 | `agentic/rules.py` | 删 `DIRECTORY_PAT`、`looks_like_*`；`COMPANY_ASSERT`→配置词表；`numbers_of/situation/missing_constraints` 保留 |
| 21 | `agentic/trace.py` | 保留·抽象 | `agentic/trace.py` | `db`→`TraceSinkPort`；删 `dept_id/kb_release` |
| 22 | `agentic/shadow.py` | **丢弃** | — | 依赖线上 `chat_message` 配对与人工盲判后台，属运营侧 |
| 23 | `agentic/focus.py` | 保留 | `agentic/focus.py` | 对话焦点（话题/实体/已澄清）通用 |
| 24 | `llm/router.py` | 保留·解 DB | `llm/router.py` + `ports/llm.py` | 三张表 → `ModelSpec` 配置对象；`ResolvedModel.cost()` 保留 |
| 25 | `llm/fake.py` | 保留·去业务词 | `llm/fake.py` | 触发词可配置；`_agentic_reply` 改按 `call_type` 打桩（不再匹配中文提示词） |
| 26 | `harness/base.py` | 保留 | `harness/base.py` | `Tool` Protocol + `ToolContext`（**全局编号总线**，语义原样） |
| 27 | `harness/registry.py` | 保留 | `harness/registry.py` | `enabled()` 开关解析 + 调用计数 |
| 28 | `harness/knowledge.py` | 保留·抽象 | `harness/knowledge.py` | `TOOL_NAME`→`search_knowledge`；`visibility/group_kb` 移插件 |
| 29 | `harness/{directory,office,kb_files,send_file,web,doc_link}.py`、`mcp/`、`skills/` | **插件** | `plugins/directory/`、`plugins/web/`、`plugins/files/` | 默认不注册；`send_file`（两步确认发文件）直接丢弃 |
| 30 | `api/routes_chat.py` | 保留·重写 | `adapters/http/routes_chat.py` | 三端点行为兼容（§5），换 deps |
| 31 | `rag/prompt.py`（format_context/pack_context/build_citation/extractive_answer） | 保留 | `kernel/context.py` | 上下文拼装 + 引用骨架 |
| 32 | `rag/answer_style.py`（HARD_LIMIT/length_instruction/expression_rules/progress_found） | 保留·配置化 | `kernel/style.py` | 长度档位、版式模板外置 |
| 33 | `rag/grounding.py`（Gate/grade/is_grounded） | 保留 | `kernel/grounding.py` | 阈值来自配置（§4.2） |
| 34 | `rag/{multi_query,pipeline,rerank,fusion,embeddings,milvus}.py` | 保留·减配 | `retrieval/` | 统一检索管线；`rerank_provider` 的 dashscope 依赖改通用 OpenAI 兼容 |
| 35 | `rag/{ground_check,semantic_cache}.py` | 保留 | `kernel/ground_check.py`、`retrieval/cache.py` | 逐句核对删句；语义缓存默认关 |
| 36 | `kb/{store,lexicon,visibility,links,actions,faq}.py` | 分别 | `store/`、`plugins/lexicon/`、`plugins/visibility/`、`plugins/links/` | `KnowledgeStore` 保留；可见范围/钉盘链接/操作目录→插件 |
| 37 | `memory/*` | 插件/可选 | `plugins/memory/` | 记忆指令、事实抽取；内核只留 `RollingSummary` 钩子 |
| 38 | `service/org.py`（OrgService/UserContext） | **丢弃·接口化** | `ports/identity.py` | 内核只收 `UserProfile(id, name, attributes: dict)` |
| 39 | `db/models.py`（dt_user/department/dt_group/chat_*） | 重写 | `store/models.py` | 见 §4.1 |
| 40 | `service/container.py` | 重写 | `app/bootstrap.py` | 保留装配顺序与降级骨架，去钉钉 client/kbsync/insight |
| 41 | `evals/*`、`insight/*`、`kbsync/*`、`admin/*`、`adapters/dingtalk/*`、`adapters/qwenwork/*` | **丢弃** | — | 测评/驾驶舱/同步/后台/渠道适配器均不入内核 |

> 体量参考：`service/`（~7.7k 行）中可进内核约 2.1k 行（agent+conversation+citations+safety+config_store）；`agentic/`（~2.5k 行）约 85% 可进（丢 `shadow.py` 与规则里的业务分支）。**内核约 4.5k 行，是可搬的量级。**

## 2. 公共接口契约（可直接照抄实现）

### 2.1 数据模型（`domain/`）
```python
# domain/chat.py
class Intent(str, Enum):                    # 可扩展声明，默认三项
    KNOWLEDGE_QA = "knowledge_qa"; SMALLTALK = "smalltalk"; UNKNOWN = "unknown"

class ChatRequest(BaseModel):
    question: str = ""                      # 长度校验在 HTTP 层（1..4000），内核允许空串
    session_id: str | None = None
    user_id: str | None = None; user_name: str | None = None
    channel: str = "api"                    # 渠道标识：仅日志/差异化，不参与逻辑
    channel_thread_id: str | None = None    # 原 conversation_id（群/线程）
    top_k: int | None = Field(None, ge=1, le=30)
    doc_types: list[str] | None = None
    scope_ids: list[str] | None = None      # 原 departments：语义改为"检索范围标签"
    stream: bool = False; debug: bool = False       # debug 仅非生产环境生效
    extra: dict[str, Any] = {}
    @property
    def has_question(self) -> bool: ...
    def normalized_question(self) -> str: ...        # 去 @ 前缀改为 settings.mention_prefixes

class ChatResponse(BaseModel):
    answer: str; session_id: str
    intent: Intent = Intent.KNOWLEDGE_QA
    citations: list[Citation] = []
    model: str | None = None; provider: str | None = None
    latency_ms: int = 0
    degraded: bool = False                  # 未走大模型（无 Key / 全失败）
    degraded_reason: str | None = None      # no_hit / all_models_failed:* / busy / blocked / db_unavailable:*
    grounded: bool = True                   # 是否有知识库依据
    grounding_score: float | None = None
    usage: dict[str, Any] = {}              # {"input_tokens","output_tokens","total_tokens"}
    trace_id: str | None = None; debug: dict[str, Any] | None = None
    actions: list[dict[str, str]] = []
    # 渠道兼容别名：text / content == answer
    def answer_with_citations(self, max_citations: int = 3) -> str: ...   # grounded=False 或 no_hit 时不挂引用

# domain/knowledge.py
class Chunk(BaseModel):
    chunk_id: str; doc_id: str; text: str; ordinal: int = 0
    title: str = ""; uri: str = ""
    metadata: dict[str, Any] = {}            # 原 department/version/source_type/doc_type/extra 全并入
    @property
    def citation(self) -> str: ...           # 「《标题》 · 第 N 段」（版本/部门有则追加）

class ScoredChunk(BaseModel):
    chunk: Chunk; score: float = 0.0
    vector_score: float | None = None; bm25_score: float | None = None; rerank_score: float | None = None
    retriever: Literal["vector","bm25","hybrid","fusion","none"] = "hybrid"

class Citation(BaseModel):
    index: int                              # 员工看到的编号（逐文档）
    doc_id: str; title: str; uri: str = ""; snippet: str = ""
    label: str = ""                         # 「《X》 · v2」
    version_id: str = ""
    url: str = ""; folder_url: str = ""; folder_name: str = ""
    score: float = 0.0
    cited: bool = True                      # False = 仅为补足篇数（docs 25 §2）

# domain/results.py
@dataclass
class AgentResult:
    answer: str; citations: list[Citation] = []
    kb_called: bool = False; hit: bool = False
    tool_calls: list[dict[str, Any]] = []; usages: list[UsageRecord] = []
    model_id: str | None = None; model_name: str | None = None
    provider_id: str | None = None; provider_name: str | None = None
    degraded: bool = False; degraded_reason: str | None = None
    latency_ms: int = 0
    mode: str = "agent"            # agent/plain/agentic/cache/faq/degraded/notice/clarify
    trace_id: str = f"{uuid4().hex[:12]}"
    llm_ms: int = 0; retrieve_ms: int = 0
    retrieved: list[dict[str, Any]] = []
    route: str = ""                # direct/rag/mixed/cache/clarify/fallback…
    agentic: dict[str, Any] = {}   # {"route","verdict","perception","rag","cached"}
    evidence: dict[int, str] = {}  # 编号→被引片段原文（不返回渠道）
    context: dict[int, str] = {}   # 编号→交给模型的全部片段
    actions: list[dict[str, str]] = []; suggestions: list[str] = []
    focus: dict[str, Any] | None = None
    # 只读属性：prompt_tokens / completion_tokens / total_tokens（累加 usages）

@dataclass
class UsageRecord:
    model_id: str | None; model_name: str; provider_id: str | None; call_type: str
    prompt_tokens: int = 0; completion_tokens: int = 0; total_tokens: int = 0
    cost: float = 0.0; latency_ms: int = 0; success: bool = True; error: str | None = None

@dataclass
class InboundMessage:
    text: str; user_id: str; user_name: str = ""
    channel: str = "api"; conversation_type: str = "single"      # single / group / thread
    conversation_id: str = ""; conversation_title: str = ""; msg_id: str | None = None

@dataclass
class ConversationReply:
    text: str; kind: str = "answer"       # answer / notice / duplicate
    citations: list[Citation] = []; show_citations: bool = True; markdown: bool = True
    grounded: bool = False; kb_called: bool = False
    degraded: bool = False; degraded_reason: str | None = None
    latency_ms: int = 0; model: str | None = None; provider: str | None = None
    usage: dict[str, Any] = {}; session_id: str = ""; segment_id: str = ""
    message_id: int | None = None; trace_id: str | None = None
    first_token_ms: int | None = None; debug: dict[str, Any] | None = None
    suggestions: list[str] = []; actions: list[dict[str, str]] = []
    @property
    def text_with_citations(self) -> str: ...     # 正文 + 按钮行 + 末尾「引用文档：」
    def to_chat_response(self) -> ChatResponse: ...
```

### 2.2 事件协议（`domain/events.py`）
`EventSink = Callable[[str, Any], None]`；**内核层只有三种事件**：
| 事件 | payload | 语义 |
|---|---|---|
| `delta` | `str`，**当前全文**（累计，不是增量） | 每次流式片段合并后的完整文本；换轮次（查库后重答）全文会重置，调用方按"不以已发送内容为前缀"判断重来 |
| `status` | `searching`/`understanding`/`searching_more`/`verifying`/`revising` | 阶段提示；`agent.py` 只发 `searching`，agentic 节点发其余 |
| `progress` | `str`，如 `正在查找：《X》等 3 份文件` | 正文前的过程文案（`style.progress_found`） |

HTTP 适配层另有三个**仅传输层**事件：`reset`（清空已推送）、`final`（终态 `ChatResponse`）、`meta`/`done`（SSE 帧名）。内核不感知 SSE。

### 2.3 `ChatAgent`（`kernel/agent.py`）
```python
class ChatAgent:
    def __init__(self, settings: KernelSettings, router: ModelRouter, config: ConfigStore,
                 tools: ToolRegistry, safety: SafetyService | None = None) -> None: ...
    # 可选注入位：agentic / links / actions / faq / semantic（None = 关该能力）
    def run(self, question: str,
            history: list[dict[str, str]] | None = None,
            user_ctx: dict[str, Any] | None = None, *,
            model_config: dict[str, Any] | None = None,
            model_id: str | None = None,          # 指定模型时跳过 FAQ / 缓存
            preference: dict[str, Any] | None = None,
            call_type: str = "chat",              # chat / summary / judge / profile …
            session_key: str = "",
            on_event: EventSink | None = None,
            mask: MaskSession | None = None) -> AgentResult: ...
    def mask_session(self, pref: dict | None = None) -> MaskSession: ...
    def harness_config(self) -> dict[str, Any]: ...          # {"tools": {name: bool}}
    def build_system_prompt(self, pref: dict, user_ctx: dict, tools: list[Tool] | None = None) -> str: ...
    def simple_completion(self, prompt: str, call_type: str = "profile", model_id: str | None = None,
                          params: dict | None = None) -> tuple[str, list[UsageRecord]]: ...
    # ---- 以下 4 个是 agentic 依赖的「半公开 API」，必须写进 ports ----
    def _new_result(self, resolved: ResolvedModel, mode: str = "agent") -> AgentResult: ...
    def _call(self, runnable: Any, messages: list[BaseMessage],
              on_event: EventSink | None, mask: MaskSession) -> Any: ...   # 有 sink 走 stream
    def _usage(self, resolved: ResolvedModel, ai: Any, call_type: str, latency: int) -> UsageRecord: ...
    def _finish(self, result: AgentResult, ctx: ToolContext, pref: dict,
                mask: MaskSession, resolved: ResolvedModel) -> AgentResult: ...  # 引用编号唯一收口
```
`run()` 语义：**永不抛业务异常**。全模型失败 → `degraded=True, mode="degraded"`，正文用 `extractive_answer`（前 3 段原文）或 `preference.fallback.text`。`_ToolUnsupported` 是内部信号：捕获后 `router.mark_tools_support(model_id, False)` 并转 `_run_plain`。

### 2.4 `ConversationService`（`kernel/conversation.py`）
```python
class ConversationService:
    def __init__(self, settings: KernelSettings, store: SessionStore, config: ConfigStore,
                 agent: ChatAgent, identity: IdentityPort, hot: HotStore | None = None) -> None: ...
    def handle(self, msg: InboundMessage, on_event: EventSink | None = None,
               open_stream: Callable[[], Any] | None = None) -> ConversationReply | None:
        """None = 重复消息不回复。open_stream 只在"确定要调模型"时调用（渠道可据此开卡片）。"""
    def record_usages(self, usages: list[UsageRecord], user_id: str | None = None) -> None: ...
    def is_duplicate(self, msg_id: str) -> bool: ...

def inbound_from_request(request: ChatRequest) -> InboundMessage: ...
def not_ready_response(reason: str) -> ChatResponse: ...
async def answer_chat_request(kernel: Kernel, request: ChatRequest,
                              on_event: EventSink | None = None) -> ChatResponse:
    """线程池执行 conversation.handle；reply 为 None 时返回空 answer。"""
```
业务扩展点保留源实现的"类属性注入可选对象"模式：`attachments/send_files/group_kbs` 三个字段删除，改为 `pre_hooks: list[PreHook]`（**在准入检查之前执行且不占额度**）。

### 2.5 `ModelRouter`（`llm/router.py`）+ `ports/llm.py`
```python
@dataclass
class ResolvedModel:
    model_id: str; code: str
    provider_id: str; provider_name: str; provider_type: str    # openai / azure / fake
    base_url: str; api_key: str; api_key_id: str | None
    supported_params: list[str]
    input_price: float = 0.0; output_price: float = 0.0
    supports_tools: bool | None = None
    extra: dict[str, Any] = {}
    def cost(self, prompt_tokens: int, completion_tokens: int) -> float: ...

class ModelRouter:
    def resolve(self, model_id: str) -> ResolvedModel: ...        # 抛 ModelUnavailable / ValueError
    def primary_id(self, cfg: dict | None = None) -> str | None: ...
    def fallback_id(self, cfg: dict | None = None) -> str | None: ...
    def chat_model(self, resolved: ResolvedModel, params: dict | None = None) -> BaseChatModel: ...
    def mark_tools_support(self, model_id: str, supported: bool) -> None: ...
    def touch_key(self, api_key_id: str | None) -> None: ...      # 每分钟最多写一次
    def invalidate(self) -> None: ...                             # 配置变更回调
```
参数映射必须保留：`temperature / topP→top_p / maxTokens→max_tokens / presencePenalty / frequencyPenalty`；`maxTokens` 转 int，其余 float；`supported_params` 非空时按白名单过滤。`thinking_switch()`（`enable_thinking`）是供应商特性，改为 `ModelSpec.extra_body` 显式配置，**不要按模型名前缀猜**。

### 2.6 工具注册表（`harness/`）
```python
@dataclass
class ToolContext:                     # 一次问答里所有工具共享；编号必须在多次检索间连续
    question: str; user_ctx: dict[str, Any]
    cfg: dict[str, Any]; pref: dict[str, Any]
    hits: dict[int, ScoredChunk] = {}  # 全局编号→命中（引用挑选的唯一依据）
    next_index: int = 1
    records: list[dict[str, Any]] = []     # 每次调用记录（落库展示）
    retrieved: list[dict[str, Any]] = []   # 检索调试（分数/闸门）
    kb_called: bool = False
    timings: dict[str, int] = {}           # 各工具累计毫秒
    direct: str = ""                       # 工具直出回复（不经模型改写）
    trusted_urls: set[str] = set()         # 工具给的链接，免被"禁止编链接"清理
    history: list[dict[str, Any]] = []
    web_sources: list[tuple[str, str, str]] = []
    def add_time(self, key: str, ms: float) -> None: ...

class Tool(Protocol):
    name: str; title: str
    spec: dict[str, Any]                  # OpenAI function 规格
    rules: str                            # 追加到系统提示词「工具使用规则」
    default_enabled: bool
    def call(self, args: dict[str, Any], ctx: ToolContext) -> str: ...
    def prefetch(self, ctx: ToolContext) -> str | None: ...   # 模型不支持工具时「先查再答」

class ToolRegistry:
    def register(self, tool: Tool) -> Tool: ...
    def get(self, name: str) -> Tool | None: ...
    def enabled(self, harness_cfg: dict | None = None) -> list[Tool]: ...   # 开关缺省 = default_enabled
    def count_call(self, name: str) -> None: ...
    def describe(self, harness_cfg: dict | None = None) -> list[dict[str, Any]]: ...

class KnowledgeTool:                      # name = "search_knowledge"
    def call(self, args: {"query": str}, ctx: ToolContext) -> str: ...
    def prefetch(self, ctx: ToolContext) -> str | None: ...      # "## 参考资料\n" + format_context
    def search(self, query: str, ctx) -> tuple[list[tuple[int, ScoredChunk]], str]: ...
    def search_multi(self, retriever, request: RetrievalRequest, ctx,
                     threshold: float | None = None) -> tuple[list[tuple[int, ScoredChunk]], Any]: ...
    def retrieve(self, query: str, cfg: dict, filters: Any,
                 top_k: int | None = None, hints: Any = None) -> list[ScoredChunk]: ...
    def filters(self, ctx) -> SearchFilters | None: ...          # 可见范围（插件）
    def hints(self, ctx) -> RerankHints | None: ...
```
**不变量**：`hits` 编号一旦分配不得重排；`_finish` 是唯一引用编号收口点；`prefetch` 与 `call` 必须写同样的 `ctx` 字段（`hits/retrieved/timings/kb_called`）。

### 2.7 Agentic 图 state 字段（`agentic/state.py`）
```python
class ChatState(TypedDict, total=False):
    question: str; history: list[dict[str, Any]]; cached: bool
    perception: Perception; plan: Plan
    evidence: Annotated[list[Evidence], operator.add]      # 累加
    sufficient: bool
    rag_summary: dict[str, Any]     # {"rounds":[{round,queries,perQuery,recalled,rerank,preRerank,top,timings,partial}],
                                    #  "topVector","fuzzy","sufficient"}
    directory: str                  # 插件查找结果（原通讯录/文件/办公）
    draft: str; emitted: bool
    answer_check: Verdict; ground: dict[str, Any]           # {"removed","unsupported"} 或 {"error"}
    revised: bool; answer: str
    route_final: str                # direct/rag/mixed/directory/clarify/web/cache
    verdict: str                    # pass/revised/fallback/insufficient/clarify/cached/skipped/error/web
    clarify: dict[str, Any]         # {"term","options","choices","buttons"}
    budget: Budget
    trace: Annotated[list[dict[str, Any]], operator.add]

class RagState(TypedDict, total=False):
    question: str; perception: Perception; plan: Plan; budget: Budget
    evidence: Annotated[list[Evidence], operator.add]
    rag_summary: dict[str, Any]
    trace: Annotated[list[dict[str, Any]], operator.add]
    queries: list[SubQuery]; round: int
    fail_reason: str                # ""/uncovered/low_overlap/no_overlap
    sufficient: bool; covered: list[str]

@dataclass(frozen=True)
class Perception:
    intent: str = "unknown"         # smalltalk/general/company/directory/mixed/unknown（可配置）
    needs_kb: bool = True; confidence: float = 0.5
    standalone: str = ""; rewrites: tuple[str, ...] = (); sub_questions: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    time_kind: str = "current"      # current/as_of/history/compare
    time_date: str = ""             # YYYY-MM-DD
    categories: tuple[str, ...] = (); titles: tuple[str, ...] = ()
    answer_shape: str = "open"      # fact/steps/table/compare/open/chat
    source: str = "rules"           # rules/llm/rules+llm/fallback
    rule: str = ""                  # 命中规则号 R1…R8
    lexicon: tuple[str, ...] = ()
    ambiguity: tuple[str, ...] = (); ambiguous_term: str = ""
    assumptions: tuple[str, ...] = ()
    act: str = "new"                # new/follow/correct/confirm/thanks/complain
    mood: str = "calm"              # calm/urgent/confused/upset
    register: str = "written"       # spoken/written
    expertise: str = ""             # new(<90d)/senior/""
    focus_update: str = "keep"      # keep/refine/shift

@dataclass(frozen=True)
class RagPolicy: max_rounds=2; max_queries=4; per_query_k=20; top_k=8; max_per_doc=3
                 l2="auto"; boost_docs=frozenset(); history=False; as_of: int | None = None
@dataclass(frozen=True)
class Plan:      route="rag"; reason=""; policy: RagPolicy; note=""
@dataclass(frozen=True)
class Evidence:  index:int; chunk_id:str; doc_id:str; title:str; version_label:str
                 score:float; vector_score:float|None; query_kind:str="original"
@dataclass(frozen=True)
class Verdict:   ok:bool=True; action="pass"        # pass/retry/revise/fallback
                 checks:dict[str,bool]; notes:tuple[str,...]
@dataclass(frozen=True)
class Budget:    llm_calls_left=6; rag_rounds_left=2; tokens_left=24000; reroutes_left=1
                 def spend(calls=1, tokens=0) -> Budget
                 .can_call   # llm_calls_left>0 and tokens_left>0
                 .exhausted  # "llm_calls" / "token_budget" / ""
```
`RunCtx`（不序列化，放 `config["configurable"]["ctx"]`）：`agent, resolved, cfg, pref, user_ctx, tool_ctx, mask, emit, deadline, settings, services, mode(live/shadow/debug), history, filters, scope_label, usages, llm_ms, emitted, last_text, ttft_ms, started, snapshots, backup, stop_reason, speculative`。
方法：`left_ms() / expired() / status(v) / progress(t) / stream_sink() / chat(params) / invoke(messages, call_type, params=None, stream=False, resolved=None, timeout_ms=None) -> str / generate(messages, call_type="chat", stream=True) -> str / max_chars() / take_tokens()`。

图结构（`graph.py`，原样保留）：
```
START → cache ─hit→ finalize
        └miss→ perceive → plan → guard ─┬direct→ verify_direct ─rag→ rag ; ─pass→ finalize
                                        ├directory→ {synthesize | rag}
                                        ├rag→ rag ─sufficient→ synthesize ; ─insufficient→ fallback
                                        ├mixed→ synthesize
                                        └clarify→ finalize
rag 子图: START → rag_perceive → rag_plan → rag_execute → rag_verify ─retry→ rag_plan ; ─done→ END
synthesize → verify_answer ─pass→ finalize ; ─revise→ revise → verify_answer ; ─fallback→ fallback
fallback → finalize → END                    recursion_limit = 25
```

## 3. 编排流程（一次问答的完整链路）

### 3.1 装配（`app/bootstrap.py` ← `service/container.py`）
`Settings → KnowledgeStore → DB → ConfigStore → ModelRouter(SecretBox) → IdentityPort → ToolRegistry(KnowledgeTool + 插件) → SafetyService → ChatAgent → ConversationService → AgenticRunner(build_agentic) → agent.agentic`。
装配失败只降级（`db_error` / `agentic=None`），服务仍启动、仍能回"暂时无法服务"——**这个骨架要保留**。

### 3.2 主链路
| # | 步骤 | 文件 / 函数 | 通用？ | 需参数化 |
|---|---|---|---|---|
| 1 | HTTP 校验（非空、≤4000） | `routes_chat._validate_question` | 通用 | 上限 |
| 2 | 线程池执行 `handle` | `conversation.answer_chat_request` | 通用 | — |
| 3 | 去重（`msgId`） | `handle` → `sessions.claim_message` / `is_duplicate` | 通用 | 键前缀、TTL |
| 4 | 识别用户 | `org.ensure_user` → `identity.ensure` | **否** | 换 `IdentityPort` |
| 5 | 业务短路径（评价/群公告/发文件确认/附件投稿/记忆指令） | `_handle_serial` | **否** | 改 `pre_hooks`，默认空 |
| 6 | 准入（在线/时段/部门/群范围/敏感词/额度） | `_precheck` | 半通用 | 留"在线/时段/敏感词"；部门/群/额度移插件；话术可配 |
| 7 | 同用户串行锁 + 全局并发槽 | `sessions.user_lock` / `_slots` | 通用 | `max_concurrent`、锁超时 |
| 8 | 会话/段（间隔 >N 分钟新开段）+ 上下文窗口 | `_open_turn` / `_context_window` | 通用 | `segment_idle_minutes`、`MAX_CONTEXT_MESSAGES=20` |
| 9 | 滚动摘要（窗口外轮次压摘要） | `memory.summary.RollingSummary` | 通用 | `contextRounds`、每 4 轮 |
| 10 | 记忆召回 / 附件上下文 | `memory.recall` / `attachments.context` | 插件 | `budgetTokens`、`semanticMinSim`、24h |
| 11 | `run`：FAQ 直答 → 语义缓存 | `_faq_answer` / `_cached_answer` | 通用 | 开关、`answerCacheTtlMin` |
| 12 | 主备模型遍历 | `run` 的 candidates 循环 | 通用 | 备用开关 |
| 13 | 分支：`agentic.mode_for` → live/shadow/旧链路 | `_run_with_model` | 通用 | `agentic.mode`、作用域 |
| 14 | 旧链路 A：预取知识库（规则判 `needs_kb`）+ 工具轮循环 | `_run_agent` | 通用 | `agent_max_tool_rounds=3`、`agent_kb_prefetch` |
| 15 | 旧链路 B：模型不支持工具 → 各工具 `prefetch` 拼系统提示词，单次调用 | `_run_plain` | 通用 | — |
| 16 | Agentic 前置：cache → perceive（R1–R8 规则 + 一次轻模型）→ plan → guard（只往更安全方向改：`direct→rag`、工具关→`direct`、无 KB→`direct`、时间不足→1 轮关 L2） | `agentic/nodes/*` | 通用 | `domain_terms`、`smalltalk_terms`、`planMinConf`、`clarifyMinConf`、`maxRounds/maxQueries/perQueryK/topK/maxPerDoc` |
| 17 | 检索：原问 1.0 + 改写 0.8 + 子问题 1.0 + 术语扩展 0.8 + 关键词 0.6(BM25) → RRF → 重排 → 每文档上限 | `nodes/rag.py`、`retrieval/` | 通用 | 术语表、`rerankL2`、`gate.low/high` |
| 18 | 充分性：规则优先（分数/词面/子问题覆盖），边界才一次模型判定；不足→第 2 轮只换一种策略（未覆盖子问题/同义/HyDE） | `rag_verify` / `rag_plan` | 通用 | `llmJudge`、`hyde`、`JUDGE_CHARS=400` |
| 19 | 生成（流式+守卫+竞速）：资料用 `<<<参考资料开始/结束>>>` 包裹；证据不足走 `FUZZY_RULES` | `nodes/synthesize.py` | 通用 | 提示词模板、`HARD_LIMIT`(1500/800)、`genFirstTokenMs/raceAfterMs/genTotalMs` |
| 20 | 核对 V1–V7：删无效编号→要求有引用→数字一致→版本一致→拒答一致→逐句核对删句→完整性 | `nodes/verify.py`、`ground_check.py` | 通用 | `answerVerify/answerJudge/completenessCheck`、`CHECK_EVIDENCE=10`、`CHECK_TIMEOUT_MS=12000` |
| 21 | 修订/兜底（拒答只留结论、删无依据句、不倒原文） | `revise` / `fallback` | 通用 | `reviseTimeoutMs`、`NO_HIT_ANSWER` |
| 22 | **引用编号收口（唯一）**：按正文 `[n]` 挑引用 → `tidy_refs`（否定句不挂、一句≤2 篇）→ `renumber`（逐段→逐文档+页码）→ `merge_pages` → `related` 补足 5 篇 → 标点清理 | `ChatAgent._finish`、`citations.py` | 通用 | `MAX_SOURCES=5`、`MAX_REFS_PER_SENTENCE=2` |
| 23 | 脱敏还原、禁编链接、按钮、`suggestions` | `_finish` | 通用 | 链接白名单来源 |
| 24 | 落库：assistant 消息 + 用量明细 + 段/会话计数 | `_finish_turn` | 通用（换表） | 计数口径 |
| 25 | 热层追加、摘要异步更新、SSE relay、组 `ConversationReply` | `sessions.save` / `update_async` / `relay` | 通用 | `SessionCache` TTL、欢迎语、推荐问题 |

### 3.3 流式细节（最容易实现错的点）
* `delta` 是**全文**：SSE 层负责 diff 成增量，并在"全文不以已发送内容开头"时先发 `reset`（换轮次重答）。
* `mask.stream_view()` **暂缓末尾 `holdback` 字**（默认 32 = 最长敏感词长度），避免手机号被切两半漏出；结束必须发一次 `final=True`。生成节点用 `ctx.generate()`：字数上限、复读检测（200 字窗口内 20 字片段出现 ≥3 次）、总时长上限都会 `StreamStop`，文末追加 `TRUNCATED_NOTE`。
* 已推送正文后图内报错：**不回退**旧链路（否则用户看到两段答案），用 `ctx.last_text` 作答，`verdict="skipped"`。

## 4. 去业务化清单

### 4.1 删除：表 / 字段 / 函数
| 对象 | 处理 |
|---|---|
| `department`、`dt_group`、`dt_user`（`union_id/job_number/dept_ids/main_dept_id/dept_name/roles/is_admin/is_boss/avatar/hired_at`） | **删**，换 `app_user(id, name, external_id, attributes JSONB)` |
| `chat_segment.dept_id / top_dept_id / category / rating / rated_at / rating_prompted_at / session_webhook*` | **删**（评价推送整体去掉） |
| `chat_message.dingtalk_msg_id` | 改名 `external_msg_id`；`conversation_type/conversation_id` 中性化保留 |
| `chat_message.topic` + `topics.classify` | 移插件（话题归类属业务分析） |
| `token_usage.dept_id / top_dept_id`、`agent_trace.dept_id / kb_release` | **删**；后者改 `knowledge_version` |
| `kb_pending_question`（待补充问题）、`kpi_*`/insight/contribution 系列、`faq` 业务字段 | 不进内核 |
| `service/agent.py` | `_dept_names`、`_contacts`、`_contact_names`、`_PEOPLE`、`_web_sources`；`AGENT_RULES` 中"（钉钉会渲染）" |
| `service/conversation.py` | `RATING_RE/RATING_ACK/_handle_rating/rate_by_card/_forget`、`CATEGORY_BY_DEPT/_category_of/GENERAL_CATEGORY`、`_precheck` 部门/群分支、`SEND_CONFIRM`、`attachments.contribute`、`group_kbs.*`、`welcome` 的 `channel=="dingtalk"`、`channel_of` 的 `qwenwork` |
| `domain.py` | `Intent.OA_REQUEST/APPROVAL/ATTENDANCE_FIX/CHECK_IN/MEETING_ROOM/PERFORMANCE`、`Channel.DINGTALK/QWENWORK`、`DocType.POLICY/SOP/NOTICE/FORM`（改 `doc_type: str`）、`SourceType.DINGTALK/UPLOAD`、`normalized_question` 写死的三个 @ 前缀 |
| `agentic/rules.py` | `DIRECTORY_PAT`、`looks_like_link_request`、`looks_like_{send_request,office_question,file_question}` 引用、`COMPANY_ASSERT`（→ 配置 `domain_assertion_terms`） |
| `agentic/nodes/plan.py` | `DIRECTORY_TOOL/FILES_TOOL/OFFICE_TOOL/SEND_TOOL`、`known_name_in`、`PERSON_QUESTION` |
| `agentic/nodes/rag.py` | `LOOKUP_TOOLS`、`directory_lookup`、`dept_kbs`、`mixed` 的通讯录并行 |
| `agentic/nodes/synthesize.py` | `_tool_question` 的四类业务判定、`web_on_miss`（移 `plugins/web`）、`cache_key` 的 `release_id` |
| `agentic/runner.py` | `add_pending`、`parents`、`has_knowledge` 的 `LOCAL_KB`、`release_id`、`AgenticServices.visibility/db/lexicon` |
| `agentic/shadow.py` / `graph.route_directory` | 整文件删除 / 改通用 `route_plugin` |
| `harness/knowledge.py` | `visibility`、`group_kb.scoped`、`hints()` 部门加权、默认 `LexicalReranker` |
| `llm/router.py` | `THINKING_SWITCH_PREFIXES`（按模型名猜）→ 配置 `extra_body` |
| `__init__.py` | `COMPANY_NAME="合趣网络公司"`、`BOT_NAME`、`APP_TITLE` → `KernelSettings.bot_name/company_name=""` |

### 4.2 保留但必须改成配置项
| 项 | 现值 / 位置 | 目标配置键 |
|---|---|---|
| 系统提示词（人设，含"公司制度/流程/福利"） | `db/engine.py:DEFAULT_SYSTEM_PROMPT` | `persona.system_prompt`（变量 `{bot_name}{user_name}{dept}{title}{date}`） |
| 语气/长度/Markdown/自适应 | `preference.style.{tone,length,markdown,lengthAdaptive,expression}` | 同路径，默认 `friendly/medium/true` |
| 欢迎语+推荐问题 / 兜底话术 | `preference.welcome.*` / `preference.fallback.text` | 同（默认空）；`fallback.transfer/contactUserIds` 删 |
| 拒答话术 | `safety.refuseText` | `safety.refuse_text` |
| 领域词表（判断要不要查库）/ 寒暄词表 | `agentic.companyTerms`（25 个公司词）/ `smalltalkTerms` | `agentic.domain_terms` / `smalltalk_terms`，默认 `[]` |
| 术语表/同义词 | `agentic.synonyms`（`["年假=年休假,带薪年假", …]`） | `agentic.synonyms` 默认 `[]`，示例进文档 |
| **检索阈值（最敏感）** | `settings.knowledge_grounding_min_score=0.48`、`Gate.of(cfg.retrieval, …)` 的 low/high | `retrieval.gate.{low,high,fuzzy}`，**必须在目标嵌入模型上重标定** |
| 检索参数 | `topK=8 / perQueryK=100 / maxPerDoc=5 / maxRounds=2 / maxQueries=4 / contextBudgetTokens=3000` | `agentic.*` 同名 |
| 上下文轮数 / 段空闲 | `preference.safety.contextRounds=6`、`segment_idle_minutes=15` | `kernel.context_rounds` / `kernel.segment_idle_minutes` |
| 工具轮数 / 预取 | `agent_max_tool_rounds=3`、`agent_kb_prefetch=True` | `agent.max_tool_rounds` / `agent.kb_prefetch` |
| 生成上限与守卫 | `genFirstTokenMs=8000 / genTotalMs=25000 / raceAfterMs=1200 / deadlineMs=30000 / maxLlmCalls=6 / maxTokens=24000 / maxConcurrent=24`；`HARD_LIMIT={False:1500,True:800}` | `agentic.limits.*` / `style.hard_limit` |
| 核对 | `answerVerify=post / answerJudge=strict / completenessCheck=True`、`CHECK_EVIDENCE=10`、`CHECK_TIMEOUT_MS=12000` | `verify.*` |
| 引用篇数 / 每句篇数 | `MAX_SOURCES=5`、`MAX_REFS_PER_SENTENCE=2` | `citations.max_sources` / `per_sentence` |
| 模型参数 | `model_config.params{...}`、`llm_temperature=0.2`、`llm_max_tokens=1500` | `models.*`（按 `ModelSpec`） |
| 缓存 / 轨迹 / 时区 | `answerCache=True`、`answerCacheTtlMin=360`、`traceSample=0.3`、`traceKeepDays=30`、`APP_TIMEZONE` | `cache.*` / `trace.*` / `kernel.timezone` |
| 并发与限流 | `max_concurrent_chats=32`、`dingtalk_answer_timeout_seconds=20` | `kernel.max_concurrent` / `kernel.queue_timeout_s` |
| 编排开关 | `agentic_enabled=False`、`HAVE_LANGGRAPH` | `agentic.enabled`；`MiniGraph` 保底保留 |

**提示词分层原则**：`kernel/prompt.py` 只放**机制性**模板（引用要求、资料围栏、情景提示、澄清、修订、HyDE、核对），不放任何公司名/制度名；`persona.system_prompt` 由部署方提供。`agentic/rules.py` 的正则（`DIRECTORY_PAT`/`COMPANY_ASSERT` 等）全部改为"从配置读词表再编译"，避免换语言/行业时重写代码。

## 5. 对外 API

### 5.1 现有行为（必须兼容）
**`POST /api/chat`** → `ChatResponse`
* 校验：`question.strip()` 为空或 >4000 → `422`（`{"detail":"question 不能为空" | "question 过长（上限 4000 字符），请精简后重试"}`）。`ChatRequest` 本身不校验长度，允许渠道构造空问题再翻译成渠道话术。
* 依赖：API Key（`verify_api_key`）+ 限流（`rate_limit`）。`debug=true` 且**非生产环境**才返回 `debug`。
* 重复消息（`reply is None`）→ `ChatResponse(answer="", session_id="-")`。`intent` 恒 `knowledge_qa`（`to_chat_response` 写死）。

**`POST /api/chat/stream`** → `text/event-stream`
```
event: status\ndata: searching\n\n
event: progress\ndata: 正在查找：《员工手册》等 2 份文件\n\n
event: reset\ndata: \n\n                       ← 换轮次时先清空
event: delta\ndata: 增量文本（换行转义为 \n）\n\n
event: meta\ndata: {"session_id":"…","citations":[…],"degraded":false,"usage":{…}}\n\n
event: done\ndata: [DONE]\n\n
```
* `meta` 的 JSON 只含 `session_id / citations / degraded / usage`（`model_dump_json(include=…)`）。
* 结束时会补发 `answer_with_citations()` 与已发送内容的差量为 `delta`（引用列表在正文后补齐）。
* `final` 值是 `Exception` 时在生成器内 `raise`（连接中断，不发 `done`）。响应头 `Cache-Control: no-cache`、`X-Accel-Buffering: no`。

**`POST /api/v1/chat/completions`** → OpenAI 兼容
* 入参 `messages[]`（`system` 被丢弃，外部 system prompt 不接管）、`user`、`session_id`、`channel`、`top_k`、`stream`；`question = 最后一条 user`；空 → `422 "messages 不能为空"` / `"未找到用户提问内容"`。
* `model` 字段**被忽略**（回显后台模型名，缺省 `hq-knowledge-assistant`）。
* 非流式：`{"id":"chatcmpl-<trace_id|session_id>","object":"chat.completion","choices":[{"message":{"role":"assistant","content":answer_with_citations()}}],"usage":{…},"citations":[…],"session_id":…,"degraded":…}`。
* 流式：首帧 `delta{"role":"assistant","content":""}`，随后只转 `delta`，末帧 `finish_reason:"stop"` + `data: [DONE]`；`id=chatcmpl-<ms hex>`。
* `GET /api/v1/models` → 单个模型，`owned_by:"hq-platform"`。

### 5.2 建议在 TsukiyoHub 保留 / 新增
| 端点 | 建议 | 理由 |
|---|---|---|
| `POST /v1/chat/completions` | **保留（主入口）** | 生态零改造；内部字段放响应扩展 |
| `POST /v1/chat`（非流式，`ChatResponse`） | **新增（= 原 `/api/chat`）** | 原生字段更好表达 `grounded/citations/degraded` |
| `POST /v1/chat/stream`（SSE：`delta/status/progress/reset/meta/done`） | **新增（= 原 `/api/chat/stream`）** | 事件协议是内核契约，值得稳定暴露 |
| `GET /v1/models` | 保留 | 兼容 |
| `POST /v1/feedback` | **新增** | 替代卡片点赞：`{message_id, value: up/down, reason?}`，只落 `feedback` 表 |
| `GET /v1/sessions/{id}/messages`、`POST /v1/knowledge/search` | 新增（可选） | 历史回放；纯检索调试（只返 `hits+citations`） |
| `GET /healthz`、`GET /metrics` | 新增 | 原散在 `HealthResponse` / `service/metrics.py` |
| 原 `/api/chat*` 路径 | 不保留 | 与 `/v1` 语义重复，避免两套 |

稳定契约建议：`X-Request-Id` 透传 → `trace_id`；并发槽满 → `429` + `Retry-After`（原实现返回正文"当前咨询人数较多"）；DB/模型未就绪 → `503` + `degraded_reason`。

## 6. 依赖与风险

### 6.1 耦合点（抽取时最容易断的 8 处）
1. **`ChatAgent` 的 4 个下划线方法被 `AgenticRunner` 与所有节点直接调用**（`_new_result/_call/_usage/_finish` + `router/tools/settings/config/build_system_prompt`）。这是事实上的内部接口，必须显式写进 `ports`（`KernelAgentPort`），否则 agentic 无法独立移植。
2. **`ToolContext.hits` 是全局编号总线**：普通链路、`prefetch`、RAG 子图、`_finish` 都靠它对齐 `[n]`。任何"改成本地编号"的重构都会破坏引用与命中率统计。
3. **`ConfigStore` 是"上帝配置"**：`model_config/preference/quota/robot_state/agentic/harness/safety/memory/retrieval` 九个命名空间混在一张 `robot_config(key, value JSONB)` 表；`preference()` 会与默认值深度合并。拆分时必须保留"缺字段用默认值补齐"，否则旧配置 KeyError。
4. **`ConversationService` 的类属性注入**（`attachments/send_files/group_kbs`）与 `container` 双向赋值（`send_files.attachments = conversation.attachments`）。改 `pre_hooks` 后注意：这些钩子**在准入检查之前**执行且不占额度。
5. **`Settings` 与 DB 配置重复**：`llm_temperature/llm_max_tokens/knowledge_grounding_min_score/agent_max_tool_rounds/max_concurrent_chats` 在 env，其余在 DB。迁移要定优先级：`env（部署） > DB（运行时） > 代码默认`。
6. **全局单例**：`limits.BREAKER`/`TRIGGERS`（进程内）、`node._GEN_POOL`（64 线程）、`rag.multi_query.pool()`、`agentic.runner.FORCED_MODE`（contextvar）。多副本时熔断与触发统计不共享——通用服务要么明确"单进程语义"，要么做成端口。
7. **`hot` 层键格式**（`summary_key(segment_id)`、`sessions.claim_message(msg_id)`、`agentic:ans:<sha1>`）是隐式契约，跨版本升级会让旧键失效。
8. **提示词与代码互相引用**：`llm/fake.py` 用中文提示词特征串打桩（`"「问题理解」模块"`、`"【核对任务】"`）；`rules.py` 的规则假设中文提问。改模板会悄悄破坏测试与规则命中。

### 6.2 必须由 Lead 先行定义的契约（阻塞项）
1. **`domain` 定稿**：`ChatRequest / ChatResponse / Citation / Intent / Chunk / ScoredChunk / AgentResult / UsageRecord / InboundMessage / ConversationReply / ChatEvent`。全下游依赖，改动成本最高。
2. **`ports` 定稿**：
```python
class LLMPort(Protocol):
    def resolve(self, model_id: str) -> ResolvedModel: ...
    def primary_id(self, cfg: dict | None = None) -> str | None: ...
    def fallback_id(self, cfg: dict | None = None) -> str | None: ...
    def chat_model(self, resolved: ResolvedModel, params: dict | None = None) -> Any: ...
class RetrieverPort(Protocol):
    def retrieve(self, request: RetrievalRequest) -> RetrievalResult: ...
    def speculate(self, query: str, filters: Any, per_query_k: int) -> dict: ...
class IdentityPort(Protocol):
    def ensure(self, user_id: str, name: str = "", channel: str = "api") -> UserProfile: ...
class SessionStorePort(Protocol):        # 内核只调这几个方法
    def open_turn(self, ...) -> TurnRef; def finish_turn(self, ...) -> int
    def context_window(self, ...) -> list[dict[str, str]]; def add_usages(self, ...) -> None
class ConfigPort(Protocol):              # get(key)->dict / set / invalidate / on_change
class TraceSinkPort(Protocol):           # record(row: dict) -> None
class ClockPort(Protocol):               # now() / monotonic() —— 便于测"段空闲/额度"
```
3. **`ChatEvent` 枚举 + `delta` 是全文**（写进 docstring 与单测，最容易误实现）。
4. **`KernelConfig` 键命名空间与默认值表**（§4.2 即初稿）+ env/DB/默认值优先级。
5. **引用编号不变量**（全局 `hits` → `tidy_refs` → `renumber` → `merge_pages` → `related`）与两个上限的可配置化。
6. **降级语义**：`degraded_reason` 取值集合（`no_hit / all_models_failed:* / busy / blocked / db_unavailable:*`）必须冻结，前端按它决定是否展示引用。

### 6.3 实现顺序（每步可独立验收）
| 阶段 | 内容 | 验收 |
|---|---|---|
| P0（0.5d） | `domain/` + `ports/` + `KernelConfig` 骨架 | 纯 pydantic，无第三方 import；pytest 过 |
| P1（2d） | `kernel/{safety,citations,style,grounding}.py`（纯函数）+ `agentic/limits.py` + `agentic/rules.py`（去业务分支） | 脱敏/还原/holdback；编号/页码/否定句；复读/截断 |
| P2（2.5d） | `llm/router.py`（`ModelSpec` 配置化）+ `harness/{base,registry,knowledge}.py` + `llm/fake.py` + 内存 store | fake 模型端到端答一次带引用 |
| P3（2d） | `kernel/agent.py`（`_run_plain` → `_run_agent` → `_finish`） | 直答/查库/全失败兜底三条路径 |
| P4（1.5d） | `kernel/conversation.py`（内存 store + `pre_hooks`） | 去重、段窗口、串行锁、SSE relay |
| P5（2.5d） | `agentic/`（图 + 5 节点 + runner，先 `mini=True`） | live 路径跑通；`MiniGraph` 与 LangGraph 结果一致 |
| P6（1d） | `adapters/http/` 三端点 + 事件映射 + bootstrap | SSE 帧与原实现逐字节对齐（含 `reset`） |
| P7（持续） | 插件 `directory`/`web`/`files`/`memory` | 注册即生效，卸下不影响内核 |

### 6.4 主要风险
| 风险 | 影响 | 缓解 |
|---|---|---|
| **阈值不可移植**：`0.48` 与 `Gate` low/high 在原嵌入模型（text-embedding-v3/1024 维）上标定 | 换模型后全"无命中"或全"模糊可答" | P2 后立刻用 30~50 条真实问题重标定，标定脚本进仓 |
| **中文硬编码规则**：`rules.py` 十多个正则 + `_ASPECT` 词表 | 换语言/行业即失效 | 词表配置化；保留"规则没结论交模型"兜底 |
| **DB 方言绑死**：`JSONB`、`flags["card"].astext`、`on_conflict_do_update` | 无法切 SQLite/MySQL | 内核只依赖 `SessionStorePort`，PG 实现放 `store/pg/` |
| **`_finish` 过重**（~60 行混了引用/链接/按钮/联系人/网页来源/脱敏） | 单点难测难裁 | 拆 `pick_citations → renumber → enrich` 三个纯函数，`_finish` 只编排 |
| **流式与核对冲突**：已推送正文后核对删句，只能"整卡替换" | 前端必须支持替换 | 保留 `reset`；文档写明 `delta` 是全文 |
| **并发与成本失控**：`maxConcurrent=24`、`maxLlmCalls=6`、竞速双发 | 费用翻倍 | 保留 `Budget`+`TRIGGERS`；默认 `race=false`、`hyde=false` |
| **LangChain 绑死**：`BaseChatModel`/`bind_tools`/消息类型贯穿内核 | 换 SDK 成本高 | 允许，但消息构造收敛在 `kernel/prompt.py`，换 SDK 只改一处 |
| **`supports_tools` 写库副作用**：一次 400 永久标记不支持工具 | 之后永远走 `_run_plain` | 改带 TTL 的进程内缓存 + 显式配置覆盖 |
| **热层/摘要键无版本** | 升级后读出错误结构、上下文串味 | 键加 schema 版本前缀（`v2:`） |
## 附：迁移检查清单（DoD）
- [ ] `domain`/`ports` 无反向依赖（import-linter 或 AST 检查脚本卡住）
- [ ] 内核代码不出现 `dingtalk|钉钉|部门|同事|员工|群|办公|发文件|评价|qwenwork`（注释/示例除外）：`grep -ri` 门禁
- [ ] 无 DB 也能起服务：`MemoryStore` 下"直答/查库/全失败"三路径全通
- [ ] `delta` 全文语义 + `reset` 事件有单测；SSE 帧与原实现逐字节对比
- [ ] 引用编号不变量有单测：`[1][1]→[1]`、`[2·第2页][2·第37页]→[2·第2、37页]`、否定句不挂引用、补足 5 篇且 `cited=false`
- [ ] 阈值标定报告（新旧模型命中率对比）落在 `docs/design/` 下；所有提示词可从配置覆盖，`llm/fake.py` 不再依赖中文特征串
