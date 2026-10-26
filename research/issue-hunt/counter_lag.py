"""Issue hunt 2026-10: how much of the stored counter-steer is the steer leading the yaw (not a slide).

    python3 research/issue-hunt/counter_lag.py HUNT.db

For every ACR corner of the replayed store: drive_log._corner's counter-steer share as stored, and again with the yaw
taken LAG rows (10 Hz) later than the steer, as the car's response follows the wheel (steer_kinematics.py measured
0.15-0.31 s on marth's ACR captures). Also the coach's thresholds: corners over COUNTER_DELTA (0.15) more than a
reference and the per-section counter-steer seconds (coach._potential_rows).
"""
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from oversteer import drive_log  # noqa: E402
from oversteer.telemetry_store import open_reader  # noqa: E402

T = drive_log.T


def share(trace, yaw, i, j, lag):
    n = len(trace)
    pairs = []
    for k in range(i, j + 1):
        s = trace[k][T['steer']]
        y = yaw[min(n - 1, k + lag)]
        if not math.isnan(s):
            pairs.append((s, y))
    against = [1 for s, y in pairs if abs(s) > 0.02]
    if not against:
        return None, 0.0
    c = sum(1 for s, y in pairs if abs(s) > 0.02 and s * y < 0)
    return c / len(against), c * 0.1


def main(db):
    r = open_reader(db)
    runs = r.db.execute("select r.id, r.stage from runs r where r.stage like 'acr:%' and r.finished = 1").fetchall()
    out = {0: [], 2: [], 3: []}
    secs = {0: 0.0, 2: 0.0, 3: 0.0}
    big = {0: 0, 2: 0, 3: 0}
    n = 0
    for run, stage in runs:
        trace = r.trace(run)
        if not trace:
            continue
        corners_ = drive_log.find_corners(trace, 'acr')
        raw = drive_log._yaw_rates(trace, 'acr')
        half = max(1, int(drive_log.CORNER_SMOOTH / drive_log.TRACE_EVERY / 2))
        smooth = []
        for i in range(len(raw)):
            w = [v for v in raw[max(0, i - half):i + half + 1] if not math.isnan(v)]
            smooth.append(sum(w) / len(w) if w else 0.0)
        for c in corners_:
            if (c.get('heading_change') or 0) >= 90:
                continue
            n += 1
            for lag in out:
                f, s = share(trace, smooth, c['_i0'], c['_i1'], lag)
                if f is not None:
                    out[lag].append(f)
                    secs[lag] += s
                    big[lag] += f >= 0.2
    for lag, v in out.items():
        v.sort()
        print('lag %.1f s: corners %d, mean share %.3f, median %.3f, p90 %.3f, corners >=20%% %d, counter-steer s total %.0f' % (
            lag / 10, len(v), sum(v) / len(v), v[len(v) // 2], v[int(len(v) * 0.9)], big[lag], secs[lag]))


if __name__ == '__main__':
    main(os.path.expanduser(sys.argv[1]))
