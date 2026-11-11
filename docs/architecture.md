# 架构

TsukiyoHub 的架构只有一句话：**内核只依赖端口，任何实现都能替换，任何前端都能接入。**

```mermaid
flowchart TB
    subgraph Clients["任何前端"]
        WEB[内置聊天工作台<br/>Vue 3]
        APP[自有 App / 桌面端]
        IDE[IDE 插件 / CLI]
        PLAT[Agent 平台<br/>Dify / FastGPT]
    end

    subgraph API["API 层（FastAPI）"]
        CHAT["/api/chat<br/>/api/chat/stream (SSE)"]
        OAI["/api/v1/chat/completions<br/>OpenAI 兼容"]
        SESS["/api/sessions"]
        KB["/api/knowledge"]
        SK["/api/skills"]
        SYS["/api/system<br/>/healthz /readyz"]
    end

    subgraph Kernel["内核（不 import adapters）"]
        subgraph ChatK["聊天内核"]
            CONV[ConversationService<br/>会话 / 历史 / 记忆注入]
            AG[ChatAgent<br/>模型工具循环 / 流式 / 降级]
            CIT[citations<br/>引用编号与来源]
            VER[answer judge<br/>逐句核对]
        end
        subgraph SkillK["技能内核"]
            REG[SkillRegistry]
            BUILTIN[内置技能<br/>knowledge_search / web_fetch / web_search / calculator]
            DOCS[SKILL.md 技能]
            MCP[MCP 工具]
        end
        subgraph RagK["RAG 内核"]
            ING[入库<br/>解析 → 切块 → 向量化 → 索引]
            RET[检索<br/>向量 + BM25 → RRF → 重排]
            GATE[证据闸门<br/>分数 + 词面重叠]
        end
        MEM[记忆内核<br/>事实召回 / 长对话摘要]
    end

    subgraph Ports["端口"]
        P1[LLMClient / LLMRouter]
        P2[EmbeddingProvider]
        P3[VectorStore]
        P4[SessionStore / MemoryStore / KVStore]
        P5[BlobStore / HotStore / Reranker]
    end

    subgraph Adapters["适配器"]
        A1[OpenAI 兼容模型<br/>+ 演示模型]
        A2[OpenAI / DashScope / Hash 向量化]
        A3[Milvus / 内存混合检索]
        A4[SQLAlchemy<br/>SQLite / PostgreSQL]
        A5[本地文件 / Redis / DashScope 重排]
    end

    WEB --> CHAT
    APP --> CHAT
    IDE --> CHAT
    PLAT --> OAI
    CHAT --> CONV
    OAI --> CONV
    SESS --> P4
    KB --> ING
    KB --> RET
    SK --> REG
    SYS --> Ports

    CONV --> AG
    CONV --> MEM
    AG --> REG
    AG --> RET
    AG --> CIT
    AG --> VER
    REG --> BUILTIN
    REG --> DOCS
    REG --> MCP
    BUILTIN --> RET
    RET --> GATE
    ING --> P2
    ING --> P3
    RET --> P3
    AG --> P1
    CONV --> P4
    MEM --> P4

    P1 -.-> A1
    P2 -.-> A2
    P3 -.-> A3
    P4 -.-> A4
    P5 -.-> A5

    classDef kernel fill:#1f2937,stroke:#60a5fa,color:#e5e7eb
    classDef port fill:#111827,stroke:#f59e0b,color:#fde68a
    classDef adapter fill:#0f172a,stroke:#34d399,color:#a7f3d0
    class CONV,AG,CIT,VER,REG,BUILTIN,DOCS,MCP,ING,RET,GATE,MEM kernel
    class P1,P2,P3,P4,P5 port
    class A1,A2,A3,A4,A5 adapter
```

## 一次问答的时序

