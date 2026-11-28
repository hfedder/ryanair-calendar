#!/usr/bin/env python3
"""Read-only test of Ryanair's published monthly timetables, December 2026.

Uses the unofficial ryanair-mcp project's documented timetable API family.
Does not modify or publish any calendar.
"""
import json
import sys
import urllib.error
import urllib.request

ROUTES = [("CPH", "BGY"), ("BGY", "CPH"), ("CPH", "TRN"), ("TRN", "CPH")]
YEAR, MONTH = 2026, 12
# Candidate host/path used by the unofficial Ryanair integrations; endpoint is not guaranteed.
URL = "https://services-api.ryanair.com/timtbl/3/schedules/{origin}/{destination}/years/{year}/months/{month}"


def get_schedule(origin, destination):
    url = URL.format(origin=origin, destination=destination, year=YEAR, month=MONTH)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 ryanair-calendar-test/1.0"})
    with urllib.request.urlopen(req, timeout=25) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("Expected JSON object")
    return payload


def main():
    print("Ryanair timetable test: December 2026; read-only, no ICS changes", flush=True)
    success = 0
    for origin, destination in ROUTES:
        print(f"\n=== {origin} -> {destination} ===", flush=True)
        try:
            data = get_schedule(origin, destination)
            print("Response keys:", sorted(data.keys()))
            print("Response sample:", json.dumps(data, ensure_ascii=False, indent=2)[:9000])
            days = data.get("days")
            if isinstance(days, list):
                flight_count = sum(len(day.get("flights", [])) for day in days if isinstance(day, dict) and isinstance(day.get("flights"), list))
                print(f"Days returned: {len(days)}; flight entries: {flight_count}")
                if flight_count:
                    success += 1
                else:
                    print("WARNING: No flight entries; not confirmed as complete timetable")
            else:
                print("WARNING: No 'days' array; inspect JSON schema before using")
        except urllib.error.HTTPError as exc:
            print(f"HTTP {exc.code}: {exc.read(500).decode('utf-8', 'replace')}")
        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}")
    print(f"\nRoutes with at least one flight entry: {success}/{len(ROUTES)}")
    print("NOTE: A successful response does not prove all flights are included.")
    return 0 if success == len(ROUTES) else 1


if __name__ == "__main__":
    sys.exit(main())
