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
import time

from .shift_learner import LIMITER_BAND, FULL_THROTTLE, SHIFT_COVERAGE

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
    is the game's gear count (summary 'gears'). Without it the highest
    gear learnt keeps top gear out of the limiter time below it, but
    measures nothing of its own: the gears learnt may stop short."""
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
    top = summary.get('gears')
    highest = top or context.get('top_gear')

    if limiter and highest and km > 0.2:
        below = on_top = 0.0
        for i, row in enumerate(trace):
            if (_finite(row[rpm]) and _finite(row[throttle]) and row[rpm] >= limiter * LIMITER_BAND
                    and row[throttle] >= FULL_THROTTLE and _finite(row[gear]) and row[gear] >= 1):
                if row[gear] >= highest:
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
    # The known limiter, never the highest rpm seen: an early shifter has
    # only been that far, and would be told he sits on the limiter there
    gears = car.gears()
    return {'limiter': car.known_limiter() or None, 'power_band_low': power_band_low(car),
            'top_gear': gears[-1] if gears else None}


# -- over time (section 9.2) --

SESSIONS_READ = 40               # the most recent sessions the coach reads
GROWTH_EVENTS = 20               # counted events each side of a before/after comparison
HABIT_SESSIONS = 5               # sessions in a slice before anything is a habit...
HABIT_SHARE = 0.7                # ...and the share of them past the threshold
TIPS = 3                         # tips per view
STILL = 3                        # quiet "still:" lines per view
QUIET_AFTER = 2                  # showings without change before a tip goes quiet
SHOWING = 6 * 3600.0             # s: views of a tip within this of its last counted showing are the same one
RESHOW_WORSE = 0.2               # a quiet tip comes back when its value got this much worse...
RESHOW_AFTER = 14 * 86400.0      # ...or after this long
DAY = 86400.0

# Thresholds past which a metric is worth a word (calibrate all)
SHIFT_OFF = 200.0                # rpm from the best change up, on average
SHIFT_MIN = 5                    # changes before a change's average counts
SHIFT_GROWTH = 150.0             # rpm the average must move towards the best to be progress
METHOD_GAP = 200.0               # rpm between two ways of changing...
METHOD_MIN = 10                  # ...each with this many changes
SPIN_SHIFT = 0.08                # driven-wheel slip at the change: traction-limited, short-shifting can be right
LIMITER_PER_KM = 0.5             # s/km on the limiter below top gear
NEUTRAL = 0.35                   # s median through neutral on the H-pattern
RATE_PER_100 = {'hpattern.missed': 3.0, 'hpattern.skip': 3.0, 'downshift.over_rev': 5.0, 'seq.double_tap': 3.0}
BOGS = 0.5                       # share of recent launches that bogged
LAUNCHES = 3                     # launches before the launch is coached
OVERLAP_SHARE = 0.05             # of the time on the move with both pedals
CORNER_LOSS = 1.5                # s in the three worst corners against the best run of the stage
COAST_MORE = 1.0                 # s/km more coasting than the best run of the stage...
COAST_RUNS = 3                   # ...with this many runs of it

# Rough seconds each costs per 10 km, only to rank what to work on first
# (calibrate): a change 1000 rpm off costs ~0.15 s of drive, a second on
# the limiter ~0.5 s, a missed gate ~1 s.
COST_SHIFT = 0.15
COST_LIMITER = 0.5
COST_EVENT = {'hpattern.missed': 1.0, 'hpattern.skip': 0.5, 'downshift.over_rev': 0.1, 'seq.double_tap': 0.5}

METHOD_NAMES = {'h-pattern': 'the H-pattern', 'sequential': 'the sequential', 'paddles': 'the paddles'}
LOOSE = ('gravel', 'snow', 'ice', 'loose-low')


def loose(surface):
    return surface in LOOSE or (surface or '').startswith('mixed:')


class Tip:
    """One line of coaching. kind: 'focus' (the habit to work on first),
    'tip', 'praise', 'still' (a tip already shown, kept quiet), 'note'
    (what the coach is waiting for). `value` is the metric behind it, kept
    in coach_state to tell when it got worse."""

    __slots__ = ('id', 'kind', 'text', 'evidence', 'value', 'cost', 'count')

    def __init__(self, id, kind, text, evidence=None, value=None, cost=0.0, count=0):
        self.id, self.kind, self.text = id, kind, text
        self.evidence, self.value, self.cost, self.count = list(evidence or []), value, cost, count

    def to_dict(self):
        return {'id': self.id, 'kind': self.kind, 'text': self.text, 'evidence': list(self.evidence),
                'value': self.value}

    def __repr__(self):
        return 'Tip({!r}, {!r}, {!r})'.format(self.id, self.kind, self.text)


def pool(rows):
    """(mean weighted by count, total count) of metric rows."""
    total = sum(r['count'] for r in rows)
    if not total:
        return None, 0
    return sum(r['value'] * r['count'] for r in rows) / total, total


def by_session(rows):
    """[(session, started, rows)] newest first."""
    sessions = {}
    for r in rows:
        entry = sessions.setdefault(r['session'], [r['started'], []])
        entry[0] = max(entry[0], r['started'])
        entry[1].append(r)
    return sorted(((s, v[0], v[1]) for s, v in sessions.items()), key=lambda x: -x[1])


def recent(rows, need):
    """(value, count, sessions) pooled over the newest sessions until they
    hold `need` counted events; None when all of them hold fewer."""
    taken, count, n = [], 0, 0
    for _, _, session_rows in by_session(rows):
        taken.extend(session_rows)
        count += sum(r['count'] for r in session_rows)
        n += 1
        if count >= need:
            return pool(taken)[0], count, n
    return None


def growth(rows, need=GROWTH_EVENTS):
    """(before, after, sessions after, when before was driven): the newest
    sessions holding `need` events against the ones before them holding
    as many; None without enough on either side."""
    sides = [[], []]
    counts = [0, 0]
    side = 0
    sessions = [0, 0]
    when = None
    for _, started, session_rows in by_session(rows):
        if side == 1 and when is None:
            when = started
        sides[side].extend(session_rows)
        counts[side] += sum(r['count'] for r in session_rows)
        sessions[side] += 1
        if counts[side] >= need:
            if side == 1:
                return pool(sides[1])[0], pool(sides[0])[0], sessions[0], when
            side = 1
    return None


def habit_counts(rows, bad):
    """(sessions where `bad(value)` held, sessions)."""
    values = [pool(session_rows)[0] for _, _, session_rows in by_session(rows)]
    return sum(1 for v in values if bad(v)), len(values)


def habit(rows, bad):
    """True when `bad(value)` held in at least HABIT_SHARE of at least
    HABIT_SESSIONS sessions."""
    bad_ones, sessions = habit_counts(rows, bad)
    return sessions >= HABIT_SESSIONS and bad_ones >= HABIT_SHARE * sessions


def _per_10km(rows, count):
    """Events per 10 km over the runs the rows come from."""
    runs = {}
    for r in rows:
        if r['run'] is not None and r['distance']:
            runs[r['run']] = r['distance']
    km = sum(runs.values()) / KM
    return 10.0 * count / km if km > 0 else 0.0


def _ago(seconds):
    days = int(seconds // DAY)
    if days <= 0:
        return 'earlier today'
    if days == 1:
        return 'yesterday'
    if days < 14:
        return '{} days ago'.format(days)
    return '{} weeks ago'.format(days // 7)


def _where(discipline, surface):
    parts = []
    if discipline and discipline != 'unknown':
        parts.append(discipline.replace('-', ' '))
    if surface and surface != 'unknown':
        parts.append(surface)
    return ', '.join(parts)


def _method(method):
    return METHOD_NAMES.get(method, method or 'an unknown way of changing')


def _with(method):
    """' with the H-pattern', or nothing when the way is not known."""
    return ' with ' + _method(method) if method else ''


def _stage_name(stage):
    return 'this stage' if not stage else 'stage ' + stage


class Coach:
    """Coaching from the metrics history, on any reader (telemetry_store).
    `now` is the wall clock (tests set it)."""

    def __init__(self, reader, now=None):
        self.reader = reader
        self.now = now

    def tips(self, profile, car_id=None, limit=TIPS, show_all=False):
        """[Tip]: the focus habit, up to `limit` tips, one praise, the gate
        notes and up to STILL quiet lines, in that order. Nothing is marked
        as seen here (the web page only reads): see seen()."""
        now = self._now()
        rows = self.reader.metrics(profile, car_id, SESSIONS_READ)
        model = None
        if car_id is not None:
            row = self.reader.car_by_id(car_id)
            if row is not None and row['model'].get('key'):
                from .shift_learner import CarModel
                try:
                    model = CarModel.from_dict(row['model'])
                except (ValueError, KeyError, TypeError, AttributeError):
                    model = None
        candidates, praise, notes = [], [], []
        slices = {}
        for r in rows:
            slices.setdefault((r['name'], r['gear'], r['method'], r['discipline'], r['surface']), []).append(r)
        self._shift_tips(slices, model, candidates, praise, notes)
        self._method_tips(rows, candidates)
        self._rate_tips(slices, rows, candidates, praise)
        self._launch_tips(slices, candidates, praise)
        self._stage_tips(rows, candidates, praise)
        if model is not None:
            self._model_notes(model, now, candidates)
        state = self.reader.coach_state(profile, car_id)
        return select(candidates, praise, notes, state, now, limit, show_all)

    # -- the families of tips --

    def _shift_tips(self, slices, model, candidates, praise, notes):
        gated = False
        for (name, gear, method, discipline, surface), rows in sorted(
                slices.items(), key=lambda kv: tuple('' if x is None else str(x) for x in kv[0])):
            if name != 'shift.error' or gear is None:
                continue
            found = recent(rows, SHIFT_MIN)
            if found is None:
                continue
            error, count, sessions = found
            change = '{}→{}'.format(gear, gear + 1)
            where = _where(discipline, surface)
            evidence = ['{} changes up flat out in {} session{}{}.'.format(
                count, sessions, '' if sessions == 1 else 's', ' ({})'.format(where) if where else '')]
            slip = recent(slices.get(('shift.slip', gear, method, discipline, surface), []), 1)
            best = model.best_shift(gear) if model is not None else None
            best_rpm = best[0] if best and best[1] >= SHIFT_COVERAGE else None
            key = '{}:{}:{}:{}'.format(gear, method, discipline, surface)
            is_habit = habit(rows, lambda v: abs(v) > SHIFT_OFF and (v > 0) == (error > 0))
            if is_habit:
                bad_ones, all_ones = habit_counts(rows, lambda v: abs(v) > SHIFT_OFF and (v > 0) == (error > 0))
                evidence.append('More than {:.0f} rpm {} in {} of your last {} sessions.'.format(
                    SHIFT_OFF, 'late' if error > 0 else 'early', bad_ones, all_ones))
            per_10km = _per_10km(rows, sum(r['count'] for r in rows))
            cost = abs(error) / 1000.0 * COST_SHIFT * per_10km
            if error < -SHIFT_OFF:
                # Early: on a loose surface, or with the wheels spinning,
                # changing early can be right
                if loose(surface):
                    continue
                if surface in (None, 'unknown') and not (slip is not None and slip[0] < SPIN_SHIFT):
                    gated = True
                    continue
                if slip is not None:
                    evidence.append('Driven wheels slipped {:.0f} % at those changes: traction was not the '
                                    'limit.'.format(slip[0] * 100))
                if best_rpm:
                    text = '{}{}: you change up at {:.0f} rpm, {:.0f} early.{} Hold it to about {:.0f}.'.format(
                        change, _with(method), best_rpm + error, -error,
                        self._drive_lost(model, gear, best_rpm + error), best_rpm)
                else:
                    text = '{}{}: you change up about {:.0f} rpm early. Hold the gear longer.'.format(
                        change, _with(method), -error)
                candidates.append(Tip('shift.early:' + key, 'focus' if is_habit else 'tip', text, evidence,
                                      error, cost, count))
            elif error > SHIFT_OFF:
                if best_rpm and model.ceiling() and best_rpm >= model.ceiling() * 0.99:
                    text = ('{}{}: you change up on the limiter; this car pulls to the limiter in {}, so '
                            'change as the lights flash.'.format(change, _with(method), gear))
                elif best_rpm:
                    text = '{}{}: you change up at {:.0f} rpm, {:.0f} late; change at about {:.0f}.'.format(
                        change, _with(method), best_rpm + error, error, best_rpm)
                else:
                    text = '{}{}: you change up about {:.0f} rpm late; change a little sooner.'.format(
                        change, _with(method), error)
                candidates.append(Tip('shift.late:' + key, 'focus' if is_habit else 'tip', text, evidence,
                                      error, cost, count))
            went = growth(rows)
            if went is not None:
                before, after, sessions_after, when = went
                if abs(before) - abs(after) > SHIFT_GROWTH:
                    praise.append(Tip('shift.better:' + key, 'praise', 'Better: {}{} is {:.0f} rpm from the '
                                      'best over your last {} session{}; {} you were {:.0f} {}.'.format(
                                          change, _with(method), abs(after), sessions_after,
                                          '' if sessions_after == 1 else 's', _ago(self._now() - when),
                                          abs(before), 'early' if before < 0 else 'late'),
                                      evidence, after, abs(before) - abs(after)))
        if gated:
            notes.append(Tip('gate.surface', 'note', 'Tips about changing up early wait until the surface is '
                             'known: on a loose surface, short-shifting can be right.'))

    def _now(self):
        return self.now if self.now is not None else time.time()

    @staticmethod
    def _drive_lost(model, gear, rpm):
        """' 3rd gives 9 % less drive there.' when the model knows both."""
        if model is None or rpm is None:
            return ''
        this, following = model.ratio(gear), model.ratio(gear + 1)
        if not this or not following:
            return ''
        stay = model.power_at(rpm, gear) or model.power_at(rpm)
        after = model.power_at(rpm * following / this, gear + 1) or model.power_at(rpm * following / this)
        if not stay or not after or after >= stay:
            return ''
        return ' Gear {} gives {:.0f} % less drive there.'.format(gear + 1, (1 - after / stay) * 100)

    def _method_tips(self, rows, candidates):
        """The same driver, two ways of changing: where one changes up
        earlier than the other."""
        per_method = {}
        for r in rows:
            if r['name'] == 'shift.error' and r['method'] is not None:
                per_method.setdefault(r['method'], []).append(r)
        pooled = {m: pool(v) for m, v in per_method.items()}
        methods = sorted(m for m, (_, n) in pooled.items() if n >= METHOD_MIN)
        for i, a in enumerate(methods):
            for b in methods[i + 1:]:
                gap = pooled[a][0] - pooled[b][0]
                if abs(gap) >= METHOD_GAP:
                    early, late = (a, b) if gap < 0 else (b, a)
                    text = 'With {} you change up {:.0f} rpm earlier than with {}.'.format(
                        _method(early), abs(gap), _method(late))
                    candidates.append(Tip('shift.method:{}:{}'.format(a, b), 'tip', text,
                                          ['{} and {} changes up flat out.'.format(pooled[a][1], pooled[b][1])],
                                          abs(gap), 0.1, pooled[a][1] + pooled[b][1]))

    def _rate_tips(self, slices, rows, candidates, praise):
        limiter = [r for r in rows if r['name'] == 'limiter.per_km']
        found = recent(limiter, 5)
        if found is not None and found[0] > LIMITER_PER_KM:
            value, count, sessions = found
            is_habit = habit(limiter, lambda v: v > LIMITER_PER_KM)
            candidates.append(Tip('limiter', 'focus' if is_habit else 'tip', '{:.1f} s per km on the limiter at '
                                  'full throttle below top gear: the engine makes nothing there. Change up when '
                                  'the lights flash.'.format(value),
                                  ['Over {} km in {} session{}.'.format(count, sessions, '' if sessions == 1 else 's')],
                                  value, value * 10 * COST_LIMITER, count))
        went = growth(limiter, GROWTH_EVENTS)
        if went is not None and went[0] > LIMITER_PER_KM and went[1] <= went[0] / 2:
            before, after, n, when = went
            praise.append(Tip('limiter.better', 'praise', 'Better: {:.1f} s per km on the limiter over your last {} '
                              'session{}, from {:.1f} {}.'.format(after, n, '' if n == 1 else 's', before,
                                                                  _ago(self._now() - when)),
                              value=after, cost=before - after))
        for method in METHOD_NAMES:
            neutral = [r for r in rows if r['name'] == 'hpattern.neutral' and r['method'] == method]
            found = recent(neutral, 10)
            if found is not None and found[0] > NEUTRAL:
                value, count, sessions = found
                shifts = _per_10km(neutral, sum(r['count'] for r in neutral))
                candidates.append(Tip('hpattern.neutral', 'focus' if habit(neutral, lambda v: v > NEUTRAL) else 'tip',
                                      'Your H-pattern changes spend {:.2f} s in neutral (the median of {}): a quicker, '
                                      'firmer throw keeps the drive on.'.format(value, count),
                                      value=value, cost=(value - 0.25) * 0.5 * shifts, count=count))
            went = growth(neutral)
            if went is not None and went[1] <= went[0] * 0.8:
                before, after, n, when = went
                praise.append(Tip('hpattern.neutral.better', 'praise', 'Quicker: {:.2f} s in neutral per H-pattern '
                                  'change over your last {} session{}, from {:.2f} {}.'.format(
                                      after, n, '' if n == 1 else 's', before, _ago(self._now() - when)),
                                  value=after, cost=before - after))
        sentences = {
            'hpattern.missed': ('You miss {:.0f} in 100 H-pattern changes up (neutral, foot down): push the lever '
                                'through the gate before you floor it.'),
            'hpattern.skip': '{:.0f} in 100 of your H-pattern changes skip a gear: guide the lever along its plane.',
            'downshift.over_rev': ('{:.0f} in 100 changes down over-rev the engine: brake a moment longer before '
                                   'going down.'),
            'seq.double_tap': ('{:.0f} in 100 changes with {} are taken twice and put back: one firm pull per '
                               'gear.'),
        }
        per_method = {}
        for r in rows:
            if r['name'] in RATE_PER_100:
                per_method.setdefault((r['name'], r['method']), []).append(r)
        for (name, method), slice_rows in sorted(per_method.items(), key=lambda kv: (kv[0][0], kv[0][1] or '')):
            found = recent(slice_rows, 30)
            limit = RATE_PER_100[name]
            if found is None or found[0] <= limit:
                continue
            value, count, sessions = found
            text = sentences[name].format(value, _method(method))
            per_10km = _per_10km(slice_rows, sum(r['count'] for r in slice_rows))
            candidates.append(Tip('{}:{}'.format(name, method), 'focus' if habit(slice_rows, lambda v: v > limit)
                                  else 'tip', text, ['{} changes in {} session{}.'.format(
                                      count, sessions, '' if sessions == 1 else 's')],
                                  value, value / 100.0 * per_10km * COST_EVENT[name], count))

    def _launch_tips(self, slices, candidates, praise):
        rows = [r for (name, *_), v in slices.items() if name == 'launch.bog' for r in v]
        last = sorted(rows, key=lambda r: -r['started'])[:5]
        if len(last) >= LAUNCHES:
            bogged = sum(1 for r in last if r['value'] >= 1.0)
            if bogged / len(last) >= BOGS:
                candidates.append(Tip('launch.bog', 'tip', '{} of your last {} launches bogged down (the revs fell '
                                      'below {:.0f} % of the launch revs): let the clutch out more gradually, or hold '
                                      'more revs.'.format(bogged, len(last), BOG * 100),
                                      value=bogged / len(last), cost=bogged / len(last) * 0.5, count=len(last)))
        rows = [r for (name, *_), v in slices.items() if name == 'launch.stall' for r in v]
        last = sorted(rows, key=lambda r: -r['started'])[:5]
        stalled = sum(1 for r in last if r['value'] >= 1.0)
        if stalled >= 2:
            candidates.append(Tip('launch.stall', 'tip', 'You stalled {} of your last {} launches: more revs, and '
                                  'the clutch out more gently.'.format(stalled, len(last)),
                                  value=stalled / len(last), cost=stalled * 1.0, count=len(last)))
        rows = [r for (name, *_), v in slices.items() if name == 'launch.t50' for r in v]
        went = growth(rows, 5)
        if went is not None and went[1] <= went[0] * 0.9:
            before, after, n, when = went
            praise.append(Tip('launch.better', 'praise', 'Quicker off the line: {:.1f} s to 50 km/h over your last {} '
                              'session{}, from {:.1f} {}.'.format(after, n, '' if n == 1 else 's', before,
                                                                  _ago(self._now() - when)),
                              value=after, cost=before - after))

    def _stage_tips(self, rows, candidates, praise):
        """What only the same stage can say: coasting against the best run
        there, corners lost against it, consistency."""
        stages = {}
        for r in rows:
            if r['stage']:
                stages.setdefault(r['stage'], []).append(r)
        for stage, stage_rows in sorted(stages.items()):
            name = _stage_name(stage)
            loss = sorted((r for r in stage_rows if r['name'] == 'corner.loss'), key=lambda r: -r['started'])
            if loss and loss[0]['value'] > CORNER_LOSS:
                candidates.append(Tip('corner.loss:' + stage, 'tip', 'On {}, your last run lost {:.1f} s in its three '
                                      'worst corners against your best run there.'.format(name, loss[0]['value']),
                                      value=loss[0]['value'], cost=loss[0]['value'], count=1))
            overlap = [r for r in stage_rows if r['name'] == 'pedal.overlap'
                       and (r['discipline'] == 'circuit' or r['surface'] == 'tarmac')]
            found = recent(overlap, 5)
            if found is not None and found[0] > OVERLAP_SHARE and loss and loss[0]['value'] > 0.5:
                # Left-foot braking is technique on loose surfaces; on tarmac
                # it only earns a word when the corners are slower too
                candidates.append(Tip('pedal.overlap:' + stage, 'tip', 'On {} you are on both pedals {:.0f} % of the '
                                      'time on the move, and slower through the corners than your best there: come '
                                      'off the brake before the throttle goes back on.'.format(name, found[0] * 100),
                                      value=found[0], cost=found[0] * 20, count=found[1]))
            coast = sorted((r for r in stage_rows if r['name'] == 'pedal.coast'), key=lambda r: -r['started'])
            if len(coast) >= COAST_RUNS:
                best = min(r['value'] for r in coast[1:])
                last = coast[0]['value']
                if last - best > COAST_MORE and last > 1.3 * best:
                    candidates.append(Tip('pedal.coast:' + stage, 'tip', 'On {} you coasted {:.1f} s per km, {:.1f} '
                                          'more than your best run there: stay on one pedal or the other.'.format(
                                              name, last, last - best),
                                          value=last, cost=(last - best) * 10 * 0.3, count=len(coast)))
            steady = sorted((r for r in stage_rows if r['name'] == 'consistency.split_sd'),
                            key=lambda r: -r['started'])
            if len(steady) >= 4 and steady[0]['value'] <= 0.7 * max(r['value'] for r in steady[1:]):
                praise.append(Tip('consistency:' + stage, 'praise', 'Steadier on {}: your splits vary by {:.1f} s, '
                                  'from {:.1f}.'.format(name, steady[0]['value'], max(r['value'] for r in steady[1:])),
                                  value=steady[0]['value'], cost=0.1))

    def _model_notes(self, model, now, candidates):
        """What the car's model says it is still learning."""
        from .shift_learner import POWER_BIN, POWER_MIN
        recent_tunes = sorted(g for g, at in model.retuned.items() if now - at < 7 * DAY)
        if recent_tunes:
            candidates.append(Tip('retuned:' + ','.join(str(g) for g in recent_tunes), 'tip', '{} re-tuned; {} shift '
                                  'points are being learnt again.'.format(
                                      'Gear {} was'.format(recent_tunes[0]) if len(recent_tunes) == 1 else
                                      'Gears {} were'.format(', '.join(str(g) for g in recent_tunes)),
                                      'its' if len(recent_tunes) == 1 else 'their'), value=0.0, cost=0.01))
        ceiling = model.ceiling()
        needed = int(ceiling * 0.5 / POWER_BIN) if ceiling else 0
        bands = sum(1 for v in model.power.values() if len(v) >= POWER_MIN)
        if needed and bands < needed * 0.8:
            candidates.append(Tip('learning', 'tip', 'Still learning the engine ({} of about {} rev bands known): '
                                  'full-throttle pulls from low revs, out of slow corners, fill it in fastest.'.format(
                                      bands, needed), value=0.0, cost=0.0))


