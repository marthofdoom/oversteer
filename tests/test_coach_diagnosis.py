"""The corner diagnosis (oversteer/coach_diagnosis.py, docs/coach-diagnosis.md): every row of the decision table from a
minimal pair of synthetic runs on tests.test_coach_context.stage's road, the owner's case (Afon Bidno, the
left-left-right at 2.2 km) as a regression, the braking point, the zone pairing, the exit speed read at a place, the
wording, and the potential's calls with and without a diagnosis."""

import gzip
import json
import os
import re

import numpy as np
import pytest

from oversteer import coach, coach_context as cc, coach_diagnosis as dg, potential
from tests.test_coach_context import C, stage

ENV = {'bins': np.array([0.0, 60.0]), 'lat': np.array([10.0, 10.0])}      # 10 m/s^2 of grip at every speed
KEY = {'d': 500.0, 'd0': 470.0, 'd1': 530.0, 'direction': 1}
SEC = {'a': 400.0, 'b': 700.0, 'lo': 300.0, 'key': KEY}
WITH = lambda d, yaw: 0.3 * np.sign(yaw)          # steering with the yaw
AGAINST = lambda d, yaw: -0.3 * np.sign(yaw)      # steering against it


def braking(lo, hi, peak):
    """A brake pedal by distance: `peak` between lo and hi m, off elsewhere."""
    return lambda i, t, d, a: peak if lo <= d <= hi else 0.0


def no_brake(i, t, d, a):
    return 0.0


def run(points, peak=0.5, corner=(470.0, 530.0), throttle=None, brake=None, gear=3.0, steer=None, xz=None):
    """A run along 900 m of road, `points` [(distance, speed m/s)] between its 28 m/s ends, a corner of yaw `peak`
    over `corner`; the brake follows the deceleration unless given. `steer` (distance, yaw) -> steering, `xz`
    distance -> (x, y, z) fill those columns."""
    rows = stage([(0.0, 28.0)] + points + [(900.0, 28.0)], [(corner[0], corner[1], 1, peak)], throttle=throttle,
                 brake=brake, gear=gear)
    if steer or xz:
        out = []
        for r in rows:
            r = list(r)
            if steer:
                r[C['steer']] = steer(r[C['distance']], r[C['yaw_rate']])
            if xz:
                r[C['x']], r[C['y']], r[C['z']] = xz(r[C['distance']])
            out.append(tuple(r))
        rows = out
    return potential.arrays(rows)


REF = run([(430.0, 28.0), (500.0, 10.0), (600.0, 28.0)])      # braked from 430 m to 10 m/s at 500 m, full throttle by 600 m


def diagnose(arr, ref=REF, game=None, loss=None, env=ENV):
    m = dg.measures(arr, ref, dict(SEC, game=game), env)
    return m, dg.diagnose(m, loss)


def check(arr, code, ref=REF, **kw):
    m, d = diagnose(arr, ref, **kw)
    assert d['code'] == code, (d['code'], d['facts'], d['fix'], d['evidence'])
    return m, d


def wording(d):
    return ' '.join(d['facts'] + [d['fix'] or ''])


# -- the table, row by row --

def test_the_same_corner_is_the_same_and_a_quicker_one_is_praised_with_no_fix():
    m, d = check(REF, 'SAME')
    assert m['loss'] == pytest.approx(0.0, abs=1e-6) and d['fix'] is None and d['facts'] == []
    m, d = check(run([(430.0, 28.0), (500.0, 12.0), (600.0, 28.0)]), 'GAIN')
    assert m['loss'] < -dg.LOSS_MIN and d['fix'] is None
    assert d['facts'] == ['carried 7 km/h more through the slowest point', 'left 4 km/h faster']


def test_slower_before_the_braking_is_the_run_up_and_points_at_the_corner_before():
    m, d = check(run([(200.0, 22.0), (430.0, 22.0), (500.0, 9.0), (600.0, 28.0)]), 'SLOW-ARRIVAL')
    assert m['pre_dv'] < -dg.SLOW_ARRIVAL and d['confidence'] == 'medium'
    assert d['facts'][0] == 'were 22 km/h slower before the braking'
    assert d['fix'] == 'Leave the corner before 22 km/h faster: you were 22 km/h slower before braking here.'
    # named where the caller knows the corner before
    named = dg.diagnose(m, None, 'the 4 left at 1.7 km')
    assert named['fix'] == 'Leave the 4 left at 1.7 km 22 km/h faster: you were 22 km/h slower before braking here.'


