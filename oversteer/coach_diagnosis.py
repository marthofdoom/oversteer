"""Corner diagnosis (docs/coach-diagnosis.md): the measures of one section of a run against the same section of a
reference run, on a common 1 m distance grid, and the decision table that turns them into one diagnosis and one
fix. The coach says a corner's loss with it (coach._stage_place) and the potential's top 3 says its fix
(potential.call).

Pure functions over two runs' columns (potential.arrays); nothing here reads the database.

  measures(run_arr, ref_arr, sec, env)  -> dict of facts, X (this run) less R (the reference)
  measures_section(...)                 -> [(corner, measures)] of a section, corner by corner
  diagnose(m, loss)                     -> {'code', 'confidence', 'facts', 'fix', 'evidence'}
  diagnose_section(parts, loss)         -> diagnose() of the corner that lost the most, plus 'corner', 'part_loss',
                                           'm' and 'parts'

Numbers marked (calibrate) are starting values from marth's i20N runs (docs/coach-diagnosis.md, section 5).
"""

import math

import numpy as np

from . import coach_context as cc
from . import potential
from .telemetry_formats import plan_xy

KMH = 3.6

# -- thresholds (calibrate) --
BRAKE_SAME = 10.0        # m: braking points closer than this are the same point (= coach_context.BRAKE_DELTA)
SPEED_SAME = 3.0         # km/h: speeds closer than this are the same
SHED_MORE = 3.0          # km/h more speed shed in the braking zone than the reference: more braking
RELEASE_SOONER = 10.0    # m: a release this much later than the reference's is "held the brake longer"
PEAK_HARDER = 0.10       # brake pedal share: a peak this much higher is "harder"
GRIP_LEFT = 0.85         # grip used at the slowest point under this: grip was left (>= 15 %)
GRIP_LIMIT = 0.92        # grip used at or over this: at the limit
COUNTER_MORE = 0.15      # share of the corner steered against the yaw, more than the reference's: a slide
APEX_SHIFT = 15.0        # m the slowest point moved: an earlier or later apex
THROTTLE_LATER = 10.0    # m later to the throttle (past 20 %) after the slowest point
COAST_LONGER = 0.3       # s more with both pedals off between the brake and the throttle
EXIT_AT = 50.0           # m past the reference's slowest point the exit speed is read at
MERGE_GAP = 8.0          # m: two brake applications closer than this are one braking
ZONE_MIN = 3.0           # km/h a braking must shed to be one
PAIR_REACH = 80.0        # m: this run's braking nearest the reference's, within this, is the same braking
APEX_REACH = 40.0        # m either side of the reference's slowest point this run's is looked for
SLOW_ARRIVAL = 5.0       # km/h slower before either braked: the time came from the run-up
LOSS_MIN = cc.SECTION_MIN    # s a section must lose or gain to be diagnosed
LINE_MIN = 1.0           # m of lateral offset worth saying


def _mode(values):
    values = [int(v) for v in values if np.isfinite(v) and v >= 1]
    return max(set(values), key=values.count) if values else None


def _first(mask, start=0):
    idx = np.flatnonzero(mask[start:])
    return None if idx.size == 0 else int(idx[0]) + start


def _zones(G, V, B, lo_i, hi_i):
    """[(onset i, release i, shed km/h, peak, integral)] of the brakings between grid indices lo_i and hi_i (the
    slowest point): applications past cc.BRAKE_ON merged across gaps under MERGE_GAP m, each shedding at least
    ZONE_MIN km/h (to the lowest speed before the next one or the slowest point)."""
    on = np.nan_to_num(B[lo_i:hi_i + 1]) > cc.BRAKE_ON
    segs, start = [], None
    for k, x in enumerate(on):
        if x and start is None:
            start = k
        elif not x and start is not None:
            segs.append([start, k - 1])
            start = None
    if start is not None:
        segs.append([start, len(on) - 1])
    if not segs:
        return []
    merged = [segs[0]]
    for s in segs[1:]:
        if G[lo_i + s[0]] - G[lo_i + merged[-1][1]] < MERGE_GAP:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    out = []
    for n, (a, b) in enumerate(merged):
        a, b = a + lo_i, b + lo_i
        end = merged[n + 1][0] + lo_i if n + 1 < len(merged) else hi_i
        shed = (V[a] - np.nanmin(V[a:max(end, a) + 1])) * KMH
        if shed >= ZONE_MIN:
            seg = np.nan_to_num(B[a:b + 1])
            out.append((a, b, float(shed), float(seg.max()), float(seg.sum())))     # integral: pedal-metres (1 m grid)
    return out


