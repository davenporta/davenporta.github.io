"""JSON CLI over TeslaService for scheduled jobs. Checks + previews only; never confirms, wakes or sends."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from .service import TeslaService

PREVIEWABLE = ("start_climate", "stop_climate", "set_temps", "set_seat_heater", "set_steering_wheel_heater",
               "set_defrost", "climate_actions", "send_destination", "add_precondition_schedule",
               "remove_precondition_schedule", "wake_vehicle")
FORBIDDEN_ARGS = {"confirm", "wake_if_asleep", "vin"}


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tesla_cli", description=__doc__)
    ap.add_argument("--vin", help="VIN (default: the only/first vehicle on the account)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("daily_check", help="needs-attention list; never wakes")
    d.add_argument("--battery-threshold", type=int, default=30)
    d.add_argument("--tire-tolerance-psi", type=float, default=3.0)
    d.add_argument("--free-only", action="store_true", help="never make the $0.002 vehicle_data call")
    d.add_argument("--no-mark-seen", action="store_true", help="don't advance the alerts last-seen marker")
    c = sub.add_parser("charge_check", help="battery below threshold? never wakes")
    c.add_argument("--threshold", type=int, default=50)
    c.add_argument("--free-only", action="store_true")
    p = sub.add_parser("preview", help="preview a command (never sent)")
    p.add_argument("command", choices=PREVIEWABLE)
    p.add_argument("--args", default="{}", help="JSON object of tool arguments (no vin/confirm)")
    sub.add_parser("list_vehicles", help="free")
    t = sub.add_parser("next_trip", help="read-only: is preconditioning for this destination/arrival reasonable?")
    t.add_argument("--arrival-time", required=True, help="ISO 8601; no offset = local timezone")
    t.add_argument("--destination", default=None, help="address (or use --lat/--lon)")
    t.add_argument("--lat", type=float, default=None)
    t.add_argument("--lon", type=float, default=None)
    t.add_argument("--title", default=None)
    return ap


async def run(argv: list[str] | None, service: TeslaService) -> tuple[int, Any]:
    a = _parser().parse_args(argv)
    if a.cmd == "list_vehicles":
        return 0, await service.list_vehicles()
    vin = a.vin
    if not vin:
        vs = (await service.list_vehicles()).get("vehicles") or []
        if not vs:
            return 2, {"error": "no vehicles on account"}
        vin = vs[0]["vin"]
    if a.cmd == "daily_check":
        r = await service.daily_check(vin, a.battery_threshold, a.tire_tolerance_psi,
                                      use_vehicle_data=not a.free_only, mark_alerts_seen=not a.no_mark_seen)
        return (0 if r["ok"] else 1), r
    if a.cmd == "next_trip":
        return 0, await service.next_trip_check(vin, a.arrival_time, a.destination, a.lat, a.lon, a.title)
    if a.cmd == "charge_check":
        r = await service.charge_check(vin, a.threshold, use_vehicle_data=not a.free_only)
        return (1 if r.get("needs_charge") else 0), r
    kwargs = json.loads(a.args)
    if not isinstance(kwargs, dict) or FORBIDDEN_ARGS & set(kwargs):
        return 2, {"error": f"--args must be a JSON object without {sorted(FORBIDDEN_ARGS)}"}
    r = await getattr(service, a.command)(vin, **kwargs, confirm=False)
    assert r.get("preview") is True, "preview expected"
    return 0, r


async def _amain(argv: list[str] | None) -> int:
    a = _parser().parse_args(argv)
    from .client import FleetClient
    from .config import Settings
    client = FleetClient(Settings.from_env())
    try:
        code, out = await run(argv, TeslaService(client))
        if isinstance(out, dict):
            out.setdefault("estimated_cost_usd_this_run", round(client.estimated_cost_usd(), 4))
    except (ValueError, TypeError) as e:
        code, out = 2, {"error": str(e)}
    finally:
        await client.aclose()
    print(json.dumps(out, indent=1, default=str))
    return code


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_amain(argv if argv is not None else sys.argv[1:]))
    except Exception as e:  # never leak tracebacks with request details into job logs
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 2
