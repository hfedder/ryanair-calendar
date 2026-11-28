#!/usr/bin/env python3
"""Rolling 12-month TEST rail calendar. Requires test_tog_december_2026_v2.py.
Reads Ryanair ICS, GTFS timetable. Does not alter the Ryanair calendar.
Published output is blocked unless every month has verified GTFS service coverage.
Airport transfer durations are estimates, NOT real shuttle itineraries.
"""
import argparse
import calendar
import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from zoneinfo import ZoneInfo
import test_tog_december_2026_v2 as rail

ROME = ZoneInfo('Europe/Rome')
TARGETS = ('NIZZA MONFERRATO', 'ASTI', 'ALESSANDRIA')
rail.STATION_START['BGY'] = ('MILANO CENTRALE',)
rail.AIRPORT_BUFFERS['BGY'] = 105
rail.AIRPORT_BUFFERS['TRN'] = 90
rail.MAX_WAIT = 45
rail.MAX_LEGS = 5


def month_shift(day, n):
    y = day.year + (day.month - 1 + n) // 12
    m = (day.month - 1 + n) % 12 + 1
    return date(y, m, 1)


def ical_escape(s):
    return str(s).replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\n', '\\n')


def fold(line):
    chunks, current = [], ''
    for ch in line:
        if len((current + ch).encode('utf-8')) > (75 if not chunks else 74):
            chunks.append(current)
            current = ch
        else:
            current += ch
    return '\r\n '.join(chunks + [current])


