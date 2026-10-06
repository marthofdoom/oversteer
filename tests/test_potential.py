"""Potential time (oversteer/potential.py): the lap simulation on a synthetic stage against the arithmetic, the
layers' order, the top 3 and its words, the stored potential, and a real ACR run replayed through the whole path."""
import math
import os

import numpy as np
import pytest

from oversteer import coach, potential
from oversteer.telemetry_store import TRACE_CHANNELS, open_store

CAPTURE = os.path.join(os.path.dirname(__file__), 'data', 'acr-elatia-run.ovcap.gz')
NAN = float('nan')
G = potential.G


def flat_env(lat=10.0, brake=8.0, drive=4.0):
    """An envelope that does not change with speed: lat, braking and drive in m/s^2."""
    bins = np.arange(0, 60, potential.VBIN) + potential.VBIN / 2
    return {'bins': bins, 'lat': np.full(len(bins), lat), 'brake': np.full(len(bins), brake),
            'drive': np.full(len(bins), drive)}


def course(radius=50.0, straight=400.0, corner=200.0, after=400.0):
    """A straight, one constant-radius left corner, a straight: the grid and its curvature."""
    grid = np.arange(0.0, straight + corner + after, potential.DS)
    k = np.where((grid >= straight) & (grid < straight + corner), 1.0 / radius, 0.0)
    return grid, k


def test_the_simulation_on_a_corner_matches_the_arithmetic():
    """Drive 4 m/s^2, braking 8, lateral 10, a left corner of 50 m (limit sqrt(10 * 50) = 22.36 m/s) after 400 m:
    the car accelerates from 0.5 m/s, brakes to the corner's limit where the two meet, holds the limit through
    the corner (all the lateral grip is in use: no drive) and accelerates away, capped at 60 m/s."""
    grid, k = course()
    v, vlim = potential.simulate(k, flat_env())
    limit = math.sqrt(10.0 * 50.0)
    corner = (grid >= 400) & (grid < 600)
    assert vlim[corner] == pytest.approx(limit, abs=0.01)
    # through the corner it stays at the limit
    assert v[(grid >= 410) & (grid < 590)] == pytest.approx(limit, abs=0.05)
    # the forward pass (0.25 + 8 s) meets the backward one (limit^2 + 16 (400 - s)) at 287.5 m
    meet = math.sqrt(0.25 + 8 * 287.5)
    assert v[int(287.5 / potential.DS)] == pytest.approx(meet, abs=0.4)
    assert v.max() == pytest.approx(60.0, abs=0.01)
    # the time: accelerate, brake, corner, accelerate to 60, hold 60
    t = (meet - 0.5) / 4 + (meet - limit) / 8 + 200 / limit + (60 - limit) / 4 + (400 - (60 ** 2 - limit ** 2) / 8) / 60
    assert potential.cumulative(v)[-1] == pytest.approx(t, abs=0.35)


def test_more_grip_is_less_time_and_the_floor_holds_the_corner_limit_up():
    grid, k = course()
    slow = potential.cumulative(potential.simulate(k, flat_env(lat=8.0, brake=6.0, drive=3.0))[0])[-1]
    fast = potential.cumulative(potential.simulate(k, flat_env(lat=12.0, brake=10.0, drive=5.0))[0])[-1]
    assert fast < slow
    # a run that was faster through the corner than the envelope says: the limit is never below it
    floor = np.where((grid >= 400) & (grid < 600), 30.0, 0.0)
    v, vlim = potential.simulate(k, flat_env(), floor=floor)
    assert vlim[(grid >= 400) & (grid < 600)] == pytest.approx(30.0)
    assert potential.cumulative(v)[-1] < potential.cumulative(potential.simulate(k, flat_env())[0])[-1]


def test_sections_are_cut_between_apexes_and_never_short():
    grid = np.arange(0.0, 3000.0, potential.DS)
    k = np.zeros_like(grid)
    for centre in (400, 900, 1500, 1560, 2300):                 # 1500 and 1560: one complex, not two sections
        k += 0.02 * np.exp(-((grid - centre) / 25.0) ** 2)
    cuts = potential.sections(grid, k)
    assert cuts[0][0] == 0 and cuts[-1][1] == len(grid) - 1
    assert all(b == c for (_a, b), (c, _d) in zip(cuts, cuts[1:]))                 # contiguous
    assert all(grid[b] - grid[a] >= potential.MIN_SECTION for a, b in cuts)
    index = lambda x: next(i for i, (a, b) in enumerate(cuts) if grid[a] <= x < grid[b])
    assert len({index(x) for x in (400, 900, 1500, 2300)}) == 4


