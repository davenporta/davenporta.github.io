import pytest

from tesla_mcp.mock import ASLEEP_VIN, MOCK_VIN
from tesla_mcp.server import build_server


def _sends(fleet):  # previews may do free state + cached/shared vehicle_data reads for the safety guard, never sends
    return [c for c in fleet.calls if "/command/" in c[1] or "/wake_up" in c[1]]
from tesla_mcp.service import TeslaService

V = MOCK_VIN


async def test_list_vehicles(svc):
    r = await svc.list_vehicles()
    assert {v["state"] for v in r["vehicles"]} == {"online", "asleep"}


async def test_status_omits_location_by_default(svc, fleet):
    r = await svc.vehicle_status(V)
    assert "location" not in r and "latitude" not in str(r)
    assert all("location_data" not in str(c) for c in fleet.calls)
    r = await svc.vehicle_status(V, include_location=True)
    assert r["location"]["latitude"] == pytest.approx(42.2808)


async def test_asleep_not_woken_and_no_vehicle_data(svc, fleet):
    r = await svc.vehicle_status(ASLEEP_VIN)
    assert r["available"] is False
    paths = [c[1] for c in fleet.calls]
    assert not any("wake_up" in p or "vehicle_data" in p for p in paths)
    assert fleet.states[ASLEEP_VIN] == "asleep"


async def test_maintenance_summary(svc):
    r = await svc.maintenance_summary(V)
    assert r["tires"]["rl"]["soft_warning"] is True and r["tires"]["any_warning"] is True
    assert r["software"]["update_status"] == "available"
    assert len(r["recent_alerts"]) == 2
    assert any("service history" in x.lower() for x in r["not_exposed_by_fleet_api"])
    assert "does NOT expose" in r["battery"]["health_note"]


async def test_maintenance_summary_asleep_still_returns_not_exposed(svc):
    r = await svc.maintenance_summary(ASLEEP_VIN)
    assert r["available"] is False and r["not_exposed_by_fleet_api"]


async def test_battery_health(svc):
    r = await svc.battery_health(V)
    assert r["battery_level"] == 78
    assert r["projected_rated_range_at_100pct_miles"] == pytest.approx(309.4, abs=0.1)


async def test_software_update_status(svc, fleet):
    r = await svc.software_update_status(V)
    assert r["installed_firmware"] == "2026.32.5" and r["virtual_key_paired"] is True
    assert isinstance(r["pending_update"], str)  # nothing cached and not requested -> no paid call
    assert not any("vehicle_data" in c[1] for c in fleet.calls)
    r = await svc.software_update_status(V, include_pending_update=True)
    assert r["pending_update"]["update_version"] == "2026.38.1"


async def test_climate_status_with_schedules(svc):
    r = await svc.climate_status(V, include_schedules=True)
    assert r["climate"]["inside_temp"] == 18.5
    assert r["precondition_schedules"][0]["precondition_time"] == "07:30"


@pytest.mark.parametrize("call", [
    lambda s: s.start_climate(V), lambda s: s.stop_climate(V), lambda s: s.set_temps(V, 21),
    lambda s: s.set_seat_heater(V, "front_left", 3), lambda s: s.set_steering_wheel_heater(V, True),
    lambda s: s.set_defrost(V, True), lambda s: s.add_precondition_schedule(V, "07:45", ["WEEKDAYS"], 42.0, -83.0),
    lambda s: s.remove_precondition_schedule(V, 1), lambda s: s.wake_vehicle(ASLEEP_VIN),
    lambda s: s.send_destination(V, "1600 Pennsylvania Ave NW, Washington, DC"),
    lambda s: s.send_destination(V, latitude=42.28, longitude=-83.74),
    lambda s: s.climate_actions(V, [{"action": "start_climate"}, {"action": "set_temps", "driver_temp_c": 21}]),
])
async def test_commands_preview_without_confirm(svc, fleet, call):
    r = await call(svc)
    assert r["preview"] is True and ("would_call" in r or "steps" in r)
    assert not _sends(fleet)  # nothing sent at all


async def test_confirm_must_be_literal_true(svc, fleet):
    r = await svc.start_climate(V, confirm="yes")
    assert r["preview"] is True and not _sends(fleet)


