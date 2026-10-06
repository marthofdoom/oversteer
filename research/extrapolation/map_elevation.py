#!/usr/bin/env python3
"""Stage map and elevation profile.

1. From positions (WRC Generations; ACR from the 2026-10-05 bridge fix on):
   the plan view, the elevation profile on a 5 m grid of distance, its
   gradient, vertical curvature, crests and dips, and the climb, descent,
   minimum and maximum, checked against the WRCG stage table (from the
   game's own files).
2. Without positions (every ACR capture so far): the plan view by dead
   reckoning, integrating the course heading (yaw rate plus the body slip
   angle from the car-frame velocity) with the speed. Checked by loop
   closure on the Livigno circuit and by how well independent runs of the
   same stage agree after a rigid alignment.
3. Without positions: the grade from the tyre forces ACR sends,
   sin(grade) = (sum fx - m a_x) / (m g), m from the wheel loads. Checked
   by repeatability between runs; its absolute level carries the rolling
   and aero drag, which wait on positions to be calibrated.

    python3 research/extrapolation/map_elevation.py
"""
import json
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

import common  # noqa: E402

STEP = 5.0


def wrcg_table():
    with open(os.path.join(common.ROOT, 'data/telemetry/stages/wrcg.json')) as f:
        return json.load(f)['stages']


def elevation_profile(s, z, step=STEP, smooth_m=30.0):
    """(grid, h, grade, vertical curvature) from height z against path
    distance s; smoothed over smooth_m metres."""
    grid = np.arange(s[0], s[-1], step)
    h = common.unwrap_resample(s, z, grid)
    h = np.where(np.isfinite(h), h, np.nanmedian(h))
    n = max(1, int(round(smooth_m / step)))
    hs = common.smooth(h, n)
    grade = np.gradient(hs, step)
    kv = np.gradient(common.smooth(grade, n), step)       # 1/m, positive = dip (concave up)
    return grid, hs, grade, kv


def climb_descent(h, hysteresis=1.0):
    """Total climb and descent of a profile, ignoring wiggles under
    `hysteresis` m (as a GPS logger would)."""
    up = down = 0.0
    ref = h[0]
    for x in h[1:]:
        if x - ref >= hysteresis:
            up += x - ref
            ref = x
        elif ref - x >= hysteresis:
            down += ref - x
            ref = x
    return up, down


def crests(grid, grade, kv, speed=None, min_kv=1 / 150.0):
    """Crests (kv < -min_kv: radius under 150 m, convex) and dips, as
    (distance, radius m, kind)."""
    out = []
    sign = np.sign(kv) * (np.abs(kv) > min_kv)
    i = 0
    while i < len(sign):
        if sign[i] == 0:
            i += 1
            continue
        j = i
        while j + 1 < len(sign) and sign[j + 1] == sign[i]:
            j += 1
        k = i + int(np.argmax(np.abs(kv[i:j + 1])))
        out.append((grid[k], 1.0 / abs(kv[k]), 'crest' if sign[i] < 0 else 'dip'))
        i = j + 1
    return out


def dead_reckon(t, v, yaw_rate, vel):
    """Plan view (x, y) and path distance by integrating the course
    heading: yaw rate plus the body slip angle."""
    dt = np.clip(np.diff(t, prepend=t[0]), 0.0, 0.1)
    beta = np.arctan2(vel[:, 1], np.maximum(vel[:, 0], 0.5))
    beta = np.where(np.isfinite(beta), beta, 0.0)
    psi = np.cumsum(np.nan_to_num(yaw_rate) * dt) + beta
    x = np.cumsum(v * np.cos(psi) * dt)
    y = np.cumsum(v * np.sin(psi) * dt)
    s = np.cumsum(v * dt)
    return x, y, s, psi


def rigid_align(a, b):
    """Rotate and translate b (n x 2) onto a; returns the moved b."""
    ok = np.isfinite(a).all(1) & np.isfinite(b).all(1)
    ca, cb = a[ok].mean(0), b[ok].mean(0)
    h = (b[ok] - cb).T @ (a[ok] - ca)
    u, _s, vt = np.linalg.svd(h)
    r = (u @ vt).T
    if np.linalg.det(r) < 0:
        vt[-1] *= -1
        r = (u @ vt).T
    return (b - cb) @ r.T + ca


