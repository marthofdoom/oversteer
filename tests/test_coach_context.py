"""The coach's context layer: what a run's trace says about where and why
(docs/coach-techniques.md, section 7.2), on synthetic traces, one per class
boundary."""
import math

from oversteer import coach_context as cc
from oversteer.drive_log import find_corners
from oversteer.telemetry_store import TRACE_CHANNELS

NAN = float('nan')
C = {name: i for i, name in enumerate(TRACE_CHANNELS)}
G = 9.80665


def rows(n, dt=0.1, **channels):
    """A trace of n rows at 10 Hz: each channel a number or a function of
    (row, time); NaN where not given; distance integrated from speed unless
    given."""
    out, d = [], 0.0
    for i in range(n):
        t = i * dt
        row = {'t': t}
        for name, value in channels.items():
            row[name] = value(i, t) if callable(value) else value
        if 'distance' not in row:
            row['distance'] = d
            d += row.get('speed', 0.0) * dt
        out.append(tuple(float(row.get(name, NAN)) for name in TRACE_CHANNELS))
    return out


def stage(points, corners=(), dt=0.1, gear=3.0, rpm=None, throttle=None, brake=None):
    """A run along a road: `points` [(distance, speed)] linear between;
    `corners` [(d0, d1, sign, peak yaw)] windows of yaw; the pedals follow
    the acceleration unless given (a function of row, time, distance, a)."""
    out, d, t = [], points[0][0], 0.0
    ds = [p[0] for p in points]

    def speed_at(x):
        for (d0, v0), (d1, v1) in zip(points, points[1:]):
            if d0 <= x <= d1:
                return v0 + (v1 - v0) * (x - d0) / (d1 - d0) if d1 > d0 else v1
        return points[-1][1]
    last = speed_at(d)
    while d < ds[-1] - 1e-6:
        v = speed_at(d)
        a = (v - last) / dt if out else 0.0
        yaw = 0.0
        for d0, d1, sign, peak in corners:
            if d0 <= d <= d1:
                yaw = sign * peak * math.sin(math.pi * (d - d0) / (d1 - d0))
        row = {'t': t, 'distance': d, 'speed': v, 'a_long': a, 'a_lat': v * yaw, 'yaw_rate': yaw,
               'gear': gear(v) if callable(gear) else gear,
               'throttle': (throttle(len(out), t, d, a) if throttle else (1.0 if a > 0.3 else (0.0 if a < -1.0 else 0.3))),
               'brake': (brake(len(out), t, d, a) if brake else (0.7 if a < -1.0 else 0.0)),
               'steer': 0.0, 'clutch': 0.0}
        row['rpm'] = rpm(len(out), t, d, v) if rpm else 4000.0 + 60.0 * v
        out.append(tuple(float(row.get(name, NAN)) for name in TRACE_CHANNELS))
        last = v
        d += max(v, 0.5) * dt
        t += dt
    return out


# -- the stage's rows --

def test_stage_rows_stop_at_the_finish():
    tr = rows(300, speed=20.0)                                # 600 m in 30 s
    cut = cc.stage_rows(tr, course=500.0)
    assert len(cut) == 251 and cut[-1][C['distance']] >= 500.0 > cut[-2][C['distance']]
    # A finished run: no row more than a second past the result time
    cut = cc.stage_rows(tr, course=None, finished=True, result_time=20.0)
    assert cut[-1][C['t']] <= 21.0 < tr[len(cut)][C['t']]
    assert cc.stage_rows(tr) is tr or cc.stage_rows(tr) == tr
    # standing after the line is not the stage
    parked = rows(100, speed=lambda i, t: 20.0 if i < 50 else 0.0)
    assert len(cc.stage_rows(parked, course=parked[50][C['distance']])) == 51


# -- incidents --

def test_a_slow_stretch_with_reverse_is_an_off_and_a_long_one_too():
    def speed(i, t):
        return 0.5 if 100 <= i < 125 else 20.0                 # 2.5 s standing, then away
    tr = rows(300, speed=speed, gear=lambda i, t: -1.0 if 105 <= i < 110 else 3.0)
    [event] = [e for e in cc.incidents(tr, course=tr[-1][C['distance']]) if e['kind'] == 'off']
    assert event['class'] == 'reverse'
    tr = rows(300, speed=lambda i, t: 0.5 if 100 <= i < 140 else 20.0, gear=3.0)
    [event] = [e for e in cc.incidents(tr, course=tr[-1][C['distance']]) if e['kind'] == 'off']
    assert event['class'] == 'long' and event['value'] >= 3.9


def test_a_slow_hairpin_is_a_stall_not_an_off():
    """The audit's 24 hairpins under 3 m/s: a corner fault, not an off."""
    def v(i, t):                                        # braked and accelerated over 15 rows each: 1.2 m/s a row
        return 2.0 if 100 <= i < 115 else (20.0 - 1.2 * (i - 85) if 85 <= i < 100 else
                                           (2.0 + 1.2 * (i - 114) if 115 <= i < 130 else 20.0))
    tr = rows(300, speed=v, gear=3.0)
    d0, d1 = tr[95][C['distance']], tr[120][C['distance']]
    corner = {'d0': d0, 'd1': d1}
    events = cc.incidents(tr, [corner], course=tr[-1][C['distance']])
    assert [e['kind'] for e in events] == ['stall']
    # the same stretch away from any corner is a stop
    assert [e['kind'] for e in cc.incidents(tr, [], course=tr[-1][C['distance']])] == ['stop']


def test_a_hit_before_a_slow_stretch_is_an_off():
    def a_long(i, t):
        return -4 * G if i in (95, 96) else 0.0
    tr = rows(300, speed=lambda i, t: 0.5 if 100 <= i < 115 else 20.0, a_long=a_long, gear=3.0)
    events = cc.incidents(tr, course=tr[-1][C['distance']])
    assert {e['kind'] for e in events} == {'hit', 'off'}
    off = [e for e in events if e['kind'] == 'off'][0]
    assert off['class'] == 'hit'


def test_the_start_and_the_finish_are_not_incidents():
    tr = rows(300, speed=lambda i, t: 0.5 if i < 15 or i >= 285 else 20.0, gear=3.0)
    assert not [e for e in cc.incidents(tr, course=tr[-1][C['distance']]) if e['kind'] in ('off', 'stop', 'stall')]


def test_a_turn_through_more_than_a_hairpin_is_a_spin_and_marks_its_section():
    corner = {'d': 500.0, 'd0': 450.0, 'd1': 560.0, 'heading_change': 230.0, 'direction': 1, 'min_speed': 6.0,
              '_i0': 40, '_i1': 60, '_apex': 50}
    tr = rows(100, speed=10.0, yaw_rate=0.2)
    [spin] = cc.spins(tr, [corner])
    assert spin['kind'] == 'spin'
    near, far = dict(d=520.0, d0=500.0, d1=540.0, complex=0, min_speed=8.0), dict(d=2000.0, d0=1950.0, d1=2050.0,
                                                                                  complex=1, min_speed=8.0)
    cc.mark_off([near, far], [spin, {'kind': 'off', 'd0': 5000.0, 'd1': 5010.0}])
    assert (near['off'], far['off']) == (1, 0)
    # an off marks 100 m either side
    cc.mark_off([near, far], [{'kind': 'off', 'd0': 2120.0, 'd1': 2130.0}])
    assert (near['off'], far['off']) == (0, 1)


