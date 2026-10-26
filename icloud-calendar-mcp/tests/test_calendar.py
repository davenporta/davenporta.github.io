import datetime as dt
import inspect

import icalendar
import pytest

from icloud_calendar_mcp import calendar as ic
from icloud_calendar_mcp.cli import run
from icloud_calendar_mcp.server import build_server

TZ = ic.ZoneInfo("Etc/GMT+5")
ic.TZ = TZ
ENV = {"ICLOUD_APPLE_ID": "someone@example.com", "ICLOUD_APP_PASSWORD": "abcd-efgh-ijkl-mnop"}
NOW = dt.datetime(2026, 10, 23, 13, 0, tzinfo=TZ)  # Friday


def _ical(*vevents: str) -> icalendar.Calendar:
    body = "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//t//t//EN", *vevents, "END:VCALENDAR"])
    return icalendar.Calendar.from_ical(body)


DENTIST = ("BEGIN:VEVENT\r\nUID:d1\r\nSUMMARY:Dentist\r\nDTSTART;TZID=Etc/GMT+5:20261026T081500\r\n"
           "DTEND;TZID=Etc/GMT+5:20261026T090000\r\nLOCATION:Smile Dental\\n100 Main St\\, Springfield\\, IL\r\nEND:VEVENT")
STANDUP = ("BEGIN:VEVENT\r\nUID:s1\r\nSUMMARY:Standup\r\nDTSTART:20261019T140000Z\r\nDTEND:20261019T141500Z\r\n"
           "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR\r\nEND:VEVENT")
HOLIDAY = "BEGIN:VEVENT\r\nUID:h1\r\nSUMMARY:Day off\r\nDTSTART;VALUE=DATE:20261026\r\nDTEND;VALUE=DATE:20261027\r\nEND:VEVENT"
CANCELLED = ("BEGIN:VEVENT\r\nUID:c1\r\nSUMMARY:Gone\r\nSTATUS:CANCELLED\r\nDTSTART:20261026T150000Z\r\n"
             "DTEND:20261026T160000Z\r\nEND:VEVENT")


class Obj:
    def __init__(self, cal):
        self.icalendar_instance = cal


class FakeCal:
    def __init__(self, name, comps, objs, fail=False):
        self.name, self.comps, self.objs, self.fail = name, comps, objs, fail
        self.searches = []

    def get_display_name(self):
        return self.name

    def get_supported_components(self):
        return self.comps

    def search(self, **kw):
        self.searches.append(kw)
        if self.fail:
            raise RuntimeError("boom")
        return self.objs


class FakeClient:
    def __init__(self, cals):
        self.cals = cals
        self.env_seen = None

    def principal(self):
        return self

    def calendars(self):
        return self.cals


def _factory(cals):
    def f(env):
        assert env["ICLOUD_APPLE_ID"] and env["ICLOUD_APP_PASSWORD"]
        return FakeClient(cals)
    return f


def _cals():
    return [FakeCal("Reminders", ["VTODO"], []),
            FakeCal("Home", ["VEVENT"], [Obj(_ical(DENTIST)), Obj(_ical(STANDUP)), Obj(_ical(HOLIDAY)),
                                         Obj(_ical(CANCELLED))])]


def test_events_expanded_local_tz():
    cals = _cals()
    r = ic.calendar_events("2026-10-26", env=ENV, client_factory=_factory(cals), now=NOW)
    assert r["ok"] and r["calendars"] == ["Home"]  # VTODO-only calendar skipped
    assert not cals[0].searches
    titles = [e["title"] for e in r["events"]]
    assert titles == ["Day off", "Dentist", "Standup"]  # all-day first, cancelled dropped
    dentist = r["events"][1]
    assert dentist["start"] == "2026-10-26T08:15:00-05:00" and dentist["all_day"] is False
    assert dentist["location"] == "Smile Dental 100 Main St, Springfield, IL" and dentist["calendar"] == "Home"
    standup = r["events"][2]
    assert standup["start"] == "2026-10-26T09:00:00-05:00"  # 14:00Z recurring instance = 09:00 CDT
    assert r["events"][0]["all_day"] is True and r["events"][0]["start"] == "2026-10-26"


def test_window_hours_and_start_hour():
    r = ic.calendar_events("2026-10-26", hours=2, start_hour=8, env=ENV, client_factory=_factory(_cals()), now=NOW)
    assert r["window"]["start"] == "2026-10-26T08:00:00-05:00" and r["window"]["end"] == "2026-10-26T10:00:00-05:00"
    assert [e["title"] for e in r["events"]] == ["Day off", "Dentist", "Standup"]
    r = ic.calendar_events("2026-10-26", hours=0.5, start_hour=10, env=ENV, client_factory=_factory(_cals()), now=NOW)
    assert [e["title"] for e in r["events"]] == ["Day off"]


