#!/usr/bin/env python3
"""Build independent, unpublished December 2026 rail candidate from v4 JSON.

Safety rules: flight arrival 06:00..18:00 local, train transfer 10..60 min,
no train before estimated station-ready time. Nizza is preferred if its
arrival is within 45 minutes of the earliest Asti/Alessandria alternative.
This is a timetable TEST, not verified airport ground transport.
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE = Path('tog_december_2026_test.json')
OUTPUT = Path('Piemonte_tog_december_2026_candidate.ics')
REPORT = Path('tog_december_2026_v5_validation.json')
ROME = ZoneInfo('Europe/Rome')
TARGETS = ('NIZZA MONFERRATO', 'ASTI', 'ALESSANDRIA')
MIN_TRANSFER, MAX_TRANSFER = 10, 60
NIZZA_TOLERANCE = 45


def mins(s):
    m = re.fullmatch(r'(\d{1,2}):(\d{2})', str(s))
    if not m or int(m[1]) > 47 or int(m[2]) > 59:
        raise ValueError('Bad GTFS time: ' + str(s))
    return int(m[1]) * 60 + int(m[2])


def local_time(day, minute):
    return datetime(day.year, day.month, day.day, tzinfo=ROME) + timedelta(minutes=minute)


def escape(s):
    return str(s).replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\n', '\\n')


def fold(line):
    parts, chunk = [], ''
    for char in line:
        cap = 75 if not parts else 74
        if len((chunk + char).encode('utf-8')) > cap:
            parts.append(chunk)
            chunk = char
        else:
            chunk += char
    return '\r\n '.join(parts + [chunk])


def validate_option(option, ready, flight_arrival):
    legs = option.get('legs', [])
    if not legs or len(legs) > 5:
        return None, 'no legs or too many legs'
    parsed = []
    for leg in legs:
        try:
            dep, arr = mins(leg['departure']), mins(leg['arrival'])
            if arr <= dep or arr - dep > 8 * 60:
                return None, 'invalid leg duration'
            parsed.append((dep, arr, leg))
        except (KeyError, ValueError, TypeError) as exc:
            return None, str(exc)
    first = parsed[0][0]
    if local_time(flight_arrival.date(), first) < ready:
        return None, 'train departs before station ready'
    if first - (ready - local_time(flight_arrival.date(), 0)).total_seconds() / 60 > 360:
        return None, 'first train wait exceeds 6 hours'
    for (_, prev_arr, prev), (dep, _, nxt) in zip(parsed, parsed[1:]):
        if prev['to'].strip().upper() != nxt['from'].strip().upper():
            return None, 'unmodelled station change'
        wait = dep - prev_arr
        if not MIN_TRANSFER <= wait <= MAX_TRANSFER:
            return None, f'transfer wait {wait} min outside {MIN_TRANSFER}-{MAX_TRANSFER}'
    if parsed[-1][1] - first > 12 * 60:
        return None, 'journey longer than 12 hours'
    return parsed, None


def main():
    data = json.loads(SOURCE.read_text(encoding='utf-8'))
    if data.get('period') != '2026-12' or data.get('status') != 'test_only_not_verified':
        raise ValueError('Wrong input report')
    rows = data.get('results', [])
    if not isinstance(rows, list) or not rows:
        raise ValueError('Missing flight rows')
    report = {'input_flights': len(rows), 'excluded_outside_06_18': 0,
              'no_valid_connection': 0, 'created_events': 0,
              'destinations': {}, 'rejections': [],
              'warning': 'Airport-to-station transfers are estimates; GTFS and train availability are not independently verified.'}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//hfedder//Piemonte Rail Candidate v5//DA',
             'CALSCALE:GREGORIAN', 'METHOD:PUBLISH', 'X-WR-CALNAME:Piemonte Tog - TEST v5']
    seen = set()
    for row in rows:
        arrival = datetime.fromisoformat(row['flight_arrival_local']).astimezone(ROME)
        if arrival.strftime('%Y-%m') != '2026-12' or not 6 * 60 <= arrival.hour * 60 + arrival.minute <= 18 * 60:
            report['excluded_outside_06_18'] += 1
            continue
        ready = datetime.fromisoformat(row['assumed_ready_at_station']).astimezone(ROME)
        valid = []
        for destination in TARGETS:
            for option in row.get('options', {}).get(destination, []):
                legs, error = validate_option(option, ready, arrival)
                if error:
                    report['rejections'].append({'flight': row['flight'], 'arrival': arrival.isoformat(),
                                                 'destination': destination, 'reason': error})
                    continue
                valid.append((destination, legs))
        if not valid:
            report['no_valid_connection'] += 1
            continue
        best_alt = min((x for x in valid if x[0] != TARGETS[0]),
                       key=lambda x: (x[1][-1][1], len(x[1])), default=None)
        best_nizza = min((x for x in valid if x[0] == TARGETS[0]),
                         key=lambda x: (x[1][-1][1], len(x[1])), default=None)
        if best_nizza and (not best_alt or best_nizza[1][-1][1] <= best_alt[1][-1][1] + NIZZA_TOLERANCE):
            chosen = best_nizza
        else:
            chosen = best_alt or best_nizza
        destination, legs = chosen
        departure = local_time(arrival.date(), legs[0][0])
        end = local_time(arrival.date(), legs[-1][1])
        uid = f"rail-{row['flight']}-{arrival.strftime('%Y%m%dT%H%M')}-{destination.replace(' ', '')}@hfedder-piemonte"
        if uid in seen:
            continue
        seen.add(uid)
        details = ' | '.join(f"{l['from']} {l['departure']} -> {l['to']} {l['arrival']} (tog {l.get('train', '?')})" for _, _, l in legs)
        desc = (f"TEST, IKKE VERIFICERET. Fly {row['flight']} ankommer {row['airport']} kl. {arrival:%H:%M}. "
                f"Klar ved station (estimat): {ready:%H:%M}. {details}. "
                'Lufthavnstransport ikke verificeret. Kontrollér tider hos operatøren.')
        lines += ['BEGIN:VEVENT', f'UID:{uid}', f'DTSTAMP:{stamp}',
                  'DTSTART:' + departure.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                  'DTEND:' + end.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                  'SUMMARY:' + escape(f'TOG (TEST) {legs[0][2]["from"]} -> {destination}'),
                  'LOCATION:' + escape(legs[0][2]['from']), 'DESCRIPTION:' + escape(desc),
                  'TRANSP:TRANSPARENT', 'STATUS:TENTATIVE', 'END:VEVENT']
        report['created_events'] += 1
        report['destinations'][destination] = report['destinations'].get(destination, 0) + 1
    lines.append('END:VCALENDAR')
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    if not report['created_events']:
        OUTPUT.unlink(missing_ok=True)
        raise ValueError('No valid train events; calendar not generated')
    OUTPUT.write_bytes(('\r\n'.join(fold(line) for line in lines) + '\r\n').encode('utf-8'))
    print(json.dumps({k: v for k, v in report.items() if k != 'rejections'}, ensure_ascii=False, indent=2))
    print('Created', OUTPUT, 'and', REPORT, '- Ryanair calendar unchanged')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
