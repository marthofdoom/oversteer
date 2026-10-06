"""The decision table on synthetic corners (tests.test_coach_context.stage): one reference and one run per row of
the table, the diagnosis each must get. The shape of the unit tests the implementation spec asks for.

    python3 research/diagnosis/synthetic.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402

from oversteer import coach_context as cc, potential  # noqa: E402
from tests.test_coach_context import stage  # noqa: E402
import diagnose as dg  # noqa: E402

ENV = {'bins': np.array([0.0, 60.0]), 'lat': np.array([10.0, 10.0])}     # 10 m/s^2 of grip at every speed
KEY = {'d': 500.0, 'd0': 470.0, 'd1': 530.0, 'direction': 1}
SEC = {'a': 400.0, 'b': 700.0, 'lo': 300.0, 'key': KEY}


def run(points, peak=0.5, corner=(470.0, 530.0), throttle=None):
    rows = stage([(0.0, 28.0)] + points + [(900.0, 28.0)], [(corner[0], corner[1], 1, peak)], throttle=throttle)
    return potential.arrays(rows)


REF = run([(430.0, 28.0), (500.0, 10.0), (600.0, 28.0)])


def check(name, arr, want, **kw):
    m = dg.measures(arr, REF, SEC, ENV)
    d = dg.diagnose(m)
    d['loss'] = m['loss']
    ok = d['code'] == want
    print('{:5s} {:14s} -> {:13s} loss {:+.2f} s | {}'.format('ok' if ok else 'FAIL', name, d['code'], m['loss'],
                                                               dg.sentence('the corner', d, 'your best run')))
    return ok


def main():
    results = [
        check('identical', REF, 'SAME'),
        # braked 20 m later, took more off: 7 m/s at the apex against 10, a third of the grip left
        check('over-slowed', run([(450.0, 28.0), (500.0, 7.0), (600.0, 28.0)], peak=0.5), 'OVER-SLOWED'),
        # braked 20 m later, 16 km/h faster at the turn-in, at the grip limit, the slowest point 20 m later
        check('overshot', run([(450.0, 28.0), (520.0, 8.0), (620.0, 28.0)], peak=1.3), 'OVERSHOT'),
        # braked 50 m earlier, the same slowest speed
        check('early brake', run([(380.0, 28.0), (450.0, 11.0), (500.0, 10.0), (600.0, 28.0)]), 'EARLY-BRAKE'),
        # the same corner, the throttle held off to 560 m and a slower exit
        check('late throttle', run([(430.0, 28.0), (500.0, 10.0), (560.0, 10.5), (680.0, 28.0)],
                                   throttle=lambda i, t, d, a: 0.0 if 480.0 < d < 560.0 else
                                   (1.0 if a > 0.3 else (0.0 if a < -1.0 else 0.3))), 'LATE-THROTTLE'),
        # 6 m/s slower all the way up to the braking point
        check('slow arrival', run([(200.0, 22.0), (430.0, 22.0), (500.0, 9.0), (600.0, 28.0)]), 'SLOW-ARRIVAL'),
    ]
    print('{} of {} as the table says'.format(sum(results), len(results)))
    return all(results)


if __name__ == '__main__':
    sys.exit(0 if main() else 1)
