#!/usr/bin/env python3
"""ACR with positions (captures from the 2026-10-05 bridge fix on): the
ground truth the position-free methods were waiting for, and the methods
that need positions.

Checks of the position-free methods (map_elevation.py, corners.py):
1. the dead-reckoned plan view against the positions, per full run;
2. the grade from the tyre forces against the grade from the height,
   and the drag bias the Livigno loop suggested;
3. the vertical curvature from a_z / v^2 against the height's;
4. the course curvature from the yaw rate against the path's;
5. the stage table's start elevation against the height at the start.
Methods that need positions:
6. the racing line: each run's lateral offset from the median line of
   all runs (a reference line built from own runs, as the game gives no
   road edges), the spread per place and the 'road used' envelope
   (min..max offset over runs), and the offset at each corner's apex;
7. the elevation profile, crests and dips from the height, against the
   pace notes' OverCrest / Jump calls.

    python3 research/extrapolation/acr_positions.py
"""
import json
import os
import warnings

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

import common  # noqa: E402
import corners as corner_mod  # noqa: E402
import map_elevation as me  # noqa: E402

warnings.simplefilter('ignore')
STEP = 5.0


def positioned_runs():
    out = []
    for r in common.all_runs('acr'):
        name, cap, idx, track, car, _g = r
        p = cap['pos'][idx]
        if np.isfinite(p).all(1).mean() > 0.99 and (np.abs(p).sum(1) > 0.1).mean() > 0.99:
            out.append(r)
    return out


def stage_entry(track):
    with open(os.path.join(common.ROOT, 'data/telemetry/stages/acr.json')) as f:
        for s in json.load(f)['stages']:
            if s['track'][:31] == track:
                return s
    return None


def path_s(p):
    return np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]


def main():
    runs = positioned_runs()
    res = {'runs': [], 'stages': {}}
    by_stage = {}
    longest = {}
    for name, cap, idx, track, car, _g in runs:
        longest[track] = max(longest.get(track, 0), float(np.ptp(cap['lap_distance'][idx])))
    for name, cap, idx, track, car, _g in runs:
        t, v, d = cap['t'][idx], cap['speed'][idx], cap['lap_distance'][idx]
        p = cap['pos'][idx]
        full = np.ptp(d) > 0.9 * longest.get(track, 0)
        xz = np.c_[p[:, 0], -p[:, 2]]      # ACR's world is y up and left-handed: (x, -z) is the plan view seen from above
        h = p[:, 1]
        s = path_s(p)
        row = {'capture': name, 'track': track, 'car': car, 'km': round(np.ptp(d) / 1000, 2), 'full': bool(full),
               'path_over_spline': round(float(s[-1] / np.ptp(d)), 4),
               'path_over_speed_integral': round(float(s[-1] / np.sum(v[1:] * np.diff(t))), 4)}
        # 1. dead reckoning
        x, y, s_dr, psi = me.dead_reckon(t, v, cap['yaw_rate'][idx], cap['vel'][idx])
        dr = me.rigid_align(xz, np.c_[x, y])
        e = np.linalg.norm(dr - xz, axis=1)
        row['dead_reckon_rms_m'] = round(float(np.sqrt(np.mean(e ** 2))), 1)
        row['dead_reckon_end_m'] = round(float(e[-1]), 1)
        row['dead_reckon_per_km_m'] = round(float(np.sqrt(np.mean(e ** 2)) / (s[-1] / 1000)), 1)
        # 2. grade: forces against height
        grid = np.arange(d[0], d[-1], STEP)
        hh = common.unwrap_resample(d, h, grid)
        g_true = np.gradient(common.smooth(hh, 7), STEP)
        g_force = common.unwrap_resample(d, me.force_grade(cap, idx), grid)
        ok = np.isfinite(g_true) & np.isfinite(g_force)
        bias = float(np.mean(g_force[ok] - g_true[ok]))
        row['grade_r_forces_vs_height'] = round(float(np.corrcoef(common.smooth(g_force[ok], 6),
                                                                   common.smooth(g_true[ok], 6))[0, 1]), 3)
        row['grade_bias_forces_minus_height'] = round(bias, 4)
        vb = common.unwrap_resample(d, v, grid)
        fit = np.polyfit(vb[ok] ** 2, (g_force - g_true)[ok], 1)
        row['grade_bias_fit_c0_c2'] = [round(float(fit[1]), 4), round(float(fit[0]), 6)]
        h_force = np.nancumsum(np.nan_to_num(g_force - bias)) * STEP
        h_force += hh[0]
        row['height_rms_err_m_bias_removed'] = round(float(np.sqrt(np.nanmean((h_force - hh) ** 2))), 1)
        row['height_range_true_m'] = round(float(np.nanmax(hh) - np.nanmin(hh)), 1)
        # 3. vertical curvature
        kv_true = np.gradient(common.smooth(g_true, 6), STEP)
        az = np.clip(cap['accel'][idx][:, 2], -30, 30)
        fast = v > 12
        kv_acc = common.unwrap_resample(d[fast], common.smooth(az / np.maximum(v, 8) ** 2, 31)[fast], grid)
        ok3 = np.isfinite(kv_true) & np.isfinite(kv_acc)
        row['vert_curv_r_az_vs_height'] = round(float(np.corrcoef(kv_acc[ok3], kv_true[ok3])[0, 1]), 3)
        row['vert_curv_slope_az_vs_height'] = round(float(np.polyfit(kv_true[ok3], kv_acc[ok3], 1)[0]), 3)
        # 4. horizontal curvature, yaw against path
        heading = np.unwrap(np.arctan2(np.gradient(common.smooth(xz[:, 1], 9)), np.gradient(common.smooth(xz[:, 0], 9))))
        k_path = common.unwrap_resample(d, heading, grid)
        k_path = np.gradient(common.smooth(k_path, 3), STEP)
        vel = cap['vel'][idx]
        beta = np.arctan2(vel[:, 1], np.maximum(vel[:, 0], 0.5))
        k_yaw = (cap['yaw_rate'][idx] + np.gradient(common.smooth(beta, 9), t)) / np.maximum(v, 3)
        k_yaw = common.unwrap_resample(d, common.smooth(k_yaw, 15), grid)
        ok4 = np.isfinite(k_path) & np.isfinite(k_yaw)
        row['curvature_r_yaw_vs_path'] = round(float(np.corrcoef(k_yaw[ok4], k_path[ok4])[0, 1]), 3)
        row['curvature_slope_yaw_vs_path'] = round(float(np.polyfit(k_path[ok4], k_yaw[ok4], 1)[0]), 3)
        # 5. start elevation
        ent = stage_entry(track)
        if ent and d[0] < 300:
            row['start_height_m'] = round(float(h[0]), 1)
            row['table_elevation_start_m'] = ent.get('elevation_start_m')
        res['runs'].append(row)
        if full:
            by_stage.setdefault(track, []).append((name, cap, idx, car))
    # 6 + 7 per stage
    for track, rs in by_stage.items():
        res['stages'][track] = line_and_profile(track, rs)
    with open(os.path.join(common.OUT, 'acr_positions.json'), 'w') as f:
        json.dump(res, f, indent=1)
    print(json.dumps(res, indent=1))


