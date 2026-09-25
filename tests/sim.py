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


# -- a car on a course: stages, circuits and corners for the run tracker --

STAGE = [('straight', 300), ('left', 60, 70), ('straight', 250), ('right', 45, 90), ('straight', 400),
         ('left', 80, 45), ('right', 50, 60), ('straight', 350), ('right', 70, 80), ('straight', 300),
         ('left', 40, 100), ('straight', 450), ('right', 90, 50), ('straight', 300), ('left', 50, 80),
         ('straight', 500), ('right', 60, 40), ('straight', 400), ('left', 70, 60), ('straight', 300)]
STAGE_CORNERS = [1, -1, 1, -1, -1, 1, -1, 1, -1, 1]          # left 1, right -1, in order
CIRCUIT = [('straight', 400), ('left', 60, 90), ('straight', 200), ('left', 60, 90), ('straight', 400),
           ('left', 60, 90), ('straight', 200), ('left', 60, 90)]


class Course:
    """A road as points every metre: (s, x, y, z, heading, curvature). World
    x and z are horizontal, y up; heading turns positive to the left (seen
    from above), as yaw rate does."""

    def __init__(self, pieces, grade=0.0, start=(100.0, 20.0, 104.9), heading=0.3):
        self.points = []
        x, y, z = start
        s = 0.0
        for piece in pieces:
            if piece[0] == 'straight':
                steps, curvature = int(piece[1]), 0.0
            else:
                radius, degrees = piece[1], piece[2]
                steps = int(round(radius * math.radians(degrees)))
                curvature = (1.0 if piece[0] == 'left' else -1.0) * math.radians(degrees) / steps
            for _ in range(steps):
                self.points.append((s, x, y, z, heading, curvature))
                # a step along the chord: a loop closes on itself
                x += math.cos(heading + curvature / 2)
                z -= math.sin(heading + curvature / 2)
                heading += curvature
                y += grade
                s += 1.0
        self.length = s

    def at(self, s):
        return self.points[min(int(s), len(self.points) - 1)]

    def speeds(self, top=30.0, lateral=8.0, accel=4.0, brake=7.0, rolling=False):
        """Speed per point: the corners' limit, reached and left within the
        car's acceleration and braking."""
        limit = [min(top, math.sqrt(lateral / abs(p[5]))) if p[5] else top for p in self.points]
        limit[0] = top if rolling else 0.0
        for i in range(1, len(limit)):
            limit[i] = min(limit[i], math.sqrt(limit[i - 1] ** 2 + 2 * accel))
        for i in range(len(limit) - 2, -1, -1):
            limit[i] = min(limit[i], math.sqrt(limit[i + 1] ** 2 + 2 * brake))
        return limit


def course_samples(course, laps=1, game='dirt', car='dirt/7500-800-5', standing=3.0, rolling=False, slide=False,
                   packets=False, top=30.0, t=0.0, dt=1 / 60):
    """(t, sample, throttle) along the course: a stand at the start with the
    revs held (a launch), then the laps. `slide`: past the middle of each
    left-hand corner the car slides and the driver steers right against
    it. `packets`: EA SPORTS WRC's start and end packets."""
    speeds = course.speeds(top=top, rolling=rolling)
    out = []

    def sample(s, v, a_long, lap, lap_time, total_s, packet=None, throttle=0.3):
        _, x, y, z, heading, curvature = course.at(s)
        gear = next((g for g in sorted(RATIOS) if RATIOS[g] * v <= 6800.0), 5)
        rpm = max(900.0, RATIOS[gear] * v) if v > 0.5 else 5500.0 if throttle > 0.5 else 900.0
        sm = Sample(rpm, LIMITER, gear=gear if v > 0.5 else 1, speed=v, car=car, game=game,
                    throttle=throttle, clutch=0.0, brake=0.6 if a_long < -1.0 else 0.0)
        grade = course.points[1][2] - course.points[0][2] if len(course.points) > 1 else 0.0
        sm.pos = (x, y, z)
        sm.forward = (math.cos(heading), grade, -math.sin(heading))
        sm.yaw_rate = v * curvature
        steer = max(-1.0, min(1.0, curvature * 15.0))
        if slide and curvature > 0 and v > 5:
            piece_start = s
            while piece_start > 0 and course.at(piece_start - 1)[5] == curvature:
                piece_start -= 1
            if s - piece_start > 20:
                steer = -0.2                                    # caught the slide: steering right in a left
        sm.steer = steer
        sm.accel = (a_long, v * v * curvature, 0.0)
        sm.accel_kind = 'kinematic'
        sm.wheel_speed = (v, v, v, v)
        sm.handbrake = 0.0
        sm.stage_time = lap_time
        sm.lap, sm.laps = lap, laps if laps > 1 else 0
        if game == 'eawrc':
            sm.distance, sm.stage_length = s + 0.0, course.length
            sm.progress = s / course.length
            sm.packet = packet or 'update'
            sm.stage = 'eawrc:4:12'
        else:
            sm.lap_distance, sm.stage_length = s + 0.0, course.length
            sm.progress = (total_s / (course.length * laps))
        return sm

    if not rolling:
        for i in range(int(standing / dt)):
            t += dt
            out.append((t, sample(0.0, 0.0, 0.0, 0, 0.0, 0.0, 'start' if packets and i == 0 else None, 0.9), 0.9))
    lap_time = 0.0
    total = 0.0
    for lap in range(laps):
        s = 0.0
        lap_time = 0.0
        v = speeds[0] if lap == 0 else speeds[-1]
        while s < course.length - 1:
            target = speeds[min(int(s) + 1, len(speeds) - 1)]
            a_long = (target - v) / dt
            a_long = max(-7.0, min(4.0, a_long))
            v = max(0.5, v + a_long * dt)
            s += v * dt
            total += v * dt
            t += dt
            lap_time += dt
            throttle = 1.0 if a_long > 0.5 else 0.0 if a_long < -1.0 else 0.3
            out.append((t, sample(s, v, a_long, lap, lap_time, total, None, throttle), throttle))
    if packets:
        last = out[-1][1]
        last.packet = 'end'
        last.progress = 1.0
    return out


