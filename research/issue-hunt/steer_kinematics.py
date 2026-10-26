"""Issue hunt 2026-10, ClickUp 86e3m0j5w: the steer channel against what the car did, per game and car.

    python3 research/issue-hunt/steer_kinematics.py OUT.json [--only SUBSTR]

Reads the captures (read-only) at packet rate. For every game and car: the yaw rate (the game's, else the heading
rate of the plan positions), the path curvature k = yaw / speed and, through the kinematic bicycle model
(road-wheel angle = wheelbase x k in low-slip driving), what the steer channel's scale must be. Reported:
- sign: corr(steer, k) (positive = steer positive left as the Sample promises),
- the yaw-rate channel against the heading rate from positions (sign, r), where both exist,
- lag: the shift of k against steer that correlates best (s, positive = the car follows the steer),
- scale: the slope steer per unit curvature in low-slip rows (|a_lat| < 3 m/s^2, 10..40 m/s) and the road-wheel
  lock it implies at a 2.5 m wheelbase (steer 1.0 -> that many degrees of road wheel),
- linearity: steer / k by |steer| band, offset: the median steer driving straight,
- use: the share of rows at |steer| > 0.9 (at lock) and the 99th percentile of |steer|,
- counter-steer: the share of cornering rows (|k| > 0.01) where steer and yaw disagree in sign, at zero lag and at
  the best lag (what drive_log._corner counts, at the trace's 10 Hz).
"""

import glob
import json
import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from oversteer.telemetry_capture import read_capture  # noqa: E402
from oversteer.telemetry_formats import decode_sample, plan_xy  # noqa: E402

FOLDERS = ('~/.local/share/oversteer/captures', '~/.var/app/io.github.berarma.Oversteer/data/oversteer/captures')
WHEELBASE = 2.5


def series(path):
    _meta, records = read_capture(path)
    out = {}
    for t, _a, data in records:
        s = decode_sample(data)
        if s is None or s.car is None or s.steer is None or s.speed is None:
            continue
        key = (s.game, s.car)
        rows = out.setdefault(key, [])
        p = plan_xy(s.game, s.pos) if s.pos is not None else (math.nan, math.nan)
        alat = s.accel[1] if s.accel is not None and len(s.accel) > 1 else math.nan
        rows.append((t, s.steer, s.speed, s.yaw_rate if s.yaw_rate is not None else math.nan, p[0], p[1], alat,
                     s.stage_time if s.stage_time is not None else math.nan))
    return {k: np.array(v, dtype=float) for k, v in out.items()}


def heading_rate(a):
    """Heading rate of the plan path (rad/s, left positive) from positions +-3 packets apart."""
    t, x, y = a[:, 0], a[:, 4], a[:, 5]
    n = len(t)
    h = np.full(n, np.nan)
    dx, dy = x[6:] - x[:-6], y[6:] - y[:-6]
    ok = np.hypot(dx, dy) > 0.5
    head = np.where(ok, np.arctan2(dy, dx), np.nan)
    hr = np.full(n, np.nan)
    # d heading / dt between consecutive headings
    dh = np.diff(head)
    dh = (dh + np.pi) % (2 * np.pi) - np.pi
    dt = t[4:-3] - t[3:-4]
    with np.errstate(invalid='ignore', divide='ignore'):
        hr[4:-3] = np.where(dt > 0.004, dh / dt, np.nan)
    return hr


def smooth(x, n):
    if n <= 1:
        return x
    k = np.ones(n) / n
    good = np.isfinite(x)
    num = np.convolve(np.where(good, x, 0.0), k, 'same')
    den = np.convolve(good.astype(float), k, 'same')
    with np.errstate(invalid='ignore', divide='ignore'):
        return np.where(den > 0.5, num / den, np.nan)


def corr(a, b):
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 50:
        return None
    return float(np.corrcoef(a[m], b[m])[0, 1])


