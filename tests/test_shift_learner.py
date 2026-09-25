import math
from oversteer.shift_learner import ShiftLearner, CarModel, DRAG_C0, DRAG_C2
from oversteer.telemetry import Sample

LIMITER = 7500.0
RATIOS = {1: 480.0, 2: 330.0, 3: 250.0, 4: 200.0, 5: 165.0}  # rpm per m/s: 1st tops out near 56 km/h


def power(rpm):
    """W/kg: peaks at 6500 and fades towards the limiter."""
    if rpm <= 6500:
        return 150.0 * math.sin(math.pi / 2 * rpm / 6500)
    return 150.0 * (1 - (rpm - 6500) / 4000)


def analytic_shift(gear):
    step = RATIOS[gear + 1] / RATIOS[gear]
    rpm = LIMITER * 0.5
    while rpm <= LIMITER:
        if power(rpm * step) > power(rpm):
            return rpm
        rpm += 5
    return LIMITER


def drive(learner, shift_at=LIMITER, car='test-car', runs=3, jitter=0.0):
    """Full-throttle pulls through the gears, changing up at `shift_at`."""
    t = 0.0
    dt = 1 / 60
    for run in range(runs):
        gear, speed = 1, 3000.0 / RATIOS[1]
        start = t
        while gear <= 5 and t - start < 90:       # top gear may never reach the limiter
            rpm = RATIOS[gear] * speed
            if rpm >= min(shift_at, LIMITER) and gear < 5:
                # through neutral for a moment, like an H-pattern box
                for _ in range(6):
                    t += dt
                    learner.feed(t, Sample(rpm * 0.9, LIMITER, gear=0, speed=speed, car=car), LIMITER, 0.0, 1.0)
                gear += 1
                continue
            if rpm >= LIMITER:
                break
            accel = power(rpm) / speed - DRAG_C0 - DRAG_C2 * speed * speed
            speed += accel * dt
            t += dt
            wobble = 1 + jitter * math.sin(t * 37)
            learner.feed(t, Sample(RATIOS[gear] * speed * wobble, LIMITER, gear=gear, speed=speed, car=car),
                         LIMITER, 1.0, 0.0)
        t += 5


def cruise(learner, t, gear, speed, car='test-car', seconds=1.0, slip=0.0):
    """Part throttle at a steady speed: where the ratios are learnt."""
    for _ in range(int(seconds * 60)):
        t += 1 / 60
        learner.feed(t, Sample(RATIOS[gear] * speed * (1 + slip), LIMITER, gear=gear, speed=speed, car=car),
                     LIMITER, 0.3, 0.0)
    return t


def exits(learner, car='test-car', runs=2):
    """Full-throttle pulls from low revs in every gear, as out of corners,
    each after a stretch of part throttle."""
    t = 1000.0
    for run in range(runs):
        for gear in RATIOS:
            speed = 2500.0 / RATIOS[gear]
            t = cruise(learner, t, gear, speed, car)
            while RATIOS[gear] * speed < LIMITER and speed < 70:
                speed += (power(RATIOS[gear] * speed) / speed - DRAG_C0 - DRAG_C2 * speed * speed) / 60
                t += 1 / 60
                learner.feed(t, Sample(RATIOS[gear] * speed, LIMITER, gear=gear, speed=speed, car=car), LIMITER, 1.0, 0.0)
            for _ in range(30):                      # braking for the next corner
                t += 1 / 60
                speed *= 0.99
                learner.feed(t, Sample(RATIOS[gear] * speed, LIMITER, gear=gear, speed=speed, car=car), LIMITER, 0.0, 0.0)


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
    assert {k for k, _ in again.known_cars()} == {'test-car'}
    again.forget('test-car')
    assert again.known_cars() == []


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
    from oversteer.shift_learner import SCHEMA
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
    assert dict(learner.known_cars()) == {'codemasters-7500-800-6': '7500 rpm, 6 gears',
                                          'codemasters-7000-900-5': 'my WRCG car'}
    snapshot = learner.load_snapshot('codemasters-7500-800-6')
    assert abs(snapshot['gears'][0]['ratio'] - 330.0) < 0.01
    fixed = learner.load_snapshot('codemasters-7000-900-5')
    assert fixed['limiter'] == 7000.0
    row = learner.db.execute('SELECT model FROM cars WHERE key = ?', ('codemasters-7500-800-6',)).fetchone()
    model = CarModel.from_dict(json.loads(row[0]))
    assert abs(model.limiter - 7215.0 / f) < 0.01 and abs(model.upshifts[2][0] - 6800.0) < 0.01
    assert list(model.power) in ([59], [60])                     # around 6000 rpm, not 6283
    assert abs(learner.history('codemasters-7500-800-6')[0]['error'] + 200.0) < 0.01
    assert learner.db.execute('PRAGMA user_version').fetchone()[0] == 1
    assert (tmp_path / 'telemetry.db.v0.bak').exists()
    learner.db.close()
    again = ShiftLearner(path, profile='rally')                 # done once only
    assert 'codemasters-7500-800-6' in dict(again.known_cars())
