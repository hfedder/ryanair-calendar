from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import shutil

OUTPUT_FILE = Path("Ryanair_TRN_BGY_CPH_verificerede_afgange.ics")
BACKUP_FILE = Path("Ryanair_TRN_BGY_CPH_verificerede_afgange.backup.ics")

ROME = ZoneInfo("Europe/Rome")
COPENHAGEN = ZoneInfo("Europe/Copenhagen")

# Static verified schedule rules. Extend this list when a new Ryanair season is published.
# weekday: Monday=0 ... Sunday=6
RULES = [
    # TRN -> CPH, FR5089
    ("FR5089", "TRN", "CPH", 0, "09:20", "11:30", "2026-11-21", "2027-03-22"),
    ("FR5089", "TRN", "CPH", 5, "08:25", "10:35", "2026-11-21", "2027-03-27"),
    ("FR5089", "TRN", "CPH", 1, "11:05", "13:15", "2026-12-22", "2026-12-29"),
    ("FR5089", "TRN", "CPH", 3, "06:10", "08:20", "2026-12-24", "2026-12-31"),
    ("FR5089", "TRN", "CPH", 3, "16:30", "18:40", "2027-03-25", "2027-03-25"),
    ("FR5089", "TRN", "CPH", 6, "08:35", "10:40", "2027-03-28", "2027-10-24"),
    ("FR5089", "TRN", "CPH", 2, "09:00", "11:05", "2027-03-31", "2027-10-27"),

    # BGY -> CPH, FR2990 (published winter schedule portions verified)
    ("FR2990", "BGY", "CPH", 3, "21:55", "00:05", "2026-11-21", "2026-11-26"),
    ("FR2990", "BGY", "CPH", 4, "19:45", "21:55", "2026-11-21", "2026-11-27"),
    ("FR2990", "BGY", "CPH", 5, "08:35", "10:45", "2026-11-21", "2026-12-12"),
    ("FR2990", "BGY", "CPH", 0, "22:00", "00:10", "2026-11-21", "2026-11-30"),
    ("FR2990", "BGY", "CPH", 6, "09:30", "11:40", "2026-11-22", "2027-01-03"),
    ("FR2990", "BGY", "CPH", 2, "19:35", "21:45", "2026-11-25", "2026-12-09"),
    ("FR2990", "BGY", "CPH", 3, "21:55", "00:05", "2026-12-03", "2026-12-10"),

    # CPH -> BGY, FR2989. Verified Nov 2026 recurring pattern.
    ("FR2989", "CPH", "BGY", 0, "19:25", "21:35", "2026-11-21", "2026-11-30"),
    ("FR2989", "CPH", "BGY", 2, "16:35", "18:45", "2026-11-21", "2026-11-30"),
    ("FR2989", "CPH", "BGY", 3, "19:20", "21:30", "2026-11-21", "2026-11-30"),
    ("FR2989", "CPH", "BGY", 4, "22:20", "00:30", "2026-11-21", "2026-11-30"),
    ("FR2989", "CPH", "BGY", 5, "06:00", "08:10", "2026-11-21", "2026-11-30"),
    ("FR2989", "CPH", "BGY", 6, "06:55", "09:05", "2026-11-21", "2026-11-30"),

    # CPH -> TRN intentionally left empty until its current Ryanair flight number/times are verified.
]


def tz(code):
    return COPENHAGEN if code == "CPH" else ROME


def parse_date(value):
    return date.fromisoformat(value)


def dt(day, hhmm, airport):
    return datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time(), tz(airport))


def escape(value):
    return str(value).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def expand_rules():
    flights = []
    today = date.today()
    for number, origin, destination, weekday, dep_time, arr_time, start_s, end_s in RULES:
        start = max(today, parse_date(start_s))
        end = parse_date(end_s)
        current = start
        while current <= end:
            if current.weekday() == weekday:
                departure = dt(current, dep_time, origin)
                arrival = dt(current, arr_time, destination)
                if arrival.astimezone(timezone.utc) <= departure.astimezone(timezone.utc):
                    arrival += timedelta(days=1)
                flights.append((number, origin, destination, departure, arrival))
            current += timedelta(days=1)
    return sorted(set(flights), key=lambda f: f[3])


def build_ics(flights):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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
    for number, origin, destination, departure, arrival in flights:
        uid = f"{number}-{origin}-{destination}-{departure.strftime('%Y%m%dT%H%M')}@hfedder-ryanair-calendar"
        dep_tz = "Europe/Copenhagen" if origin == "CPH" else "Europe/Rome"
        arr_tz = "Europe/Copenhagen" if destination == "CPH" else "Europe/Rome"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;TZID={dep_tz}:{departure.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND;TZID={arr_tz}:{arrival.strftime('%Y%m%dT%H%M%S')}",
            "SUMMARY:" + escape(f"Flight {number} {origin} -> {destination}"),
            "LOCATION:" + escape(f"{origin} -> {destination}"),
            "DESCRIPTION:" + escape("Statisk verificeret Ryanair-flyplan. Kontroller altid Ryanair foer booking."),
            "STATUS:CONFIRMED",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def main():
    flights = expand_rules()
    counts = {"TRN->CPH": 0, "BGY->CPH": 0, "CPH->TRN": 0, "CPH->BGY": 0}
    for _, origin, destination, _, _ in flights:
        counts[f"{origin}->{destination}"] += 1

    print("Static verified Ryanair calendar")
    for route, count in counts.items():
        print(f"{route}: {count} flights")

    if OUTPUT_FILE.exists():
        shutil.copy2(OUTPUT_FILE, BACKUP_FILE)
    OUTPUT_FILE.write_text(build_ics(flights), encoding="utf-8", newline="")
    print(f"Published {len(flights)} flights to {OUTPUT_FILE}")
    if counts["CPH->TRN"] == 0:
        print("WARNING: CPH->TRN is still awaiting verified schedule data and is not published.")


if __name__ == "__main__":
    main()
