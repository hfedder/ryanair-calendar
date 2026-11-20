import asyncio
import json
import os
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

MCP_URL = "https://services-api.ryanair.com/mcp-server/mcp"
AVAILABILITY_URL = "https://www.ryanair.com/api/booking/v4/en-gb/availability"

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


def parse_mcp_text(text, requested_date, origin, destination):
    pattern = re.compile(
        r"\b(FR\s?\d{1,5})\s*:\s*([A-Z]{3})\s+([0-2]\d:[0-5]\d)\s*"
        r"(?:→|->|-)\s*([A-Z]{3})\s+([0-2]\d:[0-5]\d)"
        r"(?:\s*\|\s*([0-9]+(?:[.,][0-9]+)?)\s*([A-Z]{3}))?",
        re.IGNORECASE,
    )
    flights = []
    for match in pattern.finditer(text):
        number = match.group(1).replace(" ", "").upper()
        found_origin = match.group(2).upper()
        found_destination = match.group(4).upper()
        if found_origin != origin or found_destination != destination:
            continue
        dep = datetime.combine(
            requested_date, datetime.strptime(match.group(3), "%H:%M").time(), airport_tz(origin)
        )
        arr = datetime.combine(
            requested_date, datetime.strptime(match.group(5), "%H:%M").time(), airport_tz(destination)
        )
        if arr.astimezone(timezone.utc) <= dep.astimezone(timezone.utc):
            arr += timedelta(days=1)
        flights.append({
            "flight_number": number,
            "origin": origin,
            "destination": destination,
            "departure": dep,
            "arrival": arr,
            "price": match.group(6).replace(",", ".") if match.group(6) else None,
            "currency": match.group(7),
            "source": "Ryanair MCP cheapest one-way",
        })
    return flights


def extract_mcp_text(result):
    return "\n".join(
        item.text for item in result.content if hasattr(item, "text") and item.text
    )


async def collect_mcp_flights():
    today = date.today()
    last_date = today + timedelta(days=DAYS_AHEAD)
    flights = []
    errors = []

    async with streamable_http_client(MCP_URL) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            for origin, destination in ROUTES:
                current = today
                while current <= last_date:
                    arguments = {
                        "outboundDepartureDateFrom": current.isoformat(),
                        "outboundDepartureDateTo": current.isoformat(),
                        "departureIataCodes": [origin],
                        "arrivalIataCodes": [destination],
                        "currency": "EUR",
                        "limit": 50,
                        "pageNumber": 0,
                    }
                    try:
                        result = await session.call_tool(
                            "pricing-get-cheapest-one-way-flight", arguments=arguments
                        )
                        text = extract_mcp_text(result)
                        if "failed to fetch" in text.lower() or "bad request" in text.lower():
                            raise RuntimeError(text[:500])
                        flights.extend(parse_mcp_text(text, current, origin, destination))
                    except Exception as exc:
                        errors.append(f"{origin}->{destination} {current}: {exc}")
                    current += timedelta(days=1)
    return flights, errors


def parse_availability_payload(payload, origin, destination, first_date, last_date):
    flights = []
    for trip in payload.get("trips", []):
        if trip.get("origin") != origin or trip.get("destination") != destination:
            continue
        for day in trip.get("dates", []):
            for item in day.get("flights", []):
                number = str(item.get("flightNumber") or "").replace(" ", "")
                times = item.get("time") or []
                if not number or len(times) < 2:
                    continue
                try:
                    dep = datetime.fromisoformat(str(times[0]).replace("Z", "+00:00"))
                    arr = datetime.fromisoformat(str(times[1]).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dep.tzinfo is None:
                    dep = dep.replace(tzinfo=airport_tz(origin))
                else:
                    dep = dep.astimezone(airport_tz(origin))
                if arr.tzinfo is None:
                    arr = arr.replace(tzinfo=airport_tz(destination))
                else:
                    arr = arr.astimezone(airport_tz(destination))
                if not first_date <= dep.date() <= last_date:
                    continue
                fare = None
                fares = (item.get("regularFare") or {}).get("fares") or []
                if fares:
                    fare = fares[0].get("amount")
                flights.append({
                    "flight_number": number if number.startswith("FR") else "FR" + number,
                    "origin": origin,
                    "destination": destination,
                    "departure": dep,
                    "arrival": arr,
                    "price": fare,
                    "currency": payload.get("currency", "EUR"),
                    "source": "Ryanair availability supplement",
                })
    return flights


def try_availability_supplement():
    today = date.today()
    last_date = today + timedelta(days=DAYS_AHEAD)
    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; hfedder-ryanair-calendar/1.0)",
    })
    flights = []
    errors = []
    for origin, destination in ROUTES:
        cursor = today
        while cursor <= last_date:
            params = {
                "ADT": 1, "CHD": 0, "INF": 0, "TEEN": 0, "Disc": 0,
                "DateIn": "", "DateOut": cursor.isoformat(),
                "Origin": origin, "Destination": destination,
                "RoundTrip": "false", "ToUs": "AGREED",
                "IncludeConnectingFlights": "false", "promoCode": "",
                "FlexDaysBeforeOut": 0, "FlexDaysOut": 6,
                "FlexDaysBeforeIn": 0, "FlexDaysIn": 0,
            }
            try:
                response = session.get(AVAILABILITY_URL, params=params, timeout=25)
                if response.status_code == 409:
                    errors.append(f"{origin}->{destination}: availability disabled after HTTP 409")
                    break
                response.raise_for_status()
                flights.extend(parse_availability_payload(
                    response.json(), origin, destination, today, last_date
                ))
            except Exception as exc:
                errors.append(f"{origin}->{destination} {cursor}: {exc}")
                break
            cursor += timedelta(days=7)
    return flights, errors


