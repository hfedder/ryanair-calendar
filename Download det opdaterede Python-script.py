import json
import os
import re
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

OUTPUT_FILE = Path("Ryanair_TRN_BGY_CPH_verificerede_afgange.ics")
CANDIDATE_FILE = Path("Ryanair_TRN_BGY_CPH_candidate.ics")
BACKUP_FILE = Path("Ryanair_TRN_BGY_CPH_verificerede_afgange.backup.ics")
REPORT_FILE = Path("ryanair_validation_report.json")

ROUTES = [("TRN", "CPH"), ("BGY", "CPH"), ("CPH", "TRN"), ("CPH", "BGY")]
DAYS_AHEAD = 365
WINDOW_DAYS = 6
TIMEOUT_SECONDS = 30
MAX_RETRIES = 3
PUBLISH = os.getenv("PUBLISH_CALENDAR", "false").lower() == "true"

API_URL = "https://www.ryanair.com/api/booking/v4/en-gb/availability"
ITALY_TZ = ZoneInfo("Europe/Rome")
DENMARK_TZ = ZoneInfo("Europe/Copenhagen")


def airport_tz(code):
    return DENMARK_TZ if code == "CPH" else ITALY_TZ


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


def parse_dt(value, tz):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def flight_from_key(key, origin, destination):
    if not key:
        return None
    match = re.search(
        r"(FR)~?(\d{1,5}).*?([A-Z]{3})~(\d{2}/\d{2}/\d{4} \d{2}:\d{2})~"
        r"([A-Z]{3})~(\d{2}/\d{2}/\d{4} \d{2}:\d{2})",
        key,
    )
    if not match:
        return None
    key_origin, dep_text, key_destination, arr_text = match.group(3), match.group(4), match.group(5), match.group(6)
    if key_origin != origin or key_destination != destination:
        return None
    dep = datetime.strptime(dep_text, "%m/%d/%Y %H:%M").replace(tzinfo=airport_tz(origin))
    arr = datetime.strptime(arr_text, "%m/%d/%Y %H:%M").replace(tzinfo=airport_tz(destination))
    return "FR" + match.group(2), dep, arr


def parse_flight(item, origin, destination):
    number = str(item.get("flightNumber") or item.get("flightNo") or "").replace(" ", "")
    local_times = item.get("time") or item.get("times") or []
    utc_times = item.get("timeUTC") or []
    dep = arr = None

    if isinstance(local_times, list) and len(local_times) >= 2:
        dep = parse_dt(local_times[0], airport_tz(origin))
        arr = parse_dt(local_times[1], airport_tz(destination))
    elif isinstance(utc_times, list) and len(utc_times) >= 2:
        dep = parse_dt(utc_times[0], timezone.utc)
        arr = parse_dt(utc_times[1], timezone.utc)
        if dep:
            dep = dep.astimezone(airport_tz(origin))
        if arr:
            arr = arr.astimezone(airport_tz(destination))

    if not number or not dep or not arr:
        fallback = flight_from_key(item.get("flightKey", ""), origin, destination)
        if fallback:
            number, dep, arr = fallback

    if not number or not dep or not arr:
        return None
    if not number.startswith("FR"):
        number = "FR" + number

    fare = None
    regular = item.get("regularFare") or {}
    fares = regular.get("fares") or []
    if fares:
        fare = fares[0].get("amount")

    return {
        "flight_number": number,
        "origin": origin,
        "destination": destination,
        "departure": dep,
        "arrival": arr,
        "price": fare,
    }


