"""What a run's positions say about the road and the car (ClickUp 86e3kfj8v): the stage's elevation profile with its
gradient and crests, and the drift (body slip) angle through each corner.

Pure over the trace's rows (telemetry_store.TRACE_CHANNELS, 10 Hz) and numpy; nothing is stored, the Run view
(run_analysis.analysis) and the coach compute them from the trace.

  elevation(rows, game)       height against distance on a STEP m grid (smoothed), the gradient in % per SECTION_M
                              section, the steepest gradient and the crests; None where the game's height axis is
                              not checked or the run has no positions
  drift(rows, game, corners)  per corner the peak and mean angle between where the car points and where it travels,
                              in degrees, positive when it is angled into the corner (rotated past its path)

Height is the second of telemetry_formats.plan_xyz (ACR and DiRT: y up; WRC Generations and Richard Burns Rally:
z up), so one place knows each game's axes. Both derive from the positions; a position that jumps (a reset, a
teleport) is rejected, and after a lasting one (a reset) the new place's absolute height stands.

Drift needs the car's own heading as well as its path, and the trace carries no heading, only the yaw rate: the
heading is the yaw rate's integral, anchored to the path's direction on the straights (where the car points where it
goes), so the sensor's drift between straights is corrected and a long corner is read against the straight before
and after it. A game whose yaw rate is itself derived from the positions (WRC Generations) would give a drift of
zero by construction and is NOT AVAILABLE (DRIFT_GAMES).
"""

import math

import numpy as np

from .telemetry_formats import plan_xyz
from .telemetry_store import TRACE_CHANNELS

CH = {name: i for i, name in enumerate(TRACE_CHANNELS)}

HEIGHT_GAMES = {'acr', 'dirt', 'wrcg', 'rbr'}     # plan_xyz's axes are checked (potential.PLAN_VERIFIED)
DRIFT_GAMES = {'acr'}                             # the yaw rate is the game's own, left positive (see drift())

STEP = 2.0                  # m between the profile's points
SECTION_M = 100.0           # m per gradient section
SMOOTH_M = 20.0             # m of the moving average on the height (the road's noise is a few cm per sample)
JUMP_M = 40.0               # m of horizontal jump beyond the distance travelled: a reset, not driving
JUMP_ROWS = 5               # rows in a row beyond it before the new place is the road (a real teleport)
MAX_GRADE = 45.0            # % a rally road never exceeds: a sample steeper than this over STEP is noise
CREST_UP = 1.0              # % the road must be rising by into a crest
CREST_BREAK = 6.0           # percentage points of gradient lost over the crest (convex profile, then a drop)
CREST_SPAN_M = 40.0         # m either side the two gradients are averaged over
CREST_MIN_GAP_M = 60.0      # m between two crests (the sharper one stays)

V_MIN = 8.0                 # m/s: below it the path's direction is noise
STRAIGHT_YAW = 0.06         # rad/s: a straight row (the car points where it goes), on the yaw median-filtered over YAW_MEDIAN rows
YAW_MEDIAN = 5              # rows (0.5 s): ACR's yaw rate jitters 0.03-0.05 rad/s on a straight, a raw threshold never holds
ANCHOR_S = 0.5              # s of straight a heading anchor needs
ANCHOR_REACH_S = 20.0       # s from the nearest anchor beyond which the integrated heading is not trusted (it drifts)
MIN_ANCHORS = 2             # one anchor would apply one offset to the whole stage
DRIFT_MAX = 100.0           # degrees: a reading beyond it is a reversal or a spin, not a drift angle
MIN_CORNER_ROWS = 3


def _arrays(rows):
    return {name: np.array([r[CH[name]] for r in rows], dtype=float) for name in
            ('t', 'distance', 'speed', 'yaw_rate', 'x', 'y', 'z')}


def _along(d):
    """The distance made non-decreasing (a car backing up does not undo the road), NaN filled forward."""
    out, top = np.empty(len(d)), -math.inf
    for i, v in enumerate(d):
        if math.isfinite(v):
            top = max(top, v)
        out[i] = top
    return out


