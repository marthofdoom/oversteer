#!/usr/bin/env python3
"""Potential time: a quasi-steady-state lap simulation per stage, from
the driver's own data, so the coach can say where time is left even when
every run is equally slow.

Inputs, all measured (no car data needed; works for any game that sends
speed and yaw rate, or positions):
- the road: the course curvature k(d) on a 2 m grid of the distance along
  the stage, k = (yaw rate + d(body slip)/dt) / v, the median over every
  run of the stage (any car), smoothed over 10 m. With positions (ACR from
  the 2026-10-05 bridge, WRCG, DiRT, Forza) the path's own curvature can
  stand in (acr_positions.py checks the two against each other);
- the car's grip on the surface, from the driver's g-g envelope over all
  runs of the car on that surface: a_lat_max(v) and the braking and drive
  envelopes a_brake_max(v), a_drive_max(v), each the P-th percentile
  (P = 98 by default) in 2.5 m/s speed bins, combined as a friction
  ellipse (a_x / a_x_max)^2 + (a_y / a_y_max)^2 <= 1;
Method: v_corner(d) = sqrt(a_lat_max / |k|), never below the fastest
speed any of the car's runs reached at d (the car has done it there); a forward pass at the drive
envelope and a backward pass at the braking envelope, each limited by the
lateral grip the curvature uses; the potential time is the integral of
ds / v.

Per section (apex to apex, cut where the road is straightest): potential,
the PB run's time, the best time ever through it, the time available, the
grip used at the apex (the PB run's lateral g over a_lat_max at that
speed; with ACR's tyre forces also the force-based usage), and where the
time is (entry, apex, exit) by integrating 1/v_pb - 1/v_pot over each
phase. Then the 'leaderboard target' mode: a target time's gap is
apportioned over the sections in proportion to the time available in each.

    python3 research/extrapolation/lapsim.py [--stage 'Wales Afon Bidno'] [--car 'Hyundai i20N Rally2']
                                             [--target 173] [--p 98]
"""
import argparse
import json
import os
import warnings

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

import common  # noqa: E402

warnings.simplefilter('ignore')
DS = 2.0
VBIN = 2.5
RADIUS_GRADES = ((12.0, 1), (25.0, 2), (45.0, 3), (80.0, 4), (140.0, 5))     # coach_context.RADIUS_GRADES


def curvature(cap, idx):
    t, v = cap['t'][idx], cap['speed'][idx]
    vel = cap['vel'][idx]
    yaw = cap['yaw_rate'][idx]
    if np.isfinite(vel).all() and np.isfinite(yaw).all():
        beta = np.arctan2(vel[:, 1], np.maximum(vel[:, 0], 0.5))
        k = (yaw + np.gradient(common.smooth(beta, 9), t)) / np.maximum(v, 3.0)
    else:
        p = cap['pos'][idx]
        hd = np.unwrap(np.arctan2(np.gradient(common.smooth(p[:, 1], 9)), np.gradient(common.smooth(p[:, 0], 9))))
        s = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p[:, :2], axis=0), axis=1))]
        k = np.gradient(common.smooth(hd, 9), s + np.arange(len(s)) * 1e-6)
    return np.where(v > 3, common.smooth(k, 9), np.nan)


def curvature_pos(cap, idx):
    """Path curvature from the positions (ACR: y up, (x, -z) the plan
    view), per sample, positive left."""
    p = cap['pos'][idx]
    x, y = common.smooth(p[:, 0], 9), common.smooth(-p[:, 2], 9)
    s = np.r_[0, np.cumsum(np.hypot(np.diff(x), np.diff(y)))]
    hd = np.unwrap(np.arctan2(np.gradient(y), np.gradient(x)))
    k = np.gradient(common.smooth(hd, 9), s + np.arange(len(s)) * 1e-6)
    return np.where(cap['speed'][idx] > 3, common.smooth(k, 9), np.nan)


def has_pos(cap, idx):
    p = cap['pos'][idx]
    return np.isfinite(p).all(1).mean() > 0.99 and (np.abs(p).sum(1) > 0.1).mean() > 0.99


