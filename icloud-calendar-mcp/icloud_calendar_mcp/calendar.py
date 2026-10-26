"""Read-only iCloud (CalDAV) calendar access (strictly read-only).

Credentials come only from the environment: ICLOUD_APPLE_ID and ICLOUD_APP_PASSWORD (an Apple app-specific
password). They are never logged, returned, or written anywhere. This module only issues CalDAV discovery and
REPORT/search requests; there are deliberately no create, edit, or delete code paths.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo


def _load_tz() -> ZoneInfo:
    tz_name = os.environ.get("ICLOUD_CALENDAR_TZ") or os.environ.get("TZ")
    if not tz_name:
        tz_file = Path(os.environ.get("ICLOUD_CONFIG_DIR", Path.home() / ".config/icloud-calendar-mcp")) / "timezone"
        if tz_file.is_file():
            tz_name = tz_file.read_text().strip()
    return ZoneInfo(tz_name or "UTC")


CALDAV_URL = "https://caldav.icloud.com"
TZ = _load_tz()
ENV_USER, ENV_PASS = "ICLOUD_APPLE_ID", "ICLOUD_APP_PASSWORD"


class CalendarNotConfigured(RuntimeError):
    pass


def _default_client_factory(env: dict[str, str]):
    import caldav  # imported lazily so the server starts and reports "not configured" without it
    return caldav.DAVClient(url=CALDAV_URL, username=env[ENV_USER], password=env[ENV_PASS], timeout=20)


def parse_day(day: str | None, now: dt.datetime | None = None) -> dt.date:
    now = (now or dt.datetime.now(TZ)).astimezone(TZ)
    d = (day or "today").strip().lower()
    if d == "today":
        return now.date()
    if d == "tomorrow":
        return now.date() + dt.timedelta(days=1)
    if d in ("next_weekday", "next-weekday"):
        n = now.date() + dt.timedelta(days=1)
        while n.weekday() >= 5:
            n += dt.timedelta(days=1)
        return n
    try:
        return dt.date.fromisoformat(d)
    except ValueError:
        raise ValueError("day must be 'today', 'tomorrow', 'next_weekday' or an ISO date (YYYY-MM-DD)") from None


def window(day: dt.date, start_hour: float = 0, hours: float | None = None) -> tuple[dt.datetime, dt.datetime]:
    if not (0 <= start_hour < 24):
        raise ValueError("start_hour must be in [0, 24)")
    start = dt.datetime.combine(day, dt.time(0), TZ) + dt.timedelta(hours=start_hour)
    if hours is None:
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), TZ)
    else:
        if not (0 < hours <= 24 * 14):
            raise ValueError("hours must be in (0, 336]")
        end = start + dt.timedelta(hours=hours)
    return start, end


def _to_local(v: Any) -> tuple[str, bool]:
    if isinstance(v, dt.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=TZ)  # floating time: treat as local
        return v.astimezone(TZ).isoformat(), False
    if isinstance(v, dt.date):
        return v.isoformat(), True
    return str(v), False


def _event_dict(comp, cal_name: str) -> dict[str, Any]:
    start_raw = comp.get("DTSTART").dt if comp.get("DTSTART") is not None else None
    end_raw = comp.get("DTEND").dt if comp.get("DTEND") is not None else None
    if end_raw is None and start_raw is not None and comp.get("DURATION") is not None:
        end_raw = start_raw + comp.get("DURATION").dt
    start, all_day = _to_local(start_raw) if start_raw is not None else (None, False)
    end = _to_local(end_raw)[0] if end_raw is not None else None
    loc = comp.get("LOCATION")
    loc = " ".join(str(loc).split()) if loc else None  # Apple puts address lines on separate lines
    return {"title": str(comp.get("SUMMARY") or "(no title)"), "start": start, "end": end, "all_day": all_day,
            "location": loc, "calendar": cal_name}


def _supports_events(cal) -> bool:
    try:
        comps = cal.get_supported_components()
    except Exception:
        return True
    return not comps or "VEVENT" in comps


def _expand(icals: Iterable, start: dt.datetime, end: dt.datetime):
    import recurring_ical_events
    for ical in icals:
        yield from recurring_ical_events.of(ical).between(start, end)


def _client(env: dict[str, str] | None, client_factory: Callable | None):
    env = dict(os.environ if env is None else env)
    missing = [k for k in (ENV_USER, ENV_PASS) if not env.get(k)]
    if missing:
        raise CalendarNotConfigured(f"iCloud calendar is not configured: set {', '.join(missing)} "
                                    "(Apple ID email + app-specific password) in the server environment.")
    return (client_factory or _default_client_factory)(env)


def _error(ex: Exception) -> dict[str, Any]:
    """Error result that names only the exception type, never its message (which could echo credentials)."""
    if isinstance(ex, CalendarNotConfigured):
        return {"ok": False, "configured": False, "error": str(ex)}
    name = type(ex).__name__
    hint = " (check the Apple ID / app-specific password)" if "auth" in name.lower() else ""
    return {"ok": False, "error": f"iCloud calendar read failed: {name}{hint}"}


def list_calendars(env: dict[str, str] | None = None, client_factory: Callable | None = None) -> dict[str, Any]:
    """Names of the user's iCloud calendars and whether each holds events (Reminders/VTODO-only lists are flagged)."""
    try:
        cals = _client(env, client_factory).principal().calendars()
        out = []
        for cal in cals:
            out.append({"name": str(cal.get_display_name() or "(unnamed)"), "has_events": _supports_events(cal)})
    except Exception as ex:
        return _error(ex)
    return {"ok": True, "count": len(out), "calendars": out}