def test_a_hairpin_pendulum_is_not_a_spin_but_a_hard_reversal_is():
    def corner(heading=150.0):
        return {'d': 100.0, 'd0': 80.0, 'd1': 120.0, 'heading_change': heading, 'direction': 1, 'min_speed': 6.0,
                '_i0': 10, '_i1': 20, '_apex': 15}
    swing = rows(40, speed=10.0, yaw_rate=lambda i, t: -0.6 if 16 <= i <= 19 else 0.5)
    assert cc.spins(swing, [corner()]) == []
    spun = rows(40, speed=10.0, yaw_rate=lambda i, t: -2.0 if 16 <= i <= 19 else 0.5)
    assert [e['kind'] for e in cc.spins(spun, [corner()])] == ['spin']


# -- run class and reference --

def test_run_class():
    off = [{'kind': 'off'}]
    assert cc.run_class(0, 1200.0, 5000.0, []) == 'restart'
    assert cc.run_class(0, 2600.0, 5000.0, []) == 'partial'
    assert cc.run_class(0, 900.0, None, []) == 'restart' and cc.run_class(0, 3000.0, None, []) == 'partial'
    assert cc.run_class(1, 5000.0, 5000.0, off, started=1000.0, last_started=900.0) == 'off'
    assert cc.run_class(1, 5000.0, 5000.0, [{'kind': 'stall'}], started=1000.0, last_started=900.0) == 'clean'
    assert cc.run_class(1, 5000.0, 5000.0, [], started=1000.0, last_started=None) == 'learning'
    assert cc.run_class(1, 5000.0, 5000.0, [], started=1000.0 + 8 * 86400, last_started=1000.0) == 'learning'
    assert cc.run_class(1, 5000.0, 5000.0, [], started=1000.0 + 86400, last_started=1000.0) == 'clean'
    assert cc.run_class(None, 5000.0, None, []) == 'clean'                      # no stage: nothing to compare with


def test_the_reference_is_the_fastest_finished_run_in_the_same_conditions():
    runs = [{'id': 1, 'finished': 1, 'result_time': 230.0, 'wet': 'dry'},
            {'id': 2, 'finished': 1, 'result_time': 220.0, 'wet': 'wet'},
            {'id': 3, 'finished': 0, 'result_time': None, 'wet': 'dry'},
            {'id': 4, 'finished': 1, 'result_time': 225.0, 'wet': None}]
    assert cc.reference_run(runs, wet='dry')['id'] == 4          # the wet one is not a reference for a dry run
    assert cc.reference_run(runs, wet='wet')['id'] == 2
    assert cc.reference_run(runs, wet=None)['id'] == 2
    assert cc.reference_run([runs[2]]) is None
    # ranked without the first section, which holds the launch: a 38 s start is not a quick stage
    slow_start = [{'id': 1, 'finished': 1, 'result_time': 251.9, 'wet': 'dry', 'first_section': 38.0},
                  {'id': 2, 'finished': 1, 'result_time': 240.0, 'wet': 'dry', 'first_section': 5.0}]
    assert cc.reference_run(slow_start)['id'] == 1


# -- slip from the revs --

def slip_trace(zero_in_3rd=0.03, spin=0.0):
    """5000 rpm at 20 m/s in 3rd (ratio 250 rpm per m/s): partial throttle
    reads the zero, then the throttle floors and the wheels spin."""
    def rpm(i, t):
        base = 250.0 * 20.0 * (1.0 + zero_in_3rd)
        return base * (1.0 + (spin if i >= 60 else 0.0))
    return rows(100, speed=20.0, gear=3.0, rpm=rpm, throttle=lambda i, t: 1.0 if i >= 60 else 0.4, brake=0.0,
                clutch=0.0)


def test_slip_rpm_takes_the_gears_own_zero_out():
    ratio = {3: 250.0}.get
    slip = cc.slip_rpm(slip_trace(spin=0.2), ratio)
    assert abs(slip[30]) < 1e-6                                   # partial throttle: the zero, so nothing
    assert abs(slip[80] - (1.03 * 1.2 - 1.03)) < 1e-6             # the throttle floors: the wheels spin 20 % over the zero
    assert slip[0] is None                                        # the gear is not yet settled
    on, off = cc.slip_zeros(slip_trace(), ratio)
    assert abs(on[3] - 0.03) < 1e-6 and off == {}


def test_slip_rpm_is_off_where_the_ratio_is_wrong():
    ratio = {3: 250.0}.get
    slip = cc.slip_rpm(slip_trace(zero_in_3rd=0.09, spin=0.2), ratio)
    assert all(v is None for v in slip)                           # a 9 % zero is the wrong gearing, not slip
    on, off = cc.slip_zeros(slip_trace(zero_in_3rd=0.09), ratio)
    assert on == {} and abs(off[3] - 0.09) < 1e-6
    assert cc.slip_rpm(slip_trace(), None) == [None] * 100        # without the game's gearing
    pressed = cc.slip_rpm(rows(100, speed=20.0, gear=3.0, rpm=5000.0, throttle=0.4, brake=0.0, clutch=0.8), ratio)
    assert all(v is None for v in pressed)                        # the clutch is in


# -- the limiter: what follows decides --

LIMITER = 7500.0


def limiter_run(gap_to_corner=None, brake=False, gear_after=None, speed=22.0, seconds=2.0, spin=False):
    """A pull in 3rd of 6 s that reaches the limiter at 3 s and stays
    `seconds` on the cut; then what `gear_after`, `brake` and a corner
    `gap_to_corner` metres on say. Returns (trace, corners)."""
    n = 200
    start = 30
    end = start + int(seconds * 10)

    def rpm(i, t):
        return 7480.0 if start <= i < end else 6200.0

    def g(i, t):
        if gear_after is not None and i >= end:
            return float(gear_after) if not callable(gear_after) else gear_after(i)
        return 3.0

    def thr(i, t):
        return 1.0 if i < end else (0.3 if brake else 1.0)

    def brk(i, t):
        return 0.8 if brake and end <= i < end + 20 else 0.0
    tr = rows(n, speed=speed, rpm=rpm, gear=g, throttle=thr, brake=brk, clutch=0.0, yaw_rate=0.0, a_long=1.0)
    corners = []
    if gap_to_corner is not None:
        d_end = tr[end][C['distance']]
        corners = [{'d': d_end + gap_to_corner + 20, 'd0': d_end + gap_to_corner, 'd1': d_end + gap_to_corner + 40,
                    'min_speed': 12.0}]
    return tr, corners


def classes(tr, corners=(), top=5, slip=None):
    return [e['class'] for e in cc.limiter_episodes(tr, LIMITER, top, corners, slip)]


def test_the_limiter_between_corners_on_gravel_is_not_held_on_a_straight():
    """The owner's complaint: on gravel, a gear held on the limiter with the next corner coming is the gearing,
    not a fault; a long straight with nothing ahead is a fault."""
    tr, corners = limiter_run(gap_to_corner=60.0)                              # the corner is 60 m on
    assert classes(tr, corners) == ['held-corner']
    tr, corners = limiter_run(gap_to_corner=120.0, speed=15.0)                 # 8 s at 15 m/s: far, but...
    assert classes(tr, corners) == ['held-straight']                           # ...nothing near enough
    tr, corners = limiter_run(brake=True)
    assert classes(tr, corners) == ['held-corner']                             # ended by the brake
    tr, corners = limiter_run()
    assert classes(tr, corners) == ['held-straight']                           # a straight, nothing ahead
    assert cc.held_seconds(cc.limiter_episodes(tr, LIMITER, 5, corners)) >= 2.0