def deduplicate(flights):
    unique = {}
    for flight in flights:
        key = (
            flight["flight_number"], flight["origin"], flight["destination"],
            flight["departure"].strftime("%Y%m%dT%H%M"),
        )
        current = unique.get(key)
        if current is None or flight["source"].startswith("Ryanair availability"):
            unique[key] = flight
    return sorted(unique.values(), key=lambda item: item["departure"])


def make_uid(flight):
    return "-".join([
        flight["flight_number"], flight["origin"], flight["destination"],
        flight["departure"].strftime("%Y%m%dT%H%M"),
    ]) + "@hfedder-ryanair-calendar"


def build_ics(flights):
    now = datetime.now(timezone.utc)
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//hfedder//Ryanair Piemonte Calendar//DA",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "X-WR-CALNAME:Ryanair Piemonte - CPH",
        "X-WR-CALDESC:Ryanair afgange mellem København og Piemonte",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H",
    ]
    for flight in flights:
        dep_tz = "Europe/Copenhagen" if flight["origin"] == "CPH" else "Europe/Rome"
        arr_tz = "Europe/Copenhagen" if flight["destination"] == "CPH" else "Europe/Rome"
        summary = "✈ " + flight["flight_number"] + " " + flight["origin"] + " -> " + flight["destination"]
        description = "Datakilde: " + flight["source"] + ". Kontrollér altid Ryanair før booking."
        if flight.get("price") is not None:
            description += " Observeret pris: " + str(flight["price"]) + " " + str(flight.get("currency") or "EUR") + "."
        lines.extend([
            "BEGIN:VEVENT", "UID:" + make_uid(flight),
            "DTSTAMP:" + now.strftime("%Y%m%dT%H%M%SZ"),
            "LAST-MODIFIED:" + now.strftime("%Y%m%dT%H%M%SZ"),
            "DTSTART;TZID=" + dep_tz + ":" + flight["departure"].strftime("%Y%m%dT%H%M%S"),
            "DTEND;TZID=" + arr_tz + ":" + flight["arrival"].strftime("%Y%m%dT%H%M%S"),
            "SUMMARY:" + escape_ics(summary),
            "LOCATION:" + escape_ics(flight["origin"] + " -> " + flight["destination"]),
            "DESCRIPTION:" + escape_ics(description),
            "STATUS:CONFIRMED", "TRANSP:TRANSPARENT", "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ics_line(line) for line in lines) + "\r\n"


def validate(flights, mcp_errors, availability_errors, supplement_count):
    counts = {origin + "->" + destination: 0 for origin, destination in ROUTES}
    for flight in flights:
        counts[flight["origin"] + "->" + flight["destination"]] += 1
    checks = {
        "minimum_total_100": len(flights) >= 100,
        "all_routes_present": all(value > 0 for value in counts.values()),
        "turin_plausible": 40 <= counts["TRN->CPH"] <= 250 and 40 <= counts["CPH->TRN"] <= 250,
        "bergamo_plausible": counts["BGY->CPH"] >= 250 and counts["CPH->BGY"] >= 250,
        "mcp_errors_below_10_percent": len(mcp_errors) <= 146,
    }
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "publish_requested": PUBLISH,
        "mode": "hybrid-mcp-with-optional-availability-supplement",
        "total_flights": len(flights),
        "route_counts": counts,
        "availability_supplement_flights": supplement_count,
        "mcp_error_count": len(mcp_errors),
        "availability_errors": availability_errors,
        "checks": checks,
        "validation_passed": all(checks.values()),
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not report["validation_passed"]:
        raise RuntimeError("Safety stop: hybrid candidate failed validation; production ICS was not changed")


async def main():
    mcp_flights, mcp_errors = await collect_mcp_flights()
    availability_flights, availability_errors = try_availability_supplement()
    flights = deduplicate(mcp_flights + availability_flights)
    validate(flights, mcp_errors, availability_errors, len(availability_flights))

    candidate = build_ics(flights)
    CANDIDATE_FILE.write_text(candidate, encoding="utf-8", newline="")
    print("Candidate calendar written with", len(flights), "flights")

    if not PUBLISH:
        print("SAFE MODE: PUBLISH_CALENDAR is not true. Production ICS was not changed.")
        return

    if OUTPUT_FILE.exists():
        shutil.copy2(OUTPUT_FILE, BACKUP_FILE)
    temp_file = OUTPUT_FILE.with_suffix(".ics.tmp")
    temp_file.write_text(candidate, encoding="utf-8", newline="")
    temp_file.replace(OUTPUT_FILE)
    print("Production ICS published atomically; previous file backed up")


if __name__ == "__main__":
    asyncio.run(main())
