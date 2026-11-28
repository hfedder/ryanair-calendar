#!/usr/bin/env python3
"""Read-only December 2026 test: Ryanair arrivals -> rail options in Piemonte.

Inputs: local Ryanair ICS + downloadable Trenitalia GTFS. No calendar changes.
Airport -> station is an ASSUMED time buffer, not a verified shuttle itinerary.
"""
import csv
import io
import json
import os
import re
import sys
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROME = ZoneInfo('Europe/Rome')
ICS = Path(os.getenv('RYANAIR_ICS', 'Ryanair_TRN_BGY_CPH_verificerede_afgange.ics'))
GTFS_URL = os.getenv('GTFS_URL', 'https://raw.githubusercontent.com/deryclem/trenitalia-gtfs/main/gtfs-trenitalia.zip')
GTFS_FILE = os.getenv('GTFS_FILE', '')  # Optional local ZIP for offline tests
OUT = Path('tog_december_2026_test.json')
MONTH = '2026-12'
MAX_WAIT = 45  # minutes for transfers; first departure may be later
MAX_LEGS = 3
# These are assumptions, not validated airport transfer schedules.
AIRPORT_BUFFERS = {'TRN': 90, 'BGY': 90}
STATION_START = {'TRN': ('TORINO PORTA SUSA', 'TORINO PORTA NUOVA'), 'BGY': ('BERGAMO',)}
TARGETS = ('NIZZA MONFERRATO', 'ASTI', 'ALESSANDRIA')


def ics_events(path):
    if not path.exists():
        raise FileNotFoundError(f'Missing Ryanair calendar: {path}')
    raw = path.read_text(encoding='utf-8-sig')
    unfolded = re.sub(r'\r?\n[ \t]', '', raw)
    events = []
    for block in unfolded.split('BEGIN:VEVENT')[1:]:
        text = block.split('END:VEVENT')[0]
        fields = {}
        for line in text.splitlines():
            if ':' in line:
                key, val = line.split(':', 1)
                fields[key] = val
        summary = fields.get('SUMMARY', '')
        match = re.search(r'\b(FR\d+)\s+CPH\s*(?:->|→)\s*(BGY|TRN)\b', summary)
        if not match:
            continue
        start = next(((k, v) for k, v in fields.items() if k.startswith('DTSTART')), None)
        if not start:
            continue
        key, value = start
        if value.endswith('Z'):
            dt = datetime.strptime(value, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).astimezone(ROME)
        else:
            tzid = re.search(r'TZID=([^;:]+)', key)
            tz = ZoneInfo(tzid.group(1)) if tzid else ROME
            dt = datetime.strptime(value, '%Y%m%dT%H%M%S').replace(tzinfo=tz).astimezone(ROME)
        end = next(((k, v) for k, v in fields.items() if k.startswith('DTEND')), None)
        if not end:
            continue
        ek, ev = end
        if ev.endswith('Z'):
            arr = datetime.strptime(ev, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).astimezone(ROME)
        else:
            tzid = re.search(r'TZID=([^;:]+)', ek)
            arr = datetime.strptime(ev, '%Y%m%dT%H%M%S').replace(tzinfo=ZoneInfo(tzid.group(1)) if tzid else ROME).astimezone(ROME)
        if arr.strftime('%Y-%m') == MONTH:
            events.append({'flight': match.group(1), 'airport': match.group(2), 'arrival': arr})
    return sorted({(e['flight'], e['airport'], e['arrival']): e for e in events}.values(), key=lambda e: e['arrival'])


def table(z, name):
    with z.open(name) as f:
        return list(csv.DictReader(io.TextIOWrapper(f, encoding='utf-8-sig', newline='')))


def time_min(value):
    h, m, s = (int(x) for x in value.split(':'))
    return h * 60 + m + (1 if s >= 30 else 0)


