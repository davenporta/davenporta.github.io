"""Offline mock of the Fleet API + signed-command proxy, for tests and TESLA_MOCK=1.

Field names follow the vehicle_data response shape. Values are fake.
"""
from __future__ import annotations

import copy
import json
import re
import time

import httpx

MOCK_VIN = "TEST0000000000001"
ASLEEP_VIN = "TEST0000000000002"

_VEHICLE_DATA = {
    "id": 100021, "vehicle_id": 99999, "vin": MOCK_VIN, "display_name": "Mock Model 3", "state": "online",
    "charge_state": {
        "battery_level": 78, "usable_battery_level": 77, "battery_range": 241.3, "est_battery_range": 205.1,
        "ideal_battery_range": 241.3, "charge_limit_soc": 80, "charging_state": "Disconnected",
        "charge_rate": 0.0, "minutes_to_full_charge": 0, "time_to_full_charge": 0.0, "charger_power": 0,
        "battery_heater_on": False, "charge_energy_added": 12.4, "timestamp": 1761240000000,
    },
    "climate_state": {
        "inside_temp": 18.5, "outside_temp": 9.0, "driver_temp_setting": 21.0, "passenger_temp_setting": 21.0,
        "is_climate_on": False, "is_auto_conditioning_on": False, "is_preconditioning": False,
        "is_front_defroster_on": False, "is_rear_defroster_on": False, "defrost_mode": 0,
        "seat_heater_left": 0, "seat_heater_right": 0, "seat_heater_rear_left": 0, "seat_heater_rear_center": 0,
        "seat_heater_rear_right": 0, "steering_wheel_heater": False, "fan_status": 0,
        "climate_keeper_mode": "off", "cabin_overheat_protection": "On", "battery_heater": False,
        "min_avail_temp": 15.0, "max_avail_temp": 28.0, "timestamp": 1761240000000,
    },
    "vehicle_state": {
        "odometer": 41234.56, "car_version": "2026.32.5 abcdef123456", "locked": True, "sentry_mode": False,
        "tpms_pressure_fl": 2.9, "tpms_pressure_fr": 2.875, "tpms_pressure_rl": 2.6, "tpms_pressure_rr": 2.9,
        "tpms_soft_warning_fl": False, "tpms_soft_warning_fr": False, "tpms_soft_warning_rl": True,
        "tpms_soft_warning_rr": False, "tpms_hard_warning_fl": False, "tpms_hard_warning_fr": False,
        "tpms_hard_warning_rl": False, "tpms_hard_warning_rr": False,
        "tpms_rcp_front_value": 2.9, "tpms_rcp_rear_value": 2.9,
        "tpms_last_seen_pressure_time_fl": 1761239000, "service_mode": False, "service_mode_plus": False,
        "software_update": {"status": "available", "version": "2026.38.1", "download_perc": 100,
                            "install_perc": 1, "expected_duration_sec": 2700, "scheduled_time_ms": 0},
        "timestamp": 1761240000000,
    },
    "vehicle_config": {"car_type": "model3", "trim_badging": "74d", "exterior_color": "MidnightSilver",
                       "wheel_type": "Pinwheel18", "rhd": False},
    "drive_state": {"latitude": 42.2808, "longitude": -83.7430, "heading": 90, "shift_state": None, "speed": None,
                    "power": 0, "timestamp": 1761240000000},
    "preconditioning_schedule_data": {"precondition_schedules": [
        {"id": 1761000000, "days_of_week": 62, "precondition_time": 450, "one_time": False, "enabled": True,
         "latitude": 42.28, "longitude": -83.74, "name": "Work"}]},
}


