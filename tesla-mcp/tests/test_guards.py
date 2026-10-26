import json

import pytest

from tesla_mcp import guards
from tesla_mcp.client import ALLOWED_ENDPOINTS, EndpointNotAllowed, match_endpoint
from tesla_mcp.mock import ASLEEP_VIN, MOCK_VIN as V
from tesla_mcp.service import TeslaService

HOME_POS = (42.2808, -83.7430)
AIRPORT_ORD = (41.9742, -87.9073)


def _sends(fleet):
    return [c for c in fleet.calls if "/command/" in c[1] or "/wake_up" in c[1]]


def _batt(fleet, lvl, rng=None):
    fleet.data["charge_state"]["battery_level"] = lvl
    if rng is not None:
        fleet.data["charge_state"]["battery_range"] = rng


ON_CALLS = [
    lambda s: s.start_climate(V, confirm=True),
    lambda s: s.set_temps(V, 21, confirm=True),
    lambda s: s.set_seat_heater(V, "front_left", 2, confirm=True),
    lambda s: s.set_steering_wheel_heater(V, True, confirm=True),
    lambda s: s.set_defrost(V, True, confirm=True),
    lambda s: s.climate_actions(V, [{"action": "stop_climate"}, {"action": "start_climate"}], confirm=True),
    lambda s: s.add_precondition_schedule(V, "07:30", ["WEEKDAYS"], 42.2808, -83.7430, confirm=True),
]
OFF_CALLS = [
    lambda s: s.stop_climate(V, confirm=True),
    lambda s: s.set_seat_heater(V, "front_left", 0, confirm=True),
    lambda s: s.set_steering_wheel_heater(V, False, confirm=True),
    lambda s: s.set_defrost(V, False, confirm=True),
    lambda s: s.remove_precondition_schedule(V, 5, confirm=True),
    lambda s: s.climate_actions(V, [{"action": "stop_climate"}, {"action": "set_defrost", "on": False}], confirm=True),
]


@pytest.mark.parametrize("call", ON_CALLS)
async def test_low_battery_refuses_climate_on(svc, fleet, call):
    _batt(fleet, 19)
    r = await call(svc)
    assert r["refused"] is True and r["sent"] is False and "below the 20% minimum" in r["safety"]["reason"]
    assert not _sends(fleet)


async def test_low_battery_preview_says_refused(svc, fleet):
    _batt(fleet, 10)
    for r in (await svc.start_climate(V), await svc.set_temps(V, 22),
              await svc.climate_actions(V, [{"action": "set_defrost", "on": True}])):
        assert r["preview"] is True and r["refused"] is True and "Refused" in r["message"]
    assert not _sends(fleet)


@pytest.mark.parametrize("call", OFF_CALLS)
async def test_off_actions_always_allowed(svc, fleet, call):
    _batt(fleet, 5)
    fleet.data["drive_state"].update(latitude=AIRPORT_ORD[0], longitude=AIRPORT_ORD[1])
    r = await call(svc)
    assert r.get("refused") is not True and r["sent"] is True


@pytest.mark.parametrize("lvl", [20, 25, 29])
async def test_warn_band_allows_with_warning(svc, fleet, lvl):
    _batt(fleet, lvl)
    p = await svc.start_climate(V)
    assert p["preview"] is True and not p.get("refused") and f"low ({lvl}%" in p["warning"]
    r = await svc.start_climate(V, confirm=True)
    assert r["sent"] is True


async def test_above_warn_band_no_warning(svc, fleet):
    _batt(fleet, 30)
    p = await svc.start_climate(V)
    assert "warning" not in p and p["safety"]["allowed"] is True


async def test_thresholds_env_overridable(svc, fleet, monkeypatch):
    monkeypatch.setenv("TESLA_MIN_CLIMATE_SOC", "40")
    monkeypatch.setenv("TESLA_WARN_CLIMATE_SOC", "60")
    _batt(fleet, 35)
    assert (await svc.start_climate(V))["refused"] is True
    _batt(fleet, 50)
    svc.invalidate(V)
    p = await svc.start_climate(V)
    assert not p.get("refused") and "under 60%" in p["warning"]


async def test_asleep_unknown_battery_refused_not_woken(svc, fleet):
    for r in (await svc.start_climate(ASLEEP_VIN, confirm=True),
              await svc.climate_actions(ASLEEP_VIN, [{"action": "start_climate"}], wake_if_asleep=True, confirm=True),
              await svc.add_precondition_schedule(ASLEEP_VIN, "07:30", ["WEEKDAYS"], *HOME_POS, confirm=True)):
        assert r["refused"] is True and "rather than waking" in r["safety"]["reason"]
    assert not _sends(fleet) and not [c for c in fleet.calls if "/vehicle_data" in c[1]]


async def test_asleep_uses_fresh_cache(fleet, client):
    t = [1000.0]
    svc = TeslaService(client, clock=lambda: t[0])
    await svc.battery_health(V)          # caches vehicle_data while online
    fleet.states[V] = "asleep"
    t[0] += 60
    p = await svc.start_climate(V)
    assert p["preview"] is True and not p.get("refused")  # cached battery 78% -> allowed (send would still need online)


