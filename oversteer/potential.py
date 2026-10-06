"""Potential time per stage and car: where the time is, even for a driver whose every run is equally slow
(docs/telemetry-extrapolation.md, section 2; ported from research/extrapolation/lapsim.py).

A quasi-steady-state lap simulation over the course curvature, with the grip the driver's own runs show. Three
layers, user >= grip >= car:

  user  what the driver has done: the sum of the best section times (coach.stitch / coach_context.sum_of_best, the
        number the splits show; this module only adds the same quantity over its own sections)
  grip  the lap simulation at P98 of this driver's g-g envelope on the surface (never slower than the fastest
        speed any run reached at that distance)
  car   the same with the highest grip any run of the car reached (P99.5) and the engine from the shipped car data
        (data/telemetry/cars/<game>.json), through the gear set the runs used

The pieces, all pure over the trace's rows (telemetry_store.TRACE_CHANNELS, 10 Hz) and numpy:

  arrays(rows)                 a run's columns
  curvature(arr, grid, plan)   k(d) on a 2 m grid, from the positions (ACR: the plan view is (x, -z)) or yaw/speed
  envelope(rows, p)            the g-g envelope on speed bins
  simulate(k, env, floor)      the forward and backward passes
  sections(grid, k)            the cuts between apexes
  stage(...)                   everything for one stage and car, as the dict stored in the stage_potential table
  analyse_run(pot, arr)        one run against the stored potential: per section time, time available, grip used
                               at the apex, where the time is (entry, apex, exit) and the cause
  top3(...) and call(...)      the three places with the most time available, one fix each, in the coach's words

Storage (telemetry_store.POTENTIAL_DDL): `envelopes` (car, surface) and `stage_potential` (stage, car), recomputed
by after_run() when a run is written, on the drive-log thread (coach.stage_metrics is its caller: the coach's
post-run path outside drive_log). A reader serves the stored result (stage_view, span_times); nothing here
computes on a reader's thread but analyse_run, over one trace.
"""

import hashlib
import json
import logging
import math
import time
import warnings

import numpy as np

from . import car_data, coach_context as cc, stage_tables
from .telemetry_formats import plan_xy
from .telemetry_store import TRACE_CHANNELS

CH = {name: i for i, name in enumerate(TRACE_CHANNELS)}
G = cc.G

ALGO = 4                         # bumps when the numbers a stored potential holds would change
DS = 2.0                         # m between the grid's points
VBIN = 2.5                       # m/s per speed bin of the envelope
BIN_MIN = 10                     # rows a speed bin needs (the research had 40 at 60 Hz)
P_GRIP = 98.0                    # percentile of the driver's g-g: the lowest at which the potential beat every section
P_CAR = 99.5                     # the highest grip any run reached
SMOOTH_K = 10                    # grid points the curvature is smoothed over (20 m: 10 Hz rows are 3-7 m apart)
SMOOTH_TIGHT = 3                 # grid points the curvature is smoothed over (6 m) where the path is tighter than TIGHT_RADIUS
TIGHT_RADIUS = 40.0              # m (read through SMOOTH_K): a corner tighter than this is read through SMOOTH_TIGHT
SMOOTH_A = 1                     # rows the envelope's accelerations are smoothed over: none (a 10 Hz row is one packet; the research's 60 Hz
                                 # envelope was smoothed over 0.15 s, and 1 row here lands within 2 s of its P98 potential, 3 rows 4-5 s over it)
SMOOTH_AX = 3                    # rows the run's own lateral g is smoothed over where grip used is read
LAT_MIN = 3.0                    # m/s^2: a lateral envelope under this in a speed bin is straights, not grip
A_CLIP = 25.0                    # m/s^2: an acceleration beyond it is an impact, not grip
V_MIN = 3.0                      # m/s: slower has no curvature worth the name
MIN_SECTION = 120.0              # m
START_SLACK = 5.0                # m a run may start past the grid's start and still be timed through it
END_SLACK = 15.0                 # m it may stop short of the end
ENV_RUNS = 40                    # most recent runs of the car on the surface the envelope is built from
STAGE_RUNS = 30                  # most recent runs of the stage (any car) the road is built from
ENV_MIN_RUNS = 3                 # runs the envelope needs (docs section 6.1, the coach gate)
FLOOR_SPEED = 60.0               # m/s: no corner limit above this
LAUNCH_SPEED = 0.5               # m/s at the first grid point
SPOT = 25                        # grid points either side of the apex in which the run's slowest point is looked for
CAR_MASS_EXTRA = 300.0           # kg the crew and fuel add to the shipped mass (research: 1400 kg measured, 1100.3 shipped)
CAR_DRAG = (0.0238, 9.3e-5)      # g * (c0 + c2 v^2): rolling and aero drag measured against positions
LAUNCH_REVS = 0.53               # of the limiter: below it the clutch slips at launch (4000 of 7500 rpm)
RADIUS_FALLBACK = 0.3265         # m, where the car's data names no tyre radius
RANKED_CLASSES = ('clean', 'learning', 'off')        # the run classes whose times and roads the potential is built from
GEAR_SET_TOLERANCE = 0.04        # a gear set that fits within this on the gears above first is the one used

