"""JSON CLI for scheduled jobs: read-only iCloud calendar queries. Never prints credentials.

  calendar_cli.sh events --day today
  calendar_cli.sh events --day next_weekday --start-hour 5 --hours 5
  calendar_cli.sh events --day 2026-10-30 --end-day 2026-11-02
  calendar_cli.sh list
  calendar_cli.sh events --day today --count-only
Exit codes: 0 ok, 2 error / not configured.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import calendar as cal


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="calendar_cli", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list calendars")
    e = sub.add_parser("events", help="events for a day or date range")
    e.add_argument("--day", default="today", help="today | tomorrow | next_weekday | YYYY-MM-DD")
    e.add_argument("--end-day", default=None, help="inclusive range end (max 31 days)")
    e.add_argument("--start-hour", type=float, default=0, help="local start hour (default 0)")
    e.add_argument("--hours", type=float, default=None, help="window length in hours (default: rest of day)")
    e.add_argument("--count-only", action="store_true", help="print only {ok, count}")
    return ap


def run(argv: list[str] | None, **kw) -> tuple[int, Any]:
    a = _parser().parse_args(argv)
    if a.cmd == "list":
        r = cal.list_calendars(**kw)
    else:
        r = cal.calendar_events(a.day, a.hours, a.start_hour, end_day=a.end_day, **kw)
        if a.count_only and r.get("ok"):
            r = {"ok": True, "day": r["day"], "count": r["count"]}
    return (0 if r.get("ok") else 2), r


def main(argv: list[str] | None = None) -> int:
    try:
        code, out = run(argv if argv is not None else sys.argv[1:])
    except Exception as e:  # never leak tracebacks
        code, out = 2, {"ok": False, "error": type(e).__name__}
    print(json.dumps(out, indent=1, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
