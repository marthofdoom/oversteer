"""Learning each car's best upshift points from telemetry.

The best moment to change up is where the next gear, at the same road
speed, gives more drive than staying in this one. Drive at a given speed
is proportional to engine power (force = power / speed), so what we need
is the engine's power curve and each gear's ratio:

- Power: at full throttle with the clutch out, the car's acceleration
  (plus what drag and rolling resistance take, which we estimate) times
  its speed is proportional to engine power, whatever the gear. Forza
  sends the power itself. Samples are kept per 100 rpm band; the median
  of each band makes slopes, bumps and the odd slide wash out.
- Ratios: engine rpm per m/s in each gear, from the samples themselves.
  A sample whose ratio is off its gear's (wheelspin, a jump) is not used
  for power.

For each gear the best shift is then the lowest rpm from which the next
gear's power at the same speed (rpm x next ratio / this ratio) is higher
than this gear's, or the limiter if that never happens.

Everything is per car and kept on disk, per Oversteer profile, so the
learning continues across sessions. The listener thread feeds samples;
the GUI reads snapshots.
"""

import collections
import json
import logging
import math
import os
import re
import statistics
import threading
import time

POWER_BIN = 100                  # rpm per power band
POWER_KEEP = 40                  # samples kept per band (most recent)
POWER_MIN = 4                    # samples a band needs to count
RATIO_KEEP = 200
RATIO_MIN = 20
RATIO_TOLERANCE = 0.04           # a sample off its gear's ratio by more is spin or a jump
RETUNE_SAMPLES = 90              # part-throttle samples agreeing on a new ratio: the gear was re-tuned
RETUNE_SPREAD = 0.015
SHIFTS_KEEP = 40
FULL_THROTTLE = 0.95
CLUTCH_OUT = 0.1
MIN_SPEED = 5.0                  # m/s
SETTLED = 0.25                   # seconds in a gear before its samples count
ACCEL_WINDOW = 0.3               # seconds of speed history for the acceleration
ACCEL_MIN_POINTS = 5
SHIFT_STEP = 25                  # rpm resolution of the search
SHIFT_CONFIRM = 3                # steps the next gear must stay ahead (noise)
LIMITER_BAND = 0.985             # within this of the limiter counts as on it
DRAG_C0 = 0.15                   # m/s^2: rolling resistance, typical car
DRAG_C2 = 3.5e-4                 # 1/m: aerodynamic drag / mass, typical car
SAVE_EVERY = 20.0                # seconds between saves while learning


def _median(values):
    return statistics.median(values) if values else None


def safe_name(text):
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', text)[:80] or 'car'