def _pair_zones(zx, zr):
    """(this run's zone, the reference's zone): the reference's is the one that shed the most speed (the braking
    the corner was braked for: not the hardest pedal, coach_context.brake_application, which a stab at the turn-in
    can be); this run's is the one overlapping it the most, else the nearest onset within PAIR_REACH m, else the one
    that shed the most. Either may be None."""
    r = max(zr, key=lambda z: z[2]) if zr else None
    if not zx:
        return None, r
    if r is None:
        return max(zx, key=lambda z: z[2]), None

    def overlap(z):
        return min(z[1], r[1]) - max(z[0], r[0])
    best = max(zx, key=lambda z: (overlap(z), z[2]))
    if overlap(best) >= 0:
        return best, r
    near = min(zx, key=lambda z: abs(z[0] - r[0]))
    if abs(near[0] - r[0]) <= PAIR_REACH:
        return near, r
    return max(zx, key=lambda z: z[2]), r


class Run:
    """One run's columns on the grid. `game` names the game for the plan view of its positions (None: no positions,
    so no line)."""

    def __init__(self, arr, G, game=None):
        self.G = G
        self.V = potential.resample(arr, 'speed', G)
        self.B = potential.resample(arr, 'brake', G)
        self.THR = potential.resample(arr, 'throttle', G)
        self.GEAR = potential.resample(arr, 'gear', G)
        self.RPM = potential.resample(arr, 'rpm', G)
        self.AY = potential.resample(arr, potential.smoothed_a(arr, potential.SMOOTH_AX)[1], G)
        self.STEER = potential.resample(arr, 'steer', G)
        self.YAW = potential.resample(arr, 'yaw_rate', G)
        self.T = potential.elapsed(arr, G)
        self.pos = None
        if game is not None and potential.has_positions(arr):
            x, y = plan_xy(game, (potential.resample(arr, 'x', G), potential.resample(arr, 'y', G),
                                   potential.resample(arr, 'z', G)))
            self.pos = (np.asarray(x), np.asarray(y))

    def idx(self, d):
        return int(np.clip(round(d - self.G[0]), 0, len(self.G) - 1))


def _grip(run, i, env):
    near = run.AY[max(0, i - 4):i + 5]
    near = near[np.isfinite(near)]
    if not near.size or env is None or not np.isfinite(run.V[i]):
        return None
    lim = potential.env_at(env, 'lat', run.V[i])
    return float(np.median(np.abs(near)) / lim) if lim > 0 else None


def _counter(run, i0, i1):
    s, y = run.STEER[i0:i1 + 1], run.YAW[i0:i1 + 1]
    ok = np.isfinite(s) & np.isfinite(y) & (np.abs(s) > 0.02)
    if ok.sum() < 3:
        return None
    return float(np.mean((s[ok] * y[ok]) < 0))


def _lateral(run, ref, i, direction):
    """Signed metres run's position is from the reference's path at grid index i: positive to the inside of the
    corner (direction 1 left, -1 right). None without positions."""
    if run.pos is None or ref.pos is None:
        return None
    px, py = run.pos[0][i], run.pos[1][i]
    lo, hi = max(0, i - 30), min(len(ref.G) - 1, i + 30)
    rx, ry = ref.pos[0][lo:hi + 1], ref.pos[1][lo:hi + 1]
    if not (np.isfinite(px) and np.isfinite(py)) or not np.isfinite(rx).all():
        return None
    k = int(np.argmin((rx - px) ** 2 + (ry - py) ** 2))
    k = min(max(k, 1), len(rx) - 2)
    tx, ty = rx[k + 1] - rx[k - 1], ry[k + 1] - ry[k - 1]
    n = math.hypot(tx, ty)
    if n == 0:
        return None
    cross = (tx * (py - ry[k]) - ty * (px - rx[k])) / n          # positive: to the left of the reference's path
    return float(cross * (direction or 1))


