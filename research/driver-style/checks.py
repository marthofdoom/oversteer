"""Robustness checks on the profile's shakiest numbers (LFB, flick, consistency, restarts)."""
import os, sys, json, collections
import numpy as np
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')); sys.path.insert(0, REPO)
from oversteer import coach_context as cc, potential, run_analysis, telemetry_store as ts
reader = ts.open_reader(os.path.expanduser('~/.cache/oversteer-style/flatpak.db'))
db = reader.db
ids = [r[0] for r in db.execute("SELECT r.id FROM runs r JOIN sessions s ON s.id=r.session WHERE s.game='acr' AND r.finished=1 AND r.run_class='clean'")]
tot = collections.Counter()
flick_base = []; flick_strict = []
for rid in ids:
    run = reader.run(rid); arr = potential.arrays(run_analysis.stage_trace(reader, run))
    t, d, v, thr, brk, steer, yaw = (arr[k] for k in ('t', 'd', 'speed', 'throttle', 'brake', 'steer', 'yaw_rate'))
    dt = np.r_[np.diff(t), 0.1]
    m = (v > 5) & np.isfinite(thr) & np.isfinite(brk)
    b = m & (brk > 0.1)
    tot['brake'] += dt[b].sum()
    tot['thr10'] += dt[b & (thr > 0.1)].sum()
    tot['thr30'] += dt[b & (thr > 0.3)].sum()
    # sustained: both > 0.2 for >= 3 consecutive rows (0.3 s), not a pedal hand-over
    both = m & (brk > 0.2) & (thr > 0.2)
    run_len = 0; sus = np.zeros_like(both)
    for i, x in enumerate(both):
        run_len = run_len + 1 if x else 0
        if run_len >= 3: sus[i - run_len + 1:i + 1] = True
    tot['sustained'] += dt[sus].sum()
    tot['brake_with_speed_drop'] += dt[b & (np.r_[0, np.diff(v)] < 0)].sum()
    tot['lfb_slowing'] += dt[b & (thr > 0.1) & (np.r_[0, np.diff(v)] < 0)].sum()
    corners = sorted(reader.corners(rid), key=lambda k: k['d'])
    starts = [k['d0'] for k in corners if k.get('d0') is not None]
    def flick_at(i, sign, ystrict=0.15, sstrict=0.05):
        w = (t >= t[i] - 2.0) & (t < t[i])
        return w.any() and np.nanmin(yaw[w] * sign) < -ystrict and np.nanmin(steer[w] * sign) < -sstrict
    # baseline: random points far (>150 m) from any corner start, random sign
    rng = np.random.default_rng(rid)
    for i in rng.integers(30, len(d) - 1, 60):
        if all(abs(d[i] - s0) > 150 for s0 in starts) and v[i] > 10:
            sg = rng.choice([-1, 1]); flick_base.append((flick_at(i, sg), flick_at(i, sg, 0.3, 0.1)))
    for n, k in enumerate(corners):
        if k.get('off') or k.get('d0') is None: continue
        prev = corners[n - 1] if n else None
        if prev is not None and prev['direction'] == -k['direction'] and prev.get('d1') is not None and k['d0'] - prev['d1'] < 80: continue
        i0 = int(np.searchsorted(d, k['d0']))
        flick_strict.append(flick_at(i0, k['direction'], 0.3, 0.1))
print({k: round(v, 1) for k, v in tot.items()})
print('lfb thr>10%', tot['thr10'] / tot['brake'], 'thr>30%', tot['thr30'] / tot['brake'], 'sustained both>20% 0.3s', tot['sustained'] / tot['brake'],
      'lfb while slowing', tot['lfb_slowing'] / tot['brake_with_speed_drop'])
fb = np.array(flick_base); print('flick baseline loose, strict', fb.mean(axis=0), len(flick_base), 'strict on isolated corners', np.mean(flick_strict), len(flick_strict))
# consistency within a sitting
rows = db.execute("SELECT r.id, r.session, r.stage, c.name, r.result_time, r.run_class, r.started FROM runs r JOIN sessions s ON s.id=r.session JOIN cars c ON c.id=s.car WHERE s.game='acr' AND r.finished=1 AND r.run_class IN ('clean') ORDER BY r.started").fetchall()
g = collections.defaultdict(list)
for r in rows: g[(r[1], r[2], r[3])].append(r[4])
for k, v in g.items():
    if len(v) > 1: print('sitting', k, [round(x, 1) for x in v], 'range', round(max(v) - min(v), 1), 'sd', round(float(np.std(v)), 2))
# restarts: where along the stage
rs = db.execute("SELECT r.distance FROM runs r JOIN sessions s ON s.id=r.session WHERE s.game='acr' AND r.run_class='restart'").fetchall()
ds = np.array([x[0] or 0 for x in rs]); print('restarts', len(ds), 'median m', np.median(ds), 'share <300 m', np.mean(ds < 300), 'share >1 km', np.mean(ds > 1000))