def drive_rows(grid, v, k, dt=0.1):
    """The 10 Hz trace rows (TRACE_CHANNELS) of a car that drove speed profile `v` along the grid."""
    s = np.cumsum(np.r_[0.0, 2 * potential.DS / (v[1:] + v[:-1])])           # the clock at each grid point
    times = np.arange(0.0, s[-1], dt)
    d = np.interp(times, s, grid)
    speed = np.interp(times, s, v)
    kk = np.interp(d, grid, k)
    a_long = np.gradient(speed, times)
    rows = []
    for i, t in enumerate(times):
        row = dict.fromkeys(TRACE_CHANNELS, NAN)
        row.update(t=t, distance=d[i], speed=speed[i], rpm=4000.0 + 30 * speed[i], gear=3.0,
                   throttle=1.0 if a_long[i] > 0.2 else 0.0, brake=1.0 if a_long[i] < -1.0 else 0.0,
                   a_long=a_long[i], a_lat=speed[i] ** 2 * kk[i], yaw_rate=speed[i] * kk[i])
        rows.append(tuple(row[c] for c in TRACE_CHANNELS))
    return rows


def synthetic_runs(count=4, share=0.8):
    """Runs of a driver who uses `share` of the grip of flat_env() on a course with two corners, with a little
    variation from run to run."""
    grid = np.arange(0.0, 1600.0, potential.DS)
    k = np.zeros_like(grid)
    k += np.where((grid >= 400) & (grid < 560), 1 / 40.0, 0.0) + np.where((grid >= 1000) & (grid < 1200), -1 / 70.0, 0.0)
    runs = []
    for n in range(count):
        s = share + 0.02 * (n % 2)
        env = flat_env(lat=10.0 * s, brake=8.0 * s, drive=4.0 * s)
        v, _ = potential.simulate(k, env)
        arr = potential.arrays(drive_rows(grid, v, k))
        runs.append({'id': n + 1, 'class': 'clean', 'finished': True, 'arr': arr, 'mine': True, 'bad': []})
    return runs


def test_the_three_layers_come_in_order_on_synthetic_runs():
    from oversteer import car_data
    runs = synthetic_runs()
    cols = [potential.envelope_rows(r['arr']) for r in runs]
    data = car_data.entry('acr/Hyundai i20N Rally2')
    assert data is not None
    pot = potential.stage(runs, data, 'gravel', False, cols, len(cols), sob=None)
    assert pot is not None and pot['runs'] == 4
    user, grip, car = pot['user_s'], pot['grip_s'], pot['car_s']
    assert user >= grip >= car > 0
    layers = potential.layers(pot)
    assert layers['user'] >= layers['grip'] >= layers['car']
    # per section: the grip layer is never slower than the best pass, the car never slower than the grip layer
    for s in pot['sections']:
        assert s['grip_s'] <= s['user_s'] + 0.01
        assert s['car_s'] <= s['grip_s'] + 0.2
    # the splits' sum of best stands in for the user layer, and a layer out of order is brought into it
    assert potential.layers(dict(pot, grip_s=user + 5.0, car_s=user + 9.0), sob=user)['car'] <= user
    # without enough runs for the envelope: nothing to say
    assert potential.stage(runs, data, 'gravel', False, cols[:2], 2) is None


def test_the_grip_layer_is_a_driver_at_the_edge_of_what_he_showed():
    """A driver at 80 % of the grip all the time: the potential from his own envelope is about his time (nothing
    more is claimed than he reached), and a driver slower in one corner has the time there."""
    runs = synthetic_runs()
    cols = [potential.envelope_rows(r['arr']) for r in runs]
    pot = potential.stage(runs, None, 'gravel', False, cols, len(cols))
    best = min(r['arr']['t'][-1] for r in runs)
    assert pot['car_s'] is None                                            # no shipped data: no car layer
    assert pot['grip_s'] == pytest.approx(best, rel=0.06)
    # a run through the first corner 15 % slower: the time available there, and not in the other corner
    grid = np.arange(0.0, 1600.0, potential.DS)
    k = np.where((grid >= 400) & (grid < 560), 1 / 40.0, 0.0) + np.where((grid >= 1000) & (grid < 1200), -1 / 70.0, 0.0)
    v, _ = potential.simulate(k, flat_env(lat=8.2, brake=6.56, drive=3.28))
    v = np.where((grid >= 330) & (grid < 640), v * 0.85, v)
    slow = potential.arrays(drive_rows(grid, v, k))
    rows = potential.analyse_run(pot, slow)
    corner = next(r for r in rows if r['d0'] <= 480 < r['d1'])
    other = next(r for r in rows if r['d0'] <= 1100 < r['d1'])
    assert corner['available'] > 0.6 and corner['available'] > 3 * abs(other['available'])
    assert corner['dir'] == 'left' and other['dir'] == 'right'
    assert corner['cause'] in ('entry', 'apex', 'exit')


