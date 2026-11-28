#!/usr/bin/env python3
"""Build a SEPARATE candidate rail ICS from the v4 December 2026 JSON report.

Run after test_tog_december_2026_v4.py. No Ryanair ICS is modified.
Output is a test candidate, NOT automatically published to Outlook.
"""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPORT = Path('tog_december_2026_test.json')
OUTPUT = Path('Piemonte_tog_december_2026_candidate.ics')
ROME = ZoneInfo('Europe/Rome')
TARGETS = ('NIZZA MONFERRATO', 'ASTI', 'ALESSANDRIA')


def escape(s):
    return str(s).replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\n', '\\n')


def fold(s):
    chunks = []
    chunk = ''
    limit = 75
    for char in s:
        if len((chunk + char).encode('utf-8')) > limit:
            chunks.append(chunk)
            chunk = char
            limit = 74
        else:
            chunk += char
    chunks.append(chunk)
    return '\r\n '.join(chunks)


def local_dt(day, clock):
    hh, mm = map(int, clock.split(':'))
    if not 0 <= mm <= 59 or hh < 0 or hh > 47:
        raise ValueError(f'Invalid GTFS clock {clock!r}')
    return datetime.combine(day + timedelta(days=hh // 24), datetime.min.time(), ROME) + timedelta(hours=hh % 24, minutes=mm)


def main():
    if not REPORT.is_file():
        raise FileNotFoundError(f'Missing {REPORT}; run the v4 test first')
    data = json.loads(REPORT.read_text(encoding='utf-8'))
    if data.get('period') != '2026-12' or data.get('status') != 'test_only_not_verified':
        raise ValueError('Unexpected report version or period')
    rows = data.get('results')
    if not isinstance(rows, list) or not rows:
        raise ValueError('No flight arrival results')

    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//hfedder//Piemonte Rail Candidate//DA',
             'CALSCALE:GREGORIAN', 'METHOD:PUBLISH', 'X-WR-CALNAME:Piemonte Tog - TEST',
             'X-WR-CALDESC:Foreloebige togforbindelser efter flyankomster foer kl 18']
    seen = set()
    created = 0
    for row in rows:
        arrival = datetime.fromisoformat(row['flight_arrival_local']).astimezone(ROME)
        if arrival.hour > 18 or (arrival.hour == 18 and arrival.minute > 0):
            continue
        destination = row.get('recommended_destination')
        if destination not in TARGETS:
            continue
        choices = row.get('options', {}).get(destination, [])
        if not choices:
            continue
        choice = choices[0]
        legs = choice.get('legs') or []
        if not legs:
            continue
        dep = local_dt(arrival.date(), choice['departure'])
        end = local_dt(arrival.date(), choice['arrival'])
        if end <= dep:
            end += timedelta(days=1)
        if dep < arrival or (end - dep) > timedelta(hours=12):
            continue
        key = (row['flight'], arrival.isoformat(), destination, dep.isoformat())
        if key in seen:
            continue
        seen.add(key)
        uid = f"rail-{row['flight']}-{arrival.strftime('%Y%m%dT%H%M')}-{destination.replace(' ', '')}@hfedder-piemonte"
        dep_utc = dep.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        end_utc = end.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        steps = [f"{leg['from']} {leg['departure']} -> {leg['to']} {leg['arrival']} (tog {leg['train']})" for leg in legs]
        desc = (f"TEST, IKKE VERIFICERET. Efter {row['flight']} til {row['airport']} kl. {arrival:%H:%M}. "
                'Lufthavnstransport er et estimat, ikke en kontrolleret shuttleafgang. '
                + ' | '.join(steps) + ' | Kontrollér altid transport og tog hos operatøren.')
        lines.extend(['BEGIN:VEVENT', f'UID:{uid}', f'DTSTAMP:{stamp}',
                      f'DTSTART:{dep_utc}', f'DTEND:{end_utc}',
                      f'SUMMARY:{escape("TOG (TEST) " + legs[0]["from"] + " -> " + destination)}',
                      f'LOCATION:{escape(legs[0]["from"])}',
                      f'DESCRIPTION:{escape(desc)}', 'TRANSP:TRANSPARENT', 'STATUS:TENTATIVE',
                      'END:VEVENT'])
        created += 1
    if not created:
        raise ValueError('No suitable rail connections; refusing empty calendar')
    lines.append('END:VCALENDAR')
    OUTPUT.write_bytes(('\r\n'.join(fold(line) for line in lines) + '\r\n').encode('utf-8'))
    print(f'Created {created} independent TEST rail events in {OUTPUT}')
    print('Existing Ryanair ICS remains untouched. Do not publish until routes are checked.')


if __name__ == '__main__':
    main()