def stamp(dt):
    return dt.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def valid_path(path, ready):
    if not path or len(path) > 5:
        return False
    first = path[0]['departure']
    if first < ready or first - ready > 360:
        return False
    for a, b in zip(path, path[1:]):
        if a['to_stop_id'] != b['from_stop_id'] or not 10 <= b['departure'] - a['arrival'] <= 45:
            return False
    return path[-1]['arrival'] > first and path[-1]['arrival'] - first <= 720


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default=datetime.now(ROME).strftime('%Y-%m'))
    ap.add_argument('--months', type=int, default=12)
    ap.add_argument('--publish', action='store_true')
    args = ap.parse_args()
    if args.months != 12:
        ap.error('Only a 12-month window is supported')
    first = date.fromisoformat(args.start + '-01')
    months = [month_shift(first, i) for i in range(12)]
    last = month_shift(first, 12)
    # Reuse existing ICS parser by setting its global month for each month.
    arrivals = []
    for m in months:
        rail.MONTH = m.strftime('%Y-%m')
        arrivals.extend(rail.ics_events(rail.ICS))
    arrivals = [e for e in arrivals if 360 <= e['arrival'].hour * 60 + e['arrival'].minute <= 1080]
    trips, by_trip, names, cal, exceptions, stops = rail.load_gtfs()
    starts = {airport: rail.resolve(stations, names) for airport, stations in rail.STATION_START.items()}
    targets = {dest: rail.resolve((dest,), names) for dest in TARGETS}
    if any(not ids for ids in list(starts.values()) + list(targets.values())):
        raise ValueError('One or more required stations are missing in GTFS')
    # Require explicit calendar coverage for every date in the window, including days with no flights.
    # This prevents an incomplete timetable from replacing a useful subscribed calendar.
    coverage = {}
    for m in months:
        last_day = calendar.monthrange(m.year, m.month)[1]
        days = [m + timedelta(days=i) for i in range(last_day)]
        coverage[m.strftime('%Y-%m')] = all(bool(rail.service_active(d, cal, exceptions)) for d in days)
    edges_cache = {}
    events = []
    counts = Counter()
    no_connection = []
    for e in arrivals:
        ready_dt = e['arrival'] + timedelta(minutes=rail.AIRPORT_BUFFERS[e['airport']])
        day = ready_dt.date()
        if day not in edges_cache:
            edges_cache[day] = rail.departures_for_day(day, trips, by_trip, cal, exceptions)
        edges = edges_cache[day]
        ready = ready_dt.hour * 60 + ready_dt.minute
        best = {}
        for dest in TARGETS:
            paths = rail.options(edges, starts[e['airport']], targets[dest], ready, limit=4)
            eligible = [p for p in paths if valid_path(p, ready)]
            if eligible:
                best[dest] = min(eligible, key=lambda p: (p[-1]['arrival'], len(p)))
        if not best:
            no_connection.append({'flight': e['flight'], 'arrival': e['arrival'].isoformat()})
            continue
        fastest = min(best, key=lambda d: (best[d][-1]['arrival'], len(best[d])))
        chosen = ('NIZZA MONFERRATO' if 'NIZZA MONFERRATO' in best and
                  best['NIZZA MONFERRATO'][-1]['arrival'] <= best[fastest][-1]['arrival'] + 30 else fastest)
        others = [d for d in best if d != chosen]
        alternative = min(others, key=lambda d: (best[d][-1]['arrival'], len(best[d]))) if others else None
        for dest, label in ((chosen, 'ANBEFALET'), (alternative, 'ALTERNATIV')):
            if dest is None:
                continue
            p = best[dest]
            dep = datetime.combine(day, datetime.min.time(), ROME) + timedelta(minutes=p[0]['departure'])
            end = datetime.combine(day, datetime.min.time(), ROME) + timedelta(minutes=p[-1]['arrival'])
            details = ' | '.join(f"{stops[x['from_stop_id']]['stop_name']} {rail.clock(x['departure'])} -> {stops[x['to_stop_id']]['stop_name']} {rail.clock(x['arrival'])} (tog {x['train']})" for x in p)
            uid = f"rail-rolling-{e['flight']}-{e['arrival']:%Y%m%dT%H%M}-{dest.replace(' ', '')}@hfedder-piemonte"
            fields = ['BEGIN:VEVENT', 'UID:' + uid, 'DTSTAMP:' + stamp(datetime.now(timezone.utc)),
                      'DTSTART:' + stamp(dep), 'DTEND:' + stamp(end),
                      'SUMMARY:' + ical_escape(f'TOG {label} (TEST) {stops[p[0]["from_stop_id"]]["stop_name"]} -> {dest}'),
                      'LOCATION:' + ical_escape(stops[p[0]['from_stop_id']]['stop_name']),
                      'DESCRIPTION:' + ical_escape(f"TEST, IKKE VERIFICERET. Efter {e['flight']} til {e['airport']} kl. {e['arrival']:%H:%M}. Lufthavnstransport estimeret til {rail.AIRPORT_BUFFERS[e['airport']]} minutter. {details}. Kontroller shuttle og tog hos operatoeren."),
                      'STATUS:TENTATIVE', 'TRANSP:TRANSPARENT', 'END:VEVENT']
            events.extend(fields)
            counts[label] += 1
            counts[f'{label}:{dest}'] += 1
        print(f"{e['arrival'].date()} {e['flight']} {e['airport']} -> {chosen}", flush=True)
    report = {'window_start': first.isoformat(), 'window_end_exclusive': last.isoformat(),
              'months': [m.strftime('%Y-%m') for m in months], 'gtfs_calendar_coverage': coverage,
              'qualifying_flights': len(arrivals), 'recommended': counts['ANBEFALET'],
              'alternatives': counts['ALTERNATIV'], 'destination_counts': dict(counts),
              'no_connection': no_connection, 'published': False,
              'warning': 'TEST ONLY. Airport shuttles are estimates; GTFS services and train times are not independently verified.'}
    Path('piemonte_rolling_validation.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    if not events:
        raise RuntimeError('No events generated; published calendar unchanged')
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//hfedder//Piemonte Rolling Rail Test//DA',
             'CALSCALE:GREGORIAN', 'METHOD:PUBLISH', 'X-WR-CALNAME:Piemonte Tog (TEST)'] + events + ['END:VCALENDAR']
    candidate = Path('Piemonte_tog_rolling_candidate.ics')
    candidate.write_bytes(('\r\n'.join(fold(x) for x in lines) + '\r\n').encode('utf-8'))
    # Do not publish partial months, nor overwrite when no qualifying flights for a month.
    monthly_flights = Counter(e['arrival'].strftime('%Y-%m') for e in arrivals)
    safe = all(coverage.values()) and all(monthly_flights[m.strftime('%Y-%m')] for m in months) and not no_connection
    if args.publish:
        if not safe:
            raise RuntimeError('Publication blocked: incomplete monthly coverage, missing flight months or missing connections. Candidate retained.')
        Path('Piemonte_tog_rolling.ics').write_bytes(candidate.read_bytes())
        report['published'] = True
        Path('piemonte_rolling_validation.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps({'flights': len(arrivals), 'events': len(events)//11, 'coverage': coverage, 'published': report['published']}, indent=2), flush=True)

if __name__ == '__main__':
    main()
