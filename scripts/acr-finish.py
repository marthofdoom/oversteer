#!/usr/bin/env python3
"""Add `finish_m` (the flying finish along the road spline) to the Assetto
Corsa Rally stages of data/telemetry/stages/acr.json, from recorded runs.

    scripts/acr-finish.py [CAPTURE_DIR ...] [--out PATH]

The game's pace-note tables carry no finish marker, and `pacenote_last_m`
is the stop control: marth's runs are at 120-160 km/h 200 m before it and
at 20-40 km/h on it. The stage clock is not in what the bridge reads, so
the line is taken from where the final slowdown starts: per complete run
(it reaches the last pace note), the first brake press past the fastest
point of the last 300 m before the stop control. A stage gets `finish_m`
(the median over its runs, less those further than OUTLIER_M from it) only with at least MIN_RUNS runs
within SPREAD_M of each other. Only the derived numbers are kept, never
the captures. The driver brakes once past the line, so the value is at
or a few metres beyond it.
"""
import argparse
import glob
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
from oversteer import stage_tables                                        # noqa: E402
from oversteer.telemetry_capture import read_capture                      # noqa: E402
from oversteer.telemetry_formats import decode_sample                     # noqa: E402

OUT = os.path.join(HERE, '..', 'data', 'telemetry', 'stages', 'acr.json')
CAPTURES = ('~/.local/share/oversteer/captures',
            '~/.var/app/io.github.berarma.Oversteer/data/oversteer/captures')
WINDOW = 300.0         # m before the stop control searched for the final slowdown
BRAKE = 0.2
MIN_RUNS = 2
OUTLIER_M = 25.0       # a run further than this from the median is left out (a slow approach)
SPREAD_M = 45.0        # widest gap between a stage's runs (Afon Bidno's 10 kept runs: 35 m)


def runs_of(path):
    """Each run of a capture as a list of (track, distance, speed, brake)."""
    meta, records = read_capture(path)
    cur = []
    for _t, _src, data in records:
        s = decode_sample(data)
        if s is None or s.game != 'acr' or s.lap_distance is None:
            continue
        row = (s.track, s.lap_distance, s.speed or 0.0, s.brake or 0.0, s.stage_length)
        if cur and (row[0] != cur[-1][0] or row[1] < cur[-1][1] - 300.0):
            yield cur
            cur = []
        cur.append(row)
    if cur:
        yield cur


def onset(run, last):
    """Where the final slowdown starts, relative to `last`, or None."""
    if max(r[1] for r in run) < last:
        return None                                  # did not finish
    window = [r for r in run if last - WINDOW <= r[1] <= last]
    if not window:
        return None
    peak = max(window, key=lambda r: r[2])
    after = [r for r in window if r[1] >= peak[1] and r[3] > BRAKE]
    return after[0][1] - last if after else None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('dirs', nargs='*')
    parser.add_argument('--out', default=OUT)
    args = parser.parse_args()
    found = {}
    for folder in args.dirs or CAPTURES:
        for path in sorted(glob.glob(os.path.join(os.path.expanduser(folder), '*.ovcap.gz'))):
            try:
                for run in runs_of(path):
                    entry = stage_tables.acr_stage(run[0][0], run[0][1], run[0][4])
                    if not entry or not entry.get('pacenote_last_m'):
                        continue
                    o = onset(run, entry['pacenote_last_m'])
                    if o is not None and o >= -WINDOW + 5.0:
                        found.setdefault(entry['stage'], []).append(o)
            except ValueError:
                continue
    with open(args.out, encoding='utf-8') as f:
        table = json.load(f)
    for entry in table['stages']:
        offsets = found.get(entry['stage']) if entry.get('track') else None
        entry.pop('finish_m', None)
        for key in ('finish_runs', 'finish_spread_m', 'finish_confidence', 'finish_source'):
            entry.pop(key, None)
        if offsets:
            middle = statistics.median(offsets)
            kept = [o for o in offsets if abs(o - middle) <= OUTLIER_M]
            offsets = kept if len(kept) * 3 >= len(offsets) * 2 else offsets
        if not offsets or len(offsets) < MIN_RUNS or max(offsets) - min(offsets) > SPREAD_M:
            print('{:28} {} runs, no finish_m {}'.format(entry['stage'], len(offsets or []), sorted(round(o) for o in offsets or [])))
            continue
        entry['finish_m'] = round(entry['pacenote_last_m'] + statistics.median(offsets), 1)
        entry['finish_runs'] = len(offsets)
        entry['finish_spread_m'] = round(max(offsets) - min(offsets), 1)
        entry['finish_confidence'] = 'medium'
        entry['finish_source'] = ("marth's runs: where the final slowdown to the stop control starts "
                                  "(median of {}), the game's pace notes have no finish marker".format(len(offsets)))
        print('{:28} finish_m {} ({} runs, spread {})'.format(
            entry['stage'], entry['finish_m'], len(offsets), entry['finish_spread_m']))
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(table, f, indent=1, ensure_ascii=False)
        f.write('\n')


if __name__ == '__main__':
    main()