def row(available, n=1, **extra):
    base = {'d0': 100.0 * n, 'd1': 100.0 * n + 100, 'apex_d': 100.0 * n + 50, 'dir': 'right', 'radius_m': 40.0,
            'grade': '3', 'time': 8.0, 'available': available, 'grip_s': 7.0, 'car_s': 6.5, 'grip_used': 0.5,
            'cause': 'exit', 'apex_kmh': 60.0, 'apex_kmh_pot': 70.0, 'gear': [3, 3]}
    base.update(extra)
    return base


def test_top3_takes_the_biggest_three_and_leaves_out_launch_off_and_small_ones():
    rows = [row(2.0, 0), row(0.4, 1), row(1.5, 2), row(None, 3, time=None), row(0.9, 4), row(1.1, 5), row(0.1, 6)]
    top = potential.top3(rows)
    assert [r['available'] for r in top] == [1.5, 1.1, 0.9]                  # the first section (launch) is not one
    assert potential.top3(rows, count=2) == top[:2]
    assert potential.top3([row(0.2, 1), row(0.1, 2)]) == []
    off = dict(row(3.0, 3), time=None, available=None)
    assert potential.top3([row(0.5, 0), row(1.0, 1), off]) == [row(1.0, 1)]


def test_what_improved_is_the_place_that_closed_most_on_the_potential():
    before = [row(1.5, 0), row(1.2, 1), row(0.5, 2)]
    now = [row(1.4, 0), row(0.3, 1), row(0.4, 2)]
    found, gain = potential.improved(now, before)
    assert found['d0'] == 100.0 and gain == pytest.approx(0.9)
    assert potential.improved(now, [row(0.5, 0), row(0.5, 1), row(0.5, 2)]) is None


def test_the_calls_are_in_the_coachs_words():
    corner = row(1.5, 13, apex_d=1300.0, grip_used=0.49)
    assert potential.call(corner) == ('The 3 right at 1.3 km: you use 49 % of the grip; about 1.5 s is there. '
                                      'Get to full throttle sooner after the apex.')
    assert potential.call(row(1.4, 1, cause='entry', grip_used=0.79, grade='4', dir='left', apex_d=2100.0)) == (
        'The 4 left at 2.1 km: you use 79 % of the grip; about 1.4 s is there. '
        'Brake later and carry the speed to the turn-in.')
    # at a kink in a fast section the grip is not quoted, at the limit it is "all"
    assert 'of the grip' not in potential.call(row(0.8, 1, grip_used=0.08))
    assert 'you use all of the grip' in potential.call(row(0.8, 1, grip_used=1.1))
    # a long bend is a bend: where the time is, no grip figure
    bend = row(1.3, 1, grade='6', radius_m=145.0, apex_d=4200.0, dir='left')
    assert potential.call(bend) == 'The 6 left at 4.2 km: about 1.3 s is there, mostly on the exit: full throttle sooner and longer.'
    assert potential.call(dict(bend, cause='entry')).endswith('mostly into the bend: brake later.')
    apex = potential.call(row(0.9, 1, cause='apex', apex_kmh=75.0, apex_kmh_pot=96.0))
    assert 'The grip allows about 96 km/h at the apex, you took 75.' in apex


