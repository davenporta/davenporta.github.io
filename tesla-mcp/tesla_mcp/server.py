"""MCP server entry point. stdio is the primary transport; streamable-HTTP is optional.

  tesla-mcp                          # stdio (default; for Claude Desktop, Cursor, etc.)
  tesla-mcp --transport http --port 8765   # streamable HTTP on 127.0.0.1
"""
from __future__ import annotations

import argparse
import hmac
import os
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from .client import FleetClient
from .config import Settings
from .service import TeslaService

INSTRUCTIONS = (
    "Tesla vehicle maintenance + climate + send-destination server. Read tools never wake the car. Every command "
    "tool (climate, navigation, wake) returns a preview unless called with confirm=true; only set confirm=true "
    "after the user explicitly approves the previewed action. Costs: vehicle_data $0.002 (cached ~3 min and shared "
    "by the read tools), command $0.001, wake $0.02; vehicle list/state, alerts, service status, release notes and "
    "fleet_status are free (Tesla gives a $10/month discount). next_trip_check takes a destination (address or lat/lon) and arrival time supplied by the caller (e.g. from any calendar) and decides whether offering preconditioning is sensible. Climate-on/preconditioning commands are refused below TESLA_MIN_CLIMATE_SOC (20%) battery, when the battery is unknown and the car asleep, or when the car is away from home/work; turning things off is always allowed. Prefer climate_actions to batch several climate "
    "changes after a single wake. There are no lock/unlock, door, trunk, window, charging, Sentry, or "
    "remote-start tools."
)

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
CMD = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

_service: TeslaService | None = None


def get_service() -> TeslaService:
    global _service
    if _service is None:
        _service = TeslaService(FleetClient(Settings.from_env()))
    return _service


