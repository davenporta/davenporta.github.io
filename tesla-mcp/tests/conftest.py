import pytest

from tesla_mcp.client import FleetClient
from tesla_mcp.config import Settings
from tesla_mcp.mock import MockFleet
from tesla_mcp.service import TeslaService


@pytest.fixture
def fleet():
    return MockFleet()


@pytest.fixture
def settings(tmp_path):
    return Settings.from_env({"TESLA_MOCK": "1", "TESLA_TOKEN_FILE": str(tmp_path / "t" / "tokens.json")})


@pytest.fixture
async def client(fleet, settings):
    c = FleetClient(settings, transport=fleet.transport())
    yield c
    await c.aclose()


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TESLA_MCP_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture
def svc(client):
    return TeslaService(client)


@pytest.fixture(autouse=True)
def _places(tmp_path, monkeypatch):
    # Mock car (drive_state 42.2808,-83.7430) counts as "home" in tests; real places live outside the repo.
    monkeypatch.setenv("TESLA_PLACES_FILE", str(tmp_path / "places.json"))
    monkeypatch.setenv("TESLA_HOME_LATLON", "42.2808,-83.7430")
    monkeypatch.setenv("TESLA_MCP_TZ", "Etc/GMT+5")
    from zoneinfo import ZoneInfo
    from tesla_mcp import trip
    monkeypatch.setattr(trip, "TZ", ZoneInfo("Etc/GMT+5"))
    for k in ("TESLA_WORK_LATLON", "TESLA_MIN_CLIMATE_SOC", "TESLA_WARN_CLIMATE_SOC", "TESLA_REQUIRE_LOCATION",
              "TESLA_PLACE_RADIUS_KM"):
        monkeypatch.delenv(k, raising=False)
