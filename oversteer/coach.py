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

import numpy as np

from . import car_data, coach_context, coach_diagnosis, potential, stage_tables
from .shift_learner import LIMITER_BAND, FULL_THROTTLE, SURFACES, _listed

# -- metrics of one run (section 9.1) --

KM = 1000.0
LAUNCH_WINDOW = 2.0              # s after the start in which a launch spins (slip_drive)
HANDBRAKE_PULL = 0.5
POWER_BAND = 0.85                # share of peak power where the power band starts (calibrate)
EXIT_DELAY = 1.0                 # s after the throttle goes back on that an exit's revs are read
EXIT_THROTTLE = 0.5
BALANCE_YAW = 0.1                # rad/s: cornering, for the balance fit
BALANCE_SPEED = 8.0              # m/s
SPLITS = 10                      # the stage cut into this many parts for consistency
SPLIT_RUNS = 5                   # the most recent finished runs compared for consistency
WORST_CORNERS = 3
COUNTER_STEER_HEADING = 90.0     # degrees: only the bends count for the balance (a hairpin is rotated)
BOG_ACCEL = 0.8                  # an exit with low revs that accelerates under this share of the in-band exits' median: a bog
BOG_SPEED = 5.0                  # m/s: the in-band exits it is compared with are at the same speed within this
BOG_PEERS = 3                    # in-band exits needed to judge one
POWER_SLIDE = 0.3                # rad/s of yaw in the second after the throttle: a slide, no verdict on the gear
EXIT_SPIN = 0.15                 # slip_rpm over this in that second: the exit spun


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
    """Per (gear, method) of the flat-out changes up by one that were
    classified (coach_context.classify_shift, which the caller has done): the
    shares `shift.in_band` (on the band), `shift.early_share` and
    `shift.cut_share`, `shift.cost` (seconds this run's early and cut changes
    cost on the stage, coach_context.shift_costs) and `shift.slip`; and the
    H-pattern, sequential and change-down rates. The launch's change up
    belongs to the launch and is left out."""
    out = []
    ups = _shift_groups(shifts, lambda s: s.get('class') in ('early', 'on', 'late', 'cut'))
    for (gear, method), group in sorted(ups.items(), key=lambda kv: (kv[0][0], kv[0][1] or '')):
        n = len(group)

        def share(label):
            return sum(1 for s in group if s['class'] == label) / n
        out.append({'name': 'shift.in_band', 'value': share('on'), 'count': n, 'gear': gear, 'method': method})
        out.append({'name': 'shift.early_share', 'value': share('early'), 'count': n, 'gear': gear, 'method': method})
        out.append({'name': 'shift.cut_share', 'value': share('cut'), 'count': n, 'gear': gear, 'method': method})
        costs = [s['cost'] for s in group if s.get('cost') is not None]
        if costs:
            out.append({'name': 'shift.cost', 'value': sum(costs), 'count': 1, 'gear': gear, 'method': method})
        slips = [s['slip'] for s in group if s.get('slip') is not None]
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


def trace_metrics(summary, trace, channels, corners, context, analysis=None):
    """The metrics a run's 10 Hz trace gives: the limiter held on a
    straight, top gear use, the launch, the pedals by phase, handbrake,
    counter-steer, exits that bogged or spun, and the car's balance.
    `context` (car_context()): the car's limiter and where its power band
    starts, each None if unknown; the top gear is the game's gear count
    (summary 'gears'), else the shipped gear set's (context 'shipped_top'):
    the gears learnt may stop short of it. `analysis` is what
    coach_context.analyse() made of the run (episodes, launch, phases of the
    corners, the run's class); without it, it is worked out here from the
    trace and `corners` alone."""
    c = {name: i for i, name in enumerate(channels)}
    out = []
    if len(trace) < 10:
        return out
    if analysis is None:
        analysis = coach_context.analyse(summary, trace, corners, [], context)
    corners = analysis['corners']
    klass = analysis['run_class']
    t, speed, rpm, gear = c['t'], c['speed'], c['rpm'], c['gear']
    throttle, brake, handbrake = c['throttle'], c['brake'], c['handbrake']
    distance = summary.get('distance') or 0.0
    km = distance / KM
    count = _km_count(distance)
    limiter = context.get('limiter')
    top = summary.get('gears') or context.get('shipped_top')
    highest = top or context.get('top_gear')

    if limiter and highest and km > 0.2:
        # Only the stretches held on the cut with nothing to ask for the
        # revs: a gear held on the limiter between corners is right (design,
        # section 7.3 R2), the other classes are the shift, the crawl, the
        # wheelspin and the corner
        out.append({'name': 'limiter.held', 'value': coach_context.held_seconds(analysis['episodes']) / km,
                    'count': count})
    if limiter and top and km > 0.2:
        on_top = 0.0
        for i, row in enumerate(trace):
            if (_finite(row[rpm]) and _finite(row[throttle]) and row[rpm] >= limiter * LIMITER_BAND
                    and row[throttle] >= FULL_THROTTLE and _finite(row[gear]) and row[gear] >= top):
                on_top += _dt(trace, i, t)
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

    # The launch: only when the revs were held before moving, and not when the
    # driver restarted within seconds
    launch = analysis['launch']
    if launch is not None and not launch['dropped']:
        if launch['t50'] is not None:
            out.append({'name': 'launch.t50', 'value': launch['t50'], 'count': 1})
        if launch['g'] is not None:
            out.append({'name': 'launch.g', 'value': launch['g'], 'count': 1})
        if launch['cut'] is not None and not launch['game']:
            out.append({'name': 'launch.cut', 'value': launch['cut'], 'count': 1})
        first = [row for row in trace if row[t] - trace[0][t] <= LAUNCH_WINDOW]
        slips = [row[c['slip_drive']] for row in first if _finite(row[c['slip_drive']])]
        if slips:
            out.append({'name': 'launch.slip', 'value': max(slips), 'count': 1})
        elif launch['spin'] is not None:
            out.append({'name': 'launch.slip', 'value': launch['spin'], 'count': 1})
        if not launch['game']:
            # The game's own launch neither bogs nor is the driver's
            if launch['bog'] is not None:
                out.append({'name': 'launch.bog', 'value': launch['bog'], 'count': 1})
            if launch['stall'] is not None:
                out.append({'name': 'launch.stall', 'value': launch['stall'], 'count': 1})

    if km > 0.2 and klass not in ('partial', 'restart'):
        pedals = [(i, row) for i, row in enumerate(trace) if _finite(row[throttle]) and _finite(row[brake])]
        if len(pedals) >= 0.8 * len(trace):
            events = analysis['events']
            skip = [(e['d0'] - coach_context.INCIDENT_REACH, e['d1'] + coach_context.INCIDENT_REACH)
                    for e in events if e['kind'] in ('off', 'stop')]
            kept = [k for k in corners if not k.get('off')]
            entry, leave = (sum(k.get('_' + p + '_t') or 0.0 for k in kept) for p in ('entry', 'exit'))
            if entry > 0:
                out.append({'name': 'pedal.overlap_entry', 'value': sum(k.get('overlap_entry') or 0.0 for k in kept)
                            / entry, 'count': len(kept)})
                out.append({'name': 'pedal.coast_entry', 'value': sum(k.get('coast_entry') or 0.0 for k in kept) / km,
                            'count': count})
            if leave > 0:
                out.append({'name': 'pedal.overlap_exit', 'value': sum(k.get('overlap_exit') or 0.0 for k in kept)
                            / leave, 'count': len(kept)})
                out.append({'name': 'pedal.coast_exit', 'value': sum(k.get('coast_exit') or 0.0 for k in kept) / km,
                            'count': count})
            downs = [s['t'] for s in analysis['shifts'] if s.get('method') == 'h-pattern'
                     and s.get('direction') == 'down' and s.get('t') is not None]
            coast, both, _straight_km = coach_context.pedal_time(trace, corners, downs, skip)
            out.append({'name': 'pedal.coast_straight', 'value': coast / km, 'count': count})
            out.append({'name': 'pedal.drag', 'value': both / km, 'count': count})
        pulls = [row[handbrake] for row in trace if _finite(row[handbrake])]
        if len(pulls) >= 0.8 * len(trace):
            edges = sum(1 for a, b in zip(pulls, pulls[1:]) if a <= HANDBRAKE_PULL < b)
            out.append({'name': 'handbrake.per_km', 'value': edges / km, 'count': count})

    # Steering against the turn: the bends, not the hairpins (a hairpin on
    # tarmac is rotated, which looks like a slide), and not where something went wrong
    steering = [(k['counter_steer'], k['duration']) for k in corners
                if k.get('counter_steer') is not None and k.get('duration') and not k.get('off')
                and (k.get('heading_change') or 0.0) < COUNTER_STEER_HEADING]
    if steering:
        total = sum(d for _, d in steering)
        out.append({'name': 'counter_steer', 'value': sum(f * d for f, d in steering) / total if total else 0.0,
                    'count': len(steering)})

    band_low = context.get('power_band_low')
    if band_low and klass not in ('restart',):
        exits = {}
        for e in corner_exits(trace, c, corners, band_low, analysis['slip']):
            exits.setdefault(e['gear'], []).append(e)
        for gear_n, found in sorted(exits.items()):
            judged = [e for e in found if e['bog'] is not None]
            if judged:
                out.append({'name': 'exit.low', 'value': sum(e['bog'] for e in judged) / len(judged),
                            'count': len(judged), 'gear': gear_n})
            spun = [e['spun'] for e in found if e['spun'] is not None]
            if spun:
                out.append({'name': 'exit.spin', 'value': sum(spun) / len(spun), 'count': len(spun), 'gear': gear_n})

    balance = balance_gradient(trace, c)
    if balance is not None:
        out.append({'name': 'balance.gradient', 'value': balance[0], 'count': balance[1]})
    return out


def corner_exits(trace, c, corners, band_low, slip=None):
    """The exits of the run's corners: dicts of `gear` (held at the throttle
    going back on, past the slowest point), `bog` (1.0, 0.0 or None) and
    `spun` (1.0, 0.0 or None). The revs EXIT_DELAY after the throttle went
    back on are read only when the gear is the same by then, and not in a
    slide (yaw rate over POWER_SLIDE in that second says nothing of the
    gear). An exit with the revs below the power band bogs only when it
    accelerates under BOG_ACCEL of the median of the in-band exits of the
    run at the same speed (BOG_SPEED), at least BOG_PEERS of them, else it
    is not judged; one with the revs in the band does not bog. `spun`: the
    driven wheels spun in that second (slip_rpm over EXIT_SPIN on most
    rows), where the car's gearing is known."""
    t, throttle = c['t'], c['throttle']
    along_ = _along(trace, c)
    found = []
    for corner in corners:
        if corner.get('off'):
            continue
        apex = bisect.bisect_left(along_, corner['d'])
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
        g = trace[on][c['gear']]
        if not (_finite(g) and g >= 1 and trace[later][c['gear']] == g and _finite(trace[later][c['rpm']])):
            continue
        rows = range(on, later + 1)
        yaws = [abs(trace[i][c['yaw_rate']]) for i in rows if _finite(trace[i][c['yaw_rate']])]
        if yaws and max(yaws) > POWER_SLIDE:
            continue
        accel = [trace[i][c['a_long']] for i in rows if _finite(trace[i][c['a_long']])]
        spun = None
        if slip is not None:
            vals = [slip[i] for i in rows if slip[i] is not None]
            if vals:
                spun = float(sum(1 for v in vals if v > EXIT_SPIN) > 0.5 * len(vals))
        found.append({'gear': int(g), 'low': trace[later][c['rpm']] < band_low, 'speed': trace[on][c['speed']],
                      'accel': sum(accel) / len(accel) if accel else None, 'spun': spun, 'bog': None})
    for e in found:
        if not e['low']:
            e['bog'] = 0.0
            continue
        peers = [p['accel'] for p in found if not p['low'] and p['accel'] is not None
                 and abs(p['speed'] - e['speed']) <= BOG_SPEED]
        if e['accel'] is not None and len(peers) >= BOG_PEERS:
            e['bog'] = float(e['accel'] < BOG_ACCEL * statistics.median(peers))
    return found


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
        times = [trace[0][c['t']]] + [_time_at(trace, c, m, along) for m in marks]
        if times[-1] is None and trace[-1][c['distance']] >= 0.98 * length:
            times[-1] = trace[-1][c['t']]
        if all(x is not None for x in times):
            splits.append([b - a for a, b in zip(times, times[1:])])
    if len(splits) < 3:
        return None
    return statistics.mean(statistics.stdev(part) for part in zip(*splits))


def run_metrics(summary, trace, channels, shifts, corners, context, analysis=None):
    """Every metric of one run: dicts with name, value, count and, where
    they apply, gear and method. `analysis` is coach_context.analyse() of
    the run; without it, it is made from the arguments."""
    if analysis is None:
        analysis = coach_context.analyse(summary, trace, corners, shifts, context)
    return shift_metrics(analysis['shifts']) + trace_metrics(summary, trace, channels, corners, context, analysis)


def stage_metrics(store, run, stage, trace, channels, corners, finished, analysis=None, car=None):
    """The metrics that need other runs of the same stage: consistency (at
    least 3 finished clean runs of the same car, cut where they crossed the
    finish), the time lost in the three worst sections against the car's
    best run there (analysis['loss'], coach_context.section_loss: sections
    that do not overlap, an off's surroundings left out; none for a
    learning run) and the spread of the minimum speed through the same
    section over the last runs (coach_context.spread). `trace` is the
    stage's rows."""
    if not stage or not trace:
        return []
    if finished == 1 and car is not None and hasattr(store, 'save_potential'):
        # The post-run hook of the potential time (oversteer/potential.py): this is the coach's one path after a
        # run is written that is not in drive_log, on the drive-log thread. It stores, it adds no metric.
        potential.after_run(store, run)
    c = {name: i for i, name in enumerate(channels)}
    analysis = analysis or {}
    klass = analysis.get('run_class')
    out = []
    if finished and klass in (None, 'clean'):
        others = store.stage_runs(stage, exclude=run, limit=20, car=car, run_class='clean')
        recent = [r for r in others if r['finished']][:SPLIT_RUNS - 1]
        traces = [trace]
        for r in recent:
            found = store.trace(r['id'])
            if found:
                traces.append(coach_context.stage_rows(found, r.get('course'), True, r.get('result_time')))
        length = trace[-1][c['distance']]
        sd = split_sd(traces, c, length) if length and length > 0 else None
        if sd is not None:
            out.append({'name': 'consistency.split_sd', 'value': sd, 'count': len(traces)})
    if analysis.get('loss') is not None and klass != 'learning':
        # Not the first section: it holds the launch (R3 judges that, not a corner)
        launch = {id(k) for i, sec in enumerate(analysis.get('sections') or ())
                  if i == 0 or sec.get('grid') == 0 for k in sec['corners']}
        losses = sorted((k['loss_entry'] + k['loss_exit'] for k in corners
                         if k.get('loss_entry') is not None and not k.get('off') and id(k) not in launch),
                        reverse=True)
        out.append({'name': 'corner.loss', 'value': sum(x for x in losses[:WORST_CORNERS] if x > 0), 'count': 1})
    spreads = analysis.get('spreads')
    if spreads:
        out.append({'name': 'corner.spread', 'value': max(e['value'] for e in spreads), 'count': len(spreads)})
    return out