def measures(run_arr, ref_arr, sec, env=None):
    """The facts of one section, this run (X) against the reference (R), on the reference's distances.
    `sec`: {'a', 'b' (the section's timed bounds, coach_context.spans), 'lo' (where braking is looked for from),
    'floor' (never before it), 'key' (the reference's corner: d, d0, d1, direction), 'game' (for the positions:
    None, none)}. `env` the grip envelope ({'bins', 'lat'}) or None.
    Every delta is X less R; distances in m (positive: further down the road, i.e. later), speeds in km/h."""
    lo, a, b, key = sec['lo'], sec['a'], sec['b'], sec['key']
    floor = min(sec.get('floor', lo), lo)
    G = np.arange(math.floor(floor), math.ceil(b) + 1, 1.0)
    game = sec.get('game')
    X, R = Run(run_arr, G, game), Run(ref_arr, G, game)
    if np.isnan(X.V).any() or np.isnan(R.V).any() or np.isnan(X.T).any() or np.isnan(R.T).any():
        return None
    m = {}
    # the slowest point: in the key corner's window (a little either side), each run's own
    win0, win1 = R.idx(key['d0'] - 30), R.idx(min(key['d1'] + 30, b))
    ri = win0 + int(np.nanargmin(R.V[win0:win1 + 1]))
    x0, x1 = max(win0, ri - int(APEX_REACH)), min(win1, ri + int(APEX_REACH))
    xi = x0 + int(np.nanargmin(X.V[x0:x1 + 1]))
    m['min_x'], m['min_r'] = X.V[xi] * KMH, R.V[ri] * KMH
    m['min_dv'] = m['min_x'] - m['min_r']
    m['min_dd'] = float(G[xi] - G[ri])
    m['min_d'] = float(G[ri])
    # braking: from `lo` (or the previous corner's slowest point inside the section) to each one's slowest point
    li = R.idx(lo)
    zxs, zrs = _zones(G, X.V, X.B, li, xi), _zones(G, R.V, R.B, li, ri)
    # one braked inside the window and the other not: the other's braking for the same bend may begin before it
    # (a slide or a different line moves the slowest point): looked for back to `floor`, PAIR_REACH m before
    if zxs and not zrs:
        zrs = _zones(G, R.V, R.B, max(0, zxs[0][0] - int(PAIR_REACH)), ri)
    elif zrs and not zxs:
        zxs = _zones(G, X.V, X.B, max(0, zrs[0][0] - int(PAIR_REACH)), xi)
    zx, zr = _pair_zones(zxs, zrs)
    m['braked_x'], m['braked_r'] = zx is not None, zr is not None
    for name, z, run in (('x', zx, X), ('r', zr, R)):
        if z is None:
            for f in ('onset', 'release', 'shed', 'peak', 'integral', 'v_onset'):
                m['{}_{}'.format(f, name)] = None
            continue
        m['onset_' + name], m['release_' + name] = float(G[z[0]]), float(G[z[1]])
        m['shed_' + name], m['peak_' + name], m['integral_' + name] = z[2], z[3], z[4]
        m['v_onset_' + name] = run.V[z[0]] * KMH
    both = zx is not None and zr is not None
    m['onset_dd'] = m['onset_x'] - m['onset_r'] if both else None           # positive: braked later
    m['release_dd'] = m['release_x'] - m['release_r'] if both else None     # positive: released later
    m['shed_dv'] = m['shed_x'] - m['shed_r'] if both else None              # positive: shed more speed
    m['peak_d'] = m['peak_x'] - m['peak_r'] if both else None
    m['integral_d'] = m['integral_x'] - m['integral_r'] if both else None
    # the total speed taken off from the reference's braking point to the slowest point
    ref_on = R.idx(m['onset_r']) if zr is not None else R.idx(key['d0'])
    m['arrive_dv'] = (X.V[ref_on] - R.V[ref_on]) * KMH                      # at the reference's braking point
    ti = R.idx(key['d0'])
    m['turnin_dv'] = (X.V[ti] - R.V[ti]) * KMH                              # at the reference's yaw window start
    first_on = min([z[0] for z in (zx, zr) if z is not None] or [ref_on])
    pre = max(0, first_on - 5)
    m['pre_dv'] = (X.V[pre] - R.V[pre]) * KMH                               # before either braked
    m['drop_x'] = (X.V[first_on] - X.V[xi]) * KMH                          # from the first braking point of the two
    m['drop_r'] = (R.V[first_on] - R.V[ri]) * KMH
    ei = R.idx(min(G[ri] + EXIT_AT, b))
    m['exit_dv'] = (X.V[ei] - R.V[ei]) * KMH
    m['exit_d'] = float(G[ei] - G[ri])
    m['end_dv'] = (X.V[-1] - R.V[-1]) * KMH
    # braking after the slowest point (to the stretch's end): speed taken off again on the way out
    m['exit_brake_x'] = sum(z[2] for z in _zones(G, X.V, X.B, xi + 1, len(G) - 1)) if xi + 1 < len(G) - 1 else 0.0
    m['exit_brake_r'] = sum(z[2] for z in _zones(G, R.V, R.B, ri + 1, len(G) - 1)) if ri + 1 < len(G) - 1 else 0.0
    # throttle: past 20 % after each one's slowest point (on already counts from the slowest point), then full
    px = _first(np.nan_to_num(X.THR) > cc.THROTTLE_ON, xi)
    pr = _first(np.nan_to_num(R.THR) > cc.THROTTLE_ON, ri)
    m['pickup_dd'] = float(G[px] - G[pr]) if px is not None and pr is not None else None
    fx = _first(np.nan_to_num(X.THR) >= cc.THROTTLE_FULL, xi)
    fr = _first(np.nan_to_num(R.THR) >= cc.THROTTLE_FULL, ri)
    m['full_dd'] = float(G[fx] - G[fr]) if fx is not None and fr is not None else None
    # coasting between the end of the braking and the throttle (both under cc.COAST)

    def coast(run, z, i, p):
        s = z[1] if z is not None else max(0, i - 40)
        e = p if p is not None else i
        if e <= s:
            return 0.0
        both_off = (np.nan_to_num(run.THR[s:e]) < cc.COAST) & (np.nan_to_num(run.B[s:e]) < cc.COAST)
        dt = np.diff(run.T[s:e + 1])
        return float(np.sum(dt[both_off]))
    m['coast_ds'] = coast(X, zx, xi, px) - coast(R, zr, ri, pr)
    # gear at the slowest point and on the exit
    m['gear_min_x'], m['gear_min_r'] = _mode(X.GEAR[max(0, xi - 10):xi + 11]), _mode(R.GEAR[max(0, ri - 10):ri + 11])
    m['gear_exit_x'], m['gear_exit_r'] = _mode(X.GEAR[max(0, ei - 10):ei + 11]), _mode(R.GEAR[max(0, ei - 10):ei + 11])
    # grip used at the slowest point (lateral g over the envelope at that speed), each run's own
    m['grip_x'], m['grip_r'] = _grip(X, xi, env), _grip(R, ri, env)
    # rotation: share of the key corner steered against the yaw, and the yaw peak
    k0, k1 = R.idx(key['d0']), R.idx(min(key['d1'], b))
    cx, cr = _counter(X, k0, k1), _counter(R, k0, k1)
    m['counter_x'], m['counter_r'] = cx, cr
    m['counter_d'] = cx - cr if cx is not None and cr is not None else None
    yx, yr = np.nanmax(np.abs(X.YAW[k0:k1 + 1])), np.nanmax(np.abs(R.YAW[k0:k1 + 1]))
    m['yaw_d'] = float(yx - yr) if np.isfinite(yx) and np.isfinite(yr) else None
    # the line: lateral offset from the reference's path at its turn-in and slowest point (positive: inside)
    m['line_turnin'] = _lateral(X, R, ti, key.get('direction'))
    m['line_apex'] = _lateral(X, R, ri, key.get('direction'))
    # the time: over the reference's distances, approach (to its braking point), braking (to its slowest point),
    # exit (to the section's end)
    ai, bi = R.idx(a), len(G) - 1
    on_i = max(ai, min(ref_on, ri))

    def dt(i, j):
        return float((X.T[j] - X.T[i]) - (R.T[j] - R.T[i]))
    m['t_approach'], m['t_brake'], m['t_exit'] = dt(ai, on_i), dt(on_i, ri), dt(ri, bi)
    m['loss'] = dt(ai, bi)
    return m