def envelope(rows, p):
    """a_lat_max(v), a_brake_max(v), a_drive_max(v) on speed bins (m/s^2)."""
    v, ax, ay, thr, brk = rows
    bins = np.arange(0, 60, VBIN)
    out = {}
    for name, mask, val in (('lat', np.ones_like(v, bool), np.abs(ay)),
                            ('brake', (brk > 0.3) & (np.abs(ay) < 3.0), -ax),
                            ('drive', (thr > 0.95) & (np.abs(ay) < 3.0), ax)):
        e = np.full(len(bins), np.nan)
        for i, b in enumerate(bins):
            m = mask & (v >= b) & (v < b + VBIN) & np.isfinite(val)
            if m.sum() >= 40:
                e[i] = np.percentile(val[m], p)
        good = np.isfinite(e)
        e = np.interp(bins, bins[good], e[good])
        out[name] = e
    out['bins'] = bins + VBIN / 2
    # the drive envelope cannot rise with speed (power limited); the others are kept as measured
    out['drive'] = np.minimum.accumulate(np.maximum(out['drive'], 0.3))
    return out


def env_at(env, name, v):
    return np.interp(v, env['bins'], env[name])


def simulate(k, env, v0=0.5, v_end=None, floor=None):
    n = len(k)
    ak = np.maximum(np.abs(k), 1e-5)
    vlim = np.full(n, 60.0)
    for _ in range(4):
        vlim = np.minimum(60.0, np.sqrt(env_at(env, 'lat', vlim) / ak))
    if floor is not None:
        # the car has been this fast here: the corner limit is never below the fastest observed speed
        vlim = np.maximum(vlim, np.nan_to_num(floor))
    v = vlim.copy()
    v[0] = min(v0, vlim[0])
    for i in range(n - 1):
        ay = v[i] ** 2 * ak[i]
        frac = max(0.0, 1 - (ay / env_at(env, 'lat', v[i])) ** 2)
        a = env_at(env, 'drive', v[i]) * np.sqrt(frac)
        v[i + 1] = min(vlim[i + 1], np.sqrt(v[i] ** 2 + 2 * a * DS))
    if v_end is not None:
        v[-1] = min(v[-1], v_end)
    for i in range(n - 2, -1, -1):
        ay = v[i + 1] ** 2 * ak[i + 1]
        frac = max(0.0, 1 - (ay / env_at(env, 'lat', v[i + 1])) ** 2)
        a = env_at(env, 'brake', v[i + 1]) * np.sqrt(frac)
        v[i] = min(v[i], np.sqrt(v[i + 1] ** 2 + 2 * a * DS))
    return v, vlim


def t_of_d(cap, idx, grid):
    d, t = cap['lap_distance'][idx], cap['t'][idx]
    keep = np.r_[True, np.diff(d) > 0.01]
    return np.interp(grid, d[keep], t[keep])


def sections(grid, k, min_len=120.0):
    """Cuts between apexes, at the straightest point; short ones merged."""
    a = common.smooth(np.abs(k), 15)
    apex = [i for i in range(1, len(a) - 1) if a[i] >= a[i - 1] and a[i] > a[i + 1] and a[i] > 1 / 200]
    cuts = [0]
    for i, j in zip(apex, apex[1:]):
        c = i + int(np.argmin(a[i:j + 1]))
        if grid[c] - grid[cuts[-1]] >= min_len:
            cuts.append(c)
    if grid[-1] - grid[cuts[-1]] < min_len and len(cuts) > 1:
        cuts.pop()
    cuts.append(len(grid) - 1)
    return [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)]


