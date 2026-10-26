"""Fleet API HTTP client with a HARD endpoint allowlist.

Anything not matched by ALLOWED_ENDPOINTS raises EndpointNotAllowed *before* a socket is
opened. There is deliberately no generic "send command" path: unlock/lock, doors, trunk,
frunk, windows, sunroof, remote start, honk/flash, charge port, charging, Sentry, valet,
PIN, speed limit, software update, navigation, media, homelink, sharing, etc. are absent.

The only non-climate command is navigation_request (send a destination). Commands are routed ONLY through the signed-command proxy (teslamotors/vehicle-command
tesla-http-proxy), never sent unsigned to Fleet API.
"""
from __future__ import annotations

import asyncio
import re
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from urllib.parse import urlparse

import httpx

from .config import Settings, redact
from .tokens import TokenManager

VIN_RE = r"[A-HJ-NPR-Z0-9]{17}"  # VINs never contain I, O, Q
Kind = Literal["read", "wake", "command"]


@dataclass(frozen=True)
class Endpoint:
    method: str
    pattern: re.Pattern[str]
    kind: Kind
    pricing: str  # Fleet API pricing category we budget against


def _ep(method: str, path: str, kind: Kind, pricing: str) -> Endpoint:
    rx = "^" + re.escape(path).replace(re.escape("{vin}"), VIN_RE) + "$"
    return Endpoint(method, re.compile(rx), kind, pricing)


# Pricing per Tesla's billing docs: only vehicle_data is a billed "data" request. Vehicle list/state,
# recent_alerts, service_data, release_notes and fleet_status are free (they don't hit the car).
READ_ENDPOINTS = [
    _ep("GET", "/api/1/vehicles", "read", "free"),
    _ep("GET", "/api/1/vehicles/{vin}", "read", "free"),
    _ep("GET", "/api/1/vehicles/{vin}/vehicle_data", "read", "data"),
    _ep("GET", "/api/1/vehicles/{vin}/recent_alerts", "read", "free"),
    _ep("GET", "/api/1/vehicles/{vin}/service_data", "read", "free"),
    _ep("GET", "/api/1/vehicles/{vin}/release_notes", "read", "free"),
    _ep("POST", "/api/1/vehicles/fleet_status", "read", "free"),
]
WAKE_ENDPOINTS = [_ep("POST", "/api/1/vehicles/{vin}/wake_up", "wake", "wake")]
CLIMATE_COMMANDS = (
    "auto_conditioning_start",
    "auto_conditioning_stop",
    "set_temps",
    "remote_seat_heater_request",
    "remote_steering_wheel_heater_request",
    "set_preconditioning_max",          # defrost / max preconditioning
    "add_precondition_schedule",
    "remove_precondition_schedule",
)
# Navigation: ONLY navigation_request (share an address/place/coordinates). tesla-http-proxy v0.4.x forwards it to
# Fleet API (Tesla handles it server-side). navigation_gps_request / _sc_ / _waypoints_ are NOT allowed
# (the proxy rejects navigation_gps_request with invalid_command, and we never send commands around the proxy).
NAV_COMMANDS = ("navigation_request",)
COMMAND_ENDPOINTS = [_ep("POST", f"/api/1/vehicles/{{vin}}/command/{c}", "command", "command")
                     for c in CLIMATE_COMMANDS + NAV_COMMANDS]
ALLOWED_ENDPOINTS: tuple[Endpoint, ...] = tuple(READ_ENDPOINTS + WAKE_ENDPOINTS + COMMAND_ENDPOINTS)
ALLOWED_QUERY_KEYS = {"endpoints"}

# USD per request, from https://developer.tesla.com/docs/fleet-api/billing-and-limits
# (vehicle_data: 500/$1, commands: 1000/$1, wake_up: 50/$1; the endpoints marked "free" cost $0).
PRICE_USD = {"free": 0.0, "data": 1 / 500, "command": 1 / 1000, "wake": 1 / 50}


class EndpointNotAllowed(PermissionError):
    pass


class VehicleUnavailable(RuntimeError):
    """408: vehicle asleep/offline/poor connectivity. NOTE: 4xx responses are still billed."""


class FleetAPIError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def match_endpoint(method: str, path: str) -> Endpoint:
    method = method.upper()
    if "?" in path or "#" in path or ".." in path or "//" in path:
        raise EndpointNotAllowed(f"Malformed path rejected: {path!r}")
    for ep in ALLOWED_ENDPOINTS:
        if ep.method == method and ep.pattern.match(path):
            return ep
    raise EndpointNotAllowed(f"{method} {path} is not on the allowlist")


def validate_vin(vin: str) -> str:
    vin = (vin or "").strip().upper()
    if not re.fullmatch(VIN_RE, vin):
        raise ValueError("Invalid VIN (expected 17 chars, no I/O/Q)")
    return vin


class FleetClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        if settings.mock and transport is None:
            from .mock import MockFleet
            transport = MockFleet().transport()
        self._transport = transport
        self._fleet = httpx.AsyncClient(base_url=settings.fleet_base_url, timeout=settings.timeout_s, transport=transport)
        self._proxy: httpx.AsyncClient | None = None
        if settings.proxy_url or settings.mock:
            verify: Any = settings.proxy_ca_cert or True
            self._proxy = httpx.AsyncClient(
                base_url=settings.proxy_url or "https://proxy.mock", timeout=max(settings.timeout_s, 60),
                transport=transport, verify=verify,
            )
        self.tokens = TokenManager(settings)
        self.request_log: list[tuple[str, str, str]] = []  # (method, path, pricing) for cost accounting

    async def aclose(self) -> None:
        await self._fleet.aclose()
        if self._proxy:
            await self._proxy.aclose()

    @asynccontextmanager
    async def _ephemeral_proxy(self) -> AsyncIterator[None]:
        """Spawn tesla-http-proxy ephemerally on 127.0.0.1 for a single confirmed command and terminate on exit."""
        if self.s.mock or self._transport is not None:
            yield
            return
        if not self.s.proxy_url:
            raise EndpointNotAllowed("TESLA_PROXY_URL is not set.")
        parsed = urlparse(self.s.proxy_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 4443
        if host not in ("127.0.0.1", "localhost"):
            raise EndpointNotAllowed(f"Refusing non-loopback proxy host: {host}")

        root_dir = Path(__file__).resolve().parents[1]
        proxy_bin = Path(self.s.proxy_bin or (Path.home() / ".local" / "bin" / "tesla-http-proxy"))
        cert_file = Path(self.s.proxy_ca_cert or (root_dir / "proxy-config" / "tls-cert.pem"))
        tls_key_file = Path(self.s.proxy_tls_key or (root_dir / "proxy-config" / "tls-key.pem"))
        key_file = Path(self.s.fleet_key_file or (root_dir / "keys" / "private-key.pem"))

        cmd = [
            str(proxy_bin),
            "-host", host,
            "-port", str(port),
            "-cert", str(cert_file),
            "-tls-key", str(tls_key_file),
            "-key-file", str(key_file),
        ]
        if self.s.state_dir:
            self.s.state_dir.mkdir(parents=True, exist_ok=True)
            cmd.extend(["-session-cache", str(self.s.state_dir / "session-cache.json")])

        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            ready = False
            for _ in range(50):
                if proc.poll() is not None:
                    err = (proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else "").strip()
                    raise FleetAPIError(0, f"tesla-http-proxy exited prematurely: {err}")
                try:
                    _, writer = await asyncio.open_connection(host, port)
                    writer.close()
                    await writer.wait_closed()
                    ready = True
                    break
                except OSError:
                    await asyncio.sleep(0.05)
            if not ready:
                raise FleetAPIError(0, f"Timed out waiting for ephemeral tesla-http-proxy on {host}:{port}")
            yield
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    await asyncio.to_thread(proc.wait, 2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    await asyncio.to_thread(proc.wait, 1.0)
            if proc.stderr:
                proc.stderr.close()

    async def request(self, method: str, path: str, *, params: dict[str, str] | None = None,
                      json: dict[str, Any] | None = None) -> Any:
        ep = match_endpoint(method, path)  # hard gate, before anything else
        if params and set(params) - ALLOWED_QUERY_KEYS:
            raise EndpointNotAllowed(f"Query params not allowed: {sorted(set(params) - ALLOWED_QUERY_KEYS)}")
        if ep.kind == "command":
            if self._proxy is None:
                raise EndpointNotAllowed("TESLA_PROXY_URL is not set; climate commands are only sent through the "
                                         "signed-command proxy (tesla-http-proxy), never unsigned.")
            async with self._ephemeral_proxy():
                return await self._do_request(self._proxy, ep, method, path, params=params, json=json)
        return await self._do_request(self._fleet, ep, method, path, params=params, json=json)

    async def _do_request(self, http: httpx.AsyncClient, ep: Endpoint, method: str, path: str, *,
                          params: dict[str, str] | None = None, json: dict[str, Any] | None = None) -> Any:
        token = await self.tokens.get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        self.request_log.append((ep.method, path, ep.pricing))
        try:
            r = await http.request(ep.method, path, params=params, json=json if method.upper() == "POST" else None,
                                   headers=headers)
        except httpx.HTTPError as e:
            raise FleetAPIError(0, redact(f"transport error: {type(e).__name__}: {e}", self.s)) from None
        if r.status_code == 408:
            raise VehicleUnavailable("Vehicle unavailable (asleep, offline, or poor connectivity). Use the "
                                     "wake_vehicle tool explicitly if you want to wake it (costs $0.02).")
        if r.status_code == 401 and not self.s.mock:
            # one silent refresh + retry
            await self.tokens.refresh()
            headers["Authorization"] = f"Bearer {await self.tokens.get_access_token()}"
            r = await http.request(ep.method, path, params=params, json=json if method.upper() == "POST" else None,
                                   headers=headers)
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("error_description") or body.get("error") or str(body)[:300]
            except Exception:
                msg = r.text[:300]
            raise FleetAPIError(r.status_code, redact(str(msg), self.s))
        body = r.json()
        return body.get("response", body) if isinstance(body, dict) else body

    def estimated_cost_usd(self) -> float:
        return round(sum(PRICE_USD[p] for _, _, p in self.request_log), 4)