# The corner critique's costs (doc section 4.8, the i20N's, the one with the most passes): s per instance
COST_GEAR_SHORT = 0.17           # the apex gear shorter than the fastest pass's
COST_GEAR_LONG = 0.11            # longer
COST_REVS = 0.19                 # the exit's revs under the band (torque at least 90 % of the peak)
COST_COUNTER = 0.13              # s per second steered against the yaw
CRITIQUE_MIN = 0.15              # s: a term costing less is not quoted
BAND_SHARE = 0.9                 # of the peak torque
GRIP_QUOTE_MIN = 0.3             # grip used under this at the apex is a kink in a fast section: never quoted
GRIP_ALL = 0.97
AVAILABLE_MIN = 0.3              # s a place must have to be one of the top 3
TOP = 3
IMPROVED_MIN = 0.3               # s closer to the potential than the run before: said
CORNER_MAX_RADIUS = 120.0        # m: a longer bend is "a bend", not a corner with a grip percentage
OFF_REACH = cc.INCIDENT_REACH    # m either side of an off: a section there is no fair measure


# -- a run's columns --

def _fill(x):
    """x with its NaNs replaced by the nearest finite value (all NaN: zeros)."""
    good = np.isfinite(x)
    if good.all():
        return x
    if not good.any():
        return np.zeros_like(x)
    idx = np.where(good, np.arange(len(x)), 0)
    np.maximum.accumulate(idx, out=idx)
    out = x[idx]
    first = np.argmax(good)
    out[:first] = x[first]
    return out


def arrays(rows):
    """The columns of a run's stage rows as numpy arrays (`d` the distance made non-decreasing, `t` the seconds
    from the first row), or None for a trace too short to say anything."""
    if len(rows) < 30:
        return None
    a = np.asarray([tuple(r) + (math.nan,) * (len(TRACE_CHANNELS) - len(r)) for r in rows], dtype=float)
    d = a[:, CH['distance']]
    if not np.isfinite(d).any():
        return None
    d = np.maximum.accumulate(_fill(d))
    out = {name: a[:, CH[name]] for name in ('speed', 'rpm', 'gear', 'throttle', 'brake', 'steer', 'a_long', 'a_lat',
                                              'yaw_rate', 'x', 'y', 'z')}
    out['d'] = d
    out['t'] = a[:, CH['t']] - a[0, CH['t']]
    keep = np.r_[True, np.diff(d) > 1e-6]            # the first row at each distance: when the car first got there
    out['keep'] = keep
    return out


def _smooth(x, n):
    """Centred moving average over n points, ignoring NaNs (NaN where under a third of the window is finite)."""
    if n <= 1:
        return x.copy()
    k = np.ones(n)
    good = np.isfinite(x)
    num = np.convolve(np.where(good, x, 0.0), k, mode='same')
    den = np.convolve(good.astype(float), k, mode='same')
    with np.errstate(invalid='ignore', divide='ignore'):
        out = num / den
    out[den < n / 3.0] = np.nan
    return out


def resample(arr, col, grid):
    """`col` of a run (a name, or an array of its rows) at the distances of `grid`; NaN where the run was not
    there (more than END_SLACK past its last row, before its first) or the value is not there."""
    v = arr[col] if isinstance(col, str) else col
    d = arr['d']
    ok = arr['keep'] & np.isfinite(v)
    if ok.sum() < 2:
        return np.full(len(grid), np.nan)
    out = np.interp(grid, d[ok], v[ok], left=np.nan, right=np.nan)
    out[(grid > d[-1] + END_SLACK) | (grid < d[0] - START_SLACK)] = np.nan
    edge = (grid > d[ok][-1]) & (grid <= d[-1] + END_SLACK)
    out[edge] = v[ok][-1]
    start = (grid < d[ok][0]) & (grid >= d[0] - START_SLACK)
    out[start] = v[ok][0]
    return out


def elapsed(arr, grid):
    """Seconds from the run's first row to each grid distance (the first time it got there; 0 before its first
    row), NaN past its end by more than END_SLACK."""
    d, t = arr['d'], arr['t']
    out = np.interp(grid, d[arr['keep']], t[arr['keep']])
    out[grid > d[-1] + END_SLACK] = np.nan
    return out


def covers(arr, length):
    """Whether the run was timed from the start line to the end of the grid."""
    return arr['d'][0] <= START_SLACK and arr['d'][-1] + END_SLACK >= length


def smoothed_a(arr, n=None):
    """(a_long, a_lat) in m/s^2 smoothed over `n` rows (SMOOTH_A), NaN where an impact (beyond A_CLIP) or no value."""
    out = []
    for name in ('a_long', 'a_lat'):
        a = arr[name].copy()
        a[np.abs(a) >= A_CLIP] = np.nan
        out.append(_smooth(a, SMOOTH_A if n is None else n))
    return out


# -- the road --

def has_positions(arr):
    x, z = arr['x'], arr['z']
    good = np.isfinite(x) & np.isfinite(z) & (np.abs(x) + np.abs(z) > 0.1)
    return bool(good.mean() > 0.98)


def curvature(arr, grid, plan=None):
    """The path's curvature k (1/m, positive left) on the grid: from the positions where `plan` (the game's name,
    for its plan view: telemetry_formats.plan_xy; True: ACR) and the run has
    them, else yaw rate over speed. NaN where the run was slower than V_MIN or not there."""
    speed = resample(arr, 'speed', grid)
    if plan and has_positions(arr):
        x, y = plan_xy('acr' if plan is True else plan, (resample(arr, 'x', grid), resample(arr, 'y', grid),
                                                         resample(arr, 'z', grid)))
        x, y = _smooth(_fill(x), 5), _smooth(_fill(y), 5)
        heading = np.unwrap(np.arctan2(np.gradient(y), np.gradient(x)))
        s = np.r_[0.0, np.cumsum(np.hypot(np.diff(x), np.diff(y)))]
        k = np.gradient(_smooth(heading, 5), s + np.arange(len(s)) * 1e-6)
    else:
        yaw = arr['yaw_rate']
        k = resample(arr, np.where(arr['speed'] > V_MIN, yaw / np.maximum(arr['speed'], V_MIN), np.nan), grid)
    smooth = _smooth(k, SMOOTH_K)
    # a tight corner's whole turn is shorter than the window (a hairpin of 5.5 m radius turns 180 degrees in 17 m): the
    # window spreads it and the radius reads 8.8 m. Where the smoothed path is tight, a short window is the radius
    fine = _smooth(k, SMOOTH_TIGHT)
    tight = (np.abs(smooth) > 1 / TIGHT_RADIUS) & np.isfinite(fine)
    k = np.where(tight, fine, smooth)
    k[~(speed > V_MIN)] = np.nan
    return k


