## What does this PR do?

<!-- One paragraph. Link the issue: Closes #123 -->

## Which layer?

- [ ] `server/` (chat / skills / rag kernel)
- [ ] `gateway/` (Go routing gateway)
- [ ] `web/` (Vue workbench)
- [ ] docs / deploy / observability

## Checklist

- [ ] `pytest tests -q` passes (server)
- [ ] `cd gateway && go vet ./... && go test ./... -count=1` passes
- [ ] `cd web && npm run build` passes
- [ ] `ruff check server tests` passes
- [ ] No secrets / API keys committed
- [ ] Docs updated when behavior or config changed

## Screenshots / logs (optional)

<!-- For UI changes, a before/after screenshot helps a lot. -->