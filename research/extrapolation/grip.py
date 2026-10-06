#!/usr/bin/env python3
"""Grip usage, balance and surface grip.

ACR (and ACC, by the same bridge) sends every tyre's load and its
longitudinal and lateral force. That gives, with no model:
- the friction each tyre uses, mu = sqrt(fx^2 + fy^2) / fz, and the
  peak the tyre reaches on this surface (its 99th percentile), so
  'grip used' = mu / peak, per wheel, per axle, per corner phase;
- the balance: the front axle's share of its grip against the rear's
  mid-corner (front nearer its peak = understeer, rear = oversteer);
- the surface's grip, from the peak mu.

Every other game has only the car's accelerations. Its 'grip used' is the
g-g estimate: total g over the envelope (the 98th percentile of total g
in the run's speed band). This script measures how well that generic
estimate tracks the force truth on ACR, which says how far the g-g
version can be trusted in WRCG, DiRT, Forza and the rest.

    python3 research/extrapolation/grip.py
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


def stage_surface(track):
    with open(os.path.join(common.ROOT, 'data/telemetry/stages/acr.json')) as f:
        stages = json.load(f)['stages']
    for s in stages:
        if s['track'].replace('ê', '_') == track or s['track'] == track or s['track'][:31] == track:
            return s['surface']
    return None


def per_run(cap, idx):
    v = cap['speed'][idx]
    a = cap['accel'][idx]
    ok = (v > 8) & (np.abs(a[:, 0]) < 25) & (np.abs(a[:, 1]) < 25)
    fx, fy, fz = cap['x_fx'][idx], cap['x_fy'][idx], cap['x_wheel_load'][idx]
    mu = np.sqrt(fx ** 2 + fy ** 2) / np.maximum(fz, 200.0)
    mu = np.where(fz > 500, mu, np.nan)               # a wheel in the air uses nothing
    ax, ay = common.smooth(a[:, 0], 5), common.smooth(a[:, 1], 5)
    g = np.hypot(ax, ay) / common.G
    return ok, mu, g, ax, ay, v


def main():
    out = {'runs': [], 'surfaces': {}}
    ts_r, corner_r = [], []
    peaks = {}
    gg_all = {}
    for name, cap, idx, track, car, _g in common.all_runs('acr', min_length=1500):
        surface = stage_surface(track)
        ok, mu, g, ax, ay, v = per_run(cap, idx)
        if ok.sum() < 2000:
            continue
        peak = np.nanpercentile(mu[ok], 99, axis=0)               # per wheel
        use_w = mu / peak
        use_f = np.nanmean(use_w[:, :2], axis=1)
        use_r = np.nanmean(use_w[:, 2:], axis=1)
        use = np.nanmax(np.c_[use_f, use_r], axis=1)                # the axle nearer its limit
        # generic: g over the envelope of its 10 km/h speed band
        band = np.floor(v / (10 / 3.6))
        env = np.full_like(g, np.nan)
        for b in np.unique(band[ok]):
            m = ok & (band == b)
            if m.sum() > 100:
                env[m] = np.nanpercentile(g[m], 98)
        use_gg = g / env
        m = ok & np.isfinite(use) & np.isfinite(use_gg)
        fill = lambda x: np.where(np.isfinite(x), x, np.nanmedian(x))
        r = np.corrcoef(common.smooth(fill(use), 15)[m], common.smooth(fill(use_gg), 15)[m])[0, 1]
        # per 'corner' window: 2 s blocks while cornering hard (|ay| > 4)
        corner = ok & (np.abs(ay) > 4)
        blocks = np.flatnonzero(corner)
        pairs = []
        if len(blocks):
            groups = np.split(blocks, np.flatnonzero(np.diff(blocks) > 30) + 1)
            for gr in groups:
                if len(gr) > 30:
                    pairs.append((np.nanmean(use[gr]), np.nanmean(use_gg[gr]),
                                  np.nanmean(use_f[gr] - use_r[gr]), np.nanmean(np.abs(
                                      np.arctan2(cap['vel'][idx][gr, 1], np.maximum(cap['vel'][idx][gr, 0], 1.0))))))
        pairs = np.array(pairs)
        rc = np.corrcoef(pairs[:, 0], pairs[:, 1])[0, 1] if len(pairs) > 5 else np.nan
        rb = np.corrcoef(pairs[:, 2], pairs[:, 3])[0, 1] if len(pairs) > 5 else np.nan
        ts_r.append(r)
        corner_r.append(rc)
        peaks.setdefault((surface, car), []).append(peak)
        gg_all.setdefault(surface, []).append(np.nanpercentile(g[ok], 98))
        out['runs'].append({'capture': name, 'track': track, 'car': car, 'surface': surface,
                            'km': round(float(np.ptp(cap['lap_distance'][idx])) / 1000, 2),
                            'peak_mu_FL_FR_RL_RR': [round(float(x), 2) for x in peak],
                            'g_p98': round(float(np.nanpercentile(g[ok], 98)), 2),
                            'share_time_over_90pct_grip': round(float(np.nanmean(use[ok] > 0.9)), 3),
                            'r_gg_vs_force_usage_timeseries': round(float(r), 3),
                            'r_gg_vs_force_usage_per_corner': round(float(rc), 3) if np.isfinite(rc) else None,
                            'corners': int(len(pairs)),
                            'r_frontminusrear_vs_bodyslip_per_corner': round(float(rb), 3) if np.isfinite(rb) else None,
                            'balance_front_minus_rear_median': round(float(np.nanmedian(pairs[:, 2])), 3)
                            if len(pairs) else None})
    for (surface, car), ps in peaks.items():
        out['surfaces']['%s / %s' % (surface, car)] = {
            'runs': len(ps), 'peak_mu_axle_front': round(float(np.median([p[:2].mean() for p in ps])), 2),
            'peak_mu_axle_rear': round(float(np.median([p[2:].mean() for p in ps])), 2)}
    out['surface_g_p98'] = {s: [round(float(x), 2) for x in sorted(v)] for s, v in gg_all.items()}
    out['summary'] = {'median_r_timeseries': round(float(np.nanmedian(ts_r)), 3),
                      'median_r_per_corner': round(float(np.nanmedian(corner_r)), 3)}
    plot()
    with open(os.path.join(common.OUT, 'grip.json'), 'w') as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out, indent=1))


def plot():
    """One full i20N Wales run: the traction circle per axle and grip used
    along the stage, force truth against the g-g estimate."""
    runs = [r for r in common.all_runs('acr') if r[3] == 'Wales Afon Bidno' and r[4].startswith('Hyundai')
            and np.ptp(r[1]['lap_distance'][r[2]]) > 5000]
    if not runs:
        return
    name, cap, idx, track, car, _g = runs[0]
    ok, mu, g, ax, ay, v = per_run(cap, idx)
    fx, fy, fz = cap['x_fx'][idx], cap['x_fy'][idx], cap['x_wheel_load'][idx]
    fig, axs = plt.subplots(1, 3, figsize=(16, 5))
    for k, lab in ((0, 'FL'), (3, 'RR')):
        m = ok & (fz[:, k] > 500)
        axs[0].scatter(fy[m, k] / fz[m, k], fx[m, k] / fz[m, k], s=0.3, alpha=0.3, label=lab)
    axs[0].set_xlabel('fy / fz')
    axs[0].set_ylabel('fx / fz')
    axs[0].set_aspect('equal')
    axs[0].legend(markerscale=10)
    axs[0].set_title('Friction circle per tyre (ACR forces)', fontsize=9)
    axs[1].scatter(ay[ok] / common.G, ax[ok] / common.G, s=0.3, alpha=0.3, c='k')
    axs[1].set_aspect('equal')
    axs[1].set_xlabel('lateral g')
    axs[1].set_ylabel('longitudinal g')
    axs[1].set_title('g-g diagram (any game with accelerations)', fontsize=9)
    peak = np.nanpercentile(mu[ok], 99, axis=0)
    use = np.nanmax(np.c_[np.nanmean((mu / peak)[:, :2], 1), np.nanmean((mu / peak)[:, 2:], 1)], 1)
    d = cap['lap_distance'][idx]
    sl = (d > 1000) & (d < 2000)
    axs[2].plot(d[sl], common.smooth(use, 15)[sl], label='grip used, forces')
    env = np.nanpercentile(g[ok], 98)
    axs[2].plot(d[sl], common.smooth(g / env, 15)[sl], label='g / g-g envelope')
    axs[2].set_xlabel('distance m')
    axs[2].legend(fontsize=8)
    axs[2].set_title('%s, %s: grip used along 1 km' % (car, track), fontsize=9)
    common.savefig(fig, 'grip_acr.png')


if __name__ == '__main__':
    main()
