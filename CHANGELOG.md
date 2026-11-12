# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [0.1.0] — 2026-11-12

First public release: the business-agnostic extraction of a DingTalk knowledge-bot backend and its
admin console into a reusable **LLM gateway + agent/RAG kernel**, with observability and scripted demo.

### Added

- **Server (`server/tsukiyohub`)** — FastAPI service with a port/adapter architecture.
  - `kernel/chat`: session orchestration, model tool loop, SSE streaming, primary/fallback degradation,
    sentence-level grounding checks, citation renumbering.
  - `kernel/skills`: tool protocol + registry, `SKILL.md` skills (discover / enable / `use_skill`),
    MCP bridge (stdio/SSE), built-in `knowledge_search`, `web_fetch`, `web_search`, `calculator`.
  - `kernel/rag`: markdown/pdf/docx/xlsx/pptx/csv/json parsing, sentence-aware chunking, cached embeddings,
    hybrid retrieval (vector + BM25 → weighted RRF), evidence gate, per-sentence verification.
  - `kernel/memory`: fact extraction/recall with sensitive-data policy, long-conversation summarisation.
  - `api/`: `/api/chat`, `/api/chat/stream` (SSE), `/api/v1/chat/completions` (OpenAI-compatible),
    sessions, skills, knowledge (upload/ingest/search/reindex), memory, system config, gateway proxy.
  - Zero-dependency defaults: SQLite + in-process retrieval + fake LLM + hash keyword mode.
- **Gateway (`gateway/`)** — Go service, **standard library only**.
  - OpenAI-compatible routing with 8 strategies: priority, weighted, round-robin, least-latency, cost,
    canary, sticky, capability.
  - Failover, circuit breaking, per-client RPM/TPM limits, monthly budgets, cost accounting.
  - Prometheus metrics, request tracing (attempt-level), admin API with hot config reload.
  - Built-in alert engine (pending → firing → resolved, cooldown, ack) with log / webhook / DingTalk / Slack.
- **Web (`web/`)** — Vue 3 + TypeScript workbench: chat, knowledge, skills, routing, alerts, settings.
- **Observability** — hand-written Prometheus exposition for both services, 19 recording rules,
  16 alert rules, 5-row / 42-panel Grafana dashboard, SLO burn-rate alerts.
- **Tooling & docs** — scripted demo recorder (`scripts/demo/`), CI (Python ×2, Go ×2, Node, Docker,
  config validation), 18 docs incl. extraction mapping and Prometheus reference tables.

### Known limitations

- Rate limits / budgets / circuit breakers are per-process (not shared across replicas).
- Admin API has no RBAC yet; `API_KEY` is a single shared credential.
- Agent orchestration is a single tool-using loop (no multi-agent planning).
- Tracing is in-memory (bounded ring buffer), not persisted.

[0.1.0]: https://github.com/shilaoban666/TsukiyoHub/releases/tag/v0.1.0