def road(ks):
    """The course's curvature: the median over the runs' (NaN where none was there: straight)."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        med = np.nanmedian(np.asarray(ks), axis=0)
    return _smooth(np.nan_to_num(med), 3)


# -- the grip --

def envelope_rows(arr):
    """(speed, a_long, a_lat, throttle, brake) of a run's usable rows, for envelope()."""
    ax, ay = smoothed_a(arr)
    ok = np.isfinite(ax) & np.isfinite(ay) & (arr['speed'] > V_MIN)
    return [arr['speed'][ok], ax[ok], ay[ok], np.nan_to_num(arr['throttle'])[ok], np.nan_to_num(arr['brake'])[ok]]


def envelope(rows, p):
    """a_lat_max(v), a_brake_max(v) and a_drive_max(v) on speed bins (m/s^2): the p-th percentile of |a_lat|, of
    -a_long while braking and of a_long at full throttle, `rows` being the runs' envelope_rows concatenated. The
    drive envelope never rises with speed (it is power-limited)."""
    v, ax, ay, thr, brk = rows
    bins = np.arange(0, 60, VBIN)
    out = {}
    for name, mask, val in (('lat', np.ones_like(v, bool), np.abs(ay)),
                            ('brake', (brk > 0.3) & (np.abs(ay) < 3.0), -ax),
                            ('drive', (thr > 0.95) & (np.abs(ay) < 3.0), ax)):
        e = np.full(len(bins), np.nan)
        for i, b in enumerate(bins):
            m = mask & (v >= b) & (v < b + VBIN) & np.isfinite(val)
            if m.sum() >= BIN_MIN:
                e[i] = np.percentile(val[m], p)
        if name == 'lat':
            e[e < LAT_MIN] = np.nan                  # a bin only straights were driven in has no grip in it: not measured
        good = np.isfinite(e)
        if not good.any():
            return None
        out[name] = np.interp(bins, bins[good], e[good])
    out['bins'] = bins + VBIN / 2
    out['drive'] = np.minimum.accumulate(np.maximum(out['drive'], 0.3))
    return out


def env_at(env, name, v):
    return np.interp(v, env['bins'], env[name])


def simulate(k, env, v0=LAUNCH_SPEED, floor=None):
    """(speed, corner limit) along the grid: the corner limit sqrt(a_lat_max / |k|), never below `floor` (the
    fastest speed any run reached there), then a forward pass at the drive envelope and a backward pass at the
    braking envelope, each limited by the lateral grip the curvature already uses (friction ellipse)."""
    n = len(k)
    ak = np.maximum(np.abs(k), 1e-5)
    vlim = np.full(n, FLOOR_SPEED)
    for _ in range(4):
        vlim = np.minimum(FLOOR_SPEED, np.sqrt(env_at(env, 'lat', vlim) / ak))
    if floor is not None:
        vlim = np.maximum(vlim, np.nan_to_num(floor))
    v = vlim.copy()
    v[0] = min(v0, vlim[0])
    lat, drive, brake = env['lat'], env['drive'], env['brake']
    bins = env['bins']
    for i in range(n - 1):
        ay = v[i] ** 2 * ak[i]
        frac = max(0.0, 1 - (ay / np.interp(v[i], bins, lat)) ** 2)
        a = np.interp(v[i], bins, drive) * math.sqrt(frac)
        v[i + 1] = min(vlim[i + 1], math.sqrt(v[i] ** 2 + 2 * a * DS))
    for i in range(n - 2, -1, -1):
        ay = v[i + 1] ** 2 * ak[i + 1]
        frac = max(0.0, 1 - (ay / np.interp(v[i + 1], bins, lat)) ** 2)
        a = np.interp(v[i + 1], bins, brake) * math.sqrt(frac)
        v[i] = min(v[i], math.sqrt(v[i + 1] ** 2 + 2 * a * DS))
    return v, vlim


def cumulative(v):
    """Seconds from the grid's start to each point of a speed profile."""
    return np.r_[0.0, np.cumsum(2 * DS / (v[1:] + v[:-1]))]


# -- the car layer --

