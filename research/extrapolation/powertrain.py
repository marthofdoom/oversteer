#!/usr/bin/env python3
"""Drivetrain and engine identification from the drive itself.

1. Gearing: in each gear, engine speed over road speed is the overall
   ratio over the rolling radius. Clustering rpm / speed gives every
   gear's ratio with no car data; with ACR's car data the same numbers
   name the gear set in use (setup inference) and the rolling radius.
2. Rolling radius from the wheels: ACR sends wheel spin (rad/s), so the
   free-rolling radius is speed over spin on an undriven or lightly
   loaded wheel.
3. The engine's full-load torque curve, two ways, against the curve in
   the game's own files (ACR car data):
   a. forces (ACR, ACC): the driven tyres' sum of fx times the radius
      over the overall ratio and the gearbox efficiency;
   b. accelerations only (any game): m a + drag = T(rpm) G eta / r, fitted
      jointly over every gear with T in 250 rpm bins and drag c0 + c2 v^2.
      Mass is not known in most games, so this gives T / m; its shape is
      compared after scaling to the shipped curve's peak.
   WRC Generations, which ships no car data, gets the gearing and the
   shape of its curve the generic way.

    python3 research/extrapolation/powertrain.py
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
RPM_BINS = np.arange(2500, 8001, 250)


def car_data(name):
    with open(os.path.join(common.ROOT, 'data/telemetry/cars/acr.json')) as f:
        for c in json.load(f)['cars']:
            if c['name'] == name:
                return c
    return None


def gear_ratios(rows):
    """{gear: median rpm per m/s} over steady driving in the gear (clutch
    in, no brake, little wheel slip): k = rpm / v."""
    out = {}
    for g in sorted(set(int(x) for x in rows['gear'] if x >= 1)):
        m = rows['steady'] & (rows['gear'] == g)
        if m.sum() > 200:
            out[g] = float(np.median(rows['rpm'][m] / rows['v'][m]))
    return out


def collect(game, car=None):
    """Steady, full-throttle and every-row columns over all runs of a car."""
    cols = {k: [] for k in ('rpm', 'v', 'gear', 'thr', 'brk', 'clutch', 'ax', 'fxd', 'rot', 'ws', 't', 'steer', 'run')}
    for n, (name, cap, idx, track, c, _g) in enumerate(common.all_runs(game, min_length=800)):
        if car is not None and c != car:
            continue
        cols['rpm'].append(cap['rpm'][idx])
        cols['v'].append(cap['speed'][idx])
        cols['gear'].append(cap['gear'][idx])
        cols['thr'].append(cap['throttle'][idx])
        cols['brk'].append(np.nan_to_num(cap['brake'][idx]))
        cols['clutch'].append(np.nan_to_num(cap['clutch'][idx]))
        a = cap['accel'][idx][:, 0]
        if game == 'wrcg' or not np.isfinite(a).any():
            # WRCG's packet accelerations do not match the motion (see the doc): differentiate the speed
            a = np.gradient(common.smooth(cap['speed'][idx], 9), cap['t'][idx])
            p = cap['pos'][idx]
            if np.isfinite(p).all():
                # gravity along the road from the positions (WRCG's world is z up): a + g sin(grade)
                s = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]
                z = common.smooth(p[:, 2], 61)
                grade = np.gradient(z, np.maximum(s, 0) + np.arange(len(s)) * 1e-6)
                a = a + common.G * np.clip(common.smooth(grade, 31), -0.4, 0.4)
        cols['ax'].append(common.smooth(np.clip(a, -30, 30), 9))
        cols['fxd'].append(cap['x_fx'][idx])
        cols['rot'].append(cap['wheel_rot'][idx])
        cols['ws'].append(cap['wheel_speed'][idx])
        cols['t'].append(cap['t'][idx])
        cols['steer'].append(np.nan_to_num(cap['steer'][idx]))
        cols['run'].append(np.full(len(idx), n))
    if not cols['rpm']:
        return None
    rows = {k: np.concatenate(v) for k, v in cols.items()}
    rows['steady'] = (rows['v'] > 8) & (rows['clutch'] < 0.1) & (rows['brk'] < 0.05) & (rows['rpm'] > 2500)
    return rows


def torque_from_accel(rows, ratios, r, eff, slip=None):
    """T(rpm)/m by least squares: a = sum_b T_b/m * [rpm in b] * G/(r) * eff - c0 - c2 v^2."""
    m = rows['steady'] & (rows['thr'] > 0.97) & (np.abs(rows['steer']) < 0.15) & np.isin(rows['gear'], list(ratios))
    if slip is not None:
        # engine-limited only: the driven wheels within 8 % of the road speed (not traction-limited)
        m &= np.abs(slip) < 0.08
    rpm, v, a, gear = rows['rpm'][m], rows['v'][m], rows['ax'][m], rows['gear'][m].astype(int)
    k = np.array([ratios[g] for g in gear])                          # rpm per m/s
    overall = k * r * 2 * np.pi / 60                                 # engine rad per wheel rad (G)
    b = np.digitize(rpm, RPM_BINS) - 1
    keep = (b >= 0) & (b < len(RPM_BINS) - 1)
    nb = len(RPM_BINS) - 1
    A = np.zeros((keep.sum(), nb + 2))
    A[np.arange(keep.sum()), b[keep]] = overall[keep] * eff / r
    A[:, nb] = -1.0
    A[:, nb + 1] = -v[keep] ** 2
    coef, *_ = np.linalg.lstsq(A, a[keep], rcond=None)
    counts = np.bincount(b[keep], minlength=nb)
    t_over_m = np.where(counts > 30, coef[:nb], np.nan)
    return t_over_m, coef[nb], coef[nb + 1]


def torque_from_forces(rows, ratios, r, eff, driven):
    m = rows['steady'] & (rows['thr'] > 0.97) & np.isin(rows['gear'], list(ratios))
    fx = rows['fxd'][m][:, driven].sum(1)
    k = np.array([ratios[int(g)] for g in rows['gear'][m]])
    overall = k * r * 2 * np.pi / 60
    T = fx * r / (overall * eff)
    b = np.digitize(rows['rpm'][m], RPM_BINS) - 1
    out = np.full(len(RPM_BINS) - 1, np.nan)
    for i in range(len(out)):
        s = b == i
        if s.sum() > 30:
            out[i] = np.median(T[s])
    return out


def wheel_ratios(rows, driven, by_run=False):
    """{gear: overall ratio} from engine spin over the driven wheels' mean
    spin: exact, whatever the slip and the tyre radius (ACR, Forza:
    wheel_rot)."""
    w = np.abs(rows['rot'][:, driven]).mean(1)
    G = rows['rpm'] * 2 * np.pi / 60 / np.maximum(w, 1.0)
    runs = np.unique(rows['run']) if by_run else [None]
    out = {}
    for run in runs:
        sel = rows['steady'] & (rows['thr'] > 0.2) & ((rows['run'] == run) if run is not None else True)
        out[run] = {g: float(np.median(G[sel & (rows['gear'] == g)])) for g in range(1, 8)
                    if (sel & (rows['gear'] == g)).sum() > 30}
    return out if by_run else out[None]


def match_gear_set(measured, data):
    """(set id, mean relative error %) of the shipped gear set nearest the measured overall ratios."""
    best = None
    for gs in data['gear_sets']:
        errs = [abs(measured[g] / (gs['gears'][g - 1] * gs['primary'] * data['final_drive']) - 1)
                for g in measured if g <= len(gs['gears'])]
        e = float(np.mean(errs)) * 100
        if best is None or e < best[1]:
            best = (gs['id'], round(e, 2))
    return best


def main():
    res = {}
    fig, axs = plt.subplots(1, 4, figsize=(18, 4.5))
    mids = (RPM_BINS[:-1] + RPM_BINS[1:]) / 2
    for col, car in enumerate(('Hyundai i20N Rally2', 'Skoda Fabia RS Rally2', 'Peugeot 208 Rally4')):
        data = car_data(car)
        rows = collect('acr', car)
        driven = [0, 1] if data['drivetrain'] == 'fwd' else ([2, 3] if data['drivetrain'] == 'rwd' else [0, 1, 2, 3])
        free = [2, 3] if data['drivetrain'] == 'fwd' else [0, 1, 2, 3]
        per_run = wheel_ratios(rows, driven, by_run=True)
        sets = [match_gear_set(m, data) for m in per_run.values() if len(m) >= 3]
        counts = {}
        for sid, e in sets:
            counts.setdefault(sid, []).append(e)
        # free-rolling radius: the undriven wheels of the 208; all four off the throttle otherwise
        m = (rows['v'] > 10) & (rows['brk'] < 0.05) & (np.abs(rows['steer']) < 0.05)
        if data['drivetrain'] != 'fwd':
            m &= rows['thr'] < 0.05
        r = float(np.median(rows['v'][m][:, None] / np.abs(rows['rot'][m][:, free])))
        eff = data['gearbox_efficiency']
        # per row: the overall ratio of the gear set this run used
        G_row = np.full(len(rows['rpm']), np.nan)
        for run, meas in per_run.items():
            if len(meas) < 3:
                continue
            sid = match_gear_set(meas, data)[0]
            gs = next(g for g in data['gear_sets'] if g['id'] == sid)
            sel = rows['run'] == run
            for g in range(1, len(gs['gears']) + 1):
                G_row[sel & (rows['gear'] == g)] = gs['gears'][g - 1] * gs['primary'] * data['final_drive']
        shipped = np.interp(mids, *zip(*data['torque_curve']))
        # a. forces
        full = rows['steady'] & (rows['thr'] > 0.97) & np.isfinite(G_row)
        T = rows['fxd'][full][:, driven].sum(1) * r / (G_row[full] * eff)
        b = np.digitize(rows['rpm'][full], RPM_BINS) - 1
        t_f = np.array([np.median(T[b == i]) if (b == i).sum() > 30 else np.nan for i in range(len(mids))])
        # b. accelerations only, engine-limited rows
        slip = np.abs(rows['rot'][:, driven]).mean(1) * r / np.maximum(rows['v'], 1) - 1
        ratios = {g: v * 60 / (2 * np.pi * r) for g, v in wheel_ratios(rows, driven).items()}   # rpm per m/s
        t_a, c0, c2 = torque_from_accel(rows, ratios, r, eff, slip)
        okf, oka = np.isfinite(t_f), np.isfinite(t_a)
        scale = np.nanmax(shipped[oka]) / np.nanmax(t_a) if oka.any() else np.nan
        res[car] = {
            'overall_ratio_measured_vs_shipped': {g: [round(v, 3), round(
                next(gs for gs in data['gear_sets'] if gs['id'] == max(counts, key=lambda k: len(counts[k])))['gears'][g - 1]
                * data['final_drive'], 3)] for g, v in wheel_ratios(rows, driven).items()},
            'gear_set_per_run': {sid: {'runs': len(e), 'mean_err_pct': round(float(np.mean(e)), 2)}
                                 for sid, e in counts.items()},
            'gear_sets_offered': [gs['id'] for gs in data['gear_sets']],
            'free_rolling_radius_m': round(r, 4), 'shipped_radius_m': data['tyre_radius_m'],
            'torque_forces_vs_shipped': {
                'bins': int(okf.sum()), 'r': round(float(np.corrcoef(t_f[okf], shipped[okf])[0, 1]), 3),
                'mean_ratio': round(float(np.mean(t_f[okf] / shipped[okf])), 3),
                'mean_abs_err_pct': round(float(np.mean(np.abs(t_f[okf] / shipped[okf] - 1)) * 100), 1),
                'mean_abs_err_pct_3500_7000': round(float(np.mean(np.abs(
                    t_f[okf & (mids >= 3500) & (mids <= 7000)] / shipped[okf & (mids >= 3500) & (mids <= 7000)] - 1))
                    * 100), 1)},
            'torque_accel_only_shape': {
                'bins': int(oka.sum()),
                'r': round(float(np.corrcoef(t_a[oka], shipped[oka])[0, 1]), 3) if oka.sum() > 3 else None,
                'mean_abs_err_pct_after_scaling': round(float(np.mean(np.abs(t_a[oka] * scale / shipped[oka] - 1)) * 100), 1)
                if oka.sum() > 3 else None,
                'implied_mass_kg': round(float(scale), 0) if np.isfinite(scale) else None,
                'peak_rpm_est': float(mids[oka][np.argmax(t_a[oka])]) if oka.any() else None,
                'peak_rpm_shipped': float(mids[np.argmax(shipped)])},
            'shipped_mass_kg': data['mass_kg'],
        }
        ax = axs[col]
        ax.plot(mids, shipped, 'k', label='game files')
        ax.plot(mids, t_f, 'C0o-', ms=3, label='tyre forces')
        if oka.any():
            ax.plot(mids, t_a * scale, 'C1s-', ms=3, label='accel only (scaled to the peak)')
        ax.set_title(car, fontsize=9)
        ax.set_xlabel('rpm')
        ax.set_ylabel('Nm')
        ax.legend(fontsize=7)
    # WRC Generations: the generic route only (wheel speeds, no wheel spin, no car data)
    rows = collect('wrcg')
    ws = np.nanmean(rows['ws'], axis=1)
    slip = ws / np.maximum(rows['v'], 1) - 1
    ratios = gear_ratios(rows)
    r = 0.33
    t_a, c0, c2 = torque_from_accel(rows, ratios, r, 0.92, slip - np.nanmedian(slip[rows['steady']]))
    oka = np.isfinite(t_a)
    shifts = {}
    for g in sorted(ratios)[:-1]:
        if g + 1 not in ratios:
            continue
        best_v = None
        for v in np.arange(5, 60, 0.25):
            r1, r2 = ratios[g] * v, ratios[g + 1] * v
            if r1 > mids[oka][-1]:
                break
            if r1 < 3000 or r2 < mids[oka][0]:
                continue
            if np.interp(r2, mids[oka], t_a[oka]) * ratios[g + 1] >= np.interp(r1, mids[oka], t_a[oka]) * ratios[g]:
                best_v = v
                break
        shifts[g] = round(float(ratios[g] * best_v)) if best_v else 'at or past the last bin (limiter)'
    res['wrcg (7900 rpm, 6 gears)'] = {
        'rpm_per_kmh': {g: round(k / 3.6, 2) for g, k in ratios.items()},
        'gear_steps': {g: round(ratios[g] / ratios[g + 1], 3) for g in ratios if g + 1 in ratios},
        'wheel_speed_over_speed_median': round(float(np.nanmedian((ws / rows['v'])[rows['steady']])), 3),
        'torque_shape_bins': int(oka.sum()),
        'torque_peak_rpm': float(mids[oka][np.argmax(t_a[oka])]) if oka.any() else None,
        'shape_over_peak': {int(m): round(float(t / np.nanmax(t_a)), 2) for m, t in zip(mids[oka], t_a[oka])},
        'crossover_upshift_rpm': shifts}
    ax = axs[3]
    if oka.any():
        ax.plot(mids[oka], t_a[oka] / np.nanmax(t_a), 'C1s-', ms=3)
    ax.set_title('WRC Generations: torque shape, accel only', fontsize=9)
    ax.set_xlabel('rpm')
    ax.set_ylabel('relative torque')
    common.savefig(fig, 'powertrain.png')
    with open(os.path.join(common.OUT, 'powertrain.json'), 'w') as f:
        json.dump(res, f, indent=1)
    print(json.dumps(res, indent=1))


if __name__ == '__main__':
    main()
