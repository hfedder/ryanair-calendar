import asyncio
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

ROUTES = [("TRN", "CPH"), ("BGY", "CPH"), ("CPH", "TRN"), ("CPH", "BGY")]
DAYS_AHEAD = 365
PUBLISH = os.getenv("PUBLISH_CALENDAR", "false").lower() == "true"
ROME = ZoneInfo("Europe/Rome")
COPENHAGEN = ZoneInfo("Europe/Copenhagen")


def airport_tz(code):
    return COPENHAGEN if code == "CPH" else ROME


def escape_ics(value):
    return (str(value).replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n"))


def fold_ics_line(line):
    raw = line.encode("utf-8")
    parts = []
    while len(raw) > 73:
        cut = 73
        while cut > 0 and (raw[cut] & 0xC0) == 0x80:
            cut -= 1
        parts.append(raw[:cut].decode("utf-8"))
        raw = raw[cut:]
    parts.append(raw.decode("utf-8"))
    return "\r\n ".join(parts)


def result_text(result):
    return "\n".join(item.text for item in result.content if hasattr(item, "text") and item.text)


def parse_flights(text, requested_date, origin, destination):
    # Accepts the concise Ryanair MCP text format already observed in our successful tests:
    # FR5089: TRN 09:20 -> CPH 11:30 | 66.44 EUR
    pattern = re.compile(
        r"\b(FR\s?\d{1,5})\s*:\s*([A-Z]{3})\s+([0-2]\d:[0-5]\d)\s*"
        r"(?:→|->|-)\s*([A-Z]{3})\s+([0-2]\d:[0-5]\d)"
        r"(?:\s*\|\s*([0-9]+(?:[.,][0-9]+)?)\s*([A-Z]{3}))?",
        re.IGNORECASE,
    )
    flights = []
    for m in pattern.finditer(text):
        found_origin, found_destination = m.group(2).upper(), m.group(4).upper()
        if found_origin != origin or found_destination != destination:
            continue
        dep = datetime.combine(requested_date, datetime.strptime(m.group(3), "%H:%M").time(), airport_tz(origin))
        arr = datetime.combine(requested_date, datetime.strptime(m.group(5), "%H:%M").time(), airport_tz(destination))
        if arr.astimezone(timezone.utc) <= dep.astimezone(timezone.utc):
            arr += timedelta(days=1)
        flights.append({
            "flight_number": m.group(1).replace(" ", "").upper(),
            "origin": origin,
            "destination": destination,
            "departure": dep,
            "arrival": arr,
            "price": m.group(6).replace(",", ".") if m.group(6) else None,
            "currency": m.group(7) or "EUR",
            "source": "Ryanair MCP",
        })
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
    raise RuntimeError("No supported Ryanair flight-search MCP tool is currently exposed. Available: " + ", ".join(names))


def arguments_for(tool_name, origin, destination, flight_date):
    if tool_name in ("ryanair.search_flights", "search_flights"):
        return {
            "origin": origin,
            "destination": destination,
            "dateOut": flight_date.isoformat(),
            "roundTrip": "false",
            "passengers": {"adults": 1, "teens": 0, "children": 0, "infants": 0},
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
    flights, errors = [], []
    async with streamable_http_client(MCP_URL) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            listed = await session.list_tools()
            tool_name = choose_tool(listed.tools)
            print("Selected MCP tool:", tool_name)
            expected = (DAYS_AHEAD + 1) * len(ROUTES)
            done = 0
            for origin, destination in ROUTES:
                current = today
                while current <= last_date:
                    try:
                        result = await session.call_tool(tool_name, arguments=arguments_for(tool_name, origin, destination, current))
                        text = result_text(result)
                        if getattr(result, "isError", False) or "tool not found" in text.lower() or "failed to fetch" in text.lower():
                            raise RuntimeError(text[:500] or "MCP tool error")
                        flights.extend(parse_flights(text, current, origin, destination))
                    except Exception as exc:
                        errors.append(f"{origin}->{destination} {current}: {exc}")
                    done += 1
                    if done % 50 == 0 or done == expected:
                        print(f"MCP progress: {done}/{expected}; flights={len(flights)}; errors={len(errors)}")
                    current += timedelta(days=1)
            return flights, errors, tool_name, [tool.name for tool in listed.tools]


def deduplicate(flights):
    unique = {}
    for f in flights:
        key = (f["flight_number"], f["origin"], f["destination"], f["departure"].strftime("%Y%m%dT%H%M"))
        unique[key] = f
    return sorted(unique.values(), key=lambda f: f["departure"])


def make_uid(flight):
    return "-".join([flight["flight_number"], flight["origin"], flight["destination"], flight["departure"].strftime("%Y%m%dT%H%M")]) + "@hfedder-ryanair-calendar"


def build_ics(flights):
    now = datetime.now(timezone.utc)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//hfedder//Ryanair Piemonte Calendar//DA", "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Ryanair Piemonte - CPH", "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    for f in flights:
        dep_tz = "Europe/Copenhagen" if f["origin"] == "CPH" else "Europe/Rome"
        arr_tz = "Europe/Copenhagen" if f["destination"] == "CPH" else "Europe/Rome"
        summary = "✈ " + f["flight_number"] + " " + f["origin"] + " -> " + f["destination"]
        description = "Datakilde: " + f["source"] + ". Kontrollér altid Ryanair før booking."
        if f.get("price") is not None:
            description += " Observeret pris: " + str(f["price"]) + " " + str(f.get("currency") or "EUR") + "."
        lines.extend(["BEGIN:VEVENT", "UID:" + make_uid(f), "DTSTAMP:" + now.strftime("%Y%m%dT%H%M%SZ"), "DTSTART;TZID=" + dep_tz + ":" + f["departure"].strftime("%Y%m%dT%H%M%S"), "DTEND;TZID=" + arr_tz + ":" + f["arrival"].strftime("%Y%m%dT%H%M%S"), "SUMMARY:" + escape_ics(summary), "LOCATION:" + escape_ics(f["origin"] + " -> " + f["destination"]), "DESCRIPTION:" + escape_ics(description), "STATUS:CONFIRMED", "TRANSP:TRANSPARENT", "END:VEVENT"])
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ics_line(line) for line in lines) + "\r\n"


def validate(flights, errors, selected_tool, available_tools):
    counts = {o + "->" + d: 0 for o, d in ROUTES}
    for f in flights:
        counts[f["origin"] + "->" + f["destination"]] += 1
    checks = {"minimum_total_100": len(flights) >= 100, "all_routes_present": all(v > 0 for v in counts.values()), "mcp_errors_below_10_percent": len(errors) <= 146}
    report = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "publish_requested": PUBLISH, "mode": "mcp-autodiscovery-safe", "selected_tool": selected_tool, "available_tools": available_tools, "total_flights": len(flights), "route_counts": counts, "mcp_error_count": len(errors), "mcp_errors_sample": errors[:10], "checks": checks, "validation_passed": all(checks.values())}
    REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not report["validation_passed"]:
        raise RuntimeError("Safety stop: MCP candidate failed validation; production ICS was not changed")


async def main():
    flights, errors, selected_tool, available_tools = await collect_flights()
    flights = deduplicate(flights)
    validate(flights, errors, selected_tool, available_tools)
    candidate = build_ics(flights)
    CANDIDATE_FILE.write_text(candidate, encoding="utf-8", newline="")
    print("Candidate calendar written with", len(flights), "flights")
    if not PUBLISH:
        print("SAFE MODE: PUBLISH_CALENDAR is not true. Production ICS was not changed.")
        return
    if OUTPUT_FILE.exists():
        shutil.copy2(OUTPUT_FILE, BACKUP_FILE)
    temp = OUTPUT_FILE.with_suffix(".ics.tmp")
    temp.write_text(candidate, encoding="utf-8", newline="")
    temp.replace(OUTPUT_FILE)
    print("Production ICS published atomically; previous file backed up")


if __name__ == "__main__":
    asyncio.run(main())