def test_the_corner_critique_names_only_what_the_pass_shows_and_costs_enough():
    s = row(1.0, 1, gear=[2, 3], exit_rpm=6000.0, exit_throttle=1.0)
    terms = potential.critique(s, 3, None)
    assert len(terms) == 1 and terms[0][0] == pytest.approx(0.17)
    assert 'in 2nd at the apex where your fastest pass was in 3rd' in terms[0][1]
    # the same gear, or a longer one (0.11 s: under the bar): nothing
    assert potential.critique(row(1.0, 1, gear=[3, 3]), 3, None) == []
    assert potential.critique(row(1.0, 1, gear=[4, 4]), 3, None) == []
    # revs under the band on the exit with the throttle down
    low = row(1.0, 1, gear=[3, 3], exit_rpm=3000.0, exit_throttle=0.95)
    assert 'under the power band on the exit (3000 rpm, it starts at 4000)' in potential.critique(low, 3, (4000, 6500))[0][1]
    assert potential.critique(dict(low, exit_throttle=0.4), 3, (4000, 6500)) == []
    # counter-steer: 0.13 s per second, said from 1.2 s
    assert potential.critique(row(1.0, 1, counter_steer_s=0.8), 3, None) == []
    assert 'counter-steer' in potential.critique(row(1.0, 1, counter_steer_s=1.4), 3, None)[0][1]
    # a critique term stands in for the general advice
    assert potential.call(s, terms[0][1]).endswith(terms[0][1]) and 'Get to full throttle' not in potential.call(s, terms[0][1])


def test_the_power_band_is_where_the_torque_is_nine_tenths_of_the_peak():
    from oversteer import car_data
    data = car_data.entry('acr/Hyundai i20N Rally2')
    low, high = potential.power_band(data)
    peak = max(t for _r, t in data['torque_curve'])
    assert car_data.torque(data, low) >= 0.9 * peak - 1 and 1000 < low < high <= data['limiter_rpm'] + 500


def test_the_store_keeps_a_potential_and_serves_it_back(tmp_path):
    store = open_store(str(tmp_path / 't.db'))
    car = store.car_id('p', 'acr/Hyundai i20N Rally2', 'acr')
    assert store.potential('acr:test', car) is None and store.envelope(car, 'gravel') is None
    pot = {'runs': 4, 'ds': 2.0, 'pb_s': 100.0, 'user_s': 99.0, 'grip_s': 90.0, 'car_s': 85.0,
           'sections': [{'d0': 0, 'd1': 500, 'grip_s': 20.0}], 'profile': {'ds': 2.0, 'v_grip': [1.0, 2.0]}}
    store.save_potential('acr:test', car, 'v1', 12.0, pot)
    store.save_envelope(car, 'gravel', 'acr', 'e1', 4, 12.0, {'grip': {'lat': [1.0]}})
    store.commit()
    found = store.potential('acr:test', car)
    assert found['version'] == 'v1' and found['grip_s'] == 90.0 and found['sections'][0]['d1'] == 500
    assert found['profile']['v_grip'] == [1.0, 2.0] and found['built'] == 12.0
    assert store.envelope(car, 'gravel')['data']['grip']['lat'] == [1.0]
    store.save_potential('acr:test', car, 'v2', 13.0, dict(pot, grip_s=80.0))              # replaced, not added
    assert store.potential('acr:test', car)['version'] == 'v2'
    assert potential.stored(store, 'acr:test', car)['grip_s'] == 80.0
    assert potential.stored(store, 'acr:other', car) is None
    store.close()


