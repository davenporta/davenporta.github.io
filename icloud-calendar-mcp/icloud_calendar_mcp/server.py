"""Stdio MCP server for read-only iCloud calendar access.

  icloud-calendar-mcp        # stdio
Credentials: ICLOUD_APPLE_ID + ICLOUD_APP_PASSWORD (app-specific password) from the environment only.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from . import calendar as cal

INSTRUCTIONS = (
    "Read-only access to the user's Apple iCloud calendars over CalDAV. Times are in the configured local timezone. There are no "
    "create, edit or delete tools. Reminders/VTODO-only lists are skipped for events."
)
RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)


def build_server(env: dict[str, str] | None = None, client_factory=None) -> MCPServer:
    mcp = MCPServer(name="icloud-calendar-mcp", instructions=INSTRUCTIONS, version="0.1.0", log_level="WARNING")

    @mcp.tool(annotations=RO)
    async def list_calendars() -> dict:
        """List the user's iCloud calendars (name, has_events). Read-only."""
        return await asyncio.to_thread(cal.list_calendars, env, client_factory)

    @mcp.tool(annotations=RO)
    async def calendar_events(day: str = "today", end_day: str | None = None, hours: float | None = None,
                              start_hour: float = 0) -> dict:
        """Read-only list of iCloud calendar events (title, start, end, location, calendar, all_day), recurring
        events expanded, times in the configured local timezone. day: 'today', 'tomorrow', 'next_weekday' or YYYY-MM-DD.
        end_day (optional, same formats): inclusive range day..end_day, max 31 days. Without end_day, an optional
        window: start_hour (local hour, default 0) and hours (default: rest of the day), e.g. start_hour=5, hours=5."""
        return await asyncio.to_thread(cal.calendar_events, day, hours, start_hour, env, client_factory, None, end_day)

    return mcp


def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(prog="icloud-calendar-mcp", description="stdio MCP server (read-only)").parse_args(argv)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("caldav").setLevel(logging.WARNING)
    build_server().run("stdio")


if __name__ == "__main__":
    main()
