from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from tmdb_mcp import client as tmdb
from tmdb_mcp.cli import run as cli_run
from tmdb_mcp.server import build_server

TODAY = dt.date(2026, 10, 26)
V3_ENV = {"TMDB_API_KEY": "a1b2c3d4e5f60718293a4b5c6d7e8f90", "TMDB_REGION": "US"}
V4_TOKEN = "eyJ" + ("a" * 32) + "." + ("b" * 36)
V4_ENV = {"TMDB_API_KEY": V4_TOKEN, "TMDB_REGION": "US"}


FAKE_SEARCH_TV = {
    "results": [
        {
            "id": 95396,
            "name": "Severance",
            "first_air_date": "2022-02-17",
            "overview": "Mark leads a team of office workers whose memories have been surgically divided.",
            "popularity": 120.5,
            "vote_average": 8.4,
            "origin_country": ["US"],
        },
        {
            "id": 136315,
            "name": "The Bear",
            "first_air_date": "2022-06-23",
            "overview": "A young chef from the fine dining world comes home to Chicago.",
            "popularity": 95.0,
            "vote_average": 8.3,
            "origin_country": ["US"],
        },
    ]
}

FAKE_SEARCH_MOVIE = {
    "results": [
        {
            "id": 693134,
            "title": "Dune: Part Two",
            "release_date": "2024-02-27",
            "overview": "Follow the mythic journey of Paul Atreides.",
            "popularity": 200.0,
            "vote_average": 8.2,
        }
    ]
}

FAKE_SEVERANCE_DETAILS = {
    "id": 95396,
    "name": "Severance",
    "status": "Returning Series",
    "in_production": True,
    "first_air_date": "2022-02-17",
    "last_air_date": "2025-03-21",
    "number_of_seasons": 2,
    "number_of_episodes": 19,
    "networks": [{"name": "Apple TV+"}],
    "last_episode_to_air": {
        "id": 1001,
        "name": "Cold Harbor",
        "season_number": 2,
        "episode_number": 10,
        "air_date": "2025-03-21",
        "episode_type": "finale",
        "runtime": 76,
    },
    "next_episode_to_air": None,
    "seasons": [
        {"season_number": 0, "name": "Specials", "episode_count": 1, "air_date": "2022-02-10", "vote_average": 0.0},
        {"season_number": 1, "name": "Season 1", "episode_count": 9, "air_date": "2022-02-17", "vote_average": 8.5},
        {"season_number": 2, "name": "Season 2", "episode_count": 10, "air_date": "2025-01-17", "vote_average": 8.6},
    ],
    "watch/providers": {
        "results": {
            "US": {
                "link": "https://www.themoviedb.org/tv/95396-severance/watch?locale=US",
                "flatrate": [
                    {"provider_id": 350, "provider_name": "Apple TV Plus", "display_priority": 1},
                ],
            }
        }
    },
}

FAKE_SEVERANCE_S2 = {
    "id": 2002,
    "name": "Season 2",
    "season_number": 2,
    "air_date": "2025-01-17",
    "episodes": [
        {"episode_number": 1, "name": "Hello, Ms. Cobel", "air_date": "2025-01-17", "episode_type": "standard", "runtime": 48},
        {"episode_number": 2, "name": "Goodbye, Mrs. Selvig", "air_date": "2025-01-24", "episode_type": "standard", "runtime": 47},
        {"episode_number": 10, "name": "Cold Harbor", "air_date": "2025-03-21", "episode_type": "finale", "runtime": 76},
    ],
}

FAKE_SHRINKING_DETAILS = {
    "id": 207863,
    "name": "Shrinking",
    "status": "Returning Series",
    "in_production": True,
    "first_air_date": "2023-01-26",
    "last_air_date": "2026-10-21",
    "number_of_seasons": 3,
    "number_of_episodes": 32,
    "networks": [{"name": "Apple TV+"}],
    "last_episode_to_air": {
        "id": 3002,
        "name": "Episode 2",
        "season_number": 3,
        "episode_number": 2,
        "air_date": "2026-10-21",
        "episode_type": "standard",
        "runtime": 34,
    },
    "next_episode_to_air": {
        "id": 3003,
        "name": "Episode 3",
        "season_number": 3,
        "episode_number": 3,
        "air_date": "2026-10-28",
        "episode_type": "standard",
        "runtime": 35,
    },
    "seasons": [
        {"season_number": 1, "name": "Season 1", "episode_count": 10, "air_date": "2023-01-26", "vote_average": 8.0},
        {"season_number": 2, "name": "Season 2", "episode_count": 12, "air_date": "2024-10-15", "vote_average": 8.2},
        {"season_number": 3, "name": "Season 3", "episode_count": 10, "air_date": "2026-10-14", "vote_average": 8.4},
    ],
    "watch/providers": {
        "results": {
            "US": {
                "link": "https://www.themoviedb.org/tv/207863-shrinking/watch?locale=US",
                "flatrate": [
                    {"provider_id": 350, "provider_name": "Apple TV+", "display_priority": 1},
                ],
            }
        }
    },
}

