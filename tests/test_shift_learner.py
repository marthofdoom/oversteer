import math
from oversteer.shift_learner import ShiftLearner, CarModel
from oversteer.telemetry import Sample
from tests.sim import LIMITER, RATIOS, power, analytic_shift, drive, cruise, exits

def test_learns_the_best_shift_per_gear(tmp_path):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    drive(learner)
    exits(learner)
    car = learner.car
    for gear in (1, 2, 3, 4):
        best, coverage = car.best_shift(gear)
        assert abs(best - analytic_shift(gear)) <= 150, (gear, best, analytic_shift(gear))
        assert coverage > 0.6
    assert abs(car.ratio(3) - RATIOS[3]) < 0.5


def test_records_where_the_driver_changes_up(tmp_path):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    drive(learner, shift_at=6000, runs=2)
    average, count = learner.car.average_upshift(2)
    assert count == 2 and 5950 <= average <= 6100


def test_car_profiles_persist_and_switch(tmp_path):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    drive(learner, runs=2)
    exits(learner)
    learner.idle()                                  # telemetry stopped: saved
    again = ShiftLearner(str(tmp_path / 'telemetry.db'))
    drive(again, runs=0, car='test-car')
    again.feed(0.0, Sample(3000, LIMITER, gear=1, speed=30, car='test-car'), LIMITER, 0.0, 0.0)
    assert again.car.best_shift(2) is not None     # picked up where it left off
    again.feed(1.0, Sample(3000, LIMITER, gear=1, speed=30, car='other'), LIMITER, 0.0, 0.0)
    assert again.car.key == 'other' and again.car.best_shift(2) is None
    again.save()                                   # a moment in a car that taught nothing: not kept
    assert {k for k, _ in again.known_cars()} == {'test-car'}
    again.forget('test-car')                       # learning starts over; the history stays
    assert {k for k, _ in again.known_cars()} == {'test-car'}
    # (two sessions: the pulls and the corner exits are 15 minutes apart)
    assert again.load_snapshot('test-car')['gears'] == [] and len(again.history('test-car')) == 2


def test_wheelspin_does_not_teach_power(tmp_path):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    exits(learner)                                  # ratios known
    learner.car.power = {}
    t = 5000.0
    speed = 8.0
    while speed < 14:                                # 2nd gear, revs 15 % over the ratio: spinning
        speed += 3.0 / 60
        t += 1 / 60
        learner.feed(t, Sample(RATIOS[2] * speed * 1.15, LIMITER, gear=2, speed=speed, car='test-car'),
                     LIMITER, 1.0, 0.0)
    assert learner.car.power == {}


def test_a_retuned_gear_is_relearnt(tmp_path):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    exits(learner)
    t = 9000.0
    for i in range(120):                             # cruising in a longer 3rd
        t += 1 / 60
        learner.feed(t, Sample(230.0 * 25, LIMITER, gear=3, speed=25.0, car='test-car'), LIMITER, 0.3, 0.0)
    assert abs(learner.car.ratio(3) - 230.0) < 1 and 3 in learner.car.retuned
    assert learner.car.top_seen == 0.0                 # where the data ended belonged to the old gearing


def test_spin_first_on_gravel(tmp_path):
    """400 full-throttle samples of 2nd at a steady 6 % wheelspin before any
    clean one: the ratio is still learnt right and no re-tune fires."""
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    t, speed = 0.0, 12.0
    learner.feed(t, Sample(RATIOS[2] * speed, LIMITER, gear=2, speed=speed, car='test-car'), LIMITER, 1.0, 0.0)
    for _ in range(400):
        t += 1 / 60
        speed += 2.0 / 60
        learner.feed(t, Sample(RATIOS[2] * speed * 1.06, LIMITER, gear=2, speed=speed, car='test-car'),
                     LIMITER, 1.0, 0.0)
    assert learner.car.ratio(2) is None and learner.car.power == {}
    t = cruise(learner, t, 2, speed)                   # then part throttle, clean
    assert abs(learner.car.ratio(2) - RATIOS[2]) < RATIOS[2] * 0.01
    for _ in range(400):                               # and spinning again, flat out
        t += 1 / 60
        learner.feed(t, Sample(RATIOS[2] * speed * 1.06, LIMITER, gear=2, speed=speed, car='test-car'),
                     LIMITER, 1.0, 0.0)
    assert abs(learner.car.ratio(2) - RATIOS[2]) < RATIOS[2] * 0.01
    assert learner.car.retuned == {} and learner.car.power == {}


