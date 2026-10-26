"""Tool logic, independent of the MCP transport (so it is unit-testable)."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import guards
from .client import PRICE_USD, FleetAPIError, FleetClient, VehicleUnavailable, validate_vin

SEATS = {"front_left": 0, "front_right": 1, "rear_left": 2, "rear_center": 4, "rear_right": 5,
         "third_row_left": 7, "third_row_right": 8}
DAYS = {"SUN", "MON", "TUES", "WED", "THURS", "FRI", "SAT", "ALL", "WEEKDAYS"}
DAY_ALIASES = {"TUE": "TUES", "THU": "THURS", "THUR": "THURS", "SUNDAY": "SUN", "MONDAY": "MON", "TUESDAY": "TUES",
               "WEDNESDAY": "WED", "THURSDAY": "THURS", "FRIDAY": "FRI", "SATURDAY": "SAT"}

NOT_EXPOSED = [
    "Service history / past service visits, invoices, and work orders (Tesla app only; service_data reports only "
    "current service status, if any)",
    "A battery state-of-health / degradation percentage or original-vs-current pack capacity",
    "Cell- or module-level battery data via vehicle_data (Fleet Telemetry streams some, e.g. BrickVoltageMax/Min, "
    "PackVoltage, EnergyRemaining, IsolationResistance)",
    "Tire tread depth, brake pad wear, wiper/cabin-filter/brake-fluid maintenance intervals",
    "12V/low-voltage battery health as a metric (only surfaced indirectly as an alert, if at all)",
    "Recall/campaign lists and warranty status",
]


def _pick(d: dict | None, *keys: str) -> dict[str, Any]:
    d = d or {}
    return {k: d.get(k) for k in keys if k in d}


def _cost(kinds: dict[str, int]) -> float:
    return round(sum(PRICE_USD[k] * n for k, n in kinds.items()), 4)


# All vehicle_data sections the read tools need, fetched together in ONE billed call and cached per car.
# location_data is always included (same price) so the at-home/work safety guard can use the shared cache; it is
# only *returned* by read tools when include_location=true. Without the vehicle_location scope Tesla omits lat/lon.
VD_SECTIONS = ("charge_state", "climate_state", "vehicle_state", "vehicle_config", "preconditioning_schedule_data",
               "location_data")
VD_CACHE_TTL_S = 180          # vehicle_data cache (~3 min)
PAIRING_CACHE_TTL_S = 3600    # fleet_status "key paired" result (~1 h)
UNPAIRED_CACHE_TTL_S = 60     # re-check soon after the user pairs
WAKE_POLL_INTERVAL_S = 5
WAKE_POLL_TIMEOUT_S = 60

# Batchable climate actions for climate_actions(): action name -> builder(service, params) -> (cmd, body, text)
BATCH_ACTIONS = ("start_climate", "stop_climate", "set_temps", "set_seat_heater", "set_steering_wheel_heater",
                 "set_defrost")


class TeslaService:
    def __init__(self, client: FleetClient, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, state_dir: str | Path | None = None):
        self.c = client
        self.state_dir = Path(state_dir or os.environ.get("TESLA_MCP_STATE_DIR")
                              or Path.home() / ".local/state/tesla-mcp")
        self._clock, self._sleep = clock, sleep
        self._vd_cache: dict[str, tuple[float, frozenset[str], dict]] = {}
        self._pair_cache: dict[str, tuple[float, bool]] = {}
        # injectable for tests (read-only geocoder / router / clock)
        self.geocode_fn: Callable | None = None
        self.route_fn: Callable | None = None
        self.now_fn: Callable | None = None
        self._no_location_scope = False
        self.wake_poll_interval_s = WAKE_POLL_INTERVAL_S
        self.wake_poll_timeout_s = WAKE_POLL_TIMEOUT_S

    # ---------- helpers ----------
    async def _state(self, vin: str) -> dict:
        return await self.c.request("GET", f"/api/1/vehicles/{vin}")  # free; never wakes the car

    async def _vehicle_data(self, vin: str, endpoints: list[str] | None = None, *, cached_only: bool = False
                            ) -> dict | None:
        """One shared, cached vehicle_data call per car (~3 min). Returns None if the car is asleep/offline
        (checked first via the free state endpoint, so no billed 408) or, with cached_only, if nothing is cached."""
        need = set(endpoints or ()) | set(VD_SECTIONS)
        if not self.location_scope():
            need.discard("location_data")  # Tesla answers 403 (still billed) if requested without the scope
        hit = self._vd_cache.get(vin)
        if hit and self._clock() - hit[0] < VD_CACHE_TTL_S and need <= hit[1]:
            return {**hit[2], "_data_age_s": round(self._clock() - hit[0])}
        if cached_only:
            return None
        v = await self._state(vin)
        if v.get("state") != "online":
            return None
        try:
            d = await self.c.request("GET", f"/api/1/vehicles/{vin}/vehicle_data",
                                     params={"endpoints": ";".join(sorted(need))})
        except VehicleUnavailable:
            return None
        except FleetAPIError as e:
            if e.status != 403 or "vehicle_location" not in str(e) or "location_data" not in need:
                raise
            self._no_location_scope = True  # remember; retry once without location
            need.discard("location_data")
            d = await self.c.request("GET", f"/api/1/vehicles/{vin}/vehicle_data",
                                     params={"endpoints": ";".join(sorted(need))})
        self._vd_cache[vin] = (self._clock(), frozenset(need), d)
        return {**d, "_data_age_s": 0}

    async def _guard_data(self, vin: str, fetch: bool = True) -> tuple[dict | None, str | None]:
        """vehicle_data (with location_data if possible) for the safety guards. Cached first; otherwise ONE
        $0.002 call only if the car is already online. Never wakes. Returns (data|None, car_state|None)."""
        d = await self._vehicle_data(vin, cached_only=True)
        if d is not None or not fetch:
            return d, (d or {}).get("state")
        st = (await self._state(vin)).get("state")
        return (await self._vehicle_data(vin) if st == "online" else None), st

    async def climate_guard(self, vin: str, fetch: bool = True) -> dict:
        d, st = await self._guard_data(vin, fetch)
        return guards.evaluate(d, car_state=st)

    @staticmethod
    def _turns_on(name: str, body: dict) -> bool:
        """True for anything that starts/raises climate or preconditioning (guarded); False for off/stop/remove."""
        if name in ("auto_conditioning_start", "set_temps"):
            return True
        if name == "remote_seat_heater_request":
            return int(body.get("level") or 0) > 0
        if name in ("remote_steering_wheel_heater_request", "set_preconditioning_max"):
            return bool(body.get("on"))
        if name == "add_precondition_schedule":
            return bool(body.get("enabled", True))
        return False

    def location_scope(self) -> bool:
        """Does the token carry vehicle_location? (decoded locally from the JWT; unknown -> assume yes)."""
        if self._no_location_scope:
            return False
        tm = getattr(self.c, "tokens", None)
        scopes = tm.scopes() if tm is not None and hasattr(tm, "scopes") else None
        return scopes is None or "vehicle_location" in scopes

    def invalidate(self, vin: str) -> None:
        self._vd_cache.pop(vin, None)

    async def _fleet_status(self, vin: str) -> dict:
        fs = await self.c.request("POST", "/api/1/vehicles/fleet_status", json={"vins": [vin]})  # free
        if isinstance(fs, dict):
            paired = vin in (fs.get("key_paired_vins") or [])
            self._pair_cache[vin] = (self._clock(), paired)
        return fs

    async def key_paired(self, vin: str) -> bool:
        hit = self._pair_cache.get(vin)
        if hit:
            ttl = PAIRING_CACHE_TTL_S if hit[1] else UNPAIRED_CACHE_TTL_S
            if self._clock() - hit[0] < ttl:
                return hit[1]
        await self._fleet_status(vin)
        return self._pair_cache.get(vin, (0, False))[1]

    async def _wait_online(self, vin: str) -> tuple[str | None, int]:
        """Poll the FREE state endpoint (never re-wakes) until online or timeout."""
        waited = 0.0
        state = None
        while True:
            state = (await self._state(vin)).get("state")
            if state == "online" or waited >= self.wake_poll_timeout_s:
                return state, round(waited)
            await self._sleep(self.wake_poll_interval_s)
            waited += self.wake_poll_interval_s

    @staticmethod
    def _asleep(vin: str, state: str | None = "asleep") -> dict:
        return {"vin": vin, "state": state or "asleep", "available": False,
                "message": "Vehicle is asleep/offline, so live data was not fetched (no wake, no extra charge). "
                           "Try again later, or call wake_vehicle(confirm=true) explicitly (~$0.02 per wake)."}

    # ---------- read-only ----------
    async def list_vehicles(self) -> dict:
        vs = await self.c.request("GET", "/api/1/vehicles")
        return {"vehicles": [_pick(v, "vin", "display_name", "state", "access_type") for v in vs],
                "note": "state is reported without waking the car."}

    async def vehicle_status(self, vin: str, include_location: bool = False) -> dict:
        vin = validate_vin(vin)
        eps = ["charge_state", "vehicle_state", "vehicle_config"] + (["location_data"] if include_location else [])
        d = await self._vehicle_data(vin, eps)
        if d is None:
            return self._asleep(vin)
        cs, vs = d.get("charge_state", {}), d.get("vehicle_state", {})
        out = {
            "vin": vin, "state": d.get("state"), "display_name": d.get("display_name"),
            "battery": _pick(cs, "battery_level", "usable_battery_level", "battery_range", "est_battery_range",
                             "ideal_battery_range"),
            "charge": _pick(cs, "charging_state", "charge_limit_soc", "charge_rate", "charger_power",
                            "minutes_to_full_charge", "charge_energy_added"),
            "locked": vs.get("locked"),
            "odometer_miles": vs.get("odometer"),
            "software_version": (vs.get("car_version") or "").split(" ")[0] or None,
            "units_note": "Fleet API reports odometer and ranges in miles.",
            "data_age_s": d.get("_data_age_s"),
        }
        if include_location:
            ds = d.get("drive_state") or {}
            out["location"] = _pick(ds, "latitude", "longitude", "heading") or \
                "not returned (needs vehicle_location scope; hidden for shared drivers with hide_private)"
        return out

    async def maintenance_summary(self, vin: str, include_alerts: bool = True, include_service_status: bool = True) -> dict:
        vin = validate_vin(vin)
        out: dict[str, Any] = {"vin": vin}
        d = await self._vehicle_data(vin)
        if d is None:
            out.update(self._asleep(vin))
        else:
            vs, cs = d.get("vehicle_state", {}), d.get("charge_state", {})
            out["odometer_miles"] = vs.get("odometer")
            out["tires"] = self._tires(vs)
            out["software"] = self._software(vs)
            out["battery"] = self._battery(cs)
            out["service_mode"] = _pick(vs, "service_mode", "service_mode_plus")
            out["vehicle_config"] = _pick(d.get("vehicle_config"), "car_type", "trim_badging", "wheel_type")
            out["data_age_s"] = d.get("_data_age_s")
        # Separate endpoints; failures (e.g. car asleep) are reported inline, not raised.
        if include_service_status:
            out["service_status"] = await self._safe(self.c.request("GET", f"/api/1/vehicles/{vin}/service_data"))
        if include_alerts:
            out["recent_alerts"] = await self._alerts(vin)
        out["not_exposed_by_fleet_api"] = NOT_EXPOSED
        return out

    async def battery_health(self, vin: str) -> dict:
        vin = validate_vin(vin)
        d = await self._vehicle_data(vin)
        if d is None:
            return self._asleep(vin)
        return {"vin": vin, **self._battery(d.get("charge_state", {})), "data_age_s": d.get("_data_age_s")}

    async def vehicle_alerts(self, vin: str) -> dict:
        vin = validate_vin(vin)
        return {"vin": vin, "recent_alerts": await self._alerts(vin)}

    async def software_update_status(self, vin: str, include_release_notes: bool = False,
                                     include_pending_update: bool = False) -> dict:
        """Installed firmware + pairing come from the FREE fleet_status. Pending-update details come from the
        cached vehicle_data if fresh; a new (billed, $0.002) vehicle_data call is made only if
        include_pending_update=true and the car is online."""
        vin = validate_vin(vin)
        out: dict[str, Any] = {"vin": vin}
        fs = await self._safe(self._fleet_status(vin))
        info = (fs.get("vehicle_info") or {}).get(vin, {}) if isinstance(fs, dict) else {}
        out["installed_firmware"] = info.get("firmware_version")
        out["virtual_key_paired"] = vin in (fs.get("key_paired_vins") or []) if isinstance(fs, dict) else None
        out["vehicle_command_protocol_required"] = info.get("vehicle_command_protocol_required")
        d = await self._vehicle_data(vin, cached_only=not include_pending_update)
        if d:
            out["pending_update"] = self._software(d.get("vehicle_state", {}))
            out["pending_update_data_age_s"] = d.get("_data_age_s")
        elif include_pending_update:
            out["pending_update"] = "car asleep: pending-update details need the car online"
        else:
            out["pending_update"] = ("not fetched (no fresh cached data). Re-run with include_pending_update=true "
                                     "to fetch it ($0.002, car must be online).")
        if include_release_notes:
            out["release_notes"] = await self._safe(self.c.request("GET", f"/api/1/vehicles/{vin}/release_notes"))
        return out

    async def climate_status(self, vin: str, include_schedules: bool = False) -> dict:
        vin = validate_vin(vin)
        d = await self._vehicle_data(vin)
        if d is None:
            return self._asleep(vin)
        c = d.get("climate_state", {})
        out = {"vin": vin, "climate": _pick(
            c, "is_climate_on", "is_auto_conditioning_on", "is_preconditioning", "inside_temp", "outside_temp",
            "driver_temp_setting", "passenger_temp_setting", "min_avail_temp", "max_avail_temp",
            "is_front_defroster_on", "is_rear_defroster_on", "defrost_mode", "seat_heater_left", "seat_heater_right",
            "seat_heater_rear_left", "seat_heater_rear_center", "seat_heater_rear_right", "steering_wheel_heater",
            "climate_keeper_mode", "cabin_overheat_protection", "fan_status"),
            "units_note": "Temperatures are Celsius.", "data_age_s": d.get("_data_age_s")}
        if include_schedules:
            pre = (d.get("preconditioning_schedule_data") or {}).get("precondition_schedules", [])
            out["precondition_schedules"] = [
                {**_pick(s, "id", "name", "enabled", "one_time", "days_of_week"),
                 "precondition_time": _fmt_minutes(s.get("precondition_time"))} for s in pre]
        return out

    # ---------- wake (paid, explicit) ----------
    async def wake_vehicle(self, vin: str, confirm: bool = False) -> dict:
        vin = validate_vin(vin)
        call = f"POST /api/1/vehicles/{vin}/wake_up"
        if confirm is not True:
            return {"preview": True, "would_call": call, "estimated_cost_usd": PRICE_USD["wake"],
                    "message": "Not sent. Re-run with confirm=true to wake the car. After one wake, the server polls "
                               f"the free state endpoint every {self.wake_poll_interval_s}s for up to "
                               f"{self.wake_poll_timeout_s}s instead of re-waking."}
        return await self._wake(vin)

    async def _wake(self, vin: str) -> dict:
        call = f"POST /api/1/vehicles/{vin}/wake_up"
        if (await self._state(vin)).get("state") == "online":
            return {"sent": False, "state": "online", "cost_usd": 0.0, "message": "Already online; no wake sent."}
        r = await self.c.request("POST", f"/api/1/vehicles/{vin}/wake_up")  # exactly one paid wake
        state = r.get("state") if isinstance(r, dict) else None
        waited = 0
        if state != "online":
            state, waited = await self._wait_online(vin)
        return {"sent": True, "called": call, "state": state, "waited_s": waited, "cost_usd": PRICE_USD["wake"],
                **({} if state == "online" else {"message": f"Car not online after {waited}s of free polling; "
                                                            "not re-waking. Try again shortly."})}

    # ---------- commands (confirm-gated, signed via proxy, key-pairing pre-check) ----------
    async def _precheck(self, vin: str) -> str | None:
        """Free checks before any paid command. Returns an error message, or None if OK."""
        try:
            paired = await self.key_paired(vin)
        except FleetAPIError as e:
            return f"Could not verify virtual-key pairing (fleet_status failed: {e}); command not sent."
        if not paired:
            return ("The virtual key is not paired with this car, so the car would reject the command. Pair the "
                    "key first: open https://tesla.com/_ak/<your-domain> on your phone near the car and approve it "
                    "in the Tesla app. Command not sent.")
        return None

    async def _send(self, vin: str, name: str, body: dict) -> dict:
        path = f"/api/1/vehicles/{vin}/command/{name}"
        try:
            r = await self.c.request("POST", path, json=body)
        except VehicleUnavailable as e:
            return {"sent": False, "command": name, "message": str(e)}
        except FleetAPIError as e:
            return {"sent": False, "command": name, "error": str(e)}
        self.invalidate(vin)  # state changed; don't serve stale cached climate data
        return {"sent": True, "command": name, "body": body,
                "result": r.get("result") if isinstance(r, dict) else None,
                "reason": r.get("reason") if isinstance(r, dict) else None, "cost_usd": PRICE_USD["command"]}

    async def command(self, vin: str, name: str, body: dict, description: str, confirm: bool) -> dict:
        vin = validate_vin(vin)
        path = f"/api/1/vehicles/{vin}/command/{name}"
        preview = {"would_call": f"POST {path} (via signed-command proxy)", "body": body, "effect": description,
                   "estimated_cost_usd": PRICE_USD["command"]}
        if self._turns_on(name, body):
            g = await self.climate_guard(vin)
            preview["safety"] = g
            if not g["allowed"]:
                return {"preview": confirm is not True, "sent": False, "refused": True, **preview,
                        "message": f"Refused by safety guard: {g['reason']} Nothing sent."}
            if g["warnings"]:
                preview["warning"] = " ".join(g["warnings"])
        if confirm is not True:
            return {"preview": True, **preview, "message": "Not sent. Re-run with confirm=true to execute."}
        err = await self._precheck(vin)
        if err:
            return {"sent": False, **preview, "message": err}
        v = await self._state(vin)
        if v.get("state") != "online":
            return {"sent": False, **preview, "state": v.get("state"),
                    "message": "Car is not online; command not sent (avoids a billed failure). "
                               "Call wake_vehicle(confirm=true) first, or use climate_actions(wake_if_asleep=true)."}
        r = await self._send(vin, name, body)
        if not r.get("sent"):
            return {**preview, **r}
        return {"sent": True, "called": preview["would_call"], "body": body, "result": r["result"],
                "reason": r["reason"], "cost_usd": PRICE_USD["command"]}

    # builders: (command name, body, human description)
    @staticmethod
    def _b_start_climate() -> tuple[str, dict, str]:
        return "auto_conditioning_start", {}, "Turn on climate (precondition cabin now)."

    @staticmethod
    def _b_stop_climate() -> tuple[str, dict, str]:
        return "auto_conditioning_stop", {}, "Turn off climate."

    @staticmethod
    def _b_set_temps(driver_temp_c: float, passenger_temp_c: float | None = None) -> tuple[str, dict, str]:
        p = driver_temp_c if passenger_temp_c is None else passenger_temp_c
        for t in (driver_temp_c, p):
            if not 15.0 <= float(t) <= 28.0:
                raise ValueError("Temperatures must be between 15 and 28 °C (Tesla's usual cabin range).")
        return ("set_temps", {"driver_temp": float(driver_temp_c), "passenger_temp": float(p)},
                f"Set cabin to driver {float(driver_temp_c)}°C / passenger {float(p)}°C.")

    @staticmethod
    def _b_set_seat_heater(seat: str, level: int) -> tuple[str, dict, str]:
        if seat not in SEATS:
            raise ValueError(f"seat must be one of {sorted(SEATS)}")
        if level not in (0, 1, 2, 3) or isinstance(level, bool):
            raise ValueError("level must be 0 (off) to 3 (high)")
        return ("remote_seat_heater_request", {"seat_position": SEATS[seat], "level": level},
                f"Set {seat} seat heater to {level}. Requires climate to be on.")

    @staticmethod
    def _b_set_steering_wheel_heater(on: bool) -> tuple[str, dict, str]:
        return ("remote_steering_wheel_heater_request", {"on": bool(on)},
                f"Turn steering wheel heater {'on' if on else 'off'}. Requires climate to be on.")

    @staticmethod
    def _b_set_defrost(on: bool) -> tuple[str, dict, str]:
        return ("set_preconditioning_max", {"on": bool(on), "manual_override": False},
                f"{'Enable' if on else 'Disable'} max defrost (front/rear defrost + max heat).")

    async def start_climate(self, vin: str, confirm: bool = False) -> dict:
        return await self.command(vin, *self._b_start_climate(), confirm)

    async def stop_climate(self, vin: str, confirm: bool = False) -> dict:
        return await self.command(vin, *self._b_stop_climate(), confirm)

    async def set_temps(self, vin: str, driver_temp_c: float, passenger_temp_c: float | None = None,
                        confirm: bool = False) -> dict:
        return await self.command(vin, *self._b_set_temps(driver_temp_c, passenger_temp_c), confirm)

    async def set_seat_heater(self, vin: str, seat: str, level: int, confirm: bool = False) -> dict:
        return await self.command(vin, *self._b_set_seat_heater(seat, level), confirm)

    async def set_steering_wheel_heater(self, vin: str, on: bool, confirm: bool = False) -> dict:
        return await self.command(vin, *self._b_set_steering_wheel_heater(on), confirm)

    async def set_defrost(self, vin: str, on: bool, confirm: bool = False) -> dict:
        return await self.command(vin, *self._b_set_defrost(on), confirm)

    async def climate_actions(self, vin: str, actions: list[dict], wake_if_asleep: bool = False,
                              confirm: bool = False) -> dict:
        """Several climate commands in one go, after at most ONE wake. Each action is
        {"action": <one of BATCH_ACTIONS>, ...params}, e.g. {"action": "set_temps", "driver_temp_c": 21}."""
        vin = validate_vin(vin)
        if not actions:
            raise ValueError("actions must not be empty")
        if len(actions) > 10:
            raise ValueError("at most 10 actions per batch")
        built = []
        for a in actions:
            if not isinstance(a, dict) or a.get("action") not in BATCH_ACTIONS:
                raise ValueError(f"each action needs 'action' in {list(BATCH_ACTIONS)}")
            params = {k: v for k, v in a.items() if k != "action"}
            try:
                built.append(getattr(self, f"_b_{a['action']}")(**params))
            except TypeError as e:
                raise ValueError(f"bad parameters for {a['action']}: {e}") from None
        steps = [{"would_call": f"POST /api/1/vehicles/{vin}/command/{n} (via signed-command proxy)", "body": b,
                  "effect": d} for n, b, d in built]
        est = round(PRICE_USD["command"] * len(built), 4)
        safety: dict[str, Any] = {}
        if any(self._turns_on(n, b) for n, b, _ in built):
            g = await self.climate_guard(vin)  # before any wake: asleep + unknown battery -> refused, not woken
            safety = {"safety": g, **({"warning": " ".join(g["warnings"])} if g["warnings"] else {})}
            if not g["allowed"]:
                return {"preview": confirm is not True, "sent": False, "refused": True, "steps": steps, **safety,
                        "message": f"Refused by safety guard: {g['reason']} Nothing sent (turning things off is "
                                   "always allowed)."}
        if confirm is not True:
            return {"preview": True, "steps": steps, **safety, "wake_if_asleep": bool(wake_if_asleep),
                    "estimated_cost_usd": est, "estimated_cost_if_wake_needed_usd": round(est + PRICE_USD["wake"], 4),
                    "message": "Not sent. Re-run with confirm=true to execute all steps in order."}
        err = await self._precheck(vin)
        if err:
            return {"sent": False, "steps": steps, "message": err}
        wake = None
        if (await self._state(vin)).get("state") != "online":
            if not wake_if_asleep:
                return {"sent": False, "steps": steps, "message": "Car is not online; nothing sent. Re-run with "
                        "wake_if_asleep=true (one $0.02 wake, then free polling) if you want to proceed."}
            wake = await self._wake(vin)
            if wake.get("state") != "online":
                return {"sent": False, "steps": steps, "wake": wake, "message": "Car did not come online; nothing sent."}
        results = [await self._send(vin, n, b) for n, b, _ in built]
        cost = sum(PRICE_USD["command"] for r in results if r.get("sent")) + (wake or {}).get("cost_usd", 0.0)
        return {"sent": any(r.get("sent") for r in results), "results": results,
                **({"wake": wake} if wake else {}), "cost_usd": round(cost, 4)}

    # ---------- navigation (confirm-gated, via proxy, key-pairing pre-check) ----------
    async def send_destination(self, vin: str, address: str | None = None, latitude: float | None = None,
                               longitude: float | None = None, locale: str = "en-US", confirm: bool = False) -> dict:
        """navigation_request share. With lat/lon the coordinates are shared as text (the car's geocoder
        resolves them precisely); otherwise the address/place text is shared."""
        address = (address or "").strip() or None
        if address and (len(address) > 300 or any(ord(ch) < 32 for ch in address)):
            raise ValueError("address must be a single line of at most 300 characters")
        if (latitude is None) != (longitude is None):
            raise ValueError("give both latitude and longitude, or neither")
        if latitude is not None and not (-90 <= float(latitude) <= 90 and -180 <= float(longitude) <= 180):
            raise ValueError("latitude/longitude out of range")
        if not address and latitude is None:
            raise ValueError("give an address and/or latitude+longitude")
        if not re.fullmatch(r"[a-z]{2}(-[A-Z]{2})?", locale or ""):
            raise ValueError("locale must look like en-US")
        text = f"{float(latitude):.6f},{float(longitude):.6f}" if latitude is not None else address
        body = {"type": "share_ext_content_raw", "locale": locale, "timestamp_ms": str(int(time.time() * 1000)),
                "value": {"android.intent.extra.TEXT": text}}
        desc = (f"Send destination to the car's navigation: {address or ''}"
                f"{' ' if address and latitude is not None else ''}"
                f"{f'({text})' if latitude is not None else ''}".strip())
        r = await self.command(vin, "navigation_request", body, desc, confirm)
        r.setdefault("note", "navigation_request goes through the local proxy, which forwards it to Fleet API "
                             "(Tesla processes navigation shares server-side). It only sets a destination; it cannot "
                             "unlock, start, or move the car.")
        return r

    async def add_precondition_schedule(self, vin: str, time_hhmm: str, days: list[str], latitude: float,
                                        longitude: float, one_time: bool = False, enabled: bool = True,
                                        schedule_id: int | None = None, confirm: bool = False) -> dict:
        minutes = _parse_hhmm(time_hhmm)
        norm = []
        for d in days:
            u = DAY_ALIASES.get(d.strip().upper(), d.strip().upper())
            if u not in DAYS:
                raise ValueError(f"Unknown day {d!r}; use SUN..SAT, ALL or WEEKDAYS")
            norm.append(u)
        if not norm:
            raise ValueError("days must not be empty")
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("latitude/longitude out of range")
        body: dict[str, Any] = {"precondition_time": minutes, "days_of_week": ",".join(norm), "lat": latitude,
                                "lon": longitude, "one_time": bool(one_time), "enabled": bool(enabled)}
        if schedule_id is not None:
            body["id"] = int(schedule_id)
        verb = "Modify" if schedule_id is not None else "Add"
        r = await self.command(vin, "add_precondition_schedule", body,
                               f"{verb} a preconditioning schedule: ready by {time_hhmm} (car local time) on "
                               f"{', '.join(norm)} at the given location. Requires firmware 2024.26+.", confirm)
        target = guards.classify_location(latitude, longitude)
        r["schedule_location"] = target
        if target["known"] and not target["at_home_or_work"]:
            r.setdefault("warning", "")
            r["warning"] = (r["warning"] + " Schedule location is not at home/work/local area.").strip()
        return r

    async def remove_precondition_schedule(self, vin: str, schedule_id: int, confirm: bool = False) -> dict:
        return await self.command(vin, "remove_precondition_schedule", {"id": int(schedule_id)},
                                  f"Delete preconditioning schedule {schedule_id}.", confirm)

    # ---------- scheduled checks (free-first, NEVER wake) ----------
    def _seen_file(self) -> Path:
        return self.state_dir / "alerts_last_seen.json"

    def _load_seen(self) -> dict:
        try:
            return json.loads(self._seen_file().read_text())
        except (OSError, ValueError):
            return {}

    def _save_seen(self, data: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        f = self._seen_file()
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(f)

    async def _live_data(self, vin: str, state: str | None, use_vehicle_data: bool) -> tuple[dict | None, str]:
        """Cached vehicle_data (free) if fresh; else ONE paid call only if the car is already online. Never wakes.
        The paid call includes location_data (same price) for the at-home/work guard."""
        d = await self._vehicle_data(vin, cached_only=True)
        if d is not None:
            return d, "cache"
        if use_vehicle_data and state == "online":
            d = await self._vehicle_data(vin)
            if d is not None:
                return d, "vehicle_data ($0.002)"
        return None, "none"

    async def daily_check(self, vin: str, battery_threshold_pct: int = 30, tire_tolerance_psi: float = 3.0,
                          use_vehicle_data: bool = True, mark_alerts_seen: bool = True,
                          include_release_notes: bool = False) -> dict:
        vin = validate_vin(vin)
        attention: list[str] = []
        info: list[str] = []
        state = (await self._state(vin)).get("state")
        out: dict[str, Any] = {"vin": vin, "state": state, "asleep": state != "online", "woke_car": False}

        fs = await self._safe(self._fleet_status(vin))
        if isinstance(fs, dict) and "vehicle_info" in fs:
            vi = (fs.get("vehicle_info") or {}).get(vin, {})
            out["installed_firmware"] = vi.get("firmware_version")
            if vin not in (fs.get("key_paired_vins") or []):
                attention.append("Virtual key is not paired: climate/navigation commands will be refused.")

        # alerts newer than the stored last-seen time
        alerts = await self._alerts(vin)
        seen = self._load_seen()
        last = seen.get(vin)
        if isinstance(alerts, list):
            new = [a for a in alerts if _ts(a.get("time")) and (last is None or _ts(a["time"]) > _ts(last))]
            for a in new:
                attention.append(f"New alert {a.get('time')}: {a.get('user_text') or a.get('name')}")
            newest = max((a["time"] for a in alerts if _ts(a.get("time"))), key=_ts, default=None)
            if mark_alerts_seen and newest and (last is None or _ts(newest) > _ts(last)):
                seen[vin] = newest
                self._save_seen(seen)
            out["new_alert_count"] = len(new)
            out["alerts_last_seen_before"] = last
        else:
            out["recent_alerts_error"] = alerts

        svc_data = await self._safe(self.c.request("GET", f"/api/1/vehicles/{vin}/service_data"))
        if isinstance(svc_data, dict) and svc_data.get("service_status") not in (None, "not_in_service"):
            attention.append(f"Service status: {svc_data.get('service_status')} (ETC {svc_data.get('service_etc')})")
        if include_release_notes:
            out["release_notes"] = await self._safe(self.c.request("GET", f"/api/1/vehicles/{vin}/release_notes"))

        d, src = await self._live_data(vin, state, use_vehicle_data)
        out["live_data_source"] = src
        if d is not None:
            out["data_age_s"] = d.get("_data_age_s")
            cs, vs = d.get("charge_state", {}), d.get("vehicle_state", {})
            lvl = cs.get("battery_level")
            out["battery_level"] = lvl
            if isinstance(lvl, (int, float)) and lvl < battery_threshold_pct:
                attention.append(f"Battery {lvl}% is below {battery_threshold_pct}% (charging: {cs.get('charging_state')}).")
            tires = self._tires(vs)
            rec = tires.get("recommended_cold_bar", {})
            for pos in ("fl", "fr", "rl", "rr"):
                t = tires[pos]
                rbar = rec.get("tpms_rcp_front_value" if pos[0] == "f" else "tpms_rcp_rear_value")
                rpsi = round(rbar * 14.5038, 1) if isinstance(rbar, (int, float)) else None
                if t.get("hard_warning") or t.get("soft_warning"):
                    attention.append(f"TPMS {'hard' if t.get('hard_warning') else 'soft'} warning on {pos.upper()} "
                                     f"({t.get('pressure_psi')} psi).")
                elif rpsi and isinstance(t.get("pressure_psi"), (int, float)) and \
                        t["pressure_psi"] <= rpsi - tire_tolerance_psi:
                    attention.append(f"Tire {pos.upper()} {t['pressure_psi']} psi is >= {tire_tolerance_psi:g} psi "
                                     f"below recommended {rpsi} psi.")
            sw = self._software(vs)
            out["software_update"] = {k: sw.get(k) for k in ("update_status", "update_version")}
            st = str(sw.get("update_status") or "")
            if "wifi" in st.lower():
                attention.append(f"Software update {sw.get('update_version') or ''} is waiting on Wi-Fi.".replace("  ", " "))
            elif st in ("available", "scheduled", "downloading", "installing"):
                info.append(f"Software update {sw.get('update_version')}: {st}.")
        elif state == "online":
            info.append("vehicle_data disabled (free-only): battery, tires and pending update not checked.")
        else:
            info.append("Car asleep/offline and no fresh cached data: battery, tires and pending update not checked "
                        "(not woken).")
        g = guards.evaluate(d, car_state=state)
        out["at_home_or_work"] = (g.get("location") or {}).get("at_home_or_work")
        out["location"] = {k: v for k, v in (g.get("location") or {}).items() if k != "known"}
        out["precondition_allowed"] = g["allowed"]
        out["precondition_reason"] = g["reason"] if not g["warnings"] else f"{g['reason']}; " + " ".join(g["warnings"])
        out["needs_attention"] = attention
        out["info"] = info
        out["ok"] = not attention
        return out

    async def next_trip_check(self, vin: str, arrival_time: str, destination: str | None = None,
                              latitude: float | None = None, longitude: float | None = None,
                              title: str | None = None) -> dict:
        """Read-only: caller-supplied destination + arrival time -> geocode (if needed) -> drive estimate from the
        car -> is offering preconditioning reasonable? Reads no calendar itself, never wakes the car and never
        sends a command."""
        import datetime as _dt
        from . import trip
        vin = validate_vin(vin)
        ev = trip.build_destination(arrival_time, destination, latitude, longitude, title)
        now = (self.now_fn() if self.now_fn else _dt.datetime.now(trip.TZ)).astimezone(trip.TZ)
        out: dict[str, Any] = {"vin": vin, "now": now.isoformat(timespec="minutes"), "woke_car": False,
                               "commands_sent": 0}
        if _dt.datetime.fromisoformat(ev["start"]) <= now:
            out.update(trip={"title": title, "arrival_time": ev["start"], "destination": ev.get("location")},
                       reasonable=False, reasons=["Arrival time has already passed."])
            return out
        d, st = await self._guard_data(vin, fetch=True)
        g = guards.evaluate(d, car_state=st)
        out["car_state"] = st
        ds = (d or {}).get("drive_state") or {}
        loc = g.get("location") or {}
        if loc.get("known"):
            origin, osrc = (float(ds["latitude"]), float(ds["longitude"])), "car"
        else:
            pl = guards.load_places()["places"]
            origin = pl.get("home") or pl.get("work") or (next(iter(pl.values())) if pl else None)
            if origin is None:
                return {**base, "reasonable": False, "reasons": ["Car location is unknown and no home/work origin is configured."]}
            osrc = "configured home (car location unknown)"
        out["origin"] = osrc
        out["at_home_or_work"] = loc.get("at_home_or_work")
        cs = (d or {}).get("charge_state") or {}
        r = await asyncio.to_thread(trip.plan, ev, origin, battery_level=cs.get("battery_level"),
                                    battery_range_mi=cs.get("battery_range"), guard=g,
                                    geocode_fn=self.geocode_fn or trip.geocode, route_fn=self.route_fn or trip.route)
        out.update(r)
        if out["reasonable"]:
            what = ev.get("title") or ev.get("location") or "the trip"
            out["offer"] = (f"Offer to precondition at {out['suggested_precondition_start'][11:16]} for "
                            f"'{what}' (leave ~{out['suggested_leave_time'][11:16]}). Ask the user first; "
                            "this tool sends nothing.")
        return out

    async def charge_check(self, vin: str, threshold_pct: int = 50, use_vehicle_data: bool = True) -> dict:
        vin = validate_vin(vin)
        if not 1 <= int(threshold_pct) <= 100:
            raise ValueError("threshold_pct must be 1-100")
        state = (await self._state(vin)).get("state")
        out: dict[str, Any] = {"vin": vin, "state": state, "asleep": state != "online", "woke_car": False,
                               "threshold_pct": int(threshold_pct)}
        d, src = await self._live_data(vin, state, use_vehicle_data)
        out["live_data_source"] = src
        if d is None:
            msg = ("Car is online but vehicle_data was disabled (free-only) and nothing is cached; battery level unknown."
                   if state == "online" else
                   "Car asleep/offline and no fresh cached data; battery level unknown (not woken). "
                   "A sleeping car is usually not losing much charge.")
            out.update(needs_charge=None, message=msg)
            return out
        cs = d.get("charge_state", {})
        lvl = cs.get("battery_level")
        charging = cs.get("charging_state")
        out.update(battery_level=lvl, battery_range_miles=cs.get("battery_range"), charging_state=charging,
                   charge_limit_soc=cs.get("charge_limit_soc"), data_age_s=d.get("_data_age_s"))
        below = isinstance(lvl, (int, float)) and lvl < int(threshold_pct)
        out["below_threshold"] = below
        out["needs_charge"] = below and charging not in ("Charging", "Starting", "Complete")
        out["plugged_in"] = charging not in (None, "Disconnected")
        return out

    # ---------- formatting ----------
    @staticmethod
    def _tires(vs: dict) -> dict:
        tires = {}
        for pos in ("fl", "fr", "rl", "rr"):
            bar = vs.get(f"tpms_pressure_{pos}")
            tires[pos] = {"pressure_bar": bar, "pressure_psi": round(bar * 14.5038, 1) if isinstance(bar, (int, float)) else None,
                          "soft_warning": vs.get(f"tpms_soft_warning_{pos}"), "hard_warning": vs.get(f"tpms_hard_warning_{pos}")}
        tires["recommended_cold_bar"] = _pick(vs, "tpms_rcp_front_value", "tpms_rcp_rear_value")
        tires["any_warning"] = any(t.get("soft_warning") or t.get("hard_warning") for k, t in tires.items() if k in ("fl", "fr", "rl", "rr"))
        return tires

    @staticmethod
    def _software(vs: dict) -> dict:
        su = vs.get("software_update") or {}
        return {"installed_version": (vs.get("car_version") or "").split(" ")[0] or None,
                "update_status": su.get("status") or "none reported",
                "update_version": su.get("version") or None,
                **_pick(su, "download_perc", "install_perc", "expected_duration_sec", "scheduled_time_ms")}

    @staticmethod
    def _battery(cs: dict) -> dict:
        lvl, rng = cs.get("battery_level"), cs.get("battery_range")
        est = round(rng / lvl * 100, 1) if isinstance(lvl, (int, float)) and lvl >= 20 and isinstance(rng, (int, float)) else None
        return {
            **_pick(cs, "battery_level", "usable_battery_level", "battery_range", "est_battery_range",
                    "ideal_battery_range", "charge_limit_soc", "battery_heater_on"),
            "projected_rated_range_at_100pct_miles": est,
            "health_note": ("Fleet API does NOT expose a battery state-of-health or degradation %. "
                            "projected_rated_range_at_100pct is battery_range scaled to 100% - a rough proxy only; "
                            "compare it against your car's original EPA rated range and track it over time "
                            "(it is noisy, and depends on BMS calibration and temperature). A gap between "
                            "battery_level and usable_battery_level usually means cold-locked energy, not degradation."),
        }

    async def _alerts(self, vin: str) -> Any:
        r = await self._safe(self.c.request("GET", f"/api/1/vehicles/{vin}/recent_alerts"))
        if isinstance(r, dict) and "recent_alerts" in r:
            return [_pick(a, "name", "time", "user_text", "audience") for a in r["recent_alerts"]]
        return r

    @staticmethod
    async def _safe(coro) -> Any:
        try:
            return await coro
        except VehicleUnavailable:
            return {"unavailable": "vehicle asleep/offline"}
        except FleetAPIError as e:
            return {"error": str(e)}


def _ts(v: Any) -> float | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _parse_hhmm(s: str) -> int:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", s.strip())
    if not m:
        raise ValueError("time must be HH:MM (24h, car local time)")
    return int(m.group(1)) * 60 + int(m.group(2))


def _fmt_minutes(m: Any) -> Any:
    return f"{int(m) // 60:02d}:{int(m) % 60:02d}" if isinstance(m, (int, float)) else m
