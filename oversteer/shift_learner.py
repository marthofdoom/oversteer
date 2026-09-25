"""Learning each car's best upshift points from telemetry.

The best moment to change up is where the next gear, at the same road
speed, gives more drive than staying in this one. Drive at a given speed
is proportional to engine power (force = power / speed), so what we need
is the engine's power curve and each gear's ratio:

- Power: at full throttle with the clutch out, the car's acceleration
  (plus what drag and rolling resistance take, which we estimate, and
  what the slope takes where the game says which way is up) times its
  speed is proportional to engine power, whatever the gear. Forza sends
  the power itself. Samples count once the throttle has been down a
  moment (turbo lag), off the brake, without wheelspin. They are kept
  per 100 rpm band, pooled and per gear; the median of each band (the
  75th percentile once slopes are taken out) makes bumps and the odd
  slide wash out.
- Ratios: engine rpm per m/s in each gear, from part-throttle samples
  at a steady speed off the brake, where the tyres barely slip. A
  full-throttle sample whose ratio is off its gear's (wheelspin, a jump)
  is not used for power.

For each gear the best shift is then the lowest rpm from which the next
gear's power at the same speed (rpm x next ratio / this ratio) is higher
than this gear's, or the limiter if that never happens. Each gear's own
curve is used where both gears' are known (at the same road speed drag
and slope cancel out), the pooled one elsewhere; bootstrap resamples of
the power samples give the answer a range.

Everything is per car and kept on disk, per Oversteer profile, so the
learning continues across sessions. The listener thread feeds samples;
the GUI reads snapshots.
"""

import collections
import logging
import math
import random
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
BRAKE_POWER = 0.05               # braking at least this much: no power sample
SLIP_POWER = {'raw': 0.08, 'normalised': 0.5}   # driven-wheel slip above which power is spin (calibrate per game)
SHIFT_WINDOW = 0.3               # seconds before a change in which its rpm and throttle are taken
SHIFT_SPAN = 1.5                 # seconds from leaving a gear to engaging the next that make one change
MISSED_NEUTRAL = 0.5             # seconds in neutral on a change up under throttle: a missed gate
OVER_REV = 0.95                  # a change down that takes the revs past this share of the limiter
DOUBLE_TAP = 0.25                # two changes the same way this close (calibrate)...
DOUBLE_TAP_REVERT = 1.0          # ...and the second taken back within this: a double tap
LOW_SLIP_THROTTLE = 0.5          # below this the tyres barely slip: ratios are learnt here only
BRAKE_OFF = 0.02
COASTING = 0.05                  # with no brake reading, a little throttle says the brake is off
STEADY_ACCEL = 2.0               # m/s^2: steady enough for a ratio sample
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
BOOST_HOLD = 0.8                 # s of full throttle before power counts: turbo lag (calibrate; learnt per car later)
BOOTSTRAP_RESAMPLES = 20         # for the confidence band of each best change up
BANDS_EVERY = 10.0               # seconds between working the bands out again, per car
SAVE_EVERY = 20.0                # seconds between saves while learning
SESSION_GAP = 120.0              # seconds without telemetry that end a session (a pause does not)
TIPS_SHOWN = 3                   # coaching tips at a time, the biggest first


G = 9.80665
DRIVEN = {'fwd': (0, 1), 'rwd': (2, 3), 'awd': (0, 1, 2, 3)}     # wheel indices (FL, FR, RL, RR)


def drive_slip(sample, drivetrain=None):
    """(slip, kind) of the driven wheels, or None where the game sends no
    wheel speeds or slip. From wheel speeds: how much faster the driven
    wheels turn than the car moves (0.1 = 10 %), 'raw'; from Forza's slip
    ratio, its own figure where 1 is the grip limit, 'normalised'. The
    driven wheels are the drivetrain's, or the fastest wheel when it is
    unknown (the driven ones are the ones that spin)."""
    wheels = DRIVEN.get(drivetrain)
    if sample.wheel_speed is not None and sample.speed and sample.speed > MIN_SPEED:
        speeds = [abs(v) for v in sample.wheel_speed]
        driven = sum(speeds[i] for i in wheels) / len(wheels) if wheels else max(speeds)
        return driven / sample.speed - 1.0, 'raw'
    if sample.slip_ratio is not None:
        values = [abs(v) for v in sample.slip_ratio]
        return max(values[i] for i in wheels) if wheels else max(values), sample.slip_kind or 'raw'
    return None


def _median(values):
    return statistics.median(values) if values else None


def safe_name(text):
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', text)[:80] or 'car'


