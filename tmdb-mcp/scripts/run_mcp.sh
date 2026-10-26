#!/usr/bin/env bash
# Launch the read-only TMDB stdio MCP server.
# TMDB_API_KEY (or TMDB_READ_TOKEN) must be in the environment or ~/.config/tmdb-mcp/env (0600).
# stdout is reserved for MCP JSON-RPC, so all error messages go to stderr.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=env.sh
source "$ROOT/scripts/env.sh"

if [ -z "${TMDB_API_KEY:-}" ] && [ -z "${TMDB_READ_TOKEN:-}" ]; then
  echo "tmdb-mcp: missing environment variable: TMDB_API_KEY (or TMDB_READ_TOKEN) in environment or ~/.config/tmdb-mcp/env" >&2
  exit 1
fi
[ -x "$ROOT/.venv/bin/tmdb-mcp" ] || { echo "tmdb-mcp: venv missing; run: python3 -m venv $ROOT/.venv && $ROOT/.venv/bin/pip install -e $ROOT" >&2; exit 1; }
exec "$ROOT/.venv/bin/tmdb-mcp" "$@"
