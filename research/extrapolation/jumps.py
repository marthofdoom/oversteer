#!/usr/bin/env python3
"""Jumps, airtime and landings.

Ground truth on ACR: the tyre loads. The car is in the air when all four
loads are under 50 N (moving over 5 m/s). Against it, the detectors any
game can run:
- A, vertical acceleration: free fall reads a_z near -g in a kinematic
  car-frame acceleration (ACR, Forza, EA WRC); airborne when
  a_z < -6.5 m/s^2 for 0.1 s or more;
- B, suspension: every wheel at full droop (within 3 mm of that wheel's
  1st percentile in the run) for 0.1 s or more (DiRT, WRCG, Forza, AC);
- A and B together.
Scored per event (an airborne stretch of 0.1 s or more): recall against
four wheels off, precision against three or more wheels off (a wheel
skimming the ground is still a flight to the driver), and the airtime error of the matched events. Landings: the
peak total load over m g in the half second after touching down, and how
well the generic peak a_z tracks it.

WRC Generations has no loads; there the suspension detector is compared
with free fall seen in the positions (height's second derivative under
-6.5 m/s^2).

A long 'airborne' stretch whose a_z is not near -g is a car on its roof
or side: a crash marker the offs prototype uses.

    python3 research/extrapolation/jumps.py
"""
import json
import os
import warnings

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

import common  # noqa: E402

warnings.simplefilter('ignore')
MIN_AIR = 0.1          # s


def events(mask, t, min_s=MIN_AIR):
    out = []
    idx = np.flatnonzero(mask)
    if not len(idx):
        return out
    for g in np.split(idx, np.flatnonzero(np.diff(idx) > 2) + 1):
        if t[g[-1]] - t[g[0]] >= min_s:
            out.append((g[0], g[-1]))
    return out


def overlap(a, b):
    return a[0] <= b[1] and b[0] <= a[1]


def score(truth, found, t):
    hit = [f for f in found if any(overlap(f, x) for x in truth)]
    rec = [x for x in truth if any(overlap(f, x) for f in found)]
    errs = []
    for x in truth:
        ms = [f for f in found if overlap(f, x)]
        if ms:
            errs.append((t[max(m[1] for m in ms)] - t[min(m[0] for m in ms)]) - (t[x[1]] - t[x[0]]))
    return len(rec), len(hit), errs