FAKE_SHRINKING_S3 = {
    "id": 3000,
    "name": "Season 3",
    "season_number": 3,
    "air_date": "2026-10-14",
    "episodes": [
        {"episode_number": 1, "name": "Episode 1", "air_date": "2026-10-14", "episode_type": "standard", "runtime": 36},
        {"episode_number": 2, "name": "Episode 2", "air_date": "2026-10-21", "episode_type": "standard", "runtime": 34},
        {"episode_number": 3, "name": "Episode 3", "air_date": "2026-10-28", "episode_type": "standard", "runtime": 35},
        {"episode_number": 4, "name": "Finale", "air_date": "2026-11-04", "episode_type": "finale", "runtime": 42},
    ],
}


def _make_transport(seen_requests: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        path = request.url.path
        q = request.url.params.get("query", "").lower()

        if path == "/3/search/tv":
            if "shrinking" in q:
                return httpx.Response(
                    200,
                    json={"results": [{"id": 207863, "name": "Shrinking", "first_air_date": "2023-01-26"}]},
                )
            if "unknownshowxyz" in q:
                return httpx.Response(200, json={"results": []})
            return httpx.Response(200, json=FAKE_SEARCH_TV)
        if path == "/3/search/movie":
            return httpx.Response(200, json=FAKE_SEARCH_MOVIE)
        if path == "/3/tv/95396":
            return httpx.Response(200, json=FAKE_SEVERANCE_DETAILS)
        if path == "/3/tv/95396/season/2":
            return httpx.Response(200, json=FAKE_SEVERANCE_S2)
        if path == "/3/tv/95396/season/2/watch/providers":
            return httpx.Response(
                200,
                json={"results": {"US": {"flatrate": [{"provider_id": 350, "provider_name": "Apple TV Plus", "display_priority": 1}]}}},
            )
        if path == "/3/tv/207863":
            return httpx.Response(200, json=FAKE_SHRINKING_DETAILS)
        if path == "/3/tv/207863/season/3":
            return httpx.Response(200, json=FAKE_SHRINKING_S3)
        if path == "/3/movie/693134":
            return httpx.Response(
                200,
                json={
                    "id": 693134,
                    "title": "Dune: Part Two",
                    "watch/providers": {
                        "results": {
                            "US": {
                                "flatrate": [{"provider_id": 1899, "provider_name": "Max", "display_priority": 1}],
                                "rent": [{"provider_id": 2, "provider_name": "Apple TV", "display_priority": 2}],
                            }
                        }
                    },
                },
            )
        return httpx.Response(404, json={"status_message": "Not found"})

    return httpx.MockTransport(handler)


def _factory(seen: list[httpx.Request]):
    transport = _make_transport(seen)

    def f(env: dict[str, str] | None = None) -> tmdb.TMDBClient:
        return tmdb.TMDBClient(env=env, transport=transport)

    return f


def test_auth_mode_detection_v3_vs_v4():
    seen_v3: list[httpx.Request] = []
    r1 = tmdb.search_shows("Severance", env=V3_ENV, client_factory=_factory(seen_v3))
    assert r1["ok"] is True
    assert len(seen_v3) == 1
    req_v3 = seen_v3[0]
    assert req_v3.url.params.get("api_key") == V3_ENV["TMDB_API_KEY"]
    assert "Authorization" not in req_v3.headers
    assert req_v3.headers["User-Agent"] == "tmdb-mcp/0.1 (personal-assistant)"

    seen_v4: list[httpx.Request] = []
    r2 = tmdb.search_shows("Severance", env=V4_ENV, client_factory=_factory(seen_v4))
    assert r2["ok"] is True
    req_v4 = seen_v4[0]
    assert "api_key" not in req_v4.url.params
    assert req_v4.headers["Authorization"] == f"Bearer {V4_TOKEN}"

    # TMDB_READ_TOKEN also forces bearer mode
    seen_rt: list[httpx.Request] = []
    r3 = tmdb.search_shows("Severance", env={"TMDB_READ_TOKEN": "short_read_token_val"}, client_factory=_factory(seen_rt))
    assert r3["ok"] is True
    assert seen_rt[0].headers["Authorization"] == "Bearer short_read_token_val"


def test_missing_credentials_and_secret_scrubbing():
    r = tmdb.search_shows("Severance", env={})
    assert r["ok"] is False and r["configured"] is False

    def failing_factory(env):
        c = tmdb.TMDBClient(env=env)
        c.get = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError(f"failed with key {env['TMDB_API_KEY']}"))
        return c

    r_err = tmdb.search_shows("Severance", env=V3_ENV, client_factory=failing_factory)
    assert r_err["ok"] is False
    assert V3_ENV["TMDB_API_KEY"] not in str(r_err)
    assert "[REDACTED]" in r_err["error"]


