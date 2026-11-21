import json
import sys
import urllib.error
import urllib.request
from datetime import date

ROUTES = [
    ("CPH", "BGY"),
    ("BGY", "CPH"),
    ("CPH", "TRN"),
    ("TRN", "CPH"),
]

BASE_URL = "https://api.ryanair.com/timetable/3/schedules/{origin}/{destination}/years/{year}/months/{month}"


def fetch(origin, destination, year, month):
    url = BASE_URL.format(
        origin=origin,
        destination=destination,
        year=year,
        month=month,
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "ryanair-calendar-probe/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
            print("=" * 80)
            print(f"ROUTE: {origin}->{destination}")
            print(f"URL: {url}")
            print(f"HTTP STATUS: {response.status}")
            print(f"CONTENT-TYPE: {response.headers.get('Content-Type')}")
            try:
                data = json.loads(body)
                print("JSON PARSE: OK")
                if isinstance(data, dict):
                    print("TOP-LEVEL KEYS:", ", ".join(data.keys()))
                    print("JSON SAMPLE:")
                    print(json.dumps(data, indent=2, ensure_ascii=False)[:6000])
                else:
                    print("JSON TYPE:", type(data).__name__)
                    print("JSON SAMPLE:")
                    print(json.dumps(data, indent=2, ensure_ascii=False)[:6000])
                return True
            except json.JSONDecodeError:
                print("JSON PARSE: FAILED")
                print("BODY SAMPLE:")
                print(body[:3000])
                return False
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print("=" * 80)
        print(f"ROUTE: {origin}->{destination}")
        print(f"URL: {url}")
        print(f"HTTP STATUS: {exc.code}")
        print("BODY SAMPLE:")
        print(body[:3000])
        return False
    except Exception as exc:
        print("=" * 80)
        print(f"ROUTE: {origin}->{destination}")
        print(f"URL: {url}")
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return False


def main():
    today = date.today()
    year = today.year
    month = today.month

    print(f"Testing Ryanair timetable endpoint for {year}-{month:02d}")
    print("This probe does not modify any ICS/calendar files.")

    successes = 0
    for origin, destination in ROUTES:
        if fetch(origin, destination, year, month):
            successes += 1

    print("=" * 80)
    print(f"RESULT: {successes}/{len(ROUTES)} routes returned valid JSON")

    if successes == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