def test_a_missed_upshift_on_a_straight_is_held_straight_and_a_late_one_is_a_shift():
    tr, corners = limiter_run(gear_after=4)                                    # up to 4th as the cut ends
    assert classes(tr, corners) == ['shift']
    tr, corners = limiter_run(gear_after=lambda i: 4.0 if i < 80 else 3.0)       # 4th, then back to 3rd within 4 s
    assert classes(tr, corners) == ['up-down']
    tr, corners = limiter_run(seconds=0.6)                                     # a touch, then nothing: not coached
    assert classes(tr, corners) == ['touch']
    tr, corners = limiter_run(gear_after=2)                                    # a change down is the corner coming
    assert classes(tr, corners) == ['held-corner']


def test_a_crawl_and_wheelspin_are_not_gearing():
    tr, corners = limiter_run(speed=5.0)
    assert classes(tr, corners) == ['crawl']
    tr, corners = limiter_run()
    spinning = [0.4] * len(tr)
    assert classes(tr, corners, slip=spinning) == ['spin']
    assert classes(tr, corners, slip=[0.02] * len(tr)) == ['held-straight']
    assert classes(tr, corners, top=3) == []                                   # top gear is not below top gear
    assert cc.limiter_episodes(tr, None, 5) == [] and cc.limiter_episodes(tr, LIMITER, None) == []


def test_the_limiter_inside_a_corner_is_held_in_the_corner():
    tr, _ = limiter_run()
    d = tr[40][C['distance']]
    assert classes(tr, [{'d': d, 'd0': d - 20, 'd1': d + 20, 'min_speed': 12.0}]) == ['held-corner']
    # a held straight is still a straight when the corner behind it is behind
    behind = [{'d': 10.0, 'd0': 0.0, 'd1': 20.0, 'min_speed': 12.0}]
    assert classes(tr, behind) == ['held-straight']


# -- the launch --

def launch_trace(g=0.6, gear=1.0, t50=2.0):
    """The trace of a launch: from 3 m/s at `g`, 1st (or `gear`) held."""
    return rows(60, speed=lambda i, t: min(40.0, 3.0 + t * (50 / 3.6 - 3.0) / t50), gear=gear,
                rpm=lambda i, t: 6000.0 if i > 5 else 6500.0, a_long=g * G, throttle=1.0, brake=0.0, clutch=0.0)


def test_a_launch_is_judged_by_its_outcome():
    summary = {'launch': 1.0, 'release': 0.4, 'launch_rpm': 6500.0, 'finished': 1, 'duration': 200.0,
               'launch_clutch': 0.9}
    out = cc.launch_outcome(summary, launch_trace(g=0.6))
    assert out['gear'] == 1 and abs(out['g'] - 0.6) < 1e-6 and out['bog'] == 0.0
    assert abs(out['t50'] - (0.4 + 2.0)) < 0.15 and out['game'] is False
    assert cc.launch_outcome(summary, launch_trace(g=0.2))['bog'] == 1.0         # under 0.3 g
    # The revs falling is not a bog: this one drops from 6500 to 6000 and has all the g it needs
    assert cc.launch_outcome(summary, launch_trace(g=0.7))['bog'] == 0.0
    # slow against the median of the last launches
    slow = cc.launch_outcome(summary, launch_trace(g=0.5, t50=3.0), history=[2.4, 2.5, 2.6, 2.5])
    assert slow['bog'] == 1.0
    fine = cc.launch_outcome(summary, launch_trace(g=0.5, t50=2.2), history=[2.4, 2.5, 2.6, 2.5])
    assert fine['bog'] == 0.0
    # on snow or gravel the g says what the surface allows: only a slow time against the car's own launches is a bog
    for loose in ('snow', 'ice', 'gravel', 'mixed:gravel'):
        assert cc.launch_outcome(summary, launch_trace(g=0.2), surface=loose)['bog'] == 0.0
        assert cc.launch_outcome(summary, launch_trace(g=0.2, t50=3.0), history=[2.4, 2.5, 2.6, 2.5],
                                 surface=loose)['bog'] == 1.0
    assert cc.launch_outcome(summary, launch_trace(g=0.2), surface='tarmac')['bog'] == 1.0
    # a stall is the revs gone in the launch gear
    stalled = rows(40, speed=5.0, gear=1.0, rpm=lambda i, t: 6000.0 if i < 5 else 150.0, a_long=0.0)
    assert cc.launch_outcome(summary, stalled)['stall'] == 1.0


def test_the_games_launch_is_not_the_drivers_and_a_restart_drops_it():
    summary = {'launch': 1.0, 'release': 0.0, 'launch_rpm': 6500.0, 'finished': 1, 'duration': 200.0,
               'launch_clutch': 0.1}
    assert cc.launch_outcome(summary, launch_trace())['game'] is True            # the clutch never pressed
    assert cc.launch_outcome(dict(summary, launch_clutch=None), launch_trace())['game'] is False
    restarted = dict(summary, finished=0, duration=6.0)
    assert cc.launch_outcome(restarted, launch_trace())['dropped'] is True
    assert cc.launch_outcome(dict(summary, launch=0.2), launch_trace()) is None  # no launch to judge


def test_a_second_gear_start_is_a_launch_too():
    summary = {'launch': 1.0, 'release': 0.0, 'launch_rpm': 5000.0, 'finished': 1, 'duration': 200.0}
    out = cc.launch_outcome(summary, launch_trace(gear=2.0))
    assert out['gear'] == 2 and out['g'] is not None and out['bog'] == 0.0


def test_launch_spin_from_the_revs():
    summary = {'launch': 1.0, 'release': 0.0, 'launch_rpm': 6500.0, 'finished': 1, 'duration': 200.0}
    slip = [0.88] * 60
    assert cc.launch_outcome(summary, launch_trace(), slip)['spin'] == 0.88


# -- the change-up band --

def lights_context(surface='gravel', grip=None, pulls=10, lights=None, limiter=7500.0):
    best = {'rpm': 7450.0, 'engine_rpm': 7450.0, 'coverage': 1.0, 'source': 'game', 'grip_limited': False}
    if grip:
        best = dict(best, rpm=grip, grip_limited=True, grip_share=0.7)
    return {'limiter': limiter, 'surface': surface, 'lights': lights or {'shift': 6900.0, 'late': 7100.0},
            'best_for': lambda gear: best, 'pulls': lambda gear: pulls}


def test_the_band_is_the_games_lights_not_the_torque_crossover():
    low, high, crossover = cc.shift_band(lights_context(), 3, 'sequential')
    assert (low, high, crossover) == (6900.0, 7100.0, 7450.0)                   # the Fabia's band
    # the late light is capped by the limiter less the margin: 150 on a sequential, 300 on an H-pattern
    ctx = lights_context(lights={'shift': 7000.0, 'late': 7400.0})
    assert cc.shift_band(ctx, 3, 'sequential')[1] == 7350.0
    assert cc.shift_band(ctx, 3, 'h-pattern')[1] == 7200.0


def test_grip_lowers_the_band_only_when_it_is_measured_well():
    grip = lights_context(grip=5000.0)
    assert cc.shift_band(grip, 2)[0] == 5000.0                                   # measured grip-limited: lowered
    assert cc.shift_band(lights_context(grip=5000.0, pulls=5), 2)[0] == 6900.0     # too few pulls
    assert cc.shift_band(lights_context(grip=5000.0, surface='tarmac'), 3)[0] == 6900.0   # never 3rd up on tarmac
    assert cc.shift_band(lights_context(grip=5000.0, surface='tarmac'), 2)[0] == 5000.0
    odd = lights_context(grip=5000.0)
    was = odd['best_for']
    odd['best_for'] = lambda gear: dict(was(gear), grip_share=1.4)
    assert cc.shift_band(odd, 2)[0] == 6900.0                                    # an implausible share