def test_braking_teaches_no_ratio(tmp_path):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    t, speed = 0.0, 25.0
    for _ in range(120):                               # locking the wheels: engine slower than the road says
        t += 1 / 60
        sample = Sample(RATIOS[3] * speed * 0.8, LIMITER, gear=3, speed=speed, car='test-car')
        sample.brake = 0.7
        learner.feed(t, sample, LIMITER, 0.0, 0.0)
    assert learner.car.ratio(3) is None


def test_coaching_says_early_or_late(tmp_path):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    exits(learner)
    drive(learner, shift_at=5200, runs=3)            # well short of ~6700
    tips = learner.snapshot()['advice']
    assert any(t.startswith('2→3') and 'early' in t and '% less drive' in t for t in tips), tips
    learner.car.upshifts = {}
    drive(learner, shift_at=LIMITER, runs=3)         # on the limiter every time
    tips = learner.snapshot()['advice']
    assert any(t.startswith('2→3') and 'late' in t for t in tips), tips


def test_sessions_keep_every_shift_and_how_it_was_made(tmp_path):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    exits(learner)
    drive(learner, shift_at=5600, runs=2)            # through neutral: an H-pattern box
    learner.idle()                                    # end of the stage
    history = learner.history('test-car')
    assert len(history) == 1 and history[0]['shifts'] == 8
    assert history[0]['methods'] == ['h-pattern'] and history[0]['error'] < -800
    again = ShiftLearner(str(tmp_path / 'telemetry.db'))
    assert again.known_cars() == [('test-car', 'test-car')]
    assert len(again.history('test-car')) == 1


def test_profiles_keep_their_own_cars(tmp_path):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'), profile='rally')
    exits(learner)
    learner.save()
    learner.set_profile('circuit')
    assert learner.known_cars() == []
    learner.set_profile('rally')
    assert [k for k, _ in learner.known_cars()] == ['test-car']


def test_shift_point_needs_confidence(tmp_path):
    car = CarModel('x')
    assert car.best_shift(1) is None
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    assert learner.shift_rpm(2) is None


