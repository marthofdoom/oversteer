#!/usr/bin/env python3
"""ACR's four tyres' side force over m a_y against the grip mu as played (GripScale), m the moving mass (MassLearner on
the tyres' Fx x v, J 0: Fx is past the engine), over every ACR capture with tyre forces; the evidence for Newton's front force
(ffb_forza.front_lateral) having no grip factor. Read only. --since/--until YYYYMMDD limit the captures by the date in their name (ACR's tyres
changed 20261119: compare `--until 20261105` with `--since 20261119`); the fit is printed for all of them and per car.
Fits: until 20261105 ratio = 0.690 + 0.227 mu (the fit the engine used to carry, a loose-surface deficit); since 20261119 1.048 + 0.025 mu,
flat in mu, so Newton's front force (ffb_forza.front_lateral) takes no grip factor. Log: ~/.cache/ffb-lateral-fit/latest.log"""
import os as _os, sys as _sys
_log = _os.path.expanduser("~/.cache/ffb-lateral-fit"); _os.makedirs(_log, exist_ok=True)
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__))); _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
class _Tee:
    def __init__(self, *f): self.f = f
    def write(self, s): [x.write(s) for x in self.f]
    def flush(self): [x.flush() for x in self.f]
_sys.stdout = _Tee(_sys.stdout, open(_os.path.join(_log, "latest.log"), "w"))
import argparse, sys, copy, glob, os, numpy as np
import ffb_tools as ft, ffb_benchmark as fb, ffb_parity as fp
from oversteer import ffb_forza as ff
ap = argparse.ArgumentParser(description=__doc__.split('Log:')[0])
ap.add_argument('--since', metavar='YYYYMMDD', help='captures dated from this day on')
ap.add_argument('--until', metavar='YYYYMMDD', help='captures dated up to and including this day')
args = ap.parse_args()
names = sorted(os.path.basename(p)[:15] for p in glob.glob(ft.captures_dir() + '/2026*.ovcap.gz'))
names = [n for n in names if (not args.since or n[:8] >= args.since) and (not args.until or n[:8] <= args.until)]
print('captures %s to %s: %d' % (args.since or 'start', args.until or 'end', len(names)))
allp = []; bycar = {}
for name in names:
    path = ft.capture_path(name)
    if os.path.getsize(path) < 500000: continue
    try:
        if fp.game_of(path) != 'acr': continue
        items = list(fb._walk(path)[1])
    except Exception as e:
        continue
    if not any(s.tyre_fy for _t, s in items[:5000]): continue
    ml = ff.MassLearner(); ml.J = 0.0
    for t, s in items:
        if s.tyre_fx is None or s.speed is None or s.tyre_slip_angle is None: continue
        s2 = copy.copy(s); s2.power = sum(s.tyre_fx[:4]) * s.speed; s2.slip_ratio = (0,)*4; s2.susp_norm = (0.5,)*4; s2.slip_angle = s.tyre_slip_angle
        ml.add(t, s2)
    if not ml.known: print(name, 'no mass'); continue
    M = ml.mass; g = ff.GripScale(); pts = []
    for t, s in items:
        if not (s.accel and s.speed): continue
        g.feed(t, s.accel[1], s.speed)
        mu = g.mu()
        if mu is None or not s.tyre_fy or s.speed < 8 or abs(s.accel[1]) < 3: continue
        pts.append((mu, sum(s.tyre_fy[:4]) / (M * s.accel[1])))
    p = np.array(pts)
    if len(p) < 2000 or np.percentile(p[:, 0], 90) > 2.5:   # (too short, or a crash: mu past 2.5 g)
        continue
    print('%s mass %.0f n %6d mu p10-p90 %.2f-%.2f ratio median %.3f' % (name, M, len(p), *np.percentile(p[:, 0], [10, 90]), np.median(p[:, 1])))
    allp.append(p)
    bycar.setdefault(next((s.car for _t, s in items[:5000] if s.car), '?'), []).append(p)
if not allp:
    sys.exit("no ACR capture with tyre forces and a learnt mass")
def fit(p, label, verbose=True):
    edges = np.arange(0.7, 1.8, 0.1); mids, meds, ws = [], [], []
    for lo in edges:
        k = (p[:, 0] >= lo) & (p[:, 0] < lo + 0.1)
        if k.sum() < 1000: continue
        mids.append(lo + 0.05); meds.append(np.median(p[k, 1])); ws.append(k.sum())
        if verbose: print('  mu %.1f-%.1f n %6d ratio %.3f' % (lo, lo + 0.1, k.sum(), meds[-1]))
    if len(mids) < 2:
        print('%s: too few mu bands for a fit' % label); return
    b, a = np.polyfit(mids, meds, 1, w=np.sqrt(ws)); print('fit %s: ratio = %.3f + %.3f mu  (n %d)' % (label, a, b, len(p)))
fit(np.vstack(allp), 'all')
if len(bycar) > 1:
    for car, ps in sorted(bycar.items()):
        fit(np.vstack(ps), 'car %s' % car, verbose=False)