class CarModel:
    """What has been learnt about one car."""

    def __init__(self, key, name=None):
        self.key = key
        self.name = name or key
        self.limiter = 0.0
        self.power_source = None         # 'game' (Forza's power figure) or 'accel'
        self.power = {}                  # band -> [power samples]
        self.ratios = {}                 # gear -> [rpm per m/s]
        self.upshifts = {}               # gear -> [rpm at the change up to gear + 1]
        self.limiter_time = 0.0          # seconds on the limiter at full throttle (all sessions)
        self.top_seen = 0.0              # highest rpm seen flat out in gear: where the data ends
        self.retuned = {}                # gear -> time.time() its ratio was seen to change

    # -- persistence --

    def to_dict(self):
        return {
            'key': self.key, 'name': self.name, 'limiter': self.limiter,
            'power_source': self.power_source,
            'power': {str(b): v for b, v in self.power.items()},
            'ratios': {str(g): v for g, v in self.ratios.items()},
            'upshifts': {str(g): v for g, v in self.upshifts.items()},
            'limiter_time': self.limiter_time,
            'top_seen': self.top_seen,
            'retuned': {str(g): v for g, v in self.retuned.items()},
        }

    @classmethod
    def from_dict(cls, data):
        car = cls(data['key'], data.get('name'))
        car.limiter = float(data.get('limiter') or 0.0)
        car.power_source = data.get('power_source')
        car.power = {int(b): [float(x) for x in v][-POWER_KEEP:] for b, v in (data.get('power') or {}).items()}
        car.ratios = {int(g): [float(x) for x in v][-RATIO_KEEP:] for g, v in (data.get('ratios') or {}).items()}
        car.upshifts = {int(g): [float(x) for x in v][-SHIFTS_KEEP:] for g, v in (data.get('upshifts') or {}).items()}
        car.limiter_time = float(data.get('limiter_time') or 0.0)
        car.top_seen = float(data.get('top_seen') or 0.0)
        car.retuned = {int(g): float(v) for g, v in (data.get('retuned') or {}).items()}
        return car

    def ceiling(self):
        """The highest rpm worth changing up at: the limiter, or where the
        car has actually been taken when that is lower (a game whose
        reported maximum is above the real limiter)."""
        if self.top_seen and self.limiter and self.top_seen < self.limiter:
            return max(self.top_seen, self.limiter * 0.5)
        return self.limiter or self.top_seen

    # -- what it knows --

    def ratio(self, gear):
        samples = self.ratios.get(gear)
        return _median(samples) if samples and len(samples) >= RATIO_MIN else None

    def gears(self):
        return sorted(g for g in self.ratios if self.ratio(g) is not None)

    def power_at(self, rpm):
        """Power at an rpm, interpolated between the bands either side;
        None where too little is known."""
        position = rpm / POWER_BIN - 0.5
        low = math.floor(position)
        weight = position - low
        values = []
        for band, w in ((low, 1.0 - weight), (low + 1, weight)):
            samples = self.power.get(band)
            if samples is None or len(samples) < POWER_MIN:
                if w > 0.25:
                    return None
                continue
            values.append((_median(samples), w))
        if not values:
            return None
        total = sum(w for _, w in values)
        return sum(v * w for v, w in values) / total

    def best_shift(self, gear):
        """(rpm, coverage 0..1) for changing up from `gear`, or None when
        there isn't enough known yet. rpm is the limiter when staying in
        gear always pulls harder."""
        this, following = self.ratio(gear), self.ratio(gear + 1)
        ceiling = self.ceiling()
        if not this or not following or not ceiling:
            return None
        step = following / this
        start = ceiling * 0.5
        known = checked = 0
        ahead = 0
        rpm = start
        crossing = None
        while rpm <= ceiling:
            checked += 1
            stay, change = self.power_at(rpm), self.power_at(rpm * step)
            if stay is not None and change is not None:
                known += 1
                if change > stay:
                    ahead += 1
                    if crossing is None:
                        crossing = rpm
                    if ahead >= SHIFT_CONFIRM:
                        return (crossing, known / checked)
                else:
                    ahead, crossing = 0, None
            rpm += SHIFT_STEP
        coverage = known / checked if checked else 0.0
        # Never beaten: hold it to the limiter, if the top end is known
        if self.power_at(ceiling * 0.97) is not None and coverage >= 0.5:
            return (ceiling, coverage)
        return None

    def average_upshift(self, gear):
        shifts = self.upshifts.get(gear)
        return (statistics.mean(shifts), len(shifts)) if shifts else None

    def snapshot(self):
        """A plain dict for the GUI."""
        gears = self.gears()
        rows = []
        for gear in gears:
            best = self.best_shift(gear) if gear + 1 in gears else None
            average = self.average_upshift(gear)
            rows.append({
                'gear': gear,
                'ratio': self.ratio(gear),
                'ratio_samples': len(self.ratios.get(gear, [])),
                'best': best[0] if best else None,
                'coverage': best[1] if best else 0.0,
                'average_shift': average[0] if average else None,
                'shifts': average[1] if average else 0,
                'last': gear + 1 not in gears,
            })
        bands = sum(1 for v in self.power.values() if len(v) >= POWER_MIN)
        return {'key': self.key, 'name': self.name, 'limiter': self.ceiling(), 'gears': rows,
                'power_bands': bands, 'limiter_time': self.limiter_time,
                'power_source': self.power_source}