def test_dirt_cars_learnt_in_the_wrong_unit_are_rescaled(tmp_path):
    """Before the decoder fix DiRT's rad/s were read as rpm / 10: a car with
    a 7500 rpm max was keyed 7854 and everything learnt was 30/pi too high."""
    import json
    import sqlite3
    from oversteer.telemetry_store import SCHEMA_V1 as SCHEMA
    path = str(tmp_path / 'telemetry.db')
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    f = math.pi / 3                                              # what the old decoder multiplied by
    dirt = CarModel('codemasters-7854-838-6', '7854 rpm, 6 gears')
    dirt.limiter = 7215.0                                        # launch-learnt, bouncing: not round
    dirt.top_seen = 7100.0
    dirt.ratios = {2: [330.0 * f] * 30}
    dirt.upshifts = {2: [6800.0 * f]}
    dirt.power = {int(6000 * f // 100): [100.0] * 5}
    wrcg = CarModel('codemasters-7000-900-5', 'my WRCG car')     # max round as read: left alone
    wrcg.limiter = 7000.0
    for car in (dirt, wrcg):
        db.execute('INSERT INTO cars (profile, key, name, model, updated) VALUES (?, ?, ?, ?, 0)',
                   ('rally', car.key, car.name, json.dumps(car.to_dict())))
    session = db.execute("INSERT INTO sessions (profile, car, started) VALUES ('rally', 'codemasters-7854-838-6', 0)").lastrowid
    db.execute('INSERT INTO shifts (session, at, gear, rpm, best) VALUES (?, 0, 2, ?, ?)', (session, 6800.0 * f, 7000.0 * f))
    db.commit()
    db.close()

    learner = ShiftLearner(path, profile='rally')
    assert dict(learner.known_cars()) == {'codemasters/7500-800-6': '7500 rpm, 6 gears',
                                          'codemasters/7000-900-5': 'my WRCG car'}
    snapshot = learner.load_snapshot('codemasters/7500-800-6')
    assert abs(snapshot['gears'][0]['ratio'] - 330.0) < 0.01
    fixed = learner.load_snapshot('codemasters/7000-900-5')
    assert fixed['limiter'] == 7000.0
    row = learner.db.execute('SELECT model FROM cars WHERE key = ?', ('codemasters/7500-800-6',)).fetchone()
    model = CarModel.from_dict(json.loads(row[0]))
    assert abs(model.limiter - 7215.0 / f) < 0.01 and abs(model.upshifts[2][0] - 6800.0) < 0.01
    assert list(model.power) in ([59], [60])                     # around 6000 rpm, not 6283
    assert abs(learner.history('codemasters/7500-800-6')[0]['error'] + 200.0) < 0.01
    assert learner.db.execute('PRAGMA user_version').fetchone()[0] == 2
    assert (tmp_path / 'telemetry.db.v0.bak').exists()
    learner.close()
    again = ShiftLearner(path, profile='rally')                 # done once only
    assert 'codemasters/7500-800-6' in dict(again.known_cars())
    # DiRT sends the car again: the old row is adopted, history and all
    again.feed(1.0, Sample(3000, 7500.0, gear=2, speed=10, car='dirt/7500-800-6', game='dirt'), 7500.0, 0.0, 0.0)
    assert abs(again.car.ratio(2) - 330.0) < 0.01
    again.save()
    assert 'dirt/7500-800-6' in dict(again.known_cars()) and len(again.history('dirt/7500-800-6')) == 1


def test_the_press_says_how_a_change_was_made():
    from oversteer.shift_learner import shift_method
    assert shift_method((9.8, 'sequential'), 10.0, 10.05, False) == 'sequential'
    assert shift_method((9.9, 'paddle'), 10.0, 10.3, True) == 'paddles'       # neutral shown, paddle pressed
    assert shift_method((10.2, 'gear'), 10.0, 10.3, True) == 'h-pattern'      # the new gear's button, in neutral
    assert shift_method((10.2, 'gear'), 10.0, 10.3, False) == 'h-pattern'     # an H-pattern with no neutral shown
    assert shift_method((8.0, 'sequential'), 10.0, 10.05, True) == 'h-pattern'  # an old press: the fallback
    assert shift_method(None, 10.0, 10.05, False) is None


def test_shifts_per_method(tmp_path):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    t = 0.0
    for method, shift_rpm, press in (('sequential', 6000.0, 'sequential'), ('paddles', 6400.0, 'paddle'),
                                     ('h-pattern', 5600.0, None)):
        for _ in range(3):
            speed = 5000.0 / RATIOS[2]
            for rpm in (5000.0, 5600.0, shift_rpm):
                t += 0.1
                learner.feed(t, Sample(rpm, LIMITER, gear=2, speed=rpm / RATIOS[2], car='test-car'), LIMITER, 1.0, 0.0)
            t += 0.05
            if press is None:                                    # through neutral, nothing pressed
                learner.feed(t, Sample(4000.0, LIMITER, gear=0, speed=speed, car='test-car'), LIMITER, 0.0, 1.0)
                t += 0.2
            learner.feed(t, Sample(4700.0, LIMITER, gear=3, speed=shift_rpm / RATIOS[2], car='test-car'),
                         LIMITER, 1.0, 0.0, press=(t - 0.1, press) if press else None)
            t += 1.0
    changed = learner.history_changed
    learner.idle()                                           # the last change is written once telemetry stops
    assert learner.history_changed == changed + 1
    methods = learner.method_shifts('test-car')
    assert {m: (round(v[0]), v[1]) for m, v in methods[2].items()} == {
        'sequential': (6000, 3), 'paddles': (6400, 3), 'h-pattern': (5600, 3)}


def test_coaching_is_short():
    """At most three tips, the biggest first, and one line of praise."""
    car = CarModel('x')
    car.limiter = LIMITER
    car.ratios = {g: [r] * 30 for g, r in {1: 480.0, 2: 330.0, 3: 250.0, 4: 200.0, 5: 165.0, 6: 140.0}.items()}
    for band in range(20, 80):
        car.power[band] = [power(band * 100 + 50)] * 5
    early = {g: analytic_shift(g) - 300 - 200 * g for g in (1, 2, 3, 4)}   # 4→5 the most early
    car.upshifts = {g: [rpm] * 3 for g, rpm in early.items()}
    car.upshifts[5] = [car.best_shift(5)[0]] * 4                             # and one spot on
    tips = car.advice(session_limiter_time=1.0)
    assert [t[:3] for t in tips[:3]] == ['4→5', '3→4', '2→3'], tips
    assert tips[3] == 'Spot on: 5→6 within 200 rpm of the best (4 changes).'
    assert len(tips) == 4


def test_saved_snapshot_is_cached_until_the_car_changes(tmp_path):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    exits(learner)
    learner.feed(9999.0, Sample(3000, LIMITER, gear=1, speed=10, car='other'), LIMITER, 0.0, 0.0)
    first = learner.load_snapshot('test-car')
    assert first['advice'] is not None and learner.load_snapshot('test-car') is first
    learner.rename('test-car', 'Rally car')
    assert learner.load_snapshot('test-car')['name'] == 'Rally car'
    live = learner.load_snapshot('other')                     # the car being driven: always fresh
    assert live['key'] == 'other' and 'advice' in live


def test_a_pause_does_not_split_a_session(tmp_path):
    """Telemetry stopping for a moment (a pause, a loading screen) keeps the
    session; SESSION_GAP without any ends it, fed or ticked."""
    from oversteer.shift_learner import SESSION_GAP
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    drive(learner, shift_at=6000, runs=2)          # 5 s between the pulls: one session
    learner.idle()
    first = learner.session
    t = 5000.0
    learner.feed(t, Sample(3000, LIMITER, gear=1, speed=10, car='test-car'), LIMITER, 0.3, 0.0)
    assert learner.session != first                 # the gap ended it
    learner.tick(t + SESSION_GAP / 2)
    assert learner.session is not None
    learner.tick(t + SESSION_GAP + 1)
    assert learner.session is None
    assert [h['shifts'] for h in learner.history('test-car')] == [8]    # the second one taught nothing


def learnt_error(learner):
    return max(abs(learner.car.best_shift(g)[0] - analytic_shift(g)) for g in (1, 2, 3, 4))


def test_a_hilly_stage_finds_the_same_shifts(tmp_path):
    """+-8 % grades: with the forward vector the slope comes out of the
    acceleration and the shifts land within 100 rpm; per-gear curves at the
    same road speed are compared where known."""
    from tests.sim import Road, hill
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    road = Road(grade=hill)
    t = exits(learner, road=road, runs=3)
    drive(learner, road=road, t=t)
    assert learner.car.slope_free
    assert learnt_error(learner) <= 100, [(g, learner.car.best_shift(g)) for g in (1, 2, 3, 4)]


def test_turbo_lag_does_not_bend_the_curve(tmp_path):
    """Power builds for 0.6 s after the throttle goes down: those samples are
    not the engine (BOOST_HOLD)."""
    from tests.sim import Road
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    road = Road(lag=0.6)
    t = exits(learner, road=road, runs=3)
    drive(learner, road=road, runs=4, t=t)            # every change up starts a lag
    assert learnt_error(learner) <= 100


def test_the_change_up_is_read_before_the_lift():
    """An H-pattern driver lifts, then leaves the gear: the change is taken
    at the peak rpm and the most throttle of the last 0.3 s."""
    learner = ShiftLearner()
    t = 0.0
    for rpm, throttle in ((6000, 1.0), (6400, 1.0), (6500, 0.6), (6100, 0.0), (5800, 0.0)):
        t += 0.05
        learner.feed(t, Sample(rpm, LIMITER, gear=2, speed=rpm / RATIOS[2], car='test-car'), LIMITER, throttle, 0.0)
    learner.feed(t + 0.2, Sample(4000, LIMITER, gear=0, speed=17.5, car='test-car'), LIMITER, 0.0, 1.0)
    learner.feed(t + 0.4, Sample(4400, LIMITER, gear=3, speed=17.5, car='test-car'), LIMITER, 0.2, 0.0)
    assert learner.car.upshifts == {2: [6500.0]}


def test_limiter_precedence():
    """A launch on the limiter beats the game's figure, even lower; the
    game's beats the highest rpm seen; the same source only raises it."""
    car = CarModel('x')
    assert car.set_limiter(7000.0, 'seen') and car.set_limiter(7600.0, 'game')
    assert not car.set_limiter(7800.0, 'seen') and car.limiter == 7600.0
    assert car.set_limiter(7215.0, 'launch') and car.limiter == 7215.0      # WRCG over-reports its max
    assert not car.set_limiter(7600.0, 'game') and not car.set_limiter(7210.0, 'launch')
    assert car.set_limiter(7240.0, 'launch') and car.limiter_source == 'launch'


def test_spinning_wheels_teach_no_power(tmp_path):
    """Wheel speeds 12 % ahead of the car: the drive is not reaching the road."""
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    exits(learner)
    learner.car.power, learner.car.power_g = {}, {}
    t, speed = 5000.0, 12.0
    for _ in range(200):
        speed += 3.0 / 60
        t += 1 / 60
        sample = Sample(RATIOS[2] * speed, LIMITER, gear=2, speed=speed, car='test-car')
        sample.wheel_speed = (speed, speed, speed * 1.12, speed * 1.12)
        learner.feed(t, sample, LIMITER, 1.0, 0.0)
    assert learner.car.power == {}
    sample.wheel_speed = (speed, speed, speed * 1.02, speed * 1.02)
    for _ in range(100):                                     # gripping: power again
        speed += 3.0 / 60
        t += 1 / 60
        sample = Sample(RATIOS[2] * speed, LIMITER, gear=2, speed=speed, car='test-car')
        sample.wheel_speed = (speed, speed, speed * 1.02, speed * 1.02)
        learner.feed(t, sample, LIMITER, 1.0, 0.0)
    assert learner.car.power != {}


def test_the_best_comes_with_its_range(tmp_path):
    """Bootstrap resamples of noisy power give each best change up a range,
    and the true answer lies in it."""
    from oversteer.shift_learner import SHIFT_STEP
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    drive(learner, jitter=0.01)
    exits(learner, runs=3, jitter=0.01)
    rows = {row['gear']: row for row in learner.snapshot()['gears']}
    for gear in (1, 2, 3, 4):
        low, high = rows[gear]['best_low'], rows[gear]['best_high']
        assert low <= rows[gear]['best'] + SHIFT_STEP and high >= rows[gear]['best'] - SHIFT_STEP
        assert low - 2 * SHIFT_STEP <= analytic_shift(gear) <= high + 2 * SHIFT_STEP, (gear, low, high)


def test_first_models_are_read_and_saved_as_the_second():
    old = {'key': 'x', 'name': 'x', 'limiter': 7000.0, 'power': {'60': [100.0] * 5}, 'ratios': {'2': [330.0] * 30}}
    car = CarModel.from_dict(old)
    assert car.limiter_source == 'game' and car.power_g == {} and car.boost_hold > 0
    data = car.to_dict()
    assert data['version'] == 2 and data['power_g'] == {} and data['drag'] == list(car.drag)
    car.power_g[(2, 60)] = [1.0] * 4
    assert CarModel.from_dict(car.to_dict()).power_g == {(2, 60): [1.0] * 4}


def gears(learner, t, steps, throttle=1.0, rpm=6000.0, press=None):
    """Feed a sequence of (seconds later, gear) at a steady speed."""
    for dt, gear in steps:
        t += dt
        learner.feed(t, Sample(rpm if gear else 3000.0, LIMITER, gear=gear, speed=20.0, car='test-car'), LIMITER,
                     throttle if gear else 0.0, 0.0, press=press(t) if press else None)
    return t


def written(learner):
    learner.save()
    session = learner.history('test-car')[0]['id']
    return [(s['gear'], s['gear_to'], s['direction'], s['flags']) for s in learner._reader().shifts(session)]


def test_changes_down_and_their_flags(tmp_path):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    t = gears(learner, 0.0, [(0.1, 3)] * 5)
    # a missed gate: 0.7 s in neutral, foot down, on the way from 3rd to 4th
    t = gears(learner, t, [(0.1, 0)] * 7 + [(0.1, 4)] * 20)
    # a skip: 4th to 6th
    t = gears(learner, t, [(0.05, 6)] * 20)
    # a change down that over-revs: 6th to 3rd at 3000 rpm in 6th lands past the limiter
    t = gears(learner, t, [(0.1, 3)], rpm=LIMITER * 0.98, throttle=0.0)
    t = gears(learner, t, [(0.1, 3)] * 20, throttle=0.0)
    # sequential: 3rd, 4th, 5th in a blink, then back to 4th: the second tap was one too many
    t = gears(learner, t, [(0.1, 4), (0.15, 5), (0.5, 4)] + [(0.1, 4)] * 20)
    assert written(learner) == [
        (3, 4, 'up', 'missed'), (4, 6, 'up', 'skip'), (6, 3, 'down', 'over-rev'), (3, 4, 'up', None),
        (4, 5, 'up', 'double-tap'), (5, 4, 'down', None)]


def test_flat_out_near_the_limiter_and_down_a_gear_is_a_miss(tmp_path):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    t = gears(learner, 0.0, [(0.1, 3)] * 5, rpm=LIMITER * 0.97)
    gears(learner, t, [(0.1, 2)] * 5, rpm=LIMITER * 0.7)
    assert written(learner) == [(3, 2, 'down', 'skip')]
    assert learner.car.upshifts == {}                        # not a change up to learn from
