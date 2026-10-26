"""OAuth token storage and refresh.

Tesla refresh tokens are single-use and rotate on every exchange (the previous one stays
valid for up to 24h); they expire after 3 months. So the newest refresh token is always
persisted to TESLA_TOKEN_FILE (mode 0600) and preferred over TESLA_REFRESH_TOKEN.
Source: https://developer.tesla.com/docs/fleet-api/authentication/third-party-tokens
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from .config import TOKEN_URL, Settings


class AuthError(RuntimeError):
    pass


def write_secret_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON with 0600 permissions (directory 0700)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except PermissionError:
        pass
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tokens-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_secret_json(path: Path) -> dict[str, Any] | None:
    path = Path(path)
    if not path.exists():
        return None
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        os.chmod(path, 0o600)  # tighten rather than refuse
    with open(path) as f:
        return json.load(f)


class TokenManager:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.s = settings
        self._http = http
        self._cache: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._cache is None:
            self._cache = read_secret_json(self.s.token_file) or {}
        return self._cache

    def save_token_response(self, resp: dict[str, Any]) -> None:
        data = {
            "access_token": resp["access_token"],
            "refresh_token": resp.get("refresh_token") or self._load().get("refresh_token"),
            "expires_at": int(time.time()) + int(resp.get("expires_in", 0)),
            "audience": self.s.audience,
        }
        write_secret_json(self.s.token_file, data)
        self._cache = data

    def scopes(self) -> set[str] | None:
        """Scopes from the cached access token's JWT payload (local decode, no network, never logged).
        None if unknown."""
        if self.s.mock:
            return {"openid", "offline_access", "vehicle_device_data", "vehicle_cmds", "vehicle_location"}
        import base64
        tok = (self._load() or {}).get("access_token") or ""
        try:
            payload = tok.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            scp = claims.get("scp")
            return set(scp.split() if isinstance(scp, str) else scp) if scp else None
        except (IndexError, ValueError, TypeError, AttributeError):
            return None

    async def get_access_token(self) -> str:
        if self.s.mock:
            return "mock-access-token"
        data = self._load()
        # Renew if within 60s of expiry (Tesla best practice: "within a minute").
        if data.get("access_token") and data.get("expires_at", 0) - 60 > time.time():
            return data["access_token"]
        await self.refresh()
        return self._load()["access_token"]

    async def refresh(self) -> None:
        if not self.s.client_id:
            raise AuthError("TESLA_CLIENT_ID is not set")
        rt = self._load().get("refresh_token") or self.s.refresh_token
        if not rt:
            raise AuthError("No refresh token: run scripts/tesla_setup.py exchange first, or set TESLA_REFRESH_TOKEN")
        form = {"grant_type": "refresh_token", "client_id": self.s.client_id, "refresh_token": rt}
        client = self._http or httpx.AsyncClient(timeout=self.s.timeout_s)
        try:
            r = await client.post(TOKEN_URL, data=form)
        finally:
            if self._http is None:
                await client.aclose()
        if r.status_code != 200:
            # Never echo the body verbatim beyond the error code; it can contain token material.
            try:
                err = r.json().get("error", "unknown_error")
            except Exception:
                err = "unparseable_error"
            raise AuthError(f"Token refresh failed: HTTP {r.status_code} ({err}). "
                            "If login_required: refresh token expired/rotated or password reset; re-run the OAuth flow.")
        self.save_token_response(r.json())
