#!/usr/bin/env bash
# Read-only iCloud calendar JSON CLI for scheduled jobs. Needs ICLOUD_APPLE_ID / ICLOUD_APP_PASSWORD in env.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -z "${ICLOUD_APPLE_ID:-}" ] || [ -z "${ICLOUD_APP_PASSWORD:-}" ]; then
  echo '{"ok": false, "configured": false, "error": "ICLOUD_APPLE_ID / ICLOUD_APP_PASSWORD not set"}'; exit 2
fi
exec "$ROOT/.venv/bin/python" -m icloud_calendar_mcp.cli "$@"