class ShiftLearner:
    """Feeds telemetry into the current car's model and keeps the models
    on disk. Thread-safe: the listener feeds, the GUI reads."""

    def __init__(self, directory=None):
        self.lock = threading.Lock()
        self.directory = directory
        self.car = None
        self.enabled = True
        self._reset_motion()
        self._dirty = False
        self._saved_at = time.monotonic()
        self.session_limiter_time = 0.0
        self._shift_cache = {}
        self._shift_cache_at = 0.0

    def _reset_motion(self):
        self._speeds = collections.deque()       # (t, speed) for the acceleration
        self._gear = None
        self._gear_since = 0.0
        self._last = None                        # previous (t, rpm, gear, throttle)
        self._left = None                        # (gear, rpm, throttle, t) when a forward gear was left
        self._off_ratio = {}                     # gear -> part-throttle ratios off the known one

    # -- storage --

    def set_directory(self, directory):
        """Switch to another Oversteer profile's cars."""
        with self.lock:
            self._save_locked()
            self.directory = directory
            key, name = (self.car.key, self.car.name) if self.car else (None, None)
            self.car = None
            if key is not None:
                self._load_locked(key, name)

    def _path(self, key):
        return os.path.join(self.directory, safe_name(key) + '.json') if self.directory else None

    def _load_locked(self, key, name):
        path = self._path(key)
        car = None
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    car = CarModel.from_dict(json.load(f))
            except (OSError, ValueError, KeyError, TypeError) as e:
                logging.warning("shift learner: can't read %s: %s", path, e)
        self.car = car or CarModel(key, name)
        self._reset_motion()
        self.session_limiter_time = 0.0

    def _save_locked(self):
        if self.car is None or not self._dirty or not self.directory:
            return
        path = self._path(self.car.key)
        try:
            os.makedirs(self.directory, exist_ok=True)
            tmp = path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(self.car.to_dict(), f)
            os.replace(tmp, path)
            self._dirty = False
            self._saved_at = time.monotonic()
        except OSError as e:
            logging.warning("shift learner: can't save %s: %s", path, e)

    def save(self):
        with self.lock:
            self._save_locked()

    def known_cars(self):
        """[(key, name)] saved for this profile."""
        cars = []
        if self.directory and os.path.isdir(self.directory):
            for entry in sorted(os.listdir(self.directory)):
                if not entry.endswith('.json'):
                    continue
                try:
                    with open(os.path.join(self.directory, entry)) as f:
                        data = json.load(f)
                    cars.append((data['key'], data.get('name') or data['key']))
                except (OSError, ValueError, KeyError):
                    continue
        return cars

    def rename(self, key, name):
        with self.lock:
            if self.car is not None and self.car.key == key:
                self.car.name = name
                self._dirty = True
                self._save_locked()
                return
            path = self._path(key)
            try:
                with open(path) as f:
                    data = json.load(f)
                data['name'] = name
                with open(path, 'w') as f:
                    json.dump(data, f)
            except (OSError, ValueError, TypeError) as e:
                logging.warning("shift learner: rename %s: %s", key, e)

    def forget(self, key):
        """Start the car over."""
        with self.lock:
            path = self._path(key)
            if path and os.path.exists(path):
                os.remove(path)
            if self.car is not None and self.car.key == key:
                self.car = CarModel(key, self.car.name)
                self._reset_motion()
                self._dirty = False

    def load_snapshot(self, key):
        """A saved car's snapshot without switching to it."""
        with self.lock:
            if self.car is not None and self.car.key == key:
                return self.car.snapshot()
            path = self._path(key)
        try:
            with open(path) as f:
                return CarModel.from_dict(json.load(f)).snapshot()
        except (OSError, ValueError, KeyError, TypeError):
            return None

    # -- live --

    def snapshot(self):
        with self.lock:
            if self.car is None:
                return None
            data = self.car.snapshot()
            data['session_limiter_time'] = self.session_limiter_time
            return data

    def shift_rpm(self, gear):
        """The learnt upshift for `gear`, for the rev lights; None to fall
        back to the percentage rule."""
        with self.lock:
            if self.car is None or gear is None or gear < 1:
                return None
            # Worked out at most every couple of seconds: this runs per packet
            now = time.monotonic()
            if now - self._shift_cache_at > 2.0:
                self._shift_cache, self._shift_cache_at = {}, now
            if gear not in self._shift_cache:
                best = self.car.best_shift(gear)
                self._shift_cache[gear] = best[0] if best and best[1] >= 0.6 else None
            return self._shift_cache[gear]

    def idle(self):
        """Telemetry stopped (menus, loading): keep what was learnt."""
        with self.lock:
            self._save_locked()
            self._reset_motion()

    def feed(self, now, sample, limiter, throttle, clutch):
        """One telemetry packet. `limiter` is the best known ceiling;
        `throttle` and `clutch` are pressed fractions (None = unknown)."""
        if not self.enabled or sample.car is None:
            return
        with self.lock:
            if self.car is None or self.car.key != sample.car:
                self._save_locked()
                self._load_locked(sample.car, sample.car_name)
                self._shift_cache = {}
            car = self.car
            if limiter and limiter > car.limiter * 1.001 or (limiter and not car.limiter):
                car.limiter = limiter
                self._dirty = True
            self._feed_locked(car, now, sample, throttle, clutch)
            if self._dirty and now - self._saved_at > SAVE_EVERY:
                self._save_locked()

    def _feed_locked(self, car, now, sample, throttle, clutch):
        gear, speed, rpm = sample.gear, sample.speed, sample.rpm
        previous = self._last
        self._last = (now, rpm, gear, throttle)
        if gear is None or speed is None:
            return
        if gear != self._gear:
            # Where a change up happened: the last rpm in the old gear, also
            # through neutral (an H-pattern box shows it on the way)
            if self._gear is not None and self._gear >= 1 and previous is not None:
                self._left = (self._gear, previous[1], previous[3], now)
            left = self._left
            if left is not None and gear == left[0] + 1 and now - left[3] <= 1.5 and speed > 3.0:
                if left[2] is None or left[2] >= 0.8:
                    shifts = car.upshifts.setdefault(left[0], [])
                    shifts.append(left[1])
                    del shifts[:-SHIFTS_KEEP]
                    self._dirty = True
                self._left = None
            self._gear = gear
            self._gear_since = now
            self._speeds.clear()
        self._speeds.append((now, speed))
        while self._speeds and now - self._speeds[0][0] > ACCEL_WINDOW:
            self._speeds.popleft()

        clutch_out = clutch is None or clutch <= CLUTCH_OUT
        settled = now - self._gear_since >= SETTLED
        if gear < 1 or speed < MIN_SPEED or not clutch_out or not settled:
            return
        flat_out = throttle is not None and throttle >= FULL_THROTTLE
        if flat_out and rpm > car.top_seen:
            car.top_seen = rpm
        if flat_out and car.limiter and rpm >= car.limiter * LIMITER_BAND and previous is not None:
            dt = min(0.2, max(0.0, now - previous[0]))
            car.limiter_time += dt
            self.session_limiter_time += dt

        ratio = rpm / speed
        known = car.ratio(gear)
        if known is None or abs(ratio - known) <= known * RATIO_TOLERANCE:
            samples = car.ratios.setdefault(gear, [])
            samples.append(ratio)
            del samples[:-RATIO_KEEP]
            self._dirty = True
            self._off_ratio.pop(gear, None)
        else:
            # Off the gear's ratio: wheelspin or a jump, which must not move
            # it; or the gear was re-tuned, which shows at part throttle
            # too, steadily, where a car rarely spins
            self._check_retune(car, gear, ratio, throttle)
            return
        if not flat_out:
            return
        if car.limiter and rpm > car.limiter * 1.01:
            return
        if sample.power is not None:
            if car.power_source != 'game':
                car.power, car.power_source = {}, 'game'     # the real figure beats the estimate
            power = sample.power
        else:
            if car.power_source == 'game':
                return
            car.power_source = 'accel'
            accel = self._acceleration()
            if accel is None or accel <= 0:
                return
            power = (accel + DRAG_C0 + DRAG_C2 * speed * speed) * speed
        if power <= 0:
            return
        band = int(rpm // POWER_BIN)
        values = car.power.setdefault(band, [])
        values.append(power)
        del values[:-POWER_KEEP]

    def _check_retune(self, car, gear, ratio, throttle):
        if throttle is None or throttle > 0.5:
            return
        off = self._off_ratio.setdefault(gear, [])
        off.append(ratio)
        del off[:-RETUNE_SAMPLES]
        if len(off) < RETUNE_SAMPLES:
            return
        middle = _median(off)
        if all(abs(r - middle) <= middle * RETUNE_SPREAD for r in off):
            car.ratios[gear] = list(off)
            car.upshifts.pop(gear, None)             # shifts with the old gearing
            car.upshifts.pop(gear - 1, None)
            car.retuned[gear] = time.time()
            self._off_ratio.pop(gear, None)
            self._dirty = True
            logging.info("shift learner: %s gear %d re-tuned (%.1f rpm per m/s)", car.key, gear, middle)

    def _acceleration(self):
        """Least-squares slope of speed over the recent window."""
        points = self._speeds
        if len(points) < ACCEL_MIN_POINTS or points[-1][0] - points[0][0] < ACCEL_WINDOW * 0.5:
            return None
        n = len(points)
        mean_t = sum(t for t, _ in points) / n
        mean_v = sum(v for _, v in points) / n
        spread = sum((t - mean_t) ** 2 for t, _ in points)
        if spread <= 0:
            return None
        return sum((t - mean_t) * (v - mean_v) for t, v in points) / spread
