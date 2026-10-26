"""Apple TV export parser and local 0600 JSON store for show preferences and watch history.

All user watch history and tracked show preferences are stored exclusively in
~/.config/watch-history-mcp/store.json (directory 0700, file 0600), never inside /workspace.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

VALID_STATUSES = (
    "watching",
    "want_to_watch",
    "waiting_for_next_season",
    "completed",
    "dropped",
)

# Regex patterns for parsing combined show/season/episode strings
# Pattern 1: "Severance - S2, E1 - Hello, Ms. Cobel" or "Severance - S02E01 - Hello"
_RE_DASH_SE = re.compile(
    r"^(?P<show>.+?)\s*[-–—:]\s*S(?P<season>\d+)\s*[,.\s]*E(?:p)?\s*(?P<episode>\d+)(?:\s*[-–—:]\s*(?P<ep_title>.+))?$",
    re.IGNORECASE,
)
# Pattern 2: "Shrinking: Season 3: Episode 2" or "The Bear: Season 2, Episode 7: Forks"
_RE_SEASON_WORD = re.compile(
    r"^(?P<show>.+?)\s*[-–—:]\s*Season\s+(?P<season>\d+)(?:\s*[,:\-–—]\s*(?:Episode\s+(?P<episode>\d+))?\s*(?:[:\-–—]\s*)?(?P<ep_title>.*))?$",
    re.IGNORECASE,
)
# Pattern 3: "Slow Horses (Season 4)"
_RE_PAREN_SEASON = re.compile(
    r"^(?P<show>.+?)\s*\(\s*Season\s+(?P<season>\d+)\s*\)(?:\s*[-–—:]\s*(?P<ep_title>.+))?$",
    re.IGNORECASE,
)


def _normalize_key(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip()).lower()


def resolve_store_path(env: dict[str, str] | None = None) -> Path:
    src = os.environ if env is None else env
    explicit = src.get("WATCH_HISTORY_STORE_PATH")
    if explicit:
        p = Path(explicit).expanduser().resolve()
    else:
        cfg_dir = Path(src.get("WATCH_HISTORY_CONFIG_DIR", Path.home() / ".config" / "watch-history-mcp")).expanduser().resolve()
        p = cfg_dir / "store.json"

    # Guard against accidentally placing store.json inside /workspace
    try:
        p.relative_to(Path("/workspace"))
        raise ValueError("Refusing to store watch history inside /workspace; use ~/.config/watch-history-mcp/store.json")
    except ValueError as exc:
        if "Refusing to store" in str(exc):
            raise
    return p


def _empty_store() -> dict[str, Any]:
    return {
        "version": 1,
        "updated_at": None,
        "tracked_shows": {},
        "watch_history": {},
        "play_keys": [],
    }


def load_store(env: dict[str, str] | None = None) -> dict[str, Any]:
    path = resolve_store_path(env)
    if not path.exists():
        return _empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty_store()
        for k, default in _empty_store().items():
            data.setdefault(k, default)
        return data
    except (OSError, json.JSONDecodeError):
        return _empty_store()


def save_store(data: dict[str, Any], env: dict[str, str] | None = None) -> Path:
    path = resolve_store_path(env)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    os.chmod(parent, 0o700)

    data["updated_at"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    tmp_path = path.with_suffix(".json.tmp")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)
    os.chmod(path, 0o600)
    return path


def parse_show_and_episode(
    raw_title: str,
    container_title: str | None = None,
    season_col: str | None = None,
    episode_col: str | None = None,
) -> dict[str, Any]:
    """Extract normalized show_title, season, episode, and episode_title from Apple TV fields."""
    title = (raw_title or "").strip()
    container = (container_title or "").strip()

    season_num: int | None = None
    episode_num: int | None = None
    if season_col and str(season_col).strip().isdigit():
        season_num = int(str(season_col).strip())
    if episode_col and str(episode_col).strip().isdigit():
        episode_num = int(str(episode_col).strip())

    # If a separate container/series name is provided, prefer it as the show title
    if container and container.lower() != title.lower():
        m = _RE_DASH_SE.match(title) or _RE_SEASON_WORD.match(title)
        if m:
            if season_num is None and m.groupdict().get("season"):
                season_num = int(m.group("season"))
            if episode_num is None and m.groupdict().get("episode"):
                episode_num = int(m.group("episode"))
            ep_t = (m.groupdict().get("ep_title") or "").strip() or title
            return {
                "show_title": container,
                "season": season_num,
                "episode": episode_num,
                "episode_title": ep_t,
            }
        return {
            "show_title": container,
            "season": season_num,
            "episode": episode_num,
            "episode_title": title or None,
        }

    for pattern in (_RE_DASH_SE, _RE_SEASON_WORD, _RE_PAREN_SEASON):
        m = pattern.match(title)
        if m:
            gd = m.groupdict()
            s_val = int(gd["season"]) if gd.get("season") else season_num
            e_val = int(gd["episode"]) if gd.get("episode") else episode_num
            ep_t = (gd.get("ep_title") or "").strip() or None
            return {
                "show_title": gd["show"].strip(),
                "season": s_val,
                "episode": e_val,
                "episode_title": ep_t,
            }

    return {
        "show_title": title,
        "season": season_num,
        "episode": episode_num,
        "episode_title": None,
    }


def _parse_timestamp_to_date(raw_ts: str | None) -> str | None:
    if not raw_ts:
        return None
    s = str(raw_ts).strip()
    if not s:
        return None
    # ISO 8601 or YYYY-MM-DD...
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    # US M/D/YYYY or MM/DD/YY
    m_us = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m_us:
        mm, dd, yy = int(m_us.group(1)), int(m_us.group(2)), int(m_us.group(3))
        if yy < 100:
            yy += 2000
        try:
            return dt.date(yy, mm, dd).isoformat()
        except ValueError:
            return None
    # Epoch milliseconds
    if s.isdigit() and len(s) >= 10:
        try:
            ts = int(s)
            if ts > 10_000_000_000:
                ts = ts // 1000
            return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).date().isoformat()
        except (ValueError, OSError):
            return None
    return None


def _parse_duration_seconds(row_lower: dict[str, str]) -> float | None:
    for ms_col in (
        "play duration milliseconds",
        "playback duration milliseconds",
        "duration milliseconds",
        "total play duration milliseconds",
    ):
        val = (row_lower.get(ms_col) or "").strip()
        if val:
            try:
                return max(0.0, float(val) / 1000.0)
            except ValueError:
                pass

    for sec_col in ("play duration seconds", "duration seconds", "play duration", "duration"):
        val = (row_lower.get(sec_col) or "").strip()
        if not val:
            continue
        # Handle HH:MM:SS
        if ":" in val:
            parts = val.split(":")
            try:
                if len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                if len(parts) == 2:
                    return int(parts[0]) * 60 + float(parts[1])
            except ValueError:
                pass
        try:
            return max(0.0, float(val))
        except ValueError:
            pass
    return None


def _pick_first(row_lower: dict[str, str], keys: tuple[str, ...]) -> str:
    for k in keys:
        v = (row_lower.get(k) or "").strip()
        if v:
            return v
    return ""


def _parse_apple_csv_text(
    csv_text: str,
    source_filename: str,
    min_play_seconds: int,
) -> list[dict[str, Any]]:
    is_click_file = "click" in source_filename.lower()
    reader = csv.DictReader(io.StringIO(csv_text))
    records: list[dict[str, Any]] = []

    for row in reader:
        if not row:
            continue
        row_lower = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items() if k}

        # Filter out obvious non-video trailer/promotional rows if media type indicates trailer
        media_type = _pick_first(row_lower, ("media type", "content type", "item type", "playable type")).lower()
        if any(skip in media_type for skip in ("trailer", "preview", "clip", "music", "song", "podcast")):
            continue

        raw_title = _pick_first(
            row_lower,
            (
                "content name",
                "item description",
                "title",
                "show title",
                "program title",
                "target description",
                "episode title",
            ),
        )
        container = _pick_first(
            row_lower,
            (
                "container description",
                "series name",
                "show name",
                "show title",
                "brand name",
            ),
        )
        if not raw_title and not container:
            continue
        if not raw_title:
            raw_title = container

        dur_sec = _parse_duration_seconds(row_lower)
        # Apply min_play_seconds filter when duration is present (click activity files often omit duration)
        if dur_sec is not None and not is_click_file and dur_sec < min_play_seconds:
            continue

        season_col = _pick_first(row_lower, ("season number", "season"))
        episode_col = _pick_first(row_lower, ("episode number", "episode"))
        parsed = parse_show_and_episode(raw_title, container_title=container, season_col=season_col, episode_col=episode_col)
        show_title = parsed["show_title"]
        if not show_title:
            continue

        ts_raw = _pick_first(
            row_lower,
            (
                "event timestamp",
                "event start timestamp",
                "play timestamp",
                "date",
                "event date",
                "start time",
                "timestamp",
            ),
        )
        watched_date = _parse_timestamp_to_date(ts_raw)
        provider = _pick_first(
            row_lower,
            (
                "provider name",
                "channel name",
                "app name",
                "service name",
                "subscription channel",
            ),
        ) or "Apple TV"

        records.append({
            "show_title": show_title,
            "season": parsed["season"],
            "episode": parsed["episode"],
            "episode_title": parsed["episode_title"],
            "watched_date": watched_date,
            "duration_seconds": dur_sec or 0.0,
            "provider": provider,
            "source_file": Path(source_filename).name,
            "raw_timestamp": ts_raw,
        })

    return records


def _collect_apple_csv_sources(target: Path) -> list[tuple[str, str]]:
    """Return list of (filename, csv_text) from a CSV file, directory, or Apple Media Services .zip."""
    out: list[tuple[str, str]] = []
    if target.is_file():
        if target.suffix.lower() == ".zip":
            with zipfile.ZipFile(target, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    low_name = info.filename.lower()
                    if low_name.endswith(".csv") and any(
                        tok in low_name
                        for tok in (
                            "video play activity",
                            "tv app",
                            "click activity",
                            "play activity",
                            "apple tv",
                            "media",
                        )
                    ):
                        raw_bytes = zf.read(info.filename)
                        out.append((info.filename, raw_bytes.decode("utf-8-sig", errors="replace")))
        elif target.suffix.lower() == ".csv":
            out.append((target.name, target.read_text(encoding="utf-8-sig", errors="replace")))
    elif target.is_dir():
        for p in sorted(target.rglob("*.csv")):
            low_name = p.name.lower()
            if any(
                tok in low_name
                for tok in ("video play activity", "tv app", "click activity", "play activity", "apple tv")
            ):
                out.append((str(p.relative_to(target)), p.read_text(encoding="utf-8-sig", errors="replace")))
    return out


def _delete_source(target: Path) -> bool:
    try:
        if target.is_file():
            target.unlink()
            return True
        if target.is_dir():
            shutil.rmtree(target)
            return True
    except OSError:
        return False
    return False


def import_apple_tv_export(
    path: str,
    min_play_seconds: int = 120,
    delete_source_after_import: bool = False,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Import Apple TV watch history from Video Play Activity.csv, Click Activity CSV, or Apple Media Services .zip."""
    if not path or not str(path).strip():
        return {"ok": False, "error": "path must not be empty"}
    target = Path(str(path).strip()).expanduser().resolve()
    if not target.exists():
        return {"ok": False, "error": f"Path not found: {target}"}

    try:
        csv_sources = _collect_apple_csv_sources(target)
        if not csv_sources:
            return {
                "ok": False,
                "error": "No Apple TV CSV files (e.g., 'Video Play Activity.csv' or 'TV App with Channel Support Click Activity.csv') found at path.",
            }

        store = load_store(env)
        known_keys: set[str] = set(store.get("play_keys") or [])
        history: dict[str, dict[str, Any]] = store.setdefault("watch_history", {})

        imported_plays = 0
        skipped_duplicates = 0
        touched_shows: set[str] = set()

        for fname, text in csv_sources:
            recs = _parse_apple_csv_text(text, fname, min_play_seconds=max(0, int(min_play_seconds)))
            for rec in recs:
                key = _normalize_key(rec["show_title"])
                dedup_id = (
                    f"{key}|s={rec['season']}|e={rec['episode']}|"
                    f"ep={_normalize_key(rec['episode_title'] or '')}|"
                    f"dt={rec['raw_timestamp'] or rec['watched_date'] or ''}"
                )
                if dedup_id in known_keys:
                    skipped_duplicates += 1
                    continue
                known_keys.add(dedup_id)
                imported_plays += 1
                touched_shows.add(rec["show_title"])

                entry = history.get(key)
                if entry is None:
                    entry = {
                        "title": rec["show_title"],
                        "play_count": 0,
                        "unique_episodes_watched": 0,
                        "episodes_seen": [],
                        "seasons_seen": [],
                        "total_seconds": 0.0,
                        "first_watched": None,
                        "last_watched": None,
                        "providers": [],
                        "sources": ["apple_tv"],
                    }
                    history[key] = entry

                entry["play_count"] = int(entry.get("play_count", 0)) + 1
                entry["total_seconds"] = round(float(entry.get("total_seconds", 0.0)) + float(rec["duration_seconds"]), 1)

                wdate = rec["watched_date"]
                if wdate:
                    if not entry["first_watched"] or wdate < entry["first_watched"]:
                        entry["first_watched"] = wdate
                    if not entry["last_watched"] or wdate > entry["last_watched"]:
                        entry["last_watched"] = wdate

                if rec["season"] is not None and rec["season"] not in entry["seasons_seen"]:
                    entry["seasons_seen"].append(rec["season"])
                    entry["seasons_seen"].sort()

                ep_tag = None
                if rec["season"] is not None and rec["episode"] is not None:
                    ep_tag = f"S{rec['season']:02d}E{rec['episode']:02d}"
                elif rec["episode_title"]:
                    ep_tag = rec["episode_title"]
                if ep_tag and ep_tag not in entry["episodes_seen"]:
                    entry["episodes_seen"].append(ep_tag)
                entry["unique_episodes_watched"] = max(len(entry["episodes_seen"]), 1)

                prov = rec.get("provider")
                if prov and prov not in entry["providers"]:
                    entry["providers"].append(prov)
                if "apple_tv" not in entry.get("sources", []):
                    entry.setdefault("sources", []).append("apple_tv")

        store["play_keys"] = sorted(known_keys)
        saved_path = save_store(store, env)

        deleted = False
        if delete_source_after_import:
            deleted = _delete_source(target)

        return {
            "ok": True,
            "files_parsed": [name for name, _ in csv_sources],
            "imported_plays": imported_plays,
            "skipped_duplicates": skipped_duplicates,
            "shows_updated": len(touched_shows),
            "total_shows_in_history": len(history),
            "source_deleted": deleted,
            "store_path": str(saved_path),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def upsert_show(
    title: str,
    status: str = "watching",
    service_hint: str | None = None,
    current_season: int | None = None,
    notes: str | None = None,
    favorite: bool | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Add or update a show in the user's tracked watchlist."""
    clean_title = (title or "").strip()
    if not clean_title:
        return {"ok": False, "error": "title must not be empty"}
    norm_status = (status or "watching").strip().lower()
    if norm_status not in VALID_STATUSES:
        return {
            "ok": False,
            "error": f"Invalid status '{status}'. Must be one of: {', '.join(VALID_STATUSES)}",
        }

    store = load_store(env)
    tracked: dict[str, dict[str, Any]] = store.setdefault("tracked_shows", {})
    key = _normalize_key(clean_title)
    now_iso = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

    existing = tracked.get(key) or {
        "title": clean_title,
        "status": norm_status,
        "service_hint": None,
        "current_season": None,
        "notes": None,
        "favorite": False,
        "added_at": now_iso,
        "updated_at": now_iso,
    }
    existing["title"] = clean_title
    existing["status"] = norm_status
    if service_hint is not None:
        existing["service_hint"] = service_hint.strip() or None
    if current_season is not None:
        existing["current_season"] = int(current_season)
    if notes is not None:
        existing["notes"] = notes.strip() or None
    if favorite is not None:
        existing["favorite"] = bool(favorite)
    existing["updated_at"] = now_iso

    tracked[key] = existing
    save_store(store, env)
    return {
        "ok": True,
        "show": existing,
    }


def remove_show(title: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Remove a show from the user's tracked watchlist."""
    clean_title = (title or "").strip()
    if not clean_title:
        return {"ok": False, "error": "title must not be empty"}

    store = load_store(env)
    tracked: dict[str, dict[str, Any]] = store.setdefault("tracked_shows", {})
    key = _normalize_key(clean_title)
    removed = tracked.pop(key, None)
    if removed is None:
        return {"ok": False, "removed": False, "error": f"Show '{clean_title}' is not in tracked_shows"}
    save_store(store, env)
    return {"ok": True, "removed": True, "show": removed}


def list_tracked_shows(
    status: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """List the user's tracked shows alongside any imported Apple TV episode-play stats."""
    if status is not None:
        norm_status = status.strip().lower()
        if norm_status not in VALID_STATUSES:
            return {
                "ok": False,
                "error": f"Invalid status '{status}'. Must be one of: {', '.join(VALID_STATUSES)}",
            }
    else:
        norm_status = None

    store = load_store(env)
    tracked: dict[str, dict[str, Any]] = store.get("tracked_shows") or {}
    history: dict[str, dict[str, Any]] = store.get("watch_history") or {}

    items: list[dict[str, Any]] = []
    for key, item in sorted(tracked.items(), key=lambda kv: kv[1].get("title", "").lower()):
        if norm_status and item.get("status") != norm_status:
            continue
        hist = history.get(key)
        enriched = dict(item)
        if hist:
            enriched["history_stats"] = {
                "play_count": hist.get("play_count", 0),
                "unique_episodes_watched": hist.get("unique_episodes_watched", 0),
                "seasons_seen": hist.get("seasons_seen", []),
                "total_hours": round(float(hist.get("total_seconds", 0.0)) / 3600.0, 2),
                "first_watched": hist.get("first_watched"),
                "last_watched": hist.get("last_watched"),
                "providers": hist.get("providers", []),
            }
        else:
            enriched["history_stats"] = None
        items.append(enriched)

    return {
        "ok": True,
        "status_filter": norm_status,
        "count": len(items),
        "shows": items,
    }


def watch_history_summary(
    top_n: int = 25,
    since_days: int | None = None,
    source: str | None = None,
    env: dict[str, str] | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """Return aggregated Apple TV watch history, breakdown by connected app/provider, and active tracked shows."""
    store = load_store(env)
    history: dict[str, dict[str, Any]] = store.get("watch_history") or {}
    tracked: dict[str, dict[str, Any]] = store.get("tracked_shows") or {}
    ref_date = today or dt.date.today()

    cutoff_iso: str | None = None
    if since_days is not None and int(since_days) > 0:
        cutoff_iso = (ref_date - dt.timedelta(days=int(since_days))).isoformat()

    src_filter = (source or "").strip().lower() or None

    filtered: list[dict[str, Any]] = []
    by_provider: dict[str, int] = {}

    for key, h in history.items():
        if cutoff_iso:
            last_w = h.get("last_watched")
            if not last_w or last_w < cutoff_iso:
                continue
        if src_filter:
            sources_low = [s.lower() for s in (h.get("sources") or [])]
            provs_low = [p.lower() for p in (h.get("providers") or [])]
            if src_filter not in sources_low and not any(src_filter in p for p in provs_low):
                continue

        total_hours = round(float(h.get("total_seconds", 0.0)) / 3600.0, 2)
        tracked_info = tracked.get(key)
        row = {
            "title": h.get("title"),
            "play_count": int(h.get("play_count", 0)),
            "unique_episodes_watched": int(h.get("unique_episodes_watched", 0)),
            "seasons_seen": h.get("seasons_seen") or [],
            "total_hours": total_hours,
            "first_watched": h.get("first_watched"),
            "last_watched": h.get("last_watched"),
            "providers": h.get("providers") or [],
            "tracked_status": tracked_info.get("status") if tracked_info else None,
        }
        filtered.append(row)
        for prov in h.get("providers") or ["Apple TV"]:
            by_provider[prov] = by_provider.get(prov, 0) + 1

    limit = max(1, int(top_n))
    most_watched = sorted(
        filtered,
        key=lambda x: (-x["unique_episodes_watched"], -x["play_count"], (x["title"] or "").lower()),
    )[:limit]
    recently_watched = sorted(
        [x for x in filtered if x.get("last_watched")],
        key=lambda x: (x["last_watched"], x["play_count"]),
        reverse=True,
    )[:limit]

    active_shows = [
        v["title"]
        for v in tracked.values()
        if v.get("status") in ("watching", "want_to_watch", "waiting_for_next_season")
    ]

    return {
        "ok": True,
        "total_shows_matched": len(filtered),
        "most_watched_shows": most_watched,
        "recently_watched_shows": recently_watched,
        "shows_by_provider_or_channel": by_provider,
        "active_tracked_shows": sorted(active_shows),
    }
