"""The context layer of the coach (docs/coach-techniques.md, section 7.2):
what a run's trace says about where and why, worked out before any rule
judges it.

Pure functions over a run's 10 Hz trace (telemetry_store.TRACE_CHANNELS),
its corners (drive_log.find_corners), its changes of gear and the car's
data. Nothing here reads the database: the caller fetches the reference
run and the other runs and hands them in, and writes what comes out
(drive_log._write_end). The rules in coach.py only read the classes.

Numbers marked (calibrate) are starting values from marth's Assetto Corsa
Rally captures (docs/coach-techniques.md, section 7.8).
"""

import bisect
import math
import statistics

from .shift_learner import FULL_THROTTLE, LIMITER_BAND, SHIFT_COVERAGE
from .telemetry_store import TRACE_CHANNELS

G = 9.80665
CH = {name: i for i, name in enumerate(TRACE_CHANNELS)}

# -- the run's rows --

FINISH_SLACK = 1.0               # s past the result time that a finished run's trace is still the stage

# -- incidents --
SLOW = 3.0                       # m/s: slower than this for SLOW_TIME is a slow stretch
SLOW_TIME = 1.0                  # s
SLOW_START = 100.0               # m: a slow stretch before this is the start...
SLOW_END = 50.0                  # m: ...and after this from the end the finish
OFF_TIME = 3.0                   # s: a slow stretch this long is an off (calibrate)
HIT_G = 2.5                      # g held for HIT_ROWS rows: a hit (calibrate)
HIT_ROWS = 2
HIT_ONE_G = 4.0                  # g in a single row: a hit by itself (a 10 Hz row can hold all of an impact; calibrate)
UNCHECKED_GAMES = ('wrcg',)     # games whose speed and acceleration channels are not checked (coach.LAUNCH_OUT, ATTITUDE_OUT)
HIT_DECEL = 3.0                  # g of speed lost across one row: a hit by itself (calibrate)
RESET_BACK = 2.0                 # m the distance goes back between two rows, with the car stopped: the game's reset
HIT_BEFORE = 2.0                 # s: a slow stretch this soon after a hit is an off
SPIN_HEADING = 200.0             # degrees of heading change in one yaw window: a spin (calibrate)
SPIN_REVERSAL = 1.0              # rad/s of the other sign past the slowest point (with a turn of REVERSAL_HEADING; 0.5 flags 1 hairpin in 8, the pendulum)
REVERSAL_HEADING = 90.0
INCIDENT_REACH = 100.0           # m either side of an off or a stop whose corners are not compared
SECTION_REACH = 20.0             # m a yaw window may be from a stall, a spin or a hit and still be its section

# -- run class --
RESTART_SHARE = 0.5              # of the stage: a run that stopped before is a restart
AWAY = 7 * 86400.0               # s: the first run after this long on a stage is a learning run (calibrate)
FINISH_SHARE = 0.95              # of the stage: a run that ended with no finish known, short of this, did not finish
NO_LENGTH_RESTART = 1000.0       # m: with no stage length, an unfinished run shorter than this is a restart

# -- corners and sections --
JOIN_TIME = 1.5                  # s between two corners at the pace through the gap: one complex (calibrate)
JOIN_GAP = 30.0                  # m, where the pace is not known
BRAKE_ON = 0.1                   # brake past this: an application
BRAKE_REACH = 200.0              # m before the slowest point in which the strongest application is looked for
THROTTLE_ON = 0.2
THROTTLE_FULL = 0.95
EXIT_YAW = 0.1                   # rad/s: straightened out
EXIT_LIMIT = 10.0                # s after the slowest point the exit is followed for
RELIFT = 0.3                     # throttle drop that is a lift on the exit
COAST = 0.05                     # both pedals under this above COAST_SPEED
COAST_SPEED = 10.0               # m/s
OVERLAP = 0.2                    # both pedals past this
STRAIGHT = 100.0                 # m from every corner: a straight
BLIP = 0.2                       # s either side of an H-pattern change down left out of the overlap
MATCH_CORNER = 15.0              # m: the same corner in two runs has its slowest point within this
MATCH_APEX = 25.0                # m: sections match by apex within this where the windows do not overlap
HAIRPIN = 135.0                  # degrees of heading change (calibrate)
LONG_BEND = 90.0                 # degrees: more than a radius of grade 3 or 4 implies
# Radius (m at the tightest point) to the call, ascending: 1 is the tightest (calibrate against the
# pace-note lists when they arrive, ClickUp 86e3faftn)
RADIUS_GRADES = ((12.0, 1), (25.0, 2), (45.0, 3), (80.0, 4), (140.0, 5))

# -- the limiter --
CRAWL = 8.0                      # m/s: slower than this on average, an episode is a crawl (calibrate)
SHIFT_AFTER = 1.0                # s: a change up this soon after an episode ends it
UP_DOWN = 4.0                    # s: a change down this soon after the change up
HELD = 1.5                       # s on the cut... (calibrate)
HELD_DISTANCE = 80.0             # ...or m
NEXT_CORNER = 100.0              # m: a corner this near ahead
NEXT_CORNER_TIME = 3.0           # s
SPIN_SLIP = 0.15                 # slip_rpm over this: wheelspin (calibrate)

# -- slip from the revs --
SLIP_SETTLED = 0.3               # s in the gear before a row's slip counts
SLIP_CLUTCH = 0.1                # rig clutch under this
SLIP_MIN_SPEED = 3.0             # m/s
PARTIAL = (0.2, 0.8)             # throttle band whose slip is zero for want of drive: the gear's zero
PARTIAL_ROWS = 15
ZERO_PLAUSIBLE = 0.05            # a zero further than this from 0: the ratio is wrong, the proxy is off

# -- launch --
LAUNCH_HELD = 0.5                # s of held revs before moving: a launch (as drive_detect.LAUNCH)
LAUNCH_G = 0.3                   # g over the first half second under which the car bogged (calibrate)
LAUNCH_LOOSE = ('gravel', 'snow', 'ice', 'loose-low')   # surfaces on which no g is a bog (a snow launch is 0.13-0.28 g)
LAUNCH_SLOW = 0.5                # s over the median time to 50 km/h: a bog (calibrate)
LAUNCH_HISTORY = 10              # launches the median is of
LAUNCH_ABORT = 10.0              # s: a run restarted this soon dropped its launch
LAUNCH_WINDOW = 0.5              # s
LAUNCH_CUT_WINDOW = 8.0          # s after the start in which the launch gear's time on the limiter is counted
CLUTCH_DOWN = 0.5                # rig clutch the launch never rose above: the game's auto-clutch
STALL = 300.0                    # rpm

# -- shifts --
EARLY_BELOW = 100.0              # rpm below the band's low end that is early
BAND_SLACK = 100.0               # rpm either side of the band that is still on it
MARGIN = {'sequential': 150.0, 'paddles': 150.0, 'h-pattern': 300.0}     # under the limiter, by way of changing
MARGIN_DEFAULT = 150.0
GRIP_PULLS = 6                   # pulls the grip lowering needs (calibrate)
GRIP_SHARE_MAX = 1.0             # the measured share of the engine's drive may not exceed this
LAUNCH_SHIFT = 3.0               # s: the first change up this soon after the start is the launch's
CLIMB_DEFAULT = 600.0            # rpm/s at full throttle where the trace cannot say
ACCEL_DEFAULT = 3.0              # m/s^2 where the trace cannot say
DRIVE_LOSS = 0.05                # share of the drive the next gear gives less, where the car data cannot say (calibrate)
CUT_NORMAL = 0.1                 # s of a change made well: the touch on the cut beyond it is what the cut costs
LOOKAHEAD = 15.0                 # s the time to the next braking is capped at
SKIP_BRAKE = 0.1                 # a change down with the brake past this is a block change, not a skipped gear

SPREAD_RUNS = 6                  # most recent runs the spread of a section's minimum speed is taken over
SPREAD_MIN = 4