```mermaid
sequenceDiagram
    autonumber
    participant F as 前端
    participant API as API 层
    participant C as ConversationService
    participant A as ChatAgent
    participant S as SkillRegistry
    participant R as 检索
    participant L as 模型

    F->>API: POST /api/chat/stream {question, session_id}
    API->>C: answer(request, on_event)
    C->>C: 解析/新建会话，取历史，召回记忆
    C->>A: run(question, history, user_ctx, on_event)
    A-->>F: status: thinking
    A->>L: chat(messages, tools)
    L-->>A: tool_calls: knowledge_search
    A-->>F: status: searching / tool: knowledge_search
    A->>S: call("knowledge_search", {query})
    S->>R: retrieve(query)
    R-->>S: ScoredChunk[]（编号 1..n）
    S-->>A: 证据文本（含 [n] 与来源行）
    A-->>F: progress: 正在查找《…》等 N 份资料
    A->>L: chat(messages + tool 结果)
    L-->>A: 回答（正文带 [n]）
    loop 流式
        L-->>A: 文本增量
        A-->>F: delta: 当前累积全文
    end
    A->>A: 证据闸门 → 逐句核对 → 引用编号
    A-->>C: AgentResult
    C->>C: 落库（消息 / 引用 / 用量 / 耗时）
    C-->>F: meta {citations, grounded, usage}
    C-->>F: done [DONE]
```

## 为什么这样分层

| 层 | 依赖谁 | 被谁依赖 | 换掉它的代价 |
|---|---|---|---|
| `domain.py` | pydantic | 所有层 | 高（改契约） |
| `ports/` | domain | 内核 + adapters | 高（改实现） |
| `kernel/` | domain + ports | api + container | 中 |
| `adapters/` | domain + ports + 第三方 SDK | container | 低（只改 container 一行） |
| `api/` | domain + container | 前端 | 低（前端契约不变即可） |
| `web/` | HTTP 契约 | — | 极低（可整体替换） |

关键约束：**内核不 import adapters**。这条约束保证「换模型 / 换向量库」不会牵动业务逻辑；
LangChain 只出现在 `adapters/llm_openai.py`，pymilvus 只出现在 `adapters/vector_milvus.py`。

## 降级矩阵

系统在任何依赖故障时都尽量给出「可用的回答」，而不是报错：

| 故障 | 行为 |
|---|---|
| 没配 `LLM_API_KEY` | 使用演示模型（`FakeLLMClient`），仍能演示工具调用与流式 |
| 主模型调用失败 | 自动切备用模型；都失败 → 直接给资料原文摘录（`degraded=true`） |
| 模型不支持工具调用 | 自动退回「先检索、再作答」（`mode=plain`） |
| 向量化失败 | 该文档入库失败并返回原因（可重试）；检索退化为纯 BM25 |
| Milvus 连不上 | 自动退回内存检索后端（重启后需重建索引） |
| Redis 不可用 | 直接读数据库 |
| 知识库无命中 | 证据闸门判定「不知道」，不编造（`grounded=false`） |
| 逐句核对超时 | 保留原答案，记 warning（`answer_judge=lenient` 时只告警） |

## 数据流：文档怎么变成可检索的证据

```
文件（md/txt/pdf/docx/xlsx/pptx/csv/json）
  │ DocumentParser：按扩展名分派，编码探测，失败给可读错误
  ▼
Document（meta + 纯文本 + content_hash）
  │ TextChunker：按段落/句子边界切，700 字 / 120 重叠，chunk_id = doc_id#ordinal
  ▼
Chunk[]
  │ CachedEmbeddings：批量向量化（带缓存，失败重试）
  ▼
IndexRow（chunk + vector）
  │ VectorStore.upsert：先写新切块，再删旧切块（无检索空窗）
  ▼
可检索；清单落盘在 data/index/kb_index_docs.json + kb_index_chunks.jsonl
```

## 目录与责任

| 目录 | 责任 | 不该出现的东西 |
|---|---|---|
| `kernel/chat` | 对话编排、提示词、引用 | HTTP、数据库、具体供应商 SDK |
| `kernel/skills` | 技能协议、注册、发现、MCP | 具体业务动作（请假、报销…） |
| `kernel/rag` | 解析、切块、检索、核对 | 上传接口、前端字段 |
| `kernel/memory` | 事实记忆、摘要 | 组织架构、员工画像 |
| `adapters` | 第三方实现 | 业务判断 |
| `api` | 协议转换、鉴权、限流 | 复杂业务逻辑（应放内核） |
| `web` | UI | 直接拼后端 URL（统一走 `src/api/*`） |