"""JSON CLI for Apple TV watch history imports and local TV watchlist management.

  watch_history_cli.sh import-apple "/path/to/Video Play Activity.csv"
  watch_history_cli.sh upsert "Severance" --status watching --service "Apple TV+"
  watch_history_cli.sh remove "Old Show"
  watch_history_cli.sh list --status watching
  watch_history_cli.sh summary --top-n 25
Exit codes: 0 ok, 2 error.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import store


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="watch_history_cli",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    imp = sub.add_parser("import-apple", help="import Apple TV CSV, folder, or Apple Media Services .zip")
    imp.add_argument("path", help="path to CSV, folder, or .zip")
    imp.add_argument("--min-play-seconds", type=int, default=120, help="minimum play duration in seconds")
    imp.add_argument("--delete-source", action="store_true", help="delete source file/dir after import")

    up = sub.add_parser("upsert", help="add or update a tracked show")
    up.add_argument("title", help="show title")
    up.add_argument("--status", default="watching", choices=list(store.VALID_STATUSES))
    up.add_argument("--service", default=None, help="streaming service hint")
    up.add_argument("--season", type=int, default=None, help="current season")
    up.add_argument("--notes", default=None, help="optional notes")
    up.add_argument("--favorite", action="store_true", default=None, help="mark as favorite")

    rm = sub.add_parser("remove", help="remove a tracked show")
    rm.add_argument("title", help="show title")

    ls = sub.add_parser("list", help="list tracked shows")
    ls.add_argument("--status", default=None, help="filter by status")

    sm = sub.add_parser("summary", help="aggregated watch history summary")
    sm.add_argument("--top-n", type=int, default=25, help="max shows to return")
    sm.add_argument("--since-days", type=int, default=None, help="filter to last N days")
    sm.add_argument("--source", default=None, help="filter by provider/channel")

    return ap


def run(argv: list[str] | None, **kw: Any) -> tuple[int, dict[str, Any]]:
    a = _parser().parse_args(argv)
    if a.cmd == "import-apple":
        r = store.import_apple_tv_export(
            a.path,
            min_play_seconds=a.min_play_seconds,
            delete_source_after_import=a.delete_source,
            **kw,
        )
    elif a.cmd == "upsert":
        r = store.upsert_show(
            a.title,
            status=a.status,
            service_hint=a.service,
            current_season=a.season,
            notes=a.notes,
            favorite=a.favorite,
            **kw,
        )
    elif a.cmd == "remove":
        r = store.remove_show(a.title, **kw)
    elif a.cmd == "list":
        r = store.list_tracked_shows(status=a.status, **kw)
    else:
        r = store.watch_history_summary(top_n=a.top_n, since_days=a.since_days, source=a.source, **kw)
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
