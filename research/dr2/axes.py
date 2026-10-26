"""Check DiRT Rally 2.0's axes, signs and units on marth's captures (read-only), through the current decoder.

    python3 research/dr2/axes.py [CAPTURE ...]

Per stage run (clock running, speed > 5 m/s): the vertical axis (ranges of x, y, z), the forward vector against the
direction of motion, the decoded car-frame velocity, which plan (x,z or x,-z) turns the same way as the steering and
the lateral g, the g fields against the change of speed and speed x heading rate, wheel speeds against speed,
the suspension's sign under braking, brake temperatures and the other per-wheel fields. Writes nothing.
"""
import glob
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from inventory import FOLDER, dr2_packets  # noqa: E402
from oversteer import telemetry_formats as tf  # noqa: E402


def corr(a, b):
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 50:
        return float('nan')
    return float(np.corrcoef(a[m], b[m])[0, 1])


def runs_of(path):
    run = []
    for t, f in dr2_packets(path):
        if f[1] > 0 and f[3] < 1.5 and f[63] > 0:
            if run and f[1] < run[-1][1][1] - 1:
                yield run
                run = []
            if not run or f[1] != run[-1][1][1]:          # skip repeated (paused) packets
                run.append((t, f))
        elif run:
            yield run
            run = []
    if run:
        yield run


def main(paths):
    if not paths:
        paths = sorted(glob.glob(os.path.join(os.path.expanduser(FOLDER), '*.ovcap.gz')))
    for path in paths:
        for run in runs_of(path):
            if len(run) < 2000:
                continue
            F = np.array([f for _t, f in run])
            T = F[:, 1]                                     # the stage clock
            S = [tf._codemasters(struct_pack(f), 264) for _t, f in run]
            spd = F[:, 7]
            mov = spd > 5
            print('== %s run %.0f m, %.1f s, %d pkts (%.0f Hz)' % (os.path.basename(path), F[-1, 61], T[-1], len(F),
                                                                   len(F) / max(T[-1], 1)))
            print('  ranges x %.0f y %.0f z %.0f (vertical: smallest)' % tuple(np.ptp(F[:, i]) for i in (4, 5, 6)))
            dt = np.gradient(T)
            dt[dt <= 0] = np.nan
            vwx, vwy, vwz = (np.gradient(F[:, i]) / dt for i in (4, 5, 6))
            print('  d(pos)/dt vs vel: r x %.3f y %.3f z %.3f' % (corr(vwx, F[:, 8]), corr(vwy, F[:, 9]), corr(vwz, F[:, 10])))
            fwd = F[:, 14:17]
            v = F[:, 8:11]
            cos = np.sum(fwd * v, 1) / np.maximum(np.linalg.norm(v, axis=1), 1e-6)
            print('  cos(pitch vector, velocity) median %.3f (moving)' % np.median(cos[mov]))
            vel = np.array([s.vel if s and s.vel else (np.nan,) * 3 for s in S])
            print('  car-frame vel: fwd/speed median %.3f, |lateral| p95 %.2f m/s, up p95 %.2f'
                  % (np.median(vel[mov, 0] / spd[mov]), np.percentile(np.abs(vel[mov, 1]), 95),
                     np.percentile(np.abs(vel[mov, 2]), 95)))
            up = np.array([s.up if s and s.up else (np.nan,) * 3 for s in S])
            print('  up (forward x left) mean %s' % np.round(np.nanmean(up, 0), 3))
            for name, (a, b) in (('x,z', (4, 6)), ('x,-z', (4, -6))):
                px, pz = F[:, a], F[:, abs(b)] * (1 if b > 0 else -1)
                hd = np.unwrap(np.arctan2(np.gradient(pz), np.gradient(px)))
                rate = np.gradient(hd) / dt
                rate[~mov] = np.nan
                # the decoder's steer is positive left; its accel[1] the lateral g (sign to verify)
                steer = np.array([s.steer if s and s.steer is not None else np.nan for s in S])
                print('  plan %-5s heading rate vs steer(dec) r %.3f, vs g_lat raw r %.3f, speed*rate vs g_lat*G r %.3f'
                      % (name, corr(rate, steer), corr(rate, F[:, 34]), corr(spd * rate, F[:, 34] * tf.G)))
            dv = np.gradient(spd) / dt
            k = 5
            dvs = np.convolve(np.nan_to_num(dv), np.ones(k) / k, 'same')
            print('  g_lon vs dv/dt r %.3f, slope %.2f' % (corr(F[:, 35] * tf.G, dvs),
                                                          np.polyfit(dvs[mov & np.isfinite(dvs)], F[mov & np.isfinite(dvs), 35] * tf.G, 1)[0]))
            ws = F[:, 25:29]
            print('  wheel speed / speed median RL %.3f RR %.3f FL %.3f FR %.3f' % tuple(np.median(ws[mov] / spd[mov, None], 0)))
            brk = F[:, 31] > 0.5
            sus = F[:, 17:21]
            print('  susp mm mean all %s, braking front-rear %.1f (positive: front compresses more)'
                  % (np.round(np.mean(sus, 0), 1), np.mean(sus[brk & mov][:, 2:4]) - np.mean(sus[brk & mov][:, 0:2])
                     - (np.mean(sus[mov][:, 2:4]) - np.mean(sus[mov][:, 0:2]))))
            bt = F[:, 51:55]
            print('  brake temp C p5 %.0f p95 %.0f; share at -273.15: %.3f' % (np.percentile(bt, 5), np.percentile(bt, 95),
                                                                             np.mean(bt <= -273)))
            print('  gear values %s, rpm max %.0f, idle %s'
                  % (sorted(set(F[:, 33].astype(int))), F[:, 37].max() * tf.RAD_S, sorted(set(np.round(F[:, 64] * tf.RAD_S)))))


def struct_pack(f):
    import struct
    return struct.pack('<66f', *f)


if __name__ == '__main__':
    main(sys.argv[1:])
