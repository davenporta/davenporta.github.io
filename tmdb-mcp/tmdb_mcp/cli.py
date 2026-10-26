"""JSON CLI for TMDB queries and streaming subscription plan checks. Never prints credentials.

  tmdb_cli.sh search "Severance"
  tmdb_cli.sh details 95396
  tmdb_cli.sh providers "Slow Horses" --region US
  tmdb_cli.sh schedule 95396 --season 2
  tmdb_cli.sh plan --shows "Severance" "The Bear" --subscriptions "Apple TV+" "Hulu" "Max"
Exit codes: 0 ok, 2 error / not configured.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import client as tmdb


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="tmdb_cli",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="search TV shows (and optionally movies)")
    s.add_argument("query", help="show or movie title")
    s.add_argument("--year", type=int, default=None, help="first air date year")
    s.add_argument("--include-movies", action="store_true", help="include movie matches")

    d = sub.add_parser("details", help="TV show details + watch providers")
    d.add_argument("tv_id", type=int, help="TMDB TV show ID")
    d.add_argument("--region", default="US", help="ISO 3166-1 region (default US)")

    p = sub.add_parser("providers", help="watch providers for a title or TMDB ID")
    p.add_argument("title_or_id", help="show/movie title or TMDB ID")
    p.add_argument("--media-type", default="tv", choices=["tv", "movie"], help="tv or movie")
    p.add_argument("--region", default="US", help="ISO 3166-1 region (default US)")

    sc = sub.add_parser("schedule", help="episode air schedule & binge readiness for a season")
    sc.add_argument("tv_id", type=int, help="TMDB TV show ID")
    sc.add_argument("--season", type=int, default=None, help="season number (default: latest)")

    pl = sub.add_parser("plan", help="check streaming subscriptions across a list of shows")
    pl.add_argument("--shows", nargs="+", required=True, help="show titles to check")
    pl.add_argument("--subscriptions", nargs="*", default=None, help="currently active subscriptions")
    pl.add_argument("--region", default="US", help="ISO 3166-1 region (default US)")

    return ap


def run(argv: list[str] | None, **kw: Any) -> tuple[int, dict[str, Any]]:
    a = _parser().parse_args(argv)
    if a.cmd == "search":
        r = tmdb.search_shows(a.query, year=a.year, include_movies=a.include_movies, **kw)
    elif a.cmd == "details":
        r = tmdb.tv_show_details(a.tv_id, region=a.region, **kw)
    elif a.cmd == "providers":
        r = tmdb.watch_providers(a.title_or_id, media_type=a.media_type, region=a.region, **kw)
    elif a.cmd == "schedule":
        r = tmdb.season_schedule(a.tv_id, season_number=a.season, **kw)
    else:
        r = tmdb.subscription_plan_check(a.shows, current_subscriptions=a.subscriptions, region=a.region, **kw)
    return (0 if r.get("ok") else 2), r


def main(argv: list[str] | None = None) -> int:
    try:
        code, out = run(argv if argv is not None else sys.argv[1:])
    except Exception as exc:
        code, out = 2, {"ok": False, "error": type(exc).__name__}
    print(json.dumps(out, indent=1, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