def fetch_window(session, origin, destination, start_date):
    params = {
        "ADT": 1, "CHD": 0, "INF": 0, "TEEN": 0, "Disc": 0,
        "DateIn": "", "DateOut": start_date.isoformat(),
        "Origin": origin, "Destination": destination,
        "RoundTrip": "false", "ToUs": "AGREED",
        "IncludeConnectingFlights": "false", "promoCode": "",
        "FlexDaysBeforeOut": 0, "FlexDaysOut": WINDOW_DAYS,
        "FlexDaysBeforeIn": 0, "FlexDaysIn": 0,
    }
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(API_URL, params=params, timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = str(exc)
            time.sleep(2 ** attempt)
    raise RuntimeError(last_error or "Unknown availability error")


def collect_flights():
    today = date.today()
    last_date = today + timedelta(days=DAYS_AHEAD)
    flights, errors = [], []
    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; hfedder-ryanair-calendar/1.0)",
    })

    for origin, destination in ROUTES:
        cursor = today
        while cursor <= last_date:
            try:
                payload = fetch_window(session, origin, destination, cursor)
                for trip in payload.get("trips", []):
                    if trip.get("origin") != origin or trip.get("destination") != destination:
                        continue
                    for day in trip.get("dates", []):
                        for item in day.get("flights", []):
                            flight = parse_flight(item, origin, destination)
                            if flight and today <= flight["departure"].date() <= last_date:
                                flights.append(flight)
            except Exception as exc:
                errors.append(f"{origin}->{destination} {cursor}: {exc}")
            cursor += timedelta(days=WINDOW_DAYS + 1)
            time.sleep(0.15)
    return flights, errors


def deduplicate(flights):
    unique = {}
    for f in flights:
        key = (f["flight_number"], f["origin"], f["destination"], f["departure"].isoformat())
        unique[key] = f
    return sorted(unique.values(), key=lambda f: f["departure"])


def make_uid(flight):
    return "-".join([
        flight["flight_number"], flight["origin"], flight["destination"],
        flight["departure"].strftime("%Y%m%dT%H%M")
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
    for f in flights:
        dep_tz = "Europe/Copenhagen" if f["origin"] == "CPH" else "Europe/Rome"
        arr_tz = "Europe/Copenhagen" if f["destination"] == "CPH" else "Europe/Rome"
        summary = "✈ " + f["flight_number"] + " " + f["origin"] + " -> " + f["destination"]
        description = "Alle bookbare afgange hentet fra Ryanair availability. Kontrollér altid Ryanair før booking."
        if f.get("price") is not None:
            description += " Observeret pris: " + str(f["price"]) + " EUR."
        lines.extend([
            "BEGIN:VEVENT", "UID:" + make_uid(f),
            "DTSTAMP:" + now.strftime("%Y%m%dT%H%M%SZ"),
            "LAST-MODIFIED:" + now.strftime("%Y%m%dT%H%M%SZ"),
            "DTSTART;TZID=" + dep_tz + ":" + f["departure"].strftime("%Y%m%dT%H%M%S"),
            "DTEND;TZID=" + arr_tz + ":" + f["arrival"].strftime("%Y%m%dT%H%M%S"),
            "SUMMARY:" + escape_ics(summary),
            "LOCATION:" + escape_ics(f["origin"] + " -> " + f["destination"]),
            "DESCRIPTION:" + escape_ics(description),
            "STATUS:CONFIRMED", "TRANSP:TRANSPARENT", "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ics_line(line) for line in lines) + "\r\n"


def validate(flights, errors):
    counts = {origin + "->" + destination: 0 for origin, destination in ROUTES}
    for f in flights:
        counts[f["origin"] + "->" + f["destination"]] += 1

    checks = {
        "minimum_total_100": len(flights) >= 100,
        "all_routes_present": all(value > 0 for value in counts.values()),
        "turin_plausible": 40 <= counts["TRN->CPH"] <= 250 and 40 <= counts["CPH->TRN"] <= 250,
        "bergamo_plausible": counts["BGY->CPH"] >= 250 and counts["CPH->BGY"] >= 250,
        "errors_below_10_percent": len(errors) <= 22,
    }
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "publish_requested": PUBLISH,
        "total_flights": len(flights),
        "route_counts": counts,
        "request_errors": errors,
        "checks": checks,
        "validation_passed": all(checks.values()),
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not report["validation_passed"]:
        raise RuntimeError("Safety stop: candidate calendar failed validation; production ICS was not changed")
    return report


def main():
    flights, errors = collect_flights()
    flights = deduplicate(flights)
    validate(flights, errors)

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
    main()
