#!/usr/bin/env python3
"""Offs and crashes: find them, name why, say how to avoid it, and count
how often it happens.

1. Detection (on the 60 Hz capture; the 10 Hz trace version is what the
   coach's `coach_context.incidents()` already does): an incident ends in
   - a stop: under 3 m/s for 1 s or more past the first 100 m (or reverse
     engaged), or the run abandoned (a restart) after slowing under 3 m/s;
   - a roll: every tyre load under 50 N without free fall (ACR's loads:
     the car is on its roof or side);
   - a hit: over 3 g horizontal in the car frame for 3 samples (50 ms).
   Hits, rolls and stops within 5 s are one incident.
2. Loss of control: going back up to 6 s from the incident's end, the
   first sample where the car's body slip angle passes 20 degrees, it is
   in the air, or it hits something. Where none is found, the end itself.
3. Cause, from the seconds before the loss of control, against the
   reference: the median speed and braking of the clean runs of the same
   stage (same car where there are two, else any car), on the 5 m grid:
   - landing: in the air within 1.5 s before (a crest or a jump);
   - power: the throttle over 60 % with the driven wheels spinning (over
     20 % slip) or the slip angle growing on the gas past the slowest point;
   - lift / release: the slip angle grows within 1 s of the brake coming
     off (or a lift) with the throttle under 20 %: the rear let go as the
     load came off it;
   - entry speed: in a corner, more than 8 % faster than the reference
     over the 2 s before (with braking later than the reference by 15 m
     or more: 'braked late');
   - counter-steer late: the slip angle passes 15 degrees and the steering
     does not oppose the yaw for 0.4 s or more;
   - understeer: slip under 12 degrees, steering lock over 40 %, the car's
     path wider than the reference's at that place (it ran wide);
   - clipped: a hit with no slide, at the reference's speed (the line, a
     rock, the inside of a corner);
   - unknown.
   Each cause has a sentence in the coach's style, and incidents are
   counted per driver, per cause and per place.
4. Validation: every incident is drawn (out/offs_review/, not committed)
   and labelled by hand from the traces in labels.json; the classifier is
   scored against those labels and for agreement between repeated offs
   at the same place.

    python3 research/extrapolation/offs.py [--review]
"""
import json
import os
import sys
import warnings

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

import common  # noqa: E402
import corners as corner_mod  # noqa: E402

warnings.simplefilter('ignore')
STEP = 5.0
LABELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'offs_labels.json')

ADVICE = {
    'landing': ("Off after the crest at {place}: the car landed {how}. Lift or brake before the crest so it lands "
                "straight and on four wheels; full commitment comes after the landing."),
    'power': ("Off at {place}: the throttle went to {thr:.0%} while the car was still turning, and the driven "
              "wheels spun ({slip:.0%}). Wait for the car to point down the exit, then squeeze it on."),
    'lift': ("Off at {place}: the rear let go {dt:.1f} s after the {pedal}. Get the braking done before the "
             "turn-in, or release it progressively as you turn, so the load stays on the rear."),
    'entry_speed': ("Off at {place}: {dv:+.0f} km/h over your clean runs entering a {grade}. {brake}"
                    "Brake to your clean-run speed there; the time is made on the exit."),
    'counter_late': ("Off at {place}: the slide reached {slip:.0f} degrees and the counter-steer came {lag:.1f} s "
                     "after it started. Catch it earlier: steer into the slide as soon as the rear moves."),
    'understeer': ("Off at {place}: the car ran wide with {lock:.0%} lock and little slide (understeer). Enter "
                   "slower, or keep weight on the front (brake into the turn-in) so the nose bites."),
    'clipped': ("Hit at {place} with the car straight and at your usual speed: the line touched something "
                "(a rock, a bank, the inside). Leave a little more room there."),
    'unknown': "Off at {place}: the cause is not clear from the data.",
}


