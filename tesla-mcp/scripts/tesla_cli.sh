#!/usr/bin/env bash
# Shell entry point for scheduled jobs: same env as run_mcp.sh, prints JSON. Read-only checks + previews only.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$ROOT/scripts/env.sh"
[ -n "${TESLA_CLIENT_ID:-}" ] || { echo '{"error":"TESLA_CLIENT_ID is not set"}'; exit 2; }
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/tesla_cli.py" "$@"