def test_without_lights_the_band_is_from_the_crossover():
    ctx = lights_context()
    ctx['lights'] = None
    low, high, _ = cc.shift_band(ctx, 3, 'sequential')
    assert low == 7450.0 and high == 7450.0                                      # crossover at the limiter: a point
    ctx['best_for'] = lambda gear: None
    assert cc.shift_band(ctx, 3) is None


def shift(rpm, gear=3, flags=None, flat_out=1, direction='up'):
    return {'gear': gear, 'gear_to': gear + 1, 'direction': direction, 'rpm': rpm, 'flat_out': flat_out,
            'flags': flags}


def test_classify_shift():
    band = (6900.0, 7100.0, 7450.0)
    assert cc.classify_shift(shift(6000.0), band, 7500.0) == 'early'
    assert cc.classify_shift(shift(6850.0), band, 7500.0) == 'on'                  # 100 rpm slack
    assert cc.classify_shift(shift(7150.0), band, 7500.0) == 'on'
    assert cc.classify_shift(shift(7300.0), band, 7500.0) == 'late'
    assert cc.classify_shift(shift(7400.0), band, 7500.0) == 'cut'                 # 0.985 of the limiter
    assert cc.classify_shift(shift(7000.0), band, 7500.0, cut=True) == 'cut'       # an episode ended in this change
    assert cc.classify_shift(shift(7000.0, flags='launch'), band, 7500.0) == 'launch'
    assert cc.classify_shift(shift(7000.0, flat_out=0), band, 7500.0) is None
    assert cc.classify_shift(shift(7000.0, direction='down'), band, 7500.0) is None
    assert cc.classify_shift(shift(7000.0), None, 7500.0) is None                  # no band, not on the cut


def test_what_a_shift_costs_a_stage():
    tr = rows(200, speed=lambda i, t: 15.0 + 3.0 * t, rpm=lambda i, t: 5000.0 + 600.0 * (t % 4), gear=3.0,
              brake=lambda i, t: 0.8 if i > 150 else 0.0, throttle=1.0)
    early = {'class': 'early', 't': 5.0, 'rpm': 6000.0, 'band': (6900.0, 7100.0, None), 'key': 0, 'drive_loss': 0.05}
    on = {'class': 'on', 't': 5.0, 'rpm': 7000.0, 'band': (6900.0, 7100.0, None), 'key': 1}
    cut = {'class': 'cut', 't': 5.0, 'rpm': 7480.0, 'band': (6900.0, 7100.0, None), 'key': 2, 'touch': 0.4}
    costs = cc.shift_costs(tr, [early, on, cut])
    assert 1 not in costs and 0.005 < costs[0] < 0.2 and 0.02 < costs[2] < 0.5
    assert costs[2] > costs[0]
    # a cut no longer than a clean change costs nothing
    assert cc.shift_costs(tr, [dict(cut, touch=0.1)])[2] == 0.0
    assert cc.shift_costs(tr, [dict(early, t=None)]) == {}


# -- corners, sections, phases, loss --

def road(vmin=10.0, brake_from=430.0, second_brake=1000.0, vmin2=12.0, speed=28.0, throttle_late=0.0, slow_tail=None):
    """A run through two corners (at 500 m and 1100 m) 28 m/s between them:
    braking from `brake_from` into the first, away again; the second braked
    from `second_brake`. Returns the trace and its corners (find_corners)."""
    points = [(0.0, speed), (brake_from, speed), (500.0, vmin), (600.0, speed), (second_brake, speed),
              (1100.0, vmin2), (1200.0, speed), (1700.0, speed)]
    if slow_tail:                       # slow on the long straight after the second corner, from 1400 m
        points[-1:] = [(1400.0, speed), (1420.0, slow_tail), (1700.0, slow_tail)]
    corners = [(470.0, 530.0, 1, 0.5), (1070.0, 1130.0, -1, 0.5)]

    def brake(i, t, d, a):
        return 0.6 if a < -1.0 else 0.0

    def throttle(i, t, d, a):
        if d > 500.0 and d < 500.0 + throttle_late * 20.0:
            return 0.0
        return 1.0 if a > 0.3 else (0.0 if a < -1.0 else 0.3)
    tr = stage(points, corners, brake=brake, throttle=throttle)
    return tr, find_corners(tr)


def test_corners_get_a_window_a_radius_and_a_call():
    tr, corners = road()
    assert len(corners) == 2
    first = corners[0]
    assert 460 < first['d0'] < 500 < first['d1'] < 560 and first['direction'] == 1
    assert first['radius'] is not None and first['radius'] < 80
    assert first['tightness'] in ('1', '2', '3', '4', '3 long', '4 long')
    assert cc.tightness(8.0, 100.0) == '1' and cc.tightness(40.0, 50.0) == '3'
    assert cc.tightness(40.0, 100.0) == '3 long' and cc.tightness(200.0, 20.0) == '6'
    assert cc.tightness(40.0, 140.0) == 'hairpin' and cc.tightness(None, 10.0) is None
    assert cc.corner_name({'tightness': '3', 'direction': 1, 'd': 1800.0}) == 'the 3 left at 1.8 km'
    assert cc.corner_name({'tightness': 'hairpin', 'direction': -1, 'd': 900.0}) == 'the hairpin right at 0.9 km'


def test_corners_join_by_time_not_metres():
    """30 m is two seconds at 15 m/s and under one at 35 m/s."""
    def pair(speed):
        tr = rows(200, speed=speed)
        mid = tr[100][C['distance']]
        a = {'d': mid - 40, 'd0': mid - 60, 'd1': mid - 20, 'min_speed': speed}
        b = {'d': mid + 40, 'd0': mid + 20, 'd1': mid + 60, 'min_speed': speed}
        return tr, [a, b]
    tr, cs = pair(10.0)                                    # 40 m gap at 10 m/s: 4 s
    assert len(cc.build_sections(tr, cs)) == 2
    tr, cs = pair(40.0)                                    # 40 m gap at 40 m/s: 1 s
    sections = cc.build_sections(tr, cs)
    assert len(sections) == 1 and [c['complex'] for c in cs] == [0, 0] and sections[0]['lead'] is cs[0]
    # without a pace (no rows between them) 30 m is the fallback
    far = [{'d': 100.0, 'd0': 90.0, 'd1': 110.0, 'min_speed': 9.0}, {'d': 130.0, 'd0': 125.0, 'd1': 140.0, 'min_speed': 9.0}]
    assert len(cc.build_sections([], far)) == 1


def test_the_braking_point_is_the_strongest_application_not_the_last():
    tr, corners = road()
    # A stab early, the main braking later, a touch at turn-in
    def brake(i, t, d, a):
        if 380 < d < 395:
            return 0.35
        if 440 < d < 495:
            return 0.9
        if 497 < d < 500:
            return 0.15
        return 0.0
    points = [(0.0, 28.0), (430.0, 28.0), (500.0, 10.0), (600.0, 28.0), (1700.0, 28.0)]
    tr = stage(points, [(470.0, 530.0, 1, 0.5)], brake=brake)
    corners = find_corners(tr)
    sections = cc.build_sections(tr, corners)
    cc.describe_corners(tr, corners, sections)
    k = corners[0]
    assert abs(k['brake_d'] - (k['d'] - 440.0)) < 6.0 and abs(k['brake_peak'] - 0.9) < 1e-6
    assert corners[0]['brake_d'] > 40.0                    # not the touch that is nearly at the slowest point


