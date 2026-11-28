#!/usr/bin/env python3
"""Read-only December 2026 rail test: exclude flights arriving after 18:00.

Requires test_tog_december_2026_v2.py and test_tog_december_2026_v3.py
in the same directory. Does not modify any calendar or published ICS.
"""
from datetime import time
import test_tog_december_2026_v3 as v3

rail = v3.rail
original_ics_events = rail.ics_events


def arrivals_before_cutoff(path):
    all_arrivals = original_ics_events(path)
    selected = [event for event in all_arrivals
                if event['arrival'].timetz().replace(tzinfo=None) <= time(18, 0)]
    print(f"Flight arrivals before/at 18:00: {len(selected)} of {len(all_arrivals)}")
    print(f"Excluded arrivals after 18:00: {len(all_arrivals) - len(selected)}")
    return selected


rail.ics_events = arrivals_before_cutoff

if __name__ == '__main__':
    raise SystemExit(rail.main())
