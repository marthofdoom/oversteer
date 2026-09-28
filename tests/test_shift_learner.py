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


def steady(learner, t, gear, ratio, speeds, count, throttle=0.3, wheels=None):
    """Part throttle at steady speeds (spread over `speeds`), revs at `ratio`."""
    for i in range(count):
        t += 1 / 60
        speed = speeds[i * len(speeds) // count]
        sample = Sample(ratio * speed, LIMITER, gear=gear, speed=speed, car='test-car')
        if wheels is not None:
            sample.wheel_speed = wheels(speed)
        learner.feed(t, sample, LIMITER, throttle, 0.0)
    return t


def test_a_retuned_gear_is_relearnt(tmp_path):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    exits(learner)
    t = steady(learner, 9000.0, 3, 230.0, [22.0], 120)        # a longer 3rd, at one speed only
    assert 3 not in learner.car.retuned
    steady(learner, t, 3, 230.0, [22.0, 28.0], 120)            # and at another
    assert abs(learner.car.ratio(3) - 230.0) < 1 and 3 in learner.car.retuned
    assert learner.car.top_seen == 0.0                 # where the data ended belonged to the old gearing


def test_a_steady_spin_is_not_a_retune(tmp_path):
    """Part throttle on snow spins the wheels a steady 6 %: that looks like a
    shorter gear, which needs twice the samples, and only early in a
    session (setups change in menus)."""
    from oversteer.shift_learner import RETUNE_SAMPLES, RETUNE_WINDOW
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    exits(learner)
    t = steady(learner, 9000.0, 3, RATIOS[3] * 1.06, [22.0, 28.0], RETUNE_SAMPLES * 2 - 10)
    assert learner.car.retuned == {}
    t = steady(learner, t, 3, RATIOS[3], [22.0, 28.0], int(RETUNE_WINDOW * 60))        # a minute's driving
    t = steady(learner, t, 3, RATIOS[3] * 1.06, [22.0, 28.0], RETUNE_SAMPLES * 4)
    assert learner.car.retuned == {} and abs(learner.car.ratio(3) - RATIOS[3]) < 1


def test_driven_wheels_see_through_spin(tmp_path):
    """DiRT sends wheel speeds: once they agree with the car's speed, and
    full-throttle spin shows the rear axle stays locked to the engine,
    the ratio comes from the rear wheels at any throttle."""
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    exits(learner)                                             # ratios from part throttle
    same = lambda v: (v, v, v, v)                              # noqa: E731
    t = steady(learner, 9000.0, 3, RATIOS[3], [22.0, 28.0], 120, wheels=same)
    assert learner.car.wheels_ok and learner.car.drivetrain is None
    learner.car.ratios[3] = learner.car.ratios[3][:30]
    power = {k: len(v) for k, v in learner.car.power_g.items()}
    # flat out, the rears 8 % ahead of the car: the revs follow the rears
    spin = lambda v: (v, v, v * 1.08, v * 1.08)                # noqa: E731
    steady(learner, t, 3, RATIOS[3] * 1.08, [22.0, 23.0, 24.0], 200, throttle=1.0, wheels=spin)
    assert learner.car.drivetrain == 'rwd'
    assert len(learner.car.ratios[3]) > 100 and abs(learner.car.ratio(3) - RATIOS[3]) < 0.5
    assert learner.car.retuned == {}
    assert {k: len(v) for k, v in learner.car.power_g.items()} == power                # spin: still no power


def test_tunes(tmp_path):
    """A tune is one gearing: the first, then a new one when gears change;
    two gears changed by the same factor are the final drive."""
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    exits(learner, runs=1)
    learner.save()
    car = learner._reader().car('_no_profile', 'test-car')['id']
    tunes = learner._reader().tunes(car)
    assert [t['change'] for t in tunes] == ['first'] and abs(tunes[0]['ratios'][3] - RATIOS[3]) < 0.5
    t = steady(learner, 5000.0, 3, RATIOS[3] * 0.9, [22.0, 28.0], 150)
    t = steady(learner, t, 4, RATIOS[4] * 0.9, [22.0, 28.0], 150)
    learner.save()
    tunes = learner._reader().tunes(car)
    assert [t['change'] for t in tunes] == ['first', 'final-drive']
    assert abs(tunes[1]['ratios'][4] - RATIOS[4] * 0.9) < 0.5
    session = learner.history('test-car')[0]
    assert learner._reader().session(session['id'])['tune'] == tunes[1]['id']
    steady(learner, t + 1000.0, 2, RATIOS[2] * 1.1, [12.0, 18.0], 260)      # shorter: twice the samples
    learner.save()
    assert [t['change'] for t in learner._reader().tunes(car)] == ['first', 'final-drive', 'gears:2']


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
    car.slope_free = True                                  # a pooled curve: the slope was taken out
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
    game's beats the highest rpm seen; the same source only raises it,
    but a launch measures the car as it is now and replaces the last one
    either way (a figure raised by one bad packet must not stay)."""
    car = CarModel('x')
    assert car.set_limiter(7000.0, 'seen') and car.set_limiter(7600.0, 'game')
    assert not car.set_limiter(7800.0, 'seen') and car.limiter == 7600.0
    assert car.set_limiter(7215.0, 'launch') and car.limiter == 7215.0      # WRCG over-reports its max
    assert not car.set_limiter(7600.0, 'game') and not car.set_limiter(7215.5, 'launch')
    assert car.set_limiter(8700.0, 'launch') and car.set_limiter(7240.0, 'launch')
    assert car.limiter == 7240.0 and car.limiter_source == 'launch'


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
    assert learner.car.power == {} and learner.car.power_g == {}
    sample.wheel_speed = (speed, speed, speed * 1.02, speed * 1.02)
    for _ in range(100):                                     # gripping: power again
        speed += 3.0 / 60
        t += 1 / 60
        sample = Sample(RATIOS[2] * speed, LIMITER, gear=2, speed=speed, car='test-car')
        sample.wheel_speed = (speed, speed, speed * 1.02, speed * 1.02)
        learner.feed(t, sample, LIMITER, 1.0, 0.0)
    assert learner.car.power_g != {}


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


def test_first_models_are_read_and_saved_as_the_third():
    old = {'key': 'x', 'name': 'x', 'limiter': 7000.0, 'power': {'60': [100.0] * 5}, 'ratios': {'2': [330.0] * 30}}
    car = CarModel.from_dict(old)
    assert car.limiter_source == 'game' and car.power_g == {} and car.boost_hold > 0
    assert car.power == {60: [100.0] * 5}                      # no source said: kept
    data = car.to_dict()
    assert data['version'] == 3 and data['power_g'] == {} and data['drag'] == list(car.drag)
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


def test_wheel_speeds_in_the_wrong_unit_are_not_believed(tmp_path):
    """Wheel speeds that disagree with the car's at low slip (km/h, say) are
    never used for a ratio."""
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    exits(learner)
    kmh = lambda v: (v * 3.6,) * 4                             # noqa: E731
    steady(learner, 9000.0, 3, RATIOS[3], [22.0, 28.0], 300, wheels=kmh)
    assert learner.car.wheels_ok is False and abs(learner.car.ratio(3) - RATIOS[3]) < 0.5


def test_forza_tyre_radius(tmp_path):
    """Forza sends wheel rotation (rad/s): the radius is learnt from speed at
    low slip, and the tune shows it."""
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    t = 0.0
    for gear in (2, 3):
        for i in range(300):
            t += 1 / 60
            speed = 15.0 + 10.0 * (i // 150)
            sample = Sample(RATIOS[gear] * speed, LIMITER, gear=gear, speed=speed, car='test-car')
            sample.drivetrain = 'awd'
            sample.wheel_rot = (speed / 0.33,) * 4
            learner.feed(t, sample, LIMITER, 0.3, 0.0)
    assert learner.car.radii is not None and abs(learner.car.radii[0] - 0.33) < 1e-6
    learner.save()
    reader = learner._reader()
    tune = reader.tunes(reader.car('_no_profile', 'test-car')['id'])[0]
    assert abs(tune['tyre_radius'] - 0.33) < 1e-6


def _rising_car(top_band, limiter=8000.0, source='game'):
    """Power rising all the way, known up to `top_band` x 100 rpm: staying
    in gear always pulls harder."""
    car = CarModel('forza-fm/1')
    car.power_source = 'game'
    car.ratios = {2: [300.0] * 20, 3: [220.0] * 20}
    car.power = {band: [float(band)] * 4 for band in range(20, top_band + 1)}
    car.top_seen = top_band * 100.0 + 50
    car.set_limiter(limiter, source)
    return car


def test_the_highest_rpm_seen_is_not_the_limiter():
    """A driver who has only taken the car to 6500 of 8000: nothing says the
    engine stops pulling there, so no best change up, and the lights keep
    the percentage rule instead of locking the early change in."""
    early = _rising_car(64)
    assert early.ceiling() == 8000.0 and early.best_shift(2) is None
    assert early.best_shift(2) is None and early.snapshot()['limiter'] == 8000.0
    explored = _rising_car(80)
    best, coverage = explored.best_shift(2)
    assert best == 8000.0 and coverage > 0.9                 # pulls to the limiter, now that it is known there
    # Without a limiter from the game or a launch, the scan ends where the
    # data does, and "pulls to the limiter" is never the answer
    seen = _rising_car(80, limiter=8300.0, source='seen')
    assert seen.known_limiter() == 0.0 and seen.ceiling() == 8050.0 and seen.best_shift(2) is None


def test_cars_the_game_does_not_tell_apart_teach_nothing(tmp_path):
    """BeamNG's cars all arrive as beamng/unknown: what one teaches would
    drive the lights in the next."""
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    drive(learner, car='beamng/unknown')
    exits(learner, car='beamng/unknown')
    car = learner.car
    assert car.key == 'beamng/unknown' and car.ratios == {} and car.power == {} and not car.limiter
    assert learner.shift_rpm(2) is None


def test_the_best_a_change_is_measured_against_is_one_the_lights_trust(tmp_path):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    exits(learner)                                          # ratios, but the top end hardly known
    car = learner.car
    # Known from 3400 to 5200 rpm only, peaking at 4000: a crossover found
    # from a sliver of the range
    car.power = {band: [float(band if band <= 40 else 80 - band)] * 4 for band in range(34, 53)}
    car.power_g = {}
    assert car.best_shift(2) is not None and car.best_shift(2)[1] < 0.6
    learner._shift_locked(car, (2, 5000.0, 1.0, 1.0, False, None), 3, 4000.0, 1.5)
    assert learner._pending[-1]['best'] is None


# -- the game's own data, grip per surface, slope and ratios (section 8.1) --

FABIA = 'acr/Skoda Fabia RS Rally2'
FABIA_GEARS = [3.583, 2.538, 1.867, 1.412, 1.136]
FABIA_WHEEL = 4.231 * 0.922 / 0.325                  # N at the wheels per Nm per unit of gear ratio
FABIA_RPM_PER_MS = 4.231 * 60 / (2 * math.pi * 0.325)


def fabia_pulls(learner, grip=11000.0, surface='gravel', runs=5, t=0.0, mass=1300.0):
    """Full-throttle pulls from 2500 rpm in every gear of a simulated
    Fabia (the game's torque curve and gearing), its drive held to `grip`
    newtons (the tyres slide past it), each after a stretch of part
    throttle; the run's surface is `surface`."""
    from oversteer import car_data
    from oversteer.shift_learner import DRAG_C0, DRAG_C2
    entry = car_data.entry(FABIA)
    dt = 1 / 60

    def feed(sample, throttle):
        sample.forward = (1.0, 0.0, 0.0)                  # flat: the slope is known
        learner.feed(t, sample, 7500.0, throttle, 0.0)
        if surface and learner.runs.run is not None:
            learner.run_surface.setdefault(learner.runs.run, surface)
    for _ in range(runs):
        for gear, ratio in enumerate(FABIA_GEARS, 1):
            per_ms = ratio * FABIA_RPM_PER_MS
            speed = 2500.0 / per_ms
            for _ in range(60):
                t += dt
                feed(Sample(per_ms * speed, 7500.0, gear=gear, speed=speed, car=FABIA), 0.3)
            while per_ms * speed < 7450 and speed < 60:
                force = min(car_data.torque(entry, per_ms * speed) * ratio * FABIA_WHEEL, grip)
                speed += (force / mass - DRAG_C0 - DRAG_C2 * speed * speed) * dt
                t += dt
                feed(Sample(per_ms * speed, 7500.0, gear=gear, speed=speed, car=FABIA), 1.0)
            for _ in range(30):
                t += dt
                speed *= 0.99
                feed(Sample(per_ms * speed, 7500.0, gear=gear, speed=speed, car=FABIA), 0.0)
        t += 5.0
    return t


def test_the_game_data_gives_every_best_upshift_exactly():
    """The Fabia's torque curve pulls harder in each gear than in the next
    all the way to the limiter (the ground truth: fabia-report, section
    2): the best change up is the limiter, from the first drive."""
    car = CarModel(FABIA, 'Skoda Fabia RS Rally2')
    for gear in (1, 2, 3, 4):
        best = car.best_for(gear)
        assert best['rpm'] == 7500.0 and best['source'] == 'game' and best['coverage'] == 1.0
    assert car.best_for(5) is None                           # top gear
    snapshot = car.snapshot()
    assert [row['gear'] for row in snapshot['gears']] == [1, 2, 3, 4, 5]
    assert snapshot['gear_set'] == 'SkodaFabiaRSRally2Set0' and snapshot['power_source'] == 'game data'
    assert abs(snapshot['gears'][2]['game_ratio'] - 1.867 * FABIA_RPM_PER_MS) < 0.5


def test_a_curve_that_falls_away_crosses_below_the_limiter(monkeypatch):
    from oversteer import car_data
    entry = {'name': 'x', 'limiter_rpm': 8000, 'torque_curve': [(0.0, 0.0), (3000.0, 300.0), (5000.0, 300.0),
                                                                 (8000.0, 100.0)],
             'gear_sets': [{'id': 'a', 'gears': [3.0, 2.0], 'default': True}], 'final_drive': 4.0}
    monkeypatch.setattr(car_data, '_cars', {'acr/x': entry})
    car = CarModel('acr/x')
    # 1st: 300 - (r - 5000) / 15 Nm x 3; 2nd at 2/3 of the revs: flat 300 Nm x 2 until 7500 in 1st
    crossing = 5000 + 15 * 100                               # 3 (300 - (r - 5000) / 15) = 600
    assert abs(car.best_for(1)['rpm'] - crossing) <= 25


def test_first_gear_is_grip_limited_on_gravel_only(tmp_path):
    """1st on gravel spins its drive away (held to 11 kN where the engine
    gives up to 19): changing up is worth it once 2nd reaches the same
    limit, near 3950 rpm; 2nd, 3rd and 4th stay engine-limited, and on
    tarmac, where nothing was measured, the engine's best stands."""
    from oversteer import car_data
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    fabia_pulls(learner)
    car = learner.car
    first = car.best_for(1, 'gravel')
    assert first['grip_limited'] and first['source'] == 'game' and first['surface'] == 'gravel'
    entry = car_data.entry(FABIA)
    step = FABIA_GEARS[1] / FABIA_GEARS[0]
    expected = next(r for r in range(3000, 7500, 25)
                    if car_data.torque(entry, r * step) * FABIA_GEARS[1] >= 0.98 * 11000 / FABIA_WHEEL)
    assert abs(first['rpm'] - expected) <= 75, (first, expected)
    for gear in (2, 3, 4):
        best = car.best_for(gear, 'gravel')
        assert best['rpm'] == 7500.0 and best['grip_limited'] is False, (gear, best)
    assert car.best_for(1, 'tarmac')['rpm'] == 7500.0 and car.best_for(1, 'tarmac')['grip_limited'] is None
    assert car.best_for(1)['rpm'] == 7500.0
    # The lights use the run's surface
    assert learner.surface == 'gravel' and learner.shift_rpm(1) == first['rpm'] and learner.shift_rpm(2) == 7500.0
    rows = {row['gear']: row for row in learner.snapshot()['gears']}
    assert rows[1]['grip_limited'] and rows[1]['best'] == first['rpm'] and learner.snapshot()['surface'] == 'gravel'
    # ...and a saved model keeps what it measured
    again = CarModel.from_dict(car.to_dict())
    assert again.best_for(1, 'gravel')['rpm'] == first['rpm'] and again.last_surface == 'gravel'


def test_a_high_gear_short_of_its_drive_is_not_grip_limited():
    """Gravel's rolling resistance or drag the model underestimates takes
    most, as a share, from the high gears' small drive: only a higher gear
    stands for the engine, so 4th reading low against 2nd is not grip."""
    from oversteer import car_data
    car = CarModel(FABIA)
    entry = car_data.entry(FABIA)
    for gear, share in ((2, 1.0), (3, 0.9), (4, 0.5), (5, 0.45)):
        car.drive[('gravel', gear)] = [(rpm, 20.0, share * car_data.torque(entry, rpm) * FABIA_GEARS[gear - 1],
                                        rpm % 3) for rpm in range(5000, 7500, 50)]
    assert car.best_for(4, 'gravel')['grip_limited'] is False and car.best_for(4, 'gravel')['rpm'] == 7500.0
    assert car.grip(5, 'gravel') is None                     # top gear: nothing to compare with


def test_with_enough_grip_no_gear_is_lowered(tmp_path):
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    fabia_pulls(learner, grip=1e9, surface='tarmac')
    for gear in (2, 3, 4):
        best = learner.car.best_for(gear, 'tarmac')
        assert best['rpm'] == 7500.0 and best['grip_limited'] is False
    # 1st with all the grip is over too soon to be measured: the engine's best
    best = learner.car.best_for(1, 'tarmac')
    assert best['rpm'] == 7500.0 and not best['grip_limited']


def test_the_slope_comes_from_the_position_where_no_forward_vector_is_sent(tmp_path):
    """Assetto Corsa Rally sends its position, not its orientation: the
    climb over the distance travelled takes the hills out."""
    from tests.sim import Road, hill

    class Positioned(Road):
        height = 0.0

        def sample(self, rpm, speed, gear, car, dt):
            sample = Road.sample(self, rpm, speed, gear, car, dt)
            self.height += self.grade(self.distance) * speed * dt
            sample.pos = (self.distance, self.height, 0.0)
            return sample
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    road = Positioned(grade=hill, forward=False)
    t = exits(learner, road=road, runs=3)
    t = drive(learner, road=road, t=t)
    assert learner.car.slope_free and learner.car.power
    assert learnt_error(learner) <= 100, [(g, learner.car.best_shift(g)) for g in (1, 2, 3, 4)]
    # Below about 24 km/h the car covers too little ground in the window for
    # a slope: those samples are left out, and the car stays slope-free
    pooled = {band: len(v) for band, v in learner.car.power.items()}
    t, speed, x = t + 10.0, 5.5, 0.0
    for _ in range(120):
        t += 1 / 60
        speed += 0.5 / 60
        x += speed / 60
        sample = Sample(RATIOS[1] * speed, LIMITER, gear=1, speed=speed, car='test-car')
        sample.pos, sample.brake = (x, 0.0, 0.0), 0.0
        learner.feed(t, sample, LIMITER, 1.0, 0.0)
        assert learner.car.slope_free
    assert {band: len(v) for band, v in learner.car.power.items()} == pooled


def test_hills_left_in_keep_the_gears_apart(tmp_path):
    """No slope from the game: each gear's bands carry the hills they were
    driven on, so nothing is pooled over the gears and a band needs more
    samples."""
    from tests.sim import Road, hill
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    road = Road(grade=hill, forward=False)
    t = exits(learner, road=road, runs=3)
    drive(learner, road=road, t=t)
    car = learner.car
    assert not car.slope_free and car.power == {} and car.power_g
    assert car.band_min() > 4 and car.curve() == {}


def test_coasting_and_braking_teach_no_ratio():
    """On the engine's braking the tyres slip the other way: no ratio from
    a closed throttle, nor on the brake."""
    learner = ShiftLearner()
    t, speed = 0.0, 15.0
    for throttle, brake in ((0.1, 0.0), (0.3, 0.3)):
        for _ in range(120):
            t += 1 / 60
            sample = Sample(RATIOS[2] * speed * 1.05, LIMITER, gear=2, speed=speed, car='test-car')
            sample.brake = brake
            learner.feed(t, sample, LIMITER, throttle, 0.0)
    assert learner.car.ratios == {}
    for _ in range(120):
        t += 1 / 60
        sample = Sample(RATIOS[2] * speed, LIMITER, gear=2, speed=speed, car='test-car')
        sample.brake = 0.0
        learner.feed(t, sample, LIMITER, 0.3, 0.0)
    assert abs(learner.car.ratio(2) - RATIOS[2]) < 0.5


def test_a_pooled_curve_learnt_on_hills_is_dropped_on_loading():
    old = {'version': 2, 'key': 'x', 'power_source': 'accel', 'slope_free': False,
           'power': {'60': [100.0] * 5}, 'power_g': {'2:60': [100.0] * 5}}
    car = CarModel.from_dict(old)
    assert car.power == {} and car.power_g == {(2, 60): [100.0] * 5}


# -- how long the questions take (the listener and the drive-log thread ask them) --

def _busy_car(key='fh5/busy', seed=1):
    """A learnt car with everything full: ratios, a pooled and per-gear
    power curve and 300 drive samples per gear on tarmac."""
    import random
    rng = random.Random(seed)
    car = CarModel(key)
    car.power_source, car.limiter, car.limiter_source = 'game', 8000.0, 'game'

    def power(band):
        return band * 1000.0 * (1 - (band - 60) ** 2 / 3600) + rng.gauss(0, 100)
    for gear, ratio in enumerate([150, 105, 80, 64, 53, 45], 1):
        car.ratios[gear] = [ratio * (1 + rng.gauss(0, 0.003)) for _ in range(200)]
        car.drive[('tarmac', gear)] = [(rng.uniform(3000, 7500), 20.0, rng.uniform(2, 8) / gear, i // 30)
                                       for i in range(300)]
        for band in range(20, 80):
            car.power_g[(gear, band)] = [power(band) for _ in range(40)]
    for band in range(20, 80):
        car.power[band] = [power(band) for _ in range(40)]
    return car


def _timed(fn, repeat=3):
    import time
    best = None
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        took = time.perf_counter() - start
        best = took if best is None else min(best, took)
    return best


def test_the_bests_are_quick_to_work_out():
    """A snapshot asks every gear's best, each best the grip of every gear:
    worked out from scratch (a fresh copy, as publish() makes) it must stay
    far below the drive-log thread's second, and a single best far below
    the rev lights' packet rate. The game's data (the Fabia) as well."""
    from oversteer import car_data
    busy = _busy_car().to_dict()
    assert _timed(lambda: CarModel.from_dict(busy).best_for(1, 'tarmac')) < 0.020
    assert _timed(lambda: ShiftLearner._snapshot_of(CarModel.from_dict(busy), 0.0, {}, 'tarmac')) < 0.150
    fabia = CarModel(FABIA)
    for gear, ratio in enumerate(FABIA_GEARS, 1):
        fabia.ratios[gear] = [ratio * FABIA_RPM_PER_MS] * 200
        fabia.drive[('gravel', gear)] = [(3000.0 + 15 * i, 20.0, car_data.torque(car_data.entry(FABIA), 3000.0 + 15 * i)
                                          * ratio, i // 30) for i in range(300)]
    fabia = fabia.to_dict()
    assert _timed(lambda: CarModel.from_dict(fabia).best_for(1, 'gravel')) < 0.020
    assert _timed(lambda: ShiftLearner._snapshot_of(CarModel.from_dict(fabia), 0.0, {}, 'gravel')) < 0.150


def test_the_listener_never_works_a_best_out(tmp_path, monkeypatch):
    """With a drive-log thread, the rev lights and each change up read the
    bests publish() worked out there: the listener, holding the lock, never
    works one out itself."""
    learner = ShiftLearner()
    learner.threaded = True
    fabia_pulls(learner, runs=1, surface=None)
    calls = []
    real = CarModel.best_for

    def counted(self, gear, surface=None):
        calls.append(gear)
        return real(self, gear, surface)
    monkeypatch.setattr(CarModel, 'best_for', counted)
    assert learner.shift_rpm(2) is None                     # nothing published yet: the percentage rule
    t = fabia_pulls(learner, runs=1, surface=None, t=100.0)
    assert calls == []
    learner.publish()
    assert calls                                              # worked out by publish(), on its copy
    del calls[:]
    assert learner.shift_rpm(2) == 7500.0 and learner.shift_rpm(1) == 7500.0
    learner._shift_locked(learner.car, (2, 7000.0, 1.0, t, False, None), 3, 5000.0, t + 0.1)
    assert learner._pending[-1]['best'] == 7500.0 and calls == []


def test_grip_is_judged_from_several_pulls_with_their_wheelspin(tmp_path):
    """The drive samples keep the wheelspin (a sample off the gear's ratio
    is no power sample, but it is what the gear achieves on the surface),
    ten a second, each with its pull; one long pull is not enough to judge
    a gear's grip, and samples of before (every packet, spin left out) are
    measured again."""
    from oversteer.shift_learner import DRIVE_EVERY, DRIVE_PULLS
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    t = fabia_pulls(learner, runs=DRIVE_PULLS - 1)
    car = learner.car
    assert car.drive and all(car.grip(g, 'gravel') is None for g in range(1, 5))     # long pulls, but two
    fabia_pulls(learner, runs=1, t=t)
    assert car.grip(3, 'gravel') is not None
    # 2nd at full throttle, the revs 15 % over its ratio: spinning on gravel
    power = {k: len(v) for k, v in car.power_g.items()}
    before = len(car.drive[('gravel', 2)])
    t, speed = t + 100.0, 12.0
    start = t
    per_ms = FABIA_GEARS[1] * FABIA_RPM_PER_MS
    for _ in range(120):
        t += 1 / 60
        speed += 2.0 / 60
        sample = Sample(per_ms * speed * 1.15, 7500.0, gear=2, speed=speed, car=FABIA)
        sample.forward = (1.0, 0.0, 0.0)
        learner.feed(t, sample, 7500.0, 1.0, 0.0)
        learner.run_surface.setdefault(learner.runs.run, 'gravel')
    kept = car.drive[('gravel', 2)][before:]
    assert kept and len(kept) <= (t - start) / DRIVE_EVERY + 1
    assert all(rpm > per_ms * s * 1.1 for rpm, s, _d, _p in kept) and len({p for *_x, p in kept}) == 1
    assert {k: len(v) for k, v in car.power_g.items()} == power                          # still no power
    old = dict(car.to_dict(), drive={'gravel:2': [[5000.0, 20.0, 3.0]] * 50})
    assert CarModel.from_dict(old).drive == {}


def test_advice_waits_for_the_grip_on_a_loose_surface():
    """The game's data alone: early changes are coached on tarmac, and on
    gravel only once the gear's grip there is measured."""
    car = CarModel(FABIA)
    car.upshifts[3] = [6500.0] * 5
    assert any(line.startswith('3→4: you change up around 6500 rpm, 1000 early') for line in car.advice(surface='tarmac'))
    lines = car.advice(surface='gravel')
    assert not any('1000 early' in line for line in lines)
    assert any(line.startswith('3→4 on gravel: early, but not coached until the grip') for line in lines)


def test_a_grip_limited_gear_is_on_target_up_to_the_engine_best(tmp_path):
    """From where 2nd reaches the grip limit on gravel up to the engine's
    best both gears are held to it: 1st changed up at 6500 is spot on, not
    "3000 late", and recorded as on target, with the range; below the
    lowered best it is early against it."""
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    t = fabia_pulls(learner)
    car = learner.car
    best = car.best_for(1, 'gravel')
    assert best['grip_limited'] and best['rpm'] < 4500 and best['engine_rpm'] == 7500.0
    assert car.best_for(2, 'gravel')['engine_rpm'] == car.best_for(2, 'gravel')['rpm'] == 7500.0
    car.upshifts[1] = [6500.0] * 5
    lines = car.advice(surface='gravel')
    assert lines[0].startswith('Spot on: 1→2') and not any('late' in line for line in lines)
    assert any('1→2 from {:.0f} to 7500 rpm'.format(best['rpm']) in line for line in lines)
    car.upshifts[1] = [best['rpm'] - 800] * 5
    assert car.advice(surface='gravel')[0].endswith('hold it to about {:.0f} (lowered for grip on gravel).'.format(
        best['rpm']))
    learner._shift_locked(car, (1, 6500.0, 1.0, t, False, None), 2, 4000.0, t + 0.1)
    shift = learner._pending[-1]
    assert shift['best'] == 6500.0 and (shift['best_low'], shift['best_high']) == (best['rpm'], 7500.0)
    rows = {row['gear']: row for row in learner.snapshot()['gears']}
    assert rows[1]['best'] == best['rpm'] and rows[1]['engine_best'] == 7500.0


def test_the_game_data_is_set_aside_when_the_gearing_does_not_match():
    """Learnt ratios 6 % longer than the game's in 3rd (a setup or a car
    the data does not know; no spin does that): the best changes up are learnt instead, and the tab
    says why; the Fabia's spin-shortened 1st and 2nd are not such a miss."""
    from oversteer import telemetry_view
    car = CarModel(FABIA)
    for gear, ratio in enumerate(FABIA_GEARS, 1):
        car.ratios[gear] = [ratio * FABIA_RPM_PER_MS * 1.02 * {1: 1.036, 2: 1.056}.get(gear, 1.0)] * 30
    assert car.gearing_aside() is None and car.game_data() is car.shipped
    assert car.best_for(3)['source'] == 'game' and car.snapshot()['power_source'] == 'game data'
    car.ratios[3] = [FABIA_GEARS[2] * FABIA_RPM_PER_MS * 1.02 * 0.94] * 30
    assert abs(car.gearing_aside() - 0.06) < 0.001 and car.game_data() is None
    assert car.best_for(3) is None                            # nothing learnt of the engine yet
    snapshot = car.snapshot()
    assert snapshot['power_source'] is None and abs(snapshot['gearing_aside'] - 0.06) < 0.001
    assert snapshot['limiter'] == 7500.0                        # the engine's limit still stands
    assert "the game's data set aside: the gearing learnt is 6 % off" in telemetry_view.shift_summary(snapshot)
