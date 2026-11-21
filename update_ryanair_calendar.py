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

MCP_URL = "https://services-api.ryanair.com/mcp-server/mcp"
OUTPUT_FILE = Path("Ryanair_TRN_BGY_CPH_verificerede_afgange.ics")
CANDIDATE_FILE = Path("Ryanair_TRN_BGY_CPH_candidate.ics")
BACKUP_FILE = Path("Ryanair_TRN_BGY_CPH_verificerede_afgange.backup.ics")
REPORT_FILE = Path("ryanair_validation_report.json")

ROUTES = [
    ("TRN", "CPH"),
    ("BGY", "CPH"),
    ("CPH", "TRN"),
    ("CPH", "BGY"),
]

DAYS_AHEAD = 365
PUBLISH = os.getenv("PUBLISH_CALENDAR", "false").strip().lower() == "true"
ROME = ZoneInfo("Europe/Rome")
COPENHAGEN = ZoneInfo("Europe/Copenhagen")


def airport_tz(code):
    return COPENHAGEN if code == "CPH" else ROME


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


def result_text(result):
    parts = []
    for item in getattr(result, "content", []):
        if hasattr(item, "text") and item.text:
            parts.append(html.unescape(item.text))
        elif hasattr(item, "data"):
            try:
                parts.append(json.dumps(item.data, ensure_ascii=False, default=str))
            except Exception:
                parts.append(str(item.data))
        else:
            try:
                if hasattr(item, "model_dump"):
                    parts.append(
                        json.dumps(item.model_dump(), ensure_ascii=False, default=str)
                    )
                else:
                    parts.append(str(item))
            except Exception:
                parts.append(str(item))
    return "\n".join(parts)


def create_flight(
    flight_number,
    requested_date,
    origin,
    destination,
    departure_time,
    arrival_time,
    price=None,
    currency=None,
):
    dep = datetime.combine(
        requested_date,
        datetime.strptime(departure_time, "%H:%M").time(),
        airport_tz(origin),
    )
    arr = datetime.combine(
        requested_date,
        datetime.strptime(arrival_time, "%H:%M").time(),
        airport_tz(destination),
    )
    if arr.astimezone(timezone.utc) <= dep.astimezone(timezone.utc):
        arr += timedelta(days=1)

    return {
        "flight_number": str(flight_number).replace(" ", "").upper(),
        "origin": origin,
        "destination": destination,
        "departure": dep,
        "arrival": arr,
        "price": str(price).replace(",", ".") if price is not None else None,
        "currency": str(currency).upper() if currency else "EUR",
        "source": "Ryanair MCP",
    }


def parse_flights(text, requested_date, origin, destination):
    text = html.unescape(text)
    definitions = [
        {
            "pattern": re.compile(
                r"\b(FR\s?\d{1,5})\s*:?\s*"
                r"([A-Z]{3})\s+([0-2]\d:[0-5]\d)\s*"
                r"(?:→|->|-)\s*([A-Z]{3})\s+([0-2]\d:[0-5]\d)"
                r"(?:\s*\|\s*([0-9]+(?:[.,][0-9]+)?)\s*([A-Z]{3}))?",
                re.IGNORECASE,
            ),
            "flight": 1,
            "origin": 2,
            "departure": 3,
            "destination": 4,
            "arrival": 5,
            "price": 6,
            "currency": 7,
        },
        {
            "pattern": re.compile(
                r"\b(FR\s?\d{1,5})\s+([A-Z]{3})\s*"
                r"(?:→|->|-)\s*([A-Z]{3})\s+"
                r"([0-2]\d:[0-5]\d)\s*(?:→|->|-)\s*"
                r"([0-2]\d:[0-5]\d)"
                r"(?:.*?([0-9]+(?:[.,][0-9]+)?)\s*([A-Z]{3}))?",
                re.IGNORECASE,
            ),
            "flight": 1,
            "origin": 2,
            "destination": 3,
            "departure": 4,
            "arrival": 5,
            "price": 6,
            "currency": 7,
        },
    ]

    flights = []
    for definition in definitions:
        for match in definition["pattern"].finditer(text):
            found_origin = match.group(definition["origin"]).upper()
            found_destination = match.group(definition["destination"]).upper()
            if found_origin != origin or found_destination != destination:
                continue
            flights.append(
                create_flight(
                    flight_number=match.group(definition["flight"]),
                    requested_date=requested_date,
                    origin=origin,
                    destination=destination,
                    departure_time=match.group(definition["departure"]),
                    arrival_time=match.group(definition["arrival"]),
                    price=match.group(definition["price"]),
                    currency=match.group(definition["currency"]),
                )
            )
    return flights