def measures_section(run_arr, ref_arr, sec, env=None):
    """measures() corner by corner through a section of several corners (a complex): each of the reference's
    corners is its own stretch, from where the reference began braking for it (or, without braking, its fastest
    point since the corner before) to where it began braking for the next; the first from the section's start,
    the last to its end. Returns [(corner, measures)] in order (a corner whose stretch is too short, or not
    measurable, left out). One corner: [(corner, measures(sec))]. A complex is never judged by its slowest
    corners alone: in one run that can be the first corner, in the other the last."""
    cs = sorted(sec['corners'], key=lambda k: k['d'])
    if len(cs) == 1:
        m = measures(run_arr, ref_arr, dict(sec, key=cs[0]), env)
        return [] if m is None else [(cs[0], m)]
    G = np.arange(math.floor(sec['lo']), math.ceil(sec['b']) + 1, 1.0)
    R = Run(ref_arr, G)
    if np.isnan(R.V).any():
        return []
    mins = []
    for k in cs:
        i0, i1 = R.idx(k['d0'] - 30), R.idx(min(k['d1'] + 30, sec['b']))
        mins.append(i0 + int(np.nanargmin(R.V[i0:i1 + 1])))
    edges = [sec['a']]
    for n in range(1, len(cs)):
        p, q = mins[n - 1], mins[n]
        if q <= p:
            edges.append(float(G[p]))
            continue
        zs = _zones(G, R.V, R.B, p, q)
        edges.append(float(G[zs[-1][0]]) if zs else float(G[p + int(np.nanargmax(R.V[p:q + 1]))]))
    edges.append(sec['b'])
    out = []
    for n, k in enumerate(cs):
        a, b = edges[n], edges[n + 1]
        if b - a < 20.0:
            continue
        lo = sec['lo'] if n == 0 else max(float(G[mins[n - 1]]) + 5.0, a - cc.APPROACH)
        floor = sec.get('floor', sec['lo']) if n == 0 else lo         # never back into the corner before's braking
        m = measures(run_arr, ref_arr, {'a': a, 'b': b, 'lo': lo, 'floor': floor, 'key': k, 'game': sec.get('game')}, env)
        if m is not None:
            out.append((k, m))
    return out