def build_server(service_factory=get_service) -> MCPServer:
    mcp = MCPServer(name="tesla-mcp", instructions=INSTRUCTIONS, version="0.1.0", log_level="WARNING")
    S = service_factory

    @mcp.tool(annotations=RO)
    async def list_vehicles() -> dict:
        """List vehicles on the account with their online/asleep state (does not wake them). Free."""
        return await S().list_vehicles()

    @mcp.tool(annotations=RO)
    async def vehicle_status(vin: str, include_location: bool = False) -> dict:
        """Battery %, range, charge state, locked state, odometer, software version.
        Location is only fetched when include_location=true (needs vehicle_location scope; shows a location icon
        in the car). Returns 'asleep' without waking. $0.002 (vehicle_data; free if cached <3 min)."""
        return await S().vehicle_status(vin, include_location)

    @mcp.tool(annotations=RO)
    async def maintenance_summary(vin: str, include_alerts: bool = True, include_service_status: bool = True) -> dict:
        """Odometer, tire pressures + TPMS warnings, software update status, battery figures, service mode,
        current service status, recent alerts, and a list of what Tesla does NOT expose (e.g. service history,
        battery health %). $0.002 (one shared vehicle_data; free if cached <3 min)."""
        return await S().maintenance_summary(vin, include_alerts, include_service_status)

    @mcp.tool(annotations=RO)
    async def battery_health(vin: str) -> dict:
        """Battery level, usable level, rated/estimated/ideal range and a rough projected range at 100%.
        Fleet API has no true state-of-health metric; this says so explicitly. $0.002 (free if cached <3 min)."""
        return await S().battery_health(vin)

    @mcp.tool(annotations=RO)
    async def vehicle_alerts(vin: str) -> dict:
        """Recent vehicle alerts from GET /recent_alerts. Free."""
        return await S().vehicle_alerts(vin)

    @mcp.tool(annotations=RO)
    async def software_update_status(vin: str, include_release_notes: bool = False,
                                     include_pending_update: bool = False) -> dict:
        """Installed firmware + virtual-key pairing (free fleet_status, works while asleep). Pending-update
        status/progress comes from cached vehicle_data if fresh; set include_pending_update=true to fetch it
        ($0.002, car must be online). Release notes are free."""
        return await S().software_update_status(vin, include_release_notes, include_pending_update)

    @mcp.tool(annotations=RO)
    async def daily_check(vin: str, battery_threshold_pct: int = 30, tire_tolerance_psi: float = 3.0,
                          use_vehicle_data: bool = True, mark_alerts_seen: bool = True) -> dict:
        """Compact 'needs attention' list for a scheduled daily check. NEVER wakes the car. Uses free endpoints
        (state, fleet_status, recent_alerts since last run, service_data); if the car is already online (or data is
        cached) it also checks battery vs threshold, tire pressure vs recommended (>= tolerance psi low, or TPMS
        warning) and a software update waiting on Wi-Fi, using at most one $0.002 vehicle_data call."""
        return await S().daily_check(vin, battery_threshold_pct, tire_tolerance_psi, use_vehicle_data, mark_alerts_seen)

    @mcp.tool(annotations=RO)
    async def charge_check(vin: str, threshold_pct: int = 50, use_vehicle_data: bool = True) -> dict:
        """Is the battery below threshold_pct and not charging? NEVER wakes the car: uses cached data, or one
        $0.002 vehicle_data call only if the car is already online. Asleep and uncached -> needs_charge=null."""
        return await S().charge_check(vin, threshold_pct, use_vehicle_data)

    @mcp.tool(annotations=RO)
    async def climate_status(vin: str, include_schedules: bool = False) -> dict:
        """Cabin/outside temp, set points, climate/defrost/seat/steering-wheel heater state; optionally
        preconditioning schedules. $0.002 (free if cached <3 min)."""
        return await S().climate_status(vin, include_schedules)

    @mcp.tool(annotations=RO)
    async def next_trip_check(vin: str, arrival_time: str, destination: str | None = None,
                              latitude: float | None = None, longitude: float | None = None,
                              title: str | None = None) -> dict:
        """Read-only. Given where the user needs to be and when (arrival_time ISO 8601; no offset = local timezone;
        destination address, or latitude+longitude; optional title, e.g. a calendar event from any source), geocodes
        the address (OSM, cached), estimates the drive from the car (OSRM, else haversine), and says whether offering
        preconditioning is reasonable (car at home/work/local area, drive <= TESLA_MAX_PRECOND_DRIVE_MIN (90), range
        >= 1.5x trip + margin, battery above the climate minimum). Returns suggested leave time and precondition
        start (leave - 15 min). Reads no calendar itself. Never wakes the car (uses cached data, or one $0.002
        vehicle_data only if already online) and never sends commands."""
        return await S().next_trip_check(vin, arrival_time, destination, latitude, longitude, title)

    @mcp.tool(annotations=CMD)
    async def wake_vehicle(vin: str, confirm: bool = False) -> dict:
        """Wake a sleeping car ($0.02, drains a little battery). Sends ONE wake, then polls the free state endpoint
        every 5 s for up to 60 s (never re-wakes). No-op if already online. Preview unless confirm=true."""
        return await S().wake_vehicle(vin, confirm)

    @mcp.tool(annotations=CMD)
    async def start_climate(vin: str, confirm: bool = False) -> dict:
        """Start climate / precondition now (auto_conditioning_start). Preview unless confirm=true."""
        return await S().start_climate(vin, confirm)

    @mcp.tool(annotations=CMD)
    async def stop_climate(vin: str, confirm: bool = False) -> dict:
        """Stop climate (auto_conditioning_stop). Preview unless confirm=true."""
        return await S().stop_climate(vin, confirm)

    @mcp.tool(annotations=CMD)
    async def set_temps(vin: str, driver_temp_c: float, passenger_temp_c: float | None = None,
                        confirm: bool = False) -> dict:
        """Set cabin temperature in °C (15-28). Passenger defaults to driver. Preview unless confirm=true."""
        return await S().set_temps(vin, driver_temp_c, passenger_temp_c, confirm)

    @mcp.tool(annotations=CMD)
    async def set_seat_heater(vin: str, seat: str, level: int, confirm: bool = False) -> dict:
        """Set a seat heater level 0-3. seat: front_left, front_right, rear_left, rear_center, rear_right,
        third_row_left, third_row_right. Climate must be on. Preview unless confirm=true."""
        return await S().set_seat_heater(vin, seat, level, confirm)

    @mcp.tool(annotations=CMD)
    async def set_steering_wheel_heater(vin: str, on: bool, confirm: bool = False) -> dict:
        """Steering wheel heater on/off. Climate must be on. Preview unless confirm=true."""
        return await S().set_steering_wheel_heater(vin, on, confirm)

    @mcp.tool(annotations=CMD)
    async def set_defrost(vin: str, on: bool, confirm: bool = False) -> dict:
        """Max defrost on/off (set_preconditioning_max). Preview unless confirm=true."""
        return await S().set_defrost(vin, on, confirm)

    @mcp.tool(annotations=CMD)
    async def climate_actions(vin: str, actions: list[dict], wake_if_asleep: bool = False, confirm: bool = False) -> dict:
        """Batch several climate commands (in order) after at most ONE wake. actions: list of objects like
        {"action": "start_climate"}, {"action": "stop_climate"}, {"action": "set_temps", "driver_temp_c": 21,
        "passenger_temp_c": 21}, {"action": "set_seat_heater", "seat": "front_left", "level": 3},
        {"action": "set_steering_wheel_heater", "on": true}, {"action": "set_defrost", "on": true}. Max 10.
        $0.001 per command (+$0.02 if wake_if_asleep and the car is asleep). Preview unless confirm=true."""
        return await S().climate_actions(vin, actions, wake_if_asleep, confirm)

    @mcp.tool(annotations=CMD)
    async def send_destination(vin: str, address: str | None = None, latitude: float | None = None,
                               longitude: float | None = None, confirm: bool = False) -> dict:
        """Send a destination to the car's navigation (navigation_request share): an address / place name, and/or
        latitude+longitude (coordinates take precedence for precision). $0.001. Preview unless confirm=true."""
        return await S().send_destination(vin, address, latitude, longitude, confirm=confirm)

    @mcp.tool(annotations=CMD)
    async def add_precondition_schedule(vin: str, time_hhmm: str, days: list[str], latitude: float, longitude: float,
                                        one_time: bool = False, enabled: bool = True, schedule_id: int | None = None,
                                        confirm: bool = False) -> dict:
        """Add (or modify, with schedule_id) a recurring scheduled-departure preconditioning schedule: cabin ready by
        time_hhmm (car local, 24h) on days (list of SUN..SAT, or ALL / WEEKDAYS), enabled on/off, one_time=false
        for recurring; applied when the car is at lat/lon (e.g. home). Example weekday 07:30:
        days=["WEEKDAYS"], time_hhmm="07:30". Firmware 2024.26+. $0.001. Preview unless confirm=true."""
        return await S().add_precondition_schedule(vin, time_hhmm, days, latitude, longitude, one_time, enabled,
                                                   schedule_id, confirm)

    @mcp.tool(annotations=CMD)
    async def remove_precondition_schedule(vin: str, schedule_id: int, confirm: bool = False) -> dict:
        """Delete a preconditioning schedule by id (see climate_status include_schedules=true). Preview unless confirm=true."""
        return await S().remove_precondition_schedule(vin, schedule_id, confirm)

    return mcp


class BearerAuth:
    """Tiny ASGI guard for the optional HTTP transport."""

    def __init__(self, app: Any, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(auth, b"Bearer " + self.token):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="tesla-mcp")
    ap.add_argument("--transport", choices=["stdio", "http"], default=os.environ.get("TESLA_MCP_TRANSPORT", "stdio"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args(argv)
    import logging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    Settings.from_env()  # fail fast on bad config
    mcp = build_server()
    if a.transport == "stdio":
        mcp.run("stdio")
        return
    token = os.environ.get("TESLA_MCP_HTTP_TOKEN")
    if a.host not in ("127.0.0.1", "localhost", "::1") and not token:
        sys.exit("Refusing to bind HTTP to a non-loopback host without TESLA_MCP_HTTP_TOKEN set.")
    import uvicorn
    app = mcp.streamable_http_app(host=a.host)
    if token:
        app = BearerAuth(app, token)
    print(f"tesla-mcp streamable HTTP on http://{a.host}:{a.port}/mcp", file=sys.stderr)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
