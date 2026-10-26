"""Read-only trip planning for "should I offer to precondition for the next commitment?".

Free services only: Nominatim/OSM geocoding (cached in ~/.config/tesla-mcp/geocache.json, outside the repo; at most
1 request/s per Nominatim policy) and local haversine x 1.3 at ~55 mph routing by default (live vehicle GPS is never sent
to external OSRM servers unless TESLA_MCP_ALLOW_EXTERNAL_ROUTING=1 is explicitly set).
Never talks to the car; never sends commands.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

from .guards import haversine_km

USER_AGENT = "tesla-mcp/0.1 (personal-assistant)"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OSRM = "https://router.project-osrm.org/route/v1/driving/{o_lon},{o_lat};{d_lon},{d_lat}"
KM_PER_MI = 1.609344
def _load_tz() -> ZoneInfo:
    tz_name = os.environ.get("TESLA_MCP_TZ") or os.environ.get("TZ")
    if not tz_name:
        tz_file = Path(os.environ.get("TESLA_CONFIG_DIR", Path.home() / ".config/tesla-mcp")) / "timezone"
        if tz_file.is_file():
            tz_name = tz_file.read_text().strip()
    return ZoneInfo(tz_name or "UTC")


TZ = _load_tz()
_nominatim_lock = threading.Lock()
_last_nominatim = [0.0]


def geocache_file() -> Path:
    return Path(os.environ.get("TESLA_GEOCACHE_FILE") or Path.home() / ".config/tesla-mcp/geocache.json").expanduser()


def _load_cache(f: Path) -> dict:
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return {}


def _save_cache(f: Path, data: dict) -> None:
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(f)


def _norm(q: str) -> str:
    return " ".join(q.split()).strip().lower()


def geocode(query: str, http: httpx.Client | None = None, cache_file: Path | None = None) -> dict | None:
    """{'lat', 'lon', 'display_name', 'source'} or None. Cached (hits and misses) so each address is looked up once."""
    key = _norm(query)
    if not key:
        return None
    f = cache_file or geocache_file()
    cache = _load_cache(f)
    if key in cache:
        hit = cache[key]
        return {**hit, "source": "cache"} if hit else None
    candidates = [query]
    if "\n" in query or "," in query:  # Apple puts "Venue\nStreet, City" - retry without the venue line
        parts = [p.strip() for p in query.replace("\n", ",").split(",") if p.strip()]
        if len(parts) > 2:
            candidates.append(", ".join(parts[1:]))
    own = http is None
    http = http or httpx.Client(timeout=15, headers={"User-Agent": USER_AGENT})
    result = None
    try:
        for q in candidates:
            with _nominatim_lock:
                wait = 1.1 - (time.monotonic() - _last_nominatim[0])
                if wait > 0:
                    time.sleep(wait)
                r = http.get(NOMINATIM, params={"q": " ".join(q.split()), "format": "jsonv2", "limit": 1},
                             headers={"User-Agent": USER_AGENT})
                _last_nominatim[0] = time.monotonic()
            r.raise_for_status()
            js = r.json()
            if js:
                result = {"lat": float(js[0]["lat"]), "lon": float(js[0]["lon"]),
                          "display_name": js[0].get("display_name")}
                break
    finally:
        if own:
            http.close()
    cache[key] = result
    _save_cache(f, cache)
    return {**result, "source": "nominatim"} if result else None


def route(origin: tuple[float, float], dest: tuple[float, float], http: httpx.Client | None = None) -> dict:
    """Driving distance/time. Local haversine x 1.3 at 55 mph by default so live vehicle GPS is never sent to
    third-party routing servers unless TESLA_MCP_ALLOW_EXTERNAL_ROUTING=1 is explicitly set."""
    allow_ext = os.environ.get("TESLA_MCP_ALLOW_EXTERNAL_ROUTING", "").strip().lower() in ("1", "true", "yes")
    if allow_ext:
        own = http is None
        http = http or httpx.Client(timeout=10, headers={"User-Agent": USER_AGENT})
        try:
            r = http.get(OSRM.format(o_lat=origin[0], o_lon=origin[1], d_lat=dest[0], d_lon=dest[1]),
                         params={"overview": "false"}, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            js = r.json()
            if js.get("code") == "Ok" and js.get("routes"):
                rt = js["routes"][0]
                return {"distance_mi": round(rt["distance"] / 1000 / KM_PER_MI, 1),
                        "drive_min": round(rt["duration"] / 60), "source": "osrm"}
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        finally:
            if own:
                http.close()
    mi = haversine_km(origin, dest) * 1.3 / KM_PER_MI
    return {"distance_mi": round(mi, 1), "drive_min": round(mi / 55 * 60), "source": "estimate (haversine x1.3, 55 mph)"}


def parse_arrival(value: str) -> dt.datetime:
    """ISO 8601 arrival time; a time without an offset is treated as the configured local timezone."""
    try:
        t = dt.datetime.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError("arrival_time must be an ISO 8601 date-time, e.g. 2026-10-26T08:30 or "
                         "2026-10-26T08:30:00-05:00") from None
    return (t.replace(tzinfo=TZ) if t.tzinfo is None else t).astimezone(TZ)


def build_destination(arrival_time: str, destination: str | None = None, latitude: float | None = None,
                      longitude: float | None = None, title: str | None = None) -> dict:
    """Normalise caller-supplied trip input (from any calendar or the user) into the dict plan() expects."""
    has_ll = latitude is not None or longitude is not None
    if has_ll:
        if latitude is None or longitude is None:
            raise ValueError("give both latitude and longitude")
        lat, lon = float(latitude), float(longitude)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("latitude/longitude out of range")
    elif not (destination and str(destination).strip()):
        raise ValueError("give a destination address or latitude+longitude")
    ev: dict[str, Any] = {"title": title, "start": parse_arrival(arrival_time).isoformat(),
                          "location": " ".join(str(destination).split()) if destination else None}
    if has_ll:
        ev["lat"], ev["lon"] = lat, lon
    return ev


def plan(event: dict, origin: tuple[float, float], *, battery_level: Any, battery_range_mi: Any, guard: dict,
         geocode_fn: Callable = geocode, route_fn: Callable = route, env=None) -> dict[str, Any]:
    e = env if env is not None else os.environ
    max_min = float(e.get("TESLA_MAX_PRECOND_DRIVE_MIN") or 90)
    margin = float(e.get("TESLA_RANGE_MARGIN_MI") or 20)
    buffer_min = float(e.get("TESLA_TRIP_BUFFER_MIN") or 10)
    reasons: list[str] = []
    out: dict[str, Any] = {"trip": {"title": event.get("title"), "arrival_time": event.get("start"),
                                    "destination": event.get("location")}}
    if event.get("lat") is not None and event.get("lon") is not None:
        g = {"lat": event["lat"], "lon": event["lon"], "display_name": event.get("location"), "source": "caller"}
    else:
        g = geocode_fn(event["location"])
    if not g:
        out.update(reasonable=False, reasons=[f"Could not geocode the destination: {event['location']!r}."])
        return out
    dest = (g["lat"], g["lon"])
    out["destination"] = {"lat": round(dest[0], 5), "lon": round(dest[1], 5), "name": g.get("display_name"),
                          "geocoder": g.get("source")}
    rt = route_fn(origin, dest)
    out.update(distance_mi=rt["distance_mi"], drive_min=rt["drive_min"], route_source=rt["source"])
    start = dt.datetime.fromisoformat(event["start"])
    leave = start - dt.timedelta(minutes=rt["drive_min"] + buffer_min)
    out["suggested_leave_time"] = leave.isoformat(timespec="minutes")
    out["suggested_precondition_start"] = (leave - dt.timedelta(minutes=15)).isoformat(timespec="minutes")
    ok = True
    if not guard.get("allowed"):
        ok = False
        reasons.append(guard.get("reason") or "safety guard refused")
    reasons += guard.get("warnings") or []
    if rt["drive_min"] > max_min:
        ok = False
        reasons.append(f"Drive is ~{rt['drive_min']} min, over the {max_min:g}-min limit for offering preconditioning.")
    need = 1.5 * rt["distance_mi"] + margin
    out["range_needed_mi"] = round(need, 1)
    if not isinstance(battery_range_mi, (int, float)):
        ok = False
        reasons.append("Estimated range unknown (no fresh vehicle data; car not woken).")
    elif battery_range_mi < need:
        ok = False
        reasons.append(f"Range {battery_range_mi:.0f} mi is under 1.5x trip + {margin:g} mi margin ({need:.0f} mi).")
    out["battery_level"] = battery_level
    out["battery_range_mi"] = battery_range_mi
    if ok:
        reasons.insert(0, "Reasonable: car is at a known/local place (or location unknown, see warnings), drive time "
                          "and range are fine, battery above the climate minimum.")
    out["reasonable"] = ok
    out["reasons"] = reasons
    return out
