#!/usr/bin/env python3
"""Generate 2027 monthly rail test reports using the existing v2 GTFS engine.
Read-only: never changes the Ryanair calendar or any published ICS.
"""
import json
import sys
from pathlib import Path
from datetime import time
import test_tog_december_2026_v2 as rail

rail.STATION_START['BGY'] = ('MILANO CENTRALE',)
rail.AIRPORT_BUFFERS['BGY'] = 105
rail.AIRPORT_BUFFERS['TRN'] = 90
original_events = rail.ics_events
original_load = rail.load_gtfs
cache = None

def cached_gtfs():
    global cache
    if cache is None:
        cache = original_load()
    return cache

def filtered_events(path):
    events = original_events(path)
    return [e for e in events if time(6, 0) <= e['arrival'].time().replace(tzinfo=None) <= time(18, 0)]

rail.load_gtfs = cached_gtfs
rail.ics_events = filtered_events


def main():
    report = {'months': {}, 'note': 'TEST ONLY. Airport transfers estimated; GTFS schedules not independently verified.'}
    for month in range(1, 13):
        period = f'2027-{month:02d}'
        rail.MONTH = period
        rail.OUT = Path(f'tog_{period.replace("-", "_")}.json')
        # Do not manufacture flights or train schedules for uncovered months.
        if not filtered_events(rail.ICS):
            report['months'][period] = {'status': 'no_flight_arrivals_in_ics'}
            print(f'{period}: no qualifying arrivals; no monthly file generated')
            continue
        try:
            rail.main()
            data = json.loads(rail.OUT.read_text(encoding='utf-8'))
            data['status'] = 'test_only_not_verified'
            data['warning'] = ('BGY to Milano Centrale: 105-minute estimate; TRN: 90-minute estimate. '
                               'GTFS and airport transfers not independently verified. '
                               'Same-day search only; review holiday services and transfers.')
            rail.OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            report['months'][period] = {'status': 'generated', 'flights': len(data['results'])}
        except Exception as exc:
            report['months'][period] = {'status': 'error', 'message': str(exc)}
            print(f'{period}: ERROR {exc}', file=sys.stderr)
    Path('tog_2027_search_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    if not any(x['status'] == 'generated' for x in report['months'].values()):
        raise RuntimeError('No monthly rail reports generated')
    print('Wrote tog_2027_search_report.json')

if __name__ == '__main__':
    main()
