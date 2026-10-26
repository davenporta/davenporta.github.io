#!/usr/bin/env bash
# Local Apple TV watch history & watchlist JSON CLI.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=env.sh
source "$ROOT/scripts/env.sh"

exec "$ROOT/.venv/bin/python" -m watch_history_mcp.cli "$@"
