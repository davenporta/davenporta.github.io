import datetime as dt
import json

import httpx
import pytest

from tesla_mcp import trip
from tesla_mcp.trip import TZ
from tesla_mcp.cli import run
from tesla_mcp.mock import ASLEEP_VIN, MOCK_VIN as V
from tesla_mcp.server import build_server

NOW = dt.datetime(2026, 10, 26, 6, 45, tzinfo=TZ)
DEST = {"lat": 42.30, "lon": -83.70, "display_name": "Somewhere"}


def _sends(fleet):
    return [c for c in fleet.calls if "/command/" in c[1] or "/wake_up" in c[1]]


ARR = "2026-10-26T08:30:00-05:00"
DENT = {"arrival_time": ARR, "destination": "100 Main St, Springfield, IL", "title": "Dentist"}


@pytest.fixture
def tsvc(svc):
    svc.now_fn = lambda: NOW
    svc.geocode_fn = lambda q: DEST
    svc.route_fn = lambda o, d: {"distance_mi": 12.0, "drive_min": 20, "source": "test"}
    return svc


async def test_reasonable_trip(tsvc, fleet):
    r = await tsvc.next_trip_check(V, **DENT)
    assert r["trip"]["title"] == "Dentist" and r["reasonable"] is True
    assert r["suggested_leave_time"] == "2026-10-26T08:00-05:00"          # 08:30 - 20 min - 10 min buffer
    assert r["suggested_precondition_start"] == "2026-10-26T07:45-05:00"  # leave - 15
    assert r["origin"] == "car" and r["distance_mi"] == 12.0 and "offer" in r
    assert not _sends(fleet) and r["commands_sent"] == 0


async def test_too_far(tsvc, fleet):
    tsvc.route_fn = lambda o, d: {"distance_mi": 150.0, "drive_min": 140, "source": "test"}
    r = await tsvc.next_trip_check(V, **DENT)
    assert r["reasonable"] is False and any("90-min limit" in x for x in r["reasons"])


async def test_drive_limit_env(tsvc, monkeypatch):
    monkeypatch.setenv("TESLA_MAX_PRECOND_DRIVE_MIN", "15")
    assert (await tsvc.next_trip_check(V, **DENT))["reasonable"] is False


async def test_insufficient_range(tsvc, fleet):
    fleet.data["charge_state"]["battery_range"] = 30.0  # need 1.5*12 + 20 = 38
    r = await tsvc.next_trip_check(V, **DENT)
    assert r["reasonable"] is False and r["range_needed_mi"] == 38.0 and any("Range 30" in x for x in r["reasons"])


async def test_low_battery(tsvc, fleet):
    fleet.data["charge_state"]["battery_level"] = 15
    r = await tsvc.next_trip_check(V, **DENT)
    assert r["reasonable"] is False and any("below the 20%" in x for x in r["reasons"])


async def test_car_away(tsvc, fleet):
    fleet.data["drive_state"].update(latitude=41.9742, longitude=-87.9073)
    r = await tsvc.next_trip_check(V, **DENT)
    assert r["reasonable"] is False and r["at_home_or_work"] is False


async def test_asleep_not_woken(tsvc, fleet):
    r = await tsvc.next_trip_check(ASLEEP_VIN, **DENT)
    assert r["reasonable"] is False and r["woke_car"] is False and not _sends(fleet)
    assert not [c for c in fleet.calls if "/vehicle_data" in c[1]]


async def test_unknown_location_uses_home_origin(tsvc, fleet):
    del fleet.data["drive_state"]["latitude"], fleet.data["drive_state"]["longitude"]
    seen = {}
    tsvc.route_fn = lambda o, d: seen.setdefault("o", o) and {"distance_mi": 12.0, "drive_min": 20, "source": "t"}
    r = await tsvc.next_trip_check(V, **DENT)
    assert r["origin"].startswith("configured home") and seen["o"] == (42.2808, -83.743)
    assert r["reasonable"] is True and any("location unknown" in x for x in r["reasons"])


