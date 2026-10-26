from __future__ import annotations

import datetime as dt
import stat
import zipfile
from pathlib import Path

import pytest

from watch_history_mcp import store
from watch_history_mcp.cli import run as cli_run
from watch_history_mcp.server import build_server


VIDEO_PLAY_CSV = """Content Name,Container Description,Play Duration Milliseconds,Event Timestamp,Media Type,Provider Name,Stop Reason
"Severance - S2, E1 - Hello, Ms. Cobel",,2880000,2026-10-18T01:15:00Z,TV Episode,Apple TV+,EOF
"Severance - S2, E2 - Goodbye, Mrs. Selvig",,2820000,2026-10-19T02:00:00Z,TV Episode,Apple TV+,EOF
"Cold Harbor",Severance,4500000,2026-10-20T03:00:00Z,TV Episode,Apple TV+,EOF
"Shrinking: Season 3: Episode 1",,2100000,2026-10-22T01:30:00Z,TV Episode,Apple TV+,EOF
"Shrinking - S3, E1 - Preview",,45000,2026-10-22T01:00:00Z,Trailer,Apple TV+,USER_STOP
"Brief Accidental Click",The Bear,30000,2026-10-21T00:00:00Z,TV Episode,Hulu,USER_STOP
"""

CLICK_ACTIVITY_CSV = """Item Description,Series Name,Event Date,Channel Name,Media Type
"Forks",The Bear,2026-10-23T02:10:00Z,Hulu,TV Episode
"Fishes",The Bear,2026-10-22T02:10:00Z,Hulu,TV Episode
"""


def _env(tmp_path: Path) -> dict[str, str]:
    return {"WATCH_HISTORY_CONFIG_DIR": str(tmp_path / "cfg")}


def test_parse_show_and_episode_formats():
    p1 = store.parse_show_and_episode("Severance - S2, E1 - Hello, Ms. Cobel")
    assert p1 == {
        "show_title": "Severance",
        "season": 2,
        "episode": 1,
        "episode_title": "Hello, Ms. Cobel",
    }
    p2 = store.parse_show_and_episode("Shrinking: Season 3: Episode 2: Jimmying")
    assert p2 == {
        "show_title": "Shrinking",
        "season": 3,
        "episode": 2,
        "episode_title": "Jimmying",
    }
    p3 = store.parse_show_and_episode("Cold Harbor", container_title="Severance", season_col="2", episode_col="10")
    assert p3 == {
        "show_title": "Severance",
        "season": 2,
        "episode": 10,
        "episode_title": "Cold Harbor",
    }


def test_import_video_play_csv_and_permissions(tmp_path: Path):
    env = _env(tmp_path)
    csv_file = tmp_path / "Video Play Activity.csv"
    csv_file.write_text(VIDEO_PLAY_CSV, encoding="utf-8")

    r = store.import_apple_tv_export(str(csv_file), min_play_seconds=120, env=env)
    assert r["ok"] is True
    # 3 Severance plays + 1 Shrinking play (trailer & 30s accidental click filtered out)
    assert r["imported_plays"] == 4
    assert r["shows_updated"] == 2

    # Re-importing the same file deduplicates all plays
    r_dup = store.import_apple_tv_export(str(csv_file), min_play_seconds=120, env=env)
    assert r_dup["ok"] is True
    assert r_dup["imported_plays"] == 0
    assert r_dup["skipped_duplicates"] == 4

    # Verify 0700 directory and 0600 store.json permissions
    store_path = Path(r["store_path"])
    assert store_path.exists()
    dir_mode = stat.S_IMODE(store_path.parent.stat().st_mode)
    file_mode = stat.S_IMODE(store_path.stat().st_mode)
    assert dir_mode == 0o700
    assert file_mode == 0o600


def test_import_zip_bundle_and_delete_source(tmp_path: Path):
    env = _env(tmp_path)
    zip_path = tmp_path / "Apple_Media_Services.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Apple_Media_Services/Apple TV/Video Play Activity.csv", VIDEO_PLAY_CSV)
        zf.writestr(
            "Apple_Media_Services/Apple TV/TV App with Channel Support Click Activity.csv",
            CLICK_ACTIVITY_CSV,
        )

    r = store.import_apple_tv_export(str(zip_path), min_play_seconds=120, delete_source_after_import=True, env=env)
    assert r["ok"] is True
    assert len(r["files_parsed"]) == 2
    assert r["imported_plays"] == 6  # 4 from Video Play + 2 from Click Activity
    assert r["total_shows_in_history"] == 3  # Severance, Shrinking, The Bear
    assert r["source_deleted"] is True
    assert not zip_path.exists()

    summary = store.watch_history_summary(top_n=10, env=env, today=dt.date(2026, 10, 26))
    assert summary["ok"] is True
    assert summary["total_shows_matched"] == 3
    assert summary["most_watched_shows"][0]["title"] == "Severance"
    assert summary["most_watched_shows"][0]["unique_episodes_watched"] == 3
    assert summary["shows_by_provider_or_channel"]["Apple TV+"] == 2
    assert summary["shows_by_provider_or_channel"]["Hulu"] == 1


def test_tracked_shows_crud_and_join_with_history(tmp_path: Path):
    env = _env(tmp_path)
    csv_file = tmp_path / "Video Play Activity.csv"
    csv_file.write_text(VIDEO_PLAY_CSV, encoding="utf-8")
    store.import_apple_tv_export(str(csv_file), env=env)

    u1 = store.upsert_show(
        "Severance",
        status="watching",
        service_hint="Apple TV+",
        current_season=2,
        notes="Great sci-fi thriller",
        favorite=True,
        env=env,
    )
    assert u1["ok"] is True and u1["show"]["favorite"] is True

    u2 = store.upsert_show("Slow Horses", status="want_to_watch", service_hint="Apple TV+", env=env)
    assert u2["ok"] is True

    # Invalid status rejected
    bad = store.upsert_show("Test", status="invalid_status", env=env)
    assert bad["ok"] is False

    listed = store.list_tracked_shows(env=env)
    assert listed["ok"] is True and listed["count"] == 2
    sev = next(x for x in listed["shows"] if x["title"] == "Severance")
    assert sev["history_stats"] is not None
    assert sev["history_stats"]["play_count"] == 3
    assert sev["history_stats"]["total_hours"] == 2.83

    watching_only = store.list_tracked_shows(status="watching", env=env)
    assert watching_only["count"] == 1 and watching_only["shows"][0]["title"] == "Severance"

    rm = store.remove_show("Slow Horses", env=env)
    assert rm["ok"] is True and rm["removed"] is True
    assert store.remove_show("Slow Horses", env=env)["ok"] is False


def test_refuses_workspace_store_path():
    with pytest.raises(ValueError, match="Refusing to store watch history inside /workspace"):
        store.resolve_store_path({"WATCH_HISTORY_STORE_PATH": "/workspace/watch-history-mcp/store.json"})


async def test_mcp_server_and_cli(tmp_path: Path):
    env = _env(tmp_path)
    mcp = build_server(env=env)
    names = {t.name for t in await mcp.list_tools()}
    assert names == {
        "import_apple_tv_export",
        "upsert_show",
        "remove_show",
        "list_tracked_shows",
        "watch_history_summary",
    }
    assert all("netflix" not in n.lower() for n in names)

    res = await mcp.call_tool("upsert_show", {"title": "Shrinking", "status": "watching", "service_hint": "Apple TV+"})
    assert "Shrinking" in str(res)

    code, out = cli_run(["list", "--status", "watching"], env=env)
    assert code == 0 and out["count"] == 1 and out["shows"][0]["title"] == "Shrinking"
