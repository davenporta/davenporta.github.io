"""Safety guards for climate/preconditioning: low battery and "car isn't at home/work".

Enforced in code (TeslaService.command / climate_actions / add_precondition_schedule / daily_check), not just in
prompts. stop_climate, turning things OFF and remove_precondition_schedule are never blocked. Charging commands are
not affected here because they are not on the endpoint allowlist at all.

Config (env, read at call time):
  TESLA_MIN_CLIMATE_SOC   (default 20)  battery % below which climate-on / preconditioning is REFUSED
  TESLA_WARN_CLIMATE_SOC  (default 30)  battery % below which it is allowed with a warning
  TESLA_HOME_LATLON / TESLA_WORK_LATLON  "lat,lon" (user-specific: keep them out of git; alternatively put
                          {"home": [lat, lon], "work": [lat, lon], "radius_km": 1} in ~/.config/tesla-mcp/places.json,
                          or TESLA_PLACES_FILE)
  TESLA_PLACE_RADIUS_KM   (default 1)   radius around configured home/work
  TESLA_REQUIRE_LOCATION  (default 0)   1 = refuse when the car's location is unknown (e.g. no vehicle_location scope)
Home/work coordinates live in ~/.config/tesla-mcp/places.json (mode 0600, out of git) or TESLA_HOME_LATLON.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

DEFAULT_MIN_SOC, DEFAULT_WARN_SOC = 20, 30
DEFAULT_PLACE_RADIUS_KM = 1.0


def _env() -> dict:
    return os.environ


def soc_thresholds(env=None) -> tuple[float, float]:
    e = env if env is not None else _env()
    lo = float(e.get("TESLA_MIN_CLIMATE_SOC") or DEFAULT_MIN_SOC)
    hi = float(e.get("TESLA_WARN_CLIMATE_SOC") or DEFAULT_WARN_SOC)
    return lo, max(lo, hi)


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def _parse_latlon(v: Any) -> tuple[float, float] | None:
    if v is None or v == "":
        return None
    try:
        if isinstance(v, str):
            lat, lon = (float(x) for x in v.split(","))
        else:
            lat, lon = float(v[0]), float(v[1])
    except (ValueError, TypeError, IndexError):
        return None
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def places_file(env=None) -> Path:
    e = env if env is not None else _env()
    return Path(e.get("TESLA_PLACES_FILE") or Path.home() / ".config" / "tesla-mcp" / "places.json").expanduser()


def load_places(env=None) -> dict[str, Any]:
    """{'places': {'home': (lat, lon), 'work': ...}, 'radius_km': float, 'source': ...}. Env beats places.json."""
    e = env if env is not None else _env()
    data: dict = {}
    try:
        data = json.loads(places_file(e).read_text())
    except (OSError, ValueError):
        pass
    places, src = {}, set()
    for name in ("home", "work"):
        p = _parse_latlon(e.get(f"TESLA_{name.upper()}_LATLON"))
        if p:
            places[name] = p
            src.add("env")
        elif _parse_latlon(data.get(name)):
            places[name] = _parse_latlon(data.get(name))
            src.add("places.json")
    radius = float(e.get("TESLA_PLACE_RADIUS_KM") or data.get("radius_km") or DEFAULT_PLACE_RADIUS_KM)
    return {"places": places, "radius_km": radius, "source": "+".join(sorted(src)) or "unconfigured"}


def classify_location(lat: Any, lon: Any, env=None) -> dict[str, Any]:
    """Is (lat, lon) within the configured home/work radius (from env or ~/.config/tesla-mcp/places.json)?"""
    p = _parse_latlon((lat, lon)) if lat is not None and lon is not None else None
    if p is None:
        return {"known": False, "at_home_or_work": None, "place": None}
    cfg = load_places(env)
    if cfg["places"]:
        dists = {n: haversine_km(p, c) for n, c in cfg["places"].items()}
        name = min(dists, key=dists.get)
        ok = dists[name] <= cfg["radius_km"]
        return {"known": True, "at_home_or_work": ok, "place": name if ok else "away",
                "nearest": name, "distance_km": round(dists[name], 2), "radius_km": cfg["radius_km"],
                "places_source": cfg["source"]}
    return {"known": True, "at_home_or_work": None, "place": "unconfigured", "nearest": None,
            "distance_km": None, "radius_km": cfg["radius_km"],
            "places_source": "unconfigured (set TESLA_HOME_LATLON or ~/.config/tesla-mcp/places.json)"}


def evaluate(d: dict | None, *, car_state: str | None = None, env=None) -> dict[str, Any]:
    """Decide whether climate-on / preconditioning is allowed, from (cached or fresh) vehicle_data `d`.
    Returns {allowed, reason, warnings, battery_level, location}. Never makes network calls."""
    e = env if env is not None else _env()
    lo, hi = soc_thresholds(e)
    out: dict[str, Any] = {"allowed": False, "warnings": [], "min_soc": lo, "warn_soc": hi}
    if d is None:
        out.update(battery_level=None, location={"known": False, "at_home_or_work": None},
                   reason=("Battery level unknown because the car is asleep/offline and there is no fresh cached data; "
                           "refusing rather than waking the car just to check." if car_state != "online" else
                           "Battery level unknown (no vehicle data); refusing."))
        return out
    lvl = (d.get("charge_state") or {}).get("battery_level")
    out["battery_level"] = lvl
    ds = d.get("drive_state") or {}
    loc = classify_location(ds.get("latitude"), ds.get("longitude"), e)
    out["location"] = loc
    if not isinstance(lvl, (int, float)):
        out["reason"] = "Battery level not reported; refusing climate/preconditioning."
        return out
    if lvl < lo:
        out["reason"] = f"Battery {lvl}% is below the {lo:g}% minimum for climate/preconditioning; refused."
        return out
    if loc["known"] and loc["at_home_or_work"] is False:
        out["reason"] = (f"Car is not at home/work (nearest {loc.get('nearest')} is {loc.get('distance_km')} km away, "
                         f"limit {loc.get('radius_km'):g} km); preconditioning refused.")
        return out
    if loc["known"] and loc["at_home_or_work"] is None:
        msg = "Home/work location is not configured (set TESLA_HOME_LATLON or ~/.config/tesla-mcp/places.json)."
        if str(e.get("TESLA_REQUIRE_LOCATION") or "").lower() in ("1", "true", "yes"):
            out["reason"] = msg + " TESLA_REQUIRE_LOCATION=1, so refused."
            return out
        out["warnings"].append(msg)
    if not loc["known"]:
        msg = ("Car location unknown (the token lacks the vehicle_location scope, or location was not returned), "
               "so the at-home/work check could not run.")
        if str(e.get("TESLA_REQUIRE_LOCATION") or "").lower() in ("1", "true", "yes"):
            out["reason"] = msg + " TESLA_REQUIRE_LOCATION=1, so refused."
            return out
        out["warnings"].append(msg)
    if lvl < hi:
        out["warnings"].append(f"Battery is low ({lvl}%, under {hi:g}%): climate will use noticeable range.")
    out["allowed"] = True
    out["reason"] = "ok"
    return out
