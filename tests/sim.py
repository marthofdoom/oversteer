"""A simulated car for the learner and drive-log tests: a power curve, five
gears, and drives that exercise them (docs/telemetry-coaching.md, §13)."""
import math

from oversteer.shift_learner import DRAG_C0, DRAG_C2
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
