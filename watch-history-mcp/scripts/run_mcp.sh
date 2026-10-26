#!/usr/bin/env bash
# Launch the watch-history stdio MCP server.
# stdout is reserved for MCP JSON-RPC, so all messages go to stderr.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=env.sh
source "$ROOT/scripts/env.sh"

[ -x "$ROOT/.venv/bin/watch-history-mcp" ] || { echo "watch-history-mcp: venv missing; run: python3 -m venv $ROOT/.venv && $ROOT/.venv/bin/pip install -e $ROOT" >&2; exit 1; }
exec "$ROOT/.venv/bin/watch-history-mcp" "$@"
