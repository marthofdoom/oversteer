#!/usr/bin/env python3
"""What two sets of recordings really played in the same situation, side by side: no replay, no targets, no score.

    scripts/ffb-match.py A [A2 ...] --vs B [B2 ...] [--min-s 1.0]

Names are relative to the captures folder. A situation is lateral acceleration, body slip, speed and pedals, all read from the
telemetry; the force is the recorded ext_force plus the driver's aligning spring where it played. Full log: ~/.cache/ffb-match/latest.log
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffb_benchmark as fb  # noqa: E402
from ffb_situations import AY, SLIP, SPEED, STEP, band, compare, label, summary, table  # noqa: E402,F401
from oversteer import ffb_record  # noqa: E402,F401

CAPTURES = os.path.expanduser('~/.var/app/io.github.berarma.Oversteer/data/oversteer/captures')
LOG = os.path.expanduser('~/.cache/ffb-match/latest.log')


def situations(path):
    """{(ay, slip, speed, pedal) band: |force| samples}, and a line about the drive."""
    _raw, played, header = fb._levels(path)
    if played is None:
        return None, os.path.basename(path)[:15] + ': no force recorded, skipped'
    _meta, samples = fb._walk(path)
    rows = []
    for t, s in samples:
        if s.vel and s.accel and s.vel[0] and (s.speed or 0.0) >= SPEED[0]:
            brake, throttle = s.brake or 0.0, s.throttle or 0.0
            rows.append((t, abs(s.accel[1]), abs(np.arctan2(s.vel[1], abs(s.vel[0]))), s.speed,
                         2 if brake > 0.1 else (1 if throttle > 0.1 else 0)))
    if len(rows) < 50:
        return None, os.path.basename(path)[:15] + ': too little moving data, skipped'
    r = np.array(rows, float)
    grid = np.arange(max(r[0, 0], played[0, 0]), min(r[-1, 0], played[-1, 0]), STEP)
    force = np.abs(np.interp(grid, played[:, 0], played[:, 1])) / fb.FULL
    near = np.searchsorted(r[:, 0], grid).clip(0, len(r) - 1)
    keep = np.abs(r[near, 0] - grid) < 0.1
    near, force = near[keep], force[keep]
    keys = zip(band(AY, r[near, 1]), band(SLIP, r[near, 2]), band(SPEED, r[near, 3]), r[near, 4].astype(int))
    out = {}
    for key, f in zip(keys, force):
        out.setdefault(tuple(int(k) for k in key), []).append(f)
    settings = header.get('settings', {}).get('profile', {}) if isinstance(header, dict) else {}
    return out, '%s: build %s, strength %s, %.0f s moving' % (os.path.basename(path)[:15], header.get('build'),
                                                             settings.get('strength'), len(force) * STEP)


def pool(names):
    cells, notes = {}, []
    for name in names:
        path = name if os.path.isabs(name) else os.path.join(CAPTURES, name if name.endswith('.ovcap.gz') else name + '.ovcap.gz')
        got, note = situations(path)
        notes.append(note)
        for key, values in (got or {}).items():
            cells.setdefault(key, []).extend(values)
    return cells, notes


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('a', nargs='+')
    parser.add_argument('--vs', nargs='+', required=True)
    parser.add_argument('--min-s', type=float, default=1.0)
    parser.add_argument('--top', type=int, default=12, help='rows printed to the screen; the log has all')
    args = parser.parse_args()
    a, notes_a = pool(args.a)
    b, notes_b = pool(args.vs)
    lines = ['A  ' + n for n in notes_a] + ['B  ' + n for n in notes_b]
    rows = compare(a, b, args.min_s)
    lines.append(summary(rows)[0])
    head, body = table(rows)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'w') as f:
        f.write('\n'.join(lines + [head] + body) + '\n')
    print('\n'.join(lines + [head] + body[:args.top]))
    print('all rows: ' + LOG)


if __name__ == '__main__':
    main()