def test_search_shows_and_movies():
    seen: list[httpx.Request] = []
    r = tmdb.search_shows("Severance", include_movies=True, year=2022, env=V3_ENV, client_factory=_factory(seen))
    assert r["ok"] is True and r["count"] == 3
    media_types = [x["media_type"] for x in r["results"]]
    assert "tv" in media_types and "movie" in media_types
    assert tmdb.search_shows("   ", env=V3_ENV)["ok"] is False


def test_tv_show_details_and_watch_providers():
    seen: list[httpx.Request] = []
    det = tmdb.tv_show_details(95396, env=V3_ENV, client_factory=_factory(seen))
    assert det["ok"] is True
    assert det["name"] == "Severance"
    assert det["airing_state"] == "season_complete_ready_to_binge"
    assert det["watch_providers"]["subscription"] == ["Apple TV+"]
    assert det["latest_season_number"] == 2

    # watch_providers by title string
    wp = tmdb.watch_providers("Severance", media_type="tv", region="US", env=V3_ENV, client_factory=_factory(seen))
    assert wp["ok"] is True
    assert wp["id"] == 95396
    assert wp["providers"]["subscription"] == ["Apple TV+"]
    assert wp["latest_season_providers"]["season_number"] == 2
    assert wp["latest_season_providers"]["subscription"] == ["Apple TV+"]

    # watch_providers for movie
    wp_mv = tmdb.watch_providers(693134, media_type="movie", region="US", env=V3_ENV, client_factory=_factory(seen))
    assert wp_mv["ok"] is True
    assert wp_mv["providers"]["subscription"] == ["Max"]


def test_season_schedule_binge_ready_vs_currently_airing():
    seen: list[httpx.Request] = []
    # Severance S2: all 3 episodes in mock aired in 2025 -> ready_to_binge=True
    s_done = tmdb.season_schedule(95396, env=V3_ENV, client_factory=_factory(seen), today=TODAY)
    assert s_done["ok"] is True
    assert s_done["season_number"] == 2
    assert s_done["ready_to_binge"] is True
    assert s_done["remaining_unaired_count"] == 0
    assert s_done["finale_air_date"] == "2025-03-21"
    assert s_done["season_status"] == "ready_to_binge"

    # Shrinking S3: 2 aired before 2026-10-26, 2 unaired after 2026-10-26 -> ready_to_binge=False
    s_airing = tmdb.season_schedule(207863, season_number=3, env=V3_ENV, client_factory=_factory(seen), today=TODAY)
    assert s_airing["ok"] is True
    assert s_airing["ready_to_binge"] is False
    assert s_airing["aired_episodes_count"] == 2
    assert s_airing["remaining_unaired_count"] == 2
    assert s_airing["season_status"] == "currently_airing"
    assert s_airing["next_unaired_episode"]["air_date"] == "2026-10-28"
    assert s_airing["finale_air_date"] == "2026-11-04"


def test_subscription_plan_check():
    seen: list[httpx.Request] = []
    plan = tmdb.subscription_plan_check(
        shows=["Severance", "Shrinking", "UnknownShowXYZ"],
        current_subscriptions=["Apple TV Plus", "Max", "Peacock Premium"],
        region="US",
        env=V3_ENV,
        client_factory=_factory(seen),
        today=TODAY,
    )
    assert plan["ok"] is True
    assert "Apple TV+" in plan["by_service"]
    assert len(plan["by_service"]["Apple TV+"]) == 2
    assert plan["summary"]["currently_airing"] == ["Shrinking"]
    assert plan["summary"]["ready_to_binge"] == ["Severance"]
    assert plan["summary"]["unresolved"] == ["UnknownShowXYZ"]

    recs = plan["subscription_recommendations"]
    assert [x["service"] for x in recs["active_used_subscriptions"]] == ["Apple TV+"]
    assert recs["subscriptions_with_zero_tracked_shows"] == ["Max", "Peacock"]


async def test_mcp_server_tools_and_cli():
    seen: list[httpx.Request] = []
    mcp = build_server(env=V3_ENV, client_factory=_factory(seen))
    names = {t.name for t in await mcp.list_tools()}
    assert names == {
        "search_shows",
        "tv_show_details",
        "watch_providers",
        "season_schedule",
        "subscription_plan_check",
    }
    res = await mcp.call_tool("watch_providers", {"title_or_id": "Severance"})
    assert "Apple TV+" in str(res)

    code, out = cli_run(["providers", "Severance"], env=V3_ENV, client_factory=_factory(seen))
    assert code == 0 and out["ok"] is True
    code_err, out_err = cli_run(["search", "Severance"], env={})
    assert code_err == 2 and out_err["configured"] is False


def test_run_mcp_script_exits_cleanly_without_env(tmp_path: Path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_mcp.sh"
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("TMDB_")}
    clean_env["TMDB_CONFIG_DIR"] = str(tmp_path / "empty-config")
    p = subprocess.run([str(script)], env=clean_env, capture_output=True, text=True, timeout=15)
    assert p.returncode == 1 and p.stdout == ""
    assert "TMDB_API_KEY" in p.stderr