def fetch_events(start: dt.datetime, end: dt.datetime, env: dict[str, str] | None = None,
                 client_factory: Callable | None = None) -> dict[str, Any]:
    """Events overlapping [start, end) across all iCloud calendars, recurring events expanded, the configured local timezone."""
    calendars = _client(env, client_factory).principal().calendars()
    events, names, errors = [], [], []
    for cal in calendars:
        name = str(cal.get_display_name() or "(unnamed)")
        if not _supports_events(cal):
            continue
        names.append(name)
        try:
            objs = cal.search(start=start, end=end, event=True, expand=False)
            for comp in _expand((o.icalendar_instance for o in objs), start, end):
                if str(comp.get("STATUS") or "").upper() == "CANCELLED":
                    continue
                events.append(_event_dict(comp, name))
        except Exception as e:  # one broken calendar shouldn't hide the rest
            errors.append({"calendar": name, "error": type(e).__name__})
    events.sort(key=lambda e: (not e["all_day"], e["start"] or ""))
    out: dict[str, Any] = {"window": {"start": start.isoformat(), "end": end.isoformat(), "tz": str(TZ)},
                           "calendars": names, "count": len(events), "events": events}
    if errors:
        out["errors"] = errors
    return out


def calendar_events(day: str | None = "today", hours: float | None = None, start_hour: float = 0,
                    env: dict[str, str] | None = None, client_factory: Callable | None = None,
                    now: dt.datetime | None = None, end_day: str | None = None) -> dict[str, Any]:
    """Wrapper used by the MCP tool and CLI. Returns {"ok": False, "error": ...} instead of raising.

    Single day (default) or, with end_day, the inclusive date range day..end_day (max 31 days; hours is then ignored).
    """
    try:
        d = parse_day(day, now)
        if end_day:
            last = parse_day(end_day, now)
            if last < d:
                raise ValueError("end_day must be on or after day")
            if (last - d).days > 31:
                raise ValueError("date range is limited to 31 days")
            s, _ = window(d, start_hour)
            e = dt.datetime.combine(last + dt.timedelta(days=1), dt.time(0), TZ)
        else:
            s, e = window(d, start_hour, hours)
    except ValueError as ex:
        return {"ok": False, "error": str(ex)}
    try:
        r = fetch_events(s, e, env, client_factory)
    except Exception as ex:
        return _error(ex)
    out: dict[str, Any] = {"ok": True, "day": d.isoformat()}
    if end_day:
        out["end_day"] = last.isoformat()
    return {**out, **r}