def test_in_too_fast_is_overshot_and_says_brake_earlier_only_when_it_braked_later():
    later = run([(450.0, 28.0), (520.0, 8.0), (620.0, 28.0)], peak=2.4)      # braked 20 m later, at the grip limit
    m, d = check(later, 'OVERSHOT')
    assert d['confidence'] == 'high' and m['onset_dd'] == pytest.approx(20.0, abs=1.5)
    assert d['fix'] == 'Brake 20 m earlier, where your best run did, and turn in at its speed.'
    # the same braking point with more speed left at the turn-in (it braked less hard before it): get the speed off
    ref = run([(430.0, 28.0), (500.0, 10.0), (600.0, 28.0)], brake=braking(430, 490, 0.7))
    fast = run([(430.0, 28.0), (460.0, 24.0), (530.0, 8.0), (600.0, 12.0), (640.0, 28.0)], peak=1.3,
               corner=(480.0, 560.0), brake=braking(430, 520, 0.7))
    m, d = check(fast, 'OVERSHOT', ref=ref)
    assert abs(m['onset_dd']) < dg.BRAKE_SAME and m['turnin_dv'] >= dg.SPEED_SAME
    assert d['fix'].startswith('Get the speed off before the turn-in: arrive ') and 'earlier' not in d['fix']


def test_more_steering_against_the_yaw_than_the_reference_is_a_slide_and_states_the_shares():
    ref = run([(430.0, 28.0), (500.0, 10.0), (600.0, 28.0)], steer=WITH)
    m, d = check(run([(430.0, 28.0), (500.0, 8.0), (600.0, 28.0)], steer=AGAINST), 'SLIDE', ref=ref)
    assert d['fix'] == 'Keep the car straighter through it, as your best run did.'
    assert d['facts'][0] == 'steered against the slide for 100 % of the corner (your best run 0 %)'


def test_slower_with_no_braking_and_no_cornering_load_is_lost_speed_with_no_fix():
    m, d = check(run([(430.0, 28.0), (500.0, 8.0), (600.0, 28.0)], peak=0.05, brake=no_brake), 'LOST-SPEED')
    assert d['fix'] is None and d['confidence'] == 'low'
    assert 'had hardly any cornering load there (4 % of the grip)' in d['facts']


def test_over_slowed_has_five_hows():
    # no brake at all: lift less
    m, d = check(run([(430.0, 28.0), (500.0, 8.0), (600.0, 28.0)], brake=no_brake), 'OVER-SLOWED')
    assert d['fix'].startswith('Lift less: carry 7 km/h more through the slowest point. About 58 % of the grip was left')
    # the reference did not brake: brake less there, or not at all
    ref = run([(430.0, 28.0), (500.0, 10.0), (600.0, 28.0)], brake=no_brake)
    m, d = check(run([(430.0, 28.0), (500.0, 8.0), (600.0, 28.0)]), 'OVER-SLOWED', ref=ref)
    assert d['fix'].startswith('Brake less there, or not at all, as your best run did: carry 7 km/h more')
    assert d['facts'][0] == 'braked where your best run did not (72 km/h off)'
    ref = run([(440.0, 28.0), (500.0, 10.0), (600.0, 28.0)], brake=braking(440, 490, 0.6))
    slow = [(440.0, 28.0), (500.0, 7.0), (600.0, 28.0)]
    # held the brake 15 m longer
    m, d = check(run(slow, brake=braking(440, 505, 0.6)), 'OVER-SLOWED', ref=ref)
    assert d['fix'].startswith('Keep that braking point and come off the brake 10 m sooner: carry 11 km/h more')
    # a harder pedal
    m, d = check(run(slow, brake=braking(440, 490, 0.9)), 'OVER-SLOWED', ref=ref)
    assert d['fix'].startswith('Keep that braking point and brake less hard (90 % pedal, your best run 60 %): carry 11')
    # else: brake less
    m, d = check(run(slow, brake=braking(440, 490, 0.6)), 'OVER-SLOWED', ref=ref)
    assert d['fix'].startswith('Keep that braking point and brake less: carry 11 km/h more through the slowest point.')
    assert d['confidence'] == 'high' and 'braked at the same place' in d['facts']
    assert any('shed' in e for e in d['evidence'])