async def test_confirmed_commands_execute(svc, fleet):
    assert (await svc.start_climate(V, confirm=True))["result"] is True
    await svc.set_temps(V, 22.5, confirm=True)
    await svc.set_seat_heater(V, "front_right", 2, confirm=True)
    await svc.set_steering_wheel_heater(V, True, confirm=True)
    await svc.set_defrost(V, True, confirm=True)
    c = fleet.data["climate_state"]
    assert c["is_climate_on"] and c["driver_temp_setting"] == 22.5 and c["seat_heater_right"] == 2
    assert c["steering_wheel_heater"] is True and c["defrost_mode"] == 2
    await svc.add_precondition_schedule(V, "06:15", ["mon", "Tue"], 42.0, -83.0, confirm=True)
    sched = fleet.data["preconditioning_schedule_data"]["precondition_schedules"][-1]
    assert sched["precondition_time"] == 375 and sched["days_of_week"] == "MON,TUES"
    await svc.remove_precondition_schedule(V, 1761000000, confirm=True)
    assert all(s["id"] != 1761000000 for s in fleet.data["preconditioning_schedule_data"]["precondition_schedules"])
    await svc.stop_climate(V, confirm=True)
    assert fleet.data["climate_state"]["is_climate_on"] is False


async def test_confirmed_command_on_asleep_car_not_sent(svc, fleet):
    r = await svc.start_climate(ASLEEP_VIN, confirm=True)
    assert r["sent"] is False
    assert not any("/command/" in c[1] for c in fleet.calls)


async def test_wake_then_command(svc, fleet):
    assert (await svc.wake_vehicle(ASLEEP_VIN, confirm=True))["state"] == "online"
    assert (await svc.start_climate(ASLEEP_VIN, confirm=True))["sent"] is True


@pytest.mark.parametrize("call", [
    lambda s: s.set_temps(V, 40), lambda s: s.set_seat_heater(V, "trunk", 1), lambda s: s.set_seat_heater(V, "front_left", 9),
    lambda s: s.add_precondition_schedule(V, "25:00", ["MON"], 0, 0),
    lambda s: s.add_precondition_schedule(V, "07:00", ["FUNDAY"], 0, 0), lambda s: s.vehicle_status("1234"),
    lambda s: s.send_destination(V), lambda s: s.send_destination(V, latitude=42.28),
    lambda s: s.send_destination(V, latitude=95, longitude=0), lambda s: s.send_destination(V, "a\nb"),
    lambda s: s.climate_actions(V, []), lambda s: s.climate_actions(V, [{"action": "door_unlock"}]),
    lambda s: s.climate_actions(V, [{"action": "set_temps", "driver_temp_c": 40}]),
    lambda s: s.climate_actions(V, [{"action": "set_temps", "bogus": 1}]),
])
async def test_input_validation(svc, call):
    with pytest.raises(ValueError):
        await call(svc)


async def test_cost_tracking(svc, client):
    await svc.climate_status(V)          # free state + $0.002 vehicle_data
    await svc.start_climate(V, confirm=True)  # free fleet_status + free state + $0.001 command
    assert client.estimated_cost_usd() == pytest.approx(0.002 + 0.001)


async def test_mcp_tool_list_has_no_forbidden_tools(svc):
    mcp = build_server(lambda: svc)
    names = {t.name for t in await mcp.list_tools()}
    assert {"list_vehicles", "vehicle_status", "maintenance_summary", "battery_health", "vehicle_alerts",
            "software_update_status", "climate_status", "wake_vehicle", "start_climate", "stop_climate", "set_temps",
            "set_seat_heater", "set_steering_wheel_heater", "set_defrost", "add_precondition_schedule",
            "remove_precondition_schedule", "climate_actions", "send_destination", "daily_check",
            "charge_check", "next_trip_check"} == names
    bad = ("lock", "door", "trunk", "frunk", "window", "honk", "flash", "charge", "sentry", "remote_start", "valet")
    read_only_ok = {"charge_check"}  # read-only, no charging command behind it
    assert not [n for n in names - read_only_ok if any(b in n for b in bad)]


async def test_mcp_call_tool_roundtrip(svc, fleet):
    mcp = build_server(lambda: svc)
    r = await mcp.call_tool("start_climate", {"vin": V})
    assert "preview" in str(r) and not _sends(fleet)


# ---------- cost-optimization behaviour ----------

def _paths(fleet, needle):
    return [c for c in fleet.calls if needle in c[1]]


async def test_vehicle_data_shared_cache(svc, fleet):
    await svc.maintenance_summary(V)
    await svc.battery_health(V)
    await svc.climate_status(V, include_schedules=True)
    r = await svc.software_update_status(V)
    assert len(_paths(fleet, "/vehicle_data")) == 1  # one billed call shared by all four tools
    assert r["pending_update"]["update_version"] == "2026.38.1"