def _fin(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def _dt(trace, i):
    """Seconds row i stands for (the gap to the next row, capped)."""
    if i + 1 < len(trace):
        return max(0.0, min(0.5, trace[i + 1][CH['t']] - trace[i][CH['t']]))
    return 0.1


def along(trace):
    """The distance column made non-decreasing (a car backing up does not
    undo the road it covered), for bisecting."""
    out, top = [], float('-inf')
    for row in trace:
        d = row[CH['distance']]
        if _fin(d):
            top = max(top, d)
        out.append(top)
    return out


def time_at(trace, track, distance):
    """When the run first passed `distance` (interpolated), or None if it
    never got there or the trace starts past it. `track` is along(trace)."""
    i = bisect.bisect_left(track, distance)
    t = CH['t']
    if i >= len(trace):
        return None
    if i == 0:
        return trace[0][t] if track[0] == distance else None
    a, b = track[i - 1], track[i]
    if b == a or a == float('-inf'):
        return trace[i][t]
    return trace[i - 1][t] + (trace[i][t] - trace[i - 1][t]) * (distance - a) / (b - a)


def distance_at(trace, t):
    """The distance along the run at trace time `t` (interpolated); None
    outside the trace."""
    times = [row[CH['t']] for row in trace]
    i = bisect.bisect_left(times, t)
    if i >= len(trace) or (i == 0 and times[0] > t):
        return None
    if i == 0 or times[i] == t:
        d = trace[i][CH['distance']]
        return d if _fin(d) else None
    a, b = trace[i - 1][CH['distance']], trace[i][CH['distance']]
    if not (_fin(a) and _fin(b)):
        return None
    return a + (b - a) * (t - times[i - 1]) / (times[i] - times[i - 1])


# -- 7.2.1: the run's rows, incidents, class, reference --

def stage_rows(trace, course=None, finished=False, result_time=None):
    """The rows that are the stage: up to the first row at or past
    `course` (where the run crossed the finish, or the last distance), and
    for a finished run no row past the result time (the trace's clock, from
    the run's start) plus FINISH_SLACK: a car parked after the line is not
    the stage."""
    rows = trace
    if course is not None and _fin(course):
        top = float('-inf')
        for i, row in enumerate(trace):
            d = row[CH['distance']]
            if _fin(d):
                top = max(top, d)
            if top >= course:
                rows = trace[:i + 1]
                break
    if finished and result_time:
        t = CH['t']
        limit = result_time + FINISH_SLACK
        k = next((i for i, row in enumerate(rows) if row[t] > limit), None)
        if k is not None:
            rows = rows[:k]
    return rows


def hits(trace, game=None):
    """[(first row, last row)] of the stretches with the car's
    acceleration, either way, over HIT_G for HIT_ROWS rows or more, and of
    the single rows that are a hit alone: over HIT_ONE_G, or with the speed
    dropping by more than HIT_DECEL g across the row (a 10 Hz row holds all
    of a wall). In a game whose speed channel is not checked (UNCHECKED_GAMES;
    WRCG's is noisy enough for a hundred of these in a run) a drop of speed is
    no hit: only the acceleration is read."""
    t, speed = CH['t'], CH['speed']
    big, strong = [], []
    for i, row in enumerate(trace):
        a_long, a_lat = row[CH['a_long']], row[CH['a_lat']]
        big.append((_fin(a_long) and abs(a_long) > HIT_G * G) or (_fin(a_lat) and abs(a_lat) > HIT_G * G))
        hard = (_fin(a_long) and abs(a_long) > HIT_ONE_G * G) or (_fin(a_lat) and abs(a_lat) > HIT_ONE_G * G)
        if i and game not in UNCHECKED_GAMES and _fin(row[speed]) and _fin(trace[i - 1][speed]):
            dt = max(0.05, row[t] - trace[i - 1][t])
            hard = hard or (trace[i - 1][speed] - row[speed]) / dt > HIT_DECEL * G
        strong.append(hard)
    out, i, n = [], 0, len(trace)
    while i < n:
        if big[i]:
            j = i
            while j + 1 < n and big[j + 1]:
                j += 1
            if j - i + 1 >= HIT_ROWS or any(strong[i:j + 1]):
                out.append((i, j))
            i = j + 1
        else:
            if strong[i]:
                out.append((i, i))
            i += 1
    return out


def incidents(trace, corners=(), course=None, unfinished=False, game=None):
    """The located trouble in a run: dicts (kind, class, d0, d1, t0, t1,
    value) of kinds
    - `off`: a slow stretch (under SLOW m/s for SLOW_TIME s, past the first
      SLOW_START m and before the last SLOW_END m) with reverse engaged,
      or lasting OFF_TIME s or more, or soon after a hit; class 'reverse',
      'long' or 'hit';
    - `stall`: any other slow stretch at a corner: the car nearly stopped
      in it (over-rotated, a handbrake held), a corner fault, not an off;
    - `stop`: any other slow stretch;
    - `hit`: a stretch over HIT_G (a wall, a rock, a landing);
    - `spin`: a corner whose yaw window turns the car through more than
      SPIN_HEADING, or reverses its yaw hard after the slowest point.
    A run that did not finish has no finish to be slow at: SLOW_END is not
    applied to it, and a slow stretch that runs to the end of its trace is an
    off (the crash, the stop before the restart). `game` is the trace's game
    (in a game of UNCHECKED_GAMES hits() does not read speed drops, and a slow
    stretch is not an off for a hit before it)."""
    out = []
    if not trace:
        return out
    t, speed, gear = CH['t'], CH['speed'], CH['gear']
    track = along(trace)
    end = float('inf') if unfinished else (course if course is not None and _fin(course) else track[-1]) - SLOW_END
    knocks = hits(trace, game)
    i, n = 0, len(trace)
    while i < n:
        if not (_fin(trace[i][speed]) and trace[i][speed] < SLOW):
            i += 1
            continue
        j = i
        while j + 1 < n and _fin(trace[j + 1][speed]) and trace[j + 1][speed] < SLOW:
            j += 1
        duration = trace[j][t] - trace[i][t] + _dt(trace, j)
        d0, d1 = track[i], track[j]
        if duration >= SLOW_TIME and d0 > SLOW_START and d1 < end:
            reverse = any(_fin(trace[k][gear]) and trace[k][gear] < 0 for k in range(i, j + 1))
            hit_before = None if game in UNCHECKED_GAMES else next(
                (k for k, last in knocks if trace[i][t] - HIT_BEFORE <= trace[last][t] <= trace[j][t]), None)
            event = {'d0': d0, 'd1': d1, 't0': trace[i][t], 't1': trace[j][t] + _dt(trace, j), 'value': duration}
            if reverse or duration >= OFF_TIME or hit_before is not None or (unfinished and j == n - 1):
                event.update(kind='off', **{'class': 'reverse' if reverse else (
                    'hit' if hit_before is not None and duration < OFF_TIME else 'long')})
            elif any(k['d0'] - SECTION_REACH <= d1 and d0 <= k['d1'] + SECTION_REACH for k in corners
                     if k.get('d0') is not None):
                event.update(kind='stall', **{'class': None})
            else:
                event.update(kind='stop', **{'class': None})
            out.append(event)
        i = j + 1
    # The game's reset after a crash: the distance goes back with the car stopped (and the gear 0)
    for k in range(1, n):
        back, here = trace[k - 1][CH['distance']], trace[k][CH['distance']]
        if (_fin(back) and _fin(here) and here < back - RESET_BACK and _fin(trace[k][speed])
                and trace[k][speed] < 1.0 and track[k] > SLOW_START and track[k] < end
                and not any(e['kind'] == 'off' and e['d0'] - INCIDENT_REACH <= track[k] <= e['d1'] + INCIDENT_REACH
                            for e in out)):
            out.append({'kind': 'off', 'class': 'reset', 'd0': track[k], 'd1': track[k], 't0': trace[k][t],
                        't1': trace[k][t], 'value': 0.0})
    for first, last in knocks:
        d0, d1 = track[first], track[last]
        if d0 > SLOW_START and d1 < end:
            out.append({'kind': 'hit', 'class': None, 'd0': d0, 'd1': d1, 't0': trace[first][t],
                        't1': trace[last][t], 'value': max(abs(trace[k][CH['a_long']]) if _fin(trace[k][CH['a_long']])
                                                          else 0.0 for k in range(first, last + 1)) / G})
    out.extend(spins(trace, corners))
    out.sort(key=lambda e: (e['d0'], e['kind']))
    return out


def spins(trace, corners):
    """`spin` events: yaw windows through more than SPIN_HEADING degrees,
    or turning at least REVERSAL_HEADING and then reversing hard (a
    rotation caught the other way past the slowest point)."""
    out = []
    t, yaw = CH['t'], CH['yaw_rate']
    for k in corners:
        i0, i1, apex = k.get('_i0'), k.get('_i1'), k.get('_apex')
        heading = k.get('heading_change') or 0.0
        spun = heading > SPIN_HEADING
        if not spun and i0 is not None and heading >= REVERSAL_HEADING and k.get('direction'):
            sign = 1 if k['direction'] > 0 else -1
            spun = any(_fin(trace[m][yaw]) and trace[m][yaw] * sign < -SPIN_REVERSAL
                       for m in range(apex, min(len(trace), i1 + 4)))
        if spun and k.get('d0') is not None:
            out.append({'kind': 'spin', 'class': None, 'd0': k['d0'], 'd1': k['d1'], 't0': trace[i0][t] if i0 is not None
                        else None, 't1': trace[i1][t] if i1 is not None else None, 'value': heading})
    return out


def mark_off(corners, events):
    """Set corner['off'] = 1 on the corners that are not to be compared:
    those within INCIDENT_REACH of an off or a stop, and the sections
    (complexes) of those a stall, a spin or a hit touches. 0 elsewhere."""
    for k in corners:
        k['off'] = 0
    touched = set()
    for e in events:
        if e['kind'] in ('off', 'stop'):
            for k in corners:
                if k.get('d0') is not None and k['d0'] - INCIDENT_REACH <= e['d1'] and e['d0'] <= k['d1'] + INCIDENT_REACH:
                    k['off'] = 1
        elif e['kind'] in ('stall', 'spin', 'hit'):
            for k in corners:
                if k.get('d0') is not None and k['d0'] - SECTION_REACH <= e['d1'] and e['d0'] <= k['d1'] + SECTION_REACH:
                    touched.add(k.get('complex'))
                    k['off'] = 1
    for k in corners:
        if k.get('complex') in touched and k.get('complex') is not None:
            k['off'] = 1


def run_class(finished, course, stage_length, events, started=None, last_started=None):
    """The class of a run (runs.run_class):
    - `restart`: not finished (or ended with no finish known, short of
      FINISH_SHARE of a known stage), and under half the stage;
    - `partial`: not finished, half the stage or more;
    - `off`: finished with at least one off;
    - `learning`: the first run of this car on this stage (`last_started`
      None) or the first after AWAY;
    - `clean`: everything else.
    Without a stage length an unfinished run shorter than NO_LENGTH_RESTART
    m is a restart."""
    if finished is None and stage_length and (course or 0.0) < FINISH_SHARE * stage_length:
        finished = 0                    # ended in silence well short of the line
    if finished == 0:
        length = stage_length or None
        if length is None:
            return 'restart' if (course or 0.0) < NO_LENGTH_RESTART else 'partial'
        return 'restart' if (course or 0.0) < RESTART_SHARE * length else 'partial'
    if any(e['kind'] == 'off' for e in events):
        return 'off'
    if started is not None and last_started is not None and started - last_started > AWAY:
        return 'learning'
    if started is not None and last_started is None:
        return 'learning'
    return 'clean'


def _grid_time(r):
    """The time a run is ranked by as a reference: its stage time less the first section's, which holds the
    launch (a 38 s start is not a quick stage)."""
    return r['result_time'] - (r.get('first_section') or 0.0)


def reference_run(candidates, result_time=None, wet=None):
    """The fastest finished run among `candidates` (Store.stage_runs rows
    of the same car and stage, class clean or learning; by stage time less the
    first section's, _grid_time) with the same
    wetness; None when there is none. A run with unknown wetness on either
    side is comparable."""
    best = None
    for r in candidates:
        if not r.get('finished') or not r.get('result_time'):
            continue
        if wet not in (None, 'unknown') and r.get('wet') not in (None, 'unknown') and r['wet'] != wet:
            continue
        if best is None or _grid_time(r) < _grid_time(best):
            best = r
    return best


# -- the slip of the driven wheels, from the revs --

def slip_rpm(trace, ratio):
    """[slip or None] per row: rpm / (speed x the game's ratio of the gear)
    - 1 less the gear's own zero, where `ratio(gear)` (rpm per m/s, the
    shipped gearing and tyre) is known. A row counts when the gear has been
    the same for SLIP_SETTLED s and the rig clutch is under SLIP_CLUTCH
    (or not sent). The zero of a gear is the median over its rows at partial
    throttle off the brake (the loaded tyre is a little smaller than the
    shipped one, and the gearing may differ from the shipped set); a gear
    with fewer than PARTIAL_ROWS such rows, or whose zero is further than
    ZERO_PLAUSIBLE from nothing (the ratio is wrong), gets None."""
    if ratio is None or not trace:
        return [None] * len(trace)
    t, speed, rpm, gear = CH['t'], CH['speed'], CH['rpm'], CH['gear']
    throttle, brake, clutch = CH['throttle'], CH['brake'], CH['clutch']
    raw, since, last_gear = [], 0, None
    ratios = {}
    for i, row in enumerate(trace):
        g = row[gear]
        if not _fin(g) or g < 1:
            raw.append(None)
            last_gear = None
            continue
        if g != last_gear:
            last_gear, since = g, i
        r = ratios.get(int(g), 0)
        if r == 0:
            r = ratios[int(g)] = ratio(int(g)) or None
        settled = trace[i][t] - trace[since][t] >= SLIP_SETTLED
        if (not r or not settled or not _fin(row[rpm]) or not _fin(row[speed]) or row[speed] < SLIP_MIN_SPEED
                or (_fin(row[clutch]) and row[clutch] >= SLIP_CLUTCH)):
            raw.append(None)
            continue
        raw.append(row[rpm] / (row[speed] * r) - 1.0)
    partial = {}
    for row, value in zip(trace, raw):
        if (value is not None and _fin(row[throttle]) and PARTIAL[0] <= row[throttle] <= PARTIAL[1]
                and (not _fin(row[brake]) or row[brake] < 0.05) and row[speed] >= 5.0):
            partial.setdefault(int(row[gear]), []).append(value)
    zeros = {g: statistics.median(v) for g, v in partial.items() if len(v) >= PARTIAL_ROWS}
    zeros = {g: z for g, z in zeros.items() if abs(z) <= ZERO_PLAUSIBLE}
    return [None if value is None or int(row[gear]) not in zeros else value - zeros[int(row[gear])]
            for row, value in zip(trace, raw)]


def slip_zeros(trace, ratio):
    """({gear: zero} of the gears whose proxy is on, {gear: zero} of those
    switched off for an implausible zero): the replay check's view of
    slip_rpm."""
    on, off = {}, {}
    if ratio is None:
        return on, off
    t, speed, rpm, gear = CH['t'], CH['speed'], CH['rpm'], CH['gear']
    throttle, brake = CH['throttle'], CH['brake']
    found, since, last_gear = {}, 0, None
    for i, row in enumerate(trace):
        g = row[gear]
        if not _fin(g) or g < 1:
            last_gear = None
            continue
        if g != last_gear:
            last_gear, since = g, i
        r = ratio(int(g))
        if (r and row[t] - trace[since][t] >= SLIP_SETTLED and _fin(row[rpm]) and _fin(row[speed])
                and row[speed] >= 5.0 and _fin(row[throttle]) and PARTIAL[0] <= row[throttle] <= PARTIAL[1]
                and (not _fin(row[brake]) or row[brake] < 0.05)):
            found.setdefault(int(g), []).append(row[rpm] / (row[speed] * r) - 1.0)
    for g, values in found.items():
        if len(values) >= PARTIAL_ROWS:
            z = statistics.median(values)
            (on if abs(z) <= ZERO_PLAUSIBLE else off)[g] = z
    return on, off


# -- 7.2.2: corners, complexes, sections --

def tightness(radius, heading, min_speed=None):
    """The call of a corner, as a co-driver would grade it: 'hairpin' for
    135 degrees and more, else the grade '1' (tightest) to '6' from the
    radius, with ' long' where the heading change is more than the radius
    alone implies (over 90 degrees on a grade 3 or 4). None without a
    radius."""
    if heading is not None and heading >= HAIRPIN:
        return 'hairpin'
    if radius is None:
        return None
    grade = next((g for limit, g in RADIUS_GRADES if radius <= limit), 6)
    if grade in (3, 4) and heading is not None and heading > LONG_BEND:
        return '{} long'.format(grade)
    return str(grade)


def corner_name(corner, stage_m=None):
    """'the 3 left at 1.8 km': what the corner is called until the pace
    notes arrive."""
    call = corner.get('tightness')
    side = 'left' if (corner.get('direction') or 0) > 0 else 'right'
    d = stage_m if stage_m is not None else corner.get('d')
    where = ' at {:.1f} km'.format(d / 1000.0) if d is not None else ''
    if call == 'hairpin':
        return 'the hairpin {}{}'.format(side, where)
    if call:
        return 'the {} {}{}'.format(call, side, where)
    return 'the {} corner{}'.format(side, where)


def build_sections(trace, corners):
    """Join the corners into sections (complexes) and number them (`complex`
    on each corner, from 0): two corners are one when the gap between the
    first one's yaw window and the next one's is under JOIN_TIME s at the
    run's pace through it (JOIN_GAP m where it has none). Returns [section]
    with `corners`, `d0`, `d1`, `apex` (the d of the slowest corner) and
    `lead` (the first corner)."""
    if not corners:
        return []
    track = along(trace)
    sections = []
    for k in sorted(corners, key=lambda x: x['d']):
        if sections:
            last = sections[-1]['corners'][-1]
            gap = k['d0'] - last['d1']
            if gap < 0:
                joined = True
            else:
                lo, hi = bisect.bisect_left(track, last['d1']), bisect.bisect_right(track, k['d0'])
                speeds = [trace[m][CH['speed']] for m in range(lo, hi) if _fin(trace[m][CH['speed']])]
                joined = (gap / (sum(speeds) / len(speeds)) < JOIN_TIME) if speeds and sum(speeds) > 0 \
                    else gap < JOIN_GAP
            if joined:
                sections[-1]['corners'].append(k)
                continue
        sections.append({'corners': [k]})
    for n, s in enumerate(sections):
        cs = s['corners']
        s['d0'], s['d1'] = min(x['d0'] for x in cs), max(x['d1'] for x in cs)
        s['apex'] = min(cs, key=lambda x: x['min_speed'])['d']
        s['lead'] = cs[0]
        s['id'] = n
        for x in cs:
            x['complex'] = n
    return sections


def brake_application(trace, track, lo, d_apex):
    """(distance of the onset, peak) of the strongest brake application
    between distance `lo` and the slowest point, or (None, None). A
    stab, the main braking and a touch at turn-in are all applications:
    the strongest is the one the corner was braked for."""
    a, b = bisect.bisect_left(track, lo), bisect.bisect_right(track, d_apex)
    best = None
    start = peak = None
    for i in range(a, min(b, len(trace))):
        v = trace[i][CH['brake']]
        if _fin(v) and v > BRAKE_ON:
            if start is None:
                start, peak = i, v
            peak = max(peak, v)
        elif start is not None:
            if best is None or peak > best[1]:
                best = (start, peak)
            start = None
    if start is not None and (best is None or peak > best[1]):
        best = (start, peak)
    if best is None:
        return None, None
    return track[best[0]], best[1]


def section_bounds(sections, start, end):
    """[(a, b)] distances of each section: it runs from where the braking
    for it begins (the lead corner's strongest application, never before
    the midpoint between the sections, never after its yaw window starts)
    to where the next one's begins; the first starts at `start`, the last
    ends at `end`. The straight after a corner is its exit."""
    bounds = []
    edges = [start]
    for prev, s in zip(sections, sections[1:]):
        mid = (prev['d1'] + s['d0']) / 2.0
        lead = s['lead']
        if lead.get('brake_d') is not None:
            onset = lead['d'] - lead['brake_d']
            edge = min(max(mid, onset), s['d0'])
        else:
            edge = mid
        edges.append(edge)
    edges.append(end)
    for a, b in zip(edges, edges[1:]):
        bounds.append((a, b))
    return bounds


def describe_corners(trace, corners, sections, shift_times=()):
    """Fill in each corner's braking and throttle: `brake_d` and
    `brake_peak` (the strongest application, metres before the slowest
    point and its peak), `throttle_on_t` (s from the slowest point to the
    throttle past THROTTLE_ON, 0 if already on), `throttle_t` (to
    THROTTLE_FULL; negative when it was full before the slowest point),
    `relifts`, `coast_entry`/`coast_exit` and `overlap_entry`/
    `overlap_exit` (s by phase) and `section_t` (on the section's first
    corner). The entry runs from the strongest braking's onset (the yaw
    window's start without braking) to the slowest point, the exit from
    there until the throttle is full with the car straight, or the
    section's end. `shift_times` are the trace times of the H-pattern
    changes down, whose blip is left out of the overlap. Returns the
    section bounds."""
    if not corners:
        return []
    track = along(trace)
    order = sorted(corners, key=lambda x: x['d'])
    for idx, k in enumerate(order):
        prev = order[idx - 1] if idx > 0 else None
        lo = max(k['d'] - BRAKE_REACH, prev['d1'] if prev is not None and prev['d1'] < k['d'] else float('-inf'))
        onset, peak = brake_application(trace, track, lo, k['d'])
        k['brake_d'] = (k['d'] - onset) if onset is not None else None
        k['brake_peak'] = peak
    bounds = section_bounds(sections, track[0] if track else 0.0, track[-1] if track else 0.0)
    t, speed, throttle, brake, yaw = CH['t'], CH['speed'], CH['throttle'], CH['brake'], CH['yaw_rate']
    blips = [(x - BLIP, x + BLIP) for x in shift_times]
    for s, (a, b) in zip(sections, bounds):
        s['bounds'] = (a, b)
        t_a, t_b = time_at(trace, track, a), time_at(trace, track, b)
        if t_a is None and track and a <= track[0]:
            t_a = trace[0][t]
        if t_b is None and track and b >= track[-1]:
            t_b = trace[-1][t]
        s['time'] = (t_b - t_a) if t_a is not None and t_b is not None else None
        cs = s['corners']
        for m, k in enumerate(cs):
            apex = k.get('_apex')
            if apex is None:
                apex = bisect.bisect_left(track, k['d'])
            apex = min(apex, len(trace) - 1)
            entry_d = k['d0'] if k['brake_d'] is None else min(k['d0'], k['d'] - k['brake_d'])
            entry_d = max(entry_d, a, cs[m - 1]['d'] if m else a)
            entry_i = bisect.bisect_left(track, entry_d)
            end_d = cs[m + 1]['d'] - (cs[m + 1].get('brake_d') or 0.0) if m + 1 < len(cs) else b
            end_d = min(max(end_d, k['d']), b)
            end_i = bisect.bisect_right(track, end_d)
            # Exit: until full throttle, straight
            limit_t = trace[apex][t] + EXIT_LIMIT
            exit_end = apex
            for i in range(apex, min(len(trace), end_i)):
                exit_end = i
                if trace[i][t] > limit_t:
                    break
                v, y = trace[i][throttle], trace[i][yaw]
                if i > apex and _fin(v) and v >= THROTTLE_FULL and (not _fin(y) or abs(y) < EXIT_YAW):
                    break
            on = next((i for i in range(apex, min(len(trace), end_i + 1)) if _fin(trace[i][throttle])
                       and trace[i][throttle] > THROTTLE_ON), None)
            k['throttle_on_t'] = trace[on][t] - trace[apex][t] if on is not None else None
            full = None
            if _fin(trace[apex][throttle]) and trace[apex][throttle] >= THROTTLE_FULL:
                back = apex
                while back > 0 and _fin(trace[back - 1][throttle]) and trace[back - 1][throttle] >= THROTTLE_FULL:
                    back -= 1
                full = trace[back][t] - trace[apex][t]
            else:
                j = next((i for i in range(apex, min(len(trace), end_i + 1)) if _fin(trace[i][throttle])
                          and trace[i][throttle] >= THROTTLE_FULL and trace[i][t] - trace[apex][t] <= EXIT_LIMIT),
                         None)
                if j is not None:
                    full = trace[j][t] - trace[apex][t]
            k['throttle_t'] = full
            lifts, top = 0, None
            for i in range(apex, exit_end + 1):
                v = trace[i][throttle]
                if not _fin(v):
                    continue
                top = v if top is None else max(top, v)
                if v < top - RELIFT:
                    lifts += 1
                    top = v
            k['relifts'] = lifts
            for name, rows in (('entry', range(entry_i, apex)), ('exit', range(apex, exit_end + 1))):
                coast = both = lasted = 0.0
                for i in rows:
                    v, w = trace[i][throttle], trace[i][brake]
                    if not (_fin(v) and _fin(w)):
                        continue
                    dt = _dt(trace, i)
                    lasted += dt
                    if trace[i][speed] > COAST_SPEED and v < COAST and w < COAST:
                        coast += dt
                    if v > OVERLAP and w > OVERLAP and not any(lo <= trace[i][t] <= hi for lo, hi in blips):
                        both += dt
                k['coast_' + name], k['overlap_' + name] = coast, both
                k['_' + name + '_t'] = lasted                  # not stored: what the share of a phase is of
            k['section_t'] = s['time'] if m == 0 else None
    return bounds


def pedal_time(trace, corners, shift_times=(), skip=()):
    """(coast s on straights, overlap s on straights, straight km): the
    time with both pedals off, or both on, more than STRAIGHT m from every
    corner's yaw window (and outside the `skip` distance windows, an off's
    surroundings), and the distance covered there."""
    track = along(trace)
    # A corner's window starts at its braking onset where that is earlier than STRAIGHT m before the
    # yaw window: the braking zone of a fast corner is not a straight
    near = [(min(k['d0'] - STRAIGHT, k['d'] - (k.get('brake_d') or 0.0)), k['d1'] + STRAIGHT)
            for k in corners if k.get('d0') is not None] + list(skip)
    blips = [(x - BLIP, x + BLIP) for x in shift_times]
    t, speed, throttle, brake = CH['t'], CH['speed'], CH['throttle'], CH['brake']
    coast = both = metres = 0.0
    for i, row in enumerate(trace):
        d = track[i]
        if any(lo <= d <= hi for lo, hi in near):
            continue
        dt = _dt(trace, i)
        if _fin(row[speed]):
            metres += row[speed] * dt
        v, w = row[throttle], row[brake]
        if not (_fin(v) and _fin(w)):
            continue
        if row[speed] > COAST_SPEED and v < COAST and w < COAST:
            coast += dt
        # Braking harder than the throttle with the speed falling is braking, however late the corner
        slowing = i > 0 and _fin(trace[i - 1][speed]) and row[speed] < trace[i - 1][speed] and w > v
        if (row[speed] > 1.0 and v > OVERLAP and w > OVERLAP and not slowing
                and not any(lo <= row[t] <= hi for lo, hi in blips)):
            both += dt
    return coast, both, metres / 1000.0


# -- the reference, loss and spread --

def sections_of(corners):
    """Sections rebuilt from stored corner rows (each with d0, d1, d,
    min_speed, complex, brake_d, off): [section dicts as build_sections
    gives them]. Corners without a window or a complex are left out."""
    groups = {}
    for k in corners:
        if k.get('d0') is None or k.get('complex') is None:
            continue
        groups.setdefault(k['complex'], []).append(k)
    sections = []
    for n in sorted(groups):
        cs = sorted(groups[n], key=lambda x: x['d'])
        sections.append({'corners': cs, 'd0': min(x['d0'] for x in cs), 'd1': max(x['d1'] for x in cs),
                         'apex': min(cs, key=lambda x: x['min_speed'] if x.get('min_speed') is not None else 1e9)['d'],
                         'lead': cs[0], 'id': n, 'off': int(any(x.get('off') for x in cs))})
    return sections


def match_sections(sections, grid):
    """{index in `sections`: index in `grid`}: each section of this run
    with the section of the reference's grid whose yaw windows overlap by
    at least half of the shorter one, else whose apex is within
    MATCH_APEX m; the nearest apex wins, and a grid section is matched once."""
    pairs = []
    for i, s in enumerate(sections):
        for j, g in enumerate(grid):
            overlap = min(s['d1'], g['d1']) - max(s['d0'], g['d0'])
            shorter = min(s['d1'] - s['d0'], g['d1'] - g['d0'])
            near = abs(s['apex'] - g['apex'])
            if (shorter > 0 and overlap >= 0.5 * shorter) or (shorter <= 0 and overlap >= 0) or near <= MATCH_APEX:
                pairs.append((near, i, j))
    pairs.sort()
    out, used = {}, set()
    for near, i, j in pairs:
        if i in out or j in used:
            continue
        out[i] = j
        used.add(j)
    return out


def exit_end(trace, track, grid_section):
    """Where a section's exit ends on the reference run's own trace: where the reference has full
    throttle and is straight, past the section's yaw windows, at most EXIT_REACH m past them. The
    straight after it is not the corner's: a loss there is speed, not the exit."""
    reach = grid_section['d1'] + EXIT_REACH
    start = max(bisect.bisect_left(track, grid_section['d1']), 0)
    throttle, yaw = CH['throttle'], CH['yaw_rate']
    for i in range(start, len(trace)):
        if track[i] >= reach:
            break
        v, y = trace[i][throttle], trace[i][yaw]
        if _fin(v) and v >= THROTTLE_FULL and (not _fin(y) or abs(y) < EXIT_YAW):
            return max(track[i], grid_section['d1'])
    return reach


def section_loss(trace, sections, reference, unfinished=False):
    """Time lost against the reference run on its grid: sets
    `loss_entry` and `loss_exit` on the lead corner of each section of this
    run that matches a section of the reference's (and neither was off).
    `reference` is {'trace', 'corners' (stored rows), 'course'}. The loss
    is this run's time through the reference's section less the
    reference's own, split at this run's slowest point. Returns the sum of
    the matched sections' losses (negative: quicker than the reference).
    A run that did not finish (`unfinished`) is not timed through a section
    it did not cover to the end (END_SLACK m short at most): the one it
    stopped in, which holds the crash and not a corner."""
    ref_trace = reference['trace']
    grid = sections_of(reference['corners'])
    if not grid or not sections:
        return None
    ref_track, track = along(ref_trace), along(trace)
    # Both runs cover the stage from where they start to the line, give or take a few metres: the
    # first section is measured from where the later of them starts, the last to where the shorter stops
    limit, first = min(ref_track[-1], track[-1]), max(ref_track[0], track[0])
    bounds = section_bounds(grid, ref_track[0] if ref_track else 0.0, reference.get('course') or ref_track[-1])
    reach = [b for _, b in bounds]
    bounds = [(min(max(a, first), limit), min(max(b, first), limit)) for a, b in bounds]
    matches = match_sections(sections, grid)
    total = 0.0
    found = False
    for i, j in matches.items():
        s, g = sections[i], grid[j]
        if g.get('off') or any(k.get('off') for k in s['corners']):
            continue
        a, b = bounds[j]
        if unfinished and reach[j] > track[-1] + END_SLACK:
            continue
        apex = min(max(s['apex'], a), b)
        b = min(b, exit_end(ref_trace, ref_track, g))
        apex = min(apex, b)
        mine = [time_at(trace, track, x) for x in (a, apex, b)]
        theirs = [time_at(ref_trace, ref_track, x) for x in (a, apex, b)]
        if None in mine or None in theirs:
            continue
        entry = (mine[1] - mine[0]) - (theirs[1] - theirs[0])
        exit_ = (mine[2] - mine[1]) - (theirs[2] - theirs[1])
        s['lead']['loss_entry'], s['lead']['loss_exit'] = entry, exit_
        s['grid'] = j
        total += entry + exit_
        found = True
    return total if found else None


def _matched_corner(corner, corners):
    """The stored corner of `corners` that is the same bend as `corner`: the same direction (where both
    have one), its slowest point within MATCH_CORNER m of its; the nearest, None where there is none."""
    best = None
    for k in corners:
        if k.get('d') is None or abs(k['d'] - corner['d']) > MATCH_CORNER:
            continue
        if corner.get('direction') and k.get('direction') and corner['direction'] != k['direction']:
            continue
        if best is None or abs(k['d'] - corner['d']) < abs(best['d'] - corner['d']):
            best = k
    return best


def spread(sections, others):
    """[event dict] per section of this run whose minimum speed can be
    compared: the spread of the speed at the section's slowest corner over
    this run and the other runs' `others` ([(run row, stored corners)],
    newest first; the runs to compare are the caller's choice), the last
    SPREAD_RUNS runs, at least SPREAD_MIN of them. The corner is matched
    run to run by its slowest point (MATCH_CORNER m, the same direction), not
    by section: how the corners were joined depends on each run's pace. A run
    whose matching corner was off, or has none, is left out, never the
    neighbour; so is a section an off touched, in this run. The spread is the
    median absolute deviation scaled to a standard deviation, which one odd
    run does not move."""
    out = []
    pool = [(run, [k for k in corners if k.get('d') is not None]) for run, corners in others[:SPREAD_RUNS - 1]]
    for s in sections:
        if any(k.get('off') for k in s['corners']):
            continue
        key = key_corner(s)
        if key.get('min_speed') is None:
            continue
        speeds = [key['min_speed']]
        for run, corners in pool:
            k = _matched_corner(key, corners)
            if k is None or k.get('off') or k.get('min_speed') is None or k.get('complex') is None:
                continue
            speeds.append(k['min_speed'])
        if len(speeds) >= SPREAD_MIN:
            median = statistics.median(speeds)
            mad = statistics.median(abs(v - median) for v in speeds)
            out.append({'kind': 'spread', 'class': None, 'd0': s['d0'], 'd1': s['d1'], 't0': None, 't1': None,
                        'gear': None, 'value': 1.4826 * mad,
                        'detail': {'median': median, 'min': min(speeds), 'max': max(speeds),
                                   'runs': len(speeds), 'apex': s['apex']}})
    return out


# -- 7.3 R6: the sections a run lost or gained time in, and why --

BRAKE_DELTA = 10.0               # m: a braking point further than this from the reference's is earlier or later (calibrate)
SPEED_DELTA = 3.0 / 3.6          # m/s (3 km/h): a speed this far from the reference's is lower or higher (calibrate)
THROTTLE_DELTA = 0.2             # s: the throttle this much later than the reference's (calibrate)
COUNTER_DELTA = 0.15             # share of a corner steered against the yaw, more than the reference's: over-rotated
SECTION_MIN = 0.1                # s a section must lose, or gain, to be named
END_SLACK = 15.0                 # m a run may stop short of the grid's last section and still be timed through it
EXIT_REACH = 150.0               # m past a section's yaw window that its exit is timed to at most (the rest is a straight)


def key_corner(section):
    """The corner a section is named and judged by: its slowest."""
    return next((k for k in section['corners'] if k['d'] == section['apex']), section['corners'][0])


def section_name(section):
    """'the 3 left at 1.8 km' for a corner, 'the left-right at 2.1 km' for a
    complex (the call of its corners until the pace notes arrive)."""
    cs = section['corners']
    if len(cs) == 1:
        return corner_name(cs[0])
    sides = ['left' if (k.get('direction') or 0) > 0 else 'right' for k in cs]
    where = ' at {:.1f} km'.format(section['apex'] / 1000.0)
    if len(cs) <= 3:
        return 'the {}{}'.format('-'.join(sides), where)
    return 'the {}-corner section{}'.format(len(cs), where)


def _diff(a, b):
    return None if a is None or b is None else a - b


def compare_section(section, grid_section):
    """This run's section against the reference's, as differences (this less
    the reference's; None where either is missing): `brake` (m before the
    slowest point of the lead corner's strongest braking: positive is
    earlier), `speed` (the minimum, m/s), `exit` (speed leaving the last
    corner), `entry` (speed entering the first), `throttle` (s from the
    slowest point to the throttle past 0.2), `counter` (share steered against
    the yaw), `coast_entry`, plus this run's `overlap_exit` and the two key
    corners."""
    a, b = key_corner(section), key_corner(grid_section)
    lead, ref_lead = section['corners'][0], grid_section['corners'][0]
    last, ref_last = section['corners'][-1], grid_section['corners'][-1]
    return {'brake': _diff(lead.get('brake_d'), ref_lead.get('brake_d')),
            'speed': _diff(a.get('min_speed'), b.get('min_speed')),
            'exit': _diff(last.get('exit_speed'), ref_last.get('exit_speed')),
            'entry': _diff(lead.get('entry_speed'), ref_lead.get('entry_speed')),
            'throttle': _diff(a.get('throttle_on_t'), b.get('throttle_on_t')),
            'counter': _diff(a.get('counter_steer'), b.get('counter_steer')),
            'coast_entry': _diff(a.get('coast_entry'), b.get('coast_entry')),
            'overlap_exit': a.get('overlap_exit'), 'this': a, 'ref': b}


def section_pattern(c, loss):
    """What a section's numbers say against the reference (`c`: compare_section),
    given its time `loss` (negative: quicker than the reference). For a loss:
    - `over-rotated`: a lower minimum with more steering against the yaw;
    - `over-slowing`: braked earlier, a lower minimum;
    - `under-committed`: the same braking point, a lower minimum and a slower exit;
    - `overdriven`: braked later or entered faster, a slower exit;
    - `late-throttle`: the same minimum, the throttle later;
    - `slower`: a lower minimum, none of the above;
    - `unclear`: none of them.
    For a gain: `right-entry` (braked earlier, the same minimum, a faster
    exit) or `quicker`. None where the loss is under SECTION_MIN."""
    if loss is None or abs(loss) < SECTION_MIN:
        return None
    brake, speed, exit_ = c['brake'], c['speed'], c['exit']
    low = speed is not None and speed <= -SPEED_DELTA
    slow = exit_ is not None and exit_ <= -SPEED_DELTA
    fast = exit_ is not None and exit_ >= SPEED_DELTA
    early = brake is not None and brake >= BRAKE_DELTA
    later = brake is not None and brake <= -BRAKE_DELTA
    same = brake is not None and abs(brake) < BRAKE_DELTA
    if loss < 0:
        return 'right-entry' if early and not low and fast else 'quicker'
    if low and c['counter'] is not None and c['counter'] >= COUNTER_DELTA:
        return 'over-rotated'
    if early and low:
        return 'over-slowing'
    if same and low and slow:
        return 'under-committed'
    if (later or (c['entry'] is not None and c['entry'] >= SPEED_DELTA)) and slow:
        return 'overdriven'
    if not low and c['throttle'] is not None and c['throttle'] >= THROTTLE_DELTA:
        return 'late-throttle'
    return 'slower' if low else 'unclear'


def section_report(corners, ref_corners):
    """[dict] per section of a run (its stored corner rows), in order, set
    against the reference run's (its stored corner rows): `name`, `d` (the
    apex), `section`, `off`, `loss` (entry and exit loss stored when the run
    ended, None where it had none), `entry`, `exit`, `ref` (the matched
    section of the reference), `compare` (compare_section) and `pattern`
    (section_pattern); `first` marks the section of the stage's start, whose
    time holds the launch (R3 judges that, not a corner), `last` the section the stage ends in (its time holds
    the finish, not a corner) and `ref_off` that the matched reference section had an off in it."""
    sections = sections_of(corners)
    grid = sections_of(ref_corners)
    matches = match_sections(sections, grid) if grid else {}
    out = []
    for i, s in enumerate(sections):
        lead = s['lead']
        entry, exit_ = lead.get('loss_entry'), lead.get('loss_exit')
        item = {'name': section_name(s), 'd': s['apex'], 'section': s, 'off': s['off'], 'entry': entry,
                'exit': exit_, 'loss': entry + exit_ if entry is not None and exit_ is not None else None,
                'ref': None, 'compare': None, 'pattern': None, 'first': i == 0 or matches.get(i) == 0,
                'last': i == len(sections) - 1, 'ref_off': False}
        if i in matches:
            g = grid[matches[i]]
            item['ref'] = g
            item['ref_off'] = bool(g['off'])
            item['compare'] = compare_section(s, g)
            if item['loss'] is not None and not s['off']:
                item['pattern'] = section_pattern(item['compare'], item['loss'])
        out.append(item)
    return out


def grid_of(reference):
    """(sections, bounds) of the reference run's grid: its sections rebuilt
    from the stored corners, and each one's distances (section_bounds), not
    clipped to any other run."""
    grid = sections_of(reference['corners'])
    if not grid:
        return [], []
    ref_track = along(reference['trace'])
    return grid, section_bounds(grid, ref_track[0] if ref_track else 0.0,
                                reference.get('course') or (ref_track[-1] if ref_track else 0.0))


def grid_times(trace, corners, grid, bounds):
    """{grid index: seconds} this run took through each section of the
    reference's grid that its own sections match, over the grid's distances,
    so the times of any two runs compare. The first section (it holds the
    launch), a section an off touched (in the run or the reference), one the
    run did not cover from end to end (a run may stop END_SLACK m short of
    the stage's end), or one the trace cannot time is left out."""
    track = along(trace)
    if not track:
        return {}
    sections = sections_of(corners)
    out = {}
    for i, j in match_sections(sections, grid).items():
        if j == 0 or sections[i]['off'] or grid[j]['off']:
            continue
        a, b = bounds[j]
        if a < track[0] or b > track[-1] + END_SLACK:
            continue
        start, end = time_at(trace, track, a), time_at(trace, track, min(b, track[-1]))
        if start is not None and end is not None:
            out[j] = end - start
    return out


def stitched(reference, runs):
    """The best time through each section of the reference's grid over the
    reference and `runs` ([{'run', 'trace', 'corners'}]): `base` (the
    reference's), `best`, `who` (the run id that set it), `gain` (base less
    best, per section) and `total` (what the best sections together beat the
    reference by: the stage's "possible" time is the reference's less it).
    None without a grid."""
    grid, bounds = grid_of(reference)
    if not grid:
        return None
    base = grid_times(reference['trace'], reference['corners'], grid, bounds)
    best, who = dict(base), {j: reference.get('run') for j in base}
    for run in runs:
        for j, t in grid_times(run['trace'], run['corners'], grid, bounds).items():
            if j in best and t < best[j]:
                best[j], who[j] = t, run['run']
    gain = {j: base[j] - best[j] for j in base}
    return {'grid': grid, 'bounds': bounds, 'base': base, 'best': best, 'who': who, 'gain': gain,
            'total': sum(gain.values())}


def held_vs_reference(trace, reference_trace, d0):
    """For a limiter episode that began at distance `d0`: how much longer
    this run took from there to its next braking than the reference did over
    the same distance (seconds), and the gear the reference was in at `d0`
    (None where it cannot be told). None when the run did not brake again."""
    track, ref_track = along(trace), along(reference_trace)
    i = bisect.bisect_left(track, d0)
    stop = next((m for m in range(i, len(trace)) if _fin(trace[m][CH['brake']]) and trace[m][CH['brake']] > 0.2), None)
    if stop is None:
        return None
    d1 = track[stop]
    mine = (time_at(trace, track, d0), time_at(trace, track, d1))
    theirs = (time_at(reference_trace, ref_track, d0), time_at(reference_trace, ref_track, d1))
    if None in mine or None in theirs:
        return None
    r = min(bisect.bisect_left(ref_track, d0), len(reference_trace) - 1)
    gear = reference_trace[r][CH['gear']]
    return {'lost': (mine[1] - mine[0]) - (theirs[1] - theirs[0]), 'd1': d1,
            'ref_gear': int(gear) if _fin(gear) else None}


# -- 7.3 R2: limiter episodes --

def limiter_episodes(trace, limiter, top, corners=(), slip=None):
    """The limiter episodes of a run: dicts (kind 'limiter', class, d0, d1,
    t0, t1, gear, value (seconds), detail). An episode is a stretch of rows
    with the revs at LIMITER_BAND of the limiter or more, the throttle full,
    one gear and that gear below `top`. Classes, the first that fits:
    - `crawl`: slower than CRAWL m/s on average (stuck, or the start);
    - `spin`: wheelspin on the cut (slip_rpm over SPIN_SLIP on most rows);
    - `up-down`: a change up, then down again within UP_DOWN s;
    - `shift`: ended by a change up within SHIFT_AFTER s;
    - `held-corner`: ended by the brake, a change down, inside a yaw
      window, or with the next corner within NEXT_CORNER m or
      NEXT_CORNER_TIME s;
    - `held-straight`: HELD s or HELD_DISTANCE m on the cut with none of
      the above: the engine makes nothing there and nothing needed it;
    - `touch`: anything shorter."""
    out = []
    if not limiter or not top or not trace:
        return out
    t, speed, rpm, gear, throttle, brake = CH['t'], CH['speed'], CH['rpm'], CH['gear'], CH['throttle'], CH['brake']
    track = along(trace)
    n = len(trace)
    times = [row[t] for row in trace]

    def on_cut(row):
        return (_fin(row[rpm]) and _fin(row[throttle]) and _fin(row[gear]) and row[rpm] >= limiter * LIMITER_BAND
                and row[throttle] >= FULL_THROTTLE and 1 <= row[gear] < top)
    i = 0
    while i < n:
        if not on_cut(trace[i]):
            i += 1
            continue
        j = i
        while j + 1 < n and on_cut(trace[j + 1]) and trace[j + 1][gear] == trace[i][gear]:
            j += 1
        g = int(trace[i][gear])
        t0, t1 = trace[i][t], trace[j][t] + _dt(trace, j)
        d0, d1 = track[i], track[j]
        duration = t1 - t0
        speeds = [trace[m][speed] for m in range(i, j + 1) if _fin(trace[m][speed])]
        mean_speed = sum(speeds) / len(speeds) if speeds else 0.0
        # What follows
        soon = range(j + 1, bisect.bisect_right(times, trace[j][t] + SHIFT_AFTER))
        up = next((m for m in soon if _fin(trace[m][gear]) and trace[m][gear] > g), None)
        down_after_up = up is not None and any(
            _fin(trace[m][gear]) and 1 <= trace[m][gear] <= g
            for m in range(up, bisect.bisect_right(times, trace[up][t] + UP_DOWN)))
        down = next((m for m in soon if _fin(trace[m][gear]) and 1 <= trace[m][gear] < g), None)
        braked = any(_fin(trace[m][brake]) and trace[m][brake] > 0.2 for m in soon)
        inside = next((k for k in corners if k.get('d0') is not None and k['d0'] <= d1 and d0 <= k['d1']), None)
        ahead = next((k for k in sorted(corners, key=lambda x: x['d0']) if k.get('d0') is not None and k['d0'] > d1),
                     None)
        near = ahead is not None and (ahead['d0'] - d1 < NEXT_CORNER
                                      or (mean_speed > 0 and (ahead['d0'] - d1) / mean_speed < NEXT_CORNER_TIME))
        spun = None
        if slip is not None:
            vals = [slip[m] for m in range(i, j + 1) if slip[m] is not None]
            spun = (sum(1 for v in vals if v > SPIN_SLIP) > 0.5 * len(vals)) if vals else None
        if mean_speed < CRAWL:
            klass = 'crawl'
        elif spun:
            klass = 'spin'
        elif up is not None and down_after_up:
            klass = 'up-down'
        elif up is not None:
            klass = 'shift'
        elif braked or down is not None or inside is not None or near:
            klass = 'held-corner'
        elif duration >= HELD or d1 - d0 >= HELD_DISTANCE:
            klass = 'held-straight'
        else:
            klass = 'touch'
        out.append({'kind': 'limiter', 'class': klass, 'd0': d0, 'd1': d1, 't0': t0, 't1': t1, 'gear': g,
                    'value': duration,
                    'detail': {'speed': mean_speed, 'next_corner_m': (ahead['d0'] - d1) if ahead else None}})
        i = j + 1
    return out


def held_seconds(episodes):
    """Seconds of `held-straight` limiter episodes."""
    return sum(e['value'] for e in episodes if e['class'] == 'held-straight')


# -- 7.3 R3: the launch --

def launch_outcome(summary, trace, slip=None, history=(), limiter=None, surface=None):
    """What the launch did, or None when there was none to judge: a dict
    with `gear` (held at the release), `g` (mean a_long over the first
    LAUNCH_WINDOW s, in g), `t50`, `spin` (the most slip_rpm over those
    rows), `stall`, `bog`, `game` (the game's auto-clutch: the rig clutch
    never rose above CLUTCH_DOWN while standing) and `dropped` (the run was
    restarted within LAUNCH_ABORT s). `history` are the last launches' t50
    on this surface: a bog is `g` under LAUNCH_G, or a time to 50 km/h more
    than LAUNCH_SLOW over their median (3 or more); on a loose surface (`surface`
    in LAUNCH_LOOSE, or mixed) the car's own history is the only judge, as the g
    of a launch there says what the surface and the drivetrain allow. The revs are read only
    in the launch gear, from the release to the first change: a drop of
    them is not a bog. `cut`: the seconds of that stretch the revs sat on
    the limiter (`limiter`, LIMITER_BAND of it), None where it is not
    known: a change up as the cut comes in is the technique."""
    if (summary.get('launch') or 0.0) < LAUNCH_HELD or not trace:
        return None
    t, speed, rpm, gear = CH['t'], CH['speed'], CH['rpm'], CH['gear']
    first = trace[0]
    g0 = int(first[gear]) if _fin(first[gear]) and first[gear] >= 1 else None
    out = {'gear': g0, 'dropped': False, 'game': False, 'g': None, 't50': None, 'spin': None, 'stall': None,
           'bog': None, 'cut': None}
    if summary.get('finished') == 0 and (summary.get('duration') or 0.0) < LAUNCH_ABORT:
        out['dropped'] = True
        return out
    release = summary.get('release') or 0.0
    reached = next((row[t] for row in trace if row[speed] >= 50 / 3.6), None)
    if reached is not None and reached <= 20.0:
        out['t50'] = release + reached
    window = [i for i, row in enumerate(trace) if row[t] - first[t] <= LAUNCH_WINDOW
              and (g0 is None or row[gear] == g0)]
    accel = [trace[i][CH['a_long']] for i in window if _fin(trace[i][CH['a_long']])]
    if accel:
        out['g'] = sum(accel) / len(accel) / G
    if slip is not None:
        spins = [slip[i] for i in window if slip[i] is not None]
        if spins:
            out['spin'] = max(spins)
    if g0 is not None:
        held = []
        for row in trace:
            if row[t] - first[t] > 2.0 or row[gear] != g0:
                break
            if _fin(row[rpm]):
                held.append(row[rpm])
        if held and summary.get('launch_rpm'):
            out['stall'] = float(min(held) < STALL)
        if limiter:
            on_cut = 0.0
            for i, row in enumerate(trace):
                if row[t] - first[t] > LAUNCH_CUT_WINDOW or row[gear] != g0:
                    break
                if _fin(row[rpm]) and row[rpm] >= limiter * LIMITER_BAND:
                    on_cut += _dt(trace, i)
            out['cut'] = on_cut
    clutch = summary.get('launch_clutch')
    out['game'] = clutch is not None and clutch <= CLUTCH_DOWN
    slow = None
    if len(history) >= 3 and out['t50'] is not None:
        slow = out['t50'] > statistics.median(list(history)[:LAUNCH_HISTORY]) + LAUNCH_SLOW
    if out['g'] is not None or slow is not None:
        loose = surface in LAUNCH_LOOSE or (surface or '').startswith('mixed:')
        out['bog'] = float((out['g'] is not None and out['g'] < LAUNCH_G and not loose) or bool(slow))
    return out


# -- 7.3 R1: the change-up band --

def shift_band(context, gear, method=None):
    """(low, high, crossover) rpm of the band a flat-out change up from
    `gear` is judged against, or None where nothing is known. The low end
    is the game's lights (`shift`); where the gear is measured
    grip-limited on the surface (GRIP_PULLS or more pulls, a share of the
    engine's drive of at most GRIP_SHARE_MAX, never 3rd or higher on
    tarmac) it is the lowered best if lower. The high end is the smaller of
    the lights' `late` and the limiter less a margin (150 rpm on a
    sequential, 300 on an H-pattern). Where the game ships no lights the
    band runs from the crossover (best_for) to the limiter less the margin.
    `crossover` is the engine's best, for the drive lost."""
    limiter = context.get('limiter')
    margin = MARGIN.get(method, MARGIN_DEFAULT)
    lights = context.get('lights') or {}
    best_for = context.get('best_for')
    best = best_for(gear) if best_for else None
    if best is not None and best['coverage'] < SHIFT_COVERAGE:
        best = None
    surface = context.get('surface')
    lowered = None
    if best is not None and best.get('grip_limited'):
        pulls = context['pulls'](gear) if context.get('pulls') else 0
        share = best.get('grip_share')
        if (pulls >= GRIP_PULLS and share is not None and share <= GRIP_SHARE_MAX
                and not (gear >= 3 and surface == 'tarmac')):
            lowered = best['rpm']
    if lights.get('shift'):
        low = float(lights['shift'])
        high = float(lights['late']) if lights.get('late') else (limiter - margin if limiter else None)
        if lowered is not None:
            low = min(low, lowered)
    elif best is not None:
        low = lowered if lowered is not None else best['engine_rpm']
        high = limiter - margin if limiter else None
    else:
        return None
    if limiter:
        high = min(high, limiter - margin) if high is not None else limiter - margin
    if high is None or high < low:
        high = low
    crossover = best['engine_rpm'] if best is not None else None
    return low, high, crossover


def classify_shift(shift, band, limiter, cut=False):
    """'early', 'on', 'late' or 'cut' of a flat-out change up by one
    against its band (shift_band); None for a change that is not one. `cut`:
    a limiter episode ended in this change. A peak at 0.985 x the limiter
    or more is a cut as well. 'launch' flagged changes belong to the
    launch and are 'launch'."""
    if shift.get('direction') != 'up' or shift.get('gear_to') != shift.get('gear', 0) + 1 or not shift.get('flat_out'):
        return None
    if 'launch' in (shift.get('flags') or '').split(','):
        return 'launch'
    rpm = shift['rpm']
    if cut or (limiter and rpm >= limiter * LIMITER_BAND):
        return 'cut'
    if band is None:
        return None
    low, high, _ = band
    if rpm < low - EARLY_BELOW:
        return 'early'
    if rpm > high + BAND_SLACK:
        return 'late'
    return 'on'


def shift_costs(trace, shifts):
    """{shift key: seconds} an `early` or `cut` change cost on the stage:
    the speed deficit over the speed, times the time to the next braking.
    The deficit is the acceleration at the change times the drive lost for
    as long as it lasted: for an early change the share of the drive the
    next gear gives less at that rpm (`drive_loss`, DRIVE_LOSS where the car
    data does not say) over the time the revs take to climb to the band's
    low end at the rate they were climbing (the trace's, else CLIMB_DEFAULT
    rpm/s); for a cut all of it for the touch on the limiter less the gap
    of a change made well (CUT_NORMAL). `shifts` are dicts with `class`, `t`
    (trace time), `rpm`, `band`, `touch` (seconds on the cut), `drive_loss`
    and `key`. A change with no trace around it costs nothing."""
    out = {}
    t, speed, rpm, brake = CH['t'], CH['speed'], CH['rpm'], CH['brake']
    times = [row[t] for row in trace]
    for s in shifts:
        klass = s.get('class')
        if klass not in ('early', 'cut') or s.get('t') is None:
            continue
        i = min(bisect.bisect_left(times, s['t']), len(trace) - 1)
        before = [m for m in range(max(0, i - 6), i + 1) if _fin(trace[m][rpm]) and _fin(trace[m][speed])]
        if len(before) < 2:
            continue
        span = trace[before[-1]][t] - trace[before[0]][t]
        v = trace[before[-1]][speed]
        if span <= 0 or v <= 1.0:
            continue
        accel = max(0.0, (trace[before[-1]][speed] - trace[before[0]][speed]) / span)
        accel = accel if accel > 0.5 else ACCEL_DEFAULT
        if klass == 'early':
            climb = (trace[before[-1]][rpm] - trace[before[0]][rpm]) / span
            climb = climb if climb > 100.0 else CLIMB_DEFAULT
            low = s['band'][0] if s.get('band') else s['rpm']
            lasted = max(0.0, low - s['rpm']) / climb
            loss = s.get('drive_loss')
            deficit = accel * (loss if loss is not None else DRIVE_LOSS) * lasted
        else:
            deficit = accel * max(0.0, (s.get('touch') or 0.0) - CUT_NORMAL)
        braking = next((m for m in range(i, len(trace)) if _fin(trace[m][brake]) and trace[m][brake] > 0.2), None)
        ahead = (trace[braking][t] - s['t']) if braking is not None else (times[-1] - s['t'])
        ahead = max(0.0, min(ahead, LOOKAHEAD))
        out[s['key']] = deficit / v * ahead
    return out


# -- everything at once --

def _episode_for(episodes, gear, t):
    """The `shift` or `up-down` limiter episode in `gear` that ended at the
    change at trace time `t` (within SHIFT_AFTER s before it)."""
    return next((e for e in episodes if e['class'] in ('shift', 'up-down') and e['gear'] == gear
                 and -0.3 <= t - e['t1'] <= SHIFT_AFTER + 0.3), None)


def analyse(summary, rows, corners, shifts, context, started=None, reference=None, history=(), others=(),
            last_started=None, stage_length=None):
    """Everything the context layer says about a run, for the metrics and
    for the database: the caller (drive_log._write_end, the backfill) has
    cut `rows` to the stage (stage_rows) and found `corners`
    (drive_log.find_corners). `shifts` are the run's changes of gear (store
    rows: at, id, flags...) and `started` its wall start, which puts them on
    the trace's clock. `reference` ({'trace', 'corners', 'course'}), `history`
    (the last launches' time to 50 km/h on this surface), `others` (the car's
    other runs on the stage as [(run row, stored corners)]) and
    `last_started` (when the car last drove the stage) come from the store.

    Returns a dict:
    - `corners`: the corners with their phases, sections and losses filled in;
    - `sections`, `events` (everything for the events table), `run_class`;
    - `episodes` (the limiter's), `launch` (launch_outcome), `slip`;
    - `shifts`: the changes with `class`, `band`, `cost`, `t`, `d`, and flags
      (a `cut`, the first change up of the launch as `launch`, the block
      change down no longer a `skip`);
    - `loss`: the time lost to the reference, or None."""
    course = summary.get('course')
    slip = slip_rpm(rows, context.get('ratio'))
    sections = build_sections(rows, corners)
    out_shifts = []
    for s in shifts:
        s = dict(s)
        s['t'] = (s['at'] - started) if started is not None and s.get('at') is not None else None
        out_shifts.append(s)
    downs = [s['t'] for s in out_shifts if s.get('method') == 'h-pattern' and s.get('direction') == 'down'
             and s['t'] is not None]
    describe_corners(rows, corners, sections, downs)
    length = stage_length or summary.get('stage_length')
    unfinished = summary.get('finished') == 0 or (
        summary.get('finished') is None and bool(length) and (course or 0.0) < FINISH_SHARE * length)
    events = incidents(rows, corners, course, unfinished, summary.get('game'))
    mark_off(corners, events)
    klass = run_class(summary.get('finished'), course, length, events, started, last_started)
    top = summary.get('gears') or context.get('shipped_top') or context.get('top_gear')
    episodes = limiter_episodes(rows, context.get('limiter'), top, corners, slip)
    launch = launch_outcome(summary, rows, slip, history, context.get('limiter'), context.get('surface'))
    loss = section_loss(rows, sections, reference, unfinished) if reference is not None else None
    if reference is not None:
        # The same corner has the same call in every run: the reference's, not what this run's line made of it
        for k in corners:
            same = _matched_corner(k, reference['corners']) if k.get('d') is not None else None
            if same is not None and same.get('tightness'):
                k['tightness'] = same['tightness']
    # The changes of gear
    limiter = context.get('limiter')
    brake, t_col = CH['brake'], CH['t']
    first_up = True
    for key, s in enumerate(out_shifts):
        s['key'] = key
        flags = [f for f in (s.get('flags') or '').split(',') if f]
        t = s['t']
        s['d'] = distance_at(rows, t) if t is not None else None
        if s.get('direction') == 'down' and 'skip' in flags and t is not None:
            near = [row[brake] for row in rows if abs(row[t_col] - t) <= 0.3 and _fin(row[brake])]
            if near and max(near) >= SKIP_BRAKE:
                flags.remove('skip')                     # a block change down under braking
        episode = _episode_for(episodes, s['gear'], t) if t is not None and s.get('direction') == 'up' else None
        if (s.get('direction') == 'up' and s.get('gear_to') == s['gear'] + 1 and t is not None and first_up
                and launch is not None and t <= LAUNCH_SHIFT):
            flags.append('launch')
        if s.get('direction') == 'up':
            first_up = False
        s['flags'] = ','.join(flags) or None
        s['band'] = shift_band(context, s['gear'], s.get('method')) if s.get('direction') == 'up' else None
        s['class'] = classify_shift(s, s['band'], limiter, cut=episode is not None)
        s['touch'] = episode['value'] if episode is not None else None
        drive_loss = context.get('drive_loss')
        s['drive_loss'] = (drive_loss(s['gear'], s['rpm']) if drive_loss is not None and s.get('direction') == 'up'
                           else None)
        if s['class'] == 'cut' and 'cut' not in flags:
            flags.append('cut')
            s['flags'] = ','.join(flags)
    costs = shift_costs(rows, out_shifts)
    for s in out_shifts:
        s['cost'] = costs.get(s['key'])
    spreads = spread(sections, others) if klass in ('clean', 'learning', 'off') and not unfinished else []
    all_events = events + episodes + spreads
    if launch is not None and not launch['dropped']:
        all_events.append({'kind': 'launch', 'class': 'game' if launch['game'] else 'driver', 'd0': None, 'd1': None,
                           't0': rows[0][t_col] if rows else None, 't1': None, 'gear': launch['gear'],
                           'value': launch['t50'], 'detail': {k: launch[k] for k in ('g', 'spin', 'stall', 'bog', 'cut')}})
    return {'corners': corners, 'sections': sections, 'events': all_events, 'run_class': klass,
            'episodes': episodes, 'launch': launch, 'slip': slip, 'shifts': out_shifts, 'loss': loss,
            'spreads': spreads}
