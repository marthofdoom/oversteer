#!/usr/bin/env python3
"""Braking zones: onset, peak, release shape, trail braking, lock-ups,
and their spread across runs.

Per run, a braking zone is the brake over 10 % for 0.3 s or more above
8 m/s. Each zone gets:
- onset: distance along the stage and speed where the brake passes 10 %;
- peak deceleration (g): the car-frame acceleration where sent (ACR),
  else the speed's rate on the game's clock; ACR also from the tyre forces;
- build: seconds from onset to 80 % of the zone's peak pedal;
- release: seconds from the peak pedal to under 10 %, and the trail share:
  the part of the release spent turning (|yaw rate| > 0.15 rad/s);
- lock-up: a wheel turning slower than 70 % of the road speed for 0.1 s
  (ACR: wheel spin times the free-rolling radius; WRCG and the others:
  the wheel speeds they send).
Across runs of one stage and car, zones are matched by onset distance
(within 40 m) and the inter-quartile spread of the onset is the braking-point
consistency.

Checks: ACR's own wheel-slip figure against the lock-up flag; the onset
spread across runs (a braking point that is not repeatable is not
coachable); the generic peak deceleration against the forces.

    python3 research/extrapolation/braking.py
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


def zones(cap, idx, game, r=0.327):
    t, v = cap['t'][idx], cap['speed'][idx]
    d = cap['lap_distance'][idx]
    br = np.nan_to_num(cap['brake'][idx])
    yaw = cap['yaw_rate'][idx]
    if not np.isfinite(yaw).any():
        # WRCG: the heading rate from the positions
        p = cap['pos'][idx]
        hd = np.unwrap(np.arctan2(np.gradient(common.smooth(p[:, 1], 9)), np.gradient(common.smooth(p[:, 0], 9))))
        yaw = np.gradient(common.smooth(hd, 9), t)
    gt = cap['game_time'][idx]
    if game == 'acr':
        # ACR packets carry no game clock and arrive with jitter: d(speed)/dt on the receive clock is noisy
        # (r 0.77 with the forces), the car-frame acceleration is not (r 0.98)
        dv = -common.smooth(np.clip(cap['accel'][idx][:, 0], -40, 40), 5) / common.G
    else:
        clock = gt if np.isfinite(gt).all() and (np.diff(gt) > 0).mean() > 0.95 else t
        dv = -np.gradient(common.smooth(v, 9), clock + np.arange(len(clock)) * 1e-7) / common.G
    if game == 'acr':
        ws = np.abs(cap['wheel_rot'][idx]) * r
        fx = cap['x_fx'][idx].sum(1)
        m = np.nanmedian(cap['x_wheel_load'][idx].sum(1)) / common.G
        dec_f = -common.smooth(fx, 5) / (m * common.G)
        wslip = cap['x_wheel_slip'][idx]
    else:
        ws = np.abs(cap['wheel_speed'][idx])
        dec_f = None
        wslip = None
    on = (br > 0.1) & (v > 8)
    out = []
    idxs = np.flatnonzero(on)
    if not len(idxs):
        return out
    for g in np.split(idxs, np.flatnonzero(np.diff(idxs) > 3) + 1):
        if t[g[-1]] - t[g[0]] < 0.3:
            continue
        i0, i1 = g[0], g[-1]
        pk = i0 + int(np.argmax(br[i0:i1 + 1]))
        build = next((k for k in range(i0, i1 + 1) if br[k] >= 0.8 * br[pk]), pk)
        rel = slice(pk, i1 + 1)
        lock_mask = (ws[i0:i1 + 1] < 0.7 * v[i0:i1 + 1, None]).any(1)
        lock = False
        run_len = 0
        for x in lock_mask:
            run_len = run_len + 1 if x else 0
            if run_len >= 6:
                lock = True
                break
        z = {'d': float(d[i0]), 'v0': float(v[i0]), 'v_min': float(v[i0:i1 + 1].min()),
             'peak_pedal': float(br[pk]), 'peak_dec_g': robust_peak(dv[i0:i1 + 1]),
             'build_s': float(t[build] - t[i0]), 'release_s': float(t[i1] - t[pk]),
             'trail_share': float(np.mean(np.abs(yaw[rel]) > 0.3)) if i1 > pk else 0.0,
             'brake_at_slowest': bool(br[i0 + int(np.argmin(v[i0:min(len(v), i1 + 60)]))] > 0.1)
             if i0 + int(np.argmin(v[i0:min(len(v), i1 + 60)])) < len(br) else False,
             'lock': lock, 'hard_lock': bool((ws[i0:i1 + 1] < 0.3 * v[i0:i1 + 1, None]).any(1).sum() >= 6)}
        if dec_f is not None:
            z['peak_dec_g_forces'] = robust_peak(dec_f[i0:i1 + 1])
            z['wheel_slip_max'] = float(np.nanmax(wslip[i0:i1 + 1]))
        out.append(z)
    return out


def robust_peak(x):
    """The zone's peak, as the mean of its top fifth (an impact spike is not a braking peak)."""
    x = np.sort(x[np.isfinite(x)])
    return float(np.mean(x[-max(1, len(x) // 5):])) if len(x) else float('nan')


def consistency(runs_z, tol=40.0):
    """Cluster zone onsets across runs: (median d, spread m, runs) for
    the zones seen in at least 3 runs."""
    ds = sorted((z['d'], k) for k, zs in enumerate(runs_z) for z in zs)
    clusters, cur = [], []
    for d, k in ds:
        if cur and d - cur[-1][0] > tol:
            clusters.append(cur)
            cur = []
        cur.append((d, k))
    if cur:
        clusters.append(cur)
    out = []
    for c in clusters:
        by_run = {}
        for d, k in c:
            by_run.setdefault(k, d)
        if len(by_run) >= 3:
            vals = np.array(list(by_run.values()))
            out.append((float(np.median(vals)), float(np.percentile(vals, 75) - np.percentile(vals, 25)), len(by_run)))
    return out


def main():
    out = {}
    allz = []
    stage_runs = {}
    for name, cap, idx, track, car, game in common.all_runs('acr') + common.all_runs('wrcg'):
        zs = zones(cap, idx, game)
        for z in zs:
            z['game'] = game
        allz += zs
        full = np.ptp(cap['lap_distance'][idx]) > 0.9 * (np.nanmax(cap['stage_length'][idx]) - 300)
        if full:
            stage_runs.setdefault((game, track or '%.0f m stage' % np.nanmax(cap['stage_length'][idx]), car), []).append(zs)
    acr = [z for z in allz if z['game'] == 'acr']
    wr = [z for z in allz if z['game'] == 'wrcg']
    locks = [z for z in acr if z['lock']]
    nolock = [z for z in acr if not z['lock']]
    out['zones'] = {'acr': len(acr), 'wrcg': len(wr)}
    out['acr'] = {
        'lock_share': round(len(locks) / max(1, len(acr)), 3),
        'hard_lock_share': round(float(np.mean([z['hard_lock'] for z in acr])), 3),
        'brake_at_slowest_share': round(float(np.mean([z['brake_at_slowest'] for z in acr])), 3),
        'wheel_slip_max_median_lock_vs_not': [round(float(np.median([z['wheel_slip_max'] for z in locks])), 2) if locks else None,
                                              round(float(np.median([z['wheel_slip_max'] for z in nolock])), 2)],
        'r_peak_dec_speed_vs_forces': round(float(np.corrcoef([z['peak_dec_g'] for z in acr],
                                                              [z['peak_dec_g_forces'] for z in acr])[0, 1]), 3),
        'peak_dec_g_median': round(float(np.median([z['peak_dec_g'] for z in acr])), 2),
        'build_s_median': round(float(np.median([z['build_s'] for z in acr])), 2),
        'release_s_median': round(float(np.median([z['release_s'] for z in acr])), 2),
        'trail_share_median': round(float(np.median([z['trail_share'] for z in acr])), 2)}
    out['wrcg'] = {'lock_share': round(float(np.mean([z['lock'] for z in wr])), 3) if wr else None,
                   'peak_dec_g_median': round(float(np.median([z['peak_dec_g'] for z in wr])), 2) if wr else None,
                   'trail_share_median': round(float(np.median([z['trail_share'] for z in wr])), 2) if wr else None}
    out['onset_consistency'] = {}
    plot_data = None
    for (game, track, car), runs_z in stage_runs.items():
        if len(runs_z) < 3:
            continue
        cl = consistency(runs_z)
        if not cl:
            continue
        spreads = [c[1] for c in cl]
        out['onset_consistency']['%s / %s / %s' % (game, track, car)] = {
            'full_runs': len(runs_z), 'zones_in_3plus_runs': len(cl),
            'onset_iqr_m_median': round(float(np.median(spreads)), 1),
            'onset_iqr_m_p90': round(float(np.percentile(spreads, 90)), 1),
            'least_consistent': [[round(c[0]), round(c[1], 1)] for c in sorted(cl, key=lambda c: -c[1])[:3]]}
        if plot_data is None or len(runs_z) > len(plot_data[1]):
            plot_data = ((game, track, car), runs_z, cl)
    if plot_data:
        (game, track, car), runs_z, cl = plot_data
        fig, ax = plt.subplots(figsize=(14, 3.5))
        for k, zs in enumerate(runs_z):
            ax.scatter([z['d'] for z in zs], [k] * len(zs), c=['r' if z['lock'] else 'k' for z in zs], s=10)
        for c in cl:
            ax.axvspan(c[0] - c[1] / 2, c[0] + c[1] / 2, color='C1', alpha=0.3)
        ax.set_xlabel('braking onset, distance along the stage m')
        ax.set_ylabel('run')
        ax.set_title('%s, %s: braking onsets per full run (red: lock-up); orange: inter-quartile spread' % (track, car),
                     fontsize=9)
        common.savefig(fig, 'braking_onsets.png')
    with open(os.path.join(common.OUT, 'braking.json'), 'w') as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out, indent=1))


if __name__ == '__main__':
    main()