async def test_vehicle_data_cache_expires(fleet, client):
    t = [1000.0]
    svc = TeslaService(client, clock=lambda: t[0])
    await svc.battery_health(V)
    t[0] += 170
    await svc.battery_health(V)
    assert len(_paths(fleet, "/vehicle_data")) == 1
    t[0] += 20
    await svc.battery_health(V)
    assert len(_paths(fleet, "/vehicle_data")) == 2


async def test_location_not_fetched_unless_requested(svc, fleet):
    await svc.climate_status(V)
    await svc.vehicle_status(V)
    assert not any("location_data" in str(c) for c in fleet.calls)


async def test_cache_invalidated_after_command(svc, fleet):
    await svc.climate_status(V)
    await svc.start_climate(V, confirm=True)
    r = await svc.climate_status(V)
    assert r["climate"]["is_climate_on"] is True
    assert len(_paths(fleet, "/vehicle_data")) == 2


async def test_command_refused_when_key_not_paired(svc, fleet):
    fleet.paired.discard(V)
    r = await svc.start_climate(V, confirm=True)
    assert r["sent"] is False and "pair the key first" in r["message"].lower()
    assert not _paths(fleet, "/command/")
    r = await svc.send_destination(V, "Springfield, IL", confirm=True)
    assert r["sent"] is False and not _paths(fleet, "/command/")


async def test_pairing_check_cached(fleet, client):
    t = [0.0]
    svc = TeslaService(client, clock=lambda: t[0])
    await svc.start_climate(V, confirm=True)
    await svc.stop_climate(V, confirm=True)
    assert len(_paths(fleet, "fleet_status")) == 1
    t[0] += 3601
    await svc.start_climate(V, confirm=True)
    assert len(_paths(fleet, "fleet_status")) == 2


async def test_wake_polls_free_state_instead_of_rewaking(fleet, client):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
    svc = TeslaService(client, sleep=fake_sleep)
    fleet.wake_polls_needed = 3
    r = await svc.wake_vehicle(ASLEEP_VIN, confirm=True)
    assert r["state"] == "online" and r["waited_s"] == 10
    assert len(_paths(fleet, "/wake_up")) == 1 and sleeps == [5, 5]


async def test_wake_gives_up_after_timeout_without_rewaking(fleet, client):
    async def fake_sleep(s):
        pass
    svc = TeslaService(client, sleep=fake_sleep)
    fleet.wake_polls_needed = 10_000
    r = await svc.wake_vehicle(ASLEEP_VIN, confirm=True)
    assert r["state"] != "online" and r["waited_s"] == 60
    assert len(_paths(fleet, "/wake_up")) == 1


async def test_wake_skipped_when_online(svc, fleet):
    r = await svc.wake_vehicle(V, confirm=True)
    assert r["sent"] is False and not _paths(fleet, "/wake_up")


async def test_climate_actions_batch_after_one_wake(fleet, client):
    async def fake_sleep(s):
        pass
    svc = TeslaService(client, sleep=fake_sleep)
    fleet.wake_polls_needed = 2
    acts = [{"action": "start_climate"}, {"action": "set_temps", "driver_temp_c": 22},
            {"action": "set_seat_heater", "seat": "front_left", "level": 2}, {"action": "set_defrost", "on": True}]
    # climate-ON batch on a sleeping car with unknown battery: refused by the safety guard, car NOT woken
    r = await svc.climate_actions(ASLEEP_VIN, acts, wake_if_asleep=True, confirm=True)
    assert r["sent"] is False and r["refused"] is True and "rather than waking" in r["safety"]["reason"]
    assert not _paths(fleet, "/wake_up") and not _paths(fleet, "/command/")
    # turning things off is never guarded: one wake, then all steps
    offs = [{"action": "stop_climate"}, {"action": "set_seat_heater", "seat": "front_left", "level": 0},
            {"action": "set_defrost", "on": False}, {"action": "set_steering_wheel_heater", "on": False}]
    r = await svc.climate_actions(ASLEEP_VIN, offs, wake_if_asleep=True, confirm=True)
    assert r["sent"] is True and all(x["sent"] for x in r["results"])
    assert len(_paths(fleet, "/wake_up")) == 1 and len(_paths(fleet, "/command/")) == 4
    assert r["cost_usd"] == pytest.approx(0.02 + 0.004)


async def test_climate_actions_asleep_without_wake_flag_sends_nothing(svc, fleet):
    r = await svc.climate_actions(ASLEEP_VIN, [{"action": "start_climate"}], confirm=True)
    assert r["sent"] is False and not _paths(fleet, "/wake_up") and not _paths(fleet, "/command/")