def select(candidates, praise, notes, state, now, limit=TIPS, show_all=False):
    """Rate limiting (coach_state): the focus (the costliest habit), then up
    to `limit` tips by cost, a tip shown QUIET_AFTER times without getting
    worse turning into a quiet "still:" line (at most STILL, the most
    recently shown) until it gets RESHOW_WORSE worse or RESHOW_AFTER has
    passed; one praise, not repeated once shown twice unless it grew; each
    note once."""
    def fresh(tip):
        seen = state.get(tip.id)
        if show_all or seen is None or (seen['times'] or 0) < QUIET_AFTER:
            return True
        if seen['last_shown'] is not None and now - seen['last_shown'] >= RESHOW_AFTER:
            return True
        old = seen['value']
        return old is not None and tip.value is not None and abs(tip.value) >= abs(old) * (1 + RESHOW_WORSE) \
            and abs(tip.value) > 0

    out, still = [], []
    ranked = sorted(candidates, key=lambda tip: (-tip.cost, -tip.count, tip.id))
    focus = [tip for tip in ranked if tip.kind == 'focus']
    if focus:
        out.append(focus[0])
    tips = 0
    for tip in ranked:
        if focus and tip is focus[0]:
            continue
        tip.kind = 'tip'
        if fresh(tip) and (show_all or tips < limit):
            out.append(tip)
            tips += 1
        elif not fresh(tip):
            still.append(tip)
    for tip in sorted(praise, key=lambda tip: -tip.cost):
        seen = state.get(tip.id)
        grew = seen is not None and seen['value'] is not None and tip.value is not None \
            and abs(tip.value) <= abs(seen['value']) * (1 - RESHOW_WORSE)
        if show_all or seen is None or (seen['times'] or 0) < QUIET_AFTER or grew \
                or (seen['last_shown'] is not None and now - seen['last_shown'] >= RESHOW_AFTER):
            out.append(tip)
            break
    for tip in notes:
        if show_all or tip.id not in state:
            out.append(tip)
    still.sort(key=lambda tip: -(state.get(tip.id, {}).get('last_shown') or 0.0))
    for tip in still[:STILL]:
        tip.kind = 'still'
        if not tip.text.startswith('Still'):
            tip.text = 'Still: ' + tip.text
        out.append(tip)
    return out


def seen(store, profile, car_id, tips, at=None):
    """Record that `tips` were shown (drive-log thread: the GTK tab calls
    it through the drive log; the web page never does). The tab redraws
    on every visit and every pause in the telemetry, so a showing is a
    sitting: views within SHOWING of the last counted one add nothing."""
    for tip in tips:
        store.coach_seen(profile, car_id, tip.id, tip.value, at, quiet=tip.kind == 'still', gap=SHOWING)