def test_throttle_on_and_full_times_coasting_and_overlap_by_phase():
    def brake(i, t, d, a):
        return 0.8 if 430.0 < d < 497.0 else (0.4 if 470.0 < d < 480.0 else 0.0)

    def throttle(i, t, d, a):
        if 470.0 < d < 480.0:
            return 0.5                                     # left foot: both pedals on entry
        if 497.0 < d < 505.0:
            return 0.0                                     # coasting just after the slowest point
        if d < 440.0:
            return 1.0
        if d > 560.0:
            return 1.0
        return 0.5 if d > 505.0 else 0.0
    points = [(0.0, 28.0), (430.0, 28.0), (500.0, 10.0), (600.0, 28.0), (900.0, 28.0)]
    tr = stage(points, [(470.0, 530.0, 1, 0.5)], brake=brake, throttle=throttle)
    corners = find_corners(tr)
    sections = cc.build_sections(tr, corners)
    cc.describe_corners(tr, corners, sections)
    k = corners[0]
    assert k['overlap_entry'] > 0.5 and k['overlap_exit'] == 0.0
    assert k['coast_exit'] > 0.3 and k['coast_entry'] == 0.0 or k['coast_exit'] > 0.3
    assert k['throttle_on_t'] > 0.4                        # off the throttle past the slowest point, then on
    assert k['throttle_t'] > k['throttle_on_t']            # to full later than the decision to go
    assert k['relifts'] >= 0 and k['section_t'] > 5.0 and k['_entry_t'] > 0 and k['_exit_t'] > 0


def test_a_blip_is_not_overlap():
    def brake(i, t, d, a):
        return 0.3 if 480.0 < d < 497.0 else (0.8 if 430.0 < d <= 480.0 else 0.0)

    def throttle(i, t, d, a):
        return 0.6 if 480.0 < d < 497.0 else (0.0 if d < 500.0 and d > 430.0 else 1.0)
    points = [(0.0, 28.0), (430.0, 28.0), (500.0, 10.0), (600.0, 28.0), (900.0, 28.0)]
    tr = stage(points, [(470.0, 530.0, 1, 0.5)], brake=brake, throttle=throttle)
    corners = find_corners(tr)
    sections = cc.build_sections(tr, corners)
    t_blip = tr[next(i for i, r in enumerate(tr) if r[C['distance']] >= 488.0)][C['t']]
    cc.describe_corners(tr, corners, sections, [t_blip])
    blipped = corners[0]['overlap_entry']
    corners = find_corners(tr)
    cc.describe_corners(tr, corners, cc.build_sections(tr, corners))
    assert corners[0]['overlap_entry'] - blipped > 0.25            # the 0.4 s of the blip are not counted


def test_a_section_runs_from_the_references_braking_so_the_straight_belongs_to_the_exit():
    tr, corners = road(brake_from=1000.0)
    sections = cc.build_sections(tr, corners)
    cc.describe_corners(tr, corners, sections)
    assert len(sections) == 2
    a, b = sections[0]['bounds'], sections[1]['bounds']
    assert a[1] == b[0] and a[1] > corners[0]['d1'] + 100         # the first one owns the straight up to the braking
    assert 1000.0 - 20 < b[0] < 1040.0
    assert abs(sum(s['time'] for s in sections) - (tr[-1][C['t']] - tr[0][C['t']])) < 0.15    # nothing counted twice


def reference_from(tr, corners):
    return {'trace': tr, 'corners': [{k: v for k, v in c.items()} for c in corners], 'course': tr[-1][C['distance']]}


def test_time_lost_goes_to_the_section_it_was_lost_in():
    ref_tr, ref_corners = road()
    sections = cc.build_sections(ref_tr, ref_corners)
    cc.describe_corners(ref_tr, ref_corners, sections)
    tr, corners = road(vmin=6.0, brake_from=380.0)                  # over-slowing into the first corner only
    sections = cc.build_sections(tr, corners)
    cc.describe_corners(tr, corners, sections)
    total = cc.section_loss(tr, sections, reference_from(ref_tr, ref_corners))
    first, second = sections[0]['lead'], sections[1]['lead']
    assert first['loss_entry'] + first['loss_exit'] > 0.5
    assert abs(second['loss_entry'] + second['loss_exit']) < 0.3
    run_time = tr[-1][C['t']] - tr[0][C['t']]
    ref_time = ref_tr[-1][C['t']] - ref_tr[0][C['t']]
    assert abs(total - (run_time - ref_time)) < 0.4                  # the sections account for the difference
    assert first['loss_entry'] > 0 and first['loss_exit'] > 0


def test_the_run_that_is_quicker_has_negative_loss_and_an_off_section_is_not_compared():
    ref_tr, ref_corners = road(vmin=6.0, brake_from=380.0)
    cc.describe_corners(ref_tr, ref_corners, cc.build_sections(ref_tr, ref_corners))
    tr, corners = road()
    sections = cc.build_sections(tr, corners)
    cc.describe_corners(tr, corners, sections)
    total = cc.section_loss(tr, sections, reference_from(ref_tr, ref_corners))
    assert total < -0.5 and sections[0]['lead']['loss_entry'] + sections[0]['lead']['loss_exit'] < -0.5
    tr, corners = road()
    sections = cc.build_sections(tr, corners)
    cc.describe_corners(tr, corners, sections)
    for k in corners:
        k['off'] = 1
    assert cc.section_loss(tr, sections, reference_from(ref_tr, ref_corners)) is None
    assert cc.section_loss(tr, [], reference_from(ref_tr, ref_corners)) is None


def test_sections_match_by_window_overlap_or_apex():
    grid = [{'d0': 100.0, 'd1': 160.0, 'apex': 130.0}, {'d0': 400.0, 'd1': 420.0, 'apex': 410.0}]
    mine = [{'d0': 120.0, 'd1': 175.0, 'apex': 150.0},           # overlaps the first by more than half of the shorter
            {'d0': 436.0, 'd1': 450.0, 'apex': 440.0},           # beside the second, its apex within 25 m of 410? no: 30
            {'d0': 700.0, 'd1': 720.0, 'apex': 710.0}]
    assert cc.match_sections(mine, grid) == {0: 0}
    mine[1] = {'d0': 425.0, 'd1': 440.0, 'apex': 432.0}           # apex 22 m from the second's
    assert cc.match_sections(mine, grid) == {0: 0, 1: 1}


def test_the_spread_of_the_minimum_speed_across_runs_names_the_unsure_section():
    def run(speed_a, speed_b):
        return [{'d': 500.0, 'd0': 480.0, 'd1': 520.0, 'complex': 0, 'min_speed': speed_a, 'off': 0},
                {'d': 1100.0, 'd0': 1080.0, 'd1': 1120.0, 'complex': 1, 'min_speed': speed_b, 'off': 0}]
    others = [({'id': i}, run(10.0 + (3.0 if i % 2 else -3.0), 12.0)) for i in range(5)]
    mine = cc.sections_of(run(10.0, 12.0))
    events = cc.spread(mine, others)
    assert [e['kind'] for e in events] == ['spread', 'spread']
    wide, steady = events
    assert wide['value'] > 2.0 and steady['value'] == 0.0 and wide['detail']['runs'] == 6
    assert cc.spread(mine, others[:2]) == []                       # fewer than four runs: nothing to say
    mine[0]['corners'][0]['off'] = 1
    assert [e['d0'] for e in cc.spread(mine, others)] == [1080.0]  # an off section is not compared


# -- all of it --