def diagnose_section(parts, loss=None):
    """The diagnosis of a section from measures_section's parts: the corner that lost the most time is diagnosed
    (diagnose()) and named; the section's own `loss` (the sum of the parts where None) is the time said. Returns
    diagnose()'s dict plus `corner` (the corner diagnosed), `part_loss` and `parts` ([(corner d, loss, code)])."""
    if not parts:
        return None
    total = sum(m['loss'] for _, m in parts) if loss is None else loss
    if total < 0:
        k, m = min(parts, key=lambda p: p[1]['loss'])
    else:
        k, m = max(parts, key=lambda p: p[1]['loss'])
    own = m['loss'] if abs(m['loss']) >= LOSS_MIN else total
    d = diagnose(m, own)
    d.update(corner=k, part_loss=m['loss'], m=m,
             parts=[(round(kk['d']), round(mm['loss'], 2), diagnose(mm)['code']) for kk, mm in parts])
    return d


# -- the decision table --

def _km(x):
    return '{:.0f} km/h'.format(abs(x))


def _m(x):
    return '{:.0f} m'.format(abs(x))


def _pct(x):
    return '{:d} %'.format(int(round(100 * min(x, 1.0))))


def _ord(n):
    return {1: '1st', 2: '2nd', 3: '3rd'}.get(n, '{}th'.format(n))


# The facts each diagnosis states, in the order a driver meets them (the keys of _facts)
FACTS = {'OVER-SLOWED': ('brake', 'shed', 'min', 'grip'),
         'OVERSHOT': ('brake', 'turnin', 'min', 'apex', 'counter'),
         'EARLY-BRAKE': ('brake', 'turnin', 'shed', 'min'),
         'SLOW-ARRIVAL': ('pre', 'min'),
         'SLIDE': ('counter', 'min', 'exit'),
         'LOST-SPEED': ('brake', 'min', 'grip', 'exit'),
         'EXIT-BRAKE': ('min', 'exit_brake', 'exit'),
         'LATE-THROTTLE': ('min', 'pickup', 'exit'),
         'COASTING': ('release', 'coast'),
         'GEAR': ('gear', 'exit'),
         'EARLY-APEX': ('apex', 'exit', 'line'),
         'AT-LIMIT': ('min', 'grip', 'line'),
         'UNCLEAR': None,              # the first FACTS_UNCLEAR that differ
         'SAME': ()}
FACTS_UNCLEAR = 4