def test_early_braking_with_the_time_lost_before_the_slowest_point_has_three_wordings():
    early = run([(380.0, 28.0), (450.0, 11.0), (500.0, 10.0), (600.0, 28.0)])        # 50 m earlier, the same minimum
    m, d = check(early, 'EARLY-BRAKE')
    assert d['fix'] == 'Brake 50 m later, where your best run did.' and 'took the same speed through the slowest point' in d['facts']
    # a slightly lower minimum, no more speed taken off than the reference (it was already slower at the braking)
    lower = run([(300.0, 28.0), (370.0, 27.2), (450.0, 12.0), (500.0, 9.0), (600.0, 28.0)], brake=braking(375, 470, 0.6))
    m, d = check(lower, 'EARLY-BRAKE')
    assert d['fix'].startswith('Brake 54 m later, where your best run did: carry 4 km/h more through the slowest point.')
    # a lower minimum with more taken off: later and less
    more = run([(380.0, 28.0), (450.0, 11.0), (500.0, 8.0), (600.0, 28.0)])
    m, d = check(more, 'EARLY-BRAKE')
    assert d['fix'].startswith('Brake 50 m later and less, as your best run did: carry 7 km/h more through the slowest point.')


def test_braking_again_after_the_slowest_point_is_exit_brake():
    arr = run([(430.0, 28.0), (500.0, 10.0), (545.0, 20.0), (565.0, 13.0), (700.0, 28.0)],
              brake=lambda i, t, d, a: (0.7 if a < -1.0 and d < 520 else 0.0) + (0.7 if 545 <= d <= 565 else 0.0))
    m, d = check(arr, 'EXIT-BRAKE')
    assert d['fix'].startswith('Keep the speed up after the slowest point: no brake on the way out')
    assert 'braked again on the way out (24 km/h off, your best run 0 km/h)' in d['facts']


def test_the_throttle_later_with_the_same_minimum_is_late_throttle():
    arr = run([(430.0, 28.0), (500.0, 10.0), (560.0, 10.5), (680.0, 28.0)],
              throttle=lambda i, t, d, a: 0.0 if 480.0 < d < 560.0 else (1.0 if a > 0.3 else (0.0 if a < -1.0 else 0.3)))
    m, d = check(arr, 'LATE-THROTTLE')
    assert d['fix'] == 'Throttle 59 m sooner after the slowest point, where your best run did.'
    assert d['facts'][0] == 'took the same speed through the slowest point'


def test_a_gap_with_neither_pedal_between_the_brake_and_the_throttle_is_coasting():
    ref = run([(430.0, 28.0), (500.0, 10.0), (600.0, 28.0)], brake=braking(430, 490, 0.7))
    arr = run([(430.0, 28.0), (500.0, 10.0), (550.0, 18.0), (700.0, 28.0)], brake=braking(430, 470, 0.7),
              throttle=lambda i, t, d, a: 0.0 if d < 500 else 1.0)
    m, d = check(arr, 'COASTING', ref=ref)
    assert d['fix'] == 'Go from the brake to the throttle without the gap.'
    assert 'had 1.3 s more with neither pedal between the brake and the throttle' in d['facts']


def test_another_gear_on_the_way_out_with_a_slower_exit_is_gear():
    arr = run([(430.0, 28.0), (500.0, 10.0), (600.0, 25.0), (800.0, 28.0)], gear=3.0)
    m, d = check(arr, 'GEAR', ref=run([(430.0, 28.0), (500.0, 10.0), (600.0, 28.0)], gear=4.0))
    assert d['fix'] == 'Use 4th out of it, as your best run did.' and d['confidence'] == 'medium'
    assert 'were in 3rd on the way out (your best run 4th)' in d['facts']


def test_a_slowest_point_much_earlier_with_a_slower_exit_is_an_early_apex():
    arr = run([(430.0, 28.0), (480.0, 10.0), (680.0, 28.0)], corner=(450.0, 510.0))
    m, d = check(arr, 'EARLY-APEX')
    assert d['fix'] == 'Take the slowest point 19 m later, where your best run did.' and d['confidence'] == 'low'


