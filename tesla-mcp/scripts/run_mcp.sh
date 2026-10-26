#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=env.sh
source "$ROOT/scripts/env.sh"

# Ensure no persistent signing proxy daemon is left running; FleetClient spawns
# tesla-http-proxy ephemerally per confirmed command and terminates it immediately.
"$ROOT/scripts/run_proxy.sh" stop >/dev/null 2>&1 || true

if [ -f "$ROOT/keys/private-key.pem" ] && [ ! -f "$TESLA_PROXY_CA_CERT" ]; then
  "$ROOT/scripts/gen_proxy_tls.sh" >/dev/null 2>&1 || true
fi

exec "$ROOT/.venv/bin/tesla-mcp"