def grade_word(r):
    for limit, g in RADIUS_GRADES:
        if r < limit:
            return str(g)
    return '6'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stage', default='Wales Afon Bidno')
    ap.add_argument('--car', default='Hyundai i20N Rally2')
    ap.add_argument('--target', type=float, default=173.0)
    ap.add_argument('--owner-pb', type=float, default=188.0)
    ap.add_argument('--p', type=float, default=98.0)
    ap.add_argument('--before', default=None,
                    help='use only captures whose name sorts before this (a hindcast: what the model said then)')
    ap.add_argument('--curv', default='yaw', choices=('yaw', 'pos'),
                    help='the road from the yaw rate (any game) or from the positions')
    ap.add_argument('--no-floor', dest='floor', action='store_false',
                    help="no floor at the fastest speed observed at each place")
    ap.add_argument('--d0', type=float, default=238.5)      # the start line on the spline (Afon Bidno)
    ap.add_argument('--d1', type=float, default=5277.0)     # the game's 'Finish' pace note (the flying finish)
    args = ap.parse_args()
    allr = everything = common.all_runs('acr')
    if args.before:
        allr = [r for r in allr if r[0] < args.before]
    stage_runs = [r for r in allr if r[3] == args.stage]
    full = [r for r in stage_runs if r[1]['lap_distance'][r[2]][0] <= args.d0 + 5
            and r[1]['lap_distance'][r[2]][-1] >= args.d1]
    grid = np.arange(args.d0, args.d1, DS)
    # the road: median course curvature of every run of the stage (partials too, where they cover)
    cf = curvature_pos if args.curv == 'pos' else curvature
    ks = [common.unwrap_resample(c['lap_distance'][i], cf(c, i), grid) for _n, c, i, *_ in stage_runs
          if args.curv == 'yaw' or has_pos(c, i)]
    k = common.smooth(np.nan_to_num(np.nanmedian(ks, 0)), 5)
    k_yaw = common.smooth(np.nan_to_num(np.nanmedian(
        [common.unwrap_resample(c['lap_distance'][i], curvature(c, i), grid) for _n, c, i, *_ in stage_runs], 0)), 5)
    # the sections are cut on the road's geometry from every run (a hindcast keeps the same sections)
    ks_all = [common.unwrap_resample(c['lap_distance'][i], curvature(c, i), grid) for _n, c, i, tr, *_ in everything
              if tr == args.stage]
    k_cut = common.smooth(np.nan_to_num(np.nanmedian(ks_all, 0)), 5)
    # the grip: every run of the car on stages of the same surface
    surface = stage_surface(args.stage)
    rows = [[], [], [], [], []]
    for _n, c, i, tr, car, _g in allr:
        if car != args.car or stage_surface(tr) != surface:
            continue
        a = c['accel'][i]
        ok = (np.abs(a[:, 0]) < 25) & (np.abs(a[:, 1]) < 25) & (c['speed'][i] > 3)
        rows[0].append(c['speed'][i][ok])
        rows[1].append(common.smooth(a[:, 0], 9)[ok])
        rows[2].append(common.smooth(a[:, 1], 9)[ok])
        rows[3].append(np.nan_to_num(c['throttle'][i])[ok])
        rows[4].append(np.nan_to_num(c['brake'][i])[ok])
    rows = [np.concatenate(x) for x in rows]
    env = envelope(rows, args.p)
    env_car = car_envelope(rows, args.car, allr)
    # the car's runs: times along the grid
    mine = [r for r in full if r[4] == args.car]
    floor = np.nanmax([common.unwrap_resample(c['lap_distance'][i], c['speed'][i], grid) for _n, c, i, *_ in mine],
                      axis=0) if args.floor else None
    v_pot, vlim = simulate(k, env, floor=floor)
    t_pot = np.r_[0, np.cumsum(2 * DS / (v_pot[1:] + v_pot[:-1]))]
    v_car, _vl = simulate(k, env_car, floor=floor)
    t_car = np.r_[0, np.cumsum(2 * DS / (v_car[1:] + v_car[:-1]))]
    T = np.array([t_of_d(c, i, grid) - t_of_d(c, i, grid)[0] for _n, c, i, *_ in mine])
    V = np.array([common.unwrap_resample(c['lap_distance'][i], c['speed'][i], grid) for _n, c, i, *_ in mine])
    AY = np.array([common.unwrap_resample(c['lap_distance'][i], common.smooth(c['accel'][i][:, 1], 9), grid)
                   for _n, c, i, *_ in mine])
    MU = []
    for _n, c, i, *_ in mine:
        fx, fy, fz = c['x_fx'][i], c['x_fy'][i], c['x_wheel_load'][i]
        mu = np.sqrt(fx ** 2 + fy ** 2) / np.maximum(fz, 200)
        mu = np.where(fz > 500, mu, np.nan)
        peak = np.nanpercentile(mu[c['speed'][i] > 8], 99, axis=0)
        use = np.nanmax(np.c_[np.nanmean((mu / peak)[:, :2], 1), np.nanmean((mu / peak)[:, 2:], 1)], 1)
        MU.append(common.unwrap_resample(c['lap_distance'][i], common.smooth(use, 9), grid))
    MU = np.array(MU)
    totals = T[:, -1]
    pb = int(np.argmin(totals))
    secs = sections(grid, k_cut)
    rows_out = []
    sob = 0.0
    for a, b in secs:
        seg_t = T[:, b] - T[:, a]
        pot = t_pot[b] - t_pot[a]
        ap_i = a + int(np.argmax(common.smooth(np.abs(k), 5)[a:b + 1]))
        r_apex = 1 / max(abs(k[ap_i]), 1e-4)
        # the PB run's slowest point near the apex
        lo, hi = max(a, ap_i - 25), min(b, ap_i + 25)
        m_i = lo + int(np.argmin(V[pb, lo:hi + 1]))
        v_ap = V[pb, m_i]
        grip = abs(AY[pb, m_i]) / env_at(env, 'lat', v_ap)
        # phases: entry = section start to 20 m before the apex, apex = +-20 m, exit = the rest
        inv = 1 / np.maximum(V[pb], 1) - 1 / np.maximum(v_pot, 1)
        e0, e1 = max(a, ap_i - 10), min(b, ap_i + 10)
        loss = {'entry': float(np.sum(inv[a:e0]) * DS), 'apex': float(np.sum(inv[e0:e1]) * DS),
                'exit': float(np.sum(inv[e1:b]) * DS)}
        cause = max(loss, key=loss.get)
        sob += seg_t.min()
        rows_out.append({
            'd0': round(grid[a]), 'd1': round(grid[b]), 'apex_d': round(grid[ap_i]),
            'dir': 'left' if k[ap_i] > 0 else 'right', 'radius_m': round(r_apex, 1),
            'grade': grade_word(r_apex) if r_apex < 300 else 'straight',
            'user_s': round(float(seg_t.min()), 2), 'grip_s': round(pot, 2),
            'car_s': round(float(t_car[b] - t_car[a]), 2),
            'potential_s': round(pot, 2), 'pb_s': round(float(seg_t[pb]), 2), 'best_s': round(float(seg_t.min()), 2),
            'available_s': round(float(seg_t[pb] - pot), 2),
            'potential_faster_than_best': bool(pot < seg_t.min()),
            'apex_kmh_pb': round(v_ap * 3.6, 1), 'apex_kmh_potential': round(float(v_pot[m_i]) * 3.6, 1),
            'grip_used_at_apex_gg': round(float(grip), 2),
            'grip_used_at_apex_forces': round(float(MU[pb, m_i]), 2) if np.isfinite(MU[pb, m_i]) else None,
            'loss_s': {k2: round(v2, 2) for k2, v2 in loss.items()}, 'cause': cause})
    if args.curv == 'pos':
        v_y, _ = simulate(k_yaw, env, floor=floor)
        t_y = np.r_[0, np.cumsum(2 * DS / (v_y[1:] + v_y[:-1]))]
        okc = np.abs(k) > 1 / 300
        yaw_vs_pos = {'potential_yaw_s': round(float(t_y[-1]), 1), 'potential_pos_s': round(float(t_pot[-1]), 1),
                      'curvature_r': round(float(np.corrcoef(k_yaw, k)[0, 1]), 3),
                      'radius_ratio_yaw_over_pos_median_in_corners': round(float(np.median(
                          np.abs(k[okc]) / np.maximum(np.abs(k_yaw[okc]), 1e-5))), 3),
                      'section_abs_diff_s_median': None}
    out = {'stage': args.stage, 'car': args.car, 'surface': surface, 'percentile': args.p, 'floor': args.floor,
           'curvature_from': args.curv,
           'runs': len(mine), 'run_times_s': [round(float(x), 1) for x in totals],
           'pb_captured_s': round(float(totals[pb]), 1), 'sum_of_best_sections_s': round(sob, 1),
           'potential_s': round(float(t_pot[-1]), 1),
           'layers_s': {'pb': round(float(totals[pb]), 1), 'user (sum of best)': round(sob, 1),
                        'grip (P%g of own g-g)' % args.p: round(float(t_pot[-1]), 1),
                        'car (best grip any run, engine from car data)': round(float(t_car[-1]), 1)},
           'car_envelope': {'lat_g': [round(x / common.G, 2) for x in env_car['lat'][::2]],
                            'brake_g': [round(x / common.G, 2) for x in env_car['brake'][::2]],
                            'drive_g': [round(x / common.G, 2) for x in env_car['drive'][::2]],
                            'mass_kg': env_car['mass'], 'gear_set': env_car['gear_set']},
           'owner_pb_s': args.owner_pb, 'target_s': args.target,
           'envelope': {'bins_kmh': [round(x * 3.6) for x in env['bins'][::2]],
                        'lat_g': [round(x / common.G, 2) for x in env['lat'][::2]],
                        'brake_g': [round(x / common.G, 2) for x in env['brake'][::2]],
                        'drive_g': [round(x / common.G, 2) for x in env['drive'][::2]]},
           'sections': rows_out}
    if args.curv == 'pos':
        yaw_vs_pos['section_abs_diff_s_median'] = round(float(np.median(
            [abs((t_y[b] - t_y[a]) - (t_pot[b] - t_pot[a])) for a, b in secs])), 3)
        out['yaw_vs_pos'] = yaw_vs_pos
    out['sections_where_potential_not_faster_than_best'] = sum(1 for r in rows_out if not r['potential_faster_than_best'])
    # leaderboard target mode
    gap = float(totals[pb]) - args.target
    avail = np.array([max(0.0, r['available_s']) for r in rows_out])
    share = avail / avail.sum()
    calls = []
    for r, sh in zip(rows_out, share):
        r['target_gain_s'] = round(gap * sh, 2)
        f = r['pb_s'] / max(0.1, r['pb_s'] - r['target_gain_s'])
        r['target_apex_kmh_more'] = round(r['apex_kmh_pb'] * (f - 1), 1)
        r['target_grip_at_apex'] = round(r['grip_used_at_apex_gg'] * f * f, 2)
    for r in sorted(rows_out, key=lambda r: -r['target_gain_s'])[:6]:
        km = (r['apex_d'] - args.d0) / 1000                  # from the start line, as the driver counts
        place = ('the %s %s at %.1f km' % (r['grade'], r['dir'], km) if r['grade'] != 'straight'
                 else 'the straight at %.1f km' % km)
        if r['grade'] == 'straight' or r['radius_m'] > 120:
            calls.append('%s: about %.1f s is there, mostly %s: %s.' % (
                place[0].upper() + place[1:], r['target_gain_s'], 'on the exit' if r['cause'] == 'exit' else
                'into the bend', 'full throttle sooner and longer' if r['cause'] == 'exit' else 'brake later'))
            continue
        how = {'entry': 'brake later and carry the speed to the turn-in',
               'apex': 'carry %.0f km/h more through the apex' % max(1, r['target_apex_kmh_more']),
               'exit': 'get to full throttle sooner after the apex'}[r['cause']]
        calls.append('%s: you use %s of the grip; about %.1f s is there. %s.' % (
            place[0].upper() + place[1:], 'all' if r['grip_used_at_apex_gg'] >= 0.97 else
            '%d %%' % round(100 * r['grip_used_at_apex_gg']), r['target_gain_s'],
            how[0].upper() + how[1:]))
    out['target_mode'] = {'gap_s': round(gap, 1), 'available_total_s': round(float(avail.sum()), 1),
                          'gap_over_available': round(gap / avail.sum(), 2), 'calls': calls}
    plot(grid, k, v_pot, vlim, V[pb], rows_out, out)
    name = 'lapsim_%s_%s_p%g%s%s%s.json' % (args.stage.split()[-1].lower(), args.car.split()[1].lower(), args.p,
                                            '' if args.floor else '_nofloor', '_before' if args.before else '',
                                            '_pos' if args.curv == 'pos' else '')
    with open(os.path.join(common.OUT, name), 'w') as f:
        json.dump(out, f, indent=1)
    print(json.dumps({k2: v2 for k2, v2 in out.items() if k2 != 'sections'}, indent=1))
    print('%-6s %-6s %-9s %-6s %7s %7s %7s %7s %7s %6s %6s %5s %5s %-6s %6s' % (
        'd0', 'd1', 'corner', 'R', 'car', 'grip', 'user', 'pb', 'avail', 'v_pb', 'v_pot', 'grip', 'mu', 'cause', 'tgt'))
    for r in rows_out:
        print('%-6d %-6d %-9s %6.0f %7.2f %7.2f %7.2f %7.2f %7.2f %6.1f %6.1f %5.2f %5s %-6s %6.2f' % (
            r['d0'], r['d1'], r['grade'] + ' ' + r['dir'][0], r['radius_m'], r['car_s'], r['potential_s'], r['best_s'],
            r['pb_s'],
            r['available_s'], r['apex_kmh_pb'], r['apex_kmh_potential'], r['grip_used_at_apex_gg'],
            r['grip_used_at_apex_forces'], r['cause'], r['target_gain_s']))