def _facts(m):
    """{key: sentence part} of the measured differences worth saying (only those past their threshold)."""
    out = {}
    o = m['onset_dd']
    if o is not None:
        out['brake'] = ('braked {} {}'.format(_m(o), 'later' if o > 0 else 'earlier') if abs(o) >= BRAKE_SAME
                        else 'braked at the same place')
    elif m['braked_x'] and not m['braked_r']:
        out['brake'] = 'braked where your best run did not ({} off)'.format(_km(m['shed_x']))
    elif m['braked_r'] and not m['braked_x']:
        out['brake'] = 'did not brake where your best run took {} off'.format(_km(m['shed_r']))
    if abs(m['pre_dv']) >= SLOW_ARRIVAL:
        out['pre'] = 'were {} {} before the braking'.format(_km(m['pre_dv']), 'slower' if m['pre_dv'] < 0 else 'faster')
    if abs(m['turnin_dv']) >= SPEED_SAME:
        out['turnin'] = 'reached the turn-in {} {}'.format(_km(m['turnin_dv']),
                                                           'faster' if m['turnin_dv'] > 0 else 'slower')
    if m['shed_dv'] is not None and abs(m['shed_dv']) >= SHED_MORE:
        out['shed'] = 'took {} off on the brake where your best run took {}'.format(_km(m['shed_x']), _km(m['shed_r']))
    if abs(m['min_dv']) >= SPEED_SAME:
        out['min'] = 'were {} {} at the slowest point ({:.0f} against {:.0f})'.format(
            _km(m['min_dv']), 'slower' if m['min_dv'] < 0 else 'faster', m['min_x'], m['min_r'])
    else:
        out['min'] = 'took the same speed through the slowest point'
    if abs(m['min_dd']) >= APEX_SHIFT:
        out['apex'] = 'had the slowest point {} {}'.format(_m(m['min_dd']), 'later' if m['min_dd'] > 0 else 'earlier')
    if m['grip_x'] is not None and m['grip_x'] >= potential.GRIP_QUOTE_MIN:
        out['grip'] = 'used {} of the grip there{}'.format(
            _pct(m['grip_x']), ' (your best run {})'.format(_pct(m['grip_r'])) if m['grip_r'] is not None else '')
    elif m['grip_x'] is not None:
        out['grip'] = 'had hardly any cornering load there ({} of the grip)'.format(_pct(m['grip_x']))
    if abs(m['exit_dv']) >= SPEED_SAME:
        out['exit'] = 'were {} {} {:.0f} m after it'.format(_km(m['exit_dv']), 'slower' if m['exit_dv'] < 0 else 'faster',
                                                            m['exit_d'])
    if m['counter_x'] is not None and m['counter_r'] is not None and abs(m['counter_d']) >= COUNTER_MORE:
        out['counter'] = 'steered against the slide for {} of the corner (your best run {})'.format(
            _pct(m['counter_x']), _pct(m['counter_r']))
    if m['pickup_dd'] is not None and abs(m['pickup_dd']) >= THROTTLE_LATER:
        out['pickup'] = 'took the throttle {} {} after it'.format(_m(m['pickup_dd']),
                                                                 'later' if m['pickup_dd'] > 0 else 'sooner')
    if abs(m['coast_ds']) >= COAST_LONGER:
        out['coast'] = 'had {:.1f} s {} with neither pedal between the brake and the throttle'.format(
            abs(m['coast_ds']), 'more' if m['coast_ds'] > 0 else 'less')
    if m['release_dd'] is not None and abs(m['release_dd']) >= RELEASE_SOONER:
        out['release'] = 'came off the brake {} {}'.format(_m(m['release_dd']),
                                                           'later' if m['release_dd'] > 0 else 'earlier')
    if m['gear_exit_x'] and m['gear_exit_r'] and m['gear_exit_x'] != m['gear_exit_r']:
        out['gear'] = 'were in {} on the way out (your best run {})'.format(_ord(m['gear_exit_x']),
                                                                            _ord(m['gear_exit_r']))
    if m['exit_brake_x'] - m['exit_brake_r'] >= SHED_MORE:
        out['exit_brake'] = 'braked again on the way out ({} off, your best run {})'.format(
            _km(m['exit_brake_x']), _km(m['exit_brake_r']))
    line = m['line_apex']
    if line is not None and abs(line) >= LINE_MIN:
        out['line'] = 'were {} {} of your best run\'s line at its slowest point'.format(
            _m(line), 'inside' if line > 0 else 'outside')
    return out


def _pick(code, f):
    keys = FACTS.get(code)
    if keys is None:
        keys = [k for k in f if k not in ('grip',)][:FACTS_UNCLEAR]
    return [f[k] for k in keys if k in f]


