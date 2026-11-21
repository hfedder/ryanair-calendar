import asyncio
import html
import json
import os
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MCP_URL = "https://services-api.ryanair.com/mcp-server/mcp"

OUTPUT_FILE = Path("Ryanair_TRN_BGY_CPH_verificerede_afgange.ics")
CANDIDATE_FILE = Path("Ryanair_TRN_BGY_CPH_candidate.ics")
BACKUP_FILE = Path(
    "Ryanair_TRN_BGY_CPH_verificerede_afgange.backup.ics"
)
REPORT_FILE = Path("ryanair_validation_report.json")

ROUTES = [
    ("TRN", "CPH"),
    ("BGY", "CPH"),
    ("CPH", "TRN"),
    ("CPH", "BGY"),
]

DAYS_AHEAD = 365

PUBLISH = (
    os.getenv("PUBLISH_CALENDAR", "false").strip().lower() == "true"
)

ROME = ZoneInfo("Europe/Rome")
COPENHAGEN = ZoneInfo("Europe/Copenhagen")


# ---------------------------------------------------------------------------
# Time zones
# ---------------------------------------------------------------------------

def airport_tz(code):
    if code == "CPH":
        return COPENHAGEN

    return ROME


# ---------------------------------------------------------------------------
# ICS helpers
# ---------------------------------------------------------------------------

def escape_ics(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold_ics_line(line):
    """
    Fold an ICS line without splitting a UTF-8 character.

    RFC 5545 recommends content lines of no more than 75 octets.
    We use 73 bytes for the first and following chunks because folded
    continuation lines receive one leading space.
    """

    raw = line.encode("utf-8")
    parts = []

    while len(raw) > 73:
        cut = 73

        while cut > 0 and (raw[cut] & 0xC0) == 0x80:
            cut -= 1

        if cut == 0:
            cut = 73

        parts.append(raw[:cut].decode("utf-8"))
        raw = raw[cut:]

    parts.append(raw.decode("utf-8"))

    return "\r\n ".join(parts)


# ---------------------------------------------------------------------------
# MCP response handling
# ---------------------------------------------------------------------------

def result_text(result):
    """
    Convert all supported MCP response content to searchable text.

    Ryanair may return either ordinary text or structured MCP content.
    """

    parts = []

    for item in getattr(result, "content", []):
        if hasattr(item, "text") and item.text:
            parts.append(html.unescape(item.text))
            continue

        if hasattr(item, "data"):
            try:
                parts.append(
                    json.dumps(
                 
