import pytest

from tesla_mcp.cli import run
from tesla_mcp.mock import ASLEEP_VIN, MOCK_VIN as V


def _sent(fleet):
    return [c for c in fleet.calls if "/command/" in c[1] or "/wake_up" in c[1]]


async def test_cli_daily_check_default_vin(svc, fleet):
    code, out = await run(["daily_check", "--battery-threshold", "20"], svc)
    assert out["vin"] == V and code == 1 and out["needs_attention"]  # mock has new alerts + TPMS warning
    assert not _sent(fleet)


async def test_cli_charge_check_asleep_free_only(svc, fleet, client):
    code, out = await run(["--vin", ASLEEP_VIN, "charge_check", "--free-only"], svc)
    assert code == 0 and out["needs_charge"] is None and client.estimated_cost_usd() == 0


async def test_cli_preview_never_sends(svc, fleet):
    code, out = await run(["--vin", V, "preview", "set_temps", "--args", '{"driver_temp_c": 21}'], svc)
    assert code == 0 and out["preview"] is True and not _sent(fleet)
    code, out = await run(["--vin", V, "preview", "add_precondition_schedule", "--args",
                           '{"time_hhmm":"07:30","days":["WEEKDAYS"],"latitude":42.28,"longitude":-83.74}'], svc)
    assert out["preview"] is True and not _sent(fleet)


@pytest.mark.parametrize("args", ['{"confirm": true}', '{"wake_if_asleep": true, "actions": []}', '[1]'])
async def test_cli_rejects_confirm(svc, fleet, args):
    code, out = await run(["--vin", V, "preview", "climate_actions", "--args", args], svc)
    assert code == 2 and "error" in out and fleet.calls == []


def test_cli_cannot_preview_forbidden():
    with pytest.raises(SystemExit):
        import asyncio
        asyncio.run(run(["--vin", V, "preview", "door_unlock"], None))