def gear_set_of(arr_list, data, surface):
    """The id of the shipped gear set the runs used, from each gear's engine speed over road speed (scaled by the
    top gear measured, so the radius and the final drive drop out), or the default set where none fits. Counted
    over the runs."""
    votes = {}
    for arr in arr_list:
        v, rpm, gear = arr['speed'], arr['rpm'], arr['gear']
        steady = (arr['throttle'] > 0.3) & (np.nan_to_num(arr['brake']) < 0.05) & (v > 8) & (np.abs(
            np.nan_to_num(arr['yaw_rate'])) < 0.15)
        found = {}
        for g in range(1, 9):
            m = steady & (gear == g) & np.isfinite(rpm)
            if m.sum() >= 30:
                found[g] = float(np.median(rpm[m] / v[m]))
        if len(found) < 3:
            continue
        top = max(found)
        best = None
        for gs in data['gear_sets']:
            ratios = gs['gears']
            if top > len(ratios):
                continue
            errs = [abs((found[g] / found[top]) / (ratios[g - 1] / ratios[top - 1]) - 1) for g in found
                    if g != top and g >= 2]
            if not errs:
                continue
            e = float(np.mean(errs))
            if best is None or e < best[1]:
                best = (gs['id'], e)
        if best is not None and best[1] <= GEAR_SET_TOLERANCE:
            votes[best[0]] = votes.get(best[0], 0) + 1
    if votes:
        return max(votes, key=votes.get)
    return car_data.default_set(data)['id']


def tyre_radius(data, surface):
    radii = (data.get('tyre_radius_m') or {}).get(surface)
    return float(sum(radii) / len(radii)) if radii else RADIUS_FALLBACK


def car_envelope(rows, data, surface, set_id):
    """The car's capability: the highest grip any run reached on the surface (P99.5 of lateral and braking g, held
    at its value at 8 m/s below it, where the extreme is spins and knocks) and the drive the engine gives (the
    shipped torque curve through the gear set, rolling radius and mass, less the measured rolling and aero
    drag), capped by the best traction seen and never below what any run showed at that speed. None where the
    runs say nothing."""
    env = envelope(rows, P_CAR)
    if env is None:
        return None
    low = env['bins'] < 8.0
    for name in ('lat', 'brake'):
        env[name][low] = env[name][np.argmax(~low)]
    gs = next((g for g in data['gear_sets'] if g['id'] == set_id), car_data.default_set(data))
    mass = float(data.get('mass_kg') or 1100.0) + CAR_MASS_EXTRA
    r = tyre_radius(data, surface)
    rpm_t, nm = zip(*data['torque_curve'])
    v = env['bins']
    best = np.zeros_like(v)
    for g in gs['gears']:
        ratio = g * gs.get('primary', 1.0) * data['final_drive']
        rpm = v * ratio / r * 60 / (2 * math.pi)
        ok = rpm <= data['limiter_rpm']
        rpm = np.maximum(rpm, LAUNCH_REVS * data['limiter_rpm'])
        a = np.interp(rpm, rpm_t, nm) * ratio * data.get('gearbox_efficiency', 0.92) / (r * mass)
        best = np.maximum(best, np.where(ok, a, 0.0))
    drag = G * (CAR_DRAG[0] + CAR_DRAG[1] * v ** 2)
    observed = env['drive'].copy()
    env['drive'] = np.maximum(np.minimum(best - drag, np.max(observed)), observed)
    env['mass'] = round(mass)
    env['gear_set'] = set_id
    return env


def power_band(data):
    """(low, high) rpm between which the engine gives at least BAND_SHARE of its peak torque."""
    curve = data['torque_curve']
    peak = max(t for _r, t in curve)
    inside = [r for r, t in curve if t >= BAND_SHARE * peak]
    return (min(inside), max(inside)) if inside else None


# -- sections --

def grade_word(radius):
    for limit, g in cc.RADIUS_GRADES:
        if radius < limit:
            return str(g)
    return '6'


def sections(grid, k, min_len=MIN_SECTION):
    """[(first, last)] grid indices of the sections: cuts between apexes at the straightest point, a section under
    `min_len` m merged into its neighbour."""
    a = _smooth(np.abs(k), 15)
    a = np.nan_to_num(a)
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


# -- one run against the potential --

def _mode(values):
    values = [int(v) for v in values if np.isfinite(v) and v >= 1]
    return max(set(values), key=values.count) if values else None


def analyse_run(pot, arr, bad=(), data=None):
    """One run's sections against the stored potential `pot` (stage()'s dict): a list, one per section, of the
    stored fields plus `time` (s through it, None where the run did not cover it or an off was within OFF_REACH
    of it: `bad` is the off ranges (d0, d1)), `available` (time less the grip layer's), `apex_kmh`, `apex_kmh_pot`,
    `grip_used` (the run's lateral g at its slowest point near the apex over the envelope's at that speed),
    `loss` (s lost to the grip layer before the apex, at it, and after it), `cause` (the largest) and `gear` (the
    run's gear at the slowest point, and 40 m after it)."""
    prof = pot['profile']
    ds = prof['ds']
    v_pot = np.asarray(prof['v_grip'], dtype=float)
    grid = np.arange(len(v_pot)) * ds
    env = {'bins': np.asarray(prof['env_bins']), 'lat': np.asarray(prof['env_lat'])}
    V = resample(arr, 'speed', grid)
    AY = resample(arr, smoothed_a(arr, SMOOTH_AX)[1], grid)
    T = elapsed(arr, grid)
    GEAR = resample(arr, 'gear', grid)
    RPM = resample(arr, 'rpm', grid)
    THR = resample(arr, 'throttle', grid)
    inv = 1 / np.maximum(np.nan_to_num(V), 1.0) - 1 / np.maximum(v_pot, 1.0)
    out = []
    for s in pot['sections']:
        a, b = int(round(s['d0'] / ds)), int(round(s['d1'] / ds))
        b = min(b, len(grid) - 1)
        ap = int(round(s['apex_d'] / ds))
        row = dict(s, time=None, available=None, apex_kmh=None, apex_kmh_pot=None, grip_used=None, loss=None,
                   cause=None, gear=None, exit_rpm=None, exit_throttle=None)
        touched = any(lo - OFF_REACH < s['d1'] and hi + OFF_REACH > s['d0'] for lo, hi in bad)
        if np.isfinite(T[a]) and np.isfinite(T[b]) and T[b] > T[a] and not np.isnan(V[a:b + 1]).any():
            row['time'] = float(T[b] - T[a])
            row['available'] = row['time'] - s['grip_s']
            lo, hi = max(a, ap - SPOT), min(b, ap + SPOT)
            m = lo + int(np.argmin(V[lo:hi + 1]))
            v_ap = float(V[m])
            row['apex_kmh'] = round(v_ap * 3.6, 1)
            row['apex_kmh_pot'] = round(float(v_pot[m]) * 3.6, 1)
            near = AY[max(a, m - 2):min(b, m + 2) + 1]
            near = near[np.isfinite(near)]
            if near.size:
                row['grip_used'] = round(float(np.median(np.abs(near)) / env_at(env, 'lat', v_ap)), 2)
            e0, e1 = max(a, ap - 10), min(b, ap + 10)
            loss = {'entry': float(np.sum(inv[a:e0]) * ds), 'apex': float(np.sum(inv[e0:e1]) * ds),
                    'exit': float(np.sum(inv[e1:b]) * ds)}
            row['loss'] = {k2: round(v2, 2) for k2, v2 in loss.items()}
            row['cause'] = max(loss, key=loss.get)
            far = min(len(grid) - 1, m + int(round(40.0 / ds)))
            mid = min(len(grid) - 1, m + int(round(20.0 / ds)))
            row['gear'] = [_mode(GEAR[max(a, m - 5):m + 6]), _mode(GEAR[mid:far + 1])]
            window = slice(mid, far + 1)
            if np.isfinite(RPM[window]).any():
                row['exit_rpm'] = float(np.nanmedian(RPM[window]))
                row['exit_throttle'] = float(np.nanmedian(THR[window]))
        if touched:
            row['time'] = row['available'] = None
        out.append(row)
    return out


def span_times(pot, d0, d1):
    """(grip s, car s or None) the potential takes between two distances of the stage (the splits' bounds)."""
    prof = pot['profile']
    ds = prof['ds']
    out = []
    for name in ('v_grip', 'v_car'):
        v = prof.get(name)
        if not v:
            out.append(None)
            continue
        t = cumulative(np.asarray(v, dtype=float))
        grid = np.arange(len(t)) * ds
        a, b = np.interp([d0, d1], grid, t)
        out.append(float(b - a))
    return tuple(out)


# -- the words --

def where(s):
    """'the 3 right at 1.3 km' for a section (its apex, from the start line), 'the straight at 1.3 km' for none; the
    name of a split (on_splits) where it has one."""
    if s.get('name'):
        return s['name']
    km = s['apex_d'] / 1000.0
    if s['grade'] == 'straight':
        return 'the straight at {:.1f} km'.format(km)
    return 'the {} {} at {:.1f} km'.format(s['grade'], s['dir'], km)


def critique(s, best_gear, band, data=None):
    """The corner critique's terms that this section's pass shows and that cost at least CRITIQUE_MIN: [(cost s,
    sentence)], the costliest first. `best_gear` is the gear the fastest pass had at the slowest point, `band` the
    engine's power band (power_band) or None. Only what the data shows: a gear shorter than the fastest pass's,
    the exit's revs under the band, steering against the yaw for over a second (`counter`, set by the caller)."""
    terms = []
    gear = (s.get('gear') or [None, None])[0]
    if gear is not None and best_gear is not None and gear < best_gear and COST_GEAR_SHORT >= CRITIQUE_MIN:
        terms.append((COST_GEAR_SHORT, 'You were in {} at the apex where your fastest pass was in {}: a gear shorter '
                                       'than that costs about {:.1f} s.'.format(_ordinal(gear), _ordinal(best_gear),
                                                                                COST_GEAR_SHORT)))
    rpm, thr = s.get('exit_rpm'), s.get('exit_throttle')
    if band and rpm and thr is not None and thr > 0.8 and rpm < band[0] - 100 and COST_REVS >= CRITIQUE_MIN:
        terms.append((COST_REVS, 'The revs were under the power band on the exit ({:.0f} rpm, it starts at {:.0f}): '
                                 'about {:.1f} s.'.format(rpm, band[0], COST_REVS)))
    counter = s.get('counter_steer_s')
    if counter and counter * COST_COUNTER >= CRITIQUE_MIN:
        terms.append((counter * COST_COUNTER, '{:.1f} s of counter-steer in it, about {:.1f} s.'.format(
            counter, counter * COST_COUNTER)))
    return sorted(terms, key=lambda t: -t[0])


def _ordinal(n):
    return {1: '1st', 2: '2nd', 3: '3rd'}.get(n, '{}th'.format(n))