def test_analyse_flags_the_cut_the_launch_and_the_block_change_down():
    tr, corners = limiter_run(gear_after=4)                        # on the cut in 3rd, up to 4th at 5 s
    context = {'limiter': LIMITER, 'surface': 'gravel', 'lights': {'shift': 6900.0, 'late': 7100.0},
               'best_for': lambda gear: {'rpm': 7450.0, 'engine_rpm': 7450.0, 'coverage': 1.0, 'source': 'game',
                                         'grip_limited': False}, 'pulls': lambda gear: 10, 'top_gear': 5}
    summary = {'launch': 1.0, 'release': 0.0, 'launch_rpm': 6500.0, 'finished': 1, 'duration': 20.0, 'course': 400.0}
    shifts = [{'at': 1000.0 + 3.0, 'gear': 1, 'gear_to': 2, 'direction': 'up', 'rpm': 6500.0, 'flat_out': 1,
               'method': 'sequential', 'flags': None, 'id': 1, 'throttle': 1.0, 'slip': None},
              {'at': 1000.0 + 5.0, 'gear': 3, 'gear_to': 4, 'direction': 'up', 'rpm': 7480.0, 'flat_out': 1,
               'method': 'sequential', 'flags': None, 'id': 2, 'throttle': 1.0, 'slip': None},
              {'at': 1000.0 + 9.0, 'gear': 4, 'gear_to': 3, 'direction': 'down', 'rpm': 7000.0, 'flat_out': 0,
               'method': 'h-pattern', 'flags': 'skip', 'id': 3, 'throttle': 0.0, 'slip': None, 'engage_rpm': 7000.0}]
    brake_tr = [tuple(NAN if k != C['brake'] else (0.5 if 80 <= i < 100 else 0.0) for k in range(len(row))) for i, row
                in enumerate(tr)]
    tr = [tuple(v if k != C['brake'] else b[C['brake']] for k, v in enumerate(row)) for row, b in zip(tr, brake_tr)]
    out = cc.analyse(summary, tr, [], shifts, context, started=1000.0)
    first, second, down = out['shifts']
    assert first['class'] == 'launch' and 'launch' in first['flags']          # the first change up within 3 s
    assert second['class'] == 'cut' and 'cut' in second['flags'] and second['touch'] >= 1.9
    assert down['flags'] is None                                              # braking: a block change down, not a skip
    assert out['run_class'] in ('learning', 'clean') and out['launch'] is not None
    assert [e['class'] for e in out['events'] if e['kind'] == 'limiter'] == ['shift']
    assert any(e['kind'] == 'launch' for e in out['events'])
    assert first['d'] is not None and first['d'] < second['d']


# -- step 2: the launch held on the cut, sections by place and cause, the best of the runs --

def test_the_launch_gear_held_on_the_limiter_is_counted():
    summary = {'launch': 1.0, 'release': 0.4, 'launch_rpm': 6500.0, 'finished': 1, 'duration': 200.0,
               'launch_clutch': 0.9}
    held = rows(60, speed=lambda i, t: 3.0 + t * 4.0, gear=1.0, rpm=lambda i, t: 7460.0 if 3 <= i < 9 else 6000.0,
                a_long=0.6 * G, throttle=1.0, brake=0.0, clutch=0.0)
    out = cc.launch_outcome(summary, held, limiter=7500.0)
    assert abs(out['cut'] - 0.6) < 0.11                                        # six rows on the cut in 1st
    assert cc.launch_outcome(summary, launch_trace(), limiter=7500.0)['cut'] == 0.0
    assert cc.launch_outcome(summary, held)['cut'] is None                     # no limiter known: nothing said
    changed = rows(60, speed=lambda i, t: 3.0 + t * 4.0, gear=lambda i, t: 1.0 if i < 4 else 2.0,
                   rpm=lambda i, t: 7460.0 if 3 <= i < 20 else 6000.0, a_long=0.6 * G, throttle=1.0, brake=0.0)
    assert cc.launch_outcome(summary, changed, limiter=7500.0)['cut'] < 0.2    # only the launch gear counts


def test_a_run_with_changes_of_gear_keeps_its_loss_against_the_reference():
    """The drive loss of a change up is not the section loss (they shared a name once)."""
    ref_tr, ref_corners = road()
    cc.describe_corners(ref_tr, ref_corners, cc.build_sections(ref_tr, ref_corners))
    tr, corners = road(vmin=6.0, brake_from=380.0)
    context = {'limiter': LIMITER, 'drive_loss': lambda gear, rpm: 0.04, 'top_gear': 5}
    shifts = [{'at': 1005.0, 'gear': 3, 'gear_to': 4, 'direction': 'up', 'rpm': 7000.0, 'flat_out': 1,
               'method': 'sequential', 'flags': None, 'id': 1, 'throttle': 1.0, 'slip': None}]
    summary = {'finished': 1, 'duration': 80.0, 'course': tr[-1][C['distance']]}
    out = cc.analyse(summary, tr, corners, shifts, context, started=1000.0, reference=reference_from(ref_tr, ref_corners))
    assert isinstance(out['loss'], float) and out['loss'] > 0.5
    out = cc.analyse(summary, tr, find_corners(tr), shifts, context, started=1000.0)
    assert out['loss'] is None                                                 # no reference, no loss


def stored(d, min_speed=10.0, direction=1, brake_d=50.0, exit_speed=20.0, entry_speed=25.0, throttle_on_t=0.5,
           counter_steer=0.0, loss=None, off=0, complex_=0, d0=None, d1=None, coast_entry=0.0, overlap_exit=0.0,
           tightness='3'):
    """A corner as the store holds it (CORNER_FIELDS), with the loss on it where the run had one."""
    return {'d': d, 'd0': d - 20.0 if d0 is None else d0, 'd1': d + 20.0 if d1 is None else d1, 'complex': complex_,
            'min_speed': min_speed, 'direction': direction, 'brake_d': brake_d, 'exit_speed': exit_speed,
            'entry_speed': entry_speed, 'throttle_on_t': throttle_on_t, 'counter_steer': counter_steer,
            'loss_entry': None if loss is None else loss[0], 'loss_exit': None if loss is None else loss[1],
            'off': off, 'coast_entry': coast_entry, 'overlap_exit': overlap_exit, 'tightness': tightness}


def compare(**kw):
    c = {'brake': 0.0, 'speed': 0.0, 'exit': 0.0, 'entry': 0.0, 'throttle': 0.0, 'counter': 0.0, 'coast_entry': 0.0,
         'overlap_exit': 0.0}
    c.update(kw)
    return c


def test_a_sections_numbers_name_what_it_lost_or_gained():
    kmh = 1 / 3.6
    pattern = cc.section_pattern
    assert pattern(compare(brake=25.0, speed=-7 * kmh, exit=-5 * kmh), 0.6) == 'over-slowing'
    assert pattern(compare(brake=3.0, speed=-6 * kmh, exit=-5 * kmh), 0.6) == 'under-committed'
    assert pattern(compare(brake=-20.0, speed=1 * kmh, exit=-6 * kmh), 0.6) == 'overdriven'
    assert pattern(compare(brake=2.0, speed=0.0, exit=-6 * kmh, entry=8 * kmh), 0.6) == 'overdriven'     # entered faster
    assert pattern(compare(speed=0.5 * kmh, exit=-4 * kmh, throttle=0.5), 0.4) == 'late-throttle'
    assert pattern(compare(brake=-5.0, speed=-6 * kmh, exit=-1 * kmh, counter=0.3), 0.5) == 'over-rotated'
    assert pattern(compare(brake=None, speed=-9 * kmh, exit=-8 * kmh), 0.5) == 'slower'
    assert pattern(compare(), 0.5) == 'unclear'
    # a gain: braking earlier for the same minimum and a faster exit is the loose-surface entry
    assert pattern(compare(brake=15.0, speed=0.5 * kmh, exit=6 * kmh), -0.4) == 'right-entry'
    assert pattern(compare(brake=-15.0, speed=5 * kmh, exit=6 * kmh), -0.4) == 'quicker'
    assert pattern(compare(brake=25.0, speed=-7 * kmh, exit=-5 * kmh), 0.05) is None        # under a tenth: not named
    assert pattern(compare(), None) is None