def _smooth(x, n):
    n = int(n)
    if n <= 1:
        return x.copy()
    pad = np.pad(x, (n // 2, n - 1 - n // 2), mode='edge')
    return np.convolve(pad, np.ones(n) / n, mode='valid')


def _median(x, n):
    """A running median over n rows (odd), edges kept."""
    if n <= 1 or len(x) < n:
        return x.copy()
    pad = np.pad(x, (n // 2, n // 2), mode='edge')
    return np.median(np.lib.stride_tricks.sliding_window_view(pad, n), axis=1)


def _median3(x):
    """A 3-point running median: a one-row spike goes, a ramp stays."""
    if len(x) < 3:
        return x.copy()
    out = x.copy()
    out[1:-1] = np.median(np.stack((x[:-2], x[1:-1], x[2:])), axis=0)
    return out


def _clean_path(game, arr):
    """The rows that are the road, as arrays (distance, east, height, north): positions that are finite and not
    zero (plan_xyz's east and north), no jump beyond what the distance covered (JUMP_M); after a lasting jump (a
    reset) the new place's absolute height stands: the car is back on the road, wherever it fell to. None without
    positions."""
    d = _along(arr['distance'])
    keep = []
    last = None
    bad = 0
    out = []
    for i in range(len(d)):
        pos = (arr['x'][i], arr['y'][i], arr['z'][i])
        if not all(math.isfinite(v) for v in pos) or not math.isfinite(d[i]):
            continue
        e, h, n = plan_xyz(game, pos)
        if abs(e) + abs(n) < 0.1:
            continue                                       # a position the game left empty
        if last is not None:
            hop = math.hypot(e - last[1], n - last[3])
            if hop > JUMP_M + 3.0 * max(0.0, d[i] - last[0]):
                bad += 1
                if bad < JUMP_ROWS:
                    continue
            bad = 0
        last = (d[i], e, h, n)                             # after a lasting jump the new place's own height stands
        out.append(last)
        keep.append(i)
    if len(out) < 3:
        return None, []
    return np.array(out).T, keep


def _grid_profile(dist, vals, length):
    """`vals` on a STEP m grid from 0 to `length`, linear between rows (the first of rows at one distance)."""
    d, first = np.unique(dist, return_index=True)
    grid = np.arange(0.0, length + 1e-6, STEP)
    return grid, np.interp(grid, d, vals[first])


def elevation(rows, game):
    """{step, length, h (m on the grid from the first height), gradient (% on the grid), range (m), max_grade (%,
    the steepest section), net (m, end minus start), sections [{d0, d1, grade}] (SECTION_M each), crests [{d, h,
    before, after, drop}] sorted by distance}, or None where the game's height is not checked or the run has no
    positions. `rows` are the stage's trace rows."""
    if game not in HEIGHT_GAMES or not rows:
        return None
    path, _ = _clean_path(game, _arrays(rows))
    if path is None or path[0][-1] - path[0][0] < 3 * STEP:
        return None
    dist, h = path[0], path[2]
    grid, hh = _grid_profile(dist, h, dist[-1])
    hh = _median3(hh)
    hh = _smooth(hh, SMOOTH_M / STEP)
    hh = hh - hh[0]
    grade = np.gradient(hh, STEP) * 100.0
    # a profile steeper than any road over one step is noise or a join: flatten it
    grade = np.clip(grade, -MAX_GRADE, MAX_GRADE)
    sections = []
    d0 = 0.0
    while d0 < grid[-1] - 1e-6:
        d1 = min(d0 + SECTION_M, grid[-1])
        if d1 - d0 >= SECTION_M / 2 or not sections:
            i0, i1 = int(round(d0 / STEP)), int(round(d1 / STEP))
            sections.append({'d0': d0, 'd1': d1, 'grade': float((hh[i1] - hh[i0]) / (d1 - d0) * 100.0)})
        else:
            sections[-1]['d1'] = d1
            sections[-1]['grade'] = float((hh[-1] - hh[int(round(sections[-1]['d0'] / STEP))])
                                          / (d1 - sections[-1]['d0']) * 100.0)
        d0 = d1
    return {'step': STEP, 'length': float(grid[-1]), 'h': hh, 'gradient': grade,
            'range': float(hh.max() - hh.min()), 'net': float(hh[-1] - hh[0]),
            'max_grade': float(max((abs(s['grade']) for s in sections), default=0.0)),
            'sections': sections, 'crests': crests(grid, hh)}


def crests(grid, hh):
    """The profile's crests, sorted by distance: a local maximum of the smoothed height where the road was rising
    (the mean gradient over CREST_SPAN_M before at least CREST_UP %) and the gradient falls by CREST_BREAK
    percentage points or more across it (the mean after against the mean before). [{d, h, before, after, drop}]."""
    n = len(hh)
    k = int(CREST_SPAN_M / STEP)
    found = []
    for i in range(1, n - 1):
        if not (hh[i] >= hh[i - 1] and hh[i] > hh[i + 1]):
            continue
        a, b = max(0, i - k), min(n - 1, i + k)
        if i - a < k // 2 or b - i < k // 2:
            continue                                   # the stage's own ends are not crests
        before = (hh[i] - hh[a]) / ((i - a) * STEP) * 100.0
        after = (hh[b] - hh[i]) / ((b - i) * STEP) * 100.0
        drop = before - after
        if before >= CREST_UP and drop >= CREST_BREAK:
            found.append({'d': float(grid[i]), 'h': float(hh[i]), 'before': float(before), 'after': float(after),
                          'drop': float(drop)})
    out = []
    for c in sorted(found, key=lambda c: -c['drop']):
        if all(abs(c['d'] - o['d']) >= CREST_MIN_GAP_M for o in out):
            out.append(c)
    return sorted(out, key=lambda c: c['d'])


def on_grid(profile, grid):
    """The profile's height at each distance of `grid` (m), rounded to 0.1 m, for the Run view's strips (None past
    the profile's end); None without a profile."""
    if profile is None:
        return None
    n = len(profile['h'])
    out = []
    for d in grid:
        j = d / profile['step']
        if j > n - 1 + 0.5:
            out.append(None)
            continue
        j = min(j, n - 1)
        j0 = int(j)
        j1 = min(j0 + 1, n - 1)
        out.append(round(float(profile['h'][j0] + (profile['h'][j1] - profile['h'][j0]) * (j - j0)), 1))
    return out


# -- drift --

def _wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def heading_offsets(t, yaw, speed, path_heading):
    """The car's heading (the yaw rate's integral, radians, left positive) minus the path's direction, with the
    integration's own drift taken out: the difference's median on every straight stretch (the median-filtered |yaw| under
    STRAIGHT_YAW and above V_MIN for ANCHOR_S) is the anchor, linear between anchors, constant outside them.
    (angle in radians, left positive, per row; NaN with fewer than MIN_ANCHORS anchors, and further than
    ANCHOR_REACH_S from the nearest one.)"""
    dt = np.diff(t, prepend=t[0])
    dt = np.where((dt > 0) & (dt < 1.0), dt, 0.0)
    heading = np.cumsum(np.nan_to_num(yaw) * dt)
    diff = _wrap(heading - path_heading)
    ok = np.isfinite(path_heading) & (speed > V_MIN)
    straight = ok & (np.abs(_median(yaw, YAW_MEDIAN)) < STRAIGHT_YAW)
    anchors = []
    i = 0
    n = len(t)
    while i < n:
        if not straight[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and straight[j + 1]:
            j += 1
        if t[j] - t[i] >= ANCHOR_S:
            # the difference is a small angle around its median (wrapped at the first row's value)
            ref = diff[i]
            local = _wrap(diff[i:j + 1] - ref) + ref
            anchors.append((float(t[i:j + 1].mean()), float(np.median(local))))
        i = j + 1
    if len(anchors) < MIN_ANCHORS:
        return np.full(n, np.nan)
    at = np.array([a[0] for a in anchors])
    off = np.unwrap(np.array([a[1] for a in anchors]))
    base = np.interp(t, at, off)
    near = np.min(np.abs(t[:, None] - at[None, :]), axis=1)
    return np.where(near <= ANCHOR_REACH_S, _wrap(diff - base), np.nan)


def drift(rows, game, corners):
    """Per corner `{d, direction, peak, mean, n}`: the angle in degrees between where the car points and where it
    travels (drift / body slip), positive when it is angled into the corner (past its path: the tail out), negative
    when it points to the corner's outside; `peak` the one of largest size, `mean` over the corner's rows above
    V_MIN. The corners are the stored ones (d0, d1, direction 1 left). None where the game's yaw rate is not its own
    (DRIFT_GAMES) or the run has no positions; a corner with too few rows is left out."""
    if game not in DRIFT_GAMES or not rows or not corners:
        return None
    arr = _arrays(rows)
    path, keep = _clean_path(game, arr)
    if path is None:
        return None
    keep = np.array(keep)
    t, speed, yaw, d = arr['t'][keep], arr['speed'][keep], arr['yaw_rate'][keep], _along(arr['distance'])[keep]
    e, n = path[1], path[3]
    # the path's direction from positions 2 rows either side (0.4 s): smooth enough for the 10 Hz noise
    k = 2
    de = np.r_[e[k:], np.full(k, np.nan)] - np.r_[np.full(k, np.nan), e[:-k]]
    dn = np.r_[n[k:], np.full(k, np.nan)] - np.r_[np.full(k, np.nan), n[:-k]]
    moved = np.hypot(de, dn)
    path_heading = np.where(moved > 0.5, np.arctan2(dn, de), np.nan)
    off = heading_offsets(t, np.nan_to_num(yaw), speed, path_heading)
    out = []
    for c in corners:
        d0, d1 = c.get('d0'), c.get('d1')
        if d0 is None or d1 is None or not c.get('direction'):
            continue
        m = (d >= d0) & (d <= d1) & np.isfinite(off) & (speed > V_MIN)
        if m.sum() < MIN_CORNER_ROWS:
            continue
        v = np.degrees(off[m]) * c['direction']
        v = v[np.abs(v) <= DRIFT_MAX]
        if len(v) < MIN_CORNER_ROWS:
            continue
        out.append({'d': c['d'], 'direction': c['direction'], 'peak': float(v[np.argmax(np.abs(v))]),
                    'mean': float(v.mean()), 'n': int(len(v))})
    return out