def call(s, term=None):
    """The fix for one place, in the coach's sentence: where, how much of the grip is used at the apex (not at a
    kink: GRIP_QUOTE_MIN), how much time is there, then one thing to do. `term` is the critique's costliest
    sentence, said in place of the general advice where it exists."""
    place = where(s)
    place = place[0].upper() + place[1:]
    avail = s['available']
    bend = s['grade'] == 'straight' or s['radius_m'] > CORNER_MAX_RADIUS
    cause = s.get('cause') or 'exit'
    if bend:
        head = '{}: about {:.1f} s is there, mostly {}'.format(
            place, avail, 'on the exit' if cause == 'exit' else 'into the bend')
        tail = term or ('full throttle sooner and longer.' if cause == 'exit' else 'brake later.')
        return head + (': ' if not term else '. ') + tail
    used = s.get('grip_used')
    grip = ''
    if used is not None and used >= GRIP_QUOTE_MIN:
        grip = 'you use {} of the grip; '.format('all' if used >= GRIP_ALL else '{:d} %'.format(round(100 * used)))
    how = term or {'entry': 'Brake later and carry the speed to the turn-in.',
                   'apex': 'The grip allows about {:.0f} km/h at the apex, you took {:.0f}.'.format(
                       s.get('apex_kmh_pot') or 0.0, s.get('apex_kmh') or 0.0),
                   'exit': 'Get to full throttle sooner after the apex.'}[cause]
    return '{}: {}about {:.1f} s is there. {}'.format(place, grip, avail, how)


def top3(rows, minimum=AVAILABLE_MIN, count=TOP):
    """The places with the most time available among `rows` (analyse_run's): the biggest `count` of at least
    `minimum` s, a section an off touched (time None) and the first section, which holds the launch, not among
    them, and the last section of a stage with no flying finish (the slow-down to the stop control), biggest
    first."""
    found = [r for n, r in enumerate(rows) if r['time'] is not None and r['available'] is not None
             and r['available'] >= minimum and r.get('split', n) > 0 and not r.get('tail')]
    return sorted(found, key=lambda r: -r['available'])[:count]


def improved(now_rows, before_rows, minimum=IMPROVED_MIN):
    """The section whose gap to the grip layer closed the most since the run before (both analyse_run's or on_splits'),
    as (row, seconds closer), or None under `minimum` s; the first section (the launch) is not one, as in top3."""
    best = None
    held = {b.get('split', n): b for n, b in enumerate(before_rows)}
    for n, a in enumerate(now_rows):
        b = held.get(a.get('split', n))
        if b is None or a['available'] is None or b['available'] is None or a.get('split', n) == 0 or a.get('tail'):
            continue                                     # the launch's section, and the slow-down's, are no places
        gain = b['available'] - a['available']
        if best is None or gain > best[1]:
            best = (a, gain)
    return best if best is not None and best[1] >= minimum else None


# -- the stage --

def _env_json(env):
    if env is None:
        return None
    out = {name: [round(float(x), 3) for x in env[name]] for name in ('bins', 'lat', 'brake', 'drive')}
    for name in ('mass', 'gear_set'):
        if name in env:
            out[name] = env[name]
    return out


def _version(parts):
    return hashlib.sha1('|'.join(str(p) for p in parts).encode()).hexdigest()[:12]


