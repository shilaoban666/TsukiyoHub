# Publishing checklist (GitHub)

Everything that still says `YOUR_NAME`, plus the release, demo-media and repository-splitting steps.

> **This repository**: `shilaoban666/TsukiyoHub` — the placeholder was already replaced in both READMEs;
> keep this file as a checklist for future renames / forks.
Run through this once before the repository goes public.

## 1. Replace placeholders

`README.en.md` is the only file that carries placeholders:

```bash
# macOS / Linux
sed -i.bak 's/YOUR_NAME/shilaoban666/g' README.en.md && rm README.en.md.bak

# Windows PowerShell
(Get-Content README.en.md -Raw) -replace 'YOUR_NAME','shilaoban666' |
  Set-Content README.en.md -NoNewline -Encoding utf8
```

Then verify nothing is left:

```bash
grep -rn "YOUR_NAME" . --exclude-dir=.git --exclude-dir=node_modules || echo "clean"
```

Also double-check that every relative link in both READMEs resolves in the published tree (the CI `configs`
job does not check markdown links):

```bash
python -c "import re,pathlib as P; r=P.Path('.'); m=[l for md in ('README.md','README.en.md') for l in re.findall(r'\]\(([^)#:]+)\)',(r/md).read_text(encoding='utf-8')) if not l.startswith('http') and not (r/l).exists()]; print('missing:', m or 'none')"
```

## 2. Repository settings

**About → Description** (keep it under ~120 characters, it is also the search snippet):

> LLM routing gateway + agent/RAG kernel: OpenAI-compatible streaming, hybrid retrieval, skills & MCP,
> Prometheus/Grafana, Vue 3 workbench.

**Website**: leave empty, or point at your demo deployment.

**Topics** (max 20, use all the relevant ones — GitHub topic pages are free distribution):

```
llm-gateway  llm-router  openai-compatible  ai-infrastructure  agent  rag  hybrid-search
bm25  rrf  prompt-engineering  fastapi  golang  vue3  typescript  prometheus  grafana
observability  sse  self-hosted  mit-license
```

**Features**: enable Issues, Discussions and "Require a pull request before merging" on `main`; keep Actions
permissions at *read repository contents* (`ci.yml` already declares `permissions: contents: read`).

## 3. Demo media

- `docs/assets/demo.gif` is referenced at the top of `README.en.md`. **Keep it under 10 MB** — GitHub stops
  animating larger GIFs inline (current file: ~1.7 MB, 1280 px wide, ~20 s loop).
- Regenerate with the script under `scripts/demo/` against a local instance (`LLM_PROVIDER=fake`,
  `EMBEDDING_PROVIDER=hash`) so no real API key or customer data appears in a frame.
- Per-page screenshots already live in `docs/assets/` (`chat.png`, `knowledge.png`, `routing.png`, `alerts.png`).
- Set **Settings → Social preview** to a 1280 × 640 PNG (the chat page screenshot works well); it is the card
  shown when the repository is shared on social platforms.

Recording rules of thumb: crop browser chrome, hide `.env` values, show no real user names, and prefer a short loop of *type a question → tokens stream → citation panel opens* over a full tour.

## 4. First release

```bash
git tag -a v0.1.0 -m "v0.1.0 — first public drop"
git push origin v0.1.0
gh release create v0.1.0 --title "v0.1.0" --generate-notes   # then paste the summary below into the notes
```

Release notes skeleton (keep it factual, list the verified numbers):

- Kernel: chat + tool loop + SSE, RAG with dual backends, skills/`SKILL.md`/MCP, memory.
- Gateway: 8 routing strategies, fallback, breaker, limits/budget, hot reload, alerts, metrics.
- Verification: 278 pytest cases, 9 Go packages with tests, 15 gateway smoke checks, 19 recording rules,
  16 alert rules, 5-row / 42-panel Grafana dashboard.
- Known limits: no RBAC/multi-tenancy, per-process quotas, single agent loop (see "Status and roadmap").
- Optional one-file binary: attach `tsukiyo-gateway-linux-amd64` built with `CGO_ENABLED=0 go build -trimpath`.

## 5. Discussions

Enable **Discussions**, then create categories `Announcements` (maintainers only), `Q&A`, `Ideas`,
`Show and tell`. Pin one welcome post linking `docs/architecture.md`, `docs/api.md` and the "Status and roadmap"
section so newcomers can tell implemented from planned features. Route questions to Discussions and keep Issues
for reproducible bugs and scoped PRs.

## 6. Splitting the gateway into its own repository

`gateway/` is a standalone Go module (`tsukiyohub/gateway`) with no Python dependencies, so it can be published separately and consumed as a container image or a `go install` target.

```bash
# from the monorepo root, on a clean branch
git subtree split --prefix=gateway -b tsukiyo-gateway
git push <new-remote> tsukiyo-gateway:main

# in the new repository: rewrite the module path, then rebuild
go mod edit -module github.com/YOUR_NAME/tsukiyo-gateway
git grep -l 'tsukiyohub/gateway' -- '*.go' | xargs sed -i 's#tsukiyohub/gateway#github.com/YOUR_NAME/tsukiyo-gateway#g'
go build ./... && go test ./... -count=1
```

Add a `gateway.yml` workflow (vet + test + `docker build -f Dockerfile .`) to the new repo — `ci.yml` in the
monorepo keeps covering the `gateway/` directory, so both stay green.

After the split you can either (a) keep `gateway/` in the monorepo as the source of truth and publish releases
from the split repo, or (b) delete `gateway/` here and consume the image via `docker compose`. Option (a) is less
work but needs `git subtree push` on every release; avoid a git submodule unless you accept a worse first-clone
experience.

## 7. Pre-flight checklist

- [ ] `grep -rn "YOUR_NAME\|sk-[A-Za-z0-9]\{20,\}" . --exclude-dir=.git --exclude-dir=node_modules` is clean.
- [ ] `.env`, `data/`, `logs/`, `node_modules/`, `dist/`, `gateway/bin/` are all git-ignored.
- [ ] CI is green on `main` (server 3.11/3.12, gateway 1.22/1.23, web build, image build, config validation).
- [ ] `README.md` and `README.en.md` cross-link to each other at the top.
- [ ] `docs/assets/demo.gif` < 10 MB and `docs/assets/*.png` load from a fresh clone.
- [ ] Issue templates and the PR template exist under `.github/`.
- [ ] LICENSE (MIT) present; dependencies are compatible with it.
- [ ] "Status and roadmap" still honest — anything unimplemented must stay in the *not implemented* list.