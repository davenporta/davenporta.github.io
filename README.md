# GitHub Pages & Self-Hosted MCP Servers

GitHub Pages site (hosts the Tesla Fleet API public key at `/.well-known/appspecific/`) plus two independent,
self-hosted MCP servers:

| directory | what |
|---|---|
| [`tesla-mcp/`](tesla-mcp/) | Tesla Fleet API server: maintenance reads, confirm-gated climate + send-destination, safety guards |
| [`icloud-calendar-mcp/`](icloud-calendar-mcp/) | Strictly read-only Apple iCloud calendar server (CalDAV) |

They don't import each other; each has its own `pyproject.toml`, venv, tests and `scripts/run_mcp.sh`.

**No secrets or personal data in git.** Credentials and local configuration come only from the environment or `~/.config/`. `scripts/pre-commit-secret-guard.sh` is the
pre-commit hook (install: `cp scripts/pre-commit-secret-guard.sh .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit`).