def test_a_lower_minimum_at_the_grip_limit_has_no_fix_without_positions_and_a_line_with_them():
    slow = [(430.0, 28.0), (500.0, 8.0), (600.0, 28.0)]
    m, d = check(run(slow, peak=1.3), 'AT-LIMIT')
    assert d['fix'] is None and 'used 100 % of the grip there (your best run 52 %)' in d['facts']
    assert not re.search(r'\bline\b', wording(d))
    along = lambda off: (lambda d: (d, 0.0, -off))
    mine = run(slow, peak=1.3, xz=along(3.0))
    ref = run([(430.0, 28.0), (500.0, 10.0), (600.0, 28.0)], xz=along(0.0))
    m, d = check(mine, 'AT-LIMIT', ref=ref, game='acr')
    assert m['line_apex'] == pytest.approx(3.0, abs=0.1)                    # 3 m to the inside of the reference's path
    assert d['fix'] == 'Your best run was 3 m outside of your line at its slowest point, on a wider radius.'
    # the game is not named: no positions, no line, even where the columns are there
    m, d = check(mine, 'AT-LIMIT', ref=ref, game=None)
    assert m['line_apex'] is None and d['fix'] is None


def test_a_loss_with_no_measured_cause_is_unclear_and_gives_the_facts_only():
    m, d = check(run([(300.0, 28.0), (430.0, 26.9), (500.0, 10.0), (600.0, 28.0)]), 'UNCLEAR')
    assert d['fix'] is None and d['confidence'] == 'low' and d['facts']
    assert 'took the same speed through the slowest point' in d['facts']


def test_the_rows_are_tried_in_order():
    # a slide that also braked later and carried the speed in is OVERSHOT first (it came in too fast), not SLIDE
    ref = run([(430.0, 28.0), (500.0, 10.0), (600.0, 28.0)], steer=WITH)
    arr = run([(450.0, 28.0), (520.0, 8.0), (620.0, 28.0)], peak=1.3, steer=AGAINST)
    check(arr, 'OVERSHOT', ref=ref)
    # a slide whose pedals also over-slowed it is SLIDE (3) before OVER-SLOWED (5)
    arr = run([(430.0, 28.0), (500.0, 8.0), (600.0, 28.0)], steer=AGAINST)
    check(arr, 'SLIDE', ref=ref)


# -- the owner's case --

def complex_run(points, brake=None):
    rows = stage([(0.0, 28.0)] + points + [(1000.0, 24.0)], [(470, 530, 1, 0.5), (600, 660, -1, 0.5), (750, 810, 1, 0.5)],
                 brake=brake)
    return potential.arrays(rows)


COMPLEX = [{'d': 500.0, 'd0': 470.0, 'd1': 530.0, 'direction': 1}, {'d': 630.0, 'd0': 600.0, 'd1': 660.0, 'direction': -1},
           {'d': 780.0, 'd0': 750.0, 'd1': 810.0, 'direction': 1}]


def test_the_owners_case_brake_later_then_over_slowed_the_last_corner_of_a_complex():
    """The coach said "you braked 41 m later, were 14 km/h slower ... Brake 41 m earlier". The run braked 20 m later
    for the first corner and was faster through it; the time went at the third, which the reference did not brake for
    (this run: 95 % pedal, 36 km/h off)."""
    ref = complex_run([(430.0, 28.0), (500.0, 16.0), (565.0, 24.0), (590.0, 24.0), (630.0, 17.0), (700.0, 25.0),
                       (750.0, 25.0), (780.0, 24.0), (810.0, 25.0)])
    mine = complex_run([(450.0, 28.0), (500.0, 17.0), (565.0, 24.0), (590.0, 24.0), (630.0, 17.0), (700.0, 25.0),
                        (745.0, 25.0), (780.0, 15.0), (810.0, 25.0)],
                       brake=lambda i, t, d, a: 0.95 if 745 <= d <= 780 else (0.7 if a < -1.0 else 0.0))
    parts = dg.measures_section(mine, ref, {'a': 400.0, 'b': 900.0, 'lo': 300.0, 'floor': 300.0, 'corners': COMPLEX,
                                            'game': None}, ENV)
    assert [round(k['d']) for k, _m in parts] == [500, 630, 780]
    first, third = parts[0][1], parts[2][1]
    assert first['onset_dd'] == pytest.approx(20.0, abs=1.5) and first['min_dv'] > 0 and first['loss'] < 0
    d = dg.diagnose_section(parts)
    assert d['code'] == 'OVER-SLOWED' and round(d['corner']['d']) == 780 and d['m'] is third
    assert 'Brake less there, or not at all' in d['fix'] and 'earlier' not in wording(d)
    assert d['facts'][0].startswith('braked where your best run did not (')
    assert [p[2] for p in d['parts']] == ['GAIN', 'SAME', 'OVER-SLOWED']


