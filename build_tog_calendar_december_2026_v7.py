#!/usr/bin/env python3
"""Separate TEST rail calendar: recommended + alternative per qualifying flight.
Input: tog_december_2026_test.json from test_tog_december_2026_v4.py.
Does not change Ryanair calendar. Does not verify airport shuttle schedules.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE = Path('tog_december_2026_test.json')
OUTPUT = Path('Piemonte_tog_december_2026_candidate.ics')
REPORT = Path('tog_december_2026_v7_validation.json')
ROME = ZoneInfo('Europe/Rome')
DESTINATIONS = ('NIZZA MONFERRATO', 'ASTI', 'ALESSANDRIA')


def minutes(value):
    match = re.fullmatch(r'(\d{1,2}):(\d{2})', str(value))
    if not match or int(match[1]) > 47 or int(match[2]) > 59:
        raise ValueError('Bad time: ' + str(value))
    return int(match[1]) * 60 + int(match[2])


def when(day, mins):
    return datetime(day.year, day.month, day.day, tzinfo=ROME) + timedelta(minutes=mins)


def escape(value):
    return str(value).replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\n', '\\n')


def fold(line):
    parts, current = [], ''
    for char in line:
        if len((current + char).encode('utf-8')) > (75 if not parts else 74):
            parts.append(current)
            current = char
        else:
            current += char
    return '\r\n '.join(parts + [current])


def validate(option, arrival, ready):
    legs = option.get('legs', [])
    if not 1 <= len(legs) <= 5:
        return None, 'invalid leg count'
    parsed = []
    try:
        for leg in legs:
            dep, end = minutes(leg['departure']), minutes(leg['arrival'])
            if end <= dep or end - dep > 480:
                return None, 'invalid leg duration'
            parsed.append((dep, end, leg))
        first = when(arrival.date(), parsed[0][0])
        if first < ready or first - ready > timedelta(hours=6):
            return None, 'first train not reachable or too late'
        for (_, end, previous), (dep, _, following) in zip(parsed, parsed[1:]):
            if previous['to'].strip().upper() != following['from'].strip().upper():
                return None, 'station change not modelled'
            if not 10 <= dep - end <= 45:
                return None, f'transfer {dep - end} minutes outside 10..45'
        if parsed[-1][1] - parsed[0][0] > 720:
            return None, 'journey too long'
    except (KeyError, ValueError, TypeError) as exc:
        return None, str(exc)
    return parsed, None


def add_event(lines, row, arrival, ready, destination, legs, label, stamp):
    dep = when(arrival.date(), legs[0][0])
    end = when(arrival.date(), legs[-1][1])
    uid = f"rail-v7-{row['flight']}-{arrival.strftime('%Y%m%dT%H%M')}-{destination.replace(' ', '')}@hfedder-piemonte"
    detail = ' | '.join(f"{leg['from']} {leg['departure']} -> {leg['to']} {leg['arrival']} (tog {leg.get('train', '?')})" for _, _, leg in legs)
    description = (f"TEST, IKKE VERIFICERET. {label} efter {row['flight']} til {row['airport']} kl. {arrival:%H:%M}. "
                   f"Beregnet klar paa stationen {ready:%H:%M}. {detail}. "
                   'Shuttle og togtider skal kontrolleres inden rejse.')
    lines.extend(['BEGIN:VEVENT', 'UID:' + uid, 'DTSTAMP:' + stamp,
                  'DTSTART:' + dep.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                  'DTEND:' + end.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                  'SUMMARY:' + escape(f'TOG {label} (TEST) {legs[0][2]["from"]} -> {destination}'),
                  'LOCATION:' + escape(legs[0][2]['from']), 'DESCRIPTION:' + escape(description),
                  'STATUS:TENTATIVE', 'TRANSP:TRANSPARENT', 'END:VEVENT'])


def main():
    data = json.loads(SOURCE.read_text(encoding='utf-8'))
    if data.get('period') != '2026-12' or data.get('status') != 'test_only_not_verified':
        raise ValueError('Wrong input report')
    rows = data.get('results', [])
    if not rows:
        raise ValueError('No flight data')
    report = {'input_flights': len(rows), 'excluded_outside_06_18': 0, 'without_valid_option': 0,
              'recommended_events': 0, 'alternative_events': 0, 'destinations_recommended': {},
              'rejections': [], 'note': 'Airport transfers are estimates; train times are not independently verified.'}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//hfedder//Piemonte Rail Candidate v7//DA',
             'CALSCALE:GREGORIAN', 'METHOD:PUBLISH', 'X-WR-CALNAME:Piemonte Tog - TEST v7']
    seen = set()
    for row in rows:
        arrival = datetime.fromisoformat(row['flight_arrival_local']).astimezone(ROME)
        if arrival.strftime('%Y-%m') != '2026-12' or not 360 <= arrival.hour * 60 + arrival.minute <= 1080:
            report['excluded_outside_06_18'] += 1
            continue
        ready = datetime.fromisoformat(row['assumed_ready_at_station']).astimezone(ROME)
        candidates = []
        for destination in DESTINATIONS:
            for option in row.get('options', {}).get(destination, []):
                legs, error = validate(option, arrival, ready)
                if error:
                    report['rejections'].append({'flight': row['flight'], 'date': arrival.date().isoformat(),
                                                 'destination': destination, 'reason': error})
                else:
                    candidates.append((destination, legs))
        if not candidates:
            report['without_valid_option'] += 1
            continue
        # Choose the earliest arrival across all three pickup stations.
        best_by_dest = {}
        for dest, legs in candidates:
            if dest not in best_by_dest or (legs[-1][1], len(legs)) < (best_by_dest[dest][-1][1], len(best_by_dest[dest])):
                best_by_dest[dest] = legs
        fastest_dest = min(best_by_dest, key=lambda d: (best_by_dest[d][-1][1], len(best_by_dest[d])))
        fastest_end = best_by_dest[fastest_dest][-1][1]
        # Nizza gets preference only if it is within 30 minutes of fastest pickup.
        recommended = 'NIZZA MONFERRATO' if ('NIZZA MONFERRATO' in best_by_dest and
                                            best_by_dest['NIZZA MONFERRATO'][-1][1] <= fastest_end + 30) else fastest_dest
        others = [d for d in best_by_dest if d != recommended]
        alternative = min(others, key=lambda d: (best_by_dest[d][-1][1], len(best_by_dest[d]))) if others else None
        for dest, label in ((recommended, 'ANBEFALET'), (alternative, 'ALTERNATIV')):
            if dest is None:
                continue
            uidkey = (row['flight'], arrival.isoformat(), dest)
            if uidkey in seen:
                continue
            seen.add(uidkey)
            add_event(lines, row, arrival, ready, dest, best_by_dest[dest], label, stamp)
            if label == 'ANBEFALET':
                report['recommended_events'] += 1
                report['destinations_recommended'][dest] = report['destinations_recommended'].get(dest, 0) + 1
            else:
                report['alternative_events'] += 1
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if not report['recommended_events']:
        OUTPUT.unlink(missing_ok=True)
        raise ValueError('No recommended journeys')
    lines.append('END:VCALENDAR')
    OUTPUT.write_bytes(('\r\n'.join(fold(line) for line in lines) + '\r\n').encode('utf-8'))
    print(json.dumps({k: v for k, v in report.items() if k != 'rejections'}, indent=2, ensure_ascii=False))
    print('Candidate:', OUTPUT, '; Ryanair calendar unchanged')


if __name__ == '__main__':
    main()
