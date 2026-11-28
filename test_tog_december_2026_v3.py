#!/usr/bin/env python3
"""December 2026 rail test v3: BGY shuttle to Milano Centrale.

Requires test_tog_december_2026_v2.py in the same GitHub repository.
Uses an estimated 105-minute BGY airport-to-Milano-Centrale buffer,
NOT an actual shuttle timetable. No ICS files are changed.
"""
import test_tog_december_2026_v2 as rail

# From BGY airport, travel by shuttle to Milano Centrale, then take trains.
# 30 minutes airport exit + 60 minutes shuttle + 15 minutes station buffer.
rail.STATION_START['BGY'] = ('MILANO CENTRALE',)
rail.AIRPORT_BUFFERS['BGY'] = 105

# Torino remains unchanged.
if __name__ == '__main__':
    raise SystemExit(rail.main())
