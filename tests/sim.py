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


G = 9.80665


def hill(distance):
    """A hilly stage: the sine of the slope along the road, +-8 % every 700 m."""
    return 0.08 * math.sin(distance / 700 * 2 * math.pi)


class Road:
    """Where the simulated car is on a road with a grade profile, and the
    sample it sends: its forward vector tilts with the slope (world y up)
    when `forward` is set, as Codemasters and EA WRC send it."""

    def __init__(self, grade=None, forward=True, lag=0.0):
        self.grade, self.forward, self.lag = grade, forward, lag
        self.distance = 0.0
        self.flat_since = None

    def accel(self, t, rpm, speed, throttle):
        """dv/dt of the car: the engine's drive (with turbo lag after the
        throttle goes down), less drag and the slope."""
        if throttle < 0.95:
            self.flat_since = None
            return 0.0
        if self.flat_since is None:
            self.flat_since = t
        boost = min(1.0, 0.3 + 0.7 * (t - self.flat_since) / self.lag) if self.lag else 1.0
        slope = self.grade(self.distance) if self.grade else 0.0
        return power(rpm) * boost / speed - DRAG_C0 - DRAG_C2 * speed * speed - G * slope

    def sample(self, rpm, speed, gear, car, dt):
        self.distance += speed * dt
        sample = Sample(rpm, LIMITER, gear=gear, speed=speed, car=car)
        if self.grade and self.forward:
            slope = self.grade(self.distance)
            sample.forward = (math.sqrt(1 - slope * slope), slope, 0.0)
        return sample


def drive(learner, shift_at=LIMITER, car='test-car', runs=3, jitter=0.0, road=None, t=0.0):
    """Full-throttle pulls through the gears, changing up at `shift_at`."""
    road = road or Road()
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
                    road.accel(t, rpm, speed, 0.0)
                    learner.feed(t, road.sample(rpm * 0.9, speed, 0, car, dt), LIMITER, 0.0, 1.0)
                gear += 1
                continue
            if rpm >= LIMITER:
                break
            speed = max(3.0, speed + road.accel(t, rpm, speed, 1.0) * dt)
            t += dt
            wobble = 1 + jitter * math.sin(t * 37)
            learner.feed(t, road.sample(RATIOS[gear] * speed * wobble, speed, gear, car, dt), LIMITER, 1.0, 0.0)
        t += 5
    return t


def cruise(learner, t, gear, speed, car='test-car', seconds=1.0, slip=0.0, road=None):
    """Part throttle at a steady speed: where the ratios are learnt."""
    road = road or Road()
    for _ in range(int(seconds * 60)):
        t += 1 / 60
        road.accel(t, 0, speed, 0.3)
        learner.feed(t, road.sample(RATIOS[gear] * speed * (1 + slip), speed, gear, car, 1 / 60), LIMITER, 0.3, 0.0)
    return t


def exits(learner, car='test-car', runs=2, road=None, t=1000.0, jitter=0.0):
    """Full-throttle pulls from low revs in every gear, as out of corners,
    each after a stretch of part throttle."""
    road = road or Road()
    for run in range(runs):
        for gear in RATIOS:
            speed = 2500.0 / RATIOS[gear]
            t = cruise(learner, t, gear, speed, car, road=road)
            while RATIOS[gear] * speed < LIMITER and speed < 70:
                speed = max(3.0, speed + road.accel(t, RATIOS[gear] * speed, speed, 1.0) / 60)
                t += 1 / 60
                wobble = 1 + jitter * math.sin(t * 37)
                learner.feed(t, road.sample(RATIOS[gear] * speed * wobble, speed, gear, car, 1 / 60),
                             LIMITER, 1.0, 0.0)
                if road.flat_since is not None and t - road.flat_since > 12:
                    break                            # a long climb in top gear
            for _ in range(30):                      # braking for the next corner
                t += 1 / 60
                speed *= 0.99
                road.accel(t, 0, speed, 0.0)
                learner.feed(t, road.sample(RATIOS[gear] * speed, speed, gear, car, 1 / 60), LIMITER, 0.0, 0.0)
    return t