def diagnose(m, loss=None):
    """The one diagnosis of a stretch's measures `m` (measures()), given its time `loss` against the reference (s,
    negative: quicker; m['loss'] where None). Returns {'code', 'confidence' (high, medium, low), 'facts' [str: the
    ones this diagnosis states], 'fix' (str or None), 'evidence' [the conditions that fired]}. The rows are tried
    in order and the first that holds wins (docs/coach-diagnosis.md, the table). Facts are measured; a fix is given
    only where the measures name it, and never names a cause that is not measured (the line only from positions)."""
    loss = m['loss'] if loss is None else loss
    f = _facts(m)

    def out(code, confidence, fix, evidence):
        facts = f
        if code == 'OVERSHOT' and not (m['counter_d'] is not None and m['counter_d'] > 0):
            facts = {k: v for k, v in f.items() if k != 'counter'}      # steering against it less is no fact for this
        return dict(code=code, confidence=confidence, facts=_pick(code, facts), fix=fix, evidence=evidence)
    if abs(loss) < LOSS_MIN:
        return out('SAME', 'high', None, ['|loss| < {}'.format(LOSS_MIN)])
    if loss < 0:
        return _gain(m, f)
    onset, min_dv, exit_dv, grip = m['onset_dd'], m['min_dv'], m['exit_dv'], m['grip_x']
    low_min = min_dv <= -SPEED_SAME
    low_exit = exit_dv <= -SPEED_SAME
    later = onset is not None and onset >= BRAKE_SAME
    earlier = onset is not None and onset <= -BRAKE_SAME
    fast_in = m['turnin_dv'] >= SPEED_SAME or (later and m['arrive_dv'] >= SPEED_SAME)
    at_limit = grip is not None and grip >= GRIP_LIMIT
    grip_left = grip is not None and grip < GRIP_LEFT
    slide = m['counter_d'] is not None and m['counter_d'] >= COUNTER_MORE
    late_apex = m['min_dd'] >= APEX_SHIFT
    shed_more = m['shed_dv'] is not None and m['shed_dv'] >= SHED_MORE
    dropped_more = (m['drop_x'] - m['drop_r']) >= SHED_MORE
    entry_share = (m['t_approach'] + m['t_brake']) / loss

    def conf(n):
        return 'high' if n >= 3 and loss >= 0.2 else 'medium' if n >= 2 else 'low'

    # 1. SLOW-ARRIVAL: slower before either run braked, and the time went before the slowest point: the run-up
    if m['pre_dv'] <= -SLOW_ARRIVAL and entry_share >= 0.6:
        return out('SLOW-ARRIVAL', 'medium',
                   'The time here was lost before the braking: look at the exit of the corner before.',
                   ['pre-braking {:.0f} km/h'.format(m['pre_dv']), 'entry share {:.0%}'.format(entry_share)])
    # 2. OVERSHOT: in faster, and it showed (at the grip limit, a slide, or the slowest point pushed later with the
    # grip not left over: with grip to spare a later slowest point is no overshoot), with the speed then lost
    if fast_in and (low_min or low_exit or late_apex) and (at_limit or slide or (late_apex and not grip_left)):
        ev = [x for x, c in (('turn-in +{:.0f} km/h'.format(m['turnin_dv']), m['turnin_dv'] >= SPEED_SAME),
                             ('braked {:.0f} m later'.format(onset or 0), later),
                             ('grip {:.2f}'.format(grip or 0), at_limit),
                             ('counter-steer +{:.2f}'.format(m['counter_d'] or 0), slide),
                             ('apex {:+.0f} m'.format(m['min_dd']), late_apex)) if c]
        if later:
            fix = 'Brake {} earlier, where your best run did, and turn in at its speed.'.format(_m(onset))
        else:
            fix = 'Get the speed off before the turn-in: arrive {} slower, as your best run did.'.format(
                _km(m['turnin_dv']))
        return out('OVERSHOT', conf(len(ev)), fix, ev)
    # 3. SLIDE: more steering against the yaw than the reference, speed lost, not in faster: the fact, no cause
    if slide and (low_min or low_exit):
        return out('SLIDE', 'medium' if loss >= 0.2 else 'low',
                   'Keep the car straighter through it, as your best run did.',
                   ['counter-steer {:.0f} % vs {:.0f} %'.format(100 * m['counter_x'], 100 * m['counter_r'])])
    # 4. LOST-SPEED: slower at the slowest point with no braking and hardly any cornering load there: a lift, a
    # bump, a moment or a hit, which the trace cannot tell apart: facts only
    if low_min and not m['braked_x'] and grip is not None and grip < potential.GRIP_QUOTE_MIN:
        return out('LOST-SPEED', 'low', None, ['min {:.0f} km/h, no braking, grip {:.2f}'.format(min_dv, grip)])
    # 5. OVER-SLOWED: braked at the same point or later, took more speed off, a lower minimum, grip left over
    if low_min and not earlier and (grip_left or (grip is None and (shed_more or dropped_more))) \
            and (shed_more or dropped_more or not m['braked_x']):
        ev = [x for x, c in (('min {:.0f} km/h'.format(min_dv), True),
                             ('braked {:+.0f} m'.format(onset or 0), onset is not None),
                             ('shed +{:.0f} km/h'.format(m['shed_dv'] or 0), shed_more),
                             ('dropped +{:.0f} km/h'.format(m['drop_x'] - m['drop_r']), dropped_more and not shed_more),
                             ('grip {:.2f}'.format(grip or 0), grip_left)) if c]
        if not m['braked_x']:
            how = 'Lift less'
        elif not m['braked_r']:
            how = 'Brake less there, or not at all, as your best run did'
        elif m['release_dd'] is not None and m['release_dd'] >= RELEASE_SOONER:
            how = 'Keep that braking point and come off the brake {} sooner'.format(_m(m['release_dd']))
        elif m['peak_d'] is not None and m['peak_d'] >= PEAK_HARDER:
            how = 'Keep that braking point and brake less hard ({} pedal, your best run {})'.format(
                _pct(m['peak_x']), _pct(m['peak_r']))
        else:
            how = 'Keep that braking point and brake less'
        left = (' About {} of the grip was left there.'.format(_pct(1 - grip))
                if grip is not None and potential.GRIP_QUOTE_MIN <= grip < 1 else '')
        return out('OVER-SLOWED', conf(len(ev)),
                   '{}: carry {} more through the slowest point.{}'.format(how, _km(min_dv), left), ev)
    # 6. EARLY-BRAKE: braked earlier, the time went before the slowest point, no higher minimum for it
    if earlier and m['t_approach'] + m['t_brake'] >= 0.05 and min_dv < SPEED_SAME:
        ev = ['braked {:.0f} m earlier'.format(-onset), 'entry +{:.2f} s'.format(m['t_approach'] + m['t_brake'])]
        if low_min and shed_more:
            fix = 'Brake {} later and less, as your best run did: carry {} more through the slowest point.'.format(
                _m(onset), _km(min_dv))
        elif low_min:
            fix = 'Brake {} later, where your best run did: carry {} more through the slowest point.'.format(
                _m(onset), _km(min_dv))
        else:
            fix = 'Brake {} later, where your best run did.'.format(_m(onset))
        return out('EARLY-BRAKE', conf(len(ev) + (0 if low_min else 1)), fix, ev)
    # 7. EXIT-BRAKE: braked again after the slowest point where the reference did not (or less), time on the exit
    again = m['exit_brake_x'] - m['exit_brake_r']
    if again >= 2 * SHED_MORE and m['t_exit'] >= 0.5 * loss:
        return out('EXIT-BRAKE', 'medium', 'Keep the speed up after the slowest point: no brake on the way out, as '
                                           'your best run did.', ['exit braking +{:.0f} km/h'.format(again)])
    # 8. LATE-THROTTLE: no lower minimum, the throttle later, the exit slower
    if not low_min and m['pickup_dd'] is not None and m['pickup_dd'] >= THROTTLE_LATER \
            and (low_exit or m['t_exit'] > 0.05):
        ev = ['throttle {:.0f} m later'.format(m['pickup_dd']), 'exit {:+.0f} km/h'.format(exit_dv)]
        return out('LATE-THROTTLE', conf(len(ev) + (1 if low_exit else 0)),
                   'Throttle {} sooner after the slowest point, where your best run did.'.format(_m(m['pickup_dd'])), ev)
    # 9. COASTING: a longer gap with neither pedal between the brake and the throttle
    if m['coast_ds'] >= COAST_LONGER:
        return out('COASTING', 'medium', 'Go from the brake to the throttle without the gap.',
                   ['coast +{:.1f} s'.format(m['coast_ds'])])
    # 10. GEAR: slower out (or the time on the exit) in another gear
    if (low_exit or m['end_dv'] <= -SPEED_SAME or m['t_exit'] >= 0.5 * loss) and m['gear_exit_x'] \
            and m['gear_exit_r'] and m['gear_exit_x'] != m['gear_exit_r']:
        return out('GEAR', 'medium' if low_exit else 'low', 'Use {} out of it, as your best run did.'.format(
            _ord(m['gear_exit_r'])),
                   ['gear {} vs {}'.format(m['gear_exit_x'], m['gear_exit_r'])])
    # 11. EARLY-APEX: the slowest point earlier, the same minimum, slower out
    if m['min_dd'] <= -APEX_SHIFT and not low_min and low_exit:
        return out('EARLY-APEX', 'low', 'Take the slowest point {} later, where your best run did.'.format(
            _m(m['min_dd'])), ['apex {:+.0f} m'.format(m['min_dd'])])
    # 12. AT-LIMIT: a lower minimum with the grip used: the pedals are not the fix; the line only with positions
    if low_min and at_limit:
        line = m['line_apex']
        fix = ('Your best run was {} {} of your line at its slowest point, on a wider radius.'.format(
            _m(line), 'outside' if line > 0 else 'inside') if line is not None and abs(line) >= LINE_MIN else None)
        return out('AT-LIMIT', 'low', fix, ['grip {:.2f}'.format(grip)])
    # 13. EARLY-BRAKE with a lower minimum and no time measured before the braking point
    if low_min and earlier:
        return out('EARLY-BRAKE', 'low', 'Brake {} later, where your best run did: carry {} more through the slowest '
                                         'point.'.format(_m(onset), _km(min_dv)), ['braked earlier, min lower'])
    return out('UNCLEAR', 'low', None, [])


def _gain(m, f):
    """What a quicker stretch did better (praise; no fix)."""
    good = []
    if m['min_dv'] >= SPEED_SAME:
        good.append('carried {} more through the slowest point'.format(_km(m['min_dv'])))
    if m['onset_dd'] is not None and m['onset_dd'] >= BRAKE_SAME and m['min_dv'] > -SPEED_SAME:
        good.append('braked {} later'.format(_m(m['onset_dd'])))
    if m['exit_dv'] >= SPEED_SAME:
        good.append('left {} faster'.format(_km(m['exit_dv'])))
    if m['pickup_dd'] is not None and m['pickup_dd'] <= -THROTTLE_LATER:
        good.append('took the throttle {} sooner'.format(_m(m['pickup_dd'])))
    return dict(code='GAIN', confidence='high' if good else 'low', facts=good, fix=None, evidence=good)