def test_sections_are_named_by_their_corners_and_set_against_the_reference():
    ref = [stored(300.0), stored(900.0, direction=-1, complex_=1, min_speed=12.0),
           stored(1000.0, direction=1, complex_=1, min_speed=14.0, d0=980.0, d1=1020.0)]
    # this run: the first corner is the start's, the second a complex whose slowest corner is the first of its two
    mine = [stored(305.0, loss=(0.0, 0.1)),
            stored(905.0, direction=-1, complex_=1, min_speed=8.0, brake_d=80.0, exit_speed=17.0, loss=(0.5, 0.3)),
            stored(1005.0, direction=1, complex_=1, min_speed=14.0, d0=985.0, d1=1025.0)]
    report = cc.section_report(mine, ref)
    assert [r['name'] for r in report] == ['the 3 left at 0.3 km', 'the right-left at 0.9 km']
    assert [r['first'] for r in report] == [True, False]                       # the start's section holds the launch
    second = report[1]
    assert abs(second['loss'] - 0.8) < 1e-9 and second['entry'] == 0.5 and second['exit'] == 0.3
    c = second['compare']
    assert c['brake'] == 30.0 and abs(c['speed'] - (-4.0)) < 1e-9              # braked 30 m earlier; 4 m/s slower
    assert second['pattern'] == 'over-slowing'
    assert cc.section_report(mine, [])[1]['ref'] is None                       # no reference: no comparison
    assert cc.section_name({'corners': [stored(1800.0, tightness='hairpin', direction=-1)], 'apex': 1800.0}) == \
        'the hairpin right at 1.8 km'
    many = {'corners': [stored(float(d)) for d in (100, 150, 200, 250)], 'apex': 100.0}
    assert cc.section_name(many) == 'the 4-corner section at 0.1 km'


def test_the_best_of_the_runs_is_found_section_by_section():
    ref_tr, ref_corners = road(vmin2=10.0, second_brake=1000.0)
    cc.describe_corners(ref_tr, ref_corners, cc.build_sections(ref_tr, ref_corners))
    quick_tr, quick_corners = road(vmin2=13.0, second_brake=1030.0)            # quicker through the second corner
    cc.describe_corners(quick_tr, quick_corners, cc.build_sections(quick_tr, quick_corners))
    slow_tr, slow_corners = road(vmin=6.0, brake_from=380.0, vmin2=10.0)       # slower through the first
    cc.describe_corners(slow_tr, slow_corners, cc.build_sections(slow_tr, slow_corners))
    reference = dict(reference_from(ref_tr, ref_corners), run=1)
    best = cc.stitched(reference, [{'run': 2, 'trace': quick_tr, 'corners': quick_corners},
                                   {'run': 3, 'trace': slow_tr, 'corners': slow_corners}])
    assert set(best['base']) == {1}                                            # the first section is the launch's: left out
    assert best['who'][1] == 2 and best['gain'][1] > 0.1
    assert abs(best['total'] - best['gain'][1]) < 1e-9
    assert cc.stitched(dict(reference, corners=[]), []) is None
    # a run that did not cover the section from end to end, or went off in it, has no time for it
    short = [row for row in quick_tr if row[C['distance']] < 1050.0]
    assert cc.grid_times(short, quick_corners, *cc.grid_of(reference)) == {}
    off = [dict(k, off=1) for k in quick_corners]
    assert cc.grid_times(quick_tr, off, *cc.grid_of(reference)) == {}


def test_a_held_straight_is_measured_against_the_reference_to_the_next_braking():
    # the reference takes the straight in 4th and is quicker; this run held 3rd on the cut
    mine = rows(300, speed=lambda i, t: 20.0, gear=3.0, brake=lambda i, t: 0.6 if i > 200 else 0.0)
    theirs = rows(300, speed=lambda i, t: 24.0, gear=4.0, brake=lambda i, t: 0.6 if i > 200 else 0.0)
    found = cc.held_vs_reference(mine, theirs, 100.0)
    assert found['ref_gear'] == 4 and found['lost'] > 0.5 and found['d1'] > 400.0
    even = cc.held_vs_reference(mine, mine, 100.0)
    assert abs(even['lost']) < 1e-9
    nobrake = rows(300, speed=20.0, gear=3.0, brake=0.0)
    assert cc.held_vs_reference(nobrake, theirs, 100.0) is None                # never braked again


def test_both_pedals_in_a_braking_zone_are_not_drag_on_a_straight():
    # 600 m straight at 40 m/s: the corner's yaw window is at 500-520 m, its braking began 180 m before the
    # slowest point, which is more than STRAIGHT m before the window
    corner = {'d0': 500.0, 'd1': 520.0, 'd': 510.0, 'brake_d': 180.0}
    tr = rows(150, speed=40.0, throttle=0.0, brake=0.0)
    both = lambda i, t: 0.3 if 84 <= i < 110 else 0.0              # on both at 336-440 m: in the braking zone
    held = rows(150, speed=40.0, throttle=both, brake=both)
    assert cc.pedal_time(held, [corner])[1] == 0.0
    # the same without the braking onset is a straight 100 m clear of the window: rows 84-110 are 336-440 m
    corner['brake_d'] = None
    assert cc.pedal_time(held, [corner])[1] > 1.0
    # braking harder than the throttle with the speed falling on a straight is braking too
    slowing = rows(150, speed=lambda i, t: 40.0 - 0.05 * i, throttle=lambda i, t: 0.3 if 20 <= i < 40 else 0.0,
                   brake=lambda i, t: 0.5 if 20 <= i < 40 else 0.0)
    assert cc.pedal_time(slowing, [])[1] == 0.0
    assert tr and cc.pedal_time(tr, [])[1] == 0.0


def test_the_exit_is_timed_to_full_throttle_not_to_the_next_braking():
    ref_tr, ref_corners = road()
    cc.describe_corners(ref_tr, ref_corners, cc.build_sections(ref_tr, ref_corners))
    tr, corners = road(slow_tail=15.0)                    # the same corners, a slow straight 270 m after the last
    sections = cc.build_sections(tr, corners)
    cc.describe_corners(tr, corners, sections)
    total = cc.section_loss(tr, sections, reference_from(ref_tr, ref_corners))
    run_time = tr[-1][C['t']] - tr[0][C['t']]
    ref_time = ref_tr[-1][C['t']] - ref_tr[0][C['t']]
    assert run_time - ref_time > 5.0                       # the straight cost seconds...
    last = sections[1]['lead']
    assert last['loss_entry'] + last['loss_exit'] < 0.3     # ...and no section is blamed for them
    assert abs(total) < 0.5


def test_the_spread_matches_corners_not_neighbours_and_skips_a_run_whose_corner_was_off():
    def run(speed, d=1100.0, off=0, extra=None):
        corners = [{'d': 500.0, 'd0': 480.0, 'd1': 520.0, 'complex': 0, 'min_speed': 10.0, 'off': 0, 'direction': 1},
                   {'d': d, 'd0': d - 20, 'd1': d + 20, 'complex': 1, 'min_speed': speed, 'off': off, 'direction': -1}]
        return corners + (extra or [])
    mine = cc.sections_of(run(12.0))
    # a run whose hairpin was marked off is left out: the 60 km/h corner 80 m on is not its stand-in
    neighbour = {'d': 1180.0, 'd0': 1160.0, 'd1': 1200.0, 'complex': 2, 'min_speed': 16.7, 'off': 0, 'direction': -1}
    others = [({'id': i}, run(12.0 + 0.2 * i)) for i in range(3)] + [({'id': 9}, run(5.0, off=1, extra=[neighbour]))]
    [first, second] = cc.spread(mine, others)
    assert second['detail']['runs'] == 4 and second['detail']['max'] < 13.0 and second['value'] < 0.5
    # a corner the other way, or further than MATCH_CORNER m, is not the same corner
    far = [({'id': i}, run(12.0, d=1100.0 + 20)) for i in range(5)]
    assert [e['d0'] for e in cc.spread(mine, far)] == [480.0]
    wrong_way = [({'id': i}, [dict(k, direction=-k['direction']) for k in run(12.0)]) for i in range(5)]
    assert cc.spread(mine, wrong_way) == []
    # one odd run does not make the corner a spread
    odd = [({'id': i}, run(20.0 if i == 0 else 12.0)) for i in range(5)]
    assert [e for e in cc.spread(mine, odd) if e['d0'] == 1080.0][0]['value'] == 0.0