def choose_tool(tools):
    names = [tool.name for tool in tools]
    print("Available MCP tools:", ", ".join(names))
    preferred = [
        "ryanair.search_flights",
        "search_flights",
        "pricing-get-cheapest-one-way-flight",
        "pricing-get-cheapest-flight-per-day",
    ]
    for candidate in preferred:
        if candidate in names:
            return candidate
    raise RuntimeError(
        "No supported Ryanair flight-search MCP tool is exposed. Available: "
        + ", ".join(names)
    )


def arguments_for(tool_name, origin, destination, flight_date):
    if tool_name in ("ryanair.search_flights", "search_flights"):
        return {
            "origin": origin,
            "destination": destination,
            "dateOut": flight_date.isoformat(),
            "roundTrip": "false",
            "passengers": {
                "adults": 1,
                "teens": 0,
                "children": 0,
                "infants": 0,
            },
            "langCode": "en-IE",
        }
    if tool_name == "pricing-get-cheapest-one-way-flight":
        return {
            "outboundDepartureDateFrom": flight_date.isoformat(),
            "outboundDepartureDateTo": flight_date.isoformat(),
            "departureIataCodes": [origin],
            "arrivalIataCodes": [destination],
            "currency": "EUR",
            "limit": 50,
            "pageNumber": 0,
        }
    if tool_name == "pricing-get-cheapest-flight-per-day":
        return {
            "departureIataCode": origin,
            "arrivalIataCode": destination,
            "outboundDepartureDateFrom": flight_date.isoformat(),
            "outboundDepartureDateTo": flight_date.isoformat(),
            "currency": "EUR",
        }
    raise RuntimeError("Unsupported MCP tool: " + tool_name)


async def collect_flights():
    today = date.today()
    last_date = today + timedelta(days=DAYS_AHEAD)
    flights = []
    errors = []

    async with streamable_http_client(MCP_URL) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
listed = await session.list_tools()

