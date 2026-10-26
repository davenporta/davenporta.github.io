# Sourced by wrapper scripts. User watch history lives in ~/.config/watch-history-mcp/store.json (0600).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export WATCH_HISTORY_CONFIG_DIR="${WATCH_HISTORY_CONFIG_DIR:-$HOME/.config/watch-history-mcp}"
if [ -f "$WATCH_HISTORY_CONFIG_DIR/env" ]; then
  # shellcheck disable=SC1090
  . "$WATCH_HISTORY_CONFIG_DIR/env"
fi