@pytest.mark.skipif(not os.path.exists(CAPTURE), reason='no capture')
def test_a_real_acr_run_through_the_whole_path(tmp_path):
    """Greece Elatia (a real run, 243 s) replayed three times into one database: after each finished run the
    potential is stored (the post-run hook in coach.stage_metrics), the layers are in order, the splits carry them
    and the time available per split, and the coach says its top three places for the run, every time."""
    from oversteer.shift_learner import ShiftLearner
    from oversteer.telemetry_capture import read_capture, replay
    from oversteer.telemetry_store import open_reader

    path = str(tmp_path / 'telemetry.db')
    learner = ShiftLearner(path)
    for n in range(3):
        meta, records = read_capture(CAPTURE)
        replay(records, learner, started=meta['started'] + 3600.0 * n)
    learner.close()
    reader = open_reader(path)
    car, profile = reader._rows('SELECT id, profile FROM cars')[0]
    pot = reader.potential('acr:greece:elatia', car)
    assert pot is not None and pot['runs'] == 3
    layers = potential.layers(pot)
    assert 150.0 < layers['car'] <= layers['grip'] < layers['user'] <= 244.0
    assert pot['sections'] and all(s['grip_s'] <= s['user_s'] + 0.02 for s in pot['sections'] if s['user_s'])
    assert reader.envelope(car, 'gravel')['runs'] == 3
    found = coach.splits(reader, profile, car)
    assert found['potential']['grip'] == pytest.approx(layers['grip']) and found['potential']['user'] == found['possible']
    assert all(r['grip_s'] > 0 and r['available'] is not None for r in found['splits'] if r['last'] is not None)
    assert sum(r['grip_s'] for r in found['splits']) == pytest.approx(layers['grip'], abs=1.5)
    tips = coach.Coach(reader).tips(profile, car)
    top = [t for t in tips if t.kind == 'top3']
    assert [t.rank for t in top] == [1, 2, 3] and tips[:3] == top             # said first, every run
    assert all(' s is there' in t.text and t.text.startswith('On Elatia, ') for t in top)
    avail = [float(t.text.split('about ')[1].split(' s')[0]) for t in top]
    assert avail == sorted(avail, reverse=True) and avail[-1] >= potential.AVAILABLE_MIN
    assert top[0].to_dict()['rank'] == 1 and top[0].place['stage'] == 'acr:greece:elatia'
    # one place, one name and one time wherever it is quoted: the tips say the splits' sections, with the splits
    # sheet's seconds available, and the user layer is the splits' sum of best
    from oversteer import run_analysis
    by_name = {r['name']: r for r in found['splits']}
    for t in top:
        name = t.text[len('On Elatia, '):].split(': ')[0]
        assert name in by_name and by_name[name]['available'] is not None
        assert float(t.text.split('about ')[1].split(' s')[0]) == pytest.approx(by_name[name]['available'], abs=0.06)
    assert coach._clock(found['possible']) in top[0].evidence[-1]
    latest = reader.run(max(r['id'] for r in reader.stage_runs('acr:greece:elatia', car=car)))
    assert run_analysis.stage_potential(reader, latest)['user'] == pytest.approx(found['possible'])
    # recomputing with the same runs changes nothing (the version holds), and a forced one gives the same numbers
    from oversteer.telemetry_store import open_store
    store = open_store(path)
    again = potential.recompute(store, 'acr:greece:elatia', car)
    assert again['version'] == pot['version'] and again['built'] == pot['built']
    forced = potential.recompute(store, 'acr:greece:elatia', car, force=True)
    assert forced['grip_s'] == pytest.approx(pot['grip_s'])
    # a run timed to the stop control ('partial') is no run to say time of
    newest = max(r['id'] for r in store.stage_runs('acr:greece:elatia', car=car))
    store.update_run(newest, run_class='partial')
    store.commit()
    assert not [t for t in coach.Coach(open_reader(path)).tips(profile, car) if t.kind == 'top3']
    store.close()


def _cols(runs):
    return [potential.envelope_rows(r['arr']) for r in runs]


def test_a_partial_run_and_the_trace_past_the_road_do_not_make_the_stage_longer():
    """A run timed to the stop control ('partial') holds the slow-down: not among the runs the road, the times and the
    floor come from; the grid stops at the road's length where the table knows it."""
    runs = synthetic_runs()
    cols = _cols(runs)
    pot = potential.stage(runs, None, 'gravel', False, cols, len(cols))
    longer = potential.stage(runs, None, 'gravel', False, cols, len(cols), road_m=1200.0)
    assert longer['length'] == 1200.0 and longer['sections'][-1]['d1'] <= 1200 and pot['length'] > 1500
    slow = [dict(r, id=10 + n, **{'class': 'partial'}) for n, r in enumerate(runs)]
    assert potential.stage(slow, None, 'gravel', False, cols, len(cols)) is None
    # extra partial runs, longer than the clean ones, change nothing
    stretched = [dict(r, id=20 + n, arr=dict(r['arr'], d=r['arr']['d'] * 1.5), **{'class': 'partial'})
                 for n, r in enumerate(runs)]
    again = potential.stage(runs + stretched, None, 'gravel', False, cols, len(cols))
    assert again['length'] == pot['length'] and again['runs'] == pot['runs']


def test_the_last_section_of_a_stage_with_no_flying_finish_is_no_place_for_time():
    runs = synthetic_runs()
    pot = potential.stage(runs, None, 'gravel', False, _cols(runs), len(runs), tail=True)
    assert pot['sections'][-1].get('tail') is True and not pot['sections'][0].get('tail')
    rows = [row(1.0, 0), row(0.9, 1), row(2.0, 2, tail=True)]
    assert [r['available'] for r in potential.top3(rows)] == [0.9]


