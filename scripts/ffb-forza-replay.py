#!/usr/bin/env python3
"""Replay a Forza Horizon capture through ForzaRack (oversteer/ffb_engine.py, ffb_forza.py), as numbers.

    scripts/ffb-forza-replay.py FILE.ovcap.gz [--rim 279] [--peak 3.5]

Pass 1 feeds each car's samples (in capture order) to a ForzaRack: the mass learner runs, the learnt mass per car is
printed. Pass 2 replays with that mass as the stored one and reports the rim torque (Nm at the wheel's rim, the
hand-force mapping with the --rim wheel on the --peak Nm wheel) by case, and the 30-120 Hz content of the steering
share before and after the output filter (stepped at 1 ms between the capture's packets). The game's force was not
recorded: the mismatch check has nothing to read. Nothing here touches a wheel, the proxy or the running app; the car
store is a temporary file."""
import argparse
import collections
import math
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from oversteer import ffb_carprops, ffb_engine as fe, ffb_forza as fz  # noqa: E402
from oversteer.telemetry_capture import read_capture  # noqa: E402
from oversteer.telemetry_formats import decode_sample  # noqa: E402

CASES = (('cruise |ay|<2, s<0.3', lambda ay, s: ay < 2 and s < 0.3),
         ('cornering |ay| 3-7, s<0.8', lambda ay, s: 3 <= ay < 7 and s < 0.8),
         ('hard |ay|>=7, s<1', lambda ay, s: ay >= 7 and s < 1),
         ('limit s 1-2', lambda ay, s: 1 <= s < 2),
         ('past peak s>2', lambda ay, s: s >= 2))


def load(path):
    items = []
    for t, _addr, data in read_capture(path)[1]:
        sample = decode_sample(data)
        if sample is not None and sample.game == 'forza-fh' and sample.running and sample.car is not None:
            items.append((t, sample))
    return items


def band_rms(x, dt=0.002):
    from scipy.signal import butter, sosfilt
    sos = butter(2, [30, 120], btype='band', fs=1 / dt, output='sos')
    return float(np.sqrt(np.mean(sosfilt(sos, x) ** 2)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('capture')
    ap.add_argument('--rim', type=float, default=279.0)
    ap.add_argument('--peak', type=float, default=3.5)
    args = ap.parse_args()
    items = load(args.capture)
    cars = collections.OrderedDict()
    for t, s in items:
        cars.setdefault(s.car, []).append((t, s))
    ffb_carprops.ASYNC = False
    ffb_carprops.STORE.use(os.path.join(tempfile.mkdtemp(prefix='ffb-forza-replay-'), 'props.json'))

    def rack_for(car):
        rack = fe.ForzaRack()
        rack.wheel_peak, rack.rim_user = args.peak, args.rim
        rack.reset(car)
        return rack
    print('capture %s: %d samples, cars %s' % (os.path.basename(args.capture), len(items), ', '.join(
        '%s (%d)' % (c, len(v)) for c, v in cars.items())))
    print('\nmass learnt (kg):')
    for car, rows in cars.items():
        rack = rack_for(car)
        for t, s in rows:
            rack.feed(t, s, None)
        learner = rack.mass_l
        print('  %-16s %5.0f  accepted %s  r %s  sd(a) %s  qualified %.0f s  (unclamped estimate %s)' % (
            car, rack.mass, learner.accepted, '-' if learner.r is None else '%.2f' % learner.r,
            '-' if learner.sd is None else '%.2f' % learner.sd, learner.qualified,
            '-' if learner.estimate is None else '%.0f' % learner.estimate))
        ffb_carprops.STORE.set_mass(car, rack.mass)
    print('\nrim torque (Nm at the %.0f mm rim of a %.1f Nm wheel, rho %.2f), by case:' % (args.rim, args.peak, args.rim / 375.0))
    buzz = []
    for car, rows in cars.items():
        rack = rack_for(car)
        rack.mass_l.add = lambda *a: False            # the mass learnt in pass 1 stands
        flt = fz.OutputFilter(fe.FULL)
        share, ay, slip, ts = [], [], [], []
        for t, s in rows:
            if s.speed is None or s.speed < 5.0:
                rack.feed(t, s, None)
                continue
            rack.feed(t, s, None)
            share.append(rack.force(s) / fe.FULL)
            ay.append(abs(s.accel[1]))
            slip.append(max(abs(s.combined_slip[0]), abs(s.combined_slip[1])))
            ts.append(t)
        share, ay, slip, ts = np.array(share), np.array(ay), np.array(slip), np.array(ts)
        print('  %s (mass %.0f kg, %.0f s moving)' % (car, rack.mass, len(ts) / 60.0))
        for name, test in CASES:
            q = np.array([test(a, s) for a, s in zip(ay, slip)])
            if q.sum() < 20:
                continue
            nm = np.abs(share[q]) * args.peak
            print('    %-26s %4.0f%%  rim p50 %.2f p90 %.2f max %.2f Nm' % (name, 100 * q.mean(), np.median(nm),
                                                                        np.percentile(nm, 90), nm.max()))
        # the buzz: the longest unbroken runs at a 2 ms tick, ZOH staircase against the filter
        br = np.flatnonzero(np.diff(ts) > 0.05)
        starts, ends = np.r_[0, br + 1], np.r_[br + 1, len(ts)]
        raw_all, out_all = [], []
        for a, b in zip(starts, ends):
            if b - a < 300:
                continue
            flt.reset()
            tt = np.arange(ts[a], ts[b - 1], 0.002)
            idx = np.searchsorted(ts[a:b], tt, side='right') - 1
            raw, out = [], []
            for t, i in zip(tt, idx):
                if i >= 0 and (not raw or ts[a + i] > t - 0.002):
                    flt.packet(ts[a + i])
                x = share[a + i] * fe.FULL
                raw.append(x)
                out.append(flt.step(t, x))
            raw_all.append(np.array(raw))
            out_all.append(np.array(out))
        if raw_all:
            r = np.sqrt(np.mean([band_rms(x) ** 2 for x in raw_all])) / fe.FULL
            o = np.sqrt(np.mean([band_rms(x) ** 2 for x in out_all])) / fe.FULL
            buzz.append((car, r, o, flt.rate))
    print('\n30-120 Hz content of the steering share (rms, share of full scale), before -> after the output filter:')
    for car, r, o, rate in buzz:
        print('  %-16s %.4f -> %.4f  (x%.2f; rate seen %.0f/s, corner %.0f Hz)' % (car, r, o, o / r if r else 0.0, rate,
                                                                                min(max(rate / 3.0, 15.0), 60.0)))


if __name__ == '__main__':
    main()
