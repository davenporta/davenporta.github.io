#!/usr/bin/env bash
# Ephemeral proxy status/cleanup helper.
# Note: tesla-mcp no longer runs tesla-http-proxy as a 24/7 background daemon.
# Instead, FleetClient._ephemeral_proxy() spawns tesla-http-proxy on 127.0.0.1:4443
# only during a confirmed signed command and terminates it immediately in a finally block.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=env.sh
source "$ROOT/scripts/env.sh"

STATE="${XDG_STATE_HOME:-$HOME/.local/state}/tesla-mcp"
PIDFILE="$STATE/proxy-supervisor.pid"

stop_proxy() {
  if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE"
  fi
  pkill -f "tesla-http-proxy.*-port ${TESLA_PROXY_PORT:-4443}" 2>/dev/null || true
}

case "${1:-status}" in
  stop)
    stop_proxy
    echo "stopped"
    ;;
  status)
    if pgrep -f "tesla-http-proxy.*-port ${TESLA_PROXY_PORT:-4443}" >/dev/null 2>&1; then
      echo "running (ephemeral)"
    else
      echo "idle (spawned ephemerally on confirmed command only)"
    fi
    ;;
  *)
    echo "Usage: $0 {status|stop} (proxy is spawned ephemerally per confirmed command by tesla-mcp)" >&2
    exit 1
    ;;
esac
