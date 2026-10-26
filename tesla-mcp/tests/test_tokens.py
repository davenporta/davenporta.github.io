import json
import os
import stat

import httpx
import pytest

from tesla_mcp.config import Settings, redact
from tesla_mcp.tokens import AuthError, TokenManager, read_secret_json, write_secret_json


def test_token_file_0600(tmp_path):
    p = tmp_path / "d" / "tokens.json"
    write_secret_json(p, {"a": 1})
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700


def test_loose_perms_tightened(tmp_path):
    p = tmp_path / "tokens.json"
    p.write_text("{}")
    os.chmod(p, 0o644)
    read_secret_json(p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


async def test_refresh_rotates_and_persists(tmp_path):
    seen = {}

    def handler(req: httpx.Request):
        seen["form"] = dict(httpx.QueryParams(req.content.decode()))
        return httpx.Response(200, json={"access_token": "AT2", "refresh_token": "RT2", "expires_in": 28800})

    s = Settings.from_env({"TESLA_CLIENT_ID": "cid", "TESLA_REFRESH_TOKEN": "RT1-env-secret",
                           "TESLA_TOKEN_FILE": str(tmp_path / "tok.json")})
    tm = TokenManager(s, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await tm.get_access_token() == "AT2"
    assert seen["form"]["grant_type"] == "refresh_token" and seen["form"]["refresh_token"] == "RT1-env-secret"
    assert json.loads((tmp_path / "tok.json").read_text())["refresh_token"] == "RT2"
    # Next refresh uses the rotated token from the file, not the env one
    await tm.refresh()
    assert seen["form"]["refresh_token"] == "RT2"


async def test_refresh_error_does_not_leak_body(tmp_path):
    def handler(req):
        return httpx.Response(401, json={"error": "login_required", "debug": "RT1-env-secret"})

    s = Settings.from_env({"TESLA_CLIENT_ID": "cid", "TESLA_REFRESH_TOKEN": "RT1-env-secret",
                           "TESLA_TOKEN_FILE": str(tmp_path / "tok.json")})
    tm = TokenManager(s, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(AuthError) as e:
        await tm.refresh()
    assert "login_required" in str(e.value) and "RT1-env-secret" not in str(e.value)


def test_redact(monkeypatch):
    monkeypatch.setenv("TESLA_CLIENT_SECRET", "ta-secret-abcdef")
    assert "ta-secret" not in redact("oops ta-secret-abcdef leaked")


def test_settings_repr_hides_secrets():
    s = Settings.from_env({"TESLA_CLIENT_SECRET": "ta-secret-xyz123", "TESLA_REFRESH_TOKEN": "NA_rt_123456"})
    assert "ta-secret" not in repr(s) and "NA_rt" not in repr(s)