def place_name(track, d, corners):
    best = None
    for c in corners:
        if c['d0'] - 60 <= d <= c['d1'] + 30:
            best = c
            break
    if best is None:
        return '%.2f km' % (d / 1000), None
    grade = corner_mod.GRADES[corner_mod.classify(best['radius'], RADIUS_MEDIANS)]
    word = {'HP': 'hairpin'}.get(grade, grade)
    return '%s %s at %.2f km' % ('left' if best['dir'] > 0 else 'right', word, best['apex'] / 1000), best


RADIUS_MEDIANS = {0: 8.1, 1: 14.5, 2: 31.4, 3: 44.4, 4: 86.4, 5: 103.4}     # from corners.py (game notes)


def features(cap, idx, game):
    """Per-sample columns an incident needs."""
    t, v = cap['t'][idx], cap['speed'][idx]
    vel = cap['vel'][idx]
    beta = np.degrees(np.arctan2(vel[:, 1], np.maximum(np.abs(vel[:, 0]), 0.5)))
    beta = np.where(v > 4, beta, 0.0)
    if game == 'wrcg':
        # WRCG's car-frame velocity and steer sign are unverified (its forward vector points backwards, and
        # steer anti-correlates with yaw): no attitude-based cause there until they are
        beta = np.zeros(len(t))
    a = cap['accel'][idx]
    fz = cap['x_wheel_load'][idx]
    has_loads = np.isfinite(fz).all(1).any()
    if has_loads:
        air = (fz < 50).all(1)
        az = a[:, 2]
        roll = air & (common.smooth(az, 5) > -6.0) & (v > 1)
        air = air & ~roll
    else:
        air = (common.smooth(a[:, 2], 3) < -7.5) if np.isfinite(a[:, 2]).any() else np.zeros(len(t), bool)
        roll = np.zeros(len(t), bool)
    horiz = np.hypot(a[:, 0], a[:, 1]) / common.G
    hit = np.zeros(len(t), bool)
    if np.isfinite(horiz).any():
        big = np.nan_to_num(horiz) > 3.0
        run = 0
        for i, b in enumerate(big):
            run = run + 1 if b else 0
            if run >= 3:
                hit[i - 2:i + 1] = True
    rot = cap['wheel_rot'][idx]
    slip = np.full(len(t), np.nan)
    if np.isfinite(rot).any():
        car = cap['car'][idx[0]] or ''
        driven = [0, 1] if '208' in car else [0, 1, 2, 3]
        slip = np.abs(rot[:, driven]).mean(1) * 0.327 / np.maximum(v, 2) - 1
    elif np.isfinite(cap['wheel_speed'][idx]).any():
        slip = np.abs(cap['wheel_speed'][idx]).mean(1) / np.maximum(v, 2) - 1
    yaw = cap['yaw_rate'][idx]
    return {'t': t, 'v': v, 'd': cap['lap_distance'][idx], 'beta': beta, 'air': air, 'roll': roll, 'hit': hit,
            'thr': np.nan_to_num(cap['throttle'][idx]), 'brk': np.nan_to_num(cap['brake'][idx]),
            'steer': np.nan_to_num(cap['steer'][idx]), 'yaw': np.nan_to_num(yaw), 'gear': cap['gear'][idx],
            'slip': slip, 'fz': fz, 'k': (np.nan_to_num(yaw) / np.maximum(v, 3.0))}


