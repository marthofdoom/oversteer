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
import shutil
import sqlite3
import statistics
import threading
import time

from . import telemetry_formats

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
                'power_source': self.power_source, 'retuned': dict(self.retuned)}

    def advice(self, session_limiter_time=0.0):
        """Coaching from what has been learnt: a list of sentences."""
        tips = []
        gears = self.gears()
        ceiling = self.ceiling()
        for gear in gears:
            if gear + 1 not in gears:
                continue
            best = self.best_shift(gear)
            average = self.average_upshift(gear)
            if best is None or average is None or average[1] < 3:
                continue
            best_rpm, (shift_rpm, count) = best[0], average
            step = self.ratio(gear + 1) / self.ratio(gear)
            change = '{}→{}'.format(gear, gear + 1)
            if shift_rpm < best_rpm - 200:
                stay, after = self.power_at(shift_rpm), self.power_at(shift_rpm * step)
                cost = ''
                if stay and after and after < stay:
                    cost = ' At that speed gear {} gives {:.0f} % less drive than staying in {}.'.format(
                        gear + 1, (1 - after / stay) * 100, gear)
                tips.append('{}: you change up around {:.0f} rpm, {:.0f} early; hold it to about {:.0f}.{}'.format(
                    change, shift_rpm, best_rpm - shift_rpm, best_rpm, cost))
            elif shift_rpm > best_rpm + 200:
                if best_rpm >= ceiling * 0.99:
                    tips.append('{}: you change up around {:.0f} rpm, on the limiter; this car pulls to the '
                                'limiter in {}, so change as the lights flash.'.format(change, shift_rpm, gear))
                else:
                    stay, after = self.power_at(shift_rpm), self.power_at(shift_rpm * step)
                    cost = ''
                    if stay and after and stay < after:
                        cost = ' By then gear {} would give {:.0f} % more drive.'.format(
                            gear + 1, (after / stay - 1) * 100)
                    tips.append('{}: you change up around {:.0f} rpm, {:.0f} late; change at about {:.0f}.{}'.format(
                        change, shift_rpm, shift_rpm - best_rpm, best_rpm, cost))
            else:
                tips.append('{}: spot on, around {:.0f} rpm ({} changes).'.format(change, shift_rpm, count))
        if session_limiter_time > 3:
            tips.append('{:.0f} s on the limiter at full throttle this session: the engine makes nothing '
                        'there. Change up when the lights flash.'.format(session_limiter_time))
        for gear in sorted(self.retuned):
            tips.append('Gear {} was re-tuned; its shift points are being learnt again.'.format(gear))
        needed = int(ceiling * 0.5 / POWER_BIN) if ceiling else 0
        bands = sum(1 for v in self.power.values() if len(v) >= POWER_MIN)
        if needed and bands < needed * 0.8:
            tips.append('Still learning the engine ({} of about {} rev bands known): full-throttle pulls from '
                        'low revs, out of slow corners, fill it in fastest.'.format(bands, needed))
        return tips


SCHEMA = """
CREATE TABLE IF NOT EXISTS cars (
    profile TEXT NOT NULL,
    key TEXT NOT NULL,
    name TEXT,
    model TEXT NOT NULL,            -- CarModel.to_dict() as JSON
    updated REAL,
    PRIMARY KEY (profile, key)
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    profile TEXT NOT NULL,
    car TEXT NOT NULL,
    track TEXT,
    discipline TEXT,                -- not detected yet
    surface TEXT,                   -- not detected yet
    started REAL NOT NULL,
    ended REAL,
    limiter_time REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY,
    session INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    at REAL NOT NULL,
    gear INTEGER NOT NULL,          -- changed up from
    rpm REAL NOT NULL,
    best REAL,                      -- the learnt best at the time, if known
    throttle REAL,
    method TEXT                     -- 'h-pattern', 'sequential', 'paddles', or NULL
);
CREATE INDEX IF NOT EXISTS sessions_car ON sessions (profile, car, started);
CREATE INDEX IF NOT EXISTS shifts_session ON shifts (session);
"""

CODEMASTERS_KEY = re.compile(r'^codemasters-(\d+)-(\d+)-(\d+)$')
CODEMASTERS_NAME = re.compile(r'^\d+ rpm, \d+ gears$')      # the decoder's own name, not the user's