def _replayed(name):
    """measures_section + diagnose_section of a fixture of marth's replayed runs (tests/data/<name>.json.gz)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', name + '.json.gz')
    with gzip.open(path, 'rt') as fh:
        fx = json.load(fh)
    names = fx['channels']

    def arrays(rows):
        out = []
        for r in rows:
            row = [float('nan')] * len(cc.TRACE_CHANNELS)
            for n, v in zip(names, r):
                row[C[n]] = float('nan') if v is None else v
            out.append(tuple(row))
        return potential.arrays(out)
    env = {'bins': np.asarray(fx['env']['bins']), 'lat': np.asarray(fx['env']['lat'])}
    parts = dg.measures_section(arrays(fx['run']), arrays(fx['ref']), {
        'a': fx['a'], 'b': fx['b'], 'lo': fx['lo'], 'floor': fx['floor'], 'corners': fx['corners'], 'game': None}, env)
    return dg.diagnose_section(parts, fx['loss'])


def test_the_right_left_right_at_1200_with_little_grip_used_is_not_overshot():
    """Afon Bidno - Severn, the i20N's run 60 against its best: in 10 km/h faster, the slowest point 17 m later, but
    44 % of the grip used (the best run 79 %) and no more steering against the slide: the car was not at its limit,
    so it did not overshoot, and no sentence says it steered against a slide it did not."""
    d = _replayed('afon-bidno-1200')
    assert d['code'] != 'OVERSHOT', (d['code'], d['facts'], d['fix'])
    assert not any('steered against the slide' in f for f in d['facts'])


def test_the_owners_case_replayed_from_the_captures():
    """Afon Bidno - Severn, the i20N's run 57 (199.7 s) against run 51, the left-left-right at 2.2 km: the 4 right at
    2.13 km. The rows of both runs around the complex, its corners, bounds and the car's stored envelope, as the
    replay of marth's captures has them (tests/data/afon-bidno-2200.json.gz)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'afon-bidno-2200.json.gz')
    with gzip.open(path, 'rt') as fh:
        fx = json.load(fh)
    names = fx['channels']

    def arrays(rows):
        out = []
        for r in rows:
            row = [float('nan')] * len(cc.TRACE_CHANNELS)
            for name, v in zip(names, r):
                row[C[name]] = float('nan') if v is None else v
            out.append(tuple(row))
        return potential.arrays(out)
    env = {'bins': np.asarray(fx['env']['bins']), 'lat': np.asarray(fx['env']['lat'])}
    parts = dg.measures_section(arrays(fx['run']), arrays(fx['ref']), {
        'a': fx['a'], 'b': fx['b'], 'lo': fx['lo'], 'floor': fx['floor'], 'corners': fx['corners'], 'game': None}, env)
    d = dg.diagnose_section(parts, fx['loss'])
    assert d['code'] == 'OVER-SLOWED' and d['confidence'] == 'high'
    assert round(d['corner']['d']) == 2133 and cc.corner_name(d['corner']).startswith('the 4 right')
    assert d['facts'] == ['braked where your best run did not (24 km/h off)',
                          'were 24 km/h slower at the slowest point (67 against 90)',
                          'used 74 % of the grip there (your best run 81 %)']
    assert d['fix'] == ('Brake less there, or not at all, as your best run did: carry 24 km/h more through the slowest '
                        'point. About 26 % of the grip was left there.')
    assert 'earlier' not in wording(d)


# -- the measures --

def test_the_braking_point_is_where_the_pedal_went_down_not_the_distance_to_the_slowest_point():
    """The same braking, the slowest point 20 m later: the old measure (metres before the slowest point) said 20 m
    earlier."""
    later_apex = run([(430.0, 28.0), (520.0, 10.0), (620.0, 28.0)])
    m, d = diagnose(later_apex)
    assert abs(m['onset_dd']) < 2.0 and m['min_dd'] == pytest.approx(20.0, abs=1.5)
    assert dg._facts(m)['brake'] == 'braked at the same place'
    # and the stored corners' fallback agrees: the onsets compared on the road
    mine = {'d': 520.0, 'brake_d': 90.0}
    theirs = {'d': 500.0, 'brake_d': 70.0}
    assert cc._onset_diff(mine, theirs) == 0.0
    assert cc._onset_diff(dict(mine, brake_d=None), theirs) is None