def car_envelope(rows, car, allr):
    """The car's capability: the highest grip any run of the car reached on
    the surface (P99.5 of lateral and braking g), and the drive the engine
    gives (the shipped torque curve, gear set as measured, through the
    measured rolling radius and mass, less the rolling and aero drag measured
    against the positions: g (0.0238 + 9.3e-5 v^2)), capped by the best
    traction seen (P99.5 of full-throttle g), and never below the drive
    any run showed at that speed."""
    import powertrain
    env = envelope(rows, 99.5)
    # under 8 m/s the 99.5th percentile is spins and knocks, not grip: hold the value at 8 m/s
    low = env['bins'] < 8.0
    for name in ('lat', 'brake'):
        env[name][low] = env[name][np.argmax(~low)]
    data = powertrain.car_data(car)
    prow = powertrain.collect('acr', car)
    driven = [0, 1] if data['drivetrain'] == 'fwd' else [0, 1, 2, 3]
    per_run = powertrain.wheel_ratios(prow, driven, by_run=True)
    sets = [powertrain.match_gear_set(m, data)[0] for m in per_run.values() if len(m) >= 3]
    sid = max(set(sets), key=sets.count)
    gs = next(g for g in data['gear_sets'] if g['id'] == sid)
    m = np.nanmedian(np.concatenate([c['x_wheel_load'][i].sum(1)[c['speed'][i] < 15]
                                     for _n, c, i, _tr, cc, _g in allr if cc == car])) / common.G
    r = 0.3265
    rpm_t, nm = zip(*data['torque_curve'])
    v = env['bins']
    best = np.zeros_like(v)
    for g in gs['gears']:
        G = g * gs['primary'] * data['final_drive']
        rpm = v * G / r * 60 / (2 * np.pi)
        ok = rpm <= data['limiter_rpm']
        rpm = np.maximum(rpm, 4000.0)            # below it the clutch slips at launch revs
        a = np.interp(rpm, rpm_t, nm) * G * data['gearbox_efficiency'] / (r * m)
        best = np.maximum(best, np.where(ok, a, 0.0))
    drag = common.G * (0.0238 + 9.3e-5 * v ** 2)
    observed = env['drive'].copy()
    # never below what the car was seen to do; never above the best traction seen
    env['drive'] = np.maximum(np.minimum(best - drag, np.max(observed)), observed)
    env['mass'] = round(float(m))
    env['gear_set'] = sid
    return env


