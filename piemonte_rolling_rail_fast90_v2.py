#!/usr/bin/env python3
"""Read-only Piemonte rail candidate with date-exact GTFS service validation.
Requires piemonte_rolling_rail.py and test_tog_december_2026_v2.py.
Never publishes or changes the Ryanair/Outlook calendars.
"""
import argparse
import json
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import piemonte_rolling_rail as base

rail = base.rail
ROME = base.ROME


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=90)
    args = parser.parse_args()
    if not 1 <= args.days <= 365:
        parser.error('--days must be 1..365')
    today = datetime.now(ROME).date()
    end_day = today + timedelta(days=args.days)
    arrivals = []
    month = today.replace(day=1)
    while month < end_day:
        rail.MONTH = month.strftime('%Y-%m')
        arrivals.extend(rail.ics_events(rail.ICS))
        month = base.month_shift(month, 1)
    arrivals = sorted((e for e in arrivals if today <= e['arrival'].date() < end_day
                       and 360 <= e['arrival'].hour * 60 + e['arrival'].minute <= 1080),
                      key=lambda e: e['arrival'])
    if not arrivals:
        raise RuntimeError('No qualifying flights; published calendars unchanged')

    trips, by_trip, names, cal, exceptions, stops = rail.load_gtfs()
    starts = {a: rail.resolve(s, names) for a, s in rail.STATION_START.items()}
    targets = {d: rail.resolve((d,), names) for d in base.TARGETS}
    if any(not ids for ids in [*starts.values(), *targets.values()]):
        raise RuntimeError('Required station missing in GTFS')

    # Evaluate the actual service calendar on EVERY day, including exceptions.
    # Cache only identical *exact* sets of active service IDs. Holidays and
    # cancellations produce different keys and cannot reuse a weekday graph.
    active_by_day = {}
    for offset in range(args.days + 1):
        day = today + timedelta(days=offset)
        active_by_day[day] = frozenset(rail.service_active(day, cal, exceptions))
    coverage = {d.isoformat(): bool(ids) for d, ids in active_by_day.items() if d < end_day}
    missing_service_days = [d for d, covered in coverage.items() if not covered]

    edge_cache = OrderedDict()
    service_patterns_seen = set()
    route_cache = {}
    events, counts, no_connection = [], Counter(), []
    stamp_value = base.stamp(datetime.now(timezone.utc))
    for e in arrivals:
        ready_dt = e['arrival'] + timedelta(minutes=rail.AIRPORT_BUFFERS[e['airport']])
        day = ready_dt.date()
        service_key = active_by_day.get(day)
        if not service_key:
            no_connection.append({'flight': e['flight'], 'arrival': e['arrival'].isoformat(),
                                  'reason': 'GTFS has no active services for travel date'})
            continue
        service_patterns_seen.add(service_key)
        if service_key in edge_cache:
            edges = edge_cache[service_key]
            edge_cache.move_to_end(service_key)
        else:
            edges = rail.departures_for_day(day, trips, by_trip, cal, exceptions)
            edge_cache[service_key] = edges
            if len(edge_cache) > 8:
                edge_cache.popitem(last=False)
        ready = ready_dt.hour * 60 + ready_dt.minute
        # Same service pattern, airport and readiness minute imply same route options.
        route_key = (service_key, e['airport'], ready)
        if route_key not in route_cache:
            best = {}
            for dest in base.TARGETS:
                paths = rail.options(edges, starts[e['airport']], targets[dest], ready, limit=4)
                eligible = [p for p in paths if base.valid_path(p, ready)]
                if eligible:
                    best[dest] = min(eligible, key=lambda p: (p[-1]['arrival'], len(p)))
            route_cache[route_key] = best
        best = route_cache[route_key]
        if not best:
            no_connection.append({'flight': e['flight'], 'arrival': e['arrival'].isoformat(),
                                  'reason': 'no valid route'})
            continue
        fastest = min(best, key=lambda d: (best[d][-1]['arrival'], len(best[d])))
        chosen = ('NIZZA MONFERRATO' if 'NIZZA MONFERRATO' in best and
                  best['NIZZA MONFERRATO'][-1]['arrival'] <= best[fastest][-1]['arrival'] + 45 else fastest)
        others = [d for d in best if d != chosen]
        alternative = min(others, key=lambda d: (best[d][-1]['arrival'], len(best[d]))) if others else None
        for dest, label in ((chosen, 'ANBEFALET'), (alternative, 'ALTERNATIV')):
            if not dest:
                continue
            path = best[dest]
            dep = datetime.combine(day, datetime.min.time(), ROME) + timedelta(minutes=path[0]['departure'])
            finish = datetime.combine(day, datetime.min.time(), ROME) + timedelta(minutes=path[-1]['arrival'])
            details = ' | '.join(
                f"{stops[x['from_stop_id']]['stop_name']} {rail.clock(x['departure'])} -> "
                f"{stops[x['to_stop_id']]['stop_name']} {rail.clock(x['arrival'])} (tog {x['train']})"
                for x in path)
            uid = (f"rail-fast90-{e['flight']}-{e['arrival']:%Y%m%dT%H%M}-"
                   f"{dest.replace(' ', '')}@hfedder-piemonte")
            events.extend(['BEGIN:VEVENT', 'UID:' + uid, 'DTSTAMP:' + stamp_value,
                           'DTSTART:' + base.stamp(dep), 'DTEND:' + base.stamp(finish),
                           'SUMMARY:' + base.ical_escape(
                               f"TOG {label} (TEST) {stops[path[0]['from_stop_id']]['stop_name']} -> {dest}"),
                           'LOCATION:' + base.ical_escape(stops[path[0]['from_stop_id']]['stop_name']),
                           'DESCRIPTION:' + base.ical_escape(
                               f"TEST, IKKE VERIFICERET. Efter {e['flight']} til {e['airport']} "
                               f"kl. {e['arrival']:%H:%M}. Lufthavnstransport estimeret til "
                               f"{rail.AIRPORT_BUFFERS[e['airport']]} minutter. {details}. "
                               'Kontrollér shuttle og tog hos operatøren.'),
                           'STATUS:TENTATIVE', 'TRANSP:TRANSPARENT', 'END:VEVENT'])
            counts[label] += 1
            counts[f'{label}:{dest}'] += 1
        print(f"{e['arrival'].date()} {e['flight']} {e['airport']} -> {chosen}", flush=True)

    report = {'window_start': today.isoformat(), 'window_end_exclusive': end_day.isoformat(),
              'qualifying_flights': len(arrivals), 'recommended': counts['ANBEFALET'],
              'alternatives': counts['ALTERNATIV'], 'destination_counts': dict(counts),
              'no_connection': no_connection, 'days_without_active_gtfs_service': missing_service_days,
              'service_patterns_used': len(service_patterns_seen), 'route_searches_cached': len(route_cache),
              'published': False, 'status': 'test_only_not_verified',
              'warning': 'GTFS and airport shuttle journeys not independently verified.'}
    Path('piemonte_fast90_v2_validation.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if not events:
        raise RuntimeError('No valid events; existing published calendars unchanged')
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0',
             'PRODID:-//hfedder//Piemonte Rail Fast90 v2 Test//DA', 'CALSCALE:GREGORIAN',
             'METHOD:PUBLISH', 'X-WR-CALNAME:Piemonte Tog (TEST 90 dage)'] + events + ['END:VCALENDAR']
    Path('Piemonte_tog_fast90_v2_candidate.ics').write_bytes(
        ('\r\n'.join(base.fold(line) for line in lines) + '\r\n').encode('utf-8'))
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