def test_the_braking_is_the_one_that_shed_the_most_speed_not_a_stab_after_it():
    light_then_stab = run([(400.0, 28.0), (450.0, 17.0), (480.0, 17.0), (490.0, 15.6), (600.0, 28.0)],
                          brake=lambda i, t, d, a: 0.4 if 400 <= d <= 450 else (0.9 if 480 <= d <= 490 else 0.0))
    m = dg.measures(light_then_stab, light_then_stab, dict(SEC), ENV)
    assert m['onset_r'] == pytest.approx(400.0, abs=1.5) and m['onset_x'] == m['onset_r']
    assert m['shed_r'] == pytest.approx(40.0, abs=2.0) and m['peak_r'] == pytest.approx(0.4, abs=0.01)
    # a run that braked at 400 against a reference that did it in one go from 430: paired with the light braking
    m = dg.measures(light_then_stab, REF, dict(SEC), ENV)
    assert m['onset_x'] == pytest.approx(400.0, abs=1.5) and m['onset_dd'] == pytest.approx(-30.0, abs=2.0)


def test_the_exit_speed_is_read_at_a_place_not_after_a_time():
    slow_out = run([(430.0, 28.0), (500.0, 10.0), (600.0, 25.0), (800.0, 28.0)])
    m = dg.measures(slow_out, REF, dict(SEC), ENV)
    assert m['exit_d'] == dg.EXIT_AT
    # R's slowest point + 50 m: 19 m/s on the reference, 17.5 here (a slower car is further back after 2 s)
    assert m['exit_dv'] == pytest.approx((17.5 - 19.0) * 3.6, abs=0.3)


def test_hardly_any_cornering_load_is_never_quoted_as_grip_left():
    m, d = check(run([(430.0, 28.0), (500.0, 8.0), (600.0, 28.0)], peak=0.1), 'OVER-SLOWED')
    assert 'had hardly any cornering load there' in wording(d) and 'was left' not in wording(d)
    assert 'of the grip there (your best' not in wording(d)


def test_no_sentence_names_the_line_a_flick_or_the_handbrake_unless_it_was_measured():
    slow = [(430.0, 28.0), (500.0, 8.0), (600.0, 28.0)]
    cases = [REF, run([(430.0, 28.0), (500.0, 12.0), (600.0, 28.0)]), run(slow), run(slow, peak=1.3),
             run(slow, peak=0.05, brake=no_brake), run([(380.0, 28.0), (450.0, 11.0), (500.0, 10.0), (600.0, 28.0)]),
             run([(450.0, 28.0), (520.0, 8.0), (620.0, 28.0)], peak=1.3),
             run([(200.0, 22.0), (430.0, 22.0), (500.0, 9.0), (600.0, 28.0)]),
             run([(300.0, 28.0), (430.0, 26.9), (500.0, 10.0), (600.0, 28.0)]),
             run([(430.0, 28.0), (500.0, 10.0), (600.0, 25.0), (800.0, 28.0)], gear=3.0),
             run(slow, steer=AGAINST)]
    seen = set()
    for arr in cases:
        m, d = diagnose(arr, run([(430.0, 28.0), (500.0, 10.0), (600.0, 28.0)], steer=WITH))
        seen.add(d['code'])
        text = wording(d)
        assert not re.search(r'\bline\b|flick|handbrake', text), text
    assert len(seen) >= 5
    # with positions the line is a measured fact and may be said
    m = dg.measures(run(slow, peak=1.3, xz=lambda d: (d, 0.0, -3.0)), run([(430.0, 28.0), (500.0, 10.0), (600.0, 28.0)],
                    xz=lambda d: (d, 0.0, 0.0)), dict(SEC, game='acr'), ENV)
    assert re.search(r'\bline\b', dg.diagnose(m)['fix'])


def test_a_section_the_traces_cannot_measure_has_no_diagnosis():
    short = potential.arrays(stage([(0.0, 28.0), (300.0, 28.0)]))               # ends before the section does
    assert dg.measures(short, REF, dict(SEC), ENV) is None
    assert dg.measures_section(short, REF, dict(SEC, corners=[KEY]), ENV) == []
    assert dg.diagnose_section([]) is None


# -- the potential's calls --

def row(available, **extra):
    base = {'d0': 100.0, 'd1': 200.0, 'apex_d': 150.0, 'dir': 'right', 'radius_m': 40.0, 'grade': '3', 'time': 8.0,
            'available': available, 'grip_s': 7.0, 'car_s': 6.5, 'grip_used': 0.5, 'cause': 'exit', 'apex_kmh': 60.0,
            'apex_kmh_pot': 70.0, 'gear': [3, 3]}
    base.update(extra)
    return base