async def test_away_refuses_preconditioning(svc, fleet):
    fleet.data["drive_state"].update(latitude=AIRPORT_ORD[0], longitude=AIRPORT_ORD[1])
    for call in ON_CALLS:
        svc.invalidate(V)
        r = await call(svc)
        assert r["refused"] is True and "not at home/work" in r["safety"]["reason"]
    assert not _sends(fleet)


async def test_work_place_allowed(svc, fleet, monkeypatch):
    monkeypatch.setenv("TESLA_WORK_LATLON", "42.3314,-83.0458")
    fleet.data["drive_state"].update(latitude=42.3320, longitude=-83.0460)
    p = await svc.start_climate(V)
    assert not p.get("refused") and p["safety"]["location"]["place"] == "work"


def test_unconfigured_when_no_places_set(monkeypatch):
    monkeypatch.delenv("TESLA_HOME_LATLON")
    assert guards.classify_location(42.28, -83.74)["place"] == "unconfigured"
    assert guards.classify_location(42.28, -83.74)["at_home_or_work"] is None
    assert guards.classify_location(None, None)["known"] is False


def test_places_json(tmp_path, monkeypatch):
    monkeypatch.delenv("TESLA_HOME_LATLON")
    f = tmp_path / "places.json"
    f.write_text(json.dumps({"home": [42.20, -83.70], "radius_km": 2}))
    monkeypatch.setenv("TESLA_PLACES_FILE", str(f))
    assert guards.classify_location(42.205, -83.70)["place"] == "home"
    assert guards.classify_location(42.28, -83.74)["place"] == "away"  # >2 km from configured home


async def test_unknown_location_allowed_with_warning_or_refused_in_strict_mode(svc, fleet, monkeypatch):
    del fleet.data["drive_state"]["latitude"], fleet.data["drive_state"]["longitude"]  # no vehicle_location scope
    p = await svc.start_climate(V)
    assert not p.get("refused") and "location unknown" in p["warning"]
    monkeypatch.setenv("TESLA_REQUIRE_LOCATION", "1")
    assert (await svc.start_climate(V))["refused"] is True


async def test_schedule_target_away_warns(svc, fleet):
    r = await svc.add_precondition_schedule(V, "07:30", ["WEEKDAYS"], *AIRPORT_ORD)
    assert r["preview"] is True and "not at home/work" in r["warning"]


async def test_daily_check_reports_guard_fields(svc, fleet):
    r = await svc.daily_check(V)
    assert r["at_home_or_work"] is True and r["precondition_allowed"] is True
    _batt(fleet, 15)
    svc.invalidate(V)
    r = await svc.daily_check(V)
    assert r["precondition_allowed"] is False and "below" in r["precondition_reason"]
    _batt(fleet, 80)
    fleet.data["drive_state"].update(latitude=AIRPORT_ORD[0], longitude=AIRPORT_ORD[1])
    svc.invalidate(V)
    r = await svc.daily_check(V)
    assert r["at_home_or_work"] is False and r["precondition_allowed"] is False
    r = await svc.daily_check(ASLEEP_VIN)
    assert r["precondition_allowed"] is False and "rather than waking" in r["precondition_reason"]
    assert not _sends(fleet)


CHARGING = ["charge_start", "charge_stop", "charge_port_door_open", "charge_port_door_close", "set_charge_limit",
            "charge_max_range", "charge_standard", "set_charging_amps", "set_scheduled_charging",
            "add_charge_schedule", "remove_charge_schedule", "set_scheduled_departure", "add_charge_schedule",
            "set_cabin_overheat_protection", "set_climate_keeper_mode"]


@pytest.mark.parametrize("cmd", CHARGING)
def test_charging_commands_hard_blocked(cmd):
    with pytest.raises(EndpointNotAllowed):
        match_endpoint("POST", f"/api/1/vehicles/{V}/command/{cmd}")


def test_no_charge_endpoint_on_allowlist():
    assert not [e for e in ALLOWED_ENDPOINTS if "charg" in str(e).lower() and "nearby_charging_sites" not in str(e)]


async def test_charging_blocked_before_network(client, fleet):
    for cmd in ("charge_start", "set_charge_limit", "set_scheduled_charging"):
        with pytest.raises(EndpointNotAllowed):
            await client.request("POST", f"/api/1/vehicles/{V}/command/{cmd}", json={})
    assert fleet.calls == []


async def test_no_location_scope_never_requests_location(svc, fleet, client, monkeypatch):
    monkeypatch.setattr(client.tokens, "scopes", lambda: {"openid", "vehicle_device_data", "vehicle_cmds"})
    p = await svc.start_climate(V)
    vd = [c for c in fleet.calls if "/vehicle_data" in c[1]]
    assert len(vd) == 1 and "location_data" not in str(vd[0]) and "location unknown" in p["warning"]


async def test_location_403_falls_back_once(svc, fleet):
    fleet.location_scope = False
    r = await svc.battery_health(V)
    assert r["battery_level"] == 78 and svc.location_scope() is False
    svc.invalidate(V)
    await svc.battery_health(V)
    vd = [c for c in fleet.calls if "/vehicle_data" in c[1]]
    assert len(vd) == 3  # 403, retry, then later calls skip location_data entirely
