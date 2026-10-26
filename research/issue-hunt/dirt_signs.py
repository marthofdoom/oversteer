"""Issue hunt 2026-10: absolute signs of DiRT Rally 2.0's steer, lateral g and plan positions, from the suspension.

    python3 research/issue-hunt/dirt_signs.py CAPTURE.ovcap.gz [...]

The wheel labels (rear-left, rear-right, front-left, front-right) are the one absolute reference in the packet. The
compression sign is fixed by braking (the front compresses against the rear under the brake); then the body's roll
in a corner (the outside wheels compress) says which way the car turns, against which the decoded steer (positive
left), the lateral g (positive left) and the heading rate of the plan (x, z) positions (positive left) are checked.
"""
import math
import sys
import os
import struct

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from oversteer.telemetry_capture import read_capture  # noqa: E402
from oversteer.telemetry_formats import decode_sample, plan_xy  # noqa: E402


def main(paths):
    for path in paths:
        _m, rec = read_capture(path)
        rows = []
        for t, _a, data in rec:
            if len(data) != 264:
                continue
            s = decode_sample(data)
            if s is None or s.speed is None or s.speed < 8:
                continue
            f = struct.unpack_from('<66f', data, 0)
            rl, rr, fl, fr = f[17:21]
            p = plan_xy(s.game, s.pos)
            rows.append((t, s.steer, s.accel[1] if s.accel else math.nan, (fl + rl) - (fr + rr), (fl + fr) - (rl + rr),
                         s.brake or 0.0, p[0], p[1], s.speed))
        a = np.array(rows)
        t, steer, alat, left_minus_right, front_minus_rear, brake, x, y, v = a.T
        # compression sign: braking hard vs gently
        hard, easy = brake > 0.5, brake < 0.05
        dive = np.median(front_minus_rear[hard]) - np.median(front_minus_rear[easy])
        comp = 1.0 if dive > 0 else -1.0          # +1: the value grows with compression
        roll_left_turn = -comp * left_minus_right     # > 0 when the right side is the more compressed: a left turn
        head = np.unwrap(np.arctan2(np.gradient(y), np.gradient(x)))
        hr = np.gradient(head) / np.gradient(t)
        hr[~np.isfinite(hr)] = np.nan

        def r(u, w):
            m = np.isfinite(u) & np.isfinite(w) & (np.abs(hr) < 3)
            return round(float(np.corrcoef(u[m], w[m])[0, 1]), 3)
        print(os.path.basename(path), 'rows', len(a), 'dive(front-rear, hard-easy brake)=%.4f' % dive,
              'comp_sign', comp)
        print('  corr(roll->left turn, steer)', r(roll_left_turn, steer), ' corr(roll, a_lat)', r(roll_left_turn, alat),
              ' corr(roll, heading rate of (x,z))', r(roll_left_turn, hr), ' corr(steer, a_lat)', r(steer, alat),
              ' corr(a_lat, v*hr)', r(alat, v * hr))


if __name__ == '__main__':
    main(sys.argv[1:])