def wrcg():
    table = wrcg_table()
    rows = []
    fig, axes = plt.subplots(2, 4, figsize=(16, 7))
    for col, (name, cap, idx, _tr, _car, _g) in enumerate(common.all_runs('wrcg')):
        p = cap['pos'][idx]
        length = cap['stage_length'][idx][0]
        entry = min(table, key=lambda e: abs(e['length_m'] - length))
        # WRCG's world is z up (the decoder's pos convention says y up: see the doc)
        xy, z = p[:, :2], p[:, 2]
        s = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]
        grid, h, grade, kv = elevation_profile(s, z)
        up, down = climb_descent(h)
        cr = crests(grid, grade, kv)
        finished = cap['lap_distance'][idx][-1] > length - 50
        rows.append({'capture': name, 'stage': entry['stage'], 'finished': bool(finished), 'path_m': round(s[-1]),
                     'min': round(h.min(), 1), 'max': round(h.max(), 1), 'table_min': entry['elevation_min_m'],
                     'table_max': entry['elevation_max_m'], 'climb': round(up), 'descent': round(down),
                     'table_climb': entry['climb_m'], 'table_descent': entry['descent_m'],
                     'max_grade': round(float(np.max(np.abs(grade))), 3),
                     'crests_r<150m': sum(1 for c in cr if c[2] == 'crest'),
                     'dips_r<150m': sum(1 for c in cr if c[2] == 'dip')})
        if col < 4:
            ax = axes[0, col]
            sc = ax.scatter(xy[::10, 0], xy[::10, 1], c=z[::10], s=1, cmap='viridis')
            ax.set_aspect('equal')
            ax.set_title('%s (%s)' % (entry['stage'], 'finished' if finished else 'part'), fontsize=9)
            plt.colorbar(sc, ax=ax, fraction=0.04, label='z m')
            ax2 = axes[1, col]
            ax2.plot(grid, h, lw=1)
            for d, _r, kind in cr:
                ax2.axvline(d, color='r' if kind == 'crest' else 'b', lw=0.3)
            ax2.set_xlabel('path m')
            ax2.set_ylabel('elevation m')
    fig.suptitle('WRC Generations: map and elevation from positions (red crests, blue dips, vertical radius < 150 m)')
    common.savefig(fig, 'map_elevation_wrcg.png')
    return rows