def power_band_low(car):
    """Where the car's power band starts (rpm): the lowest band giving
    POWER_BAND of its peak, from the game's torque curve where shipped,
    else the pooled curve, else the gears' own curves together; None
    until known."""
    from .shift_learner import POWER_BIN
    from . import car_data
    if car.shipped:
        limiter = car.known_limiter()
        curve = {rpm // POWER_BIN: car_data.torque(car.shipped, rpm) * rpm
                 for rpm in range(POWER_BIN // 2, int(limiter) + 1, POWER_BIN)}
    else:
        curve = car.curve()
        if len(curve) < 5:
            curve = {}
            for gear in car.gears():
                for band, power in car.curve(gear).items():
                    curve[band] = max(power, curve.get(band, power))
    if len(curve) < 5:
        return None
    peak = max(curve.values())
    lows = [band for band, power in curve.items() if power >= POWER_BAND * peak]
    return (min(lows) + 0.5) * POWER_BIN if lows else None


def _drive_loss(car, gear, rpm):
    """The share of the drive changing up from `gear` at `rpm` loses (the
    next gear pulls that much less at the same road speed), 0 where it gains,
    None where the car's data cannot say."""
    step = car.step(gear)
    if not step:
        return None
    if car.game_data():
        stay, after = car.engine_drive(gear, rpm), car.engine_drive(gear + 1, rpm * step)
    else:
        stay = car.power_at(rpm, gear) or car.power_at(rpm)
        after = car.power_at(rpm * step, gear + 1) or car.power_at(rpm * step)
    if not stay or not after:
        return None
    return max(0.0, 1.0 - after / stay)


def car_context(car, surface=None):
    """What the metrics need to know about the car (a CarModel copy) on
    `surface`. The top gear is the game's gear count (summary 'gears') where
    the game sends one, else the shipped gear set, else the gears learnt
    so far (which may stop short of it). `ratio`, `best_for` and `pulls` are
    functions of a gear (the game's gearing on that surface, the best change
    up and the pulls its grip was measured from); `lights` the game's own
    shift lights; `drivetrain` the shipped one where there is one."""
    if car is None:
        return {}
    # The known limiter, never the highest rpm seen: an early shifter has
    # only been that far, and would be told he sits on the limiter there
    shipped = car.shipped or {}
    grip_surface = surface if surface in SURFACES else None
    return {'limiter': car.known_limiter() or None, 'power_band_low': power_band_low(car),
            'top_gear': car.top_gear(), 'shipped_top': car.top_gear() if car.game_data() else None,
            'surface': surface, 'lights': shipped.get('shift_lights_rpm'),
            'drivetrain': shipped.get('drivetrain') or car.drivetrain, 'turbo': shipped.get('turbo'),
            'ratio': (lambda gear: car.game_ratio(gear, grip_surface)) if car.shipped else None,
            'best_for': lambda gear: car.best_for(gear, grip_surface),
            'drive_loss': lambda gear, rpm: _drive_loss(car, gear, rpm),
            'pulls': lambda gear: len({v[3] for v in car.drive.get((grip_surface, gear), ())})}


# -- over time (section 9.2) --

SESSIONS_READ = 40               # the most recent sessions the coach reads
GROWTH_EVENTS = 20               # counted events each side of a before/after comparison
HABIT_SESSIONS = 5               # sessions in a slice before anything is a habit...
HABIT_SHARE = 0.7                # ...and the share of them past the threshold
TIPS = 4                         # tips per view (the owner asked for a coach that speaks more readily)
STILL = 3                        # quiet "still:" lines per view
QUIET_AFTER = 2                  # showings without change before a tip goes quiet
SHOWING = 6 * 3600.0             # s: views of a tip within this of its last counted showing are the same one
RESHOW_WORSE = 0.2               # a quiet tip comes back when its value got this much worse...
RESHOW_AFTER = 14 * 86400.0      # ...or after this long
DAY = 86400.0

# Thresholds past which a metric is worth a word (calibrate all)
SHIFT_MIN = 5                    # changes before a change's shares count
SHIFT_CUT = 0.25                 # share of changes up on the cut: a tip
SHIFT_EARLY = 0.6                # share of changes up below the band: a tip (tarmac, circuit)
SHIFT_TWO = 0.3                  # early share that, with SHIFT_CUT cut, is the two-places tip
SHIFT_ON = 0.6                   # share on the band (with cut at most SHIFT_ON_CUT): praise
SHIFT_ON_CUT = 0.1
SHIFT_COST_MIN = 0.2             # s a stage that a change-up tip must cost; cheaper is described once
COST_RUNS = 3                    # runs the cost of a shift fault is averaged over
SHIFT_GROWTH = 0.2               # share of changes on the band the newest sessions must gain to be progress
METHOD_GAP = 0.3                 # share on the band between two ways of changing...
METHOD_MIN = 10                  # ...each with this many changes
LIMITER_PER_KM = 0.5             # s/km held on the limiter on a straight, below top gear
NEUTRAL = 0.35                   # s median through neutral on the H-pattern
RATE_PER_100 = {'hpattern.missed': 3.0, 'hpattern.skip': 3.0, 'downshift.over_rev': 5.0, 'seq.double_tap': 3.0}
BOGS = 0.5                       # share of recent launches that bogged
LAUNCHES = 3                     # launches before the launch is coached
DRAG_PER_KM = 0.5                # s/km on both pedals on the straights (tarmac, circuit)
CORNER_LOSS = 1.5                # s in the three worst sections against the best run of the stage
BUDGET = 0.1                     # a tip is shown when it costs this share of the costliest one
PRAISE = 3                       # praise lines per view (and one is forced in when two tips show)
TECHNIQUE_SHIFT = 0.2            # a technique line comes back when its share moved this far (20 points)
STAGES_COACHED = 3               # the stages (newest runs) whose sections are read for tips
SECTION_TIPS = 3                 # the costliest sections named per stage
TWO_PLACES = 0.5                 # the two costliest sections hold this share of the loss: "most of it in two places"
POSSIBLE_RUNS = 8                # runs whose sections are put together for the "possible" time
POSSIBLE_MIN = 3                 # runs (the reference among them) it needs
LATE_THROTTLE = 0.3              # s later than the reference on the throttle, in at least LATE_CORNERS corners: a tip
LATE_CORNERS = 2
OVERLAP_EXIT = 0.3               # s on both pedals leaving a corner that lost time, in OVERLAP_CORNERS corners
OVERLAP_CORNERS = 3
COAST_LONGER = 0.3               # s more coasting into a corner than the reference did (tarmac)
LEFT_FOOT = 0.2                  # s on both pedals on a corner's entry: it was left-foot braked
LEFT_FOOT_SHARE = 0.3            # share of the corners that is worth a word
SPREAD_KMH = 8.0                 # SD of the minimum speed through a section over the runs, km/h...
SPREAD_SHARE = 0.1               # ...and this share of its median: a tip
SPREAD_STEADY_KMH = 4.0          # the five costliest sections within this: praise
COST_SPREAD = 0.03               # rough s a stage per km/h of spread (no reference gives seconds)
COST_SPIN, COST_STALL = 2.0, 1.0 # rough s a spin or a near stop in a corner costs
COST_LAUNCH_CUT = 0.5            # rough s a launch loses for each second held on the limiter in the launch gear
HELD_RUNS = 5                    # runs whose held-corner limiter episodes are looked at for a recurring place
HELD_RECUR = 3                   # runs the same place must be in
HELD_LONG = 1.0                  # s the mean episode at that place must last
HELD_NEAR = 100.0                # m: episodes this close are one place
HELD_GAIN = 0.1                  # s the reference must have been quicker to the next braking for a held straight
LAUNCH_CUT = 0.3                 # s on the limiter in the launch gear: a late change
LAUNCH_CUTS = 3                  # of the last five launches
LAUNCH_GAME = 0.02               # s: five launches within this of each other are the game's launch, not a driver's (they never repeat to a hundredth)
LAUNCH_STEADY = 0.1              # s: the last five launches within this of each other and of the best: praise
LAUNCH_OUT = ('wrcg',)           # games whose speed channel is not checked: no launch rules
ATTITUDE_OUT = ('wrcg',)         # games whose steer and yaw signals are not checked: no spin or stall coaching

# Rough seconds each costs per 10 km, only to rank what to work on first
# where no reference gives seconds (calibrate): a second held on the limiter
# on a straight ~0.5 s, a missed gate ~1 s.
COST_LIMITER = 0.5
COST_EVENT = {'hpattern.missed': 1.0, 'hpattern.skip': 0.5, 'downshift.over_rev': 0.1, 'seq.double_tap': 0.5}

METHOD_NAMES = {'h-pattern': 'the H-pattern', 'sequential': 'the sequential', 'paddles': 'the paddles'}
LOOSE = ('gravel', 'snow', 'ice', 'loose-low')
LOOSE_SURFACES = ('gravel', 'snow', 'ice')


def loose(surface):
    return surface in LOOSE or (surface or '').startswith('mixed:')


class Tip:
    """One line of coaching. kind: 'focus' (the habit to work on first),
    'tip', 'praise', 'still' (a tip already shown, or too small to lead
    with, kept quiet), 'note' (what the coach is waiting for). `value` is the
    metric behind it, kept in coach_state to tell when it got worse."""

    __slots__ = ('id', 'kind', 'text', 'evidence', 'value', 'cost', 'count', 'ref', 'rough', 'place', 'rank',
                 'delta_prev')

    def __init__(self, id, kind, text, evidence=None, value=None, cost=0.0, count=0, ref=False, rough=False,
                 place=None, rank=None, delta_prev=None):
        self.id, self.kind, self.text = id, kind, text
        self.rank = rank                 # 'top3': 1 to 3, the place's order by the time available
        self.delta_prev = delta_prev     # s the place's gap to the potential closed since the run before (None: not said)
        self.place = place               # where on the stage it is about (place()), None for a habit
        self.evidence, self.value, self.cost, self.count = list(evidence or []), value, cost, count
        self.ref = ref                   # `cost` is seconds a stage worked out from a run (not a rough constant)
        self.rough = rough               # `cost` is a rough constant: shown only where no tip has a reference

    def to_dict(self):
        out = {'id': self.id, 'kind': self.kind, 'text': self.text, 'evidence': list(self.evidence),
               'value': self.value, 'cost': self.cost,
               'place': dict(self.place) if self.place else None}
        if self.rank is not None:
            out['rank'] = self.rank
        if self.delta_prev is not None:
            out['delta_prev'] = self.delta_prev
        return out

    def __repr__(self):
        return 'Tip({!r}, {!r}, {!r})'.format(self.id, self.kind, self.text)


def place(stage, run, d, d0=None, d1=None, section=None, ds=None):
    """Where a tip is about, for the UI to match it to a section without parsing its id: `stage`, `run` (the
    run it was read from), `d` (m along the run: the slowest point of the section, or where an event began),
    `d0` and `d1` (the section's yaw window, or the event's extent) where known, `section_index` (the run's own
    section id, as coach_context.sections_of numbers them) where the tip is about one section, and `ds` (every
    place, for a tip about several sections)."""
    out = {'stage': stage, 'run': run['id'] if isinstance(run, dict) else run, 'd': d}
    if d0 is not None:
        out['d0'], out['d1'] = d0, (d1 if d1 is not None else d0)
    if section is not None:
        out['section_index'] = section
    if ds:
        out['ds'] = list(ds)
    return out


def _section_place(stage, run, it):
    """place() of a section_report item."""
    s = it['section']
    return place(stage, run, it['d'], s['d0'], s['d1'], s['id'])


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


def _ordinal(n):
    return '{}{}'.format(n, {1: 'st', 2: 'nd', 3: 'rd'}.get(n if n < 20 else n % 10, 'th'))


def _stage_name(stage, row=None):
    """'Afon Bidno - Severn' (the stage table's name), else 'stage <key>'."""
    if row is not None and row.get('name'):
        return row['name']
    return 'this stage' if not stage else 'stage ' + stage


def _date(at):
    return time.strftime('%-d %b', time.localtime(at))


def _say(parts):
    """['a', 'b', 'c'] as 'a, b and c'."""
    return parts[0] if len(parts) == 1 else ', '.join(parts[:-1]) + ' and ' + parts[-1]


def _names(items, limit=3):
    """Section names as 'a, b and c' (the first `limit`, then 'and N more')."""
    names = [it['name'] for it in items[:limit]]
    text = _say(names)
    return text + ' and {} more'.format(len(items) - limit) if len(items) > limit else text


def _km_h(x):
    return abs(x) * 3.6


def _how(c, late_throttle=False):
    """What the run did differently from the reference through a section,
    from compare_section(): ['braked 25 m earlier', 'was 7 km/h slower at the
    slowest point', 'left 4 km/h slower'] (those that differ). Only for a section whose traces are gone: the
    diagnosis (coach_diagnosis) states its own facts, on the same metres."""
    ctx = coach_context
    parts = []
    b, v, x = c['brake'], c['speed'], c['exit']
    if late_throttle and c['throttle'] is not None:
        parts.append('took the throttle {:.1f} s later'.format(c['throttle']))
    if b is not None and abs(b) >= ctx.BRAKE_DELTA:
        parts.append('braked {:.0f} m {}'.format(abs(b), 'earlier' if b > 0 else 'later'))
    if v is not None and abs(v) >= ctx.SPEED_DELTA:
        parts.append('were {:.0f} km/h {} at the slowest point'.format(_km_h(v), 'slower' if v < 0 else 'faster'))
    elif v is not None and (b is not None and abs(b) >= ctx.BRAKE_DELTA or late_throttle):
        parts.append('took the same minimum speed')
    if x is not None and abs(x) >= ctx.SPEED_DELTA:
        parts.append('left {:.0f} km/h {}'.format(_km_h(x), 'slower' if x < 0 else 'faster'))
    return parts


def _section_action(pattern, c, it):
    """The one thing to do, from the pattern, what the reference did and where the section's time went. Only for a
    section whose traces are gone (the diagnosis, coach_diagnosis, reads them): what the stored numbers say."""
    if pattern == 'over-slowing':
        return 'Brake about {:.0f} m later, as your best run did.'.format(abs(c['brake']))
    if pattern == 'under-committed':
        return ('Brake at the same place and carry more speed in: your best run was {:.0f} km/h quicker through '
                'the middle.'.format(_km_h(c['speed'])))
    if pattern == 'overdriven':
        if c['brake'] is not None and c['brake'] <= -coach_context.BRAKE_DELTA:
            return 'Brake {:.0f} m earlier, where your best run did.'.format(abs(c['brake']))
        return 'Enter {:.0f} km/h slower, as your best run did.'.format(_km_h(c['entry']))
    if pattern == 'late-throttle':
        return 'Throttle sooner, as soon as the nose points out: your best run was {:.1f} s earlier.'.format(
            c['throttle'])
    if pattern == 'over-rotated':
        return 'Keep the car straighter through it, as your best run did.'
    if pattern == 'slower':
        return 'Carry more speed through it: your best run was {:.0f} km/h quicker at the slowest point.'.format(
            _km_h(c['speed']))
    if it['exit'] >= 0.7 * it['loss']:
        return 'The time went after the slowest point: look at how early you were back on the throttle.'
    if it['entry'] >= 0.7 * it['loss']:
        return 'The time went before the slowest point: look at where you braked.'
    return None                 # no fix to give: the section is a note


def _name_the_pass(diag, name):
    """The diagnosis's words (facts and fix) with the run it compares against called `name` ('your best run' in
    coach_diagnosis: the PB; a top-3 place is read against the quickest pass through it)."""
    diag['facts'] = [f.replace('your best run', name) for f in diag['facts']]
    if diag['fix']:
        diag['fix'] = diag['fix'].replace('your best run', name)


def _facts_say(facts):
    """The facts of a diagnosis as 'a, b and c' (a run within a few km/h and metres of the reference has none)."""
    return _say(facts) if facts else 'were within a few km/h and metres of it'


def _diagnosed_where(it):
    """The section's name, and the corner the diagnosis is about where the section has several:
    'the left-left-right at 2.2 km (the 4 right at 2.13 km)'."""
    d = it['diag']
    if len(it['section']['corners']) <= 1:
        return it['name']
    return '{} ({} at {:.2f} km)'.format(it['name'], coach_context.corner_name(d['corner']).split(' at ')[0],
                                         d['corner']['d'] / 1000.0)


def _worth_saying(lost):
    """The sections that lost time (biggest first) that are worth a tip of their own: a slow arrival is the corner
    before's time, said there when that one lost too; a diagnosis with no fix (unclear, lost speed, at the limit) is
    no tip (_unexplained): a tip always has a fix."""
    lost_ns = {it['n'] for it in lost}
    said = [it for it in lost if not (it.get('diag') is not None and it['diag']['code'] == 'SLOW-ARRIVAL'
                                      and it['n'] - 1 in lost_ns)]
    return [it for it in said if it.get('diag') is None or it['diag']['fix']]


def _unexplained(lost):
    """The sections that lost time that the diagnosis could not give a fix for (unclear, lost speed, at the limit
    with no positions): said as notes, not tips (_worth_saying has the ones with a fix)."""
    return [it for it in lost if it.get('diag') is not None and not it['diag']['fix']]


class Coach:
    """Coaching from the metrics history, on any reader (telemetry_store).
    `now` is the wall clock (tests set it)."""

    def __init__(self, reader, now=None):
        self.reader = reader
        self.now = now
        self._games = {}
        self._stages = {}
        self._lead = []                  # the potential's top 3 and what improved, said before everything else
        self._claimed = []               # (stage, run, d0, d1) of the top 3's places: the section tips inside them are theirs
        self._loaded = {}                # run id: {'rows', 'arr'} of the traces read for this view (_arrays)

    def _game(self, car):
        """The game a car (cars.id) is from, None where it is not known."""
        if car not in self._games:
            row = self.reader.car_by_id(car) if car is not None else None
            self._games[car] = (row or {}).get('game')
        return self._games[car]

    def _arrays(self, run):
        """{'rows' (the run's stage rows), 'arr' (potential.arrays of them)} of a run (a stage_runs or run row), read
        once per view; None where its trace is gone (TRACES_CAP) or too short."""
        if run['id'] not in self._loaded:
            trace = self.reader.trace(run['id'])
            rows = coach_context.stage_rows(trace, run.get('course'), run.get('finished') == 1,
                                            run.get('result_time')) if trace else None
            arr = potential.arrays(rows) if rows else None
            self._loaded[run['id']] = None if arr is None else {'rows': rows, 'arr': arr}
        return self._loaded[run['id']]

    def _envelope(self, stage, car):
        """The stored potential's grip envelope ({'bins', 'lat'}) of the stage and car, None without a potential."""
        pot = potential.stored(self.reader, stage, car) if car is not None else None
        prof = pot['profile'] if pot else None
        if not prof or not prof.get('env_bins') or not prof.get('env_lat'):
            return None
        return {'bins': np.asarray(prof['env_bins'], dtype=float), 'lat': np.asarray(prof['env_lat'], dtype=float)}

    def _diagnose(self, stage, run, ref, ref_corners, items):
        """Set `diag` (coach_diagnosis.diagnose_section) on each section_report item matched to a section of the
        reference's grid, both runs' traces read once. Nothing set where either trace is gone: the section's
        numbers (section_pattern) are then all there is."""
        mine, theirs = self._arrays(run), self._arrays(ref)
        if mine is None or theirs is None:
            return
        found = coach_context.spans({'trace': theirs['rows'], 'corners': ref_corners, 'course': ref.get('course')})
        spans = {g['id']: (g, a, b, lo, floor, found[n - 1][0] if n else None)
                 for n, (g, a, b, lo, floor) in enumerate(found)}
        env, game = self._envelope(stage, run['car']), self._game(run['car'])
        for it in items:
            span = spans.get((it['ref'] or {}).get('id'))
            if span is None or it['loss'] is None:
                continue
            g, a, b, lo, floor, behind = span
            parts = coach_diagnosis.measures_section(mine['arr'], theirs['arr'], {
                'a': a, 'b': b, 'lo': lo, 'floor': floor, 'corners': g['corners'], 'game': game}, env)
            d = coach_diagnosis.diagnose_section(parts, it['loss'],
                                                 None if behind is None else coach_context.section_name(behind))
            if d is not None:
                it['diag'] = d

    def _stage_label(self, stage):
        """The stage as the stage table names it ('Afon Bidno - Severn'), else its key."""
        if stage not in self._stages:
            self._stages[stage] = _stage_name(stage, self.reader.stage(stage))
        return self._stages[stage]

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
        candidates, praise, notes, techniques = [], [], [], []
        self._lead = []
        self._loaded = {}
        slices = {}
        for r in rows:
            slices.setdefault((r['name'], r['gear'], r['method'], r['discipline'], r['surface']), []).append(r)
        placed, judged = self._place_tips(rows, model, candidates, praise, notes, techniques)
        self._shift_tips(slices, model, candidates, praise, notes, techniques)
        self._method_tips(rows, candidates)
        self._rate_tips(slices, rows, candidates, praise, judged)
        self._launch_tips(slices, rows, candidates, praise)
        self._stage_tips(rows, candidates, praise, placed, model)
        if model is not None:
            self._model_notes(model, now, candidates)
        state = self.reader.coach_state(profile, car_id)
        # After every finished run: the three places with time available and what improved, whatever else is said
        return self._lead + select(candidates, praise, notes, state, now, limit, show_all, techniques)

    # -- the families of tips --

    def _shift_tips(self, slices, model, candidates, praise, notes, techniques):
        """The changes up, judged against the game's lights band
        (coach_context.shift_band) as a distribution per car, surface, gear
        and way of changing (docs/coach-techniques.md, section 7.3 R1): a
        share on the limiter cut, a share below the band, both, or on it.
        What it costs a stage decides whether it is a tip. A change that
        is early on a loose surface is short-shifting, which can be right:
        only 3rd and up, and only where the gear is measured not to be
        grip-limited, is it a tip."""
        gated = waiting = False
        keys = sorted({(gear, method, discipline, surface) for (name, gear, method, discipline, surface) in slices
                       if name == 'shift.in_band' and gear is not None},
                      key=lambda k: tuple('' if x is None else str(x) for x in k))
        contexts = {}
        on_the_band = {}
        for gear, method, discipline, surface in keys:
            if discipline == 'drift':
                continue                                 # a drift profile silences everything but the mechanical rules

            def get(name):
                return slices.get((name, gear, method, discipline, surface), [])
            found = recent(get('shift.in_band'), SHIFT_MIN)
            if found is None:
                continue
            on, count, sessions = found
            early = (recent(get('shift.early_share'), SHIFT_MIN) or (0.0,))[0]
            cut = (recent(get('shift.cut_share'), SHIFT_MIN) or (0.0,))[0]
            costs = [r for _, _, session_rows in by_session(get('shift.cost'))[:COST_RUNS] for r in session_rows]
            cost = pool(costs)[0] if costs else 0.0           # seconds a stage, over the last few runs
            change = '{}→{}'.format(gear, gear + 1)
            where = _where(discipline, surface)
            key = '{}:{}:{}:{}'.format(gear, method, discipline, surface)
            evidence = ['{} changes up flat out in {} session{}{}.'.format(
                count, sessions, '' if sessions == 1 else 's', ' ({})'.format(where) if where else '')]
            band = None
            best = None
            word, lights = 'shift band', False
            if model is not None:
                if surface not in contexts:
                    contexts[surface] = car_context(model, surface)
                band = coach_context.shift_band(contexts[surface], gear, method)
                best = model.best_for(gear, surface if surface in SURFACES else None)
                lights = bool(contexts[surface].get('lights'))
                word = 'lights band' if lights else 'shift band'
            if band is not None:
                evidence.append("The {} for {}: {:.0f} to {:.0f} rpm{}.".format(
                    word, change, band[0], band[1], " (the game's own shift lights)" if lights else ''))
            if on >= SHIFT_ON and cut <= SHIFT_ON_CUT:
                on_the_band.setdefault((discipline, surface, word), []).append((gear, count))
            if band is None:
                continue
            if cut >= SHIFT_CUT and early >= SHIFT_TWO:
                text = ('{}{}: {:.0f} % of your changes up come before {:.0f} rpm and {:.0f} % sit on the limiter cut; '
                        'the {} is {:.0f} to {:.0f} rpm. About {:.1f} s a stage.'.format(
                            change, _with(method), early * 100, band[0] - coach_context.EARLY_BELOW, cut * 100, word,
                            band[0], band[1], cost))
                kind, tip_id = 'tip', 'shift.band:two:' + key
                share_bad = lambda v: v >= SHIFT_CUT
                bad_rows = get('shift.cut_share')
            elif cut >= SHIFT_CUT:
                text = ('{}{}: {:.0f} % of your changes up sit on the limiter cut. Change inside the {}, '
                        '{:.0f} to {:.0f} rpm; about {:.1f} s a stage.'.format(
                            change, _with(method), cut * 100, word, band[0], band[1], cost))
                kind, tip_id = 'tip', 'shift.cut:' + key
                share_bad = lambda v: v >= SHIFT_CUT
                bad_rows = get('shift.cut_share')
            elif early >= SHIFT_EARLY:
                tarmac_like = surface == 'tarmac' or (discipline == 'circuit' and surface not in ('snow', 'ice'))
                if not tarmac_like:
                    if surface in (None, 'unknown'):
                        gated = True
                        continue
                    if surface in ('snow', 'ice') or gear < 3:
                        continue                         # short-shifting is technique here
                    measured = best['grip_limited'] if best is not None else None
                    if measured is None:
                        waiting = True
                        continue
                    if measured:
                        notes.append(Tip('grip:{}:{}'.format(gear, surface), 'note',
                                         '{} on {}: {} is grip-limited there (measured), so changing up before the '
                                         'shift band gives the same drive.'.format(change, surface, _ordinal(gear))))
                        continue
                text = ('{}{}: {:.0f} % of your changes up come before {:.0f} rpm, the start of the {} '
                        '({:.0f} to {:.0f}); about {:.1f} s a stage. Stay in the gear {}.'.format(
                            change, _with(method), early * 100, band[0] - coach_context.EARLY_BELOW, word, band[0],
                            band[1], cost, 'to the lights' if lights else 'longer'))
                kind, tip_id = 'tip', 'shift.early:' + key
                share_bad = lambda v: v <= 1.0 - SHIFT_EARLY
                bad_rows = get('shift.in_band')
                if surface in LOOSE_SURFACES:
                    evidence.append('Not a fault on every gear on loose ground: 3rd and up only, where the gear is '
                                    'measured not to be grip-limited.')
            else:
                went = growth(get('shift.in_band'))
                if went is not None:
                    before, after, sessions_after, when = went
                    if after - before >= SHIFT_GROWTH:
                        praise.append(Tip('shift.better:' + key, 'praise',
                                          'Better: {}{} is on the {} {:.0f} % of the time over your last {} '
                                          'session{}; {} it was {:.0f} %.'.format(
                                              change, _with(method), word, after * 100, sessions_after,
                                              '' if sessions_after == 1 else 's', _ago(self._now() - when),
                                              before * 100),
                                          evidence, after, after - before))
                continue
            if cost < SHIFT_COST_MIN:
                # A habit that costs a tenth of a second a stage is described, once, in the cost's words
                techniques.append(Tip('shift.cheap:' + key, 'technique',
                                      '{}{}{}: {:.0f} % of your changes up come early and {:.0f} % sit on the cut; '
                                      'that costs {} a stage, too little to coach.'.format(
                                          change, _with(method), ' on ' + surface if surface not in (None, 'unknown')
                                          else '', early * 100, cut * 100,
                                          'next to nothing' if cost < 0.05 else 'about {:.1f} s'.format(cost)),
                                      value=early + cut, count=count))
                continue
            is_habit = habit(bad_rows, share_bad)
            if is_habit:
                bad_ones, all_ones = habit_counts(bad_rows, share_bad)
                evidence.append('The same pattern in {} of your last {} sessions.'.format(bad_ones, all_ones))
            candidates.append(Tip(tip_id, 'focus' if is_habit else kind, text, evidence, early + cut, cost, count,
                                  ref=True))
        for (discipline, surface, word), gears in sorted(on_the_band.items(), key=lambda kv: tuple(map(str, kv[0]))):
            where = surface if surface and surface != 'unknown' else 'every surface'
            names = ['{}→{}'.format(g, g + 1) for g, _ in sorted(gears)]
            praise.append(Tip('shift.on:{}:{}'.format(discipline, surface), 'praise',
                              'Your changes up are on the {} in {} on {}.'.format(word, _listed(names), where),
                              ['{} changes up.'.format(sum(n for _, n in gears))], value=len(gears),
                              cost=sum(n for _, n in gears) * 0.001))
        if gated:
            notes.append(Tip('gate.surface', 'note', 'Tips about changing up early wait until the surface is '
                             'known: on a loose surface, short-shifting can be right.'))
        if waiting:
            notes.append(Tip('gate.grip', 'note', 'Tips about changing up early on a loose surface wait until '
                             'the grip of that gear there is measured (a few full-throttle pulls in it): '
                             'short-shifting can be right when the tyres cannot take the drive.'))

    def _now(self):
        return self.now if self.now is not None else time.time()

    def _method_tips(self, rows, candidates):
        """The same driver, two ways of changing: where one puts the changes
        up on the shift band far more often than the other, for the same car,
        surface and gear (each with METHOD_MIN changes)."""
        groups = {}
        for r in rows:
            if r['name'] == 'shift.in_band' and r['method'] is not None and r['gear'] is not None:
                groups.setdefault((r['gear'], r['discipline'], r['surface']), {}).setdefault(r['method'], []).append(r)
        for (gear, discipline, surface), per_method in sorted(groups.items(), key=lambda kv: tuple(map(str, kv[0]))):
            pooled = {m: pool(v) for m, v in per_method.items()}
            methods = sorted(m for m, (_, n) in pooled.items() if n >= METHOD_MIN)
            for i, a in enumerate(methods):
                for b in methods[i + 1:]:
                    gap = pooled[a][0] - pooled[b][0]
                    if abs(gap) >= METHOD_GAP:
                        better, worse = (a, b) if gap > 0 else (b, a)
                        where = _where(discipline, surface)
                        text = '{}→{}{}: {:.0f} % of your changes up with {} are on the shift band, {:.0f} % with {}.'.format(
                            gear, gear + 1, ' on ' + where if where else '', pooled[better][0] * 100,
                            _method(better), pooled[worse][0] * 100, _method(worse))
                        candidates.append(Tip('shift.method:{}:{}:{}:{}'.format(gear, a, b, surface), 'tip', text,
                                              ['{} and {} changes up flat out.'.format(pooled[a][1], pooled[b][1])],
                                              abs(gap), 0.1, pooled[a][1] + pooled[b][1]))

    def _rate_tips(self, slices, rows, candidates, praise, judged=()):
        # Only the limiter held on a straight below top gear, with no corner to use it for
        # and no change up following (coach_context.limiter_episodes): between corners on
        # gravel, being on the limiter is often right and is not counted here. A stage with a
        # reference run is judged against it (_place_tips), not by the per-km figure
        limiter = [r for r in rows if r['name'] == 'limiter.held' and r['discipline'] != 'drift'
                   and r['stage'] not in judged]
        found = recent(limiter, 5)
        if found is not None and found[0] > LIMITER_PER_KM:
            value, count, sessions = found
            is_habit = habit(limiter, lambda v: v > LIMITER_PER_KM)
            candidates.append(Tip('limiter.held', 'focus' if is_habit else 'tip', 'You held a gear on the limiter on '
                                  'straights with no corner to use it for and no change up following: the engine '
                                  'makes nothing there. Change up as the lights flash.',
                                  ['{:.1f} s per km over {} km in {} session{}.'.format(
                                      value, count, sessions, '' if sessions == 1 else 's')],
                                  value, value * 10 * COST_LIMITER, count))
        went = growth(limiter, GROWTH_EVENTS)
        if went is not None and went[0] > LIMITER_PER_KM and went[1] <= went[0] / 2:
            before, after, n, when = went
            praise.append(Tip('limiter.better', 'praise', 'Better: the limiter held on straights is down to {:.1f} s '
                              'per km over your last {} session{}, from {:.1f} {}.'.format(
                                  after, n, '' if n == 1 else 's', before, _ago(self._now() - when)),
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

    def _launch_tips(self, slices, rows, candidates, praise):
        """The launch, judged by what the car did (docs/coach-techniques.md, 7.3 R3): a bog, a stall, a
        change to 2nd held back until the cut, and praise for launches that repeat. A game whose speed
        channel is not checked (LAUNCH_OUT) has none of it."""
        def launch_rows(name):
            return [r for r in rows if r['name'] == name and self._game(r['car']) not in LAUNCH_OUT]
        found = launch_rows('launch.bog')
        last = sorted(found, key=lambda r: -r['started'])[:5]
        if len(last) >= LAUNCHES:
            bogged = sum(1 for r in last if r['value'] >= 1.0)
            if bogged / len(last) >= BOGS:
                candidates.append(Tip('launch.bog', 'tip', '{} of your last {} launches bogged: the car accelerated '
                                      'under {:.1f} g over its first half second, or took over half a second longer '
                                      'than usual to 50 km/h. Let the clutch out more gradually, or hold more '
                                      'revs.'.format(bogged, len(last), coach_context.LAUNCH_G),
                                      value=bogged / len(last), cost=bogged / len(last) * 0.5, count=len(last),
                                      rough=True))
        last = sorted(launch_rows('launch.stall'), key=lambda r: -r['started'])[:5]
        stalled = sum(1 for r in last if r['value'] >= 1.0)
        if stalled >= 2:
            candidates.append(Tip('launch.stall', 'tip', 'You stalled {} of your last {} launches: more revs, and '
                                  'the clutch out more gently.'.format(stalled, len(last)),
                                  value=stalled / len(last), cost=stalled * 1.0, count=len(last),
                                      rough=True))
        went = growth(launch_rows('launch.t50'), 5)
        if went is not None and went[1] <= went[0] * 0.9:
            before, after, n, when = went
            praise.append(Tip('launch.better', 'praise', 'Quicker off the line: {:.1f} s to 50 km/h over your last {} '
                              'session{}, from {:.1f} {}.'.format(after, n, '' if n == 1 else 's', before,
                                                                  _ago(self._now() - when)),
                              value=after, cost=before - after))
        # Only the driver's own launches (the game's auto-clutch writes no launch.bog) are praised or coached
        driven = {r['run'] for r in found}
        groups, held = {}, {}
        for r in launch_rows('launch.t50'):
            if r['run'] in driven and r['surface'] not in (None, 'unknown'):
                groups.setdefault((r['car'], r['surface']), []).append(r)
        for r in launch_rows('launch.cut'):
            if r['surface'] not in (None, 'unknown'):
                held.setdefault((r['car'], r['surface']), []).append(r)
        for (car, surface), group in sorted(groups.items(), key=lambda kv: str(kv[0])):
            group.sort(key=lambda r: -r['started'])
            values = [r['value'] for r in group[:5]]
            best = min(r['value'] for r in group)
            if len(values) == 5 and max(values) - min(values) < LAUNCH_GAME:
                continue                    # human launches do not repeat within 20 ms: the game's own launch
            if len(values) == 5 and max(values) - min(values) <= LAUNCH_STEADY and min(values) - best <= LAUNCH_STEADY:
                praise.append(Tip('launch.steady:' + surface, 'praise',
                                  'Your last 5 launches on {} took {:.2f} to {:.2f} s to 50 km/h, within a tenth of '
                                  'your best ({:.2f} s).'.format(surface, min(values), max(values), best),
                                  ['{} launches on {}.'.format(len(group), surface)], max(values) - min(values),
                                  cost=0.2, count=len(group)))
        for (car, surface), group in sorted(held.items(), key=lambda kv: str(kv[0])):
            group.sort(key=lambda r: -r['started'])
            last = group[:5]
            late = [r['value'] for r in last if r['value'] >= LAUNCH_CUT]
            if len(last) >= LAUNCHES + 2 and len(late) >= LAUNCH_CUTS:
                mean = sum(late) / len(late)
                candidates.append(Tip('launch.cut:' + surface, 'tip',
                                      '{} of your last {} launches on {} sat on the limiter in the launch gear for '
                                      '{:.1f} s: change up as the cut comes in.'.format(
                                          len(late), len(last), surface, mean),
                                      ['{} launches on {}.'.format(len(group), surface)], len(late) / len(last),
                                      cost=mean * len(late) / len(last) * COST_LAUNCH_CUT, count=len(last), rough=True))

    def _stage_tips(self, rows, candidates, praise, placed=(), model=None):
        """What only the same stage can say that the sections do not (_place_tips, which has the
        stages it could name): the three worst sections' total where the sections are not stored,
        both pedals down on the straights, consistency."""
        stages = {}
        for r in rows:
            if r['stage']:
                stages.setdefault(r['stage'], []).append(r)
        for stage, stage_rows in sorted(stages.items()):
            name = self._stage_label(stage)
            loss = sorted((r for r in stage_rows if r['name'] == 'corner.loss'), key=lambda r: -r['started'])
            if (stage not in placed and loss and loss[0]['value'] > CORNER_LOSS
                    and loss[0]['discipline'] != 'drift'):
                candidates.append(Tip('corner.loss:' + stage, 'tip', 'On {}, your last run lost {:.1f} s in its three '
                                      'worst sections against your best run there in this car.'.format(
                                          name, loss[0]['value']),
                                      value=loss[0]['value'], cost=loss[0]['value'], count=1, ref=True))
            drag = [r for r in stage_rows if r['name'] == 'pedal.drag'
                    and (r['discipline'] == 'circuit' or r['surface'] == 'tarmac')]
            if model is not None and drag and drag[0]['surface'] == 'tarmac':
                # A front-wheel drive or turbo car left-foot brakes on tarmac: technique (_left_foot)
                context = car_context(model, 'tarmac')
                if context.get('drivetrain') == 'fwd' or context.get('turbo'):
                    drag = []
            found = recent(drag, 3)
            if found is not None and found[0] > DRAG_PER_KM:
                # Both pedals down on a straight is dead weight on tarmac; on loose ground it is the technique
                candidates.append(Tip('pedal.drag:' + stage, 'tip', 'On {} you were on both pedals on the straights: '
                                      'come off the brake before the throttle goes down.'.format(name),
                                      ['{:.1f} s per km over {} runs.'.format(found[0], found[2])],
                                      value=found[0], cost=found[0] * 2, count=found[1], rough=True))
            steady = sorted((r for r in stage_rows if r['name'] == 'consistency.split_sd'),
                            key=lambda r: -r['started'])
            if len(steady) >= 4 and steady[0]['value'] <= 0.7 * max(r['value'] for r in steady[1:]):
                before = max(steady[1:], key=lambda r: r['value'])
                praise.append(Tip('consistency:' + stage, 'praise',
                                  'Steadier on {}: your splits over your last {} clean runs vary by {:.1f} s, from '
                                  '{:.1f} s over the {} runs before.'.format(
                                      name, steady[0]['count'], steady[0]['value'], before['value'], before['count']),
                                  value=steady[0]['value'], cost=0.1))

    # -- the stage's last run, section by section (docs/coach-techniques.md, 7.2.2 and 7.3 R4-R7) --

    def _place_tips(self, rows, model, candidates, praise, notes, techniques):
        """The newest run of each of the STAGES_COACHED newest stages, read section by section from its
        stored corners and events against the car's best finished run there: corner tips by place and
        cause (R6), the throttle and pedals leaving corners (R4, R5), spins, stops and offs, the speed
        through the same section from run to run (R7), the limiter held on a straight against the
        reference (R2), what was done well. Returns (the stages whose sections were read, the stages
        judged against a reference run)."""
        reader = self.reader
        stages = {}
        for r in rows:
            if r['stage'] and r['run'] is not None:
                stages.setdefault(r['stage'], {})[r['run']] = r['started']
        placed, judged = set(), set()
        for stage in sorted(stages, key=lambda k: (-max(stages[k].values()), k))[:STAGES_COACHED]:
            run = None
            for run_id, _ in sorted(stages[stage].items(), key=lambda kv: -kv[1]):
                found = reader.run(run_id)
                if found is not None and found['run_class'] not in (None, 'restart', 'unclassified'):
                    run = found
                    break
            if run is None or run['discipline'] == 'drift':
                continue
            corners = reader.corners(run['id'])
            if not corners:
                continue
            if not placed:
                self._potential_lead(stage, self._stage_label(stage), run, corners)      # the newest run's
            referenced = self._stage_place(stage, run, corners, model, candidates, praise, notes, techniques)
            placed.add(stage)
            if referenced:
                judged.add(stage)
        return placed, judged

    def _stage_place(self, stage, run, corners, model, candidates, praise, notes, techniques):
        """_place_tips for one stage's run; True when the run had a reference run to be judged against."""
        reader = self.reader
        name = self._stage_label(stage)
        events = reader.events(run['id'])
        sections = coach_context.sections_of(corners)
        surface, discipline = run['surface'], run['discipline']
        self._left_foot(run, corners, model, techniques)
        self._incidents(stage, name, run, events, sections, corners, candidates, notes)
        self._held_corner(stage, name, run, techniques)
        before = [r for r in reader.stage_runs(stage, exclude=run['id'], limit=60, car=run['car'], ranked=True)
                  if r['started'] <= run['started']]
        ref = coach_context.reference_run([r for r in before if r['run_class'] in ('clean', 'learning')],
                                          wet=run['wet'])
        ref_corners = reader.corners(ref['id']) if ref is not None else []
        report = coach_context.section_report(corners, ref_corners) if ref_corners else None
        car = self._car_name(run['car'])
        self._spread(stage, name, run, events, sections, report, candidates, praise)
        if report is None or run['run_class'] in ('learning', 'unclassified'):
            return False
        ref_text = '{}, {:.1f} s on {}'.format(car, ref['result_time'], _date(ref['started']))
        # A run is "best yet" only when quicker than every earlier clean run; it is then measured against
        # the run it beat. The reference is chosen by the time after the start, so it may not be the quickest
        prior = _prior_best(run, before)
        beat = prior is not None and run['finished'] == 1 and run['run_class'] == 'clean' \
            and bool(run['result_time']) and run['result_time'] < prior['result_time']
        quickest = _ref_is_quickest(ref, before)
        if beat:
            ref_name = 'your previous best clean run' if quickest else 'your previous quickest run after the start'
        else:
            ref_name = 'your best clean run' if quickest else 'your quickest run after the start'
        named = []
        for n, it in enumerate(report):
            if it['loss'] is None or it['off'] or it['compare'] is None or it['first'] or it['ref_off'] or it['last']:
                continue                    # the last holds the finish; one beside an off is no fair comparison
            # Time lost beside time gained is one trade (braked later into the one, too early for the next):
            # a section is named for what it lost net of the gain right before it
            prev = report[n - 1] if n else None
            if (it['loss'] > 0 and prev is not None and prev['loss'] is not None and not prev['off']
                    and not prev['first'] and prev['loss'] <= -coach_context.SECTION_MIN):
                net = it['loss'] + prev['loss']
                it = dict(it, loss=net, gained_before=-prev['loss'], pattern=(
                    coach_context.section_pattern(it['compare'], net) if net >= coach_context.SECTION_MIN else None))
            named.append(dict(it, n=n))
        self._diagnose(stage, run, ref, ref_corners, named)
        lost = sorted((it for it in named if it['loss'] >= coach_context.SECTION_MIN), key=lambda it: -it['loss'])
        claimed = self._patterns(stage, name, run, named, ref_name, ref_text, candidates, model)
        total = sum(it['loss'] for it in lost)
        top = _worth_saying(lost)[:SECTION_TIPS]
        if total > 0 and len(lost) >= 2:
            two = sum(it['loss'] for it in lost[:2]) >= TWO_PLACES * total
            spread = ('Most of it in {}.'.format('two places' if len(lost) > 2 else 'these two') if two else
                      'Spread over the stage; the biggest are {}.'.format(_names(lost, 2)))
        else:
            spread = None
        shown = 0
        mine = [(d0, d1) for stg, rid, d0, d1 in self._claimed if stg == stage and rid == run['id']]
        for it in top + _unexplained(lost)[:SECTION_TIPS]:
            d = it.get('diag')
            if it['pattern'] in claimed.get(it['d'], ()) or (d is not None and d['code'] in claimed.get(it['d'], ())):
                continue
            if any(d0 <= it['d'] < d1 for d0, d1 in mine):
                continue                    # inside one of the top 3 places, which has its own fix for it
            if d is not None and d['code'] == 'SLOW-ARRIVAL' and it['n'] \
                    and any(d0 <= report[it['n'] - 1]['d'] < d1 for d0, d1 in mine):
                continue                    # the corner before it is one, with a fix of its own
            c = it['compare']
            pattern = it['pattern']
            evidence = ['{:.1f} s of it before the slowest point and {:.1f} s after.'.format(it['entry'], it['exit'])]
            if d is not None:
                text = 'On {}, {}, {:.1f} s behind {} here ({}): you {}.{}'.format(
                    name, _diagnosed_where(it), it['loss'], ref_name, ref_text, _facts_say(d['facts']),
                    ' ' + d['fix'] if d['fix'] else ' The numbers do not point to one thing to change.')
                if d['evidence']:
                    evidence.append('Measured on the same metres as your best run: {}.'.format(', '.join(d['evidence'])))
            else:                           # no trace of the run or the reference (evicted): the section's numbers
                how = _how(c, late_throttle=pattern == 'late-throttle')
                if not how:
                    how = ['were within a few km/h and metres of it']
                action = _section_action(pattern, c, it)
                text = 'On {}, {}, {:.1f} s behind {} here ({}): you {}.{}'.format(
                    name, it['name'], it['loss'], ref_name, ref_text, _say(how),
                    ' ' + action if action else ' The time went across the whole section.')
                d = {'fix': action}
            if it.get('gained_before'):
                evidence.append('Net of the {:.1f} s the section before it gained.'.format(it['gained_before']))
            if spread:
                evidence.append('{:.1f} s behind {} over {} sections. {}'.format(total, ref_name, len(lost), spread))
            if d is not None and not d['fix']:           # nothing to do that the numbers point at: a note, not a tip
                notes.append(Tip('corner.note:{}:{:.0f}'.format(stage, it['d']), 'note', text, evidence, it['loss'],
                                 cost=0.0, count=1, place=_section_place(stage, run, it)))
                continue
            candidates.append(Tip('corner.section:{}:{:.0f}'.format(stage, it['d']), 'tip', text, evidence,
                                  it['loss'], cost=it['loss'], count=1, ref=True,
                                  place=_section_place(stage, run, it)))
            shown += 1
        loose_ = loose(surface)
        gained = sorted((it for it in named if it['loss'] <= -coach_context.SECTION_MIN), key=lambda it: it['loss'])
        wins = [it for it in gained if it['pattern'] == 'right-entry'] if loose_ else []
        for it in wins[:1]:
            c = it['compare']
            how = it['diag']['facts'] if it.get('diag') is not None else _how(c)
            praise.append(Tip('corner.entry:{}:{:.0f}'.format(stage, it['d']), 'praise',
                              'On {}, {}, {:.1f} s up on {} here ({}){}.'.format(
                                  name, it['name'], -it['loss'], ref_name, ref_text,
                                  ': you ' + _say(how) if how else ''),
                              [], -it['loss'], cost=-it['loss'], count=1, place=_section_place(stage, run, it)))
        self._best_of(stage, name, run, ref, ref_corners, before, report, gained, ref_text, car, praise, notes)
        self._held_straight(stage, name, run, ref, events, candidates)
        return True

    def _car_name(self, car):
        row = self.reader.car_by_id(car) if car is not None else None
        return (row or {}).get('name') or 'this car'

    def _patterns(self, stage, name, run, named, ref_name, ref_text, candidates, model):
        """The tips about a pattern across the run's sections, each naming its sections: the throttle
        later than in the reference leaving corners (R5), both pedals leaving them (R4, tarmac and
        circuit) and coasting into them (R5, tarmac and circuit). Returns {section d: patterns it is
        part of} so the section tips do not say it twice."""
        surface, discipline = run['surface'], run['discipline']
        claimed = {}
        icy = surface in ('snow', 'ice')
        tarmac_like = surface == 'tarmac' or (discipline == 'circuit' and not icy)
        late = [it for it in named if it['compare']['throttle'] is not None and (it['exit'] or 0.0) > coach_context.SECTION_MIN
                and (it['diag']['code'] == 'LATE-THROTTLE' if it.get('diag') is not None
                     else it['compare']['throttle'] >= LATE_THROTTLE)]
        if len(late) >= LATE_CORNERS and not icy:
            late.sort(key=lambda it: -it['exit'])
            lost = sum(it['exit'] for it in late)
            mean = sum(it['compare']['throttle'] for it in late) / len(late)
            candidates.append(Tip('throttle.late:' + stage, 'tip',
                                  'On {}, the throttle came {:.1f} s later on average than in {} here ({}) '
                                  'leaving {}: {:.1f} s of exit time. Throttle as soon as the nose points out.'.format(
                                      name, mean, ref_name, ref_text, _names(late), lost),
                                  ['{} corners, the slowest {:.1f} s later.'.format(
                                      len(late), max(it['compare']['throttle'] for it in late))],
                                  mean, cost=lost, count=len(late), ref=True,
                                  place=place(stage, run, late[0]['d'], ds=[it['d'] for it in late])))
            for it in late:
                claimed.setdefault(it['d'], set()).update(('late-throttle', 'LATE-THROTTLE'))
        both = [it for it in named if (it['compare']['overlap_exit'] or 0.0) >= OVERLAP_EXIT
                and (it['exit'] or 0.0) > coach_context.SECTION_MIN]
        turbo = model is not None and car_context(model, surface).get('turbo')
        if tarmac_like and not (discipline == 'circuit' and turbo) and len(both) >= OVERLAP_CORNERS:
            both.sort(key=lambda it: -it['exit'])
            lost = sum(it['exit'] for it in both)
            held = sum(it['compare']['overlap_exit'] for it in both)
            candidates.append(Tip('pedal.exit:' + stage, 'tip',
                                  'On {}, you were on both pedals for {:.1f} s leaving {}, which cost {:.1f} s of exit '
                                  'time against {} here ({}). Come off the brake before the throttle goes '
                                  'down.'.format(name, held, _names(both), lost, ref_name, ref_text),
                                  ['{} corners with more than {:.1f} s on both pedals leaving them.'.format(
                                      len(both), OVERLAP_EXIT)],
                                  held, cost=lost, count=len(both), ref=True,
                                  place=place(stage, run, both[0]['d'], ds=[it['d'] for it in both])))
        coast = [it for it in named if (it['diag']['code'] == 'COASTING' if it.get('diag') is not None
                                        else (it['compare']['coast_entry'] or 0.0) >= COAST_LONGER)
                 and (it['entry'] or 0.0) > coach_context.SECTION_MIN
                 and coach_context.key_corner(it['section']).get('tightness') != 'hairpin']
        if tarmac_like and coast:
            coast.sort(key=lambda it: -it['entry'])
            lost = sum(it['entry'] for it in coast)
            longer = sum(it['diag']['m']['coast_ds'] if it.get('diag') is not None else it['compare']['coast_entry']
                         for it in coast)
            candidates.append(Tip('coast.entry:' + stage, 'tip',
                                  'On {}, you coasted {:.1f} s longer than in {} here ({}) going into {}, '
                                  'which lost {:.1f} s before the slowest point. Go from the brake to the throttle '
                                  'without a gap.'.format(name, longer, ref_name, ref_text, _names(coast), lost),
                                  [], longer, cost=lost, count=len(coast), ref=True,
                                  place=place(stage, run, coast[0]['d'], ds=[it['d'] for it in coast])))
            for it in coast:
                claimed.setdefault(it['d'], set()).add('COASTING')
        return claimed

    def _left_foot(self, run, corners, model, techniques):
        """How many corners the driver left-foot brakes into: described, once, never praised (R4). On
        tarmac only for a front-wheel drive or turbo car, where it is technique too."""
        surface = run['surface']
        if surface in (None, 'unknown') or (run['discipline'] or '') == 'circuit' and surface not in ('snow', 'ice'):
            return
        kept = [k for k in corners if not k.get('off') and k.get('overlap_entry') is not None
                and not (surface == 'tarmac' and k.get('tightness') == 'hairpin')]
        if len(kept) < 10:
            return
        if surface == 'tarmac':
            context = car_context(model, surface) if model is not None else {}
            if not (context.get('drivetrain') == 'fwd' or context.get('turbo')):
                return
        left = sum(1 for k in kept if k['overlap_entry'] >= LEFT_FOOT)
        share = left / len(kept)
        if share >= LEFT_FOOT_SHARE:
            techniques.append(Tip('technique:overlap:' + surface, 'technique',
                                  'You left-foot brake into {} in 10 corners on {}.'.format(round(share * 10), surface),
                                  ['{} of {} corners with both pedals down for {:.1f} s or more going in.'.format(
                                      left, len(kept), LEFT_FOOT)], value=share, count=len(kept)))

    def _incidents(self, stage, name, run, events, sections, corners, candidates, notes):
        """A spin or a near stop in a corner is coached as a corner (R6's over-rotated); an off is named
        and its surroundings are left out of every comparison. A game whose attitude signals are not
        checked (ATTITUDE_OUT) has no spin or stall; a learning run's are described, not coached."""
        attitude = self._game(run['car']) not in ATTITUDE_OUT
        offs = []                                  # [first off, how many] where offs lie within INCIDENT_REACH of each other
        for e in sorted((e for e in events if e['kind'] == 'off' and e['d0'] is not None), key=lambda e: e['d0']):
            if offs and e['d0'] - offs[-1][0]['d0'] <= coach_context.INCIDENT_REACH:
                offs[-1][1] += 1
            else:
                offs.append([e, 1])
        for e, count in offs:
            facts = self._off_facts(stage, run, e, corners)
            notes.append(Tip('corner.off:{}:{:.0f}:{}'.format(stage, e['d0'] // coach_context.INCIDENT_REACH,
                                                               run['id']), 'note',
                             'On {}, off{} at {:.1f} km: the corners within {:.0f} m of it are left out of the '
                             'comparisons.{}'.format(name, ' {} times'.format(count) if count > 1 else '',
                                                     e['d0'] / 1000.0, coach_context.INCIDENT_REACH,
                                                     ' ' + facts if facts else ''),
                             place=place(stage, run, e['d0'], e['d0'], e['d1'])))
        for e in events:
            if e['kind'] not in ('spin', 'stall') or e['d0'] is None:
                continue
            reach = coach_context.SECTION_REACH
            if not attitude or (e['kind'] == 'stall' and any(
                    o['kind'] == 'spin' and o['d0'] - reach <= e['d1'] and e['d0'] <= o['d1'] + reach
                    for o in events)):
                continue                                  # a stop in the spin's own corner is the spin
            near = [k for k in corners if k.get('d0') is not None and k['d0'] - reach <= e['d1']
                    and e['d0'] <= k['d1'] + reach]
            corner = min(near, key=lambda k: abs(k['d'] - (e['d0'] + e['d1']) / 2.0)) if near else None
            what = ('you spun in {}' if e['kind'] == 'spin' else 'the car nearly stopped in {}').format(
                coach_context.corner_name(corner)) if corner is not None else (
                'you spun at {:.1f} km' if e['kind'] == 'spin' else 'the car nearly stopped at {:.1f} km').format(
                    e['d0'] / 1000.0)
            if run['run_class'] == 'learning':
                # The first run of a stage is described, not coached
                notes.append(Tip('corner.{}:{}:{:.0f}'.format(e['kind'], stage, e['d0']), 'note',
                                 'On {}, {}.'.format(name, what), place=place(stage, run, e['d0'], e['d0'], e['d1'])))
                continue
            facts, fix = self._incident_says(stage, run, e, corner)
            text = 'On {}, {}{}.{}'.format(name, what, ': ' + facts if facts else '', ' ' + fix if fix else '')
            if not fix:                     # nothing the run shows to change: said, not coached
                notes.append(Tip('corner.{}:{}:{:.0f}'.format(e['kind'], stage, e['d0']), 'note', text,
                                 place=place(stage, run, e['d0'], e['d0'], e['d1'])))
                continue
            candidates.append(Tip('corner.{}:{}:{:.0f}'.format(e['kind'], stage, e['d0']), 'tip', text,
                                  [], 1.0, cost=COST_SPIN if e['kind'] == 'spin' else COST_STALL, count=1, rough=True,
                                  place=place(stage, run, e['d0'], e['d0'], e['d1'])))

    def _incident_says(self, stage, run, e, corner):
        """(what was measured, the one thing to do or None) for a spin or a near stop in `corner` (None: no
        corner of the run near it). Measured: the heading change (a spin), how long the car was nearly stopped (a
        stall), the speed it came in at and slowed to, the handbrake only where the corner says it was used.
        The fix, in order: arrive at the speed of the quickest pass's corner where that was slower; catch it with
        opposite lock where the run steered against the slide less than COUNTER_DELTA of it; a shorter handbrake
        pull where it was measured; a near stop with no rotation: the diagnosis's fix against the quickest pass.
        Nothing is said about the line, a flick or the handbrake that was not measured."""
        facts = []
        entry = corner.get('entry_speed') if corner is not None else None
        low = corner.get('min_speed') if corner is not None else None
        if e['kind'] == 'spin' and e.get('value'):
            facts.append('the car turned {:.0f} degrees'.format(e['value']))
        elif e['kind'] == 'stall' and e.get('value'):
            facts.append('it was nearly stopped for {:.1f} s'.format(e['value']))
        if entry is not None and low is not None:
            facts.append('you came in at {:.0f} km/h and were down to {:.0f} km/h at the slowest point'.format(
                entry * 3.6, low * 3.6))
        hold = self._handbrake_s(run, corner) if corner is not None and corner.get('handbrake') else None
        if hold:
            facts.append('the handbrake was on for {:.1f} s'.format(hold))
        said = ' and '.join(facts) if len(facts) < 3 else ', '.join(facts[:-1]) + ' and ' + facts[-1]
        counter = corner.get('counter_steer') if corner is not None else None
        rotated = e['kind'] == 'spin' or (corner is not None and (
            (counter or 0.0) >= coach_context.COUNTER_DELTA or corner.get('handbrake')))
        quick = self._quick_corner(stage, run, corner) if corner is not None else None
        if entry is not None and quick is not None and quick.get('entry_speed') is not None \
                and entry - quick['entry_speed'] >= coach_context.SPEED_DELTA:
            return said, 'Arrive at {:.0f} km/h, as your quickest pass did (you came in at {:.0f}).'.format(
                quick['entry_speed'] * 3.6, entry * 3.6)
        if rotated and counter is not None and counter < coach_context.COUNTER_DELTA:
            return said, ('Catch it with opposite lock as soon as the rear steps out (you steered against it for '
                          '{:.0f} % of it).'.format(100.0 * counter))
        if rotated and hold:
            return said, 'A shorter handbrake pull: on for {:.1f} s.'.format(hold)
        if not rotated and e['kind'] == 'stall' and corner is not None:
            diag = self._diagnose_place(stage, run, {'d0': corner['d0'], 'd1': corner['d1'], 'split': None}, None)
            if diag is not None and diag['fix']:
                return said, diag['fix']
        return said, None

    def _handbrake_s(self, run, corner):
        """Seconds the handbrake was on (past half) through a corner's yaw window, from the run's trace; None where it
        was not on or there is no trace."""
        loaded = self._arrays(run)
        if loaded is None or corner.get('d0') is None:
            return None
        rows = loaded['rows']
        track = coach_context.along(rows)
        hb, t = coach_context.CH['handbrake'], coach_context.CH['t']
        held = sum(rows[i][t] - rows[i - 1][t] for i in range(1, len(rows))
                   if corner['d0'] <= track[i] <= corner['d1'] and rows[i][hb] is not None and rows[i][hb] > 0.5)
        return held if held > 0.0 else None

    def _quick_corner(self, stage, run, corner):
        """The stored corner of the car's quickest pass of the stage (the coach's reference run before `run`) that is
        the same bend as `corner` (the nearest within SECTION_REACH m), or None."""
        reader = self.reader
        before = [r for r in reader.stage_runs(stage, exclude=run['id'], limit=60, car=run['car'], ranked=True)
                  if r['started'] <= run['started'] and r['run_class'] in ('clean', 'learning')]
        ref = coach_context.reference_run(before, wet=run['wet'])
        if ref is None:
            return None
        near = [k for k in reader.corners(ref['id']) if k.get('d') is not None
                and abs(k['d'] - corner['d']) <= coach_context.SECTION_REACH]
        return min(near, key=lambda k: abs(k['d'] - corner['d'])) if near else None

    def _spread(self, stage, name, run, events, sections, report, candidates, praise):
        """R7: the speed through the same section from run to run (corner.spread events). A section that
        varies by SPREAD_KMH and a tenth of its median is a tip, naming the corner and, where the best run
        is known, the speed it took; the five costliest sections within SPREAD_STEADY_KMH are praised."""
        spreads = [e for e in events if e['kind'] == 'spread' and e['detail']]
        if not spreads:
            return
        by_apex = {round(s['apex'], 1): s for s in sections}
        first = sections[0]['id'] if sections else None            # the launch is in it

        def section_of(e):
            return next((s for a, s in by_apex.items() if abs(a - e['detail']['apex']) < 1.0), None)
        refs = {}
        if report is not None:
            for it in report:
                if it['ref'] is not None:
                    refs[round(it['d'], 1)] = coach_context.key_corner(it['ref']).get('min_speed')
        wide = []
        for e in spreads:
            sd, median = e['value'] * 3.6, e['detail']['median'] * 3.6
            sec = section_of(e)
            if sec is not None and sec['id'] != first and sd > SPREAD_KMH and sd > SPREAD_SHARE * median:
                wide.append((sd, e, sec))
        wide.sort(key=lambda w: -w[0])
        if wide:
            top = wide[:3]
            best = [refs.get(round(sec['apex'], 1)) for _, _, sec in top]
            action = ('Your best clean run took {} at {} km/h: aim for that each time.'.format(
                'it' if len(top) == 1 else 'them', _say(['{:.0f}'.format(b * 3.6) for b in best]))
                if all(b is not None for b in best) else
                'Pick the speed of your quickest run through {} and repeat it.'.format(
                    'it' if len(top) == 1 else 'each'))
            names = [{'name': coach_context.section_name(sec)} for _, _, sec in top]
            candidates.append(Tip('corner.spread:' + stage, 'tip',
                                  'On {}, your speed through {} varies from run to run, by {} km/h over the last {} '
                                  'runs. {}'.format(name, _names(names), _say(['{:.0f}'.format(sd) for sd, _, _ in top]),
                                                    max(e['detail']['runs'] for _, e, _ in top), action),
                                  ['{:.0f} to {:.0f} km/h through {}.'.format(
                                      e['detail']['min'] * 3.6, e['detail']['max'] * 3.6, coach_context.section_name(sec))
                                   for _, e, sec in top],
                                  top[0][0], cost=sum(sd for sd, _, _ in top) * COST_SPREAD, count=len(top),
                                  rough=True, place=place(stage, run, top[0][2]['apex'], top[0][2]['d0'], top[0][2]['d1'],
                                                          top[0][2]['id'], [sec['apex'] for _, _, sec in top])))
        # The five costliest sections: by what the run lost or gained in them where it is known
        scored = []
        for e in spreads:
            sec = section_of(e)
            if sec is not None and sec['id'] != first:
                loss = sec['lead'].get('loss_entry')
                loss = None if loss is None else abs(loss + (sec['lead'].get('loss_exit') or 0.0))
                scored.append((loss if loss is not None else -1.0, e, sec))
        scored.sort(key=lambda x: -x[0])
        if len(scored) >= 5 and all(x[1]['value'] * 3.6 <= SPREAD_STEADY_KMH for x in scored[:5]):
            top = scored[:5]
            praise.append(Tip('corner.steady:' + stage, 'praise',
                              'Your speed through the five costliest corners on {} is within {:.0f} km/h from run to '
                              'run ({}).'.format(name, SPREAD_STEADY_KMH, _names([{'name': coach_context.section_name(x[2])}
                                                                                  for x in top], 5)),
                              [], max(x[1]['value'] for x in top) * 3.6, cost=0.3, count=5,
                              place=place(stage, run, top[0][2]['apex'], ds=[x[2]['apex'] for x in top])))

    def _held_corner(self, stage, name, run, techniques):
        """R2: a gear held on the limiter into a corner is gearing, not a fault. Where it recurs at the same
        place in HELD_RECUR of the last HELD_RUNS runs and lasts more than HELD_LONG s, say so, once."""
        reader = self.reader
        runs = [r for r in reader.stage_runs(stage, limit=HELD_RUNS + 5, car=run['car'])
                if r['run_class'] in ('clean', 'learning', 'off', 'partial')][:HELD_RUNS]
        episodes = []
        for r in runs:
            for e in reader.events(r['id'], 'limiter'):
                if e['class'] == 'held-corner' and e['d0'] is not None:
                    episodes.append((e['d0'], e['d1'], r['id'], e['value'], e['gear']))
        episodes.sort()
        groups, start = [], None
        for ep in episodes:
            if groups and ep[0] - start <= HELD_NEAR:
                groups[-1].append(ep)
            else:
                groups.append([ep])
                start = ep[0]
        for group in groups:
            in_runs = {ep[2] for ep in group}
            mean = sum(ep[3] for ep in group) / len(group)
            if len(in_runs) >= HELD_RECUR and mean > HELD_LONG:
                gear = statistics.mode([ep[4] for ep in group])
                a, b = min(ep[0] for ep in group), max(ep[1] for ep in group)
                techniques.append(Tip('technique:held:{}:{:.0f}'.format(stage, a // HELD_NEAR), 'technique',
                                      'On {}, {} is short for the run from {:.1f} to {:.1f} km; holding it on the '
                                      'limiter there is fine.'.format(name, _ordinal(gear), a / 1000.0, b / 1000.0),
                                      ['{} of your last {} runs, {:.1f} s on average.'.format(
                                          len(in_runs), len(runs), mean)], value=len(in_runs), count=len(in_runs),
                                      place=place(stage, run, a, a, b)))

    def _held_straight(self, stage, name, run, ref, events, candidates):
        """R2: the limiter held on a straight with no corner to use it for and no change up following
        (a held-straight episode) is a tip only when the reference run was quicker over the same road
        to the next braking, or in a higher gear there."""
        held = [e for e in events if e['kind'] == 'limiter' and e['class'] == 'held-straight' and e['d0'] is not None]
        if not held:
            return
        trace = self.reader.trace(run['id'])
        ref_trace = self.reader.trace(ref['id'])
        if not trace or not ref_trace:
            return
        for e in held[:3]:
            found = coach_context.held_vs_reference(
                trace, coach_context.stage_rows(ref_trace, ref['course'], True, ref['result_time']), e['d0'])
            if found is None or not (found['lost'] >= HELD_GAIN or (found['ref_gear'] or 0) > (e['gear'] or 0)):
                continue
            candidates.append(Tip('limiter.held:{}:{:.0f}'.format(stage, e['d0']), 'tip',
                                  'On {}, from {:.1f} km you held {} on the limiter for {:.1f} s with no corner to use '
                                  'it for: your best clean run was {:.1f} s quicker to the next braking{}. Change up as the '
                                  'lights flash.'.format(
                                      name, e['d0'] / 1000.0, _ordinal(e['gear']), e['value'], max(0.0, found['lost']),
                                      ', in {}'.format(_ordinal(found['ref_gear'])) if found['ref_gear'] else ''),
                                  [], e['value'], cost=max(found['lost'], 0.05), count=1, ref=True,
                                  place=place(stage, run, e['d0'], e['d0'], e['d1'])))

    def _best_of(self, stage, name, run, ref, ref_corners, before, report, gained, ref_text, car, praise, notes):
        """What was done best: the run that beat the reference (and where), the sections where it set
        the best of the car's runs, and what the car's best sections put together make (the "possible"
        time, once per stage)."""
        reader = self.reader
        prior = _prior_best(run, before)
        if (prior is not None and run['finished'] == 1 and run['result_time'] and run['run_class'] in ('clean',)
                and run['result_time'] < prior['result_time']):
            gain = prior['result_time'] - run['result_time']
            sentence = 'On {}, your best clean run yet in the {}: {:.1f} s quicker than {} ({:.1f} s).'.format(
                name, car, gain, _date(prior['started']), prior['result_time'])
            if gained and prior['id'] == ref['id']:                 # the gains are against the reference
                it = gained[0]
                c = it['compare']
                lower = coach_context.SPEED_DELTA
                # a gain with both the slowest point and the exit slower was not made there
                if it.get('diag') is not None:
                    how = it['diag']['facts']
                else:
                    how = [] if (c['speed'] or 0.0) <= -lower and (c['exit'] or 0.0) <= -lower else _how(c)
                sentence += ' {:.1f} s of it in {}{}.'.format(-it['loss'], it['name'],
                                                              ', where you ' + _say(how) if how else '')
            praise.append(Tip('corner.best:' + stage, 'praise', sentence, [], gain, cost=gain + 1.0, count=1,
                              place=_section_place(stage, run, gained[0]) if gained and prior['id'] == ref['id']
                              else None))
        found = stitch(reader, run, ref, ref_corners, before)
        if found is None or len(found['loaded']) + 1 < POSSIBLE_MIN:
            return
        best, loaded = found['best'], found['loaded']
        # measured against the quickest of the other runs there (not the
        # reference's own time, which may be its slow section), and never
        # the last section, which holds the finish
        last = len(best['grid']) - 1
        mine = [(best['runner_up'][j] - best['best'][j], j) for j in best['best']
                if best['who'].get(j) == run['id'] and best['runner_up'].get(j) is not None and j not in (0, last)]
        mine = [(g, j) for g, j in mine if g >= coach_context.SECTION_MIN]
        if mine:
            g, j = max(mine)
            praise.append(Tip('corner.bestsection:{}:{:.0f}'.format(stage, best['grid'][j]['apex']), 'praise',
                              'On {}, your quickest through {} of your last {} runs here: {:.1f} s up on the '
                              'next best.'.format(name, coach_context.section_name(best['grid'][j]),
                                                  len(loaded) + 1, g),
                              [], g, cost=g, count=1,
                              place=place(stage, run, best['grid'][j]['apex'], best['grid'][j]['d0'],
                                          best['grid'][j]['d1'])))
        pb = found['pb']
        if pb is not None and found['gain'] >= 3 * coach_context.SECTION_MIN:
            mine_pb = best['per_run'].get(pb['id']) or {}
            gains = {j: t - best['best'][j] for j, t in mine_pb.items() if j in best['best'] and j != 0}
            j = max(gains, key=lambda k: gains[k]) if gains else None
            notes.append(Tip('corner.possible:' + stage, 'note',
                             'On {}, your best sections put together make {:.1f} s, {:.1f} s under {} in '
                             'the {} ({:.1f} s).{}'.format(
                                 name, found['possible'], found['gain'], 'your best clean run', car, pb['result_time'],
                                 ' The biggest gain is {}, {:.1f} s.'.format(
                                     coach_context.section_name(best['grid'][j]), gains[j]) if j is not None else ''),
                             [], found['gain'], cost=0.0, count=len(loaded) + 1))

    # -- the potential time: the top 3 places after every finished run (docs/telemetry-extrapolation.md 6.2) --

    def _potential_lead(self, stage, name, run, corners):
        """After the newest finished run of a stage: the TOP places with the most time available against the grip
        layer of the stored potential (oversteer/potential.py), one fix each, then the place that closed on the
        potential the most since the run before. Said every run, outside the budget and the quiet rules of
        select(): they are about this run. Nothing without a potential (fewer than ENV_MIN_RUNS runs of the car
        on the surface, or none finished on the stage) or a finished run with a trace."""
        reader = self.reader
        if run['finished'] != 1 or run['car'] is None or run['discipline'] == 'drift' \
                or run['run_class'] in (None, 'restart', 'unclassified', 'partial'):      # a partial one holds the slow-down
            return
        pot = potential.stored(reader, stage, run['car'])
        if pot is None:
            return
        found = potential.stitched(reader, run)
        rows = self._potential_rows(pot, run, corners, found)
        if rows is None:
            return
        car = reader.car_by_id(run['car']) or {}
        data = car_data.entry(car.get('key'))
        band = potential.power_band(data) if data else None
        layers = potential.layers(pot, None if found is None else found['splits_possible'])
        rank = 0
        for row in potential.places(rows):
            diag = self._diagnose_place(stage, run, row, found)
            terms = potential.critique(row, row.get('best_gear'), band, measures=diag['m'] if diag else None)
            said = potential.call(row, terms[0][1] if terms else None, diag)
            if said is None:
                continue                    # nothing to do that the run shows: the next place is the third
            rank += 1
            self._claimed.append((stage, run['id'], row['d0'], row['d1']))
            text = 'On {}, {}{}'.format(name, said[0].lower(), said[1:])
            evidence = ['{:.1f} s through it: the grip layer {:.1f} s{}.'.format(
                row['time'], row['grip_s'], ', the car {:.1f} s'.format(row['car_s']) if row.get('car_s') else '')]
            if row.get('apex_kmh') and row.get('apex_kmh_pot'):
                evidence.append('Slowest point {:.0f} km/h; the grip allows {:.0f}.'.format(
                    row['apex_kmh'], row['apex_kmh_pot']))
            if rank == 1 and layers['grip'] is not None:
                evidence.append('Potential on this stage: you {}, grip {}{}.'.format(
                    _clock(layers['user']), _clock(layers['grip']),
                    ', car {}'.format(_clock(layers['car'])) if layers['car'] is not None else ''))
            self._lead.append(Tip('potential.top3:{}:{}'.format(stage, rank), 'top3', text, evidence, row['available'],
                                  cost=row['available'], count=1, rank=rank,
                                  place=place(stage, run, row['apex_d'], row['d0'], row['d1'])))
            if rank == potential.TOP:
                break
        before = [r for r in reader.stage_runs(stage, exclude=run['id'], limit=20, car=run['car'])
                  if r['started'] <= run['started'] and r['finished'] == 1 and r['result_time']
                  and r['run_class'] in ('clean', 'learning', 'off')]
        if not before:
            return
        previous = self._potential_rows(pot, dict(before[0], stage=stage, car=run['car']), None, found)
        found = potential.improved(rows, previous) if previous else None
        if found is not None:
            row, gain = found
            self._lead.append(Tip('potential.improved:{}:{}'.format(stage, run['id']), 'praise',
                                  'On {}, {}: {:.1f} s closer to the potential than last time.'.format(
                                      name, potential.where(row), gain), [], gain, cost=gain, count=1, delta_prev=gain,
                                  place=place(stage, run, row['apex_d'], row['d0'], row['d1'])))

    def _quickest_pass(self, stage, run, split, found):
        """The run row of the quickest pass through a split of the stage, `run` itself excluded (None where it is the
        quickest, or there is none to compare with): the one the splits' stitch names for the split, else the car's
        quickest finished clean run."""
        reader = self.reader
        who = found['best']['who'].get(split) if found is not None and split is not None else None
        if who is None:
            if found is not None:
                pb = found['pb']
            else:
                done = [r for r in reader.stage_runs(stage, limit=60, car=run['car'], ranked=True)
                        if r['started'] <= run['started'] and r['run_class'] == 'clean' and r['finished'] == 1
                        and r['result_time']]
                pb = min(done, key=lambda r: r['result_time']) if done else None
            who = pb['id'] if pb is not None else None
        if who is None or who == run['id']:
            return None
        return reader.run(who)

    def _diagnose_place(self, stage, run, row, found):
        """coach_diagnosis.diagnose_section of `run` through a top-3 place (its `d0`..`d1`) against the quickest pass
        through it, one stretch per stored corner of that pass inside it; None where the run is the quickest, either
        trace is gone or the pass has no corner there."""
        quick = self._quickest_pass(stage, run, row.get('split'), found)
        mine = self._arrays(run)
        theirs = self._arrays(quick) if quick is not None else None
        if mine is None or theirs is None:
            return None
        d0, d1 = row['d0'], row['d1']
        stored = [k for k in self.reader.corners(quick['id']) if k.get('d') is not None]
        corners = sorted((k for k in stored if k.get('d0') is not None and d0 <= k['d'] < d1), key=lambda k: k['d'])
        if not corners:
            return None
        first = float(theirs['arr']['d'][0])
        prev = max((k['d'] + 5.0 for k in stored if k['d'] < corners[0]['d']), default=None)     # never back into the corner before
        lo = max(d0 - coach_context.APPROACH, first if prev is None else prev, first)
        parts = coach_diagnosis.measures_section(mine['arr'], theirs['arr'], {
            'a': d0, 'b': d1, 'lo': lo, 'floor': lo if prev is None else prev, 'corners': corners,
            'game': self._game(run['car'])}, self._envelope(stage, run['car']))
        before = max((k for k in stored if k['d'] < corners[0]['d']), key=lambda k: k['d'], default=None)
        d = coach_diagnosis.diagnose_section(parts, None, None if before is None else coach_context.corner_name(before))
        if d is not None and found is not None and found['best']['who'].get(row.get('split')) is not None:
            _name_the_pass(d, 'your quickest pass')          # the comparison is the split's quickest pass, not the PB
        return d

    def _potential_rows(self, pot, run, corners, found=None):
        """potential.analyse_run() of a run (stage_runs row or run row), its off ranges left out and the seconds it
        steered against the yaw in each section set; None without a trace. With `found` (the splits' stitch) the
        rows are put on the splits' sections (potential.on_splits): the names, bounds and times the splits sheet has."""
        reader = self.reader
        loaded = self._arrays(run)
        if loaded is None:
            return None
        arr = loaded['arr']
        bad = [(e['d0'], e['d1'] if e['d1'] is not None else e['d0']) for e in reader.events(run['id'])
               if e['kind'] in ('off', 'spin', 'stall', 'hit') and e['d0'] is not None]
        rows = potential.analyse_run(pot, arr, bad)
        if found is not None:
            rows = potential.on_splits(pot, rows, found, run['id'], bad)
        corners = corners if corners is not None else reader.corners(run['id'])
        for row in rows:
            row['counter_steer_s'] = sum((k.get('counter_steer') or 0.0) * (k.get('duration') or 0.0) for k in corners
                                         if k.get('d') is not None and row['d0'] <= k['d'] < row['d1'])
        return rows

    OFF_NEAR = 150.0                 # m: offs this close are one place
    OFF_RUNS = 20                    # earlier runs the count of offs at a place is over
    OFF_CLEAN = 5                    # earlier clean runs the speed, braking and rotation are compared with
    OFF_LEAD = 30.0                  # m before the off at which the speed is read
    OFF_BRAKE = 10.0                 # m: a braking point further than this from the clean runs' is earlier or later

    def _off_facts(self, stage, run, e, corners):
        """What an off was, as facts and no cause (the classifier does not generalise yet, docs/telemetry-
        extrapolation.md 5.3): where it was, how often there, the speed against the clean runs there, the braking
        point and the rotation. '' where there is nothing to compare."""
        reader = self.reader
        d = e['d0']
        before = [r for r in reader.stage_runs(stage, exclude=run['id'], limit=self.OFF_RUNS, car=run['car'])
                  if r['started'] <= run['started']]
        facts = []
        near = [k for k in corners if k.get('d') is not None and abs(k['d'] - d) <= self.OFF_NEAR]
        corner = min(near, key=lambda k: abs(k['d'] - d)) if near else None
        if corner is not None:
            facts.append('It was in {}.'.format(coach_context.corner_name(corner).rsplit(' at ', 1)[0]))
        trace = reader.trace(run['id'])
        clean = [r for r in before if r['run_class'] == 'clean' and r['finished'] == 1][:self.OFF_CLEAN]
        sheets = [(r, reader.trace(r['id'])) for r in clean]
        sheets = [(r, t) for r, t in sheets if t]
        if trace and sheets:
            at = d - self.OFF_LEAD
            mine = _speed_at(trace, at)
            theirs = [v for v in (_speed_at(t, at) for _r, t in sheets) if v is not None]
            if mine is not None and theirs:
                best = statistics.median(theirs)
                facts.append('You were at {:.0f} km/h {:.0f} m before it, {} your {} clean runs there ({:.0f}).'.format(
                    mine * 3.6, self.OFF_LEAD,
                    'level with' if abs(mine - best) < coach_context.SPEED_DELTA else
                    '{:.0f} km/h {}'.format(abs(mine - best) * 3.6, 'over' if mine > best else 'under'),
                    len(theirs), best * 3.6))
            peak = _peak_yaw(trace, d - 60.0, d)
            others = [v for v in (_peak_yaw(t, d - 60.0, d) for _r, t in sheets) if v is not None]
            if peak is not None and others:
                facts.append('The car was turning at up to {:.0f} deg/s in the 60 m before it, against {:.0f} deg/s '
                             'in your clean runs.'.format(math.degrees(peak), math.degrees(statistics.median(others))))
        if corner is not None and corner.get('brake_d') is not None:
            theirs = []
            for r in sheets or [(r, None) for r in clean]:
                found = reader.corners(r[0]['id'])
                match = min((k for k in found if k.get('d') is not None and k.get('brake_d') is not None
                             and abs(k['d'] - corner['d']) <= 3 * coach_context.MATCH_CORNER),
                            key=lambda k: abs(k['d'] - corner['d']), default=None)
                if match is not None:
                    theirs.append(match['brake_d'])
            if theirs:
                gap = corner['brake_d'] - statistics.median(theirs)
                facts.append('Braking began {} than in your clean runs.'.format(
                    'at the same place' if abs(gap) < self.OFF_BRAKE else '{:.0f} m {}'.format(
                        abs(gap), 'earlier' if gap > 0 else 'later')))
            steered = (corner.get('counter_steer') or 0.0) * (corner.get('duration') or 0.0)
            if steered >= 0.3:
                facts.append('The steering went against the yaw for {:.1f} s.'.format(steered))
        count = 1
        for r in before:
            if any(o['d0'] is not None and abs(o['d0'] - d) <= self.OFF_NEAR for o in reader.events(r['id'], 'off')):
                count += 1
        if count > 1:
            facts.append('The {} off within {:.0f} m of here in your last {} runs on it.'.format(
                _ordinal(count), self.OFF_NEAR, len(before) + 1))
        return ' '.join(facts)

    def _model_notes(self, model, now, candidates):
        """What the car's model says it is still learning."""
        from .shift_learner import POWER_BIN
        recent_tunes = sorted(g for g, at in model.retuned.items() if now - at < 7 * DAY)
        if recent_tunes:
            candidates.append(Tip('retuned:' + ','.join(str(g) for g in recent_tunes), 'tip', '{} re-tuned; {} shift '
                                  'points are being learnt again.'.format(
                                      'Gear {} was'.format(recent_tunes[0]) if len(recent_tunes) == 1 else
                                      'Gears {} were'.format(', '.join(str(g) for g in recent_tunes)),
                                      'its' if len(recent_tunes) == 1 else 'their'), value=0.0, cost=0.01))
        ceiling = model.ceiling()
        needed = int(ceiling * 0.5 / POWER_BIN) if ceiling else 0
        bands = model.known_bands()
        if not model.game_data() and needed and bands < needed * 0.8:            # the game's data needs no learning
            candidates.append(Tip('learning', 'tip', 'Still learning the engine ({} of about {} rev bands known): '
                                  'full-throttle pulls from low revs, out of slow corners, fill it in fastest.'.format(
                                      bands, needed), value=0.0, cost=0.0))


def _clock(seconds):
    """3:03.1 from seconds (the splits row's way of writing a time)."""
    if seconds is None:
        return '–'
    m, rest = divmod(seconds, 60.0)
    return '{:d}:{:04.1f}'.format(int(m), rest) if m else '{:.1f}'.format(rest)


def _speed_at(trace, d):
    """The speed (m/s) of the first row of the trace at or past distance `d`, or None."""
    track = coach_context.along(trace)
    i = bisect.bisect_left(track, d)
    if i >= len(trace):
        return None
    v = trace[i][coach_context.CH['speed']]
    return v if v == v else None


def _peak_yaw(trace, d0, d1):
    """The highest |yaw rate| (rad/s) of the trace's rows between distances d0 and d1, or None."""
    track = coach_context.along(trace)
    lo, hi = bisect.bisect_left(track, d0), bisect.bisect_right(track, d1)
    values = [abs(trace[i][coach_context.CH['yaw_rate']]) for i in range(lo, hi)
              if trace[i][coach_context.CH['yaw_rate']] == trace[i][coach_context.CH['yaw_rate']]]
    return max(values) if values else None


def _began_mid_stage(reader, run_id, stage):
    """Whether an ACR run began further along the road than the start line (run_start more than
    stage_tables.START_LINE_PAST past it): its trace's distances are not the reference grid's."""
    line = stage_tables.start_line(stage_tables.entry(stage))
    start = reader.run_start(run_id)
    return line is not None and start is not None and start > line + stage_tables.START_LINE_PAST


def stitch(reader, run, ref, ref_corners, before):
    """The best of the car's runs on the stage section by section, for both the coach's "possible" note and
    splits(), so they agree: {best (coach_context.stitched on the reference's grid), loaded (the runs read),
    pb (the quickest finished clean run, `run` itself counting; None without one), possible (its time less what
    the best sections take off it, coach_context.sum_of_best), gain}. None without the reference's trace or a grid."""
    ref_trace = reader.trace(ref['id'])
    if not ref_trace or not ref_corners:
        return None
    ref_rows = {'trace': coach_context.stage_rows(ref_trace, ref['course'], True, ref['result_time']),
                'corners': ref_corners, 'course': ref['course'], 'run': ref['id']}
    finished = [r for r in before + [run] if r['run_class'] == 'clean' and r.get('finished') == 1
                and r.get('result_time')]
    pb = min(finished, key=lambda r: r['result_time']) if finished else None
    picked = ([run] + [r for r in before if r['id'] != ref['id'] and r['run_class'] in ('clean', 'learning', 'off',
                                                                                       'partial')])[:POSSIBLE_RUNS]
    if pb is not None and pb['id'] != ref['id'] and all(r['id'] != pb['id'] for r in picked):
        picked.append(pb)
    loaded = []
    for r in picked:
        if r['id'] == ref['id']:
            continue
        trace, found = reader.trace(r['id']), reader.corners(r['id'])
        if trace and found:
            loaded.append({'run': r['id'], 'trace': coach_context.stage_rows(
                trace, r['course'], r['finished'] == 1, r['result_time']), 'corners': found,
                'mid_stage': _began_mid_stage(reader, r['id'], run['stage'])})
    # a run that began mid-stage keeps to `loaded` (the game's sectors place it from where it began) but is
    # not on the grid: its distance 0 is not the line
    best = coach_context.stitched(ref_rows, [x for x in loaded if not x['mid_stage']])
    if best is None:
        return None
    possible = gain = None
    if pb is not None:
        possible, gain = coach_context.sum_of_best(best, pb['id'], pb['result_time'])
    # the game's sectors are the splits where the stage has them (SECTOR_SPLITS, every one with a best): the splits'
    # sum of best, and so the potential's user layer, is then the sum of the best sectors (`splits_possible`, and
    # `splits_gain` against the PB), not `possible`, which stays the corner sections' (the "possible" note's)
    sectors = _sectors(run['stage'], run, ref_rows, loaded, {r['id']: r['run_class'] for r in before + [run]},
                       {x['run']: reader.run_start(x['run']) for x in [ref_rows] + loaded},
                       {x['run']: reader.run_clock(x['run']) for x in [ref_rows] + loaded},
                       {x['run']: (reader.run(x['run']) or {}).get('result_time') for x in [ref_rows] + loaded})
    by_sector = bool(sectors) and all(v['confidence'] in SECTOR_SPLITS and v['best'] is not None for v in sectors)
    splits_possible = sum(v['best'] for v in sectors) if by_sector else possible
    return {'best': best, 'loaded': loaded, 'ref_rows': ref_rows, 'pb': pb, 'possible': possible, 'gain': gain,
            'sectors': sectors, 'by_sector': by_sector, 'splits_possible': splits_possible,
            'splits_gain': None if pb is None or splits_possible is None else pb['result_time'] - splits_possible}


def _prior_best(run, before):
    """The quickest finished clean run of the car on the stage before `run`, or None."""
    found = [r for r in before if r['id'] != run['id'] and r['run_class'] == 'clean' and r.get('finished') == 1
             and r.get('result_time')]
    return min(found, key=lambda r: r['result_time']) if found else None


def _ref_is_quickest(ref, before):
    """True when no earlier finished clean or learning run was quicker over the whole stage than the reference
    (which is ranked by the time after the first section)."""
    return not any(r['id'] != ref['id'] and r['run_class'] in ('clean', 'learning') and r.get('finished')
                   and r.get('result_time') and r['result_time'] < ref['result_time'] for r in before)


SECTOR_SPLITS = ('game', 'medium')    # the confidences of a stage's sectors good enough to be its splits


def splits(reader, profile, car_id):
    """The car's split times on the stage of its most recent run (the coach's own reference and the
    runs "possible" is built from), or None with fewer than two runs, no corners or no grid: a dict with
    `stage`, `name`, `last` (the latest run's time, None unless it finished), `best` (the quickest finished
    clean run's), `possible` (the quickest run's time less what the best sections take off it, section by
    section over the whole stage, coach_context.sum_of_best: never over `best`), `gain` (best less possible),
    `new_pb` (the last run is the best, and clean), `last_class`, `finish_m` and `finish_confidence` (the stage's
    flying finish, where the table has one: the times end there, not at the stop), `runs`, and `splits`: per section of the reference's grid, `name`, `last`, `best`, `gold` (the
    latest run set the best), `delta` (last less best), `pb` (the reference run's time through it: the PB the
    LiveSplit colours are against), `margin` (a gold split: how much it beat the previous best by), `cum` (the last run's elapsed time at the split's end less the PB's: ahead
    when negative), `d0` and `d1` (m, the split's bounds on the stage) and `finish` (the last section holds
    the slow-down to the stop where the finish is not a line). `ref_run` is the reference (PB) run's id. Every grid section has a row: `last`, `best` and `pb` are None where a run did not time it (an off touched it, grid_times).
    `potential` is the stage's three layers ({user: the sum of best, grip, car: seconds or None, built}, oversteer/
    potential.py; None without one), with each split's `grip_s`, `car_s` and `available` (the last run's time less
    the grip layer's through the split; only where there is a potential). `sectors` is the game's own sectors of the stage (stage_tables.sector_bounds; S1.., `last`, `best`,
    `gold`, `delta`, `pb`, `cum`, `margin`, `finish`, `d0`, `d1` as above, `confidence`), None where the stage's cannot
    be placed. `unit` says what `splits` are: 'sectors' where the sectors lie on the game's lines or a measured start
    and finish (SECTOR_SPLITS) and every one has a best (then `splits` are the sector rows, `possible` is their sum of
    best and so is the potential's user layer), else 'sections' (the corner sections); `sections` are always those. The best is over the
    clean and learning runs read; a sector a run did not drive from end to end is not timed."""
    rows = reader.metrics(profile, car_id, SESSIONS_READ)
    started = {}
    for r in rows:
        if r['stage'] and r['run'] is not None:
            started[r['run']] = r['started']
    run = None
    for run_id, _ in sorted(started.items(), key=lambda kv: -kv[1]):
        found = reader.run(run_id)
        if found is not None and found['run_class'] not in (None, 'restart', 'unclassified') \
                and found['discipline'] != 'drift':
            run = found
            break
    if run is None:
        return None
    stage = run['stage']
    before = [r for r in reader.stage_runs(stage, exclude=run['id'], limit=60, car=run['car'], ranked=True)
              if r['started'] <= run['started']]
    ref = coach_context.reference_run([r for r in before if r['run_class'] in ('clean', 'learning')], wet=run['wet'])
    if ref is None:
        return None
    found = stitch(reader, run, ref, reader.corners(ref['id']), before)
    if found is None:
        return None
    best, loaded, ref_rows = found['best'], found['loaded'], found['ref_rows']
    mine = next((x for x in loaded if x['run'] == run['id']), None)
    last_times = best['per_run'].get(run['id'], {})
    best_time = found['pb']['result_time'] if found['pb'] is not None else None
    last = len(best['grid']) - 1
    pot = potential.stored(reader, stage, run['car'])
    out = []
    for j in range(len(best['grid'])):                   # every section: '–' where the PB run did not time it
        t = last_times.get(j)
        d1 = best['bounds'][j][1]
        mine_at = coach_context.elapsed_at(mine['trace'], d1) if mine else None
        ref_at = coach_context.elapsed_at(ref_rows['trace'], d1)
        gold = t is not None and best['who'].get(j) == run['id']
        out.append({'name': coach_context.section_name(best['grid'][j]), 'last': t, 'best': best['best'].get(j),
                    'gold': gold, 'margin': best['runner_up'][j] - t if gold and best['runner_up'].get(j) else None,
                    'delta': None if t is None or j not in best['best'] else t - best['best'][j], 'finish': j == last,
                    'pb': best['base'].get(j), 'cum': None if mine_at is None or ref_at is None else mine_at - ref_at,
                    'd0': best['bounds'][j][0], 'd1': d1})
        if pot is not None:
            # the potential through the split's own distances: the grip layer's and the car's seconds, and the time
            # the last run has available against the grip layer (docs/telemetry-extrapolation.md 6.1)
            grip_s, car_s = potential.span_times(pot, best['bounds'][j][0], d1)
            out[-1].update(grip_s=grip_s, car_s=car_s, available=None if t is None else t - grip_s)
    if not out:
        return None
    entry = stage_tables.entry(stage) or {}
    finished = run['finished'] == 1
    # the game's own sectors are THE splits where a stage has them placed from real or measured lines (and every one
    # has a best: stitch); the corner sections are then `sections`. Elsewhere the sections are the splits
    sectors, unit, rows, possible = found['sectors'], 'sections', out, found['possible']
    if found['by_sector']:
        unit, rows, possible = 'sectors', [dict(v) for v in sectors], found['splits_possible']
        if pot is not None:
            for v in rows:
                v['grip_s'], v['car_s'] = potential.span_times(pot, v['d0'], v['d1'])
                v['available'] = None if v['last'] is None else v['last'] - v['grip_s']
    return {'stage': stage, 'ref_run': ref['id'], 'name': _stage_name(stage, reader.stage(stage)),
            'last': run['result_time'] if finished else None, 'last_class': run['run_class'],
            'new_pb': bool(finished and run['run_class'] == 'clean' and found['pb'] is not None
                           and found['pb']['id'] == run['id']),
            'finish_m': entry.get('finish_m'), 'finish_confidence': entry.get('finish_confidence'),
            'best': best_time, 'possible': possible,
            'potential': None if pot is None else dict(potential.layers(pot, possible), built=pot['built']),
            'gain': None if best_time is None else best_time - possible, 'runs': len(loaded) + 1,
            'unit': unit, 'splits': rows, 'sections': out, 'sectors': sectors}


def _sectors(stage, run, ref_rows, loaded, classes, starts=None, clocks=None, results=None):
    """The game's sectors of `stage` timed from the traces (`ref_rows` and `loaded`, as splits reads them), or None.
    Each sector's bounds are on the road spline; a trace's distance is driven from the run's start, which
    is the start line, so a bound is taken less it. A run is timed through a sector it covered end to end
    (it may stop END_SLACK m short of the finish: its last sector then ends at its result); the first sector
    starts at the clock's start. A run that did
    not begin at the start line (`starts`: run id to where along the spline it began) is placed from where it did.
    A run on the game's own clock (`clocks`: run id to 'game') is timed by it where its distance crosses each line
    (coach_context.sector_times), so its sectors add up to the stage time."""
    entry = stage_tables.entry(stage)
    placed = stage_tables.sector_bounds(entry)
    if placed is None:
        return None
    bounds = [(a - placed['start_m'], b - placed['start_m']) for a, b in placed['bounds']]

    def times(trace, run_id=None):
        origin = (starts or {}).get(run_id)
        if origin is None:
            origin = stage_tables.run_origin(entry)
        shift = 0.0 if origin is None else origin - placed['start_m']
        return coach_context.sector_times(trace, bounds, shift, (clocks or {}).get(run_id) == 'game',
                                          (results or {}).get(run_id))

    mine = next((x for x in loaded if x['run'] == run['id']), None)
    last = times(mine['trace'], run['id']) if mine else {}
    best, who, runner = {}, {}, {}
    for x in [{'run': ref_rows['run'], 'trace': ref_rows['trace']}] + loaded:
        if classes.get(x['run']) not in ('clean', 'learning'):
            continue
        for i, t in times(x['trace'], x['run']).items():
            if i not in best or t < best[i]:
                if i in best:
                    runner[i] = best[i]
                best[i], who[i] = t, x['run']
            elif i not in runner or t < runner[i]:
                runner[i] = t
    pbs = times(ref_rows['trace'], ref_rows['run'])
    out = []
    for i in range(len(bounds)):
        t = last.get(i)
        mine_at = coach_context.elapsed_at(mine['trace'], bounds[i][1]) if mine else None
        ref_at = coach_context.elapsed_at(ref_rows['trace'], bounds[i][1])
        gold = t is not None and who.get(i) == run['id'] and best.get(i) == t
        out.append({'name': 'S{}'.format(i + 1), 'last': t, 'best': best.get(i), 'gold': gold,
                    'margin': runner[i] - t if gold and i in runner else None, 'finish': i == len(bounds) - 1,
                    'delta': None if t is None or i not in best else t - best[i],
                    'pb': pbs.get(i), 'cum': None if mine_at is None or ref_at is None else mine_at - ref_at,
                    'd0': bounds[i][0], 'd1': bounds[i][1], 'confidence': placed['confidence']})
    return out if any(v['last'] is not None or v['best'] is not None for v in out) else None


def select(candidates, praise, notes, state, now, limit=TIPS, show_all=False, techniques=()):
    """Rate limiting (coach_state) and the budget: the focus (the costliest tip, when it is a habit),
    then up to `limit` tips by cost. A coach opens with where the time is, so a tip is shown only when it
    costs at least BUDGET of the costliest one that has seconds from a run (`ref`), the rest going to the
    quiet "still:" lines (as do rough-constant tips beside such a tip). A habit that is not the costliest
    tip is a tip like the others: a shift habit never outranks a corner that cost more. A tip shown
    QUIET_AFTER times without getting worse turns into a quiet line too (at most STILL, the most
    recently shown) until it gets RESHOW_WORSE worse or RESHOW_AFTER has passed. Praise: up to PRAISE
    lines, not repeated once shown twice unless it grew, and when two tips show at least one, so a view
    does not read as a list of faults. Techniques (correct technique described) are shown once and come
    back when their share moved TECHNIQUE_SHIFT. Each note once."""
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
    costliest = max((tip.cost for tip in ranked if tip.ref), default=0.0)
    floor = BUDGET * costliest

    def worth(tip):
        if show_all:
            return True
        if tip.rough and costliest:
            return False                 # a rough constant is not weighed against seconds from a reference run
        return not costliest or tip.cost >= floor

    focus = ranked[0] if ranked and ranked[0].kind == 'focus' and worth(ranked[0]) else None
    if focus is not None:
        out.append(focus)
    tips = 0
    for tip in ranked:
        if tip is focus:
            continue
        tip.kind = 'tip'
        if not worth(tip):
            still.append(tip)
        elif fresh(tip) and (show_all or tips < limit):
            out.append(tip)
            tips += 1
        elif not fresh(tip):
            still.append(tip)
    praised = []
    for tip in sorted(praise, key=lambda tip: -tip.cost):
        seen = state.get(tip.id)
        grew = seen is not None and seen['value'] is not None and tip.value is not None \
            and abs(tip.value) <= abs(seen['value']) * (1 - RESHOW_WORSE)
        if show_all or seen is None or (seen['times'] or 0) < QUIET_AFTER or grew \
                or (seen['last_shown'] is not None and now - seen['last_shown'] >= RESHOW_AFTER):
            praised.append(tip)
    if not show_all:
        praised = praised[:PRAISE]
    if not praised and praise and len(out) >= 2:
        praised = [max(praise, key=lambda tip: tip.cost)]           # two tips: at least one thing done well
    out.extend(praised)
    done = set()
    for tip in sorted(techniques, key=lambda tip: tip.id):
        seen = state.get(tip.id)
        if tip.id not in done and (show_all or seen is None or seen['value'] is None or tip.value is None
                                   or abs(tip.value - seen['value']) >= TECHNIQUE_SHIFT):
            out.append(tip)
            done.add(tip.id)
    for tip in notes:
        if tip.id not in done and (show_all or tip.id not in state):
            out.append(tip)
            done.add(tip.id)
    still.sort(key=lambda tip: -(state.get(tip.id, {}).get('last_shown') or 0.0))
    for tip in still[:STILL]:
        tip.kind = 'still'
        if tip.id in state and not tip.text.startswith('Still'):
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
