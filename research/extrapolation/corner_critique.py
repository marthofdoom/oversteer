#!/usr/bin/env python3
"""Corner critique: the wrong gear and losing the rear, and what each
costs, per corner.

For every run x section (lapsim.py's sections, cut on the road's
geometry) of one stage, clean passes only (no stop, nothing over 25 % of
the best time through it):
- apex gear (the gear at the slowest point) against the gear the
  section's fastest pass used; exit revs: the rpm where the throttle is
  next full after the apex, below the engine's band (the torque curve's
  90 %-of-peak edge from the car data, else the learnt band) or not;
- losing the rear: seconds of counter-steer (steering against the yaw,
  |yaw| > 0.15 rad/s) and the peak body slip angle over the section's
  median;
- the apex speed below the fastest pass's (km/h), as a control: a lower
  gear mostly follows from a slower apex, and without the control the
  gear term would take the apex's cost;
- excess time over the section's fastest pass.
A within-section regression (each section's mean removed) of the excess
time on those terms gives seconds per unit for each, with a bootstrap
interval over sections; each pass's cost per term follows, and the PB's
corners are listed with them.

    python3 research/extrapolation/corner_critique.py [--stage ...] [--car ...]
"""
import argparse
import json
import os
import warnings

import numpy as np

import common
import lapsim
import powertrain

warnings.simplefilter('ignore')


def band_low(car):
    data = powertrain.car_data(car)
    if not data:
        return None
    rpm, nm = (np.array(x) for x in zip(*data['torque_curve']))
    return float(rpm[np.argmax(nm >= 0.9 * nm.max())])