def stage(runs, data, surface, plan, env_rows, env_runs, sob=None, road_m=None, tail=False):
    """The potential of one stage for one car: `runs` [{'id', 'class', 'arr', 'mine' (a run of this car),
    'bad' (off ranges)}] the stage's runs (any car, for the road; the car's, `mine`, for the times and the floor),
    `data` the car's shipped entry or None, `env_rows` the car's envelope_rows on the surface (a list per run),
    `env_runs` their count, `sob` the splits' sum of best (coach.stitch) where the caller has it: the user layer,
    else this module's own sum of the best section times, `road_m` the length of the road a run drives where the stage's
    table knows it (the grid stops there: a run's trace goes on to the stop control) and `tail` that the stage has
    no flying finish, so its last section holds the slow-down to the stop control and is no place to say time of.
    Only runs classed clean, learning or off count for the road, the times and the floor (a 'partial' run holds the
    slow-down in its time). Returns the dict stored in stage_potential: {ds, length, runs, pb_run, pb_s, user_s,
    grip_s, car_s, gear_set, sections, profile}, or None where there is too little to say (no finished full run of
    the car, fewer than ENV_MIN_RUNS runs for the envelope)."""
    full = [r for r in runs if r['mine'] and r['finished'] and r['arr']['d'][-1] > 500
            and r['class'] in RANKED_CLASSES]
    if not full or env_runs < ENV_MIN_RUNS or not env_rows:
        return None
    length = float(np.median([r['arr']['d'][-1] for r in full]))
    if road_m:
        length = min(length, float(road_m))
    full = [r for r in full if covers(r['arr'], length - END_SLACK)]
    if not full:
        return None
    grid = np.arange(0.0, length, DS)
    ks = [curvature(r['arr'], grid, plan) for r in runs]
    k = road(ks)
    env = envelope([np.concatenate(c) for c in zip(*env_rows)], P_GRIP)
    if env is None:
        return None
    floor = np.nanmax([resample(r['arr'], 'speed', grid) for r in full], axis=0)
    v_grip, _vlim = simulate(k, env, floor=floor)
    t_grip = cumulative(v_grip)
    set_id = car_env = v_car = None
    if data is not None:
        set_id = gear_set_of([r['arr'] for r in runs if r['mine']], data, surface)
        car_env = car_envelope([np.concatenate(c) for c in zip(*env_rows)], data, surface, set_id)
    if car_env is not None:
        v_car, _vl = simulate(k, car_env, floor=floor)
        t_car = cumulative(v_car)
    T = np.array([elapsed(r['arr'], grid) for r in full])
    pick = [r for r in full if r['class'] == 'clean'] or [r for r in full if r['class'] == 'learning'] or full
    pb = min(pick, key=lambda r: r['arr']['t'][-1])
    pb_i = full.index(pb)
    cuts = sections(grid, k)
    pot = {'ds': DS, 'length': length, 'v_grip': [round(float(x), 2) for x in v_grip],
           'v_car': None if v_car is None else [round(float(x), 2) for x in v_car],
           'env_bins': [round(float(x), 2) for x in env['bins']], 'env_lat': [round(float(x), 3) for x in env['lat']]}
    secs, user = [], 0.0
    for a, b in cuts:
        seg = T[:, b] - T[:, a]
        for j, r in enumerate(full):                     # an off's surroundings are no section time to beat
            if any(lo - OFF_REACH < grid[b] and hi + OFF_REACH > grid[a] for lo, hi in r['bad']):
                seg[j] = np.nan
        best = float(np.nanmin(seg)) if np.isfinite(seg).any() else None
        ap = a + int(np.argmax(_smooth(np.abs(k), 5)[a:b + 1]))
        radius = 1 / max(abs(float(k[ap])), 1e-4)
        s = {'d0': round(float(grid[a])), 'd1': round(float(grid[b])), 'apex_d': round(float(grid[ap])),
             'dir': 'left' if k[ap] > 0 else 'right', 'radius_m': round(radius, 1),
             'grade': grade_word(radius) if radius < 300 else 'straight',
             'user_s': None if best is None else round(best, 2),
             'grip_s': round(float(t_grip[b] - t_grip[a]), 2),
             'car_s': None if v_car is None else round(float(t_car[b] - t_car[a]), 2)}
        # the gear of the fastest pass at the slowest point, for the critique
        if best is not None and np.isfinite(seg).any():
            fast = full[int(np.nanargmin(seg))]
            g = resample(fast['arr'], 'gear', grid[max(a, ap - 5):min(b, ap + 5) + 1])
            s['best_gear'] = _mode(g)
        user += best if best is not None else float(T[pb_i, b] - T[pb_i, a])
        secs.append(s)
    if tail and secs:
        secs[-1]['tail'] = True
    # a place the car has been through faster is no slower in a layer: each section's layers are held in order, and
    # the totals are the sections' (they cover the stage), so a total can never contradict its sections
    for s in secs:
        if s['user_s'] is not None:
            s['grip_s'] = min(s['grip_s'], s['user_s'])
        if s['car_s'] is not None:
            s['car_s'] = min(s['car_s'], s['grip_s'])
    t_grip_total = sum(s['grip_s'] for s in secs)
    t_car_total = None if v_car is None else sum(s['car_s'] for s in secs)
    totals = T[:, -1]
    out = {'ds': DS, 'length': length, 'runs': len(full), 'pb_run': pb['id'], 'pb_s': float(totals[pb_i]),
           'user_s': sob if sob is not None else user, 'grip_s': float(t_grip_total), 'car_s': t_car_total,
           'gear_set': set_id, 'sections': secs, 'profile': pot, 'percentile': P_GRIP, 'algo': ALGO,
           'envelopes': {'grip': _env_json(env), 'car': _env_json(car_env)}}
    # the layers keep their order (the floors make it hold per section; a total can still be off by a rounding)
    out['grip_s'] = min(out['grip_s'], out['user_s'])
    if out['car_s'] is not None:
        out['car_s'] = min(out['car_s'], out['grip_s'])
    # the PB run's own columns, for the stage view
    for s, mine in zip(secs, analyse_run(out, pb['arr'], pb['bad'])):
        for name in ('time', 'available', 'apex_kmh', 'apex_kmh_pot', 'grip_used', 'loss', 'cause'):
            s['pb_' + name if name == 'time' else name] = mine[name]
    return out


# -- the layers as the views show them --

def layers(pot, sob=None):
    """{'user', 'grip', 'car'} seconds, in order user >= grip >= car (None where a layer is not known). `sob` is the
    splits' sum of best, which stands for the user layer where given (the number is the same wherever it shows)."""
    user = sob if sob is not None else pot.get('user_s')
    grip, car = pot.get('grip_s'), pot.get('car_s')
    if user is not None and grip is not None:
        grip = min(grip, user)
    if grip is not None and car is not None:
        car = min(car, grip)
    return {'user': user, 'grip': grip, 'car': car}


# -- the store: reading --

def stored(reader, stage_key, car_id):
    """The stored potential of a stage and car (stage()'s dict, plus `version` and `built`), or None."""
    try:
        return reader.potential(stage_key, car_id)
    except Exception:
        logging.exception("potential: reading")
        return None


# -- the store: writing, on the drive-log thread --

def _surface_of(store, stage_key, run):
    entry = stage_tables.entry(stage_key)
    surface = stage_tables.surface_of(entry)[0] if entry else None
    return surface or run.get('surface')


def _bad_ranges(store, run_id):
    return [(e['d0'], e['d1'] if e['d1'] is not None else e['d0']) for e in store.events(run_id)
            if e['kind'] in ('off', 'spin', 'stall', 'hit') and e['d0'] is not None]


def _load(store, row, rows_fn=None):
    trace = store.trace(row['id'])
    if not trace:
        return None
    rows = cc.stage_rows(trace, row['course'], row['finished'] == 1, row['result_time'])
    return arrays(rows)


