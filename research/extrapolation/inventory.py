#!/usr/bin/env python3
"""Channel inventory from the captures themselves (not from the decoder):
for each game, which channels carry data, at what packet rate, over what
range, at what resolution, and how they behave while driving.

    python3 research/extrapolation/inventory.py > research/extrapolation/out/inventory.txt
"""
import collections
import math
import os

import numpy as np

import common

CHANNELS = list(common.SCALARS) + list(common.VECTORS) + list(common.OVST3_EXTRA)


def describe(x):
    """present fraction, varying, min, p50, max, resolution of a column
    (rows x k or rows)."""
    x = np.asarray(x, dtype=float)
    if x.ndim == 1:
        x = x[:, None]
    fin = np.isfinite(x).all(axis=1)
    if not fin.any():
        return None
    v = x[fin]
    nz = (np.abs(v) > 1e-9).any(axis=1).mean()
    flat = v.ravel()
    uniq = np.unique(np.round(flat, 6))
    steps = np.diff(uniq)
    res = float(np.median(steps[steps > 0])) if (steps > 0).any() else 0.0
    return {'present': fin.mean(), 'nonzero': nz, 'varying': float(np.nanstd(v, axis=0).max()),
            'min': float(np.nanmin(v)), 'p50': float(np.nanmedian(v)), 'max': float(np.nanmax(v)),
            'res': res, 'k': x.shape[1]}


def main():
    by_game = collections.defaultdict(list)
    for p in common.capture_paths():
        cap = common.load_cached(p)
        games = [g for g in cap['game'] if g]
        g = max(set(games), key=games.count)
        moving = np.nan_to_num(cap['speed']) > 2.0
        by_game[g].append((os.path.basename(p), cap, moving))
    for g, caps in sorted(by_game.items()):
        dts, seconds, packets = [], 0.0, 0
        for name, cap, moving in caps:
            dt = np.diff(cap['t'])
            dts.append(dt[(dt > 0) & (dt < 0.5)])
            seconds += cap['t'][-1]
            packets += len(cap['t'])
        dt = np.concatenate(dts)
        print('== %s: %d captures, %d packets, %.0f min; packet interval median %.4f s (%.1f /s), p95 %.4f s, '
              'gaps > 0.1 s: %d' % (g, len(caps), packets, seconds / 60, np.median(dt), 1 / np.median(dt),
                                     np.percentile(dt, 95), int((dt > 0.1).sum())))
        print('   captures: ' + ', '.join(n for n, _c, _m in caps))
        tracks = collections.Counter(x for _n, c, _m in caps for x in c['track'] if x)
        cars = collections.Counter(x for _n, c, _m in caps for x in c['car'] if x)
        print('   tracks: %s' % dict(tracks))
        print('   cars: %s' % dict(cars))
        # repeated identical packets (game update rate below the send rate)
        reps = []
        for _n, cap, mv in caps:
            r = cap['rpm'][mv]
            if len(r) > 2:
                reps.append(np.mean(np.diff(r) == 0))
        print('   share of consecutive moving packets with identical rpm (duplicates): %.3f' % np.mean(reps))
        print('   %-16s %5s %5s %3s %12s %12s %12s %10s' % ('channel', 'pres', 'nz', 'k', 'min', 'p50', 'max', 'res'))
        for ch in CHANNELS:
            cols = []
            for _n, cap, mv in caps:
                cols.append(cap[ch][mv])
            d = describe(np.concatenate(cols))
            if d is None or (d['nonzero'] == 0):
                state = 'absent' if d is None else 'zeros'
                print('   %-16s %s' % (ch, state))
                continue
            print('   %-16s %5.2f %5.2f %3d %12.4g %12.4g %12.4g %10.3g' % (
                ch, d['present'], d['nonzero'], d['k'], d['min'], d['p50'], d['max'], d['res']))
        print()


if __name__ == '__main__':
    main()