async def test_latlon_skips_geocoder(tsvc):
    tsvc.geocode_fn = lambda q: pytest.fail("geocoder must not be called for lat/lon input")
    r = await tsvc.next_trip_check(V, ARR, latitude=42.33, longitude=-83.05)
    assert r["reasonable"] is True and r["destination"]["geocoder"] == "caller"


async def test_naive_arrival_is_central(tsvc):
    r = await tsvc.next_trip_check(V, "2026-10-26T08:30", "Detroit, MI")
    assert r["trip"]["arrival_time"] == ARR and r["suggested_leave_time"] == "2026-10-26T08:00-05:00"
    assert "Detroit" in r["offer"]


async def test_past_arrival(tsvc, fleet):
    r = await tsvc.next_trip_check(V, "2026-10-26T06:00:00-05:00", "Old place")
    assert r["reasonable"] is False and "passed" in r["reasons"][0] and fleet.calls == []


@pytest.mark.parametrize("kw", [{}, {"latitude": 42.3}, {"destination": "  "}, {"latitude": 99, "longitude": 0}])
async def test_bad_input(tsvc, kw):
    with pytest.raises(ValueError):
        await tsvc.next_trip_check(V, ARR, **kw)
    with pytest.raises(ValueError):
        await tsvc.next_trip_check(V, "tomorrow-ish", "X")


async def test_geocode_fail(tsvc):
    tsvc.geocode_fn = lambda q: None
    r = await tsvc.next_trip_check(V, **DENT)
    assert r["reasonable"] is False and "geocode" in r["reasons"][0]


async def test_cli_and_mcp(tsvc, fleet):
    code, out = await run(["--vin", V, "next_trip", "--arrival-time", ARR, "--destination", "100 Main St, Springfield, IL",
                           "--title", "Dentist"], tsvc)
    assert code == 0 and out["trip"]["title"] == "Dentist"
    mcp = build_server(lambda: tsvc)
    r = await mcp.call_tool("next_trip_check", {"vin": V, **DENT})
    assert "Dentist" in str(r) and not _sends(fleet)


# ---------- geocode / route with mocked HTTP ----------

def test_geocode_caches_and_sets_user_agent(tmp_path):
    seen = []

    def h(req):
        seen.append(req)
        if "Smile" in req.url.params["q"]:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[{"lat": "42.28", "lon": "-83.74", "display_name": "100 Main St"}])
    http = httpx.Client(transport=httpx.MockTransport(h))
    f = tmp_path / "geo.json"
    g = trip.geocode("Smile Dental\n100 Main St, Springfield, IL", http, f)  # venue line dropped on retry
    assert g["lat"] == 42.28 and g["source"] == "nominatim" and len(seen) == 2
    assert all("tesla-mcp" in r.headers["user-agent"] for r in seen)
    g2 = trip.geocode("smile dental  100 main st, springfield, il", http, f)
    assert g2["source"] == "cache" and len(seen) == 2
    assert (f.stat().st_mode & 0o777) == 0o600 and json.loads(f.read_text())


def test_route_local_default_and_osrm_when_opted_in(monkeypatch):
    called = []
    ok = httpx.Client(transport=httpx.MockTransport(lambda r: (
        called.append(1),
        httpx.Response(200, json={"code": "Ok", "routes": [{"distance": 32186.9, "duration": 1500}]})
    )[1]))
    # Default: local haversine calculation, never calls external OSRM server
    monkeypatch.delenv("TESLA_MCP_ALLOW_EXTERNAL_ROUTING", raising=False)
    r_def = trip.route((42.28, -83.74), (42.18, -83.45), ok)
    assert r_def["source"].startswith("estimate") and 15 < r_def["distance_mi"] < 25 and called == []

    # Opt-in: calls OSRM and falls back on HTTP error
    monkeypatch.setenv("TESLA_MCP_ALLOW_EXTERNAL_ROUTING", "1")
    r = trip.route((42.28, -83.74), (42.18, -83.45), ok)
    assert r == {"distance_mi": 20.0, "drive_min": 25, "source": "osrm"} and len(called) == 1
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    r2 = trip.route((42.28, -83.74), (42.18, -83.45), bad)
    assert r2["source"].startswith("estimate") and 15 < r2["distance_mi"] < 25