def service_active(day, cal, exceptions):
    ds = day.strftime('%Y%m%d')
    active = set()
    weekday = day.strftime('%A').lower()
    for sid, c in cal.items():
        if c['start_date'] <= ds <= c['end_date'] and c.get(weekday) == '1':
            active.add(sid)
    for sid, typ in exceptions.get(ds, []):
        if typ == '1':
            active.add(sid)
        elif typ == '2':
            active.discard(sid)
    return active


def norm(s):
    return re.sub(r'\s+', ' ', re.sub(r'[^A-Z0-9 ]+', ' ', s.upper())).strip()


def load_gtfs():
    if GTFS_FILE:
        raw = Path(GTFS_FILE).read_bytes()
    else:
        req = urllib.request.Request(GTFS_URL, headers={'User-Agent': 'Piemonte-rail-calendar-test/1.0'})
        with urllib.request.urlopen(req, timeout=90) as r:
            raw = r.read()
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        required = {'stops.txt', 'trips.txt', 'stop_times.txt'}
        if not required.issubset(z.namelist()):
            raise ValueError('Missing GTFS files: ' + str(required - set(z.namelist())))
        stops = table(z, 'stops.txt')
        trips = table(z, 'trips.txt')
        times = table(z, 'stop_times.txt')
        cal = {r['service_id']: r for r in table(z, 'calendar.txt')} if 'calendar.txt' in z.namelist() else {}
        exceptions = defaultdict(list)
        if 'calendar_dates.txt' in z.namelist():
            for r in table(z, 'calendar_dates.txt'):
                exceptions[r['date']].append((r['service_id'], r['exception_type']))
        if not cal and not exceptions:
            raise ValueError('GTFS has no service calendar')
    by_id = {r['stop_id']: r for r in stops}
    ids_by_name = defaultdict(set)
    for r in stops:
        name = norm(r.get('stop_name', ''))
        ids_by_name[name].add(r['stop_id'])
        parent = r.get('parent_station')
        if parent and parent in by_id:
            ids_by_name[norm(by_id[parent].get('stop_name', ''))].add(r['stop_id'])
    # Build grouped trip sequences once.
    by_trip = defaultdict(list)
    for r in times:
        if r.get('arrival_time') and r.get('departure_time'):
            by_trip[r['trip_id']].append(r)
    for seq in by_trip.values():
        seq.sort(key=lambda r: int(r['stop_sequence']))
    return trips, by_trip, ids_by_name, cal, exceptions, by_id


def resolve(names, names_index):
    ids = set()
    for target in names:
        n = norm(target)
        for candidate, found in names_index.items():
            if candidate == n or candidate.startswith(n + ' '):
                ids.update(found)
    return ids


def departures_for_day(day, trips, by_trip, cal, exceptions):
    active = service_active(day, cal, exceptions)
    edges = defaultdict(list)
    for t in trips:
        if t['service_id'] not in active:
            continue
        seq = by_trip.get(t['trip_id'], [])
        for i, a in enumerate(seq[:-1]):
            if a.get('pickup_type', '0') == '1':
                continue
            try:
                dep = time_min(a['departure_time'])
            except (ValueError, KeyError):
                continue
            # Allow staying on the same train through intermediate stations.
            for b in seq[i + 1:]:
                if b.get('drop_off_type', '0') == '1':
                    continue
                try:
                    arr = time_min(b['arrival_time'])
                except (ValueError, KeyError):
                    continue
                if dep < arr:
                    edges[a['stop_id']].append((dep, arr, b['stop_id'], t['trip_id'], t.get('trip_short_name') or t.get('trip_headsign') or 'train'))
    for v in edges.values():
        v.sort()
    return edges