def rescale_codemasters(db, path=None):
    """One-off, at schema version 0: DiRT Rally sends engine rates in
    rad/s, which Oversteer read as rpm / 10 until the decoder was fixed,
    so every rpm it learnt about those cars is 30/pi too high. The unit is
    decided from the game's max in the key (floats[63] x 10, rounded), not
    the model's limiter, which is often a launch-learnt figure bouncing on
    the limiter and never round. A car whose max is not a round rpm in
    rad/s (a WRC Generations car read right) is left as it was. The file
    is copied to `<path>.v0.bak` first when anything changes."""
    rows = db.execute("SELECT profile, key, name, model FROM cars WHERE key LIKE 'codemasters-%'").fetchall()
    todo = []
    for profile, key, name, model in rows:
        match = CODEMASTERS_KEY.match(key)
        if match is None:
            continue
        key_max, key_idle, gears = (int(x) for x in match.groups())
        if telemetry_formats.codemasters_unit(key_max / 10.0) != telemetry_formats.RAD_S:
            logging.info("shift learner: %s kept as it is (its max is a round rpm as read)", key)
            continue
        todo.append((profile, key, name, model, key_max, key_idle, gears))
    if todo and path is not None and not os.path.exists(path + '.v0.bak'):
        try:
            db.commit()
            shutil.copy2(path, path + '.v0.bak')
        except OSError as e:
            logging.warning("shift learner: no backup before the DiRT rpm fix: %s", e)
    factor = 3.0 / math.pi               # stored = raw x 10; true = raw x 30/pi
    try:
        for profile, key, name, model, key_max, key_idle, gears in todo:
            top = round(key_max * factor, -1)
            new_key = 'codemasters-{:.0f}-{:.0f}-{:.0f}'.format(top, round(key_idle * factor, -1), gears)
            if db.execute('SELECT 1 FROM cars WHERE profile = ? AND key = ?', (profile, new_key)).fetchone():
                logging.warning("shift learner: %s not rescaled, %s exists already", key, new_key)
                continue
            try:
                car = CarModel.from_dict(json.loads(model))
            except (ValueError, KeyError, TypeError) as e:
                logging.warning("shift learner: can't read %s: %s", key, e)
                continue
            car.key = new_key
            car.limiter *= factor
            car.top_seen *= factor
            car.ratios = {g: [r * factor for r in v] for g, v in car.ratios.items()}
            car.upshifts = {g: [r * factor for r in v] for g, v in car.upshifts.items()}
            power = {}
            for band in sorted(car.power):
                # The band's middle, rescaled, lands in the new band
                power.setdefault(int((band + 0.5) * factor), []).extend(car.power[band])
            car.power = {b: v[-POWER_KEEP:] for b, v in power.items()}
            if name is None or CODEMASTERS_NAME.match(name):
                name = '{:.0f} rpm, {} gears'.format(top, gears)
            car.name = name
            db.execute('UPDATE cars SET key = ?, name = ?, model = ? WHERE profile = ? AND key = ?',
                       (new_key, name, json.dumps(car.to_dict()), profile, key))
            db.execute('UPDATE shifts SET rpm = rpm * ?, best = best * ? WHERE session IN '
                       '(SELECT id FROM sessions WHERE profile = ? AND car = ?)', (factor, factor, profile, key))
            db.execute('UPDATE sessions SET car = ? WHERE profile = ? AND car = ?', (new_key, profile, key))
            logging.info("shift learner: %s is %s (DiRT rpm were read x 10 instead of rad/s)", key, new_key)
        db.execute('PRAGMA user_version = 1')
        db.commit()
    except sqlite3.Error as e:
        db.rollback()
        logging.warning("shift learner: DiRT rpm fix not applied: %s", e)


SHIFT_PRESS_WINDOW = 0.6         # a shifter/wheel button this long before a change made it