def test_the_calls_say_brake_later_only_where_the_run_braked_before_the_grip_layer_needed_to():
    entry = row(1.4, cause='entry', brake_early=25.0, braked=True)
    assert potential.call(entry).endswith('about 1.4 s is there. Brake later and carry more speed to the turn-in: the grip layer starts braking 25 m after you.')
    # braked later than the layer, the apex slower than the grip allows and grip to spare: brake less, by how much
    late = row(1.4, cause='entry', brake_early=-20.0, braked=True, apex_kmh=60.0, apex_kmh_pot=70.0)
    said = potential.call(late)
    assert said.endswith('Brake less: carry 10 km/h more to the apex (the grip allows 70, you took 60).')
    assert 'Brake 20' not in said and 'later' not in said
    # no braking at all: lift less, never "brake less"
    lift = potential.call(row(1.4, cause='entry', braked=False, brake_early=None))
    assert lift.endswith('Lift less: carry 10 km/h more to the apex (the grip allows 70, you took 60).')
    # not slower at the apex, or no grip to spare: nothing the trace shows, no place (never the bare apex sentence)
    assert potential.call(row(1.4, cause='entry', brake_early=-20.0, braked=True, apex_kmh=69.0, apex_kmh_pot=70.0)) is None
    assert potential.call(dict(late, grip_used=0.9)) is None
    # a bend says it in the same words, with the phase where the fix agrees with the potential's own cause
    bend = row(1.3, cause='entry', grade='6', radius_m=145.0, brake_early=25.0, braked=True)
    assert potential.call(bend).endswith('about 1.3 s is there, mostly into the bend. Brake later and carry more speed to the turn-in: the grip layer starts braking 25 m after you.')
    assert potential.call(dict(bend, cause='exit')).endswith('about 1.3 s is there. Brake later and carry more speed to the turn-in: the grip layer starts braking 25 m after you.')
    far = potential.call(row(1.4, cause='entry', brake_early=90.0, braked=True))
    assert far.endswith('Brake later: you braked well before the grip needs (about 90 m).') and 'after you' not in far


def test_the_calls_say_full_throttle_sooner_only_where_it_was_not_already_full():
    flat = dict(apex_kmh=70.0)                                   # nothing to carry to the apex
    assert potential.call(row(1.5, exit_throttle=0.6, **flat)).endswith('Get to full throttle sooner after the apex.')
    assert potential.call(row(1.5, exit_throttle=1.0, **flat)) is None
    bend = row(1.3, grade='6', radius_m=145.0, exit_throttle=0.5, **flat)
    assert potential.call(bend).endswith('mostly on the exit. Get to full throttle sooner after the apex.')
    assert potential.call(dict(bend, exit_throttle=1.0)) is None


def test_a_diagnosis_with_a_fix_owns_the_cause_and_the_fix_in_one_sentence():
    diag = {'code': 'OVER-SLOWED',
            'fix': 'Brake less there, or not at all, as your best run did: carry 24 km/h more through the slowest point.'}
    said = potential.call(row(1.8, cause='exit', brake_early=25.0, braked=True), None, diag)
    assert said == ('The 3 right at 0.1 km: about 1.8 s is there, mostly into the bend. ' + diag['fix'])
    assert 'of the grip' not in said and 'Brake later' not in said            # not the potential's own cause or fix
    phases = {'EARLY-BRAKE': ', mostly into the bend', 'OVER-SLOWED': ', mostly into the bend',
              'OVERSHOT': ', mostly into the bend', 'COASTING': ', mostly into the bend',
              'LATE-THROTTLE': ', mostly on the exit', 'EXIT-BRAKE': ', mostly on the exit', 'GEAR': ', mostly on the exit',
              'EARLY-APEX': ', mostly on the exit', 'SLOW-ARRIVAL': ', mostly before the braking', 'SLIDE': '',
              'AT-LIMIT': ''}
    for code, phase in phases.items():
        assert potential.call(row(1.8), None, {'code': code, 'fix': 'Do it.'}) == \
            'The 3 right at 0.1 km: about 1.8 s is there{}. Do it.'.format(phase)
    # no fix from the diagnosis: the critique's sentence where there is one, else the trace's, else no place
    assert potential.call(row(1.8), 'Three seconds of counter-steer.', {'fix': None}).endswith('Three seconds of counter-steer.')
    assert potential.call(row(1.8, braked=True), None, {'fix': None}).endswith(
        'Brake less: carry 10 km/h more to the apex (the grip allows 70, you took 60).')
    assert potential.call(row(1.8, apex_kmh=None), None, {'fix': None}) is None


