# Sourced by wrapper scripts. Never contains secrets; secrets live in ~/.config/tesla-mcp/tokens.json (0600).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "${TESLA_CONFIG_DIR:-$HOME/.config/tesla-mcp}/env" ]; then
  # shellcheck disable=SC1090
  . "${TESLA_CONFIG_DIR:-$HOME/.config/tesla-mcp}/env"
fi
export TESLA_DOMAIN="${TESLA_DOMAIN:-example.github.io}"
export TESLA_REDIRECT_URI="${TESLA_REDIRECT_URI:-https://${TESLA_DOMAIN}/callback}"
export TESLA_PROXY_PORT="${TESLA_PROXY_PORT:-4443}"
export TESLA_PROXY_URL="${TESLA_PROXY_URL:-https://127.0.0.1:${TESLA_PROXY_PORT}}"
export TESLA_PROXY_CA_CERT="${TESLA_PROXY_CA_CERT:-$ROOT/proxy-config/tls-cert.pem}"
export TESLA_PROXY_TLS_KEY="${TESLA_PROXY_TLS_KEY:-$ROOT/proxy-config/tls-key.pem}"
export TESLA_FLEET_KEY_FILE="${TESLA_FLEET_KEY_FILE:-$ROOT/keys/private-key.pem}"
export PATH="$HOME/.local/bin:$PATH"