class ShiftLearner:
    """Feeds telemetry into the current car's model and keeps everything in
    an SQLite database: the cars (per Oversteer profile), each driving
    session, and every upshift, which is what long-term coaching reads.
    Thread-safe: the listener feeds, the GUI reads; one lock serialises
    all use of the connection."""

    def __init__(self, database=None, profile='_no_profile'):
        self.lock = threading.Lock()
        self.profile = profile
        self.car = None
        self.enabled = True
        self.db = None
        self._reset_motion()
        self._dirty = False
        self._saved_at = time.monotonic()
        self.session = None                      # sessions.id of the drive going on
        self.session_limiter_time = 0.0
        self._shift_cache = {}
        self._shift_cache_at = 0.0
        if database is not None:
            self.open(database)

    def open(self, path):
        with self.lock:
            if path != ':memory:':
                os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            self.db = sqlite3.connect(path, check_same_thread=False)
            self.db.execute('PRAGMA foreign_keys = ON')
            self.db.executescript(SCHEMA)
            self.db.commit()
            if self.db.execute('PRAGMA user_version').fetchone()[0] < 1:
                rescale_codemasters(self.db, None if path == ':memory:' else path)

    def _reset_motion(self):
        self._speeds = collections.deque()       # (t, speed) for the acceleration
        self._gear = None
        self._gear_since = 0.0
        self._last = None                        # previous (t, rpm, gear, throttle)
        self._left = None                        # (gear, rpm, throttle, t, via neutral) when a forward gear was left
        self._off_ratio = {}                     # gear -> part-throttle ratios off the known one

    # -- storage --

    def set_profile(self, profile):
        """Switch to another Oversteer profile's cars."""
        with self.lock:
            if profile == self.profile:
                return
            self._end_session_locked()
            self._save_locked()
            self.profile = profile
            key, name = (self.car.key, self.car.name) if self.car else (None, None)
            self.car = None
            if key is not None:
                self._load_locked(key, name)

    def _load_locked(self, key, name):
        car = None
        if self.db is not None:
            row = self.db.execute('SELECT model, name FROM cars WHERE profile = ? AND key = ?',
                                  (self.profile, key)).fetchone()
            if row is not None:
                try:
                    car = CarModel.from_dict(json.loads(row[0]))
                    car.name = row[1] or car.name
                except (ValueError, KeyError, TypeError) as e:
                    logging.warning("shift learner: can't read %s: %s", key, e)
        self.car = car or CarModel(key, name)
        self._reset_motion()
        self.session_limiter_time = 0.0

    def _save_locked(self):
        if self.car is None or not self._dirty or self.db is None:
            return
        try:
            self.db.execute('INSERT OR REPLACE INTO cars (profile, key, name, model, updated) VALUES (?, ?, ?, ?, ?)',
                            (self.profile, self.car.key, self.car.name, json.dumps(self.car.to_dict()), time.time()))
            if self.session is not None:
                self.db.execute('UPDATE sessions SET ended = ?, limiter_time = ? WHERE id = ?',
                                (time.time(), self.session_limiter_time, self.session))
            self.db.commit()
            self._dirty = False
            self._saved_at = time.monotonic()
        except sqlite3.Error as e:
            logging.warning("shift learner: can't save %s: %s", self.car.key, e)

    def _start_session_locked(self, track):
        if self.db is None or self.car is None:
            return
        cursor = self.db.execute('INSERT INTO sessions (profile, car, track, started) VALUES (?, ?, ?, ?)',
                                 (self.profile, self.car.key, track, time.time()))
        self.session = cursor.lastrowid
        self.session_limiter_time = 0.0

    def _end_session_locked(self):
        if self.session is None:
            return
        self._dirty = True
        self._save_locked()
        # A session with no upshift taught nothing about the driver
        if self.db is not None:
            self.db.execute('DELETE FROM sessions WHERE id = ? AND NOT EXISTS '
                            '(SELECT 1 FROM shifts WHERE session = ?)', (self.session, self.session))
            self.db.commit()
        self.session = None

    def save(self):
        with self.lock:
            self._end_session_locked()
            self._save_locked()

    def known_cars(self):
        """[(key, name)] learnt in this profile."""
        if self.db is None:
            return []
        with self.lock:
            return [(k, n or k) for k, n in self.db.execute(
                'SELECT key, name FROM cars WHERE profile = ? ORDER BY name', (self.profile,))]

    def rename(self, key, name):
        with self.lock:
            if self.car is not None and self.car.key == key:
                self.car.name = name
                self._dirty = True
                self._save_locked()
            elif self.db is not None:
                self.db.execute('UPDATE cars SET name = ? WHERE profile = ? AND key = ?', (name, self.profile, key))
                self.db.commit()

    def forget(self, key):
        """Start the car over, history and all."""
        with self.lock:
            if self.db is not None:
                self.db.execute('DELETE FROM cars WHERE profile = ? AND key = ?', (self.profile, key))
                self.db.execute('DELETE FROM sessions WHERE profile = ? AND car = ?', (self.profile, key))
                self.db.commit()
            if self.car is not None and self.car.key == key:
                self.car = CarModel(key, self.car.name)
                self.session = None
                self._reset_motion()
                self._dirty = False

    def history(self, key, limit=10):
        """The car's recent sessions, newest first: dicts with started,
        track, shifts, mean error to the best (rpm, signed), limiter time,
        and the shifting methods used."""
        if self.db is None:
            return []
        with self.lock:
            rows = self.db.execute(
                'SELECT s.id, s.started, s.track, s.limiter_time, COUNT(h.id), AVG(h.rpm - h.best), '
                '       GROUP_CONCAT(DISTINCT h.method) '
                'FROM sessions s LEFT JOIN shifts h ON h.session = s.id '
                'WHERE s.profile = ? AND s.car = ? GROUP BY s.id ORDER BY s.started DESC LIMIT ?',
                (self.profile, key, limit)).fetchall()
        return [{'id': r[0], 'started': r[1], 'track': r[2], 'limiter_time': r[3] or 0.0, 'shifts': r[4],
                 'error': r[5], 'methods': sorted((r[6] or '').split(',')) if r[6] else []} for r in rows]

    def load_snapshot(self, key):
        """A saved car's snapshot without switching to it."""
        with self.lock:
            if self.car is not None and self.car.key == key:
                return self.car.snapshot()
            row = self.db.execute('SELECT model, name FROM cars WHERE profile = ? AND key = ?',
                                  (self.profile, key)).fetchone() if self.db is not None else None
        if row is None:
            return None
        try:
            car = CarModel.from_dict(json.loads(row[0]))
        except (ValueError, KeyError, TypeError):
            return None
        car.name = row[1] or car.name
        data = car.snapshot()
        data['session_limiter_time'] = 0.0
        data['advice'] = car.advice()
        return data

    # -- live --

    def snapshot(self):
        with self.lock:
            if self.car is None:
                return None
            data = self.car.snapshot()
            data['session_limiter_time'] = self.session_limiter_time
            data['advice'] = self.car.advice(self.session_limiter_time)
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
        """Telemetry stopped (menus, loading, the end of a stage): the
        session is over; keep what was learnt."""
        with self.lock:
            self._end_session_locked()
            self._save_locked()
            self._reset_motion()

    def feed(self, now, sample, limiter, throttle, clutch, press=None):
        """One telemetry packet. `limiter` is the best known ceiling;
        `throttle` and `clutch` are pressed fractions (None = unknown);
        `press` is (monotonic time, 'shifter' or 'wheel') of the last
        button that could have changed gear."""
        if not self.enabled or sample.car is None:
            return
        with self.lock:
            if self.car is None or self.car.key != sample.car:
                self._end_session_locked()
                self._save_locked()
                self._load_locked(sample.car, sample.car_name)
                self._shift_cache = {}
            if self.session is None:
                self._start_session_locked(getattr(sample, 'track', None))
            self._press = press
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
                self._left = (self._gear, previous[1], previous[3], now, gear == 0)
            elif gear == 0 and self._left is not None:
                self._left = self._left[:4] + (True,)
            left = self._left
            if left is not None and gear == left[0] + 1 and now - left[3] <= 1.5 and speed > 3.0:
                if left[2] is None or left[2] >= 0.8:
                    shifts = car.upshifts.setdefault(left[0], [])
                    shifts.append(left[1])
                    del shifts[:-SHIFTS_KEEP]
                    self._dirty = True
                    self._record_shift_locked(car, left, now)
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

    def _record_shift_locked(self, car, left, now):
        """Keep the change up for the long run: gear, rpm, the learnt best
        then, and how it was made."""
        if self.db is None or self.session is None:
            return
        gear, rpm, throttle, left_at, via_neutral = left
        if via_neutral:
            method = 'h-pattern'
        else:
            press = getattr(self, '_press', None)
            kind = press[1] if press and 0 <= left_at - press[0] <= SHIFT_PRESS_WINDOW else None
            method = {'shifter': 'sequential', 'wheel': 'paddles'}.get(kind)
        best = car.best_shift(gear)
        try:
            self.db.execute('INSERT INTO shifts (session, at, gear, rpm, best, throttle, method) '
                            'VALUES (?, ?, ?, ?, ?, ?, ?)',
                            (self.session, time.time(), gear, rpm, best[0] if best else None, throttle, method))
        except sqlite3.Error as e:
            logging.warning("shift learner: %s", e)

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
