#!/usr/bin/env bash
# Read-only TMDB JSON CLI for scheduled jobs or shell checks.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=env.sh
source "$ROOT/scripts/env.sh"

if [ -z "${TMDB_API_KEY:-}" ] && [ -z "${TMDB_READ_TOKEN:-}" ]; then
  echo '{"ok": false, "configured": false, "error": "TMDB_API_KEY / TMDB_READ_TOKEN not set"}'
  exit 2
fi
exec "$ROOT/.venv/bin/python" -m tmdb_mcp.cli "$@"
