"""Coaching: what each run measured about the driver, and what the history
of those measures says (docs/telemetry-coaching.md, section 9).

Metrics are worked out on the drive-log thread when a run ends
(run_metrics(), stage_metrics()) and stored with the run's discipline,
surface and the way of changing gear, so a metric is only ever compared
with its own kind: a gravel stage on the H-pattern with other gravel
stages on the H-pattern. The Coach reads them back and turns them into a
few sentences, each an observation with its number, what it costs and
one thing to do, without repeating itself (coach_state).
"""

import bisect
import math
import statistics

from .shift_learner import LIMITER_BAND, FULL_THROTTLE

# -- metrics of one run (section 9.1) --

KM = 1000.0
LAUNCH_HELD = 0.5                # s with the revs held before moving: a launch (as drive_detect.LAUNCH)
LAUNCH_WINDOW = 2.0              # s after the start in which a launch bogs, stalls or spins
LAUNCH_SPEED = 50 / 3.6          # m/s: the launch is timed to 50 km/h
BOG = 0.6                        # the revs fall below this share of the launch revs: a bog
STALL = 300.0                    # rpm: the engine stopped
IN_BAND = 100.0                  # rpm either side of the best change's range that still count as on it
SPOT_ON = 200.0                  # rpm either side of the best, where no range is known yet
OVERLAP = 0.2                    # throttle and brake both past this: both pedals at once
COAST = 0.05                     # both pedals under this above COAST_SPEED: coasting
COAST_SPEED = 10.0               # m/s
HANDBRAKE_PULL = 0.5
POWER_BAND = 0.85                # share of peak power where the power band starts (calibrate)
EXIT_DELAY = 1.0                 # s after the throttle goes back on that an exit's revs are read
EXIT_THROTTLE = 0.5
BALANCE_YAW = 0.1                # rad/s: cornering, for the balance fit
BALANCE_SPEED = 8.0              # m/s
SPLITS = 10                      # the stage cut into this many parts for consistency
SPLIT_RUNS = 5                   # the most recent finished runs compared for consistency
CORNER_WINDOW = 50.0             # m either side of a corner's apex timed against the best run
WORST_CORNERS = 3