for tool in listed.tools:
    print("=" * 80)
    print("MCP TOOL:", tool.name)
    print("DESCRIPTION:", getattr(tool, "description", None))
    print(
        "INPUT SCHEMA:",
        json.dumps(
            getattr(tool, "inputSchema", {}),
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
    )

raise RuntimeError("STOP AFTER MCP TOOL SCHEMA DUMP")
            print("Selected MCP tool:", tool_name)

            if tool_name.startswith("pricing-get-cheapest"):
                print(
                    "WARNING: Selected pricing tool may return only the cheapest "
                    "flight rather than every departure."
                )

            expected = (DAYS_AHEAD + 1) * len(ROUTES)
            done = 0

            for origin, destination in ROUTES:
                route_start_count = len(flights)
                route_error_start = len(errors)
                current = today
                print(f"Starting route {origin}->{destination}")

                while current <= last_date:
                    try:
                        result = await session.call_tool(
                            tool_name,
                            arguments=arguments_for(
                                tool_name, origin, destination, current
                            ),
                        )
                        text = result_text(result)
                        lower_text = text.lower()
                        if getattr(result, "isError", False):
                            raise RuntimeError(text[:500] or "MCP tool returned an error")
                        if "tool not found" in lower_text:
                            raise RuntimeError(text[:500] or "MCP tool not found")
                        if "failed to fetch" in lower_text:
                            raise RuntimeError(text[:500] or "MCP failed to fetch data")

                        flights.extend(
                            parse_flights(
                                text, current, origin, destination
                            )
                        )
                    except Exception as exc:
                        errors.append(
                            f"{origin}->{destination} {current.isoformat()}: {exc}"
                        )

                    done += 1
                    if done % 50 == 0 or done == expected:
                        print(
                            f"MCP progress: {done}/{expected}; "
                            f"flights={len(flights)}; errors={len(errors)}"
                        )
                    current += timedelta(days=1)

                route_count = len(flights) - route_start_count
                route_errors = len(errors) - route_error_start
                print(
                    f"ROUTE RESULT {origin}->{destination}: "
                    f"{route_count} flights; {route_errors} errors"
                )

            return flights, errors, tool_name, available_tools


def deduplicate(flights):
    unique = {}
    for flight in flights:
        key = (
            flight["flight_number"],
            flight["origin"],
            flight["destination"],
            flight["departure"].strftime("%Y%m%dT%H%M"),
        )
        unique[key] = flight
    return sorted(unique.values(), key=lambda flight: flight["departure"])


def make_uid(flight):
    return (
        "-".join(
            [
                flight["flight_number"],
                flight["origin"],
                flight["destination"],
                flight["departure"].strftime("%Y%m%dT%H%M"),
            ]
        )
        + "@hfedder-ryanair-calendar"
    )


def build_ics(flights):
    now = datetime.now(timezone.utc)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//hfedder//Ryanair Piemonte Calendar//DA",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Ryanair Piemonte - CPH",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]

    for flight in flights:
        dep_tz = "Europe/Copenhagen" if flight["origin"] == "CPH" else "Europe/Rome"
        arr_tz = (
            "Europe/Copenhagen"
            if flight["destination"] == "CPH"
            else "Europe/Rome"
        )
        summary = (
            f"Flight {flight['flight_number']} "
            f"{flight['origin']} -> {flight['destination']}"
        )
        description = (
            f"Datakilde: {flight['source']}. "
            "Kontroller altid Ryanair for booking."
        )
        if flight.get("price") is not None:
            description += (
                f" Observeret pris: {flight['price']} "
                f"{flight.get('currency') or 'EUR'}."
            )

        lines.extend(
            [
                "BEGIN:VEVENT",
                "UID:" + make_uid(flight),
                "DTSTAMP:" + now.strftime("%Y%m%dT%H%M%SZ"),
                "DTSTART;TZID="
                + dep_tz
                + ":"
                + flight["departure"].strftime("%Y%m%dT%H%M%S"),
                "DTEND;TZID="
                + arr_tz
                + ":"
                + flight["arrival"].strftime("%Y%m%dT%H%M%S"),
                "SUMMARY:" + escape_ics(summary),
                "LOCATION:"
                + escape_ics(
                    flight["origin"] + " -> " + flight["destination"]
                ),
                "DESCRIPTION:" + escape_ics(description),
                "STATUS:CONFIRMED",
                "TRANSP:TRANSPARENT",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ics_line(line) for line in lines) + "\r\n"


def validate(flights, errors, selected_tool, available_tools):
    counts = {f"{origin}->{destination}": 0 for origin, destination in ROUTES}
    for flight in flights:
        route_name = f"{flight['origin']}->{flight['destination']}"
        if route_name in counts:
            counts[route_name] += 1

    total_requests = (DAYS_AHEAD + 1) * len(ROUTES)
    maximum_allowed_errors = int(total_requests * 0.10)
    checks = {
        "minimum_total_100": len(flights) >= 100,
        "all_routes_present": all(count > 0 for count in counts.values()),
        "bergamo_both_directions_present": (
            counts["BGY->CPH"] > 0 and counts["CPH->BGY"] > 0
        ),
        "turin_both_directions_present": (
            counts["TRN->CPH"] > 0 and counts["CPH->TRN"] > 0
        ),
        "mcp_errors_below_10_percent": len(errors) <= maximum_allowed_errors,
    }

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "publish_requested": PUBLISH,
        "mode": "mcp-autodiscovery-safe",
        "days_ahead": DAYS_AHEAD,
        "selected_tool": selected_tool,
        "available_tools": available_tools,
        "total_flights": len(flights),
        "route_counts": counts,
        "mcp_request_count": total_requests,
        "maximum_allowed_mcp_errors": maximum_allowed_errors,
        "mcp_error_count": len(errors),
        "mcp_errors_sample": errors[:10],
        "checks": checks,
        "validation_passed": all(checks.values()),
    }

    REPORT_FILE.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))

    if not report["validation_passed"]:
        failed_checks = [name for name, passed in checks.items() if not passed]
        raise RuntimeError(
            "Safety stop: MCP candidate failed validation. Production ICS was "
            "not changed. Failed checks: "
            + ", ".join(failed_checks)
        )


async def main():
    flights, errors, selected_tool, available_tools = await collect_flights()
    flights = deduplicate(flights)
    validate(flights, errors, selected_tool, available_tools)

    candidate = build_ics(flights)
    CANDIDATE_FILE.write_text(candidate, encoding="utf-8", newline="")
    print("Candidate calendar written with", len(flights), "flights")

    if not PUBLISH:
        print(
            "SAFE MODE: PUBLISH_CALENDAR is not true. "
            "Production ICS was not changed."
        )
        return

    if OUTPUT_FILE.exists():
        shutil.copy2(OUTPUT_FILE, BACKUP_FILE)
        print("Existing production calendar backed up to:", BACKUP_FILE)

    temporary_file = OUTPUT_FILE.with_suffix(".ics.tmp")
    temporary_file.write_text(candidate, encoding="utf-8", newline="")
    temporary_file.replace(OUTPUT_FILE)
    print("Production ICS published atomically:", OUTPUT_FILE)


if __name__ == "__main__":
    asyncio.run(main())