def test_the_brake_against_the_grip_layer_is_read_off_the_trace():
    ds = 2.0
    v_pot = np.concatenate([np.full(20, 40.0), np.linspace(40.0, 20.0, 21), np.full(20, 20.0)])    # braking from 20
    apex = 41
    brake = np.zeros(len(v_pot))
    brake[10:36] = 0.8                                             # the run braked from point 10: 20 m before the layer
    assert potential._braking_vs_layer(brake, v_pot, apex, 40, 0, ds) == (True, pytest.approx(20.0))
    brake[:] = 0.0
    brake[30:36] = 0.8                                             # 20 m after the layer's
    assert potential._braking_vs_layer(brake, v_pot, apex, 40, 0, ds) == (True, pytest.approx(-20.0))
    assert potential._braking_vs_layer(np.zeros(len(v_pot)), v_pot, apex, 40, 0, ds) == (False, None)


def test_the_gear_term_is_quoted_only_when_the_pass_lost_time_after_the_slowest_point():
    s = row(1.0, gear=[2, 3], exit_rpm=6000.0, exit_throttle=1.0)
    lost = {'t_exit': 0.3, 'gear_exit_x': 2, 'gear_exit_r': 3}
    [(cost, text)] = potential.critique(s, 3, None, measures=lost)
    assert cost == pytest.approx(0.3) and 'in 2nd at the apex where your fastest pass was in 3rd' in text
    assert 'you lost 0.3 s after the slowest point' in text and 'costs about' not in text
    assert potential.critique(s, 3, None, measures=dict(lost, t_exit=0.05)) == []
    assert potential.critique(s, 3, None, measures=dict(lost, gear_exit_x=3)) == []
    assert potential.critique(s, 3, None) == []                    # nothing measured against the quickest pass: not quoted


# -- the coach's selection --

def item(n, loss, code=None, fix='fix'):
    out = {'n': n, 'loss': loss}
    if code:
        out['diag'] = {'code': code, 'fix': fix}
    return out


def test_a_slow_arrival_is_not_a_tip_of_its_own_when_the_section_before_lost_too():
    lost = [item(5, 1.0, 'SLOW-ARRIVAL'), item(4, 0.8, 'OVER-SLOWED'), item(9, 0.5, 'SLOW-ARRIVAL')]
    assert [it['n'] for it in coach._worth_saying(lost)] == [4, 9]


def test_a_diagnosis_with_no_fix_is_never_a_tip():
    unclear = item(2, 1.0, 'UNCLEAR', None)
    limit = item(3, 0.9, 'AT-LIMIT', None)
    slide = item(4, 0.4, 'SLIDE')
    assert [it['n'] for it in coach._worth_saying([unclear, limit, slide])] == [4]
    assert [it['n'] for it in coach._worth_saying([unclear, limit])] == []                   # notes, not tips
    assert [it['n'] for it in coach._unexplained([unclear, limit, slide])] == [2, 3]
    assert [it['n'] for it in coach._worth_saying([unclear, item(7, 0.3)])] == [7]          # no traces: the old action is one


def test_the_patterns_count_only_the_sections_diagnosed_as_such(tmp_path):
    from tests.test_coach_places import Stage
    h = Stage(tmp_path / 'p.db')
    c = coach.Coach(h.store)

    def named(d, code, throttle=0.5, coast=0.4):
        return {'d': d, 'name': 'the corner at {:.0f}'.format(d), 'exit': 0.5, 'entry': 0.5, 'loss': 0.5, 'section': {'corners': [{'d': d}], 'apex': d},
                'diag': {'code': code, 'm': {'coast_ds': coast}},
                'compare': {'throttle': throttle, 'coast_entry': coast, 'overlap_exit': 0.0}}
    run_row = {'surface': 'tarmac', 'discipline': 'rally-stage', 'id': 1}
    items = [named(100.0, 'LATE-THROTTLE'), named(200.0, 'OVER-SLOWED'), named(300.0, 'LATE-THROTTLE'),
             named(400.0, 'COASTING')]
    cands = []
    claimed = c._patterns('s', 'Stage', run_row, items, 'your best run', 'ref', cands, None)
    late = [t for t in cands if t.id.startswith('throttle.late')]
    assert len(late) == 1 and late[0].count == 2 and claimed[100.0] >= {'late-throttle', 'LATE-THROTTLE'}
    assert 200.0 not in claimed
    [coast] = [t for t in cands if t.id.startswith('coast.entry')]
    assert coast.count == 1 and 'COASTING' in claimed[400.0]
