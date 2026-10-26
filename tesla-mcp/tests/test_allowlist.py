import pytest

from tesla_mcp.client import (ALLOWED_ENDPOINTS, CLIMATE_COMMANDS, NAV_COMMANDS, PRICE_USD, EndpointNotAllowed,
                              FleetClient, match_endpoint)
from tesla_mcp.config import Settings
from tesla_mcp.mock import MOCK_VIN

V = MOCK_VIN
FORBIDDEN = ["door_unlock", "door_lock", "actuate_trunk", "honk_horn", "flash_lights", "remote_start_drive",
             "window_control", "charge_port_door_open", "charge_port_door_close", "set_sentry_mode", "charge_start",
             "set_valet_mode", "speed_limit_deactivate", "trigger_homelink", "media_toggle_playback", "sun_roof_control",
             "set_vehicle_name", "erase_user_data", "reset_valet_pin", "set_pin_to_drive",
             # other navigation endpoints stay blocked; only navigation_request is allowed
             "navigation_gps_request", "navigation_sc_request", "navigation_waypoints_request", "share",
             "remote_boombox", "guest_mode", "set_climate_keeper_mode", "set_bioweapon_mode", "adjust_volume",
             "schedule_software_update", "cancel_software_update", "set_charge_limit", "speed_limit_activate"]


@pytest.mark.parametrize("cmd", FORBIDDEN)
def test_forbidden_commands_rejected(cmd):
    with pytest.raises(EndpointNotAllowed):
        match_endpoint("POST", f"/api/1/vehicles/{V}/command/{cmd}")


@pytest.mark.parametrize("path", [
    f"/api/1/vehicles/{V}/command/auto_conditioning_start/../door_unlock",
    f"/api/1/vehicles/{V}//command/door_unlock",
    f"/api/1/vehicles/{V}/command/auto_conditioning_start?x=1",
    f"/api/1/vehicles/{V}/command/auto_conditioning_start#",
    "/api/1/users/me", "/api/1/partner_accounts", "/api/1/vehicles/BADVIN/vehicle_data",
    f"/api/1/vehicles/{V}/command/AUTO_CONDITIONING_START", "https://evil.example/api/1/vehicles",
])
def test_malformed_or_unlisted_paths_rejected(path):
    with pytest.raises(EndpointNotAllowed):
        match_endpoint("POST", path)
    with pytest.raises(EndpointNotAllowed):
        match_endpoint("GET", path)


def test_wrong_method_rejected():
    with pytest.raises(EndpointNotAllowed):
        match_endpoint("GET", f"/api/1/vehicles/{V}/command/auto_conditioning_start")
    with pytest.raises(EndpointNotAllowed):
        match_endpoint("DELETE", f"/api/1/vehicles/{V}")


def test_allowlist_is_climate_only():
    allowed = set(CLIMATE_COMMANDS)
    assert allowed == {"auto_conditioning_start", "auto_conditioning_stop", "set_temps", "remote_seat_heater_request",
                       "remote_steering_wheel_heater_request", "set_preconditioning_max",
                       "add_precondition_schedule", "remove_precondition_schedule"}


async def test_client_blocks_before_network(client, fleet):
    with pytest.raises(EndpointNotAllowed):
        await client.request("POST", f"/api/1/vehicles/{V}/command/door_unlock")
    assert fleet.calls == []


async def test_disallowed_query_params(client, fleet):
    with pytest.raises(EndpointNotAllowed):
        await client.request("GET", f"/api/1/vehicles/{V}/vehicle_data", params={"foo": "bar"})
    assert fleet.calls == []


async def test_commands_require_proxy(tmp_path, fleet):
    s = Settings.from_env({"TESLA_TOKEN_FILE": str(tmp_path / "t.json")})  # not mock, no proxy
    c = FleetClient(s, transport=fleet.transport())
    with pytest.raises(EndpointNotAllowed, match="proxy"):
        await c.request("POST", f"/api/1/vehicles/{V}/command/auto_conditioning_start")
    assert fleet.calls == []
    await c.aclose()


def test_proxy_must_be_https():
    with pytest.raises(ValueError):
        Settings.from_env({"TESLA_PROXY_URL": "http://localhost:4443"})


async def test_commands_go_to_proxy_host(client, fleet):
    await client.request("POST", f"/api/1/vehicles/{V}/command/auto_conditioning_start", json={})
    assert fleet.calls[-1][1].startswith("proxy.mock/")


def test_only_navigation_request_added():
    assert NAV_COMMANDS == ("navigation_request",)
    ep = match_endpoint("POST", f"/api/1/vehicles/{V}/command/navigation_request")
    assert ep.kind == "command" and ep.pricing == "command"
    cmds = {e.pattern.pattern.rsplit("/", 1)[-1].rstrip("$") for e in ALLOWED_ENDPOINTS if e.kind == "command"}
    assert cmds == set(CLIMATE_COMMANDS) | {"navigation_request"}


@pytest.mark.parametrize("path", [
    f"/api/1/vehicles/{V}/command/navigation_request/../door_unlock",
    f"/api/1/vehicles/{V}/command/navigation_requestx",
    f"/api/1/vehicles/{V}/command/navigation_request/extra",
])
def test_navigation_path_tricks_rejected(path):
    with pytest.raises(EndpointNotAllowed):
        match_endpoint("POST", path)


def test_navigation_get_rejected():
    with pytest.raises(EndpointNotAllowed):
        match_endpoint("GET", f"/api/1/vehicles/{V}/command/navigation_request")


@pytest.mark.parametrize("method,path,price", [
    ("GET", "/api/1/vehicles", 0.0), ("GET", f"/api/1/vehicles/{V}", 0.0),
    ("GET", f"/api/1/vehicles/{V}/recent_alerts", 0.0), ("GET", f"/api/1/vehicles/{V}/service_data", 0.0),
    ("GET", f"/api/1/vehicles/{V}/release_notes", 0.0), ("POST", "/api/1/vehicles/fleet_status", 0.0),
    ("GET", f"/api/1/vehicles/{V}/vehicle_data", 0.002), ("POST", f"/api/1/vehicles/{V}/wake_up", 0.02),
    ("POST", f"/api/1/vehicles/{V}/command/set_temps", 0.001),
    ("POST", f"/api/1/vehicles/{V}/command/navigation_request", 0.001),
])
def test_cost_table(method, path, price):
    assert PRICE_USD[match_endpoint(method, path).pricing] == pytest.approx(price)


async def test_ephemeral_proxy_lifecycle(monkeypatch, tmp_path):
    import subprocess
    from tesla_mcp.config import Settings
    from tesla_mcp.client import FleetClient

    terminated = []
    class DummyProc:
        def __init__(self, cmd, **kw):
            self.cmd = cmd
            self.stderr = None
        def poll(self):
            return 0 if terminated else None
        def terminate(self):
            terminated.append(True)
        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: DummyProc(cmd, **kw))
    import asyncio
    class DummyWriter:
        def close(self): pass
        async def wait_closed(self): pass
    async def fake_open_conn(h, pt):
        return None, DummyWriter()
    monkeypatch.setattr(asyncio, "open_connection", fake_open_conn)

    s = Settings(
        client_id="cid",
        client_secret="csec",
        region="na",
        token_file=tmp_path / "tok.json",
        proxy_url="https://127.0.0.1:4443",
        proxy_ca_cert=None,
        mock=False,
        state_dir=tmp_path / "state",
    )
    c = FleetClient(s, transport=None)
    try:
        async with c._ephemeral_proxy():
            assert not terminated
        assert terminated == [True]
    finally:
        await c.aclose()
