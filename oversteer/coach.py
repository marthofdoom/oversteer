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

from . import coach_context
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
        first = [row for row in trace if row[t] <= LAUNCH_WINDOW]
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
        times = [0.0] + [_time_at(trace, c, m, along) for m in marks]
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
        losses = sorted((k['loss_entry'] + k['loss_exit'] for k in corners
                         if k.get('loss_entry') is not None and not k.get('off')), reverse=True)
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
TIPS = 3                         # tips per view
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

    __slots__ = ('id', 'kind', 'text', 'evidence', 'value', 'cost', 'count', 'ref')

    def __init__(self, id, kind, text, evidence=None, value=None, cost=0.0, count=0, ref=False):
        self.id, self.kind, self.text = id, kind, text
        self.evidence, self.value, self.cost, self.count = list(evidence or []), value, cost, count
        self.ref = ref                   # `cost` is seconds a stage worked out from a run (not a rough constant)

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


def _ordinal(n):
    return '{}{}'.format(n, {1: 'st', 2: 'nd', 3: 'rd'}.get(n if n < 20 else n % 10, 'th'))


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
                notes.append(Tip('shift.cheap:' + key, 'note',
                                 '{}{}: {:.0f} % of your changes up are early and {:.0f} % on the cut, about {:.1f} s '
                                 'a stage: too little to coach.'.format(change, _with(method), early * 100, cut * 100,
                                                                         cost)))
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

    def _rate_tips(self, slices, rows, candidates, praise):
        # Only the limiter held on a straight below top gear, with no corner to use it for
        # and no change up following (coach_context.limiter_episodes): between corners on
        # gravel, being on the limiter is often right and is never counted here
        limiter = [r for r in rows if r['name'] == 'limiter.held' and r['discipline'] != 'drift']
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

    def _launch_tips(self, slices, candidates, praise):
        rows = [r for (name, *_), v in slices.items() if name == 'launch.bog' for r in v]
        last = sorted(rows, key=lambda r: -r['started'])[:5]
        if len(last) >= LAUNCHES:
            bogged = sum(1 for r in last if r['value'] >= 1.0)
            if bogged / len(last) >= BOGS:
                candidates.append(Tip('launch.bog', 'tip', '{} of your last {} launches bogged: the car accelerated '
                                      'under {:.1f} g over its first half second, or took over half a second longer '
                                      'than usual to 50 km/h. Let the clutch out more gradually, or hold more '
                                      'revs.'.format(bogged, len(last), coach_context.LAUNCH_G),
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
        """What only the same stage can say: sections lost against the
        car's best run there, both pedals down on the straights, consistency."""
        stages = {}
        for r in rows:
            if r['stage']:
                stages.setdefault(r['stage'], []).append(r)
        for stage, stage_rows in sorted(stages.items()):
            name = _stage_name(stage)
            loss = sorted((r for r in stage_rows if r['name'] == 'corner.loss'), key=lambda r: -r['started'])
            if loss and loss[0]['value'] > CORNER_LOSS and loss[0]['discipline'] != 'drift':
                candidates.append(Tip('corner.loss:' + stage, 'tip', 'On {}, your last run lost {:.1f} s in its three '
                                      'worst sections against your best run there in this car.'.format(
                                          name, loss[0]['value']),
                                      value=loss[0]['value'], cost=loss[0]['value'], count=1, ref=True))
            drag = [r for r in stage_rows if r['name'] == 'pedal.drag'
                    and (r['discipline'] == 'circuit' or r['surface'] == 'tarmac')]
            found = recent(drag, 3)
            if found is not None and found[0] > DRAG_PER_KM:
                # Both pedals down on a straight is dead weight on tarmac; on loose ground it is the technique
                candidates.append(Tip('pedal.drag:' + stage, 'tip', 'On {} you were on both pedals on the straights: '
                                      'come off the brake before the throttle goes down.'.format(name),
                                      ['{:.1f} s per km over {} runs.'.format(found[0], found[2])],
                                      value=found[0], cost=found[0] * 2, count=found[1]))
            steady = sorted((r for r in stage_rows if r['name'] == 'consistency.split_sd'),
                            key=lambda r: -r['started'])
            if len(steady) >= 4 and steady[0]['value'] <= 0.7 * max(r['value'] for r in steady[1:]):
                praise.append(Tip('consistency:' + stage, 'praise', 'Steadier on {}: your splits vary by {:.1f} s, '
                                  'from {:.1f}.'.format(name, steady[0]['value'], max(r['value'] for r in steady[1:])),
                                  value=steady[0]['value'], cost=0.1))

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


def select(candidates, praise, notes, state, now, limit=TIPS, show_all=False):
    """Rate limiting (coach_state) and the budget: the focus (the costliest
    habit), then up to `limit` tips by cost. A coach opens with where the
    time is, so a tip is shown only when it costs at least BUDGET of the
    costliest one that has seconds from a run (`ref`), the rest going to the
    quiet "still:" lines (as do rough-constant tips beside such a tip). A
    tip shown QUIET_AFTER times without getting worse turns into a quiet
    line too (at most STILL, the most recently shown) until it gets
    RESHOW_WORSE worse or RESHOW_AFTER has passed; one praise, not repeated
    once shown twice unless it grew; each note once."""
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
        return show_all or not costliest or tip.cost >= floor

    focus = [tip for tip in ranked if tip.kind == 'focus' and worth(tip)]
    if focus:
        out.append(focus[0])
    tips = 0
    for tip in ranked:
        if focus and tip is focus[0]:
            continue
        tip.kind = 'tip'
        if not worth(tip):
            still.append(tip)
        elif fresh(tip) and (show_all or tips < limit):
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
