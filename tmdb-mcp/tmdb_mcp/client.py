"""TMDB v3 API client and streaming subscription analysis logic.

Strictly read-only. Never logs or returns API keys or bearer tokens.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any, Callable

import httpx

BASE_URL = "https://api.themoviedb.org/3"
USER_AGENT = "tmdb-mcp/0.1 (personal-assistant)"
DEFAULT_REGION = "US"

# Canonical service name aliases for subscription matching
CANONICAL_SERVICES: dict[str, str] = {
    "apple tv+": "Apple TV+",
    "apple tv plus": "Apple TV+",
    "apple tv": "Apple TV+",
    "max": "Max",
    "hbo max": "Max",
    "max amazon channel": "Max",
    "hulu": "Hulu",
    "disney plus": "Disney+",
    "disney+": "Disney+",
    "amazon prime video": "Prime Video",
    "amazon prime video with ads": "Prime Video",
    "prime video": "Prime Video",
    "peacock": "Peacock",
    "peacock premium": "Peacock",
    "peacock premium plus": "Peacock",
    "paramount plus": "Paramount+",
    "paramount+": "Paramount+",
    "paramount+ with showtime": "Paramount+",
    "paramount plus apple tv channel": "Paramount+",
    "paramount+ amazon channel": "Paramount+",
    "netflix": "Netflix",
    "netflix standard with ads": "Netflix",
    "crunchyroll": "Crunchyroll",
    "amc+": "AMC+",
    "amc plus": "AMC+",
    "starz": "Starz",
    "mgm+": "MGM+",
    "mgm plus": "MGM+",
    "britbox": "BritBox",
    "acorn tv": "Acorn TV",
}


def canonical_service_name(name: str) -> str:
    """Normalize provider names (e.g., 'Disney Plus' -> 'Disney+', 'Apple TV Plus' -> 'Apple TV+')."""
    cleaned = name.strip()
    low = cleaned.lower()
    if low in CANONICAL_SERVICES:
        return CANONICAL_SERVICES[low]
    for prefix, canon in (
        ("apple tv+", "Apple TV+"),
        ("apple tv plus", "Apple TV+"),
        ("paramount+", "Paramount+"),
        ("paramount plus", "Paramount+"),
        ("peacock", "Peacock"),
        ("disney+", "Disney+"),
        ("disney plus", "Disney+"),
        ("amazon prime video", "Prime Video"),
        ("hbo max", "Max"),
    ):
        if low.startswith(prefix):
            return canon
    return cleaned


def _load_config_env(env: dict[str, str] | None = None) -> dict[str, str]:
    merged: dict[str, str] = {}
    cfg_dir = Path((env or os.environ).get("TMDB_CONFIG_DIR", Path.home() / ".config" / "tmdb-mcp"))
    env_file = cfg_dir / "env"
    if env is None and env_file.is_file():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    v = v.strip().strip("'").strip('"')
                    merged[k.strip()] = v
        except OSError:
            pass
    source = os.environ if env is None else env
    for k, v in source.items():
        if v is not None:
            merged[k] = str(v)
    return merged


def resolve_auth(env: dict[str, str] | None = None) -> tuple[str | None, str | None, str]:
    """Return (auth_mode, credential, region).

    auth_mode is 'bearer', 'api_key', or None if unconfigured.
    Auto-detects v4 Bearer Read Access Token if it starts with 'eyJ' or len > 64,
    or if supplied via TMDB_READ_TOKEN.
    """
    cfg = _load_config_env(env)
    region = (cfg.get("TMDB_REGION") or DEFAULT_REGION).strip().upper() or DEFAULT_REGION
    read_token = (cfg.get("TMDB_READ_TOKEN") or "").strip()
    api_key = (cfg.get("TMDB_API_KEY") or "").strip()

    raw = read_token or api_key
    if not raw:
        return None, None, region

    if read_token and not api_key:
        return "bearer", read_token, region

    if raw.startswith("eyJ") or len(raw) > 64:
        return "bearer", raw, region
    return "api_key", raw, region


def _not_configured_response() -> dict[str, Any]:
    return {
        "ok": False,
        "configured": False,
        "error": "TMDB_API_KEY or TMDB_READ_TOKEN is not configured. Set it in environment or ~/.config/tmdb-mcp/env (0600).",
    }


def _scrub_secret(text: str, secret: str | None) -> str:
    if not secret:
        return text
    return text.replace(secret, "[REDACTED]")


class TMDBClient:
    """Thin, synchronous httpx wrapper for TMDB v3 endpoints."""

    def __init__(
        self,
        env: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.auth_mode, self._credential, self.region = resolve_auth(env)
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.auth_mode and self._credential)

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.configured or not self._credential:
            raise RuntimeError("TMDB_API_KEY not configured")
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        qparams: dict[str, Any] = dict(params or {})
        if self.auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {self._credential}"
        else:
            qparams["api_key"] = self._credential

        url = f"{BASE_URL}/{path.lstrip('/')}"
        try:
            with httpx.Client(
                headers=headers,
                timeout=15.0,
                transport=self._transport,
                follow_redirects=True,
            ) as client:
                resp = client.get(url, params=qparams)
                if resp.status_code >= 400:
                    raise RuntimeError(f"TMDB HTTP {resp.status_code}")
                return resp.json()
        except Exception as exc:
            msg = _scrub_secret(str(exc), self._credential)
            raise RuntimeError(f"{type(exc).__name__}: {msg}") from None


ClientFactory = Callable[[dict[str, str] | None], TMDBClient]


def _get_client(env: dict[str, str] | None = None, client_factory: ClientFactory | None = None) -> TMDBClient:
    if client_factory is not None:
        return client_factory(env)
    return TMDBClient(env=env)


def _format_provider_list(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not items:
        return []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in sorted(items, key=lambda x: x.get("display_priority", 999)):
        raw_name = item.get("provider_name") or ""
        if not raw_name:
            continue
        canon = canonical_service_name(raw_name)
        key = canon.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "provider_id": item.get("provider_id"),
            "name": canon,
            "raw_name": raw_name,
            "display_priority": item.get("display_priority"),
        })
    return out


def extract_region_providers(raw_results: dict[str, Any] | None, region: str = DEFAULT_REGION) -> dict[str, Any]:
    """Extract and normalize JustWatch streaming providers for a region."""
    reg = (region or DEFAULT_REGION).upper()
    block = (raw_results or {}).get(reg) or {}
    flatrate = _format_provider_list(block.get("flatrate"))
    free = _format_provider_list(block.get("free"))
    ads = _format_provider_list(block.get("ads"))
    rent = _format_provider_list(block.get("rent"))
    buy = _format_provider_list(block.get("buy"))
    return {
        "region": reg,
        "link": block.get("link"),
        "subscription": [p["name"] for p in flatrate],
        "flatrate": flatrate,
        "free": [p["name"] for p in free],
        "ads": [p["name"] for p in ads],
        "rent": [p["name"] for p in rent],
        "buy": [p["name"] for p in buy],
    }


def search_shows(
    query: str,
    year: int | None = None,
    include_movies: bool = False,
    env: dict[str, str] | None = None,
    client_factory: ClientFactory | None = None,
) -> dict[str, Any]:
    """Search TV shows (and optionally movies) on TMDB."""
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "query must not be empty"}
    client = _get_client(env, client_factory)
    if not client.configured:
        return _not_configured_response()

    try:
        tv_params: dict[str, Any] = {"query": q, "include_adult": "false", "language": "en-US", "page": 1}
        if year is not None:
            tv_params["first_air_date_year"] = int(year)
        tv_data = client.get("/search/tv", tv_params)
        results: list[dict[str, Any]] = []
        for item in (tv_data.get("results") or [])[:15]:
            results.append({
                "id": item.get("id"),
                "media_type": "tv",
                "name": item.get("name") or item.get("original_name"),
                "first_air_date": item.get("first_air_date"),
                "overview": item.get("overview"),
                "popularity": item.get("popularity"),
                "vote_average": item.get("vote_average"),
                "origin_country": item.get("origin_country") or [],
            })

        if include_movies:
            mv_params: dict[str, Any] = {"query": q, "include_adult": "false", "language": "en-US", "page": 1}
            if year is not None:
                mv_params["year"] = int(year)
            mv_data = client.get("/search/movie", mv_params)
            for item in (mv_data.get("results") or [])[:10]:
                results.append({
                    "id": item.get("id"),
                    "media_type": "movie",
                    "name": item.get("title") or item.get("original_title"),
                    "first_air_date": item.get("release_date"),
                    "overview": item.get("overview"),
                    "popularity": item.get("popularity"),
                    "vote_average": item.get("vote_average"),
                    "origin_country": [],
                })

        return {
            "ok": True,
            "query": q,
            "count": len(results),
            "results": results,
        }
    except Exception as exc:
        return {"ok": False, "error": _scrub_secret(str(exc), client._credential)}


def _format_episode_brief(ep: dict[str, Any] | None) -> dict[str, Any] | None:
    if not ep:
        return None
    return {
        "id": ep.get("id"),
        "name": ep.get("name"),
        "season_number": ep.get("season_number"),
        "episode_number": ep.get("episode_number"),
        "air_date": ep.get("air_date"),
        "episode_type": ep.get("episode_type"),
        "runtime": ep.get("runtime"),
    }


def tv_show_details(
    tv_id: int,
    region: str | None = None,
    env: dict[str, str] | None = None,
    client_factory: ClientFactory | None = None,
) -> dict[str, Any]:
    """Return full TV show status, next/last episode air dates, seasons, and embedded watch/providers."""
    client = _get_client(env, client_factory)
    if not client.configured:
        return _not_configured_response()
    reg = (region or client.region or DEFAULT_REGION).upper()

    try:
        data = client.get(f"/tv/{int(tv_id)}", {"append_to_response": "watch/providers", "language": "en-US"})
        wp_raw = (data.get("watch/providers") or {}).get("results") or {}
        providers = extract_region_providers(wp_raw, reg)

        seasons_out = []
        regular_seasons = []
        for s in data.get("seasons") or []:
            snum = s.get("season_number")
            entry = {
                "season_number": snum,
                "name": s.get("name"),
                "episode_count": s.get("episode_count"),
                "air_date": s.get("air_date"),
                "vote_average": s.get("vote_average"),
            }
            seasons_out.append(entry)
            if snum and snum > 0:
                regular_seasons.append(entry)

        last_ep = _format_episode_brief(data.get("last_episode_to_air"))
        next_ep = _format_episode_brief(data.get("next_episode_to_air"))
        status = data.get("status")
        in_production = bool(data.get("in_production"))

        if next_ep is not None:
            airing_state = "currently_airing"
        elif last_ep is not None:
            airing_state = "season_complete_ready_to_binge"
        else:
            airing_state = "unreleased"

        return {
            "ok": True,
            "id": data.get("id", int(tv_id)),
            "name": data.get("name"),
            "status": status,
            "in_production": in_production,
            "airing_state": airing_state,
            "first_air_date": data.get("first_air_date"),
            "last_air_date": data.get("last_air_date"),
            "number_of_seasons": data.get("number_of_seasons"),
            "number_of_episodes": data.get("number_of_episodes"),
            "networks": [n.get("name") for n in (data.get("networks") or []) if n.get("name")],
            "last_episode_to_air": last_ep,
            "next_episode_to_air": next_ep,
            "seasons": seasons_out,
            "latest_season_number": regular_seasons[-1]["season_number"] if regular_seasons else None,
            "watch_providers": providers,
        }
    except Exception as exc:
        return {"ok": False, "error": _scrub_secret(str(exc), client._credential)}


def watch_providers(
    title_or_id: str | int,
    media_type: str = "tv",
    region: str = DEFAULT_REGION,
    env: dict[str, str] | None = None,
    client_factory: ClientFactory | None = None,
) -> dict[str, Any]:
    """Return streaming services carrying a TV show or movie in `region`."""
    client = _get_client(env, client_factory)
    if not client.configured:
        return _not_configured_response()
    reg = (region or client.region or DEFAULT_REGION).upper()
    mtype = (media_type or "tv").strip().lower()
    if mtype not in ("tv", "movie"):
        return {"ok": False, "error": "media_type must be 'tv' or 'movie'"}

    try:
        resolved_id: int | None = None
        resolved_title: str | None = None
        if isinstance(title_or_id, int) or (isinstance(title_or_id, str) and title_or_id.strip().isdigit()):
            resolved_id = int(str(title_or_id).strip())
        else:
            query_str = str(title_or_id).strip()
            if not query_str:
                return {"ok": False, "error": "title_or_id must not be empty"}
            search_data = client.get(
                f"/search/{mtype}",
                {"query": query_str, "include_adult": "false", "language": "en-US", "page": 1},
            )
            hits = search_data.get("results") or []
            if not hits:
                return {"ok": False, "error": f"No {mtype} match found for '{query_str}'"}
            top = hits[0]
            resolved_id = int(top["id"])
            resolved_title = top.get("name") or top.get("title")

        if mtype == "tv":
            details = client.get(f"/tv/{resolved_id}", {"append_to_response": "watch/providers", "language": "en-US"})
            resolved_title = details.get("name") or resolved_title
            wp_raw = (details.get("watch/providers") or {}).get("results") or {}
            providers = extract_region_providers(wp_raw, reg)
            networks = [n.get("name") for n in (details.get("networks") or []) if n.get("name")]

            # Check latest season provider breakdown if available
            seasons = [s for s in (details.get("seasons") or []) if (s.get("season_number") or 0) > 0]
            latest_season_providers = None
            if seasons:
                latest_snum = seasons[-1]["season_number"]
                try:
                    s_wp = client.get(f"/tv/{resolved_id}/season/{latest_snum}/watch/providers")
                    s_reg = extract_region_providers(s_wp.get("results") or {}, reg)
                    latest_season_providers = {
                        "season_number": latest_snum,
                        "subscription": s_reg["subscription"],
                        "rent": s_reg["rent"],
                        "buy": s_reg["buy"],
                    }
                except Exception:
                    latest_season_providers = None

            return {
                "ok": True,
                "id": resolved_id,
                "media_type": "tv",
                "title": resolved_title,
                "networks": networks,
                "region": reg,
                "providers": providers,
                "latest_season_providers": latest_season_providers,
            }
        else:
            details = client.get(f"/movie/{resolved_id}", {"append_to_response": "watch/providers", "language": "en-US"})
            resolved_title = details.get("title") or resolved_title
            wp_raw = (details.get("watch/providers") or {}).get("results") or {}
            providers = extract_region_providers(wp_raw, reg)
            return {
                "ok": True,
                "id": resolved_id,
                "media_type": "movie",
                "title": resolved_title,
                "region": reg,
                "providers": providers,
            }
    except Exception as exc:
        return {"ok": False, "error": _scrub_secret(str(exc), client._credential)}


def season_schedule(
    tv_id: int,
    season_number: int | None = None,
    env: dict[str, str] | None = None,
    client_factory: ClientFactory | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Fetch episode-by-episode air dates for a season and compute binge readiness."""
    client = _get_client(env, client_factory)
    if not client.configured:
        return _not_configured_response()
    ref_date = today or dt.date.today()

    try:
        show_name: str | None = None
        target_season = season_number
        if target_season is None:
            show_data = client.get(f"/tv/{int(tv_id)}", {"language": "en-US"})
            show_name = show_data.get("name")
            regular = [
                s for s in (show_data.get("seasons") or [])
                if (s.get("season_number") or 0) > 0
            ]
            if not regular:
                return {"ok": False, "error": f"No regular seasons found for tv_id={tv_id}"}
            # Prefer latest season that has an air_date or episodes, or the last regular season
            chosen = regular[-1]
            for s in reversed(regular):
                if s.get("air_date") or (s.get("episode_count") or 0) > 0:
                    chosen = s
                    break
            target_season = int(chosen["season_number"])

        sdata = client.get(f"/tv/{int(tv_id)}/season/{int(target_season)}", {"language": "en-US"})
        episodes_raw = sdata.get("episodes") or []
        episodes_out: list[dict[str, Any]] = []
        aired_count = 0
        unaired_count = 0
        next_unaired: dict[str, Any] | None = None

        for ep in episodes_raw:
            air_str = ep.get("air_date")
            aired = False
            if air_str:
                try:
                    ep_date = dt.date.fromisoformat(air_str)
                    aired = ep_date <= ref_date
                except ValueError:
                    aired = False
            if aired:
                aired_count += 1
            else:
                unaired_count += 1

            ep_entry = {
                "episode_number": ep.get("episode_number"),
                "name": ep.get("name"),
                "air_date": air_str,
                "aired": aired,
                "episode_type": ep.get("episode_type"),
                "runtime": ep.get("runtime"),
            }
            episodes_out.append(ep_entry)
            if not aired and next_unaired is None:
                next_unaired = ep_entry

        finale_air_date = episodes_out[-1]["air_date"] if episodes_out else None
        total_eps = len(episodes_out)
        ready_to_binge = total_eps > 0 and unaired_count == 0 and aired_count == total_eps

        if ready_to_binge:
            season_status = "ready_to_binge"
        elif aired_count > 0 and unaired_count > 0:
            season_status = "currently_airing"
        else:
            season_status = "upcoming"

        return {
            "ok": True,
            "tv_id": int(tv_id),
            "show_name": show_name,
            "season_number": int(target_season),
            "season_name": sdata.get("name"),
            "season_air_date": sdata.get("air_date"),
            "total_episodes": total_eps,
            "aired_episodes_count": aired_count,
            "remaining_unaired_count": unaired_count,
            "ready_to_binge": ready_to_binge,
            "season_status": season_status,
            "finale_air_date": finale_air_date,
            "next_unaired_episode": next_unaired,
            "episodes": episodes_out,
        }
    except Exception as exc:
        return {"ok": False, "error": _scrub_secret(str(exc), client._credential)}


