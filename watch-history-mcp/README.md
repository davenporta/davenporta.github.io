# watch-history-mcp

Self-hosted stdio MCP server for parsing Apple TV exports (`privacy.apple.com` -> Apple Media Services) and maintaining the user's personal TV show watchlist locally.

## Tools

- `import_apple_tv_export(path, min_play_seconds=120, delete_source_after_import=False)` — Parse `Video Play Activity.csv`, `TV App with Channel Support Click Activity.csv`, or an Apple Media Services `.zip`/folder, deduplicate plays, aggregate by TV show, and save to `~/.config/watch-history-mcp/store.json` (`0600`).
- `upsert_show(title, status="watching", service_hint=None, current_season=None, notes=None, favorite=None)` — Add or update a show in the user's tracked list (`watching`, `want_to_watch`, `waiting_for_next_season`, `completed`, `dropped`).
- `remove_show(title)` — Remove a show from the tracked list.
- `list_tracked_shows(status=None)` — List tracked shows alongside any imported Apple TV episode-play stats.
- `watch_history_summary(top_n=25, since_days=None, source=None)` — Return aggregated show history, recently watched shows, breakdown by connected app/channel, and active tracked shows.

## Privacy & Zero Retention

- All user watch history and preferences are stored exclusively in `~/.config/watch-history-mcp/store.json` (directory mode `0700`, file mode `0600`), never inside `/workspace` or git.
- `delete_source_after_import=True` scrubs temporary CSV or `.zip` exports immediately after ingestion.
