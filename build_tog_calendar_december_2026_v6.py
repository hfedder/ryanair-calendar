#!/usr/bin/env python3
"""Separate TEST train calendar, December 2026. Does not touch flight calendar.

Requires tog_december_2026_test.json produced by the v4 train test.
Nizza first if every transfer is 10..45 minutes; otherwise choose earliest
Asti/Alessandria option with transfers of 10..60 minutes.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

INPUT = Path('tog_december_2026_test.json')
OUTPUT = Path('Piemonte_tog_december_2026_candidate.ics')
REPORT = Path('tog_december_2026_v6_validation.json')
ROME = ZoneInfo('Europe/Rome')
TARGETS = ('NIZZA MONFERRATO', 'ASTI', 'ALESSANDRIA')


def minute(value):
    match = re.fullmatch(r'(\d{1,2}):(\d{2})', str(value))
    if not match or int(match[1]) > 47 or int(match[2]) > 59:
        raise ValueError('Invalid time: ' + str(value))
    return int(match[1]) * 60 + int(match[2])


def at(day, m):
    return datetime(day.year, day.month, day.day, tzinfo=ROME) + timedelta(minutes=m)


def esc(text):
    return str(text).replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\n', '\\n')


def fold(text):
    chunks, current = [], ''
    for ch in text:
        limit = 75 if not chunks else 74
        if len((current + ch).encode('utf-8')) > limit:
            chunks.append(current)
            current = ch
        else:
            current += ch
    return '\r\n '.join(chunks + [current])


def validate(choice, arrival, ready, destination):
    legs = choice.get('legs', [])
    if not 1 <= len(legs) <= 5:
        return None, 'invalid number of train legs'
    parsed = []
    for leg in legs:
        try:
            d, a = minute(leg['departure']), minute(leg['arrival'])
            if a <= d or a - d > 480:
                return None, 'invalid leg duration'
            parsed.append((d, a, leg))
        except (KeyError, TypeError, ValueError) as exc:
            return None, str(exc)
    dep = at(arrival.date(), parsed[0][0])
    if dep < ready:
        return None, 'train departs before estimated station arrival'
    if dep - ready > timedelta(hours=6):
        return None, 'first train wait exceeds 6 hours'
    for (_, prev_arr, prev), (next_dep, _, nxt) in zip(parsed, parsed[1:]):
        if prev['to'].strip().upper() != nxt['from'].strip().upper():
            return None, 'station transfer not modeled'
        wait = next_dep - prev_arr
        max_wait = 45 if destination == 'NIZZA MONFERRATO' else 60
        if not 10 <= wait <= max_wait:
            return None, f'transfer {wait} minutes outside 10-{max_wait}'
    if parsed[-1][1] - parsed[0][0] > 720:
        return None, 'journey over 12 hours'
    return parsed, None


def main():
    data = json.loads(INPUT.read_text(encoding='utf-8'))
    if data.get('period') != '2026-12' or data.get('status') != 'test_only_not_verified':
        raise ValueError('Unexpected input report')
    rows = data.get('results')
    if not isinstance(rows, list) or not rows:
        raise ValueError('No flight arrival results')
    stats = {'input_flights': len(rows), 'excluded_outside_06_18': 0,
             'no_valid_connection': 0, 'created_events': 0,
             'destinations': {}, 'rejections': [],
             'note': 'TEST ONLY: airport shuttles are estimated, not timetable-verified.'}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//hfedder//Piemonte Rail Candidate v6//DA',
             'CALSCALE:GREGORIAN', 'METHOD:PUBLISH', 'X-WR-CALNAME:Piemonte Tog - TEST v6']
    seen = set()
    for row in rows:
        arrival = datetime.fromisoformat(row['flight_arrival_local']).astimezone(ROME)
        if arrival.strftime('%Y-%m') != '2026-12' or not 360 <= arrival.hour * 60 + arrival.minute <= 1080:
            stats['excluded_outside_06_18'] += 1
            continue
        ready = datetime.fromisoformat(row['assumed_ready_at_station']).astimezone(ROME)
        choices = {}
        for destination in TARGETS:
            valid = []
            for choice in row.get('options', {}).get(destination, []):
                legs, error = validate(choice, arrival, ready, destination)
                if error:
                    stats['rejections'].append({'flight': row['flight'], 'date': arrival.date().isoformat(),
                                                'destination': destination, 'reason': error})
                else:
                    valid.append(legs)
            if valid:
                choices[destination] = min(valid, key=lambda x: (x[-1][1], len(x)))
        if 'NIZZA MONFERRATO' in choices:
            dest = 'NIZZA MONFERRATO'
        elif choices:
            dest = min(choices, key=lambda d: (choices[d][-1][1], len(choices[d])))
        else:
            stats['no_valid_connection'] += 1
            continue
        legs = choices[dest]
        dep, end = at(arrival.date(), legs[0][0]), at(arrival.date(), legs[-1][1])
        uid = f"rail-{row['flight']}-{arrival.strftime('%Y%m%dT%H%M')}-{dest.replace(' ', '')}@hfedder-piemonte"
        if uid in seen:
            continue
        seen.add(uid)
        steps = ' | '.join(f"{leg['from']} {leg['departure']} -> {leg['to']} {leg['arrival']} (tog {leg.get('train', '?')})" for _, _, leg in legs)
        desc = (f"TEST, IKKE VERIFICERET. Efter {row['flight']} til {row['airport']} kl. {arrival:%H:%M}. "
                f"Klar paa stationen ca. {ready:%H:%M} (estimat). {steps}. "
                'Lufthavnstransport og tog skal kontrolleres foer rejse.')
        lines += ['BEGIN:VEVENT', 'UID:' + uid, 'DTSTAMP:' + stamp,
                  'DTSTART:' + dep.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                  'DTEND:' + end.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                  'SUMMARY:' + esc(f'TOG (TEST) {legs[0][2]["from"]} -> {dest}'),
                  'LOCATION:' + esc(legs[0][2]['from']), 'DESCRIPTION:' + esc(desc),
                  'STATUS:TENTATIVE', 'TRANSP:TRANSPARENT', 'END:VEVENT']
        stats['created_events'] += 1
        stats['destinations'][dest] = stats['destinations'].get(dest, 0) + 1
    REPORT.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if not stats['created_events']:
        OUTPUT.unlink(missing_ok=True)
        raise ValueError('No valid rail events; no calendar published')
    lines.append('END:VCALENDAR')
    OUTPUT.write_bytes(('\r\n'.join(fold(x) for x in lines) + '\r\n').encode('utf-8'))
    print(json.dumps({k: v for k, v in stats.items() if k != 'rejections'}, ensure_ascii=False, indent=2))
    print('Created', OUTPUT, 'and', REPORT, '; Ryanair calendar unchanged.')


if __name__ == '__main__':
    main()
