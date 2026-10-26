# tmdb-mcp

Self-hosted, strictly read-only stdio MCP server for The Movie Database (TMDB v3 API) and embedded JustWatch streaming availability.

## Tools

- `search_shows(query, year=None, include_movies=False)` — Search TV shows (and optionally movies) on TMDB.
- `tv_show_details(tv_id)` — Show status, episode counts, networks, `last_episode_to_air`, `next_episode_to_air`, season summaries, and US `watch/providers`.
- `watch_providers(title_or_id, media_type="tv", region="US")` — Subscription (`flatrate`), `free`, `ads`, `rent`, and `buy` streaming services carrying a show or movie right now.
- `season_schedule(tv_id, season_number=None)` — Episode-by-episode air dates for a season, `ready_to_binge`, `finale_air_date`, and `remaining_unaired_count`.
- `subscription_plan_check(shows, current_subscriptions=None, region="US")` — Maps tracked show titles to streaming services, airing/binge readiness, and identifies active vs. pausable subscriptions.

## Configuration & Privacy

- Credentials (`TMDB_API_KEY` or `TMDB_READ_TOKEN`) are read from the environment or `~/.config/tmdb-mcp/env` (`0600`).
- Auto-detects TMDB v3 API key query param vs. v4 Bearer Read Access Token (`eyJ...` or >64 chars).
- Uses a generic `User-Agent: tmdb-mcp/0.1 (personal-assistant)` with no personal identifiers.
