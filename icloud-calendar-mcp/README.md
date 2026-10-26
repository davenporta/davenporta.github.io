# icloud-calendar-mcp

Self-hosted, **strictly read-only** MCP server (stdio) for Apple iCloud calendars over CalDAV.
Separate from the Tesla server; it knows nothing about cars.

* Only CalDAV discovery and event searches — there is **no create, edit or delete code**.
* Times are returned in the configured local timezone (`ICLOUD_CALENDAR_TZ` or `~/.config/icloud-calendar-mcp/timezone`, default `UTC`); recurring events are expanded; cancelled instances are dropped.
* Reminders / VTODO-only lists are skipped when reading events.
* Credentials come only from the environment and are never logged, returned, or written to disk/git. Error messages
  name only the exception type.

## Tools
| tool | what |
|---|---|
| `list_calendars()` | calendar names + `has_events` |
| `calendar_events(day="today", end_day?, hours?, start_hour=0)` | events (title, start, end, location, calendar, all_day). `day` / `end_day`: today, tomorrow, next_weekday or YYYY-MM-DD. `end_day` gives an inclusive range (max 31 days); otherwise `start_hour` + `hours` window a single day |

## Setup
1. Create an **app-specific password** at <https://account.apple.com> → Sign-In and Security → App-Specific Passwords.
2. Provide `ICLOUD_APPLE_ID` (Apple ID email) and `ICLOUD_APP_PASSWORD` in the environment — never in files in this repo.
3. Install:
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
   .venv/bin/pytest -q
   ```

## Run
```bash
scripts/run_mcp.sh            # stdio MCP server; exits 1 with a clear message if the env vars are missing
```
MCP client config:
```json
{"mcpServers": {"icloud-calendar": {"command": "/workspace/icloud-calendar-mcp/scripts/run_mcp.sh",
  "env": {"ICLOUD_APPLE_ID": "…", "ICLOUD_APP_PASSWORD": "…"}}}}
```

## CLI for scheduled jobs (JSON)
```bash
scripts/calendar_cli.sh events --day today
scripts/calendar_cli.sh events --day next_weekday --start-hour 5 --hours 5   # morning window
scripts/calendar_cli.sh events --day 2026-10-30 --end-day 2026-11-02
scripts/calendar_cli.sh events --day today --count-only
scripts/calendar_cli.sh list
```
Exit codes: 0 ok, 2 error / not configured.

To ask the Tesla server whether preconditioning makes sense for an event, pass the event's location and start time to
its `next_trip_check(vin, arrival_time, destination)` tool — the two servers do not import each other.

Revoke access any time by deleting the app-specific password at account.apple.com.
