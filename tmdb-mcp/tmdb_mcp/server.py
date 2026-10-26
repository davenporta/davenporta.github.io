"""Stdio MCP server for TMDB TV show & JustWatch US streaming provider lookups.

  tmdb-mcp        # stdio
Credentials: TMDB_API_KEY (v3 key or v4 bearer token) or TMDB_READ_TOKEN from environment or ~/.config/tmdb-mcp/env.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from . import client as tmdb

# Alias for callers referencing FastMCP
FastMCP = MCPServer

INSTRUCTIONS = (
    "Read-only access to The Movie Database (TMDB v3) and embedded JustWatch streaming availability. "
    "Use search_shows, tv_show_details, watch_providers, season_schedule, and subscription_plan_check "
    "to map TV shows and movies to active streaming subscriptions and check whether seasons are currently airing "
    "or finished and ready to binge."
)
RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)


def build_server(env: dict[str, str] | None = None, client_factory: tmdb.ClientFactory | None = None) -> MCPServer:
    mcp = MCPServer(name="tmdb", instructions=INSTRUCTIONS, version="0.1.0", log_level="WARNING")

    @mcp.tool(annotations=RO)
    async def search_shows(query: str, year: int | None = None, include_movies: bool = False) -> dict:
        """Search TV shows (and optionally movies) on TMDB, returning TMDB ID, name, first_air_date, overview, popularity, and vote_average."""
        return await asyncio.to_thread(tmdb.search_shows, query, year, include_movies, env, client_factory)

    @mcp.tool(annotations=RO)
    async def tv_show_details(tv_id: int) -> dict:
        """Return show status (Returning Series, Ended, Canceled, In Production), seasons/episodes count, networks, last_air_date, last_episode_to_air, next_episode_to_air, season summaries, and US watch/providers."""
        return await asyncio.to_thread(tmdb.tv_show_details, tv_id, None, env, client_factory)

    @mcp.tool(annotations=RO)
    async def watch_providers(title_or_id: str | int, media_type: str = "tv", region: str = "US") -> dict:
        """Accept a TMDB ID or show/movie title and return the streaming services carrying it right now in region (separating subscription/flatrate from free, ads, rent, and buy, plus latest season availability)."""
        return await asyncio.to_thread(tmdb.watch_providers, title_or_id, media_type, region, env, client_factory)

    @mcp.tool(annotations=RO)
    async def season_schedule(tv_id: int, season_number: int | None = None) -> dict:
        """Fetch episode-by-episode air dates for a season (defaults to latest season), reporting ready_to_binge, finale_air_date, and remaining_unaired_count."""
        return await asyncio.to_thread(tmdb.season_schedule, tv_id, season_number, env, client_factory)

    @mcp.tool(annotations=RO)
    async def subscription_plan_check(
        shows: list[str],
        current_subscriptions: list[str] | None = None,
        region: str = "US",
    ) -> dict:
        """Map a list of show titles (and optional active subscriptions) to streaming services, airing/binge readiness, and which subscriptions are actively needed vs. pausable."""
        return await asyncio.to_thread(
            tmdb.subscription_plan_check,
            shows,
            current_subscriptions,
            region,
            env,
            client_factory,
        )

    return mcp


def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(prog="tmdb-mcp", description="stdio MCP server for TMDB & JustWatch (read-only)").parse_args(argv)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    build_server().run("stdio")


if __name__ == "__main__":
    main()