def crash_run(finished=0):
    """The road's first corner, then the second one's braking and a crawl of 20 s that ends the trace."""
    points = [(0.0, 28.0), (430.0, 28.0), (500.0, 10.0), (600.0, 28.0), (1000.0, 28.0), (1100.0, 12.0),
              (1110.0, 1.0), (1130.0, 1.0)]
    tr = stage(points, [(470.0, 530.0, 1, 0.5), (1070.0, 1130.0, -1, 0.5)],
               brake=lambda i, t, d, a: 0.6 if a < -1.0 else 0.0, throttle=lambda i, t, d, a: 0.0 if a < -1.0 else 0.3)
    return tr, find_corners(tr)


def test_a_run_that_ended_in_a_crash_has_an_off_and_no_loss_in_the_section_it_stopped_in():
    ref_tr, ref_corners = road()
    cc.describe_corners(ref_tr, ref_corners, cc.build_sections(ref_tr, ref_corners))
    tr, corners = crash_run()
    end = tr[-1][C['distance']]
    # the crawl reaches the end of the trace: past the finish's margin on a finished run, an off on an unfinished one
    summary = {'finished': 0, 'course': end, 'duration': 80.0}
    out = cc.analyse(summary, tr, corners, [], {}, reference=reference_from(ref_tr, ref_corners), stage_length=1500.0)
    assert [e['class'] for e in out['events'] if e['kind'] == 'off'] == ['long']
    assert out['run_class'] == 'partial'
    assert all(k.get('loss_exit') is None for k in corners[1:]) and all(k['off'] for k in corners[1:])
    # a run that ended in silence short of the line is not a finished run either
    tr, corners = crash_run()
    out = cc.analyse({'finished': None, 'course': end}, tr, corners, [], {}, stage_length=1500.0)
    assert out['run_class'] == 'partial' and any(e['kind'] == 'off' for e in out['events'])
    assert cc.run_class(None, 5000.0, None, []) == 'clean'                       # no stage length: unknown
    assert cc.run_class(None, 5000.0, 5000.0, []) == 'clean'
    assert cc.run_class(None, 1200.0, 5000.0, []) == 'restart'


def test_an_unfinished_run_is_not_timed_through_the_section_it_stopped_in():
    ref_tr, ref_corners = road()
    cc.describe_corners(ref_tr, ref_corners, cc.build_sections(ref_tr, ref_corners))
    tr, corners = crash_run()
    sections = cc.build_sections(tr, corners)
    cc.describe_corners(tr, corners, sections)
    ref = reference_from(ref_tr, ref_corners)
    assert cc.section_loss(tr, sections, ref, unfinished=True) is not None          # the first corner is timed
    assert 'loss_exit' not in sections[-1]['lead'] and 'loss_exit' in sections[0]['lead']


def test_a_hit_that_fits_in_one_row_is_a_hit_and_a_reset_is_an_off():
    # the Loutraki crash: 31 to 0 km/h in one row, the distance going back 5 m and the gear 0
    def speed(i, t):
        return 0.0 if 120 <= i < 135 else 20.0

    tr = rows(300, speed=speed, distance=lambda i, t: i * 2.0 - (5.4 if i >= 120 else 0.0),
              gear=lambda i, t: 0.0 if 120 <= i < 135 else 3.0)
    assert cc.hits(tr) == [(120, 120)]                              # 20 m/s lost in 0.1 s
    events = cc.incidents(tr, course=tr[-1][C['distance']])
    assert {e['kind'] for e in events} == {'hit', 'off'}
    assert [e['class'] for e in events if e['kind'] == 'off'] == ['hit']
    # a reset with no stretch at 3 m/s: the distance going back with the car stopped is an off by itself
    tr = rows(300, speed=lambda i, t: 0.5 if i == 150 else 20.0,
              distance=lambda i, t: i * 2.0 - (5.4 if i >= 150 else 0.0))
    assert [e['class'] for e in cc.incidents(tr, course=tr[-1][C['distance']]) if e['kind'] == 'off'] == ['reset']
    # one row of 11 g sideways is a hit; a landing's two rows over 2.5 g still are; one row of 3 g is not
    tr = rows(300, speed=20.0, a_lat=lambda i, t: 11 * G if i == 50 else 0.0)
    assert cc.hits(tr) == [(50, 50)]
    tr = rows(300, speed=20.0, a_long=lambda i, t: 3 * G if i in (50, 51) else 0.0)
    assert cc.hits(tr) == [(50, 51)]
    tr = rows(300, speed=20.0, a_long=lambda i, t: 3 * G if i == 50 else 0.0)
    assert cc.hits(tr) == []


def test_a_game_whose_speed_is_not_checked_has_no_hits_from_one_row_of_speed_and_no_off_for_them():
    # a noisy speed channel: 12 m/s lost across a row now and then, the slow stretch after it
    def speed(i, t):
        return 2.0 if 120 <= i < 135 else (8.0 if i % 40 == 0 else 20.0)

    tr = rows(300, speed=speed, gear=3.0)
    assert len(cc.hits(tr)) > 3 and len(cc.hits(tr, 'acr')) == len(cc.hits(tr))
    assert cc.hits(tr, 'wrcg') == []
    course = tr[-1][C['distance']]
    assert [e['class'] for e in cc.incidents(tr, course=course) if e['kind'] == 'off'] == ['hit']
    events = cc.incidents(tr, course=course, game='wrcg')
    assert not [e for e in events if e['kind'] in ('hit', 'off')] and [e['kind'] for e in events] == ['stop']
    # the game reaches the context layer through the run summary
    out = cc.analyse({'finished': 1, 'course': course, 'game': 'wrcg'}, tr, [], [], {})
    assert out['run_class'] != 'off'


def test_a_corners_call_ignores_a_crash_row_and_follows_the_reference():
    # 20 m/s through a 0.6 rad/s bend is a 33 m radius; one row at 3 m/s and 2 rad/s is no 1.5 m radius
    def yaw(i, t):
        return 0.6 if 60 <= i < 80 else 0.0

    def speed(i, t):
        return 3.0 if i == 70 else 20.0
    tr = rows(150, speed=speed, yaw_rate=yaw, gear=3.0)
    tr = [tuple(2.0 if (n == 70 and name == 'yaw_rate') else v for name, v in zip(TRACE_CHANNELS, row))
          for n, row in enumerate(tr)]
    [corner] = find_corners(tr)
    assert corner['radius'] is not None and corner['radius'] > 15.0 and corner['tightness'] in ('2', '3')
    # the call the reference gave the same corner is kept
    ref = reference_from(tr, [dict(corner, tightness='2', complex=0)])
    mine = [dict(corner, tightness='4')]
    out = cc.analyse({'finished': 1, 'course': tr[-1][C['distance']]}, tr, mine, [], {}, reference=ref)
    assert out['corners'][0]['tightness'] == '2'