def acr():
    tot = {k: [0, 0, 0, 0, []] for k in ('A_accel', 'B_susp', 'A_and_B')}
    n_truth = 0
    landing = []
    rollovers = []
    air_times = []
    for name, cap, idx, track, car, _g in common.all_runs('acr'):
        t, v = cap['t'][idx], cap['speed'][idx]
        fz = cap['x_wheel_load'][idx]
        az = cap['accel'][idx][:, 2]
        su = cap['susp'][idx]
        mv = v > 5
        truth_mask = (fz < 50).all(1) & mv
        truth_all = events(truth_mask, t)
        # a 'flight' whose a_z is not free fall is a car on its roof or side
        truth = []
        for e in truth_all:
            if np.median(az[e[0]:e[1] + 1]) < -6.0:
                truth.append(e)
            else:
                rollovers.append({'capture': name, 'track': track, 'd': round(float(cap['lap_distance'][idx][e[0]])),
                                  's': round(float(t[e[1]] - t[e[0]]), 2),
                                  'a_z': round(float(np.median(az[e[0]:e[1] + 1])), 1)})
        n_truth += len(truth)
        loose = events(((fz < 50).sum(1) >= 3) & mv, t, 0.05)
        droop = np.nanpercentile(su[mv], 1, axis=0)
        a_mask = (common.smooth(az, 3) < -7.5) & mv
        b_mask = (np.abs(su - droop) < 0.003).all(1) & mv
        for key, mask in (('A_accel', a_mask), ('B_susp', b_mask), ('A_and_B', a_mask & b_mask)):
            found = events(mask, t)
            r, _h, errs = score(truth, found, t)
            h = sum(1 for f in found if any(overlap(f, x) for x in loose))
            tot[key][0] += r
            tot[key][1] += h
            tot[key][2] += len(found)
            tot[key][4] += errs
        m = np.nanmedian(fz[mv].sum(1)) / common.G
        for e in truth:
            air_times.append(t[e[1]] - t[e[0]])
            k1 = min(len(t) - 1, e[1] + 30)
            land = fz[e[1]:k1 + 1].sum(1).max() / (m * common.G)
            landing.append((land, az[e[1]:k1 + 1].max() / common.G + 1.0, t[e[1]] - t[e[0]]))
    out = {'airborne_events_truth': n_truth,
           'airtime_s': {'median': round(float(np.median(air_times)), 2), 'max': round(float(np.max(air_times)), 2)},
           'detectors': {}}
    for key, (r, h, nf, _x, errs) in tot.items():
        out['detectors'][key] = {'recall': round(r / max(1, n_truth), 3), 'precision': round(h / max(1, nf), 3),
                                 'found': nf, 'airtime_err_median_s': round(float(np.median(errs)), 3) if errs else None,
                                 'airtime_err_p90_abs_s': round(float(np.percentile(np.abs(errs), 90)), 3) if errs else None}
    landing = np.array(landing)
    out['landing'] = {'events': len(landing), 'peak_load_g_median': round(float(np.median(landing[:, 0])), 2),
                      'peak_load_g_max': round(float(landing[:, 0].max()), 2),
                      'r_peak_az_vs_peak_load': round(float(np.corrcoef(landing[:, 0], landing[:, 1])[0, 1]), 3),
                      'r_airtime_vs_peak_load': round(float(np.corrcoef(landing[:, 2], landing[:, 0])[0, 1]), 3)}
    out['not_free_fall_stretches (roof/side)'] = rollovers
    return out, landing


def wrcg():
    res = []
    for name, cap, idx, _tr, _car, _g in common.all_runs('wrcg'):
        t, v = cap['t'][idx], cap['speed'][idx]
        z = cap['pos'][idx][:, 2]
        su = cap['susp'][idx]
        mv = v > 5
        zs = common.smooth(z, 5)
        zdd = np.gradient(np.gradient(zs, t), t)
        f_mask = (common.smooth(zdd, 5) < -6.5) & mv
        droop = np.nanpercentile(su[mv], 1, axis=0)
        b_mask = (np.abs(su - droop) < 0.003 * (np.nanpercentile(su[mv], 99) - droop.min()) / 0.15).all(1) & mv
        ff, bb = events(f_mask, t), events(b_mask, t)
        agree = sum(1 for f in ff if any(overlap(f, b) for b in bb))
        res.append({'capture': name, 'freefall_from_positions': len(ff), 'susp_droop': len(bb),
                    'freefall_with_susp_droop': agree,
                    'susp_range_raw': [round(float(x), 5) for x in (np.nanmin(su), np.nanmax(su))]})
    return res


def main():
    out, landing = acr()
    out['wrcg'] = wrcg()
    fig, ax = plt.subplots(1, 2, figsize=(11, 4))
    ax[0].scatter(landing[:, 2], landing[:, 0], s=8)
    ax[0].set_xlabel('airtime s (loads)')
    ax[0].set_ylabel('peak landing load / m g')
    ax[0].set_title('ACR: airtime against the landing (tyre loads)', fontsize=9)
    ax[1].scatter(landing[:, 0], landing[:, 1], s=8)
    ax[1].set_xlabel('peak landing load / m g (truth)')
    ax[1].set_ylabel('1 + peak a_z / g (generic)')
    ax[1].set_title('Landing severity: generic a_z against the loads', fontsize=9)
    common.savefig(fig, 'jumps_acr.png')
    with open(os.path.join(common.OUT, 'jumps.json'), 'w') as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out, indent=1))


if __name__ == '__main__':
    main()