def subscription_plan_check(
    shows: list[str],
    current_subscriptions: list[str] | None = None,
    region: str = DEFAULT_REGION,
    env: dict[str, str] | None = None,
    client_factory: ClientFactory | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Map a list of show titles to streaming services and season binge/airing readiness."""
    if not shows:
        return {"ok": False, "error": "shows list must not be empty"}
    client = _get_client(env, client_factory)
    if not client.configured:
        return _not_configured_response()
    reg = (region or client.region or DEFAULT_REGION).upper()

    by_service: dict[str, list[dict[str, Any]]] = {}
    shows_report: list[dict[str, Any]] = []
    currently_airing: list[str] = []
    ready_to_binge: list[str] = []
    between_seasons_or_ended: list[str] = []
    unresolved: list[str] = []

    for raw_title in shows:
        title_q = (raw_title or "").strip()
        if not title_q:
            continue
        srch = search_shows(title_q, env=env, client_factory=client_factory)
        if not srch.get("ok") or not srch.get("results"):
            unresolved.append(title_q)
            continue
        top = srch["results"][0]
        tv_id = int(top["id"])
        det = tv_show_details(tv_id, region=reg, env=env, client_factory=client_factory)
        if not det.get("ok"):
            unresolved.append(title_q)
            continue

        sched = None
        latest_snum = det.get("latest_season_number")
        if latest_snum:
            sched = season_schedule(
                tv_id,
                season_number=latest_snum,
                env=env,
                client_factory=client_factory,
                today=today,
            )

        subs = det["watch_providers"]["subscription"]
        show_name = det.get("name") or top.get("name") or title_q
        s_ready = bool(sched and sched.get("ready_to_binge"))
        s_status = (sched or {}).get("season_status") or det.get("airing_state")
        finale_date = (sched or {}).get("finale_air_date")
        rem_unaired = (sched or {}).get("remaining_unaired_count")

        if det.get("next_episode_to_air") or s_status == "currently_airing":
            bucket = "currently_airing"
            currently_airing.append(show_name)
        elif s_ready:
            bucket = "ready_to_binge"
            ready_to_binge.append(show_name)
        else:
            bucket = "between_seasons_or_upcoming"
            between_seasons_or_ended.append(show_name)

        entry = {
            "query": title_q,
            "id": tv_id,
            "name": show_name,
            "status": det.get("status"),
            "latest_season": latest_snum,
            "bucket": bucket,
            "ready_to_binge": s_ready,
            "remaining_unaired_count": rem_unaired,
            "finale_air_date": finale_date,
            "last_air_date": det.get("last_air_date"),
            "next_episode_to_air": det.get("next_episode_to_air"),
            "subscription_services": subs,
            "networks": det.get("networks") or [],
            "rent_or_buy_only": not subs and bool(det["watch_providers"]["rent"] or det["watch_providers"]["buy"]),
        }
        shows_report.append(entry)

        for svc in subs:
            by_service.setdefault(svc, []).append({
                "name": show_name,
                "bucket": bucket,
                "ready_to_binge": s_ready,
                "finale_air_date": finale_date,
            })

    # Compare against current_subscriptions if provided
    normalized_current = [canonical_service_name(s) for s in (current_subscriptions or []) if s and s.strip()]
    keep_active: list[dict[str, Any]] = []
    can_pause_or_drop: list[str] = []
    missing_subscriptions: list[dict[str, Any]] = []

    for svc in normalized_current:
        matched_shows = by_service.get(svc, [])
        if matched_shows:
            keep_active.append({"service": svc, "shows": matched_shows, "show_count": len(matched_shows)})
        else:
            can_pause_or_drop.append(svc)

    current_set = {s.lower() for s in normalized_current}
    for svc, svc_shows in sorted(by_service.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if svc.lower() not in current_set:
            missing_subscriptions.append({"service": svc, "shows": svc_shows, "show_count": len(svc_shows)})

    return {
        "ok": True,
        "region": reg,
        "shows": shows_report,
        "by_service": by_service,
        "summary": {
            "currently_airing": currently_airing,
            "ready_to_binge": ready_to_binge,
            "between_seasons_or_upcoming": between_seasons_or_ended,
            "unresolved": unresolved,
        },
        "subscription_recommendations": {
            "current_subscriptions": normalized_current,
            "active_used_subscriptions": keep_active,
            "subscriptions_with_zero_tracked_shows": can_pause_or_drop,
            "services_needed_for_other_shows": missing_subscriptions,
        },
    }