def _finite(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def _dt(trace, i, t):
    """Seconds row i stands for (the gap to the next row, capped)."""
    if i + 1 < len(trace):
        return max(0.0, min(0.5, trace[i + 1][t] - trace[i][t]))
    return 0.1


def _shift_groups(shifts, keep):
    groups = {}
    for s in shifts:
        if keep(s):
            groups.setdefault((s['gear'], s['method']), []).append(s)
    return groups


def shift_metrics(shifts):
    """shift.error, shift.in_band and shift.slip per (gear, method) of the
    flat-out changes up by one with a known best; the H-pattern, sequential
    and change-down rates."""
    out = []
    ups = _shift_groups(shifts, lambda s: s['direction'] == 'up' and s['gear_to'] == s['gear'] + 1
                        and s['flat_out'] and s['best'] is not None)
    for (gear, method), group in sorted(ups.items(), key=lambda kv: (kv[0][0], kv[0][1] or '')):
        errors = [s['rpm'] - s['best'] for s in group]
        out.append({'name': 'shift.error', 'value': statistics.mean(errors), 'count': len(group),
                    'gear': gear, 'method': method})

        def on_it(s):
            if s['best_low'] is not None and s['best_high'] is not None:
                return s['best_low'] - IN_BAND <= s['rpm'] <= s['best_high'] + IN_BAND
            return abs(s['rpm'] - s['best']) <= SPOT_ON
        out.append({'name': 'shift.in_band', 'value': sum(1 for s in group if on_it(s)) / len(group),
                    'count': len(group), 'gear': gear, 'method': method})
        slips = [s['slip'] for s in group if s['slip'] is not None]
        if slips:
            out.append({'name': 'shift.slip', 'value': statistics.mean(slips), 'count': len(slips),
                        'gear': gear, 'method': method})
    by_method = {}
    for s in shifts:
        by_method.setdefault(s['method'], []).append(s)
    for method, group in sorted(by_method.items(), key=lambda kv: kv[0] or ''):
        flags = [set((s['flags'] or '').split(',')) for s in group]
        if method == 'h-pattern':
            out.append({'name': 'hpattern.neutral', 'value': statistics.median(s['neutral_time'] or 0.0 for s in group),
                        'count': len(group), 'method': method})
            ups_here = [f for s, f in zip(group, flags) if s['direction'] == 'up']
            if ups_here:
                out.append({'name': 'hpattern.missed', 'value': 100.0 * sum('missed' in f for f in ups_here)
                            / len(ups_here), 'count': len(ups_here), 'method': method})
            out.append({'name': 'hpattern.skip', 'value': 100.0 * sum('skip' in f for f in flags) / len(group),
                        'count': len(group), 'method': method})
        elif method in ('sequential', 'paddles'):
            out.append({'name': 'seq.double_tap', 'value': 100.0 * sum('double-tap' in f for f in flags)
                        / len(group), 'count': len(group), 'method': method})
        downs = [f for s, f in zip(group, flags) if s['direction'] == 'down' and s['engage_rpm'] is not None]
        if downs:
            out.append({'name': 'downshift.over_rev', 'value': 100.0 * sum('over-rev' in f for f in downs)
                        / len(downs), 'count': len(downs), 'method': method})
    return out


def _km_count(distance):
    """The weight of a per-km metric when runs are pooled: its kilometres
    (at least one), so a long stage weighs more than a short one."""
    return max(1, int(round(distance / KM)))


def trace_metrics(summary, trace, channels, corners, context):
    """The metrics a run's 10 Hz trace gives: limiter time, launch, pedals,
    handbrake, counter-steer, top gear use, exits below the power band,
    and the car's balance. `context` (car_context()): the car's limiter
    and where its power band starts, each None if unknown; the top gear
    is the game's gear count (summary 'gears')."""
    c = {name: i for i, name in enumerate(channels)}
    out = []
    if len(trace) < 10:
        return out
    t, speed, rpm, gear = c['t'], c['speed'], c['rpm'], c['gear']
    throttle, brake, handbrake = c['throttle'], c['brake'], c['handbrake']
    distance = summary.get('distance') or 0.0
    km = distance / KM
    count = _km_count(distance)
    limiter = context.get('limiter')
    top = summary.get('gears') or context.get('top_gear')

    if limiter and km > 0.2:
        below = on_top = 0.0
        for i, row in enumerate(trace):
            if (_finite(row[rpm]) and _finite(row[throttle]) and row[rpm] >= limiter * LIMITER_BAND
                    and row[throttle] >= FULL_THROTTLE and _finite(row[gear]) and row[gear] >= 1):
                if top is not None and row[gear] >= top:
                    on_top += _dt(trace, i, t)
                else:
                    below += _dt(trace, i, t)
        out.append({'name': 'limiter.per_km', 'value': below / km, 'count': count})
        if top is not None:
            out.append({'name': 'limiter.top', 'value': on_top, 'count': 1})
            moving = [i for i, row in enumerate(trace) if row[speed] > 1.0]
            in_top = [i for i in moving if _finite(trace[i][gear]) and trace[i][gear] >= top]
            if moving:
                share = sum(_dt(trace, i, t) for i in in_top) / max(1e-6, sum(_dt(trace, i, t) for i in moving))
                out.append({'name': 'top.share', 'value': share, 'count': 1})
                peaks = [trace[i][rpm] for i in in_top if _finite(trace[i][rpm]) and _finite(trace[i][throttle])
                         and trace[i][throttle] >= FULL_THROTTLE]
                if peaks:
                    out.append({'name': 'top.peak', 'value': max(peaks) / limiter, 'count': 1})

    # The launch: only when the revs were held before moving
    if (summary.get('launch') or 0.0) >= LAUNCH_HELD:
        release = summary.get('release') or 0.0
        reached = next((row[t] for row in trace if row[speed] >= LAUNCH_SPEED), None)
        if reached is not None and reached <= 20.0:
            out.append({'name': 'launch.t50', 'value': release + reached, 'count': 1})
        first = [row for row in trace if row[t] <= LAUNCH_WINDOW]
        slips = [row[c['slip_drive']] for row in first if _finite(row[c['slip_drive']])]
        if slips:
            out.append({'name': 'launch.slip', 'value': max(slips), 'count': 1})
        revs = [row[rpm] for row in first if _finite(row[rpm])]
        launch_rpm = summary.get('launch_rpm')
        if revs and launch_rpm:
            out.append({'name': 'launch.bog', 'value': float(min(revs) < BOG * launch_rpm), 'count': 1})
            out.append({'name': 'launch.stall', 'value': float(min(revs) < STALL), 'count': 1})

    if km > 0.2:
        pedals = [(i, row) for i, row in enumerate(trace) if _finite(row[throttle]) and _finite(row[brake])]
        if len(pedals) >= 0.8 * len(trace):
            moving = [(i, row) for i, row in pedals if row[speed] > 1.0]
            if moving:
                both = sum(_dt(trace, i, t) for i, row in moving if row[throttle] > OVERLAP and row[brake] > OVERLAP)
                out.append({'name': 'pedal.overlap', 'value': both / sum(_dt(trace, i, t) for i, _ in moving),
                            'count': count})
            coast = sum(_dt(trace, i, t) for i, row in pedals
                        if row[speed] > COAST_SPEED and row[throttle] < COAST and row[brake] < COAST)
            out.append({'name': 'pedal.coast', 'value': coast / km, 'count': count})
        pulls = [row[handbrake] for row in trace if _finite(row[handbrake])]
        if len(pulls) >= 0.8 * len(trace):
            edges = sum(1 for a, b in zip(pulls, pulls[1:]) if a <= HANDBRAKE_PULL < b)
            out.append({'name': 'handbrake.per_km', 'value': edges / km, 'count': count})

    steering = [(k['counter_steer'], k['duration']) for k in corners
                if k.get('counter_steer') is not None and k.get('duration')]
    if steering:
        total = sum(d for _, d in steering)
        out.append({'name': 'counter_steer', 'value': sum(f * d for f, d in steering) / total if total else 0.0,
                    'count': len(steering)})

    band_low = context.get('power_band_low')
    if band_low:
        exits = {}
        for gear_n, low in corner_exits(trace, c, corners, band_low):
            exits.setdefault(gear_n, []).append(low)
        for gear_n, lows in sorted(exits.items()):
            out.append({'name': 'exit.low', 'value': sum(lows) / len(lows), 'count': len(lows), 'gear': gear_n})

    balance = balance_gradient(trace, c)
    if balance is not None:
        out.append({'name': 'balance.gradient', 'value': balance[0], 'count': balance[1]})
    return out


def corner_exits(trace, c, corners, band_low):
    """(gear, 1 if the revs were below the power band) of each corner exit:
    the revs EXIT_DELAY after the throttle went back on past the apex."""
    t, throttle = c['t'], c['throttle']
    along = _along(trace, c)
    out = []
    for corner in corners:
        apex = bisect.bisect_left(along, corner['d'])
        if apex >= len(trace):
            continue
        on = next((i for i in range(apex, min(len(trace), apex + 60))
                   if _finite(trace[i][throttle]) and trace[i][throttle] >= EXIT_THROTTLE), None)
        if on is None:
            continue
        later = next((i for i in range(on, min(len(trace), on + 50)) if trace[i][t] - trace[on][t] >= EXIT_DELAY),
                     None)
        if later is None:
            continue
        row = trace[later]
        if _finite(row[c['gear']]) and row[c['gear']] >= 1 and _finite(row[c['rpm']]):
            out.append((int(row[c['gear']]), int(row[c['rpm']] < band_low)))
    return out


def balance_gradient(trace, c):
    """(K, rows): the fit steer = a * curvature + K * lateral g over the
    cornering rows where the driver steers into the turn. K > 0 means the
    car asks for more lock as the grip used rises (understeer), K < 0 less
    (oversteer). Its unit is the wheel's lock per g, which depends on the
    wheel's rotation setting: compare it with itself (between tunes), not
    with a number. None without steering, yaw rate and lateral
    acceleration."""
    xs, ys, zs = [], [], []
    for row in trace:
        steer, yaw, lateral, v = row[c['steer']], row[c['yaw_rate']], row[c['a_lat']], row[c['speed']]
        if not (_finite(steer) and _finite(yaw) and _finite(lateral)) or v < BALANCE_SPEED:
            continue
        if abs(yaw) < BALANCE_YAW or steer * yaw <= 0:
            continue                                   # straight, or catching a slide
        sign = 1.0 if yaw > 0 else -1.0
        xs.append(abs(yaw) / v)
        ys.append(abs(lateral) / 9.80665)
        zs.append(steer * sign)
    n = len(xs)
    if n < 50:
        return None
    # Least squares for z = a x + K y (no intercept: no lock, no turn)
    sxx = sum(x * x for x in xs)
    syy = sum(y * y for y in ys)
    sxy = sum(x * y for x, y in zip(xs, ys))
    sxz = sum(x * z for x, z in zip(xs, zs))
    syz = sum(y * z for y, z in zip(ys, zs))
    det = sxx * syy - sxy * sxy
    if det <= 1e-12 * max(1.0, sxx * syy):
        return None                                    # one speed only: curvature and grip cannot be told apart
    k = (sxx * syz - sxy * sxz) / det
    return k, n


def _along(trace, c):
    """The trace's distance column made non-decreasing (a car backing up
    does not undo the road it covered), for bisecting."""
    out, top = [], float('-inf')
    for row in trace:
        d = row[c['distance']]
        if not math.isnan(d):
            top = max(top, d)
        out.append(top)
    return out


def _time_at(trace, c, distance, along=None):
    """When the run first passed `distance` (interpolated), or None if it
    never got there or the trace starts past it."""
    along = along if along is not None else _along(trace, c)
    i = bisect.bisect_left(along, distance)
    t = c['t']
    if i >= len(trace):
        return None
    if i == 0:
        return trace[0][t] if along[0] == distance else None
    a, b = along[i - 1], along[i]
    if b == a or a == float('-inf'):
        return trace[i][t]
    return trace[i - 1][t] + (trace[i][t] - trace[i - 1][t]) * (distance - a) / (b - a)


def split_sd(traces, c, length):
    """The mean over the stage's tenths of the SD of each tenth's time,
    across runs (their traces): how evenly the driver drives it. A run
    whose trace stops just short of the end is timed to its last row."""
    marks = [length * k / SPLITS for k in range(1, SPLITS + 1)]
    splits = []
    for trace in traces:
        along = _along(trace, c)
        times = [0.0] + [_time_at(trace, c, m, along) for m in marks]
        if times[-1] is None and trace[-1][c['distance']] >= 0.98 * length:
            times[-1] = trace[-1][c['t']]
        if all(x is not None for x in times):
            splits.append([b - a for a, b in zip(times, times[1:])])
    if len(splits) < 3:
        return None
    return statistics.mean(statistics.stdev(part) for part in zip(*splits))


def corner_loss(trace, best, c, corners):
    """[(apex distance, s lost)] of this run's corners against the best
    run's trace over the same stretch, worst first."""
    losses = []
    along, best_along = _along(trace, c), _along(best, c)
    for corner in corners:
        a, b = corner['d'] - CORNER_WINDOW, corner['d'] + CORNER_WINDOW
        mine = [_time_at(trace, c, a, along), _time_at(trace, c, b, along)]
        theirs = [_time_at(best, c, a, best_along), _time_at(best, c, b, best_along)]
        if None in mine or None in theirs:
            continue
        losses.append((corner['d'], (mine[1] - mine[0]) - (theirs[1] - theirs[0])))
    return sorted(losses, key=lambda x: -x[1])


def run_metrics(summary, trace, channels, shifts, corners, context):
    """Every metric of one run: dicts with name, value, count and, where
    they apply, gear and method."""
    return shift_metrics(shifts) + trace_metrics(summary, trace, channels, corners, context)


def stage_metrics(store, run, stage, trace, channels, corners, finished):
    """The metrics that need other runs of the same stage: consistency
    (at least 3 finished runs) and the time lost in the worst corners
    against the best run (at least one other finished run)."""
    if not stage or not trace:
        return []
    c = {name: i for i, name in enumerate(channels)}
    others = store.stage_runs(stage, exclude=run, limit=20)
    out = []
    if finished:
        recent = [r for r in others if r['finished']][:SPLIT_RUNS - 1]
        traces = [trace] + [x for x in (store.trace(r['id']) for r in recent) if x]
        length = trace[-1][c['distance']]
        sd = split_sd(traces, c, length) if length and length > 0 else None
        if sd is not None:
            out.append({'name': 'consistency.split_sd', 'value': sd, 'count': len(traces)})
    finished_others = [r for r in others if r['finished'] and r['result_time']]
    if finished_others and corners:
        best = min(finished_others, key=lambda r: r['result_time'])
        best_trace = store.trace(best['id'])
        if best_trace:
            losses = corner_loss(trace, best_trace, c, corners)
            worst = [loss for _, loss in losses[:WORST_CORNERS] if loss > 0]
            if losses:
                out.append({'name': 'corner.loss', 'value': sum(worst), 'count': 1})
    return out


def power_band_low(car):
    """Where the car's power band starts (rpm): the lowest band giving
    POWER_BAND of its peak, from the pooled curve; None until known."""
    curve = car.curve()
    if len(curve) < 5:
        return None
    peak = max(curve.values())
    from .shift_learner import POWER_BIN
    lows = [band for band, power in curve.items() if power >= POWER_BAND * peak]
    return (min(lows) + 0.5) * POWER_BIN if lows else None


def car_context(car):
    """What the metrics need to know about the car (a CarModel copy). The
    top gear is the game's gear count (summary 'gears'): the gears learnt
    so far may stop short of it."""
    if car is None:
        return {}
    return {'limiter': car.ceiling() or None, 'power_band_low': power_band_low(car)}