def feed_course(learner, samples):
    for t, sample, throttle in samples:
        learner.feed(t, sample, LIMITER, throttle, 0.0)
    return samples[-1][0]


def eawrc_packet(sample):
    """The sample as EA SPORTS WRC would send it in Oversteer's structure
    (the one format the bench and the end-to-end tests encode)."""
    import struct
    from oversteer.telemetry_formats import EAWRC_CHANNELS, EAWRC_FORMAT
    forward = sample.forward or (1.0, 0.0, 0.0)
    up = (0.0, 1.0, 0.0)
    left = (up[1] * forward[2] - up[2] * forward[1], up[2] * forward[0] - up[0] * forward[2],
            up[0] * forward[1] - up[1] * forward[0])
    a_long, a_lat = (sample.accel or (0.0, 0.0, 0.0))[:2]
    values = dict.fromkeys(EAWRC_CHANNELS, 0)
    values.update({
        'vehicle_gear_index': sample.gear if sample.gear and sample.gear > 0 else 0,
        'vehicle_gear_index_neutral': 0, 'vehicle_gear_index_reverse': 9, 'vehicle_gear_maximum': 5,
        'vehicle_speed': sample.speed, 'vehicle_transmission_speed': sample.speed,
        'vehicle_engine_rpm_max': LIMITER, 'vehicle_engine_rpm_idle': 900.0, 'vehicle_engine_rpm_current': sample.rpm,
        'vehicle_throttle': sample.throttle or 0.0, 'vehicle_brake': sample.brake or 0.0,
        'vehicle_clutch': sample.clutch or 0.0, 'vehicle_steering': -(sample.steer or 0.0),
        'vehicle_handbrake': sample.handbrake or 0.0, 'stage_current_time': sample.stage_time or 0.0,
        'stage_current_distance': sample.distance or 0.0, 'stage_length': sample.stage_length or 0.0,
        'vehicle_id': 17, 'location_id': 4, 'route_id': 12, 'shiftlights_rpm_end': 7000.0,
    })
    for axis, i in zip('xyz', range(3)):
        values['vehicle_position_' + axis] = sample.pos[i] if sample.pos else 0.0
        values['vehicle_forward_direction_' + axis] = forward[i]
        values['vehicle_left_direction_' + axis] = left[i]
        values['vehicle_up_direction_' + axis] = up[i]
        values['vehicle_velocity_' + axis] = forward[i] * sample.speed
        values['vehicle_acceleration_' + axis] = forward[i] * a_long + left[i] * a_lat
    for wheel, i in (('fl', 0), ('fr', 1), ('bl', 2), ('br', 3)):
        values['vehicle_cp_forward_speed_' + wheel] = sample.wheel_speed[i] if sample.wheel_speed else sample.speed
    fourcc = {'start': b'SESS', 'end': b'SESE', 'pause': b'SESP', 'resume': b'SESR'}.get(sample.packet, b'SESU')
    return struct.pack(EAWRC_FORMAT, fourcc, *[values[c] for c in EAWRC_CHANNELS])


def stage_packets(minutes=10.0):
    """(t, packet): EA SPORTS WRC stages back to back for about `minutes`."""
    out, t = [], 0.0
    while t < minutes * 60:
        for t, sample, _ in course_samples(Course(STAGE), game='eawrc', car='eawrc/17', packets=True, t=t):
            out.append((t, eawrc_packet(sample)))
        t += 5.0
    return out