def acr_dead_reckoning():
    out = {}
    allr = common.all_runs('acr')
    # 1. loop closure on the Livigno circuit
    loops = []
    for name, cap, idx, tr, car, _g in allr:
        if tr and tr.startswith('Livigno'):
            x, y, s, psi = dead_reckon(cap['t'][idx], cap['speed'][idx], cap['yaw_rate'][idx], cap['vel'][idx])
            loops.append({'capture': name, 'path_m': round(s[-1]), 'turns': round((psi[-1] - psi[0]) / 2 / np.pi, 3),
                          'closure_m': round(float(np.hypot(x[-1] - x[0], y[-1] - y[0])), 1)})
    out['livigno'] = loops
    # 2. cross-run agreement on Wales Afon Bidno (full runs)
    full = [r for r in allr if r[3] == 'Wales Afon Bidno' and np.ptp(r[1]['lap_distance'][r[2]]) > 5000]
    grid = np.arange(300, 5500, STEP)
    maps = []
    for name, cap, idx, _tr, car, _g in full:
        x, y, s, psi = dead_reckon(cap['t'][idx], cap['speed'][idx], cap['yaw_rate'][idx], cap['vel'][idx])
        d = cap['lap_distance'][idx]
        maps.append((name, car, np.c_[common.unwrap_resample(d, x, grid), common.unwrap_resample(d, y, grid)],
                     s[-1] / (d[-1] - d[0])))
    ref = maps[0][2]
    dev = []
    fig, ax = plt.subplots(1, 2, figsize=(13, 6))
    aligned = []
    for name, car, m, ratio in maps:
        b = rigid_align(ref, m)
        aligned.append(b)
    cons = np.nanmedian(np.array(aligned), axis=0)
    for (name, car, m, ratio), b in zip(maps, aligned):
        b2 = rigid_align(cons, b)
        e = np.linalg.norm(b2 - cons, axis=1)
        dev.append({'capture': name, 'car': car, 'rms_m': round(float(np.sqrt(np.nanmean(e ** 2))), 1),
                    'p95_m': round(float(np.nanpercentile(e, 95)), 1),
                    'path_over_spline': round(float(ratio), 4)})
        ax[0].plot(b2[:, 0], b2[:, 1], lw=0.6, alpha=0.7)
    ax[0].plot(cons[:, 0], cons[:, 1], 'k', lw=1.2)
    ax[0].set_aspect('equal')
    # 3. elevation shape from forces
    gs = []
    for name, cap, idx, _tr, car, _g in full:
        g = force_grade(cap, idx)
        gs.append(common.unwrap_resample(cap['lap_distance'][idx], g, grid))
    gs = np.array(gs)
    cons_g = np.nanmedian(gs, axis=0)
    rs = np.corrcoef(np.array([common.smooth(np.nan_to_num(g), 10) for g in gs]))
    rs = rs[np.triu_indices(len(gs), 1)]
    raw = np.cumsum(np.nan_to_num(cons_g)) * STEP
    bias = livigno_bias(allr)
    ax[1].plot(grid, raw, 'k', label='uncorrected (rolling + aero drag read as climb)')
    if bias is not None:
        ax[1].plot(grid, np.cumsum(np.nan_to_num(cons_g - bias)) * STEP, 'C1',
                   label='less the Livigno loop bias %.4f (another car, snow)' % bias)
    ax[1].legend(fontsize=8)
    ax[1].set_title('ACR Wales: height from tyre forces, median of %d runs\n(the shape is repeatable; the level waits on positions)'
                    % len(gs), fontsize=9)
    ax[1].set_xlabel('stage distance m')
    ax[1].set_ylabel('relative height m')
    ax[0].set_title('ACR Wales Afon Bidno: %d runs dead-reckoned\n(no positions), aligned; black = median' % len(maps), fontsize=9)
    common.savefig(fig, 'map_acr_dead_reckoning.png')
    out['wales_runs'] = dev
    out['force_grade_pairwise_r'] = {'median': round(float(np.median(rs)), 3), 'min': round(float(rs.min()), 3)}
    out['force_grade_mean_bias_livigno'] = bias
    out['wales_net_climb_uncorrected_m'] = round(float(raw[-1]))
    if bias is not None:
        out['wales_net_climb_less_bias_m'] = round(float(np.nansum(cons_g - bias) * STEP))
    return out


def livigno_bias(allr):
    """Mean force grade over a closed lap of the Livigno circuit, which
    must be 0: what rolling and aero drag add (208 Rally4, snow)."""
    for name, cap, idx, tr, car, _g in allr:
        if tr and tr.startswith('Livigno'):
            x, y, s, psi = dead_reckon(cap['t'][idx], cap['speed'][idx], cap['yaw_rate'][idx], cap['vel'][idx])
            if abs((psi[-1] - psi[0]) / 2 / np.pi) > 0.95:
                return round(float(np.nanmean(force_grade(cap, idx))), 4)
    return None


def force_grade(cap, idx):
    """sin(grade) from ACR's tyre forces: (sum fx - m a_x) / (m g), m
    from the median total wheel load; smoothed over half a second."""
    v = cap['speed'][idx]
    ax = np.clip(cap['accel'][idx][:, 0], -30, 30)
    fx = cap['x_fx'][idx].sum(1)
    m = np.nanmedian(cap['x_wheel_load'][idx].sum(1)) / common.G
    g = common.smooth((fx - m * ax) / (m * common.G), 31)
    return np.where((v > 5) & (np.abs(cap['accel'][idx][:, 0]) < 25), g, np.nan)


def main():
    res = {'wrcg': wrcg(), 'acr': acr_dead_reckoning()}
    with open(os.path.join(common.OUT, 'map_elevation.json'), 'w') as f:
        json.dump(res, f, indent=1)
    print(json.dumps(res, indent=1))


if __name__ == '__main__':
    main()
