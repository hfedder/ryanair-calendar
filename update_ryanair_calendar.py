#!/usr/bin/env python3
"""Fetch Ryanair timetables and safely build an Outlook-compatible ICS calendar.

Default: candidate + validation report ONLY. Existing published ICS is untouched.
Set PUBLISH_CALENDAR=true to publish only if every route/month passes validation.
The timetable endpoint is unofficial/undocumented; verify results against Ryanair.
"""
import calendar
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROUTES = [("CPH", "BGY"), ("BGY", "CPH"), ("CPH", "TRN"), ("TRN", "CPH")]
URL = "https://services-api.ryanair.com/timtbl/3/schedules/{origin}/{destination}/years/{year}/months/{month}"
CANDIDATE = Path("Ryanair_TRN_BGY_CPH_candidate.ics")
REPORT = Path("ryanair_validation_report.json")
PUBLISHED = Path("Ryanair_TRN_BGY_CPH_verificerede_afgange.ics")
BACKUP = Path("Ryanair_TRN_BGY_CPH_verificerede_afgange.backup.ics")
TZ = {"CPH": ZoneInfo("Europe/Copenhagen"), "BGY": ZoneInfo("Europe/Rome"), "TRN": ZoneInfo("Europe/Rome")}


def bool_env(name):
    return os.getenv(name, "false").strip().lower() == "true"


