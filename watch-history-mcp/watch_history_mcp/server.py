"""Stdio MCP server for Apple TV watch history exports and local TV show watchlist tracking.

  watch-history-mcp        # stdio
All user data is stored exclusively in ~/.config/watch-history-mcp/store.json (0600).
"""
from __future__ import annotations

import argparse
import asyncio

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from . import store

# Alias for callers referencing FastMCP
FastMCP = MCPServer

INSTRUCTIONS = (
    "Local TV show preference & Apple TV watch history manager. "
    "Parses official Apple Media Services exports (Video Play Activity.csv, TV App with Channel Support Click Activity.csv, or .zip) "
    "and maintains the user's tracked TV shows (watching, want_to_watch, waiting_for_next_season, completed, dropped) "
    "in ~/.config/watch-history-mcp/store.json (0600)."
)
RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
RW = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def build_server(env: dict[str, str] | None = None) -> MCPServer:
    mcp = MCPServer(name="watch-history", instructions=INSTRUCTIONS, version="0.1.0", log_level="WARNING")

    @mcp.tool(annotations=RW)
    async def import_apple_tv_export(
        path: str,
        min_play_seconds: int = 120,
        delete_source_after_import: bool = False,
    ) -> dict:
        """Parse Apple's Video Play Activity.csv, TV App with Channel Support Click Activity.csv, or Apple Media Services .zip/folder, deduplicate plays, aggregate by TV show, and save to ~/.config/watch-history-mcp/store.json (0600)."""
        return await asyncio.to_thread(
            store.import_apple_tv_export,
            path,
            min_play_seconds,
            delete_source_after_import,
            env,
        )

    @mcp.tool(annotations=RW)
    async def upsert_show(
        title: str,
        status: str = "watching",
        service_hint: str | None = None,
        current_season: int | None = None,
        notes: str | None = None,
        favorite: bool | None = None,
    ) -> dict:
        """Add or update a show in the user's tracked list (status: watching, want_to_watch, waiting_for_next_season, completed, dropped)."""
        return await asyncio.to_thread(
            store.upsert_show,
            title,
            status,
            service_hint,
            current_season,
            notes,
            favorite,
            env,
        )

    @mcp.tool(annotations=RW)
    async def remove_show(title: str) -> dict:
        """Remove a show from the user's tracked list."""
        return await asyncio.to_thread(store.remove_show, title, env)

    @mcp.tool(annotations=RO)
    async def list_tracked_shows(status: str | None = None) -> dict:
        """List the user's tracked shows (optionally filtered by status) alongside any imported Apple TV episode-play stats."""
        return await asyncio.to_thread(store.list_tracked_shows, status, env)

    @mcp.tool(annotations=RO)
    async def watch_history_summary(
        top_n: int = 25,
        since_days: int | None = None,
        source: str | None = None,
    ) -> dict:
        """Return aggregated watch history (most-watched shows, recently watched shows, breakdown by connected app/channel, and active tracked shows)."""
        return await asyncio.to_thread(store.watch_history_summary, top_n, since_days, source, env)

    return mcp


def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(prog="watch-history-mcp", description="stdio MCP server for Apple TV watch history & watchlist").parse_args(argv)
    build_server().run("stdio")


if __name__ == "__main__":
    main()