@pytest.mark.parametrize("day,expect", [("today", "2026-10-23"), ("tomorrow", "2026-10-24"),
                                        ("next_weekday", "2026-10-26"), ("2026-12-01", "2026-12-01")])
def test_parse_day(day, expect):
    assert ic.parse_day(day, NOW).isoformat() == expect


def test_bad_day_and_hours():
    assert ic.calendar_events("someday", env=ENV, client_factory=_factory([]))["ok"] is False
    assert ic.calendar_events("today", hours=-1, env=ENV, client_factory=_factory([]))["ok"] is False


def test_missing_env_degrades_gracefully():
    called = []
    r = ic.calendar_events("today", env={}, client_factory=lambda e: called.append(1))
    assert r["ok"] is False and r["configured"] is False and "ICLOUD_APP_PASSWORD" in r["error"] and not called


def test_auth_error_reports_type_not_secret():
    class AuthorizationError(Exception):
        pass

    def f(env):
        raise AuthorizationError(f"bad creds for {env['ICLOUD_APPLE_ID']} / {env['ICLOUD_APP_PASSWORD']}")
    r = ic.calendar_events("today", env=ENV, client_factory=f)
    assert r["ok"] is False and "AuthorizationError" in r["error"]
    assert ENV["ICLOUD_APPLE_ID"] not in str(r) and ENV["ICLOUD_APP_PASSWORD"] not in str(r)


def test_one_broken_calendar_does_not_hide_others():
    cals = _cals() + [FakeCal("Work", ["VEVENT"], [], fail=True)]
    r = ic.calendar_events("2026-10-26", env=ENV, client_factory=_factory(cals), now=NOW)
    assert r["ok"] and r["errors"] == [{"calendar": "Work", "error": "RuntimeError"}] and r["count"] == 3


def test_module_is_read_only():
    from icloud_calendar_mcp import cli, server
    src = "".join(inspect.getsource(m) for m in (ic, cli, server))
    for bad in ("save_event", "add_event", ".save(", ".delete(", "save_todo", "make_calendar", ".put(", "PROPPATCH"):
        assert bad not in src


def test_date_range():
    r = ic.calendar_events("2026-10-25", end_day="2026-10-27", env=ENV, client_factory=_factory(_cals()), now=NOW)
    assert r["ok"] and r["end_day"] == "2026-10-27"
    assert r["window"]["start"] == "2026-10-25T00:00:00-05:00" and r["window"]["end"] == "2026-10-28T00:00:00-05:00"
    assert ic.calendar_events("2026-10-27", end_day="2026-10-25", env=ENV, client_factory=_factory([]))["ok"] is False
    assert ic.calendar_events("2026-10-01", end_day="2026-12-01", env=ENV, client_factory=_factory([]))["ok"] is False


def test_list_calendars():
    r = ic.list_calendars(env=ENV, client_factory=_factory(_cals()))
    assert r["ok"] and r["calendars"] == [{"name": "Reminders", "has_events": False}, {"name": "Home", "has_events": True}]
    assert ic.list_calendars(env={})["configured"] is False


def test_cli_events_and_list():
    code, out = run(["events", "--day", "2026-10-26", "--count-only"], env=ENV, client_factory=_factory(_cals()))
    assert code == 0 and out == {"ok": True, "day": "2026-10-26", "count": 3}
    code, out = run(["list"], env=ENV, client_factory=_factory(_cals()))
    assert code == 0 and out["count"] == 2
    code, out = run(["events"], env={})
    assert code == 2 and out["configured"] is False


async def test_mcp_tools_list_and_calls():
    mcp = build_server(env=ENV, client_factory=_factory(_cals()))
    names = {t.name for t in await mcp.list_tools()}
    assert names == {"list_calendars", "calendar_events"}
    r = await mcp.call_tool("calendar_events", {"day": "2026-10-26"})
    assert "Dentist" in str(r) and ENV["ICLOUD_APP_PASSWORD"] not in str(r)
    r = await mcp.call_tool("list_calendars", {})
    assert "Home" in str(r)


async def test_mcp_without_env():
    mcp = build_server(env={})
    r = await mcp.call_tool("calendar_events", {"day": "today"})
    assert "not configured" in str(r)


def test_run_script_exits_clearly_without_env():
    import os
    import subprocess
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_mcp.sh"
    env = {k: v for k, v in os.environ.items() if not k.startswith("ICLOUD_")}
    p = subprocess.run([str(script)], env=env, capture_output=True, text=True, timeout=20)
    assert p.returncode == 1 and p.stdout == ""
    assert "ICLOUD_APPLE_ID" in p.stderr and "ICLOUD_APP_PASSWORD" in p.stderr
