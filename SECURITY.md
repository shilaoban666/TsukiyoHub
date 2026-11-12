# Security policy

## Supported versions

This project is at `v0.1.x`. Security fixes land on `main` and are tagged as patch releases.

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

1. Open a private report via GitHub: **Security → Advisories → New draft advisory**
   (<https://github.com/shilaoban666/TsukiyoHub/security/advisories/new>), or
2. If that is not available, email the maintainer listed on the GitHub profile.

Include:

- affected component (`server/`, `gateway/`, `web/`, `deploy/`),
- version / commit sha,
- a minimal reproduction (request + config, **with secrets removed**),
- impact you believe it has.

You can expect an initial response within 7 days. Once fixed, a patched release and a
GitHub Security Advisory will be published; credit is given unless you prefer otherwise.

## Scope notes for self-hosters

- The kernel ships with **no authentication by default** (`API_KEY` empty). Set `API_KEY` before
  exposing it beyond localhost; `/metrics`, `/healthz` and `/readyz` are intentionally public.
- Gateway admin endpoints (`:8082/admin/api/*`) are restricted to localhost when `admin_token` is
  empty. Set `admin_token` for any non-local deployment.
- Demo mode (`LLM_PROVIDER=fake`, `EMBEDDING_PROVIDER=hash`) is for local evaluation only.
- Secrets are never stored in the repo; `gateway/config.json` and `.env` are git-ignored.
  Gateway config supports `${ENV_VAR}` placeholders, prefer that over committing keys.