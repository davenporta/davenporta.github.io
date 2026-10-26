#!/usr/bin/env python3
"""One-time setup helper. Never prints secrets or tokens.

  authorize-url      print the Tesla login/consent URL
  exchange CODE      swap the ?code=... for tokens, saved to TESLA_TOKEN_FILE (0600)
  register           register your domain with Fleet API in your region (partner token)
  check-public-key   ask Fleet API which public key it has on file for your domain
  pairing-link       print the link to add the virtual key to your car (open on your phone)

Env: TESLA_CLIENT_ID, TESLA_CLIENT_SECRET, TESLA_REDIRECT_URI, TESLA_DOMAIN, TESLA_REGION/TESLA_FLEET_BASE_URL,
     TESLA_SCOPES (optional), TESLA_TOKEN_FILE (optional).
Sources: https://developer.tesla.com/docs/fleet-api/authentication/third-party-tokens
         https://developer.tesla.com/docs/fleet-api/authentication/partner-tokens
         https://developer.tesla.com/docs/fleet-api/endpoints/partner-endpoints
         https://developer.tesla.com/docs/fleet-api/virtual-keys/developer-guide
"""
from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from tesla_mcp.config import AUTHORIZE_URL, DEFAULT_SCOPES, TOKEN_URL, Settings, redact  # noqa: E402
from tesla_mcp.tokens import TokenManager  # noqa: E402

# Setup-only allowlist (separate from the runtime client's allowlist).
PARTNER_PATHS = {"/api/1/partner_accounts", "/api/1/partner_accounts/public_key"}


def need(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"{name} is not set")
    return v


def authorize_url(_: argparse.Namespace) -> None:
    q = {"response_type": "code", "client_id": need("TESLA_CLIENT_ID"), "redirect_uri": need("TESLA_REDIRECT_URI"),
         "scope": os.environ.get("TESLA_SCOPES", DEFAULT_SCOPES), "state": secrets.token_urlsafe(16),
         "prompt_missing_scopes": "true"}
    print("Open this URL, log in, approve. You'll land on your redirect URI with ?code=...&state=...")
    print(f"Check that state matches: {q['state']}\n")
    print(f"{AUTHORIZE_URL}?{urllib.parse.urlencode(q)}")


async def exchange(a: argparse.Namespace) -> None:
    s = Settings.from_env()
    code = a.code
    if code.startswith("http"):
        code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query).get("code", [""])[0]
    form = {"grant_type": "authorization_code", "client_id": need("TESLA_CLIENT_ID"),
            "client_secret": need("TESLA_CLIENT_SECRET"), "code": code, "audience": s.audience,
            "redirect_uri": need("TESLA_REDIRECT_URI")}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(TOKEN_URL, data=form)
    if r.status_code != 200:
        err = r.json().get("error", "unknown") if r.headers.get("content-type", "").startswith("application/json") else ""
        sys.exit(f"Exchange failed: HTTP {r.status_code} {err} (codes are single-use and short-lived; get a new one)")
    body = r.json()
    TokenManager(s).save_token_response(body)
    print(f"Saved tokens to {s.token_file} (mode 0600). Scopes granted: {body.get('scope', '?')}")
    print("You do not need to put the refresh token in an env var; the server reads/rotates it from that file.")


async def partner_token(s: Settings) -> str:
    form = {"grant_type": "client_credentials", "client_id": need("TESLA_CLIENT_ID"),
            "client_secret": need("TESLA_CLIENT_SECRET"), "scope": "openid vehicle_device_data vehicle_cmds",
            "audience": s.audience}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(TOKEN_URL, data=form)
    if r.status_code != 200:
        sys.exit(f"Partner token failed: HTTP {r.status_code}")
    return r.json()["access_token"]


async def partner_call(method: str, path: str, **kw) -> None:
    assert path in PARTNER_PATHS
    s = Settings.from_env()
    tok = await partner_token(s)
    async with httpx.AsyncClient(base_url=s.fleet_base_url, timeout=30) as c:
        r = await c.request(method, path, headers={"Authorization": f"Bearer {tok}"}, **kw)
    print(f"HTTP {r.status_code}")
    print(redact(r.text[:2000], s))


async def register(_: argparse.Namespace) -> None:
    await partner_call("POST", "/api/1/partner_accounts", json={"domain": need("TESLA_DOMAIN")})


async def check_public_key(_: argparse.Namespace) -> None:
    await partner_call("GET", "/api/1/partner_accounts/public_key", params={"domain": need("TESLA_DOMAIN")})


def pairing_link(a: argparse.Namespace) -> None:
    url = f"https://tesla.com/_ak/{need('TESLA_DOMAIN')}"
    if a.vin:
        url += f"?vin={a.vin}"
    print("Open on the phone with the Tesla app (4.27.3+) signed in as the car owner, near the car:")
    print(url)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("authorize-url").set_defaults(f=authorize_url)
    e = sub.add_parser("exchange"); e.add_argument("code", help="the code, or the full redirected URL"); e.set_defaults(f=exchange)
    sub.add_parser("register").set_defaults(f=register)
    sub.add_parser("check-public-key").set_defaults(f=check_public_key)
    p = sub.add_parser("pairing-link"); p.add_argument("--vin"); p.set_defaults(f=pairing_link)
    a = ap.parse_args()
    r = a.f(a)
    if asyncio.iscoroutine(r):
        asyncio.run(r)


if __name__ == "__main__":
    main()