def _quantile(values, q):
    """The q-quantile of values, interpolated (q = 0.5 is the median)."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _interpolate(curve, rpm):
    """A {band: value} curve at `rpm`, interpolated between the bands
    either side; None where too little is known."""
    position = rpm / POWER_BIN - 0.5
    low = math.floor(position)
    weight = position - low
    total = value = 0.0
    for band, w in ((low, 1.0 - weight), (low + 1, weight)):
        v = curve.get(band)
        if v is None:
            if w > 0.25:
                return None
            continue
        total += w
        value += v * w
    return value / total if total else None


LIMITER_SOURCES = {'seen': 0, 'game': 1, 'launch': 2}     # which ceiling beats which


class CarModel:
    """What has been learnt about one car."""

    VERSION = 2

    def __init__(self, key, name=None):
        self.key = key
        self.name = name or key
        self.limiter = 0.0
        self.limiter_source = None       # 'launch' (held on the limiter), 'game' (its max), 'seen' (highest rpm)
        self.power_source = None         # 'game' (Forza's power figure) or 'accel'
        self.power = {}                  # band -> [power samples], all gears
        self.power_g = {}                # (gear, band) -> [power samples] in that gear
        self.slope_free = False          # power from slope-corrected acceleration: a high quantile is safe
        self.ratios = {}                 # gear -> [rpm per m/s]
        self.upshifts = {}               # gear -> [rpm at the change up to gear + 1]
        self.limiter_time = 0.0          # seconds on the limiter at full throttle (all sessions)
        self.top_seen = 0.0              # highest rpm seen flat out in gear: where the data ends
        self.retuned = {}                # gear -> time.time() its ratio was seen to change
        self.boost_hold = BOOST_HOLD     # seconds of full throttle before power counts (turbo lag)
        self.drag = (DRAG_C0, DRAG_C2)
        self.game = None                 # Sample.game
        self.car_class = None
        self.drivetrain = None           # 'fwd', 'rwd', 'awd' where the game says

    # -- persistence --

    def to_dict(self):
        """A plain dict of copies: another thread may serialise it while
        the listener goes on learning."""
        return {
            'version': self.VERSION,
            'key': self.key, 'name': self.name, 'game': self.game, 'limiter': self.limiter,
            'limiter_source': self.limiter_source,
            'power_source': self.power_source,
            'power': {str(b): list(v) for b, v in self.power.items()},
            'power_g': {'{}:{}'.format(g, b): list(v) for (g, b), v in self.power_g.items()},
            'slope_free': self.slope_free,
            'ratios': {str(g): list(v) for g, v in self.ratios.items()},
            'upshifts': {str(g): list(v) for g, v in self.upshifts.items()},
            'limiter_time': self.limiter_time,
            'top_seen': self.top_seen,
            'retuned': {str(g): v for g, v in self.retuned.items()},
            'boost_hold': self.boost_hold,
            'drag': list(self.drag),
        }

    def copy(self):
        car = CarModel.from_dict(self.to_dict())
        car.car_class, car.drivetrain = self.car_class, self.drivetrain
        return car

    @classmethod
    def from_dict(cls, data):
        """Version 1 (pooled power only, no limiter source) is read as it
        is and saved as version 2."""
        car = cls(data['key'], data.get('name'))
        car.limiter = float(data.get('limiter') or 0.0)
        car.limiter_source = data.get('limiter_source') or ('game' if car.limiter else None)
        car.power_source = data.get('power_source')
        car.power = {int(b): [float(x) for x in v][-POWER_KEEP:] for b, v in (data.get('power') or {}).items()}
        power_g = {}
        for key, v in (data.get('power_g') or {}).items():
            gear, band = key.split(':')
            power_g[(int(gear), int(band))] = [float(x) for x in v][-POWER_KEEP:]
        car.power_g = power_g
        car.slope_free = bool(data.get('slope_free'))
        car.ratios = {int(g): [float(x) for x in v][-RATIO_KEEP:] for g, v in (data.get('ratios') or {}).items()}
        car.upshifts = {int(g): [float(x) for x in v][-SHIFTS_KEEP:] for g, v in (data.get('upshifts') or {}).items()}
        car.limiter_time = float(data.get('limiter_time') or 0.0)
        car.top_seen = float(data.get('top_seen') or 0.0)
        car.retuned = {int(g): float(v) for g, v in (data.get('retuned') or {}).items()}
        car.boost_hold = float(data.get('boost_hold') or BOOST_HOLD)
        drag = data.get('drag')
        car.drag = (float(drag[0]), float(drag[1])) if drag else (DRAG_C0, DRAG_C2)
        car.game = data.get('game')
        return car

    def set_limiter(self, rpm, source):
        """A ceiling from `source`: a better source replaces the figure even
        when it is lower (a launch on the limiter beats a game's max, which
        WRC Generations over-reports); the same source only raises it.
        True when it changed."""
        if not rpm:
            return False
        rank, known = LIMITER_SOURCES.get(source, 0), LIMITER_SOURCES.get(self.limiter_source, -1)
        if rank > known or (rank == known and rpm > self.limiter * 1.001) or not self.limiter:
            self.limiter, self.limiter_source = rpm, source
            return True
        return False

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

    def _estimate(self, samples):
        # With the slope taken out, what is left low is lift and slides: a
        # high quantile is the engine. Without, a downhill would inflate it.
        return _quantile(samples, 0.75) if self.slope_free else _median(samples)

    def curve(self, gear=None, resample=None):
        """{band: power estimate} for the bands known well enough, pooled
        over all gears or in one gear. `resample(samples)` draws a
        bootstrap sample instead of the samples themselves."""
        if gear is None:
            bands = self.power.items()
        else:
            bands = ((b, v) for (g, b), v in self.power_g.items() if g == gear)
        out = {}
        for band, samples in bands:
            if len(samples) >= POWER_MIN:
                out[band] = self._estimate(resample(samples) if resample else samples)
        return out

    def power_at(self, rpm, gear=None):
        """Power at an rpm (in `gear`, or pooled), interpolated between the
        bands either side; None where too little is known."""
        return _interpolate(self.curve(gear), rpm)

    def best_shift(self, gear, resample=None):
        """(rpm, coverage 0..1) for changing up from `gear`, or None when
        there isn't enough known yet. rpm is the limiter when staying in
        gear always pulls harder. Where both gears' own curves are known
        they are compared (the same road speed on both sides, so drag and
        slope cancel); elsewhere the pooled curve."""
        this, following = self.ratio(gear), self.ratio(gear + 1)
        ceiling = self.ceiling()
        if not this or not following or not ceiling:
            return None
        pooled = self.curve(resample=resample)
        own, next_own = self.curve(gear, resample), self.curve(gear + 1, resample)
        step = following / this
        known = checked = ahead = 0
        rpm = ceiling * 0.5
        crossing = None
        while rpm <= ceiling:
            checked += 1
            stay, change = _interpolate(own, rpm), _interpolate(next_own, rpm * step)
            if stay is None or change is None:
                stay, change = _interpolate(pooled, rpm), _interpolate(pooled, rpm * step)
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
        top = _interpolate(own, ceiling * 0.97)
        if top is None:
            top = _interpolate(pooled, ceiling * 0.97)
        if top is not None and coverage >= 0.5:
            return (ceiling, coverage)
        return None

    def best_bands(self, resamples=BOOTSTRAP_RESAMPLES, seed=0):
        """{gear: (low, high)}: the 10th and 90th percentiles of the best
        change up over bootstrap resamples of the power samples, for the
        gears whose best is known. Slow (tens of ms): the drive-log thread
        works it out, at most every BANDS_EVERY seconds per car."""
        rng = random.Random(seed)

        def resample(samples):
            return [samples[rng.randrange(len(samples))] for _ in samples]
        gears = self.gears()
        bands = {}
        for gear in gears:
            if gear + 1 not in gears or self.best_shift(gear) is None:
                continue
            found = [b[0] for b in (self.best_shift(gear, resample) for _ in range(resamples)) if b is not None]
            if len(found) >= resamples // 2:
                bands[gear] = (_quantile(found, 0.1), _quantile(found, 0.9))
        return bands

    def average_upshift(self, gear):
        shifts = self.upshifts.get(gear)
        return (statistics.mean(shifts), len(shifts)) if shifts else None

    def snapshot(self, bands=None):
        """A plain dict for the GUI; `bands` from best_bands() give each
        best change up its range (best_low, best_high)."""
        gears = self.gears()
        bands = bands or {}
        rows = []
        for gear in gears:
            best = self.best_shift(gear) if gear + 1 in gears else None
            average = self.average_upshift(gear)
            band = bands.get(gear) if best else None
            rows.append({
                'gear': gear,
                'ratio': self.ratio(gear),
                'ratio_samples': len(self.ratios.get(gear, [])),
                'best': best[0] if best else None,
                'best_low': band[0] if band else None,
                'best_high': band[1] if band else None,
                'coverage': best[1] if best else 0.0,
                'average_shift': average[0] if average else None,
                'shifts': average[1] if average else 0,
                'last': gear + 1 not in gears,
            })
        known = sum(1 for v in self.power.values() if len(v) >= POWER_MIN)
        return {'key': self.key, 'name': self.name, 'limiter': self.ceiling(), 'gears': rows,
                'limiter_source': self.limiter_source, 'power_bands': known, 'limiter_time': self.limiter_time,
                'power_source': self.power_source, 'retuned': dict(self.retuned)}

    def advice(self, session_limiter_time=0.0):
        """Coaching from what has been learnt: a list of sentences. At most
        TIPS_SHOWN tips (the largest rpm errors first), one line of praise
        and one line each about re-tuned gears and what is still being
        learnt, so the same list is not repeated gear by gear."""
        tips = []                        # (weight, sentence): the biggest first
        spot_on = []
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
                tips.append((best_rpm - shift_rpm, '{}: you change up around {:.0f} rpm, {:.0f} early; hold it to '
                             'about {:.0f}.{}'.format(change, shift_rpm, best_rpm - shift_rpm, best_rpm, cost)))
            elif shift_rpm > best_rpm + 200:
                if best_rpm >= ceiling * 0.99:
                    tips.append((shift_rpm - best_rpm, '{}: you change up around {:.0f} rpm, on the limiter; this car '
                                 'pulls to the limiter in {}, so change as the lights flash.'.format(
                                     change, shift_rpm, gear)))
                else:
                    stay, after = self.power_at(shift_rpm), self.power_at(shift_rpm * step)
                    cost = ''
                    if stay and after and stay < after:
                        cost = ' By then gear {} would give {:.0f} % more drive.'.format(
                            gear + 1, (after / stay - 1) * 100)
                    tips.append((shift_rpm - best_rpm, '{}: you change up around {:.0f} rpm, {:.0f} late; change at '
                                 'about {:.0f}.{}'.format(change, shift_rpm, shift_rpm - best_rpm, best_rpm, cost)))
            else:
                spot_on.append((change, count))
        if session_limiter_time > 3:
            # Seconds on the limiter weigh like a few hundred rpm of error
            tips.append((session_limiter_time * 100, '{:.0f} s on the limiter at full throttle this session: the '
                         'engine makes nothing there. Change up when the lights flash.'.format(session_limiter_time)))
        lines = [text for _, text in sorted(tips, key=lambda tip: -tip[0])[:TIPS_SHOWN]]
        if spot_on:
            names = [change for change, _ in spot_on]
            listed = names[0] if len(names) == 1 else '{} and {}'.format(', '.join(names[:-1]), names[-1])
            lines.append('Spot on: {} within 200 rpm of the best ({} changes).'.format(
                listed, sum(count for _, count in spot_on)))
        if self.retuned:
            retuned = sorted(self.retuned)
            lines.append('{} re-tuned; {} shift points are being learnt again.'.format(
                'Gear {} was'.format(retuned[0]) if len(retuned) == 1 else
                'Gears {} were'.format(', '.join(str(g) for g in retuned)),
                'its' if len(retuned) == 1 else 'their'))
        needed = int(ceiling * 0.5 / POWER_BIN) if ceiling else 0
        bands = sum(1 for v in self.power.values() if len(v) >= POWER_MIN)
        if needed and bands < needed * 0.8:
            lines.append('Still learning the engine ({} of about {} rev bands known): full-throttle pulls from '
                         'low revs, out of slow corners, fill it in fastest.'.format(bands, needed))
        return lines


SHIFT_PRESS_WINDOW = 0.6         # a shifter/wheel button this long before a change made it
PRESS_METHODS = {'gear': 'h-pattern', 'sequential': 'sequential', 'paddle': 'paddles'}
METHODS = ('h-pattern', 'sequential', 'paddles')


def shift_method(press, left_at, engaged_at, via_neutral):
    """How a change was made. The physical control pressed decides: a gear
    of the H-pattern shifter, the sequential plate or a paddle, pressed
    between SHIFT_PRESS_WINDOW before the old gear was left and the new
    one engaging. Neutral between the gears only says H-pattern when no
    press was seen, because whether a game shows it depends on its
    gearbox model, not on the shifter. None when nothing tells."""
    if press is not None and left_at - SHIFT_PRESS_WINDOW <= press[0] <= engaged_at + 0.05:
        method = PRESS_METHODS.get(press[1])
        if method is not None:
            return method
    return 'h-pattern' if via_neutral else None


class ShiftLearner:
    """Feeds telemetry into the current car's model. What it learns goes to
    the telemetry database (telemetry_store) through a DriveLog: the cars
    (per Oversteer profile), each driving session, and every change of
    gear, which is what long-term coaching reads.

    Thread-safe: the listener feeds, the GUI reads. `lock` guards only the
    in-memory models and is never held across I/O; the listener never
    touches the database but to read a car's model when it first sees
    it. With `threaded`, the writes run on the drive-log thread (the app);
    without, at once in the caller's thread (tests, replays)."""

    def __init__(self, database=None, profile='_no_profile', threaded=False):
        self.lock = threading.RLock()       # re-entrant: without a thread, events run under it
        self.profile = profile
        self.threaded = threaded
        self.car = None
        self.enabled = True
        self.log = None                     # DriveLog, once a database is open
        self.clock = None                   # now -> epoch seconds; None: time.time() (replays set it)
        self._models = {}                   # key -> CarModel seen in this profile since it was loaded
        self._readers = threading.local()
        self._reset_motion()
        self._dirty = False
        self._saved_at = 0.0
        self.session = None                 # the drive going on: a number, its row id is the drive log's
        self._sessions = 0
        self._session_rows = {}             # session number -> (sessions.id, cars.id); drive-log thread only
        self._last_feed = None              # monotonic time of the last packet fed
        self._pending = collections.deque()  # changes of gear waiting DOUBLE_TAP_REVERT before they are written
        self.session_limiter_time = 0.0
        self.history_changed = 0            # counts up when history readers should query again
        self._loaded = None                 # load_snapshot(): ((profile, key, updated), snapshot)
        self._shift_cache = {}
        self._shift_cache_at = 0.0
        self._bands = {}                    # key -> (monotonic time, best_bands())
        if database is not None:
            self.open(database)

    @property
    def db(self):
        """The writer's connection, or None before open()."""
        return self.log.store.db if self.log is not None else None

    def open(self, path):
        from .drive_log import DriveLog
        log = DriveLog(path, threaded=self.threaded)
        if self.threaded:
            log.ticks.append(self.publish)
        with self.lock:
            self.log = log

    def close(self):
        self.save()
        if self.log is not None:
            self.log.close()

    def _reader(self):
        """This thread's read-only connection."""
        if self.log is None:
            return None
        reader = getattr(self._readers, 'reader', None)
        if reader is None:
            from .telemetry_store import open_reader
            reader = self._readers.reader = open_reader(self.log.path)
        return reader

    def wall(self, now):
        return self.clock(now) if self.clock is not None else time.time()

    def _reset_motion(self):
        self._speeds = collections.deque()       # (t, speed, world height or None, rpm) for the acceleration
        self._gear = None
        self._gear_since = 0.0
        self._last = None                        # previous (t, rpm, gear, throttle)
        self._left = None                        # (gear, rpm, throttle, t, via neutral) when a forward gear was left
        self._off_ratio = {}                     # gear -> part-throttle ratios off the known one
        self._recent = collections.deque()       # (t, rpm, throttle) over SHIFT_WINDOW in a forward gear
        self._slip = None                        # driven-wheel slip of the last sample in a forward gear
        self._flat_since = None                  # when the throttle went to the floor

    # -- storage (the listener side posts, the drive log writes) --

    def set_profile(self, profile):
        """Switch to another Oversteer profile's cars."""
        with self.lock:
            if profile == self.profile:
                return
            self._end_session_locked()
            self._save_locked(force=True)
            self.profile = profile
            self._models = {}
            key, name, game = (self.car.key, self.car.name, self.car.game) if self.car else (None, None, None)
            self.car = None
            if key is not None:
                self._load_locked(key, name, game)

    def _load_locked(self, key, name, game=None):
        car = self._models.get(key)
        reader = self._reader() if car is None else None
        if reader is not None:
            try:
                row = reader.car(self.profile, key)
            except Exception as e:
                logging.warning("shift learner: can't read %s: %s", key, e)
                row = None
            if row is not None and row['model'].get('key') is not None:
                try:
                    car = CarModel.from_dict(row['model'])
                    car.key = key                            # adopted from a key of before
                    car.name = row['name'] or car.name
                except (ValueError, KeyError, TypeError, AttributeError) as e:
                    logging.warning("shift learner: can't read %s: %s", key, e)
        if car is None:
            car = CarModel(key, name)
        car.game = car.game or game
        self._models[key] = car
        self.car = car
        self._reset_motion()
        self.session_limiter_time = 0.0

    def _save_locked(self, force=False):
        """Have the drive log write the current car's model (at most every
        SAVE_EVERY seconds while learning, unless `force`)."""
        car = self.car
        if car is None or self.log is None or not (self._dirty or force):
            return
        self._dirty = False
        self._saved_at = self._last_feed or 0.0
        self.log.post(self._write_model, self.profile, car, self.wall(self._last_feed))

    def _write_model(self, profile, car, at):
        """Drive-log thread: the model as it is now (copied under the lock)."""
        with self.lock:
            data = car.to_dict()
        # A car only glimpsed (no gear learnt) gets no row of its own
        self.log.store.save_model(profile, car.key, car.game or 'unknown', car.name, data, car.car_class,
                                  car.drivetrain, at, create=bool(data['ratios']))

    def _start_session_locked(self, now, sample):
        if self.car is None:
            return
        self._sessions += 1
        self.session = self._sessions
        self.session_limiter_time = 0.0
        if self.log is not None:
            self.log.post(self._write_session_start, self.session, self.profile, self.car, self.wall(now),
                          sample.track, sample.stage)

    def _write_session_start(self, number, profile, car, started, track, stage):
        store = self.log.store
        with self.lock:
            data = car.to_dict()
        car_id = store.car_id(profile, car.key, car.game or 'unknown', car.name, data)
        self._session_rows[number] = (store.start_session(profile, car_id, car.game, started, track, stage), car_id)

    def _end_session_locked(self):
        if self.session is None:
            return
        if self.car is not None:
            self._flush_shifts_locked(self.car)
        number, self.session = self.session, None
        if self.log is None:
            self.history_changed += 1
            return
        self._save_locked(force=True)
        self.log.post(self._write_session_end, number, self.wall(self._last_feed), self.session_limiter_time)

    def _write_session_end(self, number, ended, limiter_time):
        row = self._session_rows.pop(number, None)
        if row is not None:
            store = self.log.store
            session = row[0]
            store.update_session(session, ended=ended, limiter_time=limiter_time,
                                 shifter=store.session_shifter(session))
            if store.drop_session_if_empty(session):
                store.drop_car_if_empty(row[1])
            store.commit()
        self.history_changed += 1
        return True

    def save(self):
        """End the session and write everything (quitting, a profile
        switch): waits for the drive log."""
        with self.lock:
            self._end_session_locked()
            self._save_locked()
        if self.log is not None:
            self.log.sync()

    def known_cars(self):
        """[(key, name)] learnt in this profile."""
        reader = self._reader()
        return reader.cars(self.profile) if reader is not None else []

    def rename(self, key, name):
        with self.lock:
            car = self._models.get(key)
            if car is not None:
                car.name = name
            profile = self.profile
        if self.log is not None:
            self.log.call(self.log.store.rename_car, profile, key, name)

    def forget(self, key):
        """Start the car's learning over. Its sessions, runs and labels stay."""
        with self.lock:
            car = self._models.get(key)
            if car is not None:
                fresh = CarModel(key, car.name)
                fresh.game, fresh.car_class, fresh.drivetrain = car.game, car.car_class, car.drivetrain
                self._models[key] = fresh
                if self.car is car:
                    self.car = fresh
                    self._reset_motion()
                    self._dirty = False
            profile = self.profile
            self._shift_cache = {}
        if self.log is not None:
            self.log.call(self.log.store.forget_model, profile, key)

    def history(self, key, limit=10):
        """The car's recent sessions, newest first: dicts with started,
        track, flat-out changes up, mean error to the best (rpm, signed),
        limiter time, the shifting methods used and the session's shifter."""
        reader = self._reader()
        return reader.history(self.profile, key, limit) if reader is not None else []

    def method_shifts(self, key):
        """{gear: {method: (mean rpm, count)}} of the car's recent
        flat-out changes up, per way of changing (the most recent
        SHIFTS_KEEP of each). A history query: run it on events (car
        change, history_changed, the tab shown), not on a timer."""
        reader = self._reader()
        return reader.method_shifts(self.profile, key, SHIFTS_KEEP) if reader is not None else {}

    def load_snapshot(self, key):
        """A car's snapshot without switching to it. A saved car's is kept
        until its `updated` time changes: the tab asks every second, and
        parsing the model and working out its advice each time is not
        free."""
        with self.lock:
            live = self.car is not None and self.car.key == key
            car = self._models.get(key)
            profile = self.profile
            if car is not None and not live:
                # Seen since the profile was loaded, not being driven: only a
                # rename or a forget (a new object) changes it now
                stamp = (profile, key, id(car), car.name)
                if self._loaded is not None and self._loaded[0] == stamp:
                    return self._loaded[1]
                copy = car.copy()
        if live:
            return self.snapshot()
        if car is not None:
            data = self._snapshot_of(copy, bands=self._bands_of(copy))
            self._loaded = (stamp, data)
            return data
        reader = self._reader()
        if reader is None:
            return None
        updated = reader.updated(profile, key)
        if updated is None:
            return None
        stamp = (profile, key, updated)
        if self._loaded is not None and self._loaded[0] == stamp:
            return self._loaded[1]
        row = reader.car(profile, key)
        try:
            car = CarModel.from_dict(dict(row['model'], key=key))
        except (ValueError, KeyError, TypeError, AttributeError):
            return None
        car.name = row['name'] or car.name
        data = self._snapshot_of(car, bands=self._bands_of(car))
        self._loaded = (stamp, data)
        return data

    # -- live --

    def snapshot(self):
        """The current car's snapshot: the one the drive-log thread last
        published, or worked out now from a copy (the lock is held only to
        copy the model)."""
        published = self.published
        car = self.car
        if published is not None and car is not None and published['key'] == car.key:
            return published
        with self.lock:
            if self.car is None:
                return None
            copy, limiter_time = self.car.copy(), self.session_limiter_time
        return self._snapshot_of(copy, limiter_time, self._bands_of(copy))

    published = None                    # the drive-log thread's last snapshot of the current car

    def publish(self):
        """Drive-log thread, once a second: work the snapshot out here so
        readers never do it under the lock."""
        with self.lock:
            if self.car is None:
                self.published = None
                return
            copy, limiter_time = self.car.copy(), self.session_limiter_time
        self.published = self._snapshot_of(copy, limiter_time, self._bands_of(copy))

    def _bands_of(self, car):
        """The car's best_bands(), worked out again at most every
        BANDS_EVERY seconds (they take tens of milliseconds)."""
        cached = self._bands.get(car.key)
        now = time.monotonic()
        if cached is None or now - cached[0] > BANDS_EVERY:
            cached = (now, car.best_bands())
            bands = dict(self._bands)
            bands[car.key] = cached
            self._bands = bands                  # a new dict: the listener reads it without the lock
        return cached[1]

    @staticmethod
    def _snapshot_of(car, session_limiter_time=0.0, bands=None):
        data = car.snapshot(bands)
        data['session_limiter_time'] = session_limiter_time
        data['advice'] = car.advice(session_limiter_time)
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
        """Telemetry stopped for a moment (menus, loading, a pause, the end
        of a stage): keep what was learnt. The session goes on until
        SESSION_GAP passes without telemetry (tick()), so a pause does not
        split a stage."""
        with self.lock:
            if self.car is not None:
                self._flush_shifts_locked(self.car)
            self._save_locked()
            self._reset_motion()
        self.history_changed += 1

    def tick(self, now):
        """No telemetry is arriving (the listener's idle loop): end the
        session once SESSION_GAP has passed since the last packet."""
        with self.lock:
            if self.session is not None and self._last_feed is not None and now - self._last_feed > SESSION_GAP:
                self._end_session_locked()

    def feed(self, now, sample, limiter, throttle, clutch, press=None, limiter_source='game'):
        """One telemetry packet. `limiter` is the best known ceiling and
        `limiter_source` where it comes from ('launch', 'game', 'seen');
        `throttle` and `clutch` are pressed fractions (None = unknown);
        `press` is (monotonic time, 'gear', 'sequential' or 'paddle') of
        the last button that could have changed gear."""
        if not self.enabled or sample.car is None:
            return
        with self.lock:
            if self.session is not None and self._last_feed is not None and now - self._last_feed > SESSION_GAP:
                self._end_session_locked()
            if self.car is None or self.car.key != sample.car:
                self._end_session_locked()
                self._save_locked()
                self._load_locked(sample.car, sample.car_name, sample.game)
                self._shift_cache = {}
            self._last_feed = now
            car = self.car
            if sample.car_class is not None:
                car.car_class = sample.car_class
            if sample.drivetrain is not None:
                car.drivetrain = sample.drivetrain
            if self.session is None:
                self._start_session_locked(now, sample)
            self._press = press
            if car.set_limiter(limiter, limiter_source):
                self._dirty = True
            self._feed_locked(car, now, sample, throttle, clutch)
            if self._dirty and now - self._saved_at > SAVE_EVERY:
                self._save_locked()

    def _feed_locked(self, car, now, sample, throttle, clutch):
        gear, speed, rpm = sample.gear, sample.speed, sample.rpm
        previous = self._last
        self._last = (now, rpm, gear, throttle)
        flat_out = throttle is not None and throttle >= FULL_THROTTLE
        if not flat_out:
            self._flat_since = None
        elif self._flat_since is None:
            self._flat_since = now
        if gear is None or speed is None:
            return
        recent = self._recent
        if gear != self._gear:
            # Where a change happened: the peak rpm and the most throttle
            # over the last moments in the old gear, also through neutral (an
            # H-pattern box shows it on the way). An H-pattern driver lifts
            # before the gear leaves, so the last sample alone reads low and
            # often not flat out.
            if self._gear is not None and self._gear >= 1 and recent:
                pressed = [x for _, _, x in recent if x is not None]
                self._left = (self._gear, max(r for _, r, _ in recent), max(pressed) if pressed else None, now,
                              gear == 0, self._slip)
            elif gear == 0 and self._left is not None:
                self._left = self._left[:4] + (True,) + self._left[5:]
            left = self._left
            if left is not None and gear != 0:
                if gear >= 1 and gear != left[0] and now - left[3] <= SHIFT_SPAN and speed > 3.0:
                    self._shift_locked(car, left, gear, rpm, now)
                self._left = None
            self._gear = gear
            self._gear_since = now
            self._speeds.clear()
            recent.clear()
        if gear >= 1:
            recent.append((now, rpm, throttle))
            while now - recent[0][0] > SHIFT_WINDOW:
                recent.popleft()
            slip = drive_slip(sample, car.drivetrain)
            self._slip = slip[0] if slip is not None and slip[1] == 'raw' else None
        pending = self._pending
        if pending:
            last = pending[-1]
            if gear == last['gear_to'] and now - last['_t'] <= SHIFT_WINDOW and rpm > last['engage_rpm']:
                last['engage_rpm'] = rpm              # the revs the new gear brought, once the clutch bites
            if now - pending[0]['_t'] > DOUBLE_TAP_REVERT:
                self._flush_shifts_locked(car, now)
        self._speeds.append((now, speed, sample.pos[1] if sample.pos is not None else None, rpm))
        while self._speeds and now - self._speeds[0][0] > ACCEL_WINDOW:
            self._speeds.popleft()

        clutch_out = clutch is None or clutch <= CLUTCH_OUT
        settled = now - self._gear_since >= SETTLED
        if gear < 1 or speed < MIN_SPEED or not clutch_out or not settled:
            return
        if flat_out and rpm > car.top_seen:
            car.top_seen = rpm
        if flat_out and car.limiter and rpm >= car.limiter * LIMITER_BAND and previous is not None:
            dt = min(0.2, max(0.0, now - previous[0]))
            car.limiter_time += dt
            self.session_limiter_time += dt

        ratio = rpm / speed
        known = car.ratio(gear)
        # A ratio is learnt only where the tyres barely slip: part throttle,
        # off the brake, at a steady speed. Full throttle on gravel spins
        # the wheels a steady few percent, which would look like a ratio of
        # its own and then reject every clean sample as off-ratio for good.
        brake = sample.brake
        low_slip = (throttle is not None and throttle < LOW_SLIP_THROTTLE
                    and (brake <= BRAKE_OFF if brake is not None else throttle >= COASTING))
        if low_slip:
            accel = self._acceleration()
            low_slip = accel is not None and abs(accel) <= STEADY_ACCEL
        on_ratio = known is not None and abs(ratio - known) <= known * RATIO_TOLERANCE
        if low_slip and (known is None or on_ratio):
            samples = car.ratios.setdefault(gear, [])
            samples.append(ratio)
            del samples[:-RATIO_KEEP]
            self._dirty = True
            self._off_ratio.pop(gear, None)
        elif low_slip:
            # Off the gear's ratio without spin to blame: the gear was
            # re-tuned, if it stays that way
            self._check_retune(car, gear, ratio)
            return
        if not on_ratio:
            # Wheelspin or a jump, or a gear not learnt yet: no power from it
            return
        if not flat_out or now - self._flat_since < car.boost_hold:
            # Power counts once the throttle has been floored a moment: a
            # turbo builds boost after the pedal goes down
            return
        if car.limiter and rpm > car.limiter * 1.01:
            return
        if brake is not None and brake >= BRAKE_POWER:
            return                                   # left-foot braking: the engine is fighting it
        slip = drive_slip(sample, car.drivetrain)
        if slip is not None and slip[0] > SLIP_POWER[slip[1]]:
            return                                   # spinning: the drive is not reaching the road
        if sample.power is not None:
            if car.power_source != 'game':
                car.power, car.power_g, car.power_source = {}, {}, 'game'   # the real figure beats the estimate
            power = sample.power
        else:
            if car.power_source == 'game':
                return
            car.power_source = 'accel'
            accel, slope_free = self._drive_acceleration(sample)
            if accel is None or accel <= 0:
                return
            car.slope_free = slope_free
            # The acceleration is the window's, so it belongs to the window's
            # middle: at 3000 rpm a second in 2nd gear, the last sample's rpm
            # would be 500 too high
            points = self._speeds
            rpm = sum(p[3] for p in points) / len(points)
            speed = sum(p[1] for p in points) / len(points)
            c0, c2 = car.drag
            power = (accel + c0 + c2 * speed * speed) * speed
        if power <= 0:
            return
        band = int(rpm // POWER_BIN)
        for values in (car.power.setdefault(band, []), car.power_g.setdefault((gear, band), [])):
            values.append(power)
            del values[:-POWER_KEEP]

    def _shift_locked(self, car, left, to, engage_rpm, now):
        """A change from a forward gear to another: kept pending for
        DOUBLE_TAP_REVERT seconds, so a quick correction can flag it and
        the revs the new gear brought are known, then written."""
        start, peak, throttle, left_at, via_neutral, slip = left
        up = to > start
        flat_out = up and (throttle is None or throttle >= 0.8)
        if up and to == start + 1 and flat_out:
            shifts = car.upshifts.setdefault(start, [])
            shifts.append(peak)
            del shifts[:-SHIFTS_KEEP]
            self._dirty = True
        best = car.best_shift(start) if up and to == start + 1 else None
        band = self._bands.get(car.key, (0, {}))[1].get(start) if best else None
        shift = {'_t': now, 'at': self.wall(now), 'gear': start, 'gear_to': to, 'direction': 'up' if up else 'down',
                 'rpm': peak, 'best': best[0] if best else None, 'best_low': band[0] if band else None,
                 'best_high': band[1] if band else None, 'throttle': throttle,
                 'method': shift_method(getattr(self, '_press', None), left_at, now, via_neutral),
                 'neutral_time': now - left_at if via_neutral else 0.0, 'engage_rpm': engage_rpm,
                 'flat_out': int(flat_out), 'slip': slip, 'flags': []}
        pending = self._pending
        if len(pending) >= 1:
            # Two taps the same way in a blink, the second taken back at once:
            # the paddle or the lever was hit twice
            before = pending[-1]
            earlier = pending[-2] if len(pending) >= 2 else None
            if (earlier is not None and earlier['direction'] == before['direction']
                    and before['_t'] - earlier['_t'] < DOUBLE_TAP and now - before['_t'] <= DOUBLE_TAP_REVERT
                    and to == before['gear'] and start == before['gear_to']):
                before['flags'].append('double-tap')
        pending.append(shift)

    def _flush_shifts_locked(self, car, now=None):
        """Write the pending changes older than DOUBLE_TAP_REVERT (all of
        them when `now` is None), with their flags."""
        pending = self._pending
        while pending and (now is None or now - pending[0]['_t'] > DOUBLE_TAP_REVERT):
            shift = pending.popleft()
            del shift['_t']
            flags = shift['flags']
            limiter = car.limiter
            if shift['direction'] == 'up':
                if shift['neutral_time'] > MISSED_NEUTRAL and shift['throttle'] is not None and shift['throttle'] >= 0.8:
                    flags.append('missed')           # stuck in neutral, foot down: a missed gate
                if shift['gear_to'] >= shift['gear'] + 2:
                    flags.append('skip')
            else:
                if (shift['gear_to'] == shift['gear'] - 1 and shift['throttle'] is not None
                        and shift['throttle'] >= 0.8 and limiter and shift['rpm'] >= 0.9 * limiter):
                    flags.append('skip')             # flat out near the limiter and down a gear: meant to go up
                if limiter and shift['engage_rpm'] > OVER_REV * limiter:
                    flags.append('over-rev')
            shift['flags'] = ','.join(flags) or None
            if self.log is not None and self.session is not None:
                self.log.post(self._write_shift, self.session, shift)

    def _write_shift(self, number, shift):
        row = self._session_rows.get(number)
        if row is not None:
            self.log.store.add_shift(row[0], None, shift)

    def _check_retune(self, car, gear, ratio):
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
            car.top_seen = 0.0                       # where the data ends, with the old gearing
            self._off_ratio.pop(gear, None)
            self._dirty = True
            logging.info("shift learner: %s gear %d re-tuned (%.1f rpm per m/s)", car.key, gear, middle)

    @staticmethod
    def _slope(points, index):
        """Least-squares slope over time of field `index` of the points."""
        n = len(points)
        mean_t = sum(p[0] for p in points) / n
        mean_v = sum(p[index] for p in points) / n
        spread = sum((p[0] - mean_t) ** 2 for p in points)
        if spread <= 0:
            return None
        return sum((p[0] - mean_t) * (p[index] - mean_v) for p in points) / spread

    def _acceleration(self):
        """The rate of change of speed over the recent window."""
        points = self._speeds
        if len(points) < ACCEL_MIN_POINTS or points[-1][0] - points[0][0] < ACCEL_WINDOW * 0.5:
            return None
        return self._slope(points, 1)

    def _drive_acceleration(self, sample):
        """(what the drive accelerates the car by, slope-free): an
        accelerometer reading as it is (gravity is in it); otherwise the
        change of speed plus what the slope takes, from the car's forward
        vector or from how fast it climbs; otherwise the change of speed
        alone (False: a hill is in it)."""
        if sample.accel_kind == 'specific' and sample.accel is not None:
            return sample.accel[0], True
        accel = self._acceleration()
        if accel is None:
            return None, False
        if sample.forward is not None:
            return accel + G * sample.forward[1], True
        points = self._speeds
        if all(p[2] is not None for p in points) and sample.speed:
            climb = self._slope(points, 2)
            if climb is not None:
                return accel + G * max(-1.0, min(1.0, climb / sample.speed)), True
        return accel, False
