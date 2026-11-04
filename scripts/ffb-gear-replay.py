#!/usr/bin/env python3
"""Replay check of the gear-crossing stage on a tuning capture: the force as recorded (ext_force at its gain, the driver's
slew and column friction, as the research did) through oversteer.ffb_gear.Crossing, scored like the research's table:
force reversals per second on the straights (no hysteresis, 3 %, 10 % FS), the lag at 5 Hz, the longest error over 0.1 FS.

    python3 scripts/ffb-gear-replay.py CAPTURE.ovcap.gz [level [flip %]]

`research` runs the research's own candidate (no drop of the old side's excess at a flip), for comparison."""
import gzip
import json
import os
import struct
import sys

import numpy as np
from scipy.signal import butter, csd, sosfiltfilt

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from oversteer import ffb_gear  # noqa: E402

FS = 1 / 500.0
GAIN = 0.75
V0, COLUMN = 499.0, 0.0201


def load(path):
    with gzip.open(path + '.ffb.gz', 'rb') as s:
        assert s.readline() == b'OVFFB1\n'
        meta = json.loads(s.readline())
        body = s.read()
    size = struct.calcsize('<dB5d')
    a = np.frombuffer(body[:len(body) // size * size], dtype=np.dtype([('t', '<f8'), ('c', 'u1'), ('v', '<f8', 5)]))
    f = a[a['c'] == meta['channels']['ext_force']]
    hands = a[a['c'] == meta['channels']['ffb_hands']]
    with gzip.open(path + '.wheel.gz', 'rb') as s:
        s.readline()
        s.readline()
        body = s.read()
    w = np.frombuffer(body[:len(body) // 16 * 16], dtype=np.dtype([('t', '<f8'), ('ty', '<u2'), ('co', '<u2'), ('va', '<i4')]))
    x = w[(w['ty'] == 3) & (w['co'] == 0)]
    t, v = f['t'], f['v'][:, 0] / 32767.0
    cuts = np.concatenate([[0], np.where(np.diff(t) > 0.25)[0] + 1, [len(t)]])
    a0, b0 = max(((cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1) if t[cuts[i + 1] - 1] - t[cuts[i]] >= 5), key=lambda r: r[0])
    g = np.arange(t[a0], t[b0 - 1], FS)
    force = v[np.searchsorted(t[a0:b0], g, side='right') - 1 + a0] * GAIN
    pos = x['va'].astype(float)[np.clip(np.searchsorted(x['t'].astype(float), g, side='right') - 1, 0, len(x) - 1)]
    on = hands['v'][:, 1][np.clip(np.searchsorted(hands['t'], g, side='right') - 1, 0, len(hands) - 1)] > 0.5
    return force, pos, on


def sat(x):
    x = np.clip(x, -3, 3)
    return x * (27 + x * x) / (27 + 9 * x * x)


def chain(force, pos):
    """The driver's slew, velocity EMA and the column friction it adds."""
    y, c = np.zeros(len(force)), 0.0
    step = 2 / 12.0
    for i, f in enumerate(force):
        c += min(max(f - c, -step), step)
        y[i] = c
    vel, v, p0 = np.zeros(len(pos)), 0.0, pos[0]
    for i in range(1, len(pos)):
        v += ((pos[i] - p0) / FS - v) / 4.0
        p0 = pos[i]
        vel[i] = v
    return y + COLUMN * sat(vel / V0), vel


def reversals(y, h):
    if h == 0:
        nz = y[y != 0]
        return int(np.sum(nz[1:] * nz[:-1] < 0))
    up, dn = y > h, y < -h
    k = np.where(up | dn)[0]
    s = up[k].astype(int) * 2 - 1
    return int(np.sum(s[1:] != s[:-1]))


def research(s):
    p, flip, band, rate, rejoin = ffb_gear.params(ffb_gear.OVERALL_GEAR_GUARD_LEVEL)
    y, side, c, moving = np.zeros(len(s)), (1 if s[0] >= 0 else -1), s[0], False
    for i, f in enumerate(s):
        if (side > 0 and f < -flip) or (side < 0 and f > flip):
            side, moving = -side, True
        want = max(f, p) if side > 0 else min(f, -p)
        if not moving:
            c = want
        elif side * c < band:
            c += side * rate * FS
            if side * c >= side * want:
                c, moving = want, False
        else:
            c += min(max(want - c, -rejoin * FS), rejoin * FS)
            if abs(want - c) <= rejoin * FS:
                c, moving = want, False
        y[i] = c
    return y


def score(name, force, y, straight):
    st = straight.sum() * FS
    rev = [round(float(reversals(y[straight], h)) / st, 1) for h in (0, 0.03, 0.10)]
    cor = [round(float(reversals(y[~straight], h)) / ((~straight).sum() * FS), 1) for h in (0, 0.03, 0.10)]
    f, p = csd(force, y, 500, nperseg=4096)
    k = (f > 4) & (f < 6)
    lag = float(-np.angle(np.sum(p[k])) / (2 * np.pi * 5) * 1e3)
    d = np.abs(y - force) > 0.1
    e = np.diff(np.concatenate([[0], d.astype(int), [0]]))
    longest = int((np.where(e == -1)[0] - np.where(e == 1)[0]).max()) * 2
    print('%-26s reversals/s straight %s corners %s  lag %.1f ms  longest off %d ms' % (name, rev, cor, lag, longest))
    return rev, lag, longest


def main():
    path = sys.argv[1]
    level = float(sys.argv[2]) if len(sys.argv) > 2 else ffb_gear.OVERALL_GEAR_GUARD_LEVEL
    flip = float(sys.argv[3]) if len(sys.argv) > 3 else ffb_gear.OVERALL_GEAR_FLIP
    force, pos, hands = load(path)
    deg = 540 / 65535.0
    angle = (pos - 32768) * deg
    straight = np.abs(sosfiltfilt(butter(2, 1 / 250, output='sos'), angle)) < 10
    s, vel = chain(force, pos)
    score('no stage', force, s, straight)
    score('research candidate', force, research(s), straight)

    def through(gate):
        stage = ffb_gear.Crossing(ffb_gear.params(level, flip_pct=flip), gate=gate)
        speed = vel * deg
        return np.array([stage.step(f, FS, v, h) for f, v, h in zip(s, speed, hands)]), stage
    score('driver stage, no gate', force, through(False)[0], straight)
    y, stage = through(True)
    score('driver stage, gate', force, y, straight)
    print('gate: bypassed %.1f %% of the drive; the output differs on %.1f %% of the straights'
          % (100.0 * stage.bypassed / stage.ticks, 100.0 * np.mean(np.abs(y - through(False)[0])[straight] > 1e-9)))


if __name__ == '__main__':
    main()
