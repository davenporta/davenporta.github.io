# Sourced by wrapper scripts. Never contains secrets; secrets live in ~/.config/tmdb-mcp/env (0600) or environment.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "${TMDB_CONFIG_DIR:-$HOME/.config/tmdb-mcp}/env" ]; then
  # shellcheck disable=SC1090
  . "${TMDB_CONFIG_DIR:-$HOME/.config/tmdb-mcp}/env"
fi
export TMDB_REGION="${TMDB_REGION:-US}"
