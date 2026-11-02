#!/usr/bin/env python3
"""Replay marth's ACR dump (ffb-slip/dump.npz, from the bridge dump by ~/.cache/ffb-slip/load.py) through
RackForce by segment (a time gap over 60 s or a packet-id reset: a session or car) and report, per segment, the
trail and scrub the learner converges to against the offline regression, and R^2 of the played force against
finalFF for Mz only, the fixed pooled trail/scrub, and the learnt. The dump is a 4 Hz snapshot, not a stream, so
its snapshots (independent of each other) are shuffled and repeated at 333 Hz for SECONDS, as a stream.

    python3 scripts/ffb-carprops-replay.py [dump.npz]"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from oversteer import ffb_carprops, ffb_engine as fe                      # noqa: E402
from oversteer.telemetry_formats import Sample                            # noqa: E402

PATH = os.path.expanduser('~/.cache/ffb-slip/dump.npz')
DT = 1.0 / 333.0                         # ACR's physics rate
SECONDS = 150.0                          # of driving per segment: its snapshots shuffled and repeated to fill it


def sample(d, i, car):
    s = Sample(6000.0, 7500.0, gear=3, speed=float(d['speed'][i]) / 3.6, car=car, game='acr')
    s.tyre_mz = tuple(float(x) for x in d['mz'][i])
    s.tyre_fy = tuple(float(x) for x in d['fy'][i])
    s.tyre_fx = tuple(float(x) for x in d['fx'][i])
    s.packet_id = int(d['pid'][i])
    return s


def r2(y, p):
    return 1 - ((y - p) ** 2).sum() / ((y - y.mean()) ** 2).sum()


def offline(d, m):
    mz, fy, fx = d['mz'][m], d['fy'][m], d['fx'][m]
    X = np.c_[mz[:, 0] + mz[:, 1], fy[:, 0] + fy[:, 1], fx[:, 0] - fx[:, 1]]
    c = np.linalg.lstsq(X, d['ffb'][m], rcond=None)[0]
    return -c[1] / c[0], -c[2] / c[0]


def main(path):
    d = dict(np.load(path))
    t, pid = d['t'], d['pid']
    seg = np.zeros(len(t), int)
    for b in np.where((np.diff(t) > 60) | (np.diff(pid) < 0))[0] + 1:
        seg[b:] += 1
    ffb_carprops.STORE.use(os.path.join(os.environ.get('TMPDIR', '/tmp'), 'ffb-carprops-replay.json'))
    ffb_carprops.ASYNC = False
    print('seg      n | offline trail/scrub mm | learnt trail/scrub mm | R2 vs finalFF: Mz only / pooled / learnt')
    for k in range(seg.max() + 1):
        m = (seg == k) & (d['speed'] > 20) & (np.abs(d['ffb']) < 0.98)
        if m.sum() < 300:
            continue
        ffb_carprops.STORE.use(None)
        ffb_carprops.STORE.path = os.devnull + '.x'       # nothing kept between segments
        ffb_carprops.STORE._cars = {}
        rack = fe.RackForce()
        idx = np.where((seg == k) & (d['speed'] > 20))[0]
        ys, mzo, pooled, learnt = [], [], [], []
        rng = np.random.default_rng(1)
        order = np.concatenate([rng.permutation(idx) for _ in range(int(SECONDS / DT / len(idx)) + 1)])
        for n, i in enumerate(order[:int(SECONDS / DT)]):
            s = sample(d, i, 'seg%d' % k)
            raw = float(d['ffb'][i]) * fe.FULL
            rack.feed(n * DT, s, raw)
            if abs(d['ffb'][i]) >= 0.98:
                continue
            ys.append(d['ffb'][i] * fe.FULL)
            mzo.append(fe.front_mz(s))
            pooled.append(fe.front_torque(s))
            learnt.append(fe.front_torque(s, rack.trail, rack.scrub))
        y = np.array(ys)

        def fitted(x):
            x = np.array(x)
            return r2(y, x * (x @ y) / (x @ x))               # each at its own best strength
        ot, os_ = offline(d, m)
        print('%3d %6d | %6.1f %6.1f | %6.1f %6.1f | %.3f / %.3f / %.3f' % (
            k, m.sum(), ot * 1000, os_ * 1000, rack.trail * 1000, rack.scrub * 1000,
            fitted(mzo), fitted(pooled), fitted(learnt)))


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else PATH)