def options(edges, start_ids, target_ids, ready, limit=2):
    """Time-dependent search, <=3 trains, max 45 min between different trains."""
    candidates = []
    states = [(sid, ready, [], None) for sid in start_ids]
    for leg in range(MAX_LEGS):
        new_states = []
        for station, earliest, path, previous_trip in states:
            for dep, arr, dest, trip, train in edges.get(station, []):
                if dep < earliest or dep > ready + 12 * 60:
                    continue
                if path and (dep - earliest > MAX_WAIT or dep - earliest < 5):
                    continue
                segment = {'from_stop_id': station, 'to_stop_id': dest, 'departure': dep, 'arrival': arr, 'train': train, 'trip_id': trip}
                new_path = path + [segment]
                if dest in target_ids:
                    candidates.append(new_path)
                elif leg < MAX_LEGS - 1:
                    new_states.append((dest, arr, new_path, trip))
        # Bound search to avoid combinatorial explosion.
        new_states.sort(key=lambda s: (s[1], len(s[2])))
        states = new_states[:1500]
    candidates.sort(key=lambda p: (p[-1]['arrival'], len(p)))
    distinct = []
    seen = set()
    for p in candidates:
        key = (p[0]['departure'], p[-1]['arrival'], tuple(x['trip_id'] for x in p))
        if key not in seen:
            distinct.append(p)
            seen.add(key)
        if len(distinct) >= limit:
            break
    return distinct


def clock(mins):
    return f'{mins // 60:02d}:{mins % 60:02d}'


def main():
    arrivals = ics_events(ICS)
    if not arrivals:
        raise ValueError('No December 2026 CPH->BGY/TRN arrivals found in Ryanair ICS')
    trips, by_trip, names, cal, exceptions, stop_names = load_gtfs()
    stations = {airport: resolve(names_list, names) for airport, names_list in STATION_START.items()}
    targets = {name: resolve((name,), names) for name in TARGETS}
    missing = [name for name, ids in {**stations, **targets}.items() if not ids]
    if missing:
        raise ValueError('Stations missing from GTFS: ' + ', '.join(missing))
    cache = {}
    results = []
    for event in arrivals:
        arrival = event['arrival']
        ready_dt = arrival + timedelta(minutes=AIRPORT_BUFFERS[event['airport']])
        day = ready_dt.date()
        # Search first train from station on ready date; if late-night, next-day feed not handled.
        if day not in cache:
            cache[day] = departures_for_day(day, trips, by_trip, cal, exceptions)
        ready = ready_dt.hour * 60 + ready_dt.minute
        choices = {}
        for name, target_ids in targets.items():
            paths = options(cache[day], stations[event['airport']], target_ids, ready)
            choices[name] = [{'departure': clock(p[0]['departure']), 'arrival': clock(p[-1]['arrival']), 'transfers': len(p)-1, 'legs': [{'train': leg['train'], 'departure': clock(leg['departure']), 'arrival': clock(leg['arrival']), 'from': stop_names[leg['from_stop_id']]['stop_name'], 'to': stop_names[leg['to_stop_id']]['stop_name']} for leg in p]} for p in paths]
        # Nizza preferred when first option has acceptable transfer times; otherwise earliest alternate.
        recommended = 'NIZZA MONFERRATO' if choices['NIZZA MONFERRATO'] else min((k for k in TARGETS if choices[k]), key=lambda k: choices[k][0]['arrival'], default=None)
        results.append({'flight': event['flight'], 'airport': event['airport'], 'flight_arrival_local': arrival.isoformat(), 'assumed_ready_at_station': ready_dt.isoformat(), 'recommended_destination': recommended, 'options': choices})
    output = {'period': MONTH, 'status': 'test_only_not_verified', 'gtfs_source': GTFS_FILE or GTFS_URL, 'airport_transfer_assumption_minutes': AIRPORT_BUFFERS, 'max_train_transfer_wait_minutes': MAX_WAIT, 'warning': 'Airport-to-station ground transfer is NOT calculated from real bus/train timetables. Confirm schedules and connection times before travel. GTFS may not yet cover all December dates.', 'flight_arrivals': len(arrivals), 'results': results}
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Flight arrivals:', len(arrivals))
    for r in results:
        print(r['flight'], r['flight_arrival_local'], r['airport'], '=>', r['recommended_destination'] or 'NO CONNECTION')
    print('Report:', OUT)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'TEST FAILED: {type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