class MockFleet:
    def __init__(self) -> None:
        self.data = copy.deepcopy(_VEHICLE_DATA)
        self.states = {MOCK_VIN: "online", ASLEEP_VIN: "asleep"}
        self.calls: list[tuple[str, str, dict]] = []
        self.paired: set[str] = {MOCK_VIN, ASLEEP_VIN}
        self.location_scope = True   # False: vehicle_data with location_data -> 403 (like Tesla)
        self.wake_polls_needed = 0   # >0: after wake_up, the car stays "asleep" for this many state polls
        self._pending_wake: dict[str, int] = {}
        self.nav_shares: list[dict] = []

    def _vehicle(self, vin: str) -> dict:
        return {"id": 1 if vin == MOCK_VIN else 2, "vin": vin, "display_name": "Mock" if vin == MOCK_VIN else "Sleepy",
                "state": self.states[vin], "access_type": "OWNER", "api_version": 80,
                "granular_access": {"hide_private": False}}

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}") if request.method == "POST" else {}
        self.calls.append((request.method, f"{request.url.host}{path}", body))
        if request.headers.get("authorization") != "Bearer mock-access-token":
            return httpx.Response(401, json={"error": "invalid_token"})
        ok = lambda r: httpx.Response(200, json={"response": r})  # noqa: E731
        if path == "/api/1/vehicles":
            return ok([self._vehicle(v) for v in self.states])
        m = re.fullmatch(r"/api/1/vehicles/([A-Z0-9]{17})(/.*)?", path)
        if path == "/api/1/vehicles/fleet_status":
            vins = body.get("vins", [])
            return ok({"key_paired_vins": [v for v in vins if v in self.paired],
                       "unpaired_vins": [v for v in vins if v not in self.paired],
                       "vehicle_info": {v: {"firmware_version": "2026.32.5", "vehicle_command_protocol_required": True,
                                            "fleet_telemetry_version": "1.3.0", "total_number_of_keys": 4,
                                            "discounted_device_data": False} for v in vins}})
        if not m or m.group(1) not in self.states:
            return httpx.Response(404, json={"error": "not_found"})
        vin, sub = m.group(1), m.group(2) or ""
        if sub == "":
            if vin in self._pending_wake:
                self._pending_wake[vin] -= 1
                if self._pending_wake[vin] <= 0:
                    del self._pending_wake[vin]
                    self.states[vin] = "online"
            return ok(self._vehicle(vin))
        if sub == "/wake_up":
            if self.wake_polls_needed > 0:
                self._pending_wake[vin] = self.wake_polls_needed
                return ok(self._vehicle(vin))
            self.states[vin] = "online"
            return ok(self._vehicle(vin))
        if self.states[vin] != "online":
            return httpx.Response(408, json={"error": "vehicle unavailable: vehicle is offline or asleep"})
        if sub == "/vehicle_data":
            wanted = (request.url.params.get("endpoints") or "").split(";")
            d = {k: v for k, v in self.data.items() if not isinstance(v, dict)}
            for k in ("charge_state", "climate_state", "vehicle_state", "vehicle_config", "preconditioning_schedule_data"):
                if not wanted[0] or k in wanted:
                    d[k] = copy.deepcopy(self.data[k])
            if "location_data" in wanted and not self.location_scope:
                return httpx.Response(403, json={"error": "Unauthorized missing scopes vehicle_location for vehicle data access"})
            if "location_data" in wanted:
                d["drive_state"] = copy.deepcopy(self.data["drive_state"])
            return ok(d)
        if sub == "/recent_alerts":
            return ok({"recent_alerts": [
                {"name": "VCFRONT_a192_lowVoltageBattery", "time": "2026-10-20T07:12:00Z", "audience": ["customer"],
                 "user_text": "12V battery needs service"},
                {"name": "TPMS_w029_tirePressureLow", "time": "2026-10-22T18:01:00Z", "audience": ["customer"],
                 "user_text": "Tire pressure low - rear left"}]})
        if sub == "/service_data":
            return ok({"service_status": "not_in_service", "service_etc": None, "service_visit_number": None,
                       "status_id": 0})
        if sub == "/release_notes":
            return ok({"release_notes": [{"title": "Mock feature", "subtitle": "", "description": "Fake notes"}]})
        cm = re.fullmatch(r"/command/([a-z_]+)", sub)
        if cm:
            if request.url.host != "proxy.mock":
                return httpx.Response(403, json={"error": "unsigned command rejected (mock)"})
            return self._command(cm.group(1), body)
        return httpx.Response(404, json={"error": "not_found"})

    def _command(self, name: str, body: dict) -> httpx.Response:
        c = self.data["climate_state"]
        if name == "auto_conditioning_start":
            c.update(is_climate_on=True, is_auto_conditioning_on=True)
        elif name == "auto_conditioning_stop":
            c.update(is_climate_on=False, is_auto_conditioning_on=False, defrost_mode=0)
        elif name == "set_temps":
            c.update(driver_temp_setting=body["driver_temp"], passenger_temp_setting=body["passenger_temp"])
        elif name == "remote_seat_heater_request":
            key = {0: "seat_heater_left", 1: "seat_heater_right", 2: "seat_heater_rear_left",
                   4: "seat_heater_rear_center", 5: "seat_heater_rear_right"}.get(body["seat_position"])
            if key:
                c[key] = body["level"]
        elif name == "remote_steering_wheel_heater_request":
            c["steering_wheel_heater"] = body["on"]
        elif name == "set_preconditioning_max":
            c.update(defrost_mode=2 if body["on"] else 0, is_front_defroster_on=body["on"],
                     is_climate_on=body["on"] or c["is_climate_on"])
        elif name == "add_precondition_schedule":
            s = dict(body)
            s.setdefault("id", int(time.time()))
            self.data["preconditioning_schedule_data"]["precondition_schedules"].append(s)
        elif name == "navigation_request":
            self.nav_shares.append(body)
        elif name == "remove_precondition_schedule":
            lst = self.data["preconditioning_schedule_data"]["precondition_schedules"]
            self.data["preconditioning_schedule_data"]["precondition_schedules"] = [x for x in lst if x["id"] != body["id"]]
        else:
            return httpx.Response(404, json={"error": "unknown command"})
        return httpx.Response(200, json={"response": {"result": True, "reason": ""}})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)