def passes(stage, car, d0, d1):
    allr = common.all_runs('acr')
    stage_runs = [r for r in allr if r[3] == stage]
    grid = np.arange(d0, d1, lapsim.DS)
    ks = [common.unwrap_resample(c['lap_distance'][i], lapsim.curvature(c, i), grid) for _n, c, i, *_ in stage_runs]
    k = common.smooth(np.nan_to_num(np.nanmedian(ks, 0)), 5)
    secs = lapsim.sections(grid, k)
    low = band_low(car)
    out = []
    for rn, (name, cap, idx, _tr, c, _g) in enumerate(stage_runs):
        if c != car:
            continue
        d, t, v = cap['lap_distance'][idx], cap['t'][idx], cap['speed'][idx]
        keep = np.r_[True, np.diff(d) > 0.01]
        for si, (a, b) in enumerate(secs):
            da, db = grid[a], grid[b]
            if d[0] > da or d[-1] < db:
                continue
            ta, tb = np.interp([da, db], d[keep], t[keep])
            m = (d >= da) & (d <= db)
            ii = np.flatnonzero(m)
            if len(ii) < 10 or v[ii].min() < 3:
                continue
            ap_i = a + int(np.argmax(np.abs(k[a:b + 1])))
            near = ii[np.abs(d[ii] - grid[ap_i]) < 40]
            if not len(near):
                continue
            vmin = near[np.argmin(v[near])]
            after = ii[ii > vmin]
            full = after[np.nan_to_num(cap['throttle'][idx][after]) > 0.95]
            exit_rpm = float(cap['rpm'][idx][full[0]]) if len(full) else np.nan
            yaw = cap['yaw_rate'][idx][ii]
            st = cap['steer'][idx][ii]
            dt = np.clip(np.diff(t[ii], prepend=t[ii][0]), 0, 0.05)
            cs = float(np.sum(dt[(np.abs(yaw) > 0.15) & (np.abs(st) > 0.03) & (np.sign(st) != np.sign(yaw))]))
            vel = cap['vel'][idx][ii]
            beta = np.degrees(np.abs(np.arctan2(vel[:, 1], np.maximum(vel[:, 0], 1.0))))
            out.append({'run': rn, 'capture': name, 'sec': si, 'd0': round(da), 'd1': round(db), 't': tb - ta,
                        'apex_gear': int(cap['gear'][idx][vmin]), 'exit_rpm': exit_rpm,
                        'low_exit': bool(np.isfinite(exit_rpm) and low and exit_rpm < low),
                        'cs_s': cs, 'beta_peak': float(np.percentile(beta, 98)),
                        'apex_kmh': float(v[vmin] * 3.6)})
    return out, secs, grid, low


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stage', default='Wales Afon Bidno')
    ap.add_argument('--car', default='Hyundai i20N Rally2')
    ap.add_argument('--d0', type=float, default=238.5)
    ap.add_argument('--d1', type=float, default=5277.0)
    args = ap.parse_args()
    ps, secs, grid, low = passes(args.stage, args.car, args.d0, args.d1)
    by = {}
    for p in ps:
        by.setdefault(p['sec'], []).append(p)
    X, Y, S, rows = [], [], [], []
    for si, group in by.items():
        best = min(group, key=lambda p: p['t'])
        med_beta = np.median([p['beta_peak'] for p in group])
        for p in group:
            p['excess'] = p['t'] - best['t']
            if p['excess'] > 0.25 * best['t'] or len(group) < 3:
                p['dropped'] = True
                continue
            p['gear_longer'] = int(p['apex_gear'] > best['apex_gear'])
            p['gear_shorter'] = int(p['apex_gear'] < best['apex_gear'])
            p['beta_over'] = max(0.0, p['beta_peak'] - med_beta)
            rows.append(p)
            p['apex_deficit'] = max(0.0, best['apex_kmh'] - p['apex_kmh'])
            X.append([p['gear_longer'], p['gear_shorter'], int(p['low_exit']), p['cs_s'], p['beta_over'],
                      p['apex_deficit']])
            Y.append(p['excess'])
            S.append(si)
    X, Y, S = np.array(X, float), np.array(Y), np.array(S)
    names = ['gear_longer_than_best (s)', 'gear_shorter_than_best (s)', 'exit_below_band (s)',
             'counter_steer (s per s)', 'body_slip_over_median (s per deg)',
             'apex_speed_below_best (s per km/h, the control)']

    def fit(idx):
        Xd, Yd = X[idx].copy(), Y[idx].copy()
        for s in np.unique(S[idx]):
            m = S[idx] == s
            Xd[m] -= Xd[m].mean(0)
            Yd[m] -= Yd[m].mean()
        return np.linalg.lstsq(Xd, Yd, rcond=None)[0]

    coef = fit(np.arange(len(Y)))
    rng = np.random.default_rng(0)
    secs_u = np.unique(S)
    boots = []
    for _ in range(300):
        pick = rng.choice(secs_u, len(secs_u))
        idx = np.concatenate([np.flatnonzero(S == s) for s in pick])
        boots.append(fit(idx))
    boots = np.array(boots)
    out = {'stage': args.stage, 'car': args.car, 'passes': len(Y), 'sections': len(secs_u),
           'band_low_rpm': low,
           'prevalence': {'gear_longer': round(float(X[:, 0].mean()), 3), 'gear_shorter': round(float(X[:, 1].mean()), 3),
                          'exit_below_band': round(float(X[:, 2].mean()), 3),
                          'counter_steer_s_median': round(float(np.median(X[:, 3])), 2)},
           'cost': {n: {'coef': round(float(c), 3), 'ci90': [round(float(x), 3) for x in np.percentile(boots[:, j], [5, 95])]}
                    for j, (n, c) in enumerate(zip(names, coef))}}
    # the PB's corners: what each term cost there
    runs_full = {}
    for p in rows:
        runs_full.setdefault(p['run'], []).append(p)
    nsec = len(secs_u)
    complete = {r: v for r, v in runs_full.items() if len(v) >= nsec - 1}
    if complete:
        pb = min(complete, key=lambda r: sum(p['t'] for p in complete[r]))
        calls = []
        for p in complete[pb]:
            x = np.array([p['gear_longer'], p['gear_shorter'], int(p['low_exit']), p['cs_s'], p['beta_over'], 0.0])
            cost = np.maximum(coef, 0) * x
            j = int(np.argmax(cost))
            if cost[j] >= 0.1:
                what = ['a gear longer than your fastest pass', 'a gear shorter than your fastest pass',
                        'the revs under the band on the exit (%.0f rpm)' % p['exit_rpm'],
                        '%.1f s of counter-steer (the rear stepped out)' % p['cs_s'],
                        '%.0f degrees more slide than usual' % p['beta_over']][j]
                calls.append({'d0': p['d0'], 'd1': p['d1'], 'cost_s': round(float(cost[j]), 2), 'what': what,
                              'excess_s': round(p['excess'], 2)})
        out['pb_corner_calls'] = sorted(calls, key=lambda c: -c['cost_s'])
    name = 'corner_critique_%s_%s.json' % (args.stage.split()[-1].lower(), args.car.split()[1].lower())
    with open(os.path.join(common.OUT, name), 'w') as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out, indent=1))


if __name__ == '__main__':
    main()