def line_and_profile(track, rs):
    lo = max(np.nanmin(c['lap_distance'][i]) for _n, c, i, _car in rs) + 20
    hi = min(np.nanmax(c['lap_distance'][i]) for _n, c, i, _car in rs) - 20
    grid = np.arange(lo, hi, 2.0)
    P = []
    for _n, cap, idx, _car in rs:
        d = cap['lap_distance'][idx]
        p = cap['pos'][idx]
        P.append(np.c_[common.unwrap_resample(d, p[:, 0], grid), common.unwrap_resample(d, -p[:, 2], grid),
                       common.unwrap_resample(d, p[:, 1], grid)])
    P = np.array(P)                         # runs x grid x (x, z, h)
    ref = np.nanmedian(P[:, :, :2], axis=0)
    ref = np.c_[common.smooth(ref[:, 0], 5), common.smooth(ref[:, 1], 5)]
    tang = np.gradient(ref, axis=0)
    tang /= np.linalg.norm(tang, axis=1)[:, None]
    normal = np.c_[-tang[:, 1], tang[:, 0]]     # to the left of the direction of travel
    # the spline distance is the same point on the road for every run, so
    # the offset is the position's component along the road's normal
    off = np.einsum('rgk,gk->rg', P[:, :, :2] - ref[None], normal)
    along = np.einsum('rgk,gk->rg', P[:, :, :2] - ref[None], tang)
    out = {'runs': len(rs), 'cars': sorted(set(c for *_x, c in rs)), 'span_m': [round(lo), round(hi)],
           'along_track_misfit_m_p95': round(float(np.nanpercentile(np.abs(along), 95)), 2),
           'lateral_spread_m_median': round(float(np.nanmedian(np.nanstd(off, axis=0))), 2),
           'lateral_spread_m_p95': round(float(np.nanpercentile(np.nanstd(off, axis=0), 95)), 2),
           'road_used_envelope_m_median': round(float(np.nanmedian(np.nanmax(off, 0) - np.nanmin(off, 0))), 2),
           'road_used_envelope_m_p95': round(float(np.nanpercentile(np.nanmax(off, 0) - np.nanmin(off, 0), 95)), 2)}
    # corners on the reference line: offset at the apex per run (inside positive)
    heading = np.unwrap(np.arctan2(tang[:, 1], tang[:, 0]))
    k = np.gradient(common.smooth(heading, 5), 2.0)
    cs = corner_mod.detect_corners(grid[::3], common.smooth(k, 3)[::3])
    apex_off = []
    for c in cs:
        g = int(np.argmin(np.abs(grid - c['apex'])))
        side = np.sign(k[g])            # the inside of the corner in the normal's sign convention
        apex_off.append(off[:, g] * side)
    apex_off = np.array(apex_off)
    if len(apex_off):
        out['corners'] = len(cs)
        out['apex_offset_inside_m_median_per_run'] = [round(float(x), 2) for x in np.nanmedian(apex_off, axis=0)]
        out['apex_offset_spread_m_median'] = round(float(np.nanmedian(np.nanstd(apex_off, axis=1))), 2)
    # 7. profile from the height and the notes' crests
    h = np.nanmedian(P[:, :, 2], axis=0)
    gq, hs, grade, kv = me.elevation_profile(grid, h, step=STEP, smooth_m=15.0)
    up, down = me.climb_descent(hs)
    cr = me.crests(gq, grade, kv, min_kv=1 / 400.0)
    out['elevation'] = {'min': round(float(hs.min()), 1), 'max': round(float(hs.max()), 1), 'climb': round(up),
                        'descent': round(down), 'max_grade': round(float(np.max(np.abs(grade))), 3),
                        'crests_r_lt_400m': sum(1 for c in cr if c[2] == 'crest'),
                        'dips_r_lt_400m': sum(1 for c in cr if c[2] == 'dip')}
    stage = corner_mod.STAGES.get(track)
    notes = corner_mod.read_notes(stage) if stage else None
    if notes:
        cn = [d for d in corner_mod.crest_notes(notes) if lo <= d <= hi]
        crest_d = [c[0] for c in cr if c[2] == 'crest']
        hit = sum(1 for d in cn if any(-20 <= c - d <= 120 for c in crest_d))
        prec = sum(1 for c in crest_d if any(-20 <= c - d <= 150 for d in cn))
        out['elevation']['crest_notes'] = len(cn)
        out['elevation']['crest_recall'] = round(hit / max(1, len(cn)), 3)
        out['elevation']['crest_precision'] = round(prec / max(1, len(crest_d)), 3)
        cnotes = corner_mod.corner_notes(notes)
        pairs = corner_mod.match([n for n in cnotes if lo + 50 <= n[0] <= hi - 100], cs)
        m = [p for p in pairs if p[3] is not None]
        out['corner_notes_matched_from_positions'] = '%d of %d' % (len(m), len(pairs))
        out['spearman_grade_vs_radius_positions'] = round(corner_mod.spearman([p[2] for p in m],
                                                                             [p[3]['radius'] for p in m]), 3) \
            if len(m) > 3 else None
    # plot
    fig, ax = plt.subplots(1, 3, figsize=(18, 5))
    ax[0].plot(ref[:, 0], ref[:, 1], 'k', lw=0.8)
    ax[0].set_aspect('equal')
    ax[0].set_title('%s: median line of %d runs (positions)' % (track, len(rs)), fontsize=9)
    sl = slice(0, min(len(grid), 600))
    for r in range(len(rs)):
        ax[1].plot(grid[sl], off[r, sl], lw=0.8)
    ax[1].set_xlabel('distance m')
    ax[1].set_ylabel('lateral offset from the median line m')
    ax[1].set_title('Line per run over the first 1.2 km', fontsize=9)
    ax[2].plot(gq, hs, 'k')
    for dd, _r, kind in cr:
        ax[2].axvline(dd, color='r' if kind == 'crest' else 'b', lw=0.4)
    if notes:
        for dd in corner_mod.crest_notes(notes):
            if lo <= dd <= hi:
                ax[2].plot(dd, np.interp(dd, gq, hs), 'g^', ms=5)
    ax[2].set_title('Height (red crests, blue dips; green: the notes\' crests)', fontsize=9)
    common.savefig(fig, 'acr_positions_%s.png' % track.split()[1].lower())
    return out


if __name__ == '__main__':
    main()
