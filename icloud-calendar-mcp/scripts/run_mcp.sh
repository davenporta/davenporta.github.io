#!/usr/bin/env bash
# Launch the read-only iCloud calendar stdio MCP server.
# ICLOUD_APPLE_ID and ICLOUD_APP_PASSWORD must already be in the environment; this script never prints or
# writes them. stdout is reserved for MCP JSON-RPC, so all messages go to stderr.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
missing=()
[ -n "${ICLOUD_APPLE_ID:-}" ] || missing+=(ICLOUD_APPLE_ID)
[ -n "${ICLOUD_APP_PASSWORD:-}" ] || missing+=(ICLOUD_APP_PASSWORD)
if [ ${#missing[@]} -gt 0 ]; then
  echo "icloud-calendar-mcp: missing environment variable(s): ${missing[*]} (Apple ID email + app-specific password)" >&2
  exit 1
fi
[ -x "$ROOT/.venv/bin/icloud-calendar-mcp" ] || { echo "icloud-calendar-mcp: venv missing; run: python3 -m venv $ROOT/.venv && $ROOT/.venv/bin/pip install -e $ROOT" >&2; exit 1; }
exec "$ROOT/.venv/bin/icloud-calendar-mcp" "$@"