def recompute(store, stage_key, car_id, force=False, now=None, sob=None):
    """The envelope of the car on the stage's surface and the stage's potential, stored (not committed: the
    caller's transaction does that). Skipped when the runs they are built from are the ones the stored version
    was. `sob` is the splits' sum of best, the user layer (after_run finds it). Returns the potential dict or None."""
    car = store.car_by_id(car_id)
    if car is None:
        return None
    game = car.get('game')
    data = car_data.entry(car.get('key'))
    stage_rows = [r for r in store.stage_runs(stage_key, limit=STAGE_RUNS, ranked=True) if r['id'] is not None]
    mine_rows = [r for r in stage_rows if r['car'] == car_id and r['finished'] == 1]
    if not mine_rows:
        return None
    surface = _surface_of(store, stage_key, store.run(mine_rows[0]['id']) or {})
    env_rows = store.surface_runs(car_id, surface, ENV_RUNS) if surface else []
    entry = stage_tables.entry(stage_key) or {}
    acr = stage_key.startswith('acr:')
    road = stage_tables.road_length(entry) if acr else None
    tail = acr and not entry.get('finish_m')
    version = _version([ALGO, stage_key, car_id, surface, road, tail] + [
        (r['id'], r['run_class'], r['finished'], r['course'], r['result_time']) for r in stage_rows] + [
        (r['id'], r['run_class'], r['course']) for r in env_rows])
    held = store.potential(stage_key, car_id)
    if not force and held is not None and held['version'] == version:
        return held
    cache = {}

    def arr_of(r):
        if r['id'] not in cache:
            cache[r['id']] = _load(store, r)
        return cache[r['id']]

    runs = []
    for r in stage_rows:
        a = arr_of(r)
        if a is None:
            continue
        runs.append({'id': r['id'], 'class': r['run_class'], 'finished': r['finished'] == 1, 'arr': a,
                     'mine': r['car'] == car_id, 'bad': _bad_ranges(store, r['id']) if r['car'] == car_id else []})
    env_cols = [envelope_rows(a) for a in (arr_of(r) for r in env_rows) if a is not None]
    plan = 'acr' if acr else None
    pot = stage(runs, data, surface, plan, env_cols, len(env_cols), sob, road, tail)
    if pot is None:
        return None
    built = now if now is not None else time.time()
    envelopes = pot.pop('envelopes')
    store.save_potential(stage_key, car_id, version, built, pot)
    if surface:
        store.save_envelope(car_id, surface, game, _version([ALGO, car_id, surface] + [
            (r['id'], r['run_class'], r['course']) for r in env_rows]), len(env_cols), built, envelopes)
    return dict(pot, version=version, built=built)


def stitched(store, run):
    """The splits' stitch for `run`'s stage and car as coach.splits() has it with `run` the latest (coach.stitch of the
    same ranked runs: the best sections on the reference's grid), or None where there is no reference run or grid."""
    from . import coach
    before = [r for r in store.stage_runs(run['stage'], exclude=run['id'], limit=60, car=run['car'], ranked=True)
              if r['started'] <= run['started']]
    ref = cc.reference_run([r for r in before if r['run_class'] in ('clean', 'learning')], wet=run['wet'])
    if ref is None:
        return None
    return coach.stitch(store, run, ref, store.corners(ref['id']), before)


def sum_of_best(store, run):
    """The splits' sum of best for `run`'s stage and car as coach.splits() has it with `run` the latest, or None
    where there is no reference run or grid."""
    found = stitched(store, run)
    return None if found is None else found['possible']


def stage_sob(store, stage_key, car_id):
    """The splits' sum of best of a stage and car as the coach's splits() has it: with the car's newest run on the
    stage that the coach reads (a class, not a drift run) the latest. None where there is none."""
    for r in store.stage_runs(stage_key, car=car_id, limit=20):
        run = store.run(r['id'])
        if run is not None and run['run_class'] not in (None, 'restart', 'unclassified') \
                and run['discipline'] != 'drift':
            return sum_of_best(store, run)
    return None


def on_splits(pot, rows, found, run_id, bad=()):
    """analyse_run's rows (`rows`, on the potential's sections) put on the splits' sections (`found`, stitch()'s: the
    reference's grid), so that a place has one name, bounds and time wherever it is quoted: per split its name
    (coach_context.section_name), `d0` and `d1`, `time` (the run's, as the splits sheet has it; None where an off
    was within OFF_REACH of it: `bad`), `grip_s`, `car_s` and `available` through its bounds, and the fields of the
    potential section whose apex is in it (the one with the most time available where several are: the place to
    talk about) for the rest. A split no section's apex or bounds fall in is left out."""
    best = found['best']
    per = best['per_run'].get(run_id, {})
    out = []
    last = len(best['bounds']) - 1
    for j, (a, b) in enumerate(best['bounds']):
        inside = [r for r in rows if a <= r['apex_d'] < b] or [r for r in rows if r['d0'] < b and r['d1'] > a]
        if not inside:
            continue
        src = max(inside, key=lambda r: -math.inf if r['available'] is None else r['available'])
        t = per.get(j)
        if t is not None and any(lo - OFF_REACH < b and hi + OFF_REACH > a for lo, hi in bad):
            t = None
        grip_s, car_s = span_times(pot, a, b)
        row = dict(src, name=cc.section_name(best['grid'][j]), d0=a, d1=b, time=t, grip_s=grip_s, car_s=car_s,
                   available=None if t is None else t - grip_s, split=j,
                   tail=bool(j == last and pot['sections'] and pot['sections'][-1].get('tail')))
        out.append(row)
    return out


def after_run(store, run_id):
    """The post-run hook (coach.stage_metrics calls it from the drive-log thread): the potential of the run's stage
    and car, recomputed when the runs it is built from changed. Never raises: the run's other work goes on."""
    try:
        run = store.run(run_id)
        if run is None or not run['stage'] or run['car'] is None or run['finished'] != 1:
            return None
        return recompute(store, run['stage'], run['car'], sob=sum_of_best(store, run))
    except Exception:
        logging.exception("potential: after run %s", run_id)
        return None