def incidents(f, finished, stage_length):
    t, v, d = f['t'], f['v'], f['d']
    n = len(t)
    ends = []
    slow = v < 3.0
    i = 0
    while i < n:
        if not slow[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and slow[j + 1]:
            j += 1
        dur = t[j] - t[i]
        if d[i] - d[0] > 100 and (dur >= 1.0 or (np.nan_to_num(f['gear'][i:j + 1]) < 0).any() or j == n - 1):
            if not (finished and d[i] > stage_length - 150):
                ends.append((i, 'stop'))
        i = j + 1
    for mask, kind in ((f['roll'], 'roll'), (f['hit'], 'hit')):
        idx = np.flatnonzero(mask)
        if len(idx):
            for g in np.split(idx, np.flatnonzero(np.diff(idx) > 30) + 1):
                if d[g[0]] - d[0] > 100:
                    ends.append((g[0], kind))
    ends.sort()
    merged = []
    for i, kind in ends:
        if merged and t[i] - t[merged[-1]['end']] < 5.0:
            merged[-1]['kinds'].add(kind)
            continue
        merged.append({'end': i, 'kinds': {kind}})
    return merged


def air_events(f, i0, i1):
    """(start, end) of the flights in rows i0..i1 (end within the span)."""
    out = []
    idx = np.flatnonzero(f['air'][i0:i1 + 1]) + i0
    if len(idx):
        for g in np.split(idx, np.flatnonzero(np.diff(idx) > 2) + 1):
            out.append((g[0], g[-1]))
    return out


def loss_of_control(f, end):
    t = f['t']
    k0 = end
    while k0 > 0 and t[end] - t[k0 - 1] <= 6.0:
        k0 -= 1
    for k in range(k0, end + 1):
        if (abs(f['beta'][k]) > 20 and f['v'][k] > 5) or f['air'][k] or f['hit'][k] or f['roll'][k]:
            return k
    return max(0, end - 30)


def reference(clean, grid):
    """Median speed, curvature and braking flags of the clean runs on the grid."""
    vs, ks, bs = [], [], []
    for f in clean:
        vs.append(common.unwrap_resample(f['d'], f['v'], grid))
        ks.append(common.unwrap_resample(f['d'], np.abs(common.smooth(f['k'], 15)), grid))
        bs.append(common.unwrap_resample(f['d'], (f['brk'] > 0.1).astype(float), grid))
    if not vs:
        return None
    return {'v': np.nanmedian(vs, 0), 'k': np.nanmedian(ks, 0), 'b': np.nanmedian(bs, 0), 'n': len(vs)}


def at(grid, x, d):
    return float(np.interp(d, grid, x)) if np.isfinite(d) else np.nan


VERSION = 2
BLIND = '--blind' in sys.argv


def classify(f, inc, ref, grid, corners, track):
    if VERSION == 2:
        return classify_v2(f, inc, ref, grid, corners, track)
    end, lc = inc['end'], inc['lc']
    t, v, d = f['t'], f['v'], f['d']
    w0 = lc
    while w0 > 0 and t[lc] - t[w0 - 1] <= 4.0:
        w0 -= 1
    pre = slice(w0, lc + 1)
    place, corner = place_name(track, d[lc], corners)
    ev = {'place': place, 'd': round(float(d[lc])), 'beta_max': round(float(np.max(np.abs(f['beta'][lc:end + 1]))
                                                                            if end >= lc else 0), 1)}
    # landing
    flights = [e for e in air_events(f, max(0, lc - 90), lc) if f['t'][e[1]] - f['t'][e[0]] >= 0.12]
    if flights:
        k = flights[-1][1]
        land = np.nan
        if np.isfinite(f['fz']).all(1).any():
            m = np.nanmedian(np.nansum(f['fz'], 1)) / common.G
            land = np.nanmax(np.nansum(f['fz'][k:k + 30], 1)) / (m * common.G)
        ev.update(cause='landing', how=('heavily (%.1f g)' % land if land > 2.5 else
                                        'with %.0f degrees of slide' % abs(f['beta'][min(len(t) - 1, k + 20)])))
        return ev
    # speed against the reference over the 2 s before the loss
    s0 = lc
    while s0 > 0 and t[lc] - t[s0 - 1] <= 2.0:
        s0 -= 1
    dv = np.nan
    if ref is not None:
        rv = np.array([at(grid, ref['v'], x) for x in d[s0:lc + 1]])
        dv = float(np.nanmean(v[s0:lc + 1] - rv)) * 3.6
        rel = float(np.nanmean(v[s0:lc + 1] / rv))
    else:
        rel = np.nan
    ev['dv_kmh'] = round(dv, 1) if np.isfinite(dv) else None
    # braking against the reference: the onset nearest before the loss
    brake_late = None
    on = np.flatnonzero(f['brk'][w0:lc + 1] > 0.1)
    if ref is not None and len(on):
        my_on = d[w0 + on[0]]
        rb = ref['b']
        ks = [g for g in range(len(grid)) if my_on - 80 <= grid[g] <= d[lc] and rb[g] >= 0.5]
        if ks:
            brake_late = float(my_on - grid[ks[0]])
    ev['brake_late_m'] = round(brake_late, 1) if brake_late is not None else None
    beta = np.abs(f['beta'])
    slide_start = next((k for k in range(w0, end + 1) if beta[k] > 15), None)
    # power: throttle with wheelspin or slip growing on the gas past the slowest point
    vmin_k = w0 + int(np.argmin(v[pre])) if lc > w0 else lc
    win = slice(max(w0, vmin_k), lc + 1)
    thr_on = f['thr'][win] > 0.6
    spin = np.nan_to_num(f['slip'][win]) > 0.2
    if lc > w0 and thr_on.any() and (spin & thr_on).mean() > 0.2 and (slide_start is not None):
        ev.update(cause='power', thr=float(np.max(f['thr'][win])), slip=float(np.nanmax(f['slip'][win])))
        return ev
    # lift / release: the slide starts within 1 s of the brake (or the throttle) coming off
    if slide_start is not None:
        k = slide_start
        back = max(0, k - 60)
        brk_off = [j for j in range(back, k) if f['brk'][j] > 0.2 and f['brk'][j + 1] <= 0.2]
        thr_off = [j for j in range(back, k) if f['thr'][j] > 0.5 and f['thr'][j + 1] <= 0.2]
        if (brk_off or thr_off) and f['thr'][k] < 0.2 and not (np.isfinite(rel) and rel > 1.08):
            j = (brk_off or thr_off)[-1]
            ev.update(cause='lift', dt=float(t[k] - t[j]), pedal='brake' if brk_off else 'throttle')
            return ev
    # entry speed (and braking)
    in_corner = corner is not None or (ref is not None and at(grid, ref['k'], d[lc]) > 1 / 150)
    if np.isfinite(rel) and rel > 1.08 and in_corner:
        grade = place.split(' at ')[0] if corner is not None else 'corner'
        brake = ('You braked %.0f m later than in them. ' % brake_late) if brake_late and brake_late > 15 else ''
        ev.update(cause='entry_speed', dv=dv, grade=grade, brake=brake)
        return ev
    # counter-steer late
    if slide_start is not None:
        sgn = np.sign(f['yaw'][slide_start])
        opp = next((k for k in range(slide_start, min(end + 1, slide_start + 120))
                    if np.sign(f['steer'][k]) == -sgn and abs(f['steer'][k]) > 0.05), None)
        lag = (t[opp] - t[slide_start]) if opp is not None else (t[min(end, slide_start + 120)] - t[slide_start])
        if lag >= 0.4:
            ev.update(cause='counter_late', slip=float(beta[slide_start:end + 1].max()), lag=float(lag))
            return ev
    # understeer: little slide, big lock, wider than the reference
    lock = float(np.max(np.abs(f['steer'][pre]))) if lc > w0 else 0.0
    if beta[pre].max() < 12 and lock > 0.4 and ref is not None:
        k_me = float(np.nanmean(np.abs(f['k'][s0:lc + 1])))
        k_ref = float(np.nanmean([at(grid, ref['k'], x) for x in d[s0:lc + 1]]))
        if k_me < 0.85 * k_ref:
            ev.update(cause='understeer', lock=lock)
            return ev
    if 'hit' in inc['kinds'] and beta[pre].max() < 15 and (not np.isfinite(rel) or rel < 1.08):
        ev.update(cause='clipped')
        return ev
    ev.update(cause='unknown')
    return ev


def classify_v2(f, inc, ref, grid, corners, track):
    """v2 (after labelling v1's output): the cause tree reordered so the
    common rally offs are not all 'counter-steer late':
    landing (a flight of 0.08 s or more in the 1.5 s before) -> power ->
    understeer (big lock, little slide: the car ran wide) -> lift / brake
    mid-corner (the slide starts while braking with lock on, or within 1 s
    of a release) -> entry speed -> counter-steer late -> clipped ->
    unknown."""
    end, lc = inc['end'], inc['lc']
    t, v, d = f['t'], f['v'], f['d']
    w0 = lc
    while w0 > 0 and t[lc] - t[w0 - 1] <= 4.0:
        w0 -= 1
    pre = slice(w0, lc + 1)
    place, corner = place_name(track, d[lc], corners)
    beta = np.abs(f['beta'])
    ev = {'place': place, 'd': round(float(d[lc])), 'beta_max': round(float(beta[lc:end + 1].max()) if end >= lc else 0, 1)}
    flights = [e for e in air_events(f, max(0, lc - 90), lc) if t[e[1]] - t[e[0]] >= 0.08]
    if flights:
        k = flights[-1][1]
        land = np.nan
        if np.isfinite(f['fz']).all(1).any():
            m = np.nanmedian(np.nansum(f['fz'], 1)) / common.G
            land = np.nanmax(np.nansum(f['fz'][k:k + 30], 1)) / (m * common.G)
        ev.update(cause='landing', how=('heavily (%.1f g)' % land if land > 2.5 else
                                        'with %.0f degrees of slide' % beta[min(len(t) - 1, k + 20)]))
        return ev
    s0 = lc
    while s0 > 0 and t[lc] - t[s0 - 1] <= 2.0:
        s0 -= 1
    rel = dv = np.nan
    if ref is not None:
        rv = np.array([at(grid, ref['v'], x) for x in d[s0:lc + 1]])
        dv = float(np.nanmean(v[s0:lc + 1] - rv)) * 3.6
        rel = float(np.nanmean(v[s0:lc + 1] / rv))
    ev['dv_kmh'] = round(dv, 1) if np.isfinite(dv) else None
    brake_late = None
    on = np.flatnonzero(f['brk'][w0:lc + 1] > 0.1)
    if ref is not None and len(on):
        my_on = d[w0 + on[0]]
        ks = [g for g in range(len(grid)) if my_on - 80 <= grid[g] <= d[lc] and ref['b'][g] >= 0.5]
        if ks:
            brake_late = float(my_on - grid[ks[0]])
    ev['brake_late_m'] = round(brake_late, 1) if brake_late is not None else None
    slide_start = next((k for k in range(w0, end + 1) if beta[k] > 15), None)
    vmin_k = w0 + int(np.argmin(v[pre])) if lc > w0 else lc
    win = slice(max(w0, vmin_k), lc + 1)
    thr_on = f['thr'][win] > 0.6
    spin = np.nan_to_num(f['slip'][win]) > 0.2
    if lc > w0 and thr_on.any() and (spin & thr_on).mean() > 0.2 and slide_start is not None:
        ev.update(cause='power', thr=float(np.max(f['thr'][win])), slip=float(np.nanmax(f['slip'][win])))
        return ev
    # understeer: the car runs wide with the lock on and little slide
    tail = slice(max(w0, end - 120), end + 1)
    lock = float(np.max(np.abs(f['steer'][tail])))
    if lock > 0.35 and beta[tail].max() < 15:
        ev.update(cause='understeer', lock=lock)
        return ev
    if slide_start is not None:
        k = slide_start
        back = max(0, k - 60)
        # the brake applied after the turn-in (the lock was on before the pedal went down), still on at the slide
        onset = k
        while onset > back and f['brk'][onset - 1] > 0.1:
            onset -= 1
        braking_turning = f['brk'][k] > 0.3 and onset > back and abs(f['steer'][onset]) > 0.2
        brk_off = [j for j in range(back, k) if f['brk'][j] > 0.2 and f['brk'][j + 1] <= 0.2]
        thr_off = [j for j in range(back, k) if f['thr'][j] > 0.5 and f['thr'][j + 1] <= 0.2]
        if (braking_turning or ((brk_off or thr_off) and f['thr'][k] < 0.2)) and not (np.isfinite(rel) and rel > 1.08):
            if braking_turning:
                ev.update(cause='lift', dt=0.0, pedal='brake (still on with the lock on)')
            else:
                j = (brk_off or thr_off)[-1]
                ev.update(cause='lift', dt=float(t[k] - t[j]), pedal='brake' if brk_off else 'throttle')
            return ev
    in_corner = corner is not None or (ref is not None and at(grid, ref['k'], d[lc]) > 1 / 150)
    if np.isfinite(rel) and rel > 1.06 and in_corner:
        grade = place.split(' at ')[0] if corner is not None else 'corner'
        brake = ('You braked %.0f m later than in them. ' % brake_late) if brake_late and brake_late > 15 else ''
        ev.update(cause='entry_speed', dv=dv, grade=grade, brake=brake)
        return ev
    if slide_start is not None:
        sgn = np.sign(f['yaw'][slide_start])
        opp = next((k for k in range(slide_start, min(end + 1, slide_start + 120))
                    if np.sign(f['steer'][k]) == -sgn and abs(f['steer'][k]) > 0.05), None)
        lag = (t[opp] - t[slide_start]) if opp is not None else (t[min(end, slide_start + 120)] - t[slide_start])
        if lag >= 0.4:
            ev.update(cause='counter_late', slip=float(beta[slide_start:end + 1].max()), lag=float(lag))
            return ev
    if 'hit' in inc['kinds'] and beta[pre].max() < 15 and (not np.isfinite(rel) or rel < 1.08):
        ev.update(cause='clipped')
        return ev
    ev.update(cause='unknown')
    return ev


def clean_up(incs, f, finish_d):
    """v2 detection: no incident past the finish (the stop at the
    marshals), and an incident within 20 s and 150 m of the one before is
    its aftermath, not a new one."""
    out = []
    for inc in incs:
        dd, tt = f['d'][inc['end']], f['t'][inc['end']]
        if finish_d is not None and dd > finish_d - 30:
            continue
        if out and tt - f['t'][out[-1]['end']] < 20 and abs(dd - f['d'][out[-1]['end']]) < 150:
            out[-1]['kinds'] |= inc['kinds']
            continue
        out.append(inc)
    return out


def finish_of(track):
    """The finish along the spline: the 'Finish' pace note, else the stage table's flying finish."""
    stage = corner_mod.STAGES.get(track)
    notes = corner_mod.read_notes(stage) if stage else None
    for dd, toks in notes or []:
        if 'Finish' in toks:
            return dd
    try:
        with open(os.path.join(common.ROOT, 'data/telemetry/stages/acr.json')) as fh:
            for st in json.load(fh)['stages']:
                if st['track'][:31] == track:
                    return st.get('finish_m') or st.get('pacenote_last_m')
    except OSError:
        pass
    return None


def run_finished(f, length):
    return f['d'][-1] > length - 150


def main():
    global VERSION
    review = '--review' in sys.argv
    if '--v1' in sys.argv:
        VERSION = 1
    allr = common.all_runs('acr') + common.all_runs('wrcg')
    stages = {}
    for name, cap, idx, track, car, game in allr:
        key = (game, track or '%.0f m' % np.nanmax(cap['stage_length'][idx]))
        f = features(cap, idx, game)
        length = float(np.nanmax(cap['stage_length'][idx]))
        fin = run_finished(f, length)
        incs = incidents(f, fin, length)
        if VERSION == 2:
            incs = clean_up(incs, f, finish_of(track))
        for inc in incs:
            inc['lc'] = loss_of_control(f, inc['end'])
        stages.setdefault(key, []).append({'capture': name, 'car': car, 'f': f, 'incs': incs, 'finished': fin,
                                           'length': length, 'start': float(cap['meta'].get('started', 0)) +
                                           float(cap['t'][idx[0]])})
    rows = []
    sheet = []
    for (game, track), runs in stages.items():
        lo = min(r['f']['d'].min() for r in runs)
        hi = max(r['f']['d'].max() for r in runs)
        grid = np.arange(lo, hi, STEP)
        notes = corner_mod.read_notes(corner_mod.STAGES[track]) if track in corner_mod.STAGES else None
        allk = [common.unwrap_resample(r['f']['d'], common.smooth(r['f']['k'], 15), grid) for r in runs]
        corners = corner_mod.detect_corners(grid, common.smooth(np.nan_to_num(np.nanmedian(allk, 0)), 5))
        for r in runs:
            clean_same = [o['f'] for o in runs if o is not r and o['finished'] and not o['incs'] and o['car'] == r['car']]
            clean_any = [o['f'] for o in runs if o is not r and o['finished'] and not o['incs']]
            ref = reference(clean_same if len(clean_same) >= 2 else clean_any, grid)
            for n, inc in enumerate(r['incs']):
                ev = classify(r['f'], inc, ref, grid, corners, track)
                text = ADVICE[ev['cause']].format(**{k: v for k, v in ev.items()})
                near = [n for n in (notes or []) if ev['d'] - 150 <= n[0] <= ev['d'] + 20]
                if near:
                    ev['note'] = ', '.join(near[-1][1])
                    text += " (The game's note there: %s.)" % ev['note']
                rid = '%s@%.0f' % (r['capture'][:15], r['f']['t'][inc['end']])
                rows.append({'id': rid, 'game': game, 'track': track, 'car': r['car'], 'kinds': sorted(inc['kinds']),
                             'ref_runs': ref['n'] if ref else 0, 'start': r['start'], **{k: v for k, v in ev.items()
                                                                                          if k in ('place', 'd', 'cause', 'beta_max', 'dv_kmh', 'brake_late_m', 'note')},
                             'text': text})
                if review:
                    sheet.append((r['f'], inc, ref, grid, rid, ev))
    if review:
        if '--only' in sys.argv:
            only = sys.argv[sys.argv.index('--only') + 1]
            sheet = [x for x in sheet if x[4].startswith(only)]
        draw_sheets(sheet)
    # frequency: per driver (all of marth's runs), per cause and per place
    rows.sort(key=lambda x: x['start'])
    seen = {}
    for x in rows:
        key = (x['track'], x['cause'])
        prev = [y for y in seen.get(key, []) if abs(y - x['d']) < 60]
        x['same_cause_same_place_before'] = len(prev)
        x['same_cause_before'] = len(seen.get(key, []))
        seen.setdefault(key, []).append(x['d'])
    out = {'incidents': rows, 'by_cause': {}, 'validation': validate(rows, False),
           'validation_held_out': validate(rows, True)}
    for x in rows:
        out['by_cause'][x['cause']] = out['by_cause'].get(x['cause'], 0) + 1
    with open(os.path.join(common.OUT, 'offs%s.json' % ('' if VERSION == 2 else '_v1')), 'w') as fh:
        json.dump(out, fh, indent=1, default=float)
    print(json.dumps({'n': len(rows), 'by_cause': out['by_cause'], 'validation': out['validation'],
                      'validation_held_out': out['validation_held_out']}, indent=1))
    for x in rows:
        print('%-22s %-5s %-24s %-10s %-22s %s' % (x['id'], x['game'], (x['track'] or '')[:24], x['car'][:10],
                                                     ','.join(x['kinds']), x['cause']), '|', x['text'][:150])


def validate(rows, held_out=None):
    """Score against the hand labels: detection (share of detections that
    are real incidents), cause accuracy on the incidents with a clear
    label (strict, and counting the 'alt' cause), what the classifier says
    where the label is unclear."""
    if not os.path.exists(LABELS):
        return None
    with open(LABELS) as fh:
        labels = {k: v for k, v in json.load(fh).items() if not k.startswith('_')}
    if held_out is not None:
        labels = {k: v for k, v in labels.items() if bool(v.get('held_out')) == held_out}
    mine = [x for x in rows if x['id'] in labels]
    false = [x for x in mine if labels[x['id']]['cause'] == 'not_an_off']
    unclear = [x for x in mine if labels[x['id']]['cause'] == 'unclear']
    clear = [x for x in mine if labels[x['id']]['cause'] not in ('not_an_off', 'unclear')]
    strict = sum(1 for x in clear if x['cause'] == labels[x['id']]['cause'])
    loose = sum(1 for x in clear if x['cause'] in (labels[x['id']]['cause'], labels[x['id']].get('alt')))
    conf = {}
    for x in clear:
        a = labels[x['id']]['cause']
        conf.setdefault(a, {}).setdefault(x['cause'], 0)
        conf[a][x['cause']] += 1
    per_cause = {}
    for c in sorted(set(labels[x['id']]['cause'] for x in clear)):
        tp = sum(1 for x in clear if labels[x['id']]['cause'] == c and x['cause'] == c)
        n_true = sum(1 for x in clear if labels[x['id']]['cause'] == c)
        n_pred = sum(1 for x in clear if x['cause'] == c)
        per_cause[c] = {'labelled': n_true, 'recall': round(tp / max(1, n_true), 2),
                        'precision': round(tp / max(1, n_pred), 2) if n_pred else None}
    return {'detections': len(mine), 'unlabelled': len(rows) - len(mine), 'not_an_off': len(false),
            'not_an_off_ids': [x['id'] for x in false],
            'detection_precision': round(1 - len(false) / max(1, len(mine)), 3),
            'clear_labels': len(clear), 'cause_accuracy_strict': round(strict / max(1, len(clear)), 3),
            'cause_accuracy_with_alt': round(loose / max(1, len(clear)), 3),
            'unclear_labels': len(unclear),
            'unclear_said_unknown': sum(1 for x in unclear if x['cause'] == 'unknown'),
            'per_cause': per_cause, 'confusion_label_to_predicted': conf}


def draw_sheets(items, per=6):
    """Contact sheets for labelling by hand: 8 s before each incident's
    end; every channel scaled into one panel. Not committed (they show
    samples)."""
    folder = os.path.join(common.OUT, 'offs_review')
    os.makedirs(folder, exist_ok=True)
    for s0 in range(0, len(items), per):
        fig, axs = plt.subplots(3, 2, figsize=(16, 11))
        for ax, (f, inc, ref, grid, rid, ev) in zip(axs.ravel(), items[s0:s0 + per]):
            end, lc = inc['end'], inc['lc']
            t = f['t']
            k0 = end
            while k0 > 0 and t[end] - t[k0 - 1] <= 8.0:
                k0 -= 1
            sl = slice(k0, min(len(t), end + 60))
            tt = t[sl] - t[lc]
            ax.plot(tt, f['v'][sl] / 30, 'k', label='speed/30')
            if ref is not None:
                ax.plot(tt, np.interp(f['d'][sl], grid, ref['v']) / 30, 'k--', label='clean/30')
            ax.plot(tt, f['thr'][sl], 'g', label='thr')
            ax.plot(tt, f['brk'][sl], 'r', label='brk')
            ax.plot(tt, f['steer'][sl], 'b', label='steer+L')
            ax.plot(tt, f['beta'][sl] / 45, 'm', label='slip/45deg')
            ax.plot(tt, f['yaw'][sl] / 2, 'c', label='yaw/2')
            ax.plot(tt, f['air'][sl] * 1.6, 'y', lw=2, label='air')
            ax.plot(tt, f['hit'][sl] * 1.8, 'C1', lw=2, label='hit')
            ax.plot(tt, f['roll'][sl] * 1.7, 'C5', lw=2, label='roll')
            ax.plot(tt, np.clip(np.nan_to_num(f['slip'][sl]), -1, 2), 'C7', lw=0.8, label='drv slip')
            ax.axvline(0, color='k', lw=0.6)
            ax.set_ylim(-2, 2)
            ax.grid(alpha=0.3)
            ax.set_title('%s d=%s %s%s' % (rid, ev['d'], ev.get('note', '')[:40],
                                            '' if BLIND else ' | pred ' + ev['cause']), fontsize=8)
        axs[0, 0].legend(fontsize=6, ncol=4)
        fig.savefig(os.path.join(folder, 'sheet_%02d.png' % (s0 // per)), dpi=55, bbox_inches='tight')
        plt.close(fig)


if __name__ == '__main__':
    main()
