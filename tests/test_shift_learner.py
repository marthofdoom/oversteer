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


def exits(learner, car='test-car', runs=2):
    """Full-throttle pulls from low revs in every gear, as out of corners."""
    t = 1000.0
    for run in range(runs):
        for gear in RATIOS:
            speed = 2500.0 / RATIOS[gear]
            while RATIOS[gear] * speed < LIMITER and speed < 70:
                speed += (power(RATIOS[gear] * speed) / speed - DRAG_C0 - DRAG_C2 * speed * speed) / 60
                t += 1 / 60
                learner.feed(t, Sample(RATIOS[gear] * speed, LIMITER, gear=gear, speed=speed, car=car), LIMITER, 1.0, 0.0)
            for _ in range(30):                      # braking for the next corner
                t += 1 / 60
                speed *= 0.99
                learner.feed(t, Sample(RATIOS[gear] * speed, LIMITER, gear=gear, speed=speed, car=car), LIMITER, 0.0, 0.0)


def test_learns_the_best_shift_per_gear(tmp_path):
    learner = ShiftLearner(str(tmp_path))
    drive(learner)
    exits(learner)
    car = learner.car
    for gear in (1, 2, 3, 4):
        best, coverage = car.best_shift(gear)
        assert abs(best - analytic_shift(gear)) <= 150, (gear, best, analytic_shift(gear))
        assert coverage > 0.6
    assert abs(car.ratio(3) - RATIOS[3]) < 0.5


def test_records_where_the_driver_changes_up(tmp_path):
    learner = ShiftLearner(str(tmp_path))
    drive(learner, shift_at=6000, runs=2)
    average, count = learner.car.average_upshift(2)
    assert count == 2 and 5950 <= average <= 6100


def test_car_profiles_persist_and_switch(tmp_path):
    learner = ShiftLearner(str(tmp_path))
    drive(learner, runs=2)
    exits(learner)
    learner.idle()                                  # telemetry stopped: saved
    again = ShiftLearner(str(tmp_path))
    drive(again, runs=0, car='test-car')
    again.feed(0.0, Sample(3000, LIMITER, gear=1, speed=30, car='test-car'), LIMITER, 0.0, 0.0)
    assert again.car.best_shift(2) is not None     # picked up where it left off
    again.feed(1.0, Sample(3000, LIMITER, gear=1, speed=30, car='other'), LIMITER, 0.0, 0.0)
    assert again.car.key == 'other' and again.car.best_shift(2) is None
    assert {k for k, _ in again.known_cars()} == {'test-car'}
    again.forget('test-car')
    assert again.known_cars() == []


def test_wheelspin_does_not_teach_power(tmp_path):
    learner = ShiftLearner(str(tmp_path))
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
    learner = ShiftLearner(str(tmp_path))
    exits(learner)
    t = 9000.0
    for i in range(120):                             # cruising in a longer 3rd
        t += 1 / 60
        learner.feed(t, Sample(230.0 * 25, LIMITER, gear=3, speed=25.0, car='test-car'), LIMITER, 0.3, 0.0)
    assert abs(learner.car.ratio(3) - 230.0) < 1 and 3 in learner.car.retuned


def test_shift_point_needs_confidence(tmp_path):
    car = CarModel('x')
    assert car.best_shift(1) is None
    learner = ShiftLearner(str(tmp_path))
    assert learner.shift_rpm(2) is None