async def test_send_destination_address(svc, fleet):
    r = await svc.send_destination(V, "Central Market, Springfield, IL", confirm=True)
    assert r["sent"] is True
    call = _paths(fleet, "/command/navigation_request")[-1]
    assert call[1].startswith("proxy.mock/")
    assert call[2]["type"] == "share_ext_content_raw"
    assert call[2]["value"]["android.intent.extra.TEXT"] == "Central Market, Springfield, IL"
    assert isinstance(call[2]["timestamp_ms"], str)


async def test_send_destination_coords(svc, fleet):
    await svc.send_destination(V, "Home", latitude=42.280800, longitude=-83.74300, confirm=True)
    call = _paths(fleet, "/command/navigation_request")[-1]
    assert call[2]["value"]["android.intent.extra.TEXT"] == "42.280800,-83.743000"


async def test_send_destination_preview_cost(svc, fleet):
    r = await svc.send_destination(V, "Springfield, IL")
    assert r["preview"] is True and r["estimated_cost_usd"] == pytest.approx(0.001) and not _sends(fleet)


# ---------- daily_check / charge_check ----------

async def test_daily_check_online(svc, fleet):
    r = await svc.daily_check(V, battery_threshold_pct=80)
    att = " | ".join(r["needs_attention"])
    assert r["asleep"] is False and r["woke_car"] is False
    assert r["new_alert_count"] == 2 and "12V battery" in att
    assert "TPMS soft warning on RL" in att and "Battery 78% is below 80%" in att
    assert len(_paths(fleet, "/vehicle_data")) == 1 and not _paths(fleet, "/wake_up")
    # second run: alerts already seen, data served from cache (no new paid call)
    r2 = await svc.daily_check(V, battery_threshold_pct=80)
    assert r2["new_alert_count"] == 0 and r2["live_data_source"] == "cache"
    assert len(_paths(fleet, "/vehicle_data")) == 1


async def test_daily_check_tire_low_vs_recommended(svc, fleet):
    vs = fleet.data["vehicle_state"]
    vs.update(tpms_soft_warning_rl=False, tpms_pressure_fl=2.6, tpms_pressure_rl=2.8)  # 37.7 psi vs 42.1
    r = await svc.daily_check(V)
    att = " | ".join(r["needs_attention"])
    assert "Tire FL" in att and "Tire RL" not in att


async def test_daily_check_wifi_wait(svc, fleet):
    fleet.data["vehicle_state"]["software_update"]["status"] = "downloading_wifi_wait"
    r = await svc.daily_check(V)
    assert any("waiting on Wi-Fi" in x for x in r["needs_attention"])


async def test_daily_check_asleep_never_wakes_or_pays(svc, fleet, client):
    r = await svc.daily_check(ASLEEP_VIN)
    assert r["asleep"] is True and r["live_data_source"] == "none"
    assert not _paths(fleet, "/wake_up") and not _paths(fleet, "/vehicle_data")
    assert client.estimated_cost_usd() == 0
    assert fleet.states[ASLEEP_VIN] == "asleep"


async def test_daily_check_free_only_when_disabled(svc, fleet, client):
    await svc.daily_check(V, use_vehicle_data=False)
    assert client.estimated_cost_usd() == 0


async def test_charge_check(svc, fleet):
    r = await svc.charge_check(V, threshold_pct=90)
    assert r["below_threshold"] is True and r["needs_charge"] is True and r["plugged_in"] is False
    r = await svc.charge_check(V, threshold_pct=50)
    assert r["needs_charge"] is False
    assert len(_paths(fleet, "/vehicle_data")) == 1


async def test_charge_check_asleep(svc, fleet):
    r = await svc.charge_check(ASLEEP_VIN)
    assert r["needs_charge"] is None and not _paths(fleet, "/wake_up") and not _paths(fleet, "/vehicle_data")


async def test_weekday_recurring_precondition_schedule(svc, fleet):
    p = await svc.add_precondition_schedule(V, "07:30", ["WEEKDAYS"], 42.28, -83.74)
    assert p["preview"] is True and p["body"]["days_of_week"] == "WEEKDAYS" and p["body"]["one_time"] is False
    assert p["body"]["enabled"] is True and p["body"]["precondition_time"] == 450 and not _sends(fleet)
    r = await svc.add_precondition_schedule(V, "07:30", ["mon", "tue", "wed", "thu", "fri"], 42.28, -83.74,
                                            confirm=True)
    assert r["sent"] is True
    body = _paths(fleet, "/command/add_precondition_schedule")[-1][2]
    assert body["days_of_week"] == "MON,TUES,WED,THURS,FRI" and body["enabled"] is True
