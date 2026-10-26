"""Configuration, read from environment variables only.

Sources (verified 2026-10-23):
  Regions:  https://developer.tesla.com/docs/fleet-api/getting-started/regions-countries
  Token URL: https://developer.tesla.com/docs/fleet-api/authentication/third-party-tokens
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REGION_BASE_URLS = {
    "na": "https://fleet-api.prd.na.vn.cloud.tesla.com",  # North America + Asia-Pacific (excl. China)
    "eu": "https://fleet-api.prd.eu.vn.cloud.tesla.com",  # Europe, Middle East, Africa
    "cn": "https://fleet-api.prd.cn.vn.cloud.tesla.cn",   # China (separate developer.tesla.cn app)
}
# Tesla requires server-to-server token calls to use this host (announcement 2025-07-21).
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
AUTHORIZE_URL = "https://auth.tesla.com/oauth2/v3/authorize"
# Least privilege: no vehicle_charging_cmds, no user_data. vehicle_location is only needed
# if you want vehicle_status(include_location=True) or precondition schedules at the car's spot.
DEFAULT_SCOPES = "openid offline_access vehicle_device_data vehicle_location vehicle_cmds"

SECRET_ENV_VARS = ("TESLA_CLIENT_SECRET", "TESLA_REFRESH_TOKEN", "TESLA_ACCESS_TOKEN", "TESLA_MCP_HTTP_TOKEN")


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    region: str = "na"
    fleet_base_url: str = REGION_BASE_URLS["na"]
    proxy_url: str | None = None
    proxy_ca_cert: str | None = None
    token_file: Path = Path.home() / ".config" / "tesla-mcp" / "tokens.json"
    mock: bool = False
    timeout_s: float = 30.0
    proxy_bin: str | None = None
    proxy_tls_key: str | None = None
    fleet_key_file: str | None = None
    state_dir: Path | None = None

    @property
    def audience(self) -> str:
        return self.fleet_base_url

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        region = (e.get("TESLA_REGION") or "na").strip().lower()
        if region not in REGION_BASE_URLS:
            raise ValueError(f"TESLA_REGION must be one of {sorted(REGION_BASE_URLS)}")
        base = (e.get("TESLA_FLEET_BASE_URL") or REGION_BASE_URLS[region]).rstrip("/")
        if not base.startswith("https://"):
            raise ValueError("TESLA_FLEET_BASE_URL must be https")
        proxy = (e.get("TESLA_PROXY_URL") or "").rstrip("/") or None
        if proxy and not proxy.startswith("https://"):
            raise ValueError("TESLA_PROXY_URL must be https (tesla-http-proxy only serves TLS)")
        token_file = Path(e.get("TESLA_TOKEN_FILE") or (Path.home() / ".config" / "tesla-mcp" / "tokens.json")).expanduser()
        return cls(
            client_id=e.get("TESLA_CLIENT_ID") or None,
            client_secret=e.get("TESLA_CLIENT_SECRET") or None,
            refresh_token=e.get("TESLA_REFRESH_TOKEN") or None,
            region=region,
            fleet_base_url=base,
            proxy_url=proxy,
            proxy_ca_cert=e.get("TESLA_PROXY_CA_CERT") or None,
            token_file=token_file,
            mock=_truthy(e.get("TESLA_MOCK")),
            timeout_s=float(e.get("TESLA_HTTP_TIMEOUT") or 30),
            proxy_bin=e.get("TESLA_PROXY_BIN") or str(Path.home() / ".local" / "bin" / "tesla-http-proxy"),
            proxy_tls_key=e.get("TESLA_PROXY_TLS_KEY") or str(Path(__file__).resolve().parents[1] / "proxy-config" / "tls-key.pem"),
            fleet_key_file=e.get("TESLA_FLEET_KEY_FILE") or str(Path(__file__).resolve().parents[1] / "keys" / "private-key.pem"),
            state_dir=Path(e.get("TESLA_MCP_STATE_DIR") or (Path.home() / ".local" / "state" / "tesla-mcp")).expanduser(),
        )


def redact(text: str, settings: Settings | None = None) -> str:
    """Remove any known secret values from a string before it is logged or returned."""
    secrets = [os.environ.get(k) for k in SECRET_ENV_VARS]
    if settings:
        secrets += [settings.client_secret, settings.refresh_token]
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "[REDACTED]")
    return text
