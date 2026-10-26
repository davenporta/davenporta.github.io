#!/usr/bin/env python3
"""Safe Tesla Fleet CLI for scheduled jobs / scripts (same guardrails as the MCP server).

Examples:
  tesla_cli.sh status
  tesla_cli.sh charge
  tesla_cli.sh guard
  tesla_cli.sh next_trip --arrival-time 2026-10-26T08:30 --destination "100 Main St, Springfield, IL"
  tesla_cli.sh preview precondition_start --args '{"temp_f":70}'
  tesla_cli.sh run     precondition_start --args '{"temp_f":70}' --confirm
  tesla_cli.sh run     set_charge_limit   --args '{"percent":80}' --confirm
  tesla_cli.sh preview add_precondition_schedule --args '{"time_hhmm":"07:30","days":["WEEKDAYS"],"latitude":42.28,"longitude":-83.74}'
"""
import sys
from tesla_mcp.cli import main

if __name__ == "__main__":
    sys.exit(main())