def stage_surface(track):
    with open(os.path.join(common.ROOT, 'data/telemetry/stages/acr.json')) as f:
        for s in json.load(f)['stages']:
            if track and s['track'][:31].replace('ê', '_') == track:
                return s['surface']
    return None


def plot(grid, k, v_pot, vlim, v_pb, rows_out, out):
    fig, ax = plt.subplots(2, 1, figsize=(16, 7), sharex=True)
    ax[0].plot(grid, v_pb * 3.6, 'k', lw=0.8, label='PB run (captured) %.1f s' % out['pb_captured_s'])
    ax[0].plot(grid, v_pot * 3.6, 'C1', lw=0.8, label='potential %.1f s (P%.0f grip)' % (out['potential_s'],
                                                                                         out['percentile']))
    ax[0].plot(grid, np.minimum(vlim, 60) * 3.6, 'C1:', lw=0.5, label='corner limit')
    ax[0].set_ylim(0, 200)
    ax[0].set_ylabel('km/h')
    ax[0].legend(fontsize=8)
    ax[0].set_title('%s, %s: potential against the PB' % (out['stage'], out['car']), fontsize=9)
    for r in rows_out:
        ax[0].axvline(r['d0'], color='grey', lw=0.3)
    ax[1].bar([(r['d0'] + r['d1']) / 2 for r in rows_out], [r['available_s'] for r in rows_out],
              width=[r['d1'] - r['d0'] for r in rows_out], color='C0', alpha=0.6, edgecolor='k', lw=0.3)
    ax[1].set_ylabel('time available s')
    ax[1].set_xlabel('distance along the stage m')
    common.savefig(fig, 'lapsim_%s.png' % out['stage'].split()[-1].lower())


if __name__ == '__main__':
    main()