def test_a_stage_with_runs_and_no_potential_gets_one_on_the_backfill_tick(tmp_path):
    """Stages driven before the potential existed (or re-classed since) get theirs from the backfill, one pair a tick,
    without waiting for the next finished run."""
    from oversteer.shift_learner import ShiftLearner
    from oversteer.telemetry_capture import read_capture, replay
    from oversteer.telemetry_store import open_reader

    path = str(tmp_path / 'telemetry.db')
    learner = ShiftLearner(path)
    for n in range(3):
        meta, records = read_capture(CAPTURE)
        replay(records, learner, started=meta['started'] + 3600.0 * n)
    learner.backfill()
    store = learner.log.store
    car = store.potentials_missing() == [] and store._rows('SELECT id FROM cars')[0][0]
    store.db.execute('DELETE FROM stage_potential')
    store.commit()
    assert [(s, c) for s, c, _r in store.potentials_missing()] == [('acr:greece:elatia', car)]
    assert learner.backfill() == 1 and learner.backfill() == 0                 # built once, not tried over and over
    assert open_reader(path).potential('acr:greece:elatia', car) is not None and not store.potentials_missing()
    learner.close()


def test_on_splits_puts_the_potentials_sections_on_the_splits_bounds(monkeypatch):
    found = {'best': {'bounds': [(0.0, 300.0), (300.0, 700.0), (700.0, 1000.0)], 'grid': [{'apex': 100.0}] * 3,
                      'per_run': {7: {0: 20.0, 1: 30.0, 2: 15.0}}}}
    pot = {'profile': {'ds': 2.0, 'v_grip': [20.0] * 501}, 'sections': [{}, {}, {'tail': True}]}
    rows = [row(1.0, 0, d0=0.0, d1=250.0, apex_d=100.0), row(0.5, 1, d0=250.0, d1=500.0, apex_d=400.0),
            row(1.5, 2, d0=500.0, d1=1000.0, apex_d=800.0)]
    monkeypatch.setattr(potential.cc, 'section_name', lambda s: 'split at {:.0f}'.format(s['apex']))
    out = potential.on_splits(pot, rows, found, 7)
    assert [r['name'] for r in out] == ['split at 100'] * 3 and [(r['d0'], r['d1']) for r in out] == [
        (0.0, 300.0), (300.0, 700.0), (700.0, 1000.0)]
    assert [r['time'] for r in out] == [20.0, 30.0, 15.0] and [r['split'] for r in out] == [0, 1, 2]
    assert out[1]['grip_s'] == pytest.approx(400.0 / 20.0) and out[1]['available'] == pytest.approx(10.0)
    assert out[2]['tail'] and not out[0]['tail']
    assert [r['split'] for r in potential.top3(out)] == [1]                   # not the launch's, not the slow-down's
    assert potential.on_splits(pot, rows, found, 7, bad=[(650.0, 650.0)])[1]['time'] is None


def _path_arr(radius, straight=150.0, turn=math.pi, speed=10.0):
    """A run's arrays along a straight, an arc of `radius` through `turn`, a straight: positions on ACR's axes
    (x, y up, -north), 10 Hz rows at `speed`."""
    arc = radius * turn
    total = 2 * straight + arc
    rows = []
    for i in range(int(total / speed / 0.1)):
        s = i * speed * 0.1
        if s < straight:
            east, north, _h = s, 0.0, 0.0
        elif s < straight + arc:
            a = (s - straight) / radius
            east, north = straight + radius * math.sin(a), radius * (1 - math.cos(a))
            _h = a
        else:
            east, north = straight + radius * math.sin(turn) + (s - straight - arc) * math.cos(turn), \
                radius * (1 - math.cos(turn)) + (s - straight - arc) * math.sin(turn)
        row = dict.fromkeys(TRACE_CHANNELS, NAN)
        row.update(t=i * 0.1, distance=s, speed=speed, x=east, y=1.0, z=-north, gear=2.0)
        rows.append(tuple(row[c] for c in TRACE_CHANNELS))
    return potential.arrays(rows), total


@pytest.mark.parametrize('radius', (5.5, 12.0, 60.0))
def test_the_curvature_reads_a_hairpins_radius_not_the_smoothing_windows(radius):
    """Smoothed over 20 m, a hairpin of 5.5 m radius (a turn of 17 m) read 8.8 m: the grip layer allowed a speed that
    no car could turn it at. Against a circle fit of the same positions, the radius reads within 12 %."""
    arr, total = _path_arr(radius)
    grid = np.arange(0.0, total, potential.DS)
    k = potential.curvature(arr, grid, 'acr')
    peak = np.nanmax(np.abs(k))
    assert 1 / peak == pytest.approx(radius, rel=0.12)
    assert k[np.nanargmax(np.abs(k))] > 0                                      # the arc bends north: a left turn