def months_to_fetch():
    """START_MONTH=YYYY-MM; MONTHS_AHEAD=12, including start month."""
    start = os.getenv("START_MONTH", date.today().strftime("%Y-%m"))
    try:
        y, m = map(int, start.split("-"))
        date(y, m, 1)
        count = int(os.getenv("MONTHS_AHEAD", "12"))
        if not 1 <= count <= 24:
            raise ValueError("MONTHS_AHEAD must be between 1 and 24")
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid START_MONTH or MONTHS_AHEAD") from exc
    return [((y * 12 + m - 1 + n) // 12, (y * 12 + m - 1 + n) % 12 + 1) for n in range(count)]


def fetch(origin, destination, year, month):
    url = URL.format(origin=origin, destination=destination, year=year, month=month)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 ryanair-calendar/2.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                payload = json.load(response)
            if not isinstance(payload, dict) or not isinstance(payload.get("days"), list):
                raise ValueError("Unexpected timetable JSON: missing days list")
            return payload
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            if attempt == 2 or isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                raise
            time.sleep(2 ** attempt)


def local_datetime(day, clock, airport):
    if not isinstance(clock, str):
        raise ValueError("Missing time")
    t = datetime.strptime(clock, "%H:%M").time()
    return datetime.combine(day, t, TZ[airport])


def extract(payload, origin, destination, year, month):
    result = []
    seen_days = set()
    last_day = calendar.monthrange(year, month)[1]
    for entry in payload["days"]:
        if not isinstance(entry, dict):
            raise ValueError("Malformed day entry")
        number = entry.get("day")
        if type(number) is not int or not 1 <= number <= last_day or number in seen_days:
            raise ValueError(f"Invalid/duplicate day: {number!r}")
        seen_days.add(number)
        flights = entry.get("flights")
        if not isinstance(flights, list):
            raise ValueError(f"Missing flights list on day {number}")
        for flight in flights:
            if not isinstance(flight, dict):
                raise ValueError("Malformed flight")
            carrier = flight.get("carrierCode")
            number_str = str(flight.get("number", "")).strip()
            if carrier != "FR" or not number_str.isdigit():
                raise ValueError(f"Unexpected carrier or flight number: {flight!r}")
            d = date(year, month, number)
            departure = local_datetime(d, flight.get("departureTime"), origin)
            arrival = local_datetime(d, flight.get("arrivalTime"), destination)
            if arrival.astimezone(timezone.utc) <= departure.astimezone(timezone.utc):
                arrival += timedelta(days=1)
            duration = arrival.astimezone(timezone.utc) - departure.astimezone(timezone.utc)
            if not timedelta(minutes=30) <= duration <= timedelta(hours=5):
                raise ValueError(f"Implausible duration for FR{number_str} on {d}: {duration}")
            result.append((f"FR{number_str}", origin, destination, departure, arrival))
    return result


def esc(text):
    return str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    """Fold ICS content lines at 75 octets, preserving UTF-8 characters."""
    parts, current = [], ""
    for char in line:
        if len((current + char).encode("utf-8")) > (75 if not parts else 74):
            parts.append(current)
            current = char
        else:
            current += char
    parts.append(current)
    return "\r\n ".join(parts)


def make_ics(flights):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//hfedder//Ryanair Piemonte//DA", "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Ryanair Piemonte - CPH", "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    for number, origin, dest, dep, arr in flights:
        # Stable UID for same flight/date, even if Ryanair changes departure time.
        uid = f"{number}-{origin}-{dest}-{dep.date().isoformat()}@hfedder-ryanair-calendar"
        utc_dep = dep.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        utc_arr = arr.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines.extend(["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}", f"DTSTART:{utc_dep}", f"DTEND:{utc_arr}", f"SUMMARY:{esc(number + ' ' + origin + ' -> ' + dest)}", f"LOCATION:{esc(origin + ' -> ' + dest)}", "DESCRIPTION:" + esc("Published timetable (not a booking). Check Ryanair before travel."), "STATUS:CONFIRMED", "TRANSP:TRANSPARENT", "END:VEVENT"])
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


def main():
    publish = bool_env("PUBLISH_CALENDAR")
    report = {"generated_utc": datetime.now(timezone.utc).isoformat(), "source": URL, "publish_requested": publish, "published": False, "months": [], "errors": [], "counts": {}, "status": "failed"}
    collected = []
    try:
        months = months_to_fetch()
        for year, month in months:
            for origin, destination in ROUTES:
                label = f"{origin}->{destination} {year}-{month:02d}"
                try:
                    data = fetch(origin, destination, year, month)
                    flights = extract(data, origin, destination, year, month)
                    # Empty months may be legitimate, but cannot be certified complete.
                    if not flights:
                        raise ValueError("No flights returned; cannot validate completeness")
                    collected.extend(flights)
                    report["months"].append({"route": label, "flights": len(flights), "ok": True})
                    print(f"OK {label}: {len(flights)} flights", flush=True)
                except Exception as exc:
                    message = f"{label}: {type(exc).__name__}: {exc}"
                    report["errors"].append(message)
                    report["months"].append({"route": label, "ok": False, "error": str(exc)})
                    print("ERROR", message, flush=True)
        unique = {}
        for flight in collected:
            key = (flight[0], flight[1], flight[2], flight[3].date())
            if key in unique and unique[key] != flight:
                report["errors"].append(f"Duplicate flight/date with different times: {key}")
            unique[key] = flight
        flights = sorted(unique.values(), key=lambda f: f[3].astimezone(timezone.utc))
        report["counts"] = {f"{a}->{b}": sum(f[1] == a and f[2] == b for f in flights) for a, b in ROUTES}
        report["total_flights"] = len(flights)
        if flights:
            CANDIDATE.write_bytes(make_ics(flights).encode("utf-8"))
        else:
            # Prevent stale candidate from passing a later workflow check.
            CANDIDATE.unlink(missing_ok=True)
        if not report["errors"] and flights:
            report["status"] = "validated_structure_not_completeness"
            if publish:
                if PUBLISHED.exists():
                    shutil.copy2(PUBLISHED, BACKUP)
                PUBLISHED.write_bytes(CANDIDATE.read_bytes())
                report["published"] = True
                print(f"Published {len(flights)} flights to {PUBLISHED}")
        else:
            print("Publication blocked: incomplete or invalid data", flush=True)
    except Exception as exc:
        report["errors"].append(f"Fatal: {type(exc).__name__}: {exc}")
    finally:
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Validation report: {REPORT}; status={report['status']}; published={report['published']}")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