def analyse(a):
    t, steer, v, yaw, alat = a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 6]
    hr = smooth(heading_rate(a), 5)
    have_yaw = np.isfinite(yaw).mean() > 0.5
    rate = yaw if have_yaw else hr
    dt = np.median(np.diff(t)) if len(t) > 2 else 1 / 60
    moving = (v > 10) & (v < 40)
    with np.errstate(invalid='ignore', divide='ignore'):
        k = np.where(v > 3, rate / v, np.nan)
        k_pos = np.where(v > 3, hr / v, np.nan)
    out = {'rows': int(len(t)), 'hz': round(1 / dt, 1) if dt > 0 else None, 'yaw_channel': bool(have_yaw)}
    out['yaw_vs_heading_r'] = corr(np.where(moving, yaw, np.nan), np.where(moving, hr, np.nan)) if have_yaw else None
    out['steer_vs_k_r'] = corr(np.where(moving, steer, np.nan), k)
    out['steer_vs_kpos_r'] = corr(np.where(moving, steer, np.nan), k_pos)
    # lag: k shifted against steer
    best = (None, -2)
    maxlag = int(round(0.6 / dt)) if dt > 0 else 36
    for lag in range(-maxlag, maxlag + 1, max(1, int(round(0.0333 / dt)))):
        if lag >= 0:
            s_, k_ = steer[:len(steer) - lag], k[lag:]
            m_ = moving[:len(moving) - lag]
        else:
            s_, k_ = steer[-lag:], k[:lag]
            m_ = moving[-lag:]
        r = corr(np.where(m_, s_, np.nan), k_)
        if r is not None and r > best[1]:
            best = (lag, r)
    lag = best[0] or 0
    out['lag_s'] = round(lag * dt, 3) if best[0] is not None else None
    out['lag_r'] = round(best[1], 3) if best[0] is not None else None
    if lag >= 0:
        S, K, A, V = steer[:len(steer) - lag], k[lag:], alat[:len(alat) - lag], v[:len(v) - lag]
    else:
        S, K, A, V = steer[-lag:], k[:lag], alat[-lag:], v[-lag:]
    low = (V > 10) & (V < 40) & np.isfinite(K) & (np.abs(K) < 0.05)
    if np.isfinite(A).mean() > 0.5:
        low &= np.abs(A) < 3.0
    if low.sum() > 200:
        slope, icpt = np.polyfit(K[low], S[low], 1)
        out['slope_steer_per_k_m'] = round(float(slope), 2)
        out['offset_fit'] = round(float(icpt), 4)
        out['implied_lock_deg_at_2.5m'] = round(math.degrees(WHEELBASE / slope), 1) if slope > 0 else None
        lin = []
        for lo, hi in ((0.02, 0.05), (0.05, 0.1), (0.1, 0.2), (0.2, 0.4), (0.4, 1.01)):
            m = low & (np.abs(S) >= lo) & (np.abs(S) < hi) & (np.abs(K) > 1e-4)
            if m.sum() > 30:
                lin.append({'band': [lo, hi], 'n': int(m.sum()), 'median_steer_over_k': round(float(np.median(S[m] / K[m])), 2)})
        out['linearity'] = lin
    slow = (V > 4) & (V < 10) & np.isfinite(K) & (np.abs(K) > 0.01) & (np.abs(S) > 0.05)
    if np.isfinite(A).mean() > 0.5:
        slow &= np.abs(A) < 3.0
    if slow.sum() > 50:
        ratio = np.median(S[slow] / K[slow])
        out['slow_steer_over_k_m'] = round(float(ratio), 2)
        out['slow_implied_lock_deg'] = round(math.degrees(WHEELBASE / ratio), 1) if ratio > 0 else None
        out['slow_n'] = int(slow.sum())
    straight = moving & np.isfinite(k) & (np.abs(k) < 0.0007) & (v > 20)
    out['offset_straight_median'] = round(float(np.median(steer[straight])), 4) if straight.sum() > 50 else None
    out['abs_steer_p50'] = round(float(np.percentile(np.abs(steer[moving]), 50)), 3) if moving.sum() else None
    out['abs_steer_p99'] = round(float(np.percentile(np.abs(steer[moving]), 99)), 3) if moving.sum() else None
    out['abs_steer_max'] = round(float(np.max(np.abs(steer))), 3)
    out['share_at_lock'] = round(float((np.abs(steer[v > 3]) > 0.9).mean()), 4) if (v > 3).sum() else None
    corner = moving & np.isfinite(k) & (np.abs(k) > 0.01) & (np.abs(steer) > 0.02)
    out['counter_share_lag0'] = round(float((steer[corner] * k[corner] < 0).mean()), 3) if corner.sum() else None
    cl = (V > 10) & (V < 40) & np.isfinite(K) & (np.abs(K) > 0.01) & (np.abs(S) > 0.02)
    out['counter_share_bestlag'] = round(float((S[cl] * K[cl] < 0).mean()), 3) if cl.sum() else None
    # counter-steer as the trace sees it: 10 Hz, yaw smoothed like find_corners (CORNER_SMOOTH s)
    return out


def main(out_path, only=None):
    files = sorted((p for d in FOLDERS for p in glob.glob(os.path.join(os.path.expanduser(d), '*.ovcap.gz'))),
                   key=os.path.basename)
    results = []
    for path in files:
        name = os.path.basename(path)
        if only and only not in name:
            continue
        for (game, car), a in series(path).items():
            if len(a) < 600:
                continue
            r = analyse(a)
            r.update(capture=name, game=game, car=car)
            results.append(r)
            print(json.dumps(r), flush=True)
    with open(out_path, 'w') as f:
        json.dump(results, f, indent=1)


if __name__ == '__main__':
    args = sys.argv[1:]
    only = None
    if '--only' in args:
        i = args.index('--only')
        only = args[i + 1]
        del args[i:i + 2]
    main(os.path.expanduser(args[0]), only)
