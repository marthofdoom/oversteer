"""Learning each car's best upshift points from telemetry.

The best moment to change up is where the next gear, at the same road
speed, gives more drive than staying in this one. Drive at a given speed
is proportional to engine power (force = power / speed), so what we need
is the engine's power curve and each gear's ratio:

- Power: at full throttle with the clutch out, the car's acceleration
  (plus what drag and rolling resistance take, which we estimate, and
  what the slope takes: from the car's forward vector where the game
  sends it, else from the climb of its position over the distance
  travelled) times its speed is proportional to engine power, whatever
  the gear. Forza sends the power itself. Samples count once the
  throttle has been down a moment (turbo lag), off the brake, without
  wheelspin. They are kept per 100 rpm band per gear, and pooled over
  the gears only where the slope is taken out (a hill in one gear's
  bands and not in the next one's would show as a power drop); the
  median of each band (the 75th percentile once slopes are taken out)
  makes bumps and the odd slide wash out.
- Ratios: engine rpm per m/s in each gear, from samples on some
  throttle (not coasting), off the brake, clutch out, at a steady speed,
  where the tyres barely slip. A full-throttle sample whose ratio is off
  its gear's (wheelspin, a jump) is not used for power.

For each gear the best shift is then the lowest rpm from which the next
gear's power at the same speed (rpm x next ratio / this ratio) is higher
than this gear's, or the limiter if that never happens. Each gear's own
curve is used where both gears' are known (at the same road speed drag
and slope cancel out), the pooled one elsewhere when there is one;
bootstrap resamples of the power samples give the answer a range.

Where Oversteer ships the game's own data for the car (car_data: the
torque curve and gearing of every Assetto Corsa Rally car), the engine's
best change up is worked out from it instead, exactly. Either way it is
then checked per surface: a gear whose measured drive at full throttle
stays well below what the engine gives in it (1st on gravel) is
grip-limited there, and changing up is worth it as soon as the next gear
reaches the same grip limit (best_for()).

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

from . import car_data

POWER_BIN = 100                  # rpm per power band
POWER_KEEP = 40                  # samples kept per band (most recent)
POWER_MIN = 4                    # samples a band needs to count (slope taken out, or the game's power)...
POWER_MIN_SLOPED = 8             # ...and with hills left in: more, as each carries its gradient
RATIO_KEEP = 200
RATIO_MIN = 20
RATIO_TOLERANCE = 0.04           # a sample off its gear's ratio by more is spin or a jump
RETUNE_SAMPLES = 90              # part-throttle samples agreeing on a new ratio: the gear was re-tuned
RETUNE_SPREAD = 0.015
RETUNE_WINDOW = 60.0             # seconds on the move into a session in which a new ratio is believed...
RETUNE_DISTANCE = 2000.0         # ...or metres: setups change in menus, which end sessions
RETUNE_SPEED_BIN = 5.0           # m/s; a new ratio must be seen at two speeds at least
SHIFTS_KEEP = 40
FULL_THROTTLE = 0.95
SHIFT_THROTTLE = 0.8             # at a change up, flat out: an H-pattern driver lifts a little before the gear leaves
SHIFT_COVERAGE = 0.6             # share of the rev range known before a best change up is used (lights, coaching)
BRAKE_POWER = 0.05               # braking at least this much: no power sample
SLIP_POWER = {'raw': 0.08, 'normalised': 0.5}   # driven-wheel slip above which power is spin (calibrate per game)
SHIFT_WINDOW = 0.3               # seconds before a change in which its rpm and throttle are taken
SHIFT_SPAN = 1.5                 # seconds from leaving a gear to engaging the next that make one change
MISSED_NEUTRAL = 0.5             # seconds in neutral on a change up under throttle: a missed gate
OVER_REV = 0.95                  # a change down that takes the revs past this share of the limiter
DOUBLE_TAP = 0.25                # two changes the same way this close (calibrate)...
DOUBLE_TAP_REVERT = 1.0          # ...and the second taken back within this: a double tap
LOW_SLIP_THROTTLE = 0.5          # below this the tyres barely slip: ratios are learnt here only...
RATIO_THROTTLE = 0.2             # ...and from here up: coasting on the engine's braking slips too
BRAKE_OFF = 0.02
COASTING = 0.05                  # with no brake reading, a little throttle says the brake is off
STEADY_ACCEL = 2.0               # m/s^2: steady enough for a ratio sample
CLUTCH_OUT = 0.1
MIN_SPEED = 5.0                  # m/s
SETTLED = 0.25                   # seconds in a gear before its samples count
ACCEL_WINDOW = 0.3               # seconds of speed history for the acceleration
ACCEL_MIN_POINTS = 5
GRADE_PATH = 2.0                 # m of travel in the window before the climb over it is a slope
SHIFT_STEP = 25                  # rpm resolution of the search
SHIFT_CONFIRM = 3                # steps the next gear must stay ahead (noise)
LIMITER_BAND = 0.985             # within this of the limiter counts as on it
ADVICE_MIN = 5                   # changes up in a gear before its shares are judged (as coach.SHIFT_MIN)
ADVICE_CUT = 0.25                # share on the cut: a tip (as coach.SHIFT_CUT, ...EARLY, ...TWO, ...ON)
ADVICE_EARLY = 0.6
ADVICE_TWO = 0.3
ADVICE_ON = 0.6
ADVICE_ON_CUT = 0.1
LAUNCH_WITHIN = 0.02             # a launch figure this close to the game's limiter is the launch overshooting the cut
DRAG_C0 = 0.15                   # m/s^2: rolling resistance, typical car
DRAG_C2 = 3.5e-4                 # 1/m: aerodynamic drag / mass, typical car
BOOST_HOLD = 0.8                 # s of full throttle before power counts: turbo lag (calibrate; learnt per car later)
BOOTSTRAP_RESAMPLES = 20         # for the confidence band of each best change up
BANDS_EVERY = 10.0               # seconds between working the bands out again, per car
SAVE_EVERY = 20.0                # seconds between saves while learning
SESSION_GAP = 120.0              # seconds without telemetry that end a session (a pause does not)
TIPS_SHOWN = 3                   # coaching tips at a time, the biggest first
DRIVE_KEEP = 300                 # full-throttle drive samples kept per surface and gear (30 s of pulling)...
DRIVE_EVERY = 0.1                # ...one every this many seconds
DRIVE_MIN = 20                   # samples before a gear's grip on a surface is judged...
DRIVE_PULLS = 3                  # ...from at least this many pulls
GRIP_SHARE = 0.8                 # measured drive below this share of the engine's: the gear is grip-limited
GRIP_TOLERANCE = 0.02            # the next gear within this of the grip limit counts as reaching it
GEARING_ASIDE = 0.04             # learnt ratios off the game's gear set by more (not spin): its data is set aside
SURFACES = ('tarmac', 'gravel', 'snow', 'ice')     # the surfaces the best changes up are kept for
LOOSE = ('gravel', 'snow', 'ice')


G = 9.80665
DRIVEN = {'fwd': (0, 1), 'rwd': (2, 3), 'awd': (0, 1, 2, 3)}     # wheel indices (FL, FR, RL, RR)


def drive_slip(sample, drivetrain=None, radii=None):
    """(slip, kind) of the driven wheels, or None where the game sends no
    wheel speeds or slip. From wheel speeds: how much faster the driven
    wheels turn than the car moves (0.1 = 10 %), 'raw'; from Forza's slip
    ratio, its own figure where 1 is the grip limit, 'normalised'. The
    driven wheels are the drivetrain's, or the fastest wheel when it is
    unknown (the driven ones are the ones that spin). Where the game sends
    wheel rotation but no tyre radius (ACR's is 0), `radii` is the car's
    learnt one."""
    wheels = DRIVEN.get(drivetrain)
    wheel_speed = sample.wheel_speed
    if wheel_speed is None and radii is not None and sample.wheel_rot is not None:
        wheel_speed = [w * r for w, r in zip(sample.wheel_rot, radii)]
    if wheel_speed is not None and sample.speed and sample.speed > MIN_SPEED:
        speeds = [abs(v) for v in wheel_speed]
        driven = sum(speeds[i] for i in wheels) / len(wheels) if wheels else max(speeds)
        return driven / sample.speed - 1.0, 'raw'
    if sample.slip_ratio is not None:
        values = [abs(v) for v in sample.slip_ratio]
        return max(values[i] for i in wheels) if wheels else max(values), sample.slip_kind or 'raw'
    return None


def shared_car(key):
    """Cars the game does not tell apart share one key (BeamNG over
    OutGauge, AC without the bridge's names): what one of them teaches
    would drive the lights in the next, so nothing about the car is
    learnt under such a key."""
    return key is not None and key.endswith('/unknown')


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


def _stamp(values):
    """A cheap fingerprint of {key: [samples]}: a sample added (even to a
    full list, which drops its oldest) changes the last one."""
    return tuple((k, len(v), v[-1] if v else None) for k, v in values.items())


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

    VERSION = 3

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
        self.drivetrain = None           # 'fwd', 'rwd', 'awd': the game's word, or learnt (DrivenWheels)
        self.drivetrain_votes = {}       # DrivenWheels: which axle stayed locked to the engine in wheelspin
        self.wheel_check = []            # wheel speed / car speed at low slip: are the wheel speeds right?
        self.wheels_ok = None
        self.tyre_radius = {}            # wheel index -> [m], Forza: speed / wheel rotation at low slip
        self.radii = None                # their medians, once each has enough
        self.drive = {}                  # (surface, gear) -> [(rpm, m/s, drive m/s^2, pull)] at full throttle
        self.pulls = 0                   # full-throttle pulls seen (numbers the drive samples' pulls)
        self.last_surface = None         # the surface last driven on: what the tab shows between drives
        self._shipped = False            # car_data entry, looked up once (False: not yet)
        self._cache = {}                 # name -> (fingerprint of what it was worked out from, value)

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
            'drivetrain': self.drivetrain,
            'drivetrain_votes': dict(self.drivetrain_votes),
            'wheel_check': list(self.wheel_check),
            'tyre_radius': {str(i): list(v) for i, v in self.tyre_radius.items()},
            'drive': {'{}:{}'.format(surface, g): [list(x) for x in v] for (surface, g), v in self.drive.items()},
            'pulls': self.pulls,
            'last_surface': self.last_surface,
        }

    def copy(self):
        car = CarModel.from_dict(self.to_dict())
        car.car_class = self.car_class
        return car

    @classmethod
    def from_dict(cls, data):
        """Version 1 (pooled power only, no limiter source) is read as it
        is and saved as version 3. Before version 3 power was pooled over
        the gears with hills left in: that pooled curve is dropped (the
        per-gear curves stay)."""
        car = cls(data['key'], data.get('name'))
        car.limiter = float(data.get('limiter') or 0.0)
        car.limiter_source = data.get('limiter_source') or ('game' if car.limiter else None)
        car.power_source = data.get('power_source')
        car.power = {int(b): [float(x) for x in v][-POWER_KEEP:] for b, v in (data.get('power') or {}).items()}
        if (data.get('version') or 1) < 3 and data.get('power_source') == 'accel' and not data.get('slope_free'):
            car.power = {}
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
        car.drivetrain = data.get('drivetrain')
        car.drivetrain_votes = {k: int(v) for k, v in (data.get('drivetrain_votes') or {}).items()}
        car.wheel_check = [float(x) for x in data.get('wheel_check') or []][-WHEEL_CHECK:]
        car.tyre_radius = {int(i): [float(x) for x in v][-WHEEL_CHECK:] for i, v in (data.get('tyre_radius') or {}).items()}
        if len(car.tyre_radius) == 4 and all(len(v) >= WHEEL_CHECK for v in car.tyre_radius.values()):
            car.radii = tuple(_median(car.tyre_radius[i]) for i in range(4))
        drive = {}
        for key, v in (data.get('drive') or {}).items():
            surface, gear = key.rsplit(':', 1)
            # Samples without their pull were kept at every packet and only
            # on the gear's ratio (no wheelspin): measured again
            kept = [tuple(float(x) for x in item) for item in v if len(item) == 4][-DRIVE_KEEP:]
            if kept:
                drive[(surface, int(gear))] = kept
        car.drive = drive
        car.pulls = int(data.get('pulls') or 0)
        car.last_surface = data.get('last_surface')
        return car

    @property
    def shipped(self):
        """The game's own data for this car (car_data), or None."""
        if self._shipped is False:
            self._shipped = car_data.entry(self.key)
        return self._shipped

    def set_limiter(self, rpm, source):
        """A ceiling from `source`: a better source replaces the figure even
        when it is lower (a launch on the limiter beats a game's max, which
        WRC Generations over-reports); the same source only raises it,
        except a launch, which measures the car as it is now and replaces
        an earlier launch's figure either way. True when it changed."""
        if not rpm:
            return False
        if source == 'launch':
            # A launch overshoots the cut: within LAUNCH_WITHIN of the game's figure, the game's stands
            game = float(self.shipped['limiter_rpm']) if self.shipped else (
                self.limiter if self.limiter_source == 'game' else None)
            if game and abs(rpm - game) <= LAUNCH_WITHIN * game:
                return False
        rank, known = LIMITER_SOURCES.get(source, 0), LIMITER_SOURCES.get(self.limiter_source, -1)
        same = rank == known and (rpm > self.limiter * 1.001
                                  or (source == 'launch' and abs(rpm - self.limiter) > 1.0))
        if rank > known or same or not self.limiter:
            self.limiter, self.limiter_source = rpm, source
            return True
        return False

    def known_limiter(self):
        """The limiter when a launch measured it or the game reported it
        (or its files say, car_data); 0 when only the highest rpm seen
        stands in for it (OutGauge sends no maximum)."""
        if self.shipped:
            game = float(self.shipped['limiter_rpm'])
            # A launch figure within LAUNCH_WITHIN of it is the launch overshooting (stored by an older version too)
            if self.limiter_source != 'launch' or abs(self.limiter - game) <= LAUNCH_WITHIN * game:
                return game
        return self.limiter if self.limiter_source in ('launch', 'game') else 0.0

    def ceiling(self):
        """The highest rpm worth changing up at: the known limiter, else
        the highest rpm seen flat out, where the data ends. The highest rpm
        seen never stands in for a known limiter: a driver who changes up
        early has only been that far, which says nothing about the engine
        above it (a game that over-reports its maximum needs a launch)."""
        return self.known_limiter() or self.top_seen or self.limiter

    # -- what it knows --

    def _memo(self, name, stamp, compute):
        """compute(), kept until `stamp` (a fingerprint of what it is worked
        out from) changes: the listener adds samples to a live model, the
        readers work on copies, and a snapshot asks the same questions many
        times over."""
        hit = self._cache.get(name)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        value = compute()
        self._cache[name] = (stamp, value)
        return value

    def _ratios(self):
        """{gear: rpm per m/s} of the gears learnt well enough."""
        return self._memo('ratios', _stamp(self.ratios), lambda: {
            g: _median(v) for g, v in self.ratios.items() if len(v) >= RATIO_MIN})

    def ratio(self, gear):
        return self._ratios().get(gear)

    def gears(self):
        return sorted(self._ratios())

    def _estimate(self, samples):
        # With the slope taken out, what is left low is lift and slides: a
        # high quantile is the engine. Without, a downhill would inflate it.
        return _quantile(samples, 0.75) if self.slope_free else _median(samples)

    def pools(self):
        """Whether power may be pooled over the gears: the game's own
        figure, or acceleration with the slope taken out. Otherwise each
        band carries the hills it was driven on, and different gears were
        driven on different roads."""
        return self.power_source == 'game' or self.slope_free

    def band_min(self):
        """Samples a band needs: more while hills are left in."""
        return POWER_MIN if self.pools() else POWER_MIN_SLOPED

    def curve(self, gear=None, resample=None):
        """{band: power estimate} for the bands known well enough, pooled
        over all gears or in one gear. `resample(samples)` draws a
        bootstrap sample instead of the samples themselves."""
        if resample is None:
            stamp = (_stamp(self.power if gear is None else self.power_g), self.power_source, self.slope_free)
            return self._memo(('curve', gear), stamp, lambda: self._curve(gear, None))
        return self._curve(gear, resample)

    def _curve(self, gear, resample):
        if gear is None:
            bands = self.power.items()
        else:
            bands = ((b, v) for (g, b), v in self.power_g.items() if g == gear)
        out = {}
        need = self.band_min()
        for band, samples in bands:
            if len(samples) >= need:
                out[band] = self._estimate(resample(samples) if resample else samples)
        return out

    def known_bands(self):
        """Rev bands whose power is known, in any gear."""
        need = self.band_min()
        bands = {b for b, v in self.power.items() if len(v) >= need}
        bands.update(b for (_, b), v in self.power_g.items() if len(v) >= need)
        return len(bands)

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
        pooled = self.curve(resample=resample) if self.pools() else {}
        own, next_own = self.curve(gear, resample), self.curve(gear + 1, resample)
        step = following / this
        known = checked = ahead = 0
        rpm = ceiling * 0.5
        crossing = None
        while rpm <= ceiling:
            checked += 1
            stay, change = _interpolate(own, rpm), _interpolate(next_own, rpm * step)
            if (stay is None or change is None) and pooled:
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
        # Never beaten: hold it to the limiter, if the top end is known. Where
        # the limiter is not, the scan ended at the highest rpm seen, and
        # nothing says the engine stops pulling there
        if not self.known_limiter():
            return None
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

    # -- the game's own data, and grip per surface --

    def gear_set(self):
        """(the shipped gear set in use, worst mismatch of the learnt
        ratios against it) or (None, None) without shipped data: the set
        whose steps between gears fit the learnt ratios best, else the
        car's default. The game's ratios are used; the learnt ones are
        the check."""
        return self._gearing()[:2]

    def _gearing(self):
        """(gear set, worst mismatch, mismatch spin does not explain), or
        Nones without shipped data."""
        if not self.shipped:
            return None, None, None
        ratios = self._ratios()

        def work_out():
            gear_set, miss = car_data.match_set(self.shipped, ratios)
            return gear_set, miss, car_data.unexplained_miss(gear_set, ratios, GEARING_ASIDE)
        return self._memo('gear_set', _stamp(self.ratios), work_out)

    def gearing_aside(self):
        """How far the learnt gearing is off every gear set the game's
        files give the car (not wheelspin) when that is more than
        GEARING_ASIDE: a setup or a car the data does not know, so the
        best changes up are learnt instead; else None."""
        miss = self._gearing()[2]
        return miss if miss is not None and miss > GEARING_ASIDE else None

    def game_data(self):
        """The shipped entry the best changes up are worked out from: None
        without one, or with its gearing set aside."""
        return self.shipped if self.shipped and self.gearing_aside() is None else None

    def step(self, gear):
        """rpm in gear + 1 per rpm in `gear` at the same road speed: the
        game's gearing where shipped, else the learnt ratios; None when
        unknown."""
        gear_set = self.gear_set()[0] if self.game_data() else None
        if gear_set is not None:
            gears = gear_set['gears']
            return gears[gear] / gears[gear - 1] if 1 <= gear < len(gears) else None
        this, following = self.ratio(gear), self.ratio(gear + 1)
        return following / this if this and following else None

    def top_gear(self):
        gear_set = self.gear_set()[0] if self.game_data() else None
        if gear_set is not None:
            return len(gear_set['gears'])
        gears = self.gears()
        return gears[-1] if gears else None

    def engine_drive(self, gear, rpm):
        """What the engine can push the car with in `gear` at `rpm`, in a
        unit of its own (only ratios of it mean anything): torque x gear
        from the game's data; else the learnt power over the road speed,
        from the curve pooled over the gears, which only exists with the
        slope taken out. None where unknown."""
        return self._drive_fn()(gear, rpm)

    def _drive_fn(self):
        """engine_drive as a function of (gear, rpm), with the gearing and
        the pooled curve looked up once: loops over many samples or revs
        call it."""
        if self.game_data():
            data, gears = self.shipped, self.gear_set()[0]['gears']

            def drive(gear, rpm):
                return car_data.torque(data, rpm) * gears[gear - 1] if 1 <= gear <= len(gears) else None
            return drive
        if not self.pools():
            return lambda gear, rpm: None
        ratios, pooled = self._ratios(), self.curve()

        def drive(gear, rpm):
            ratio = ratios.get(gear)
            if not ratio or rpm <= 0:
                return None
            power = _interpolate(pooled, rpm)
            return power * ratio / rpm if power is not None else None
        return drive

    def _scan(self, gear, cap=None, tolerance=0.0):
        """The lowest rpm from which, all the way to the limiter, gear + 1
        pulls at least as hard as `gear` at the same road speed (both held
        to the grip limit `cap`, within `tolerance`); the limiter when
        `gear` pulls harder there. None when the drive is not known over
        the whole range."""
        step, ceiling = self.step(gear), self.known_limiter()
        if not step or not ceiling:
            return None
        best = None
        rpm = ceiling
        engine_drive = self._drive_fn()
        while rpm >= ceiling * 0.4:
            stay, change = engine_drive(gear, rpm), engine_drive(gear + 1, rpm * step)
            if stay is None or change is None:
                return best                      # known down to here only (None: not even at the limiter)
            if cap is not None:
                stay, change = min(stay, cap), min(change, cap)
            if change < stay * (1.0 - tolerance):
                break
            best = rpm
            rpm -= SHIFT_STEP
        return best if best is not None else ceiling

    def game_best(self, gear):
        """The engine's best change up from `gear` from the game's own
        torque curve and gearing: exact, so None only past the top gear."""
        top = self.top_gear()
        if not self.game_data() or top is None or not 1 <= gear < top:
            return None
        return self._scan(gear)

    def grip(self, gear, surface):
        """(grip-limited, grip limit in engine_drive units, drive measured
        against the engine's, samples) of `gear` on `surface`, or None
        until enough is measured (DRIVE_MIN samples from DRIVE_PULLS
        pulls, in this gear and a higher one). The measured drive (acceleration plus
        what drag and rolling take) over the engine's at those revs is
        compared with the same in the other gears on the surface, the
        highest of the higher gears stands for "all the engine gives" (it
        cancels the car's mass, the efficiency and the tyre): a gear
        clearly below it spins or slides its drive away. The top gear has
        nothing to be compared with and is never grip-limited."""
        return self._grip_table(surface).get(gear)

    def _grip_table(self, surface):
        """{gear: grip()} on `surface`, worked out for every gear at once
        and kept until the samples, the gearing or the power change."""
        drive = tuple((k, len(v), v[-1] if v else None) for k, v in self.drive.items() if k[0] == surface)
        stamp = (drive, _stamp(self.ratios)) if self.game_data() else \
            (drive, _stamp(self.ratios), _stamp(self.power), self.power_source, self.slope_free)
        return self._memo(('grip', surface), stamp, lambda: self._work_out_grip(surface))

    def _work_out_grip(self, surface):
        engine_drive = self._drive_fn()
        medians = {}
        for (s, g), values in self.drive.items():
            if s == surface and len(values) >= DRIVE_MIN and len({v[3] for v in values}) >= DRIVE_PULLS:
                found = []
                for rpm, _speed, drive, _pull in values:
                    engine = engine_drive(g, rpm)
                    if engine:
                        found.append(drive / engine)
                if len(found) >= DRIVE_MIN:
                    medians[g] = _median(found)
        out = {}
        for gear, own in medians.items():
            higher = [m for g, m in medians.items() if g > gear]
            if not higher:
                continue
            # Only a higher gear stands for the engine: grip caps the force at
            # the wheels, which is highest in the low gears, while a resistance
            # the model underestimates (loose gravel, drag) takes most, as a
            # share, from the high gears' smaller drive
            reference = max(higher)
            if reference <= 0:
                continue
            samples = self.drive[(surface, gear)]
            cap = _median([drive for _, _, drive, _ in samples]) / reference
            # The limit shows only where the engine could pull past it: at
            # low revs the gear is short of it on the engine's account
            capped = [drive for rpm, _, drive, _ in samples if (engine_drive(gear, rpm) or 0.0) > cap]
            if len(capped) >= DRIVE_MIN // 2:
                cap = _median(capped) / reference
            out[gear] = (own < GRIP_SHARE * reference, cap, own / reference, len(samples))
        return out

    def best_for(self, gear, surface=None):
        """The best change up from `gear` on `surface` (None: whatever the
        surface): {'rpm', 'coverage', 'source' ('game': from the game's
        engine data, 'learnt'), 'surface' (the one it was checked on, or
        None), 'grip_limited' (True, False, or None where not measured),
        'engine_rpm' (the engine's best)}, or None when not known. The
        engine's best, from the game's data where shipped, else learnt; on
        a surface where `gear` is measured grip-limited, the lowest rpm
        from which gear + 1 reaches the same grip limit (the drive lost
        changing up there is none). From there up to the engine's best
        both gears are held to the grip limit, so a change anywhere in
        between costs nothing (measured_against())."""
        rpm = self.game_best(gear)
        if rpm is not None:
            found = {'rpm': rpm, 'coverage': 1.0, 'source': 'game'}
        else:
            if self.game_data():
                return None
            best = self.best_shift(gear)
            if best is None:
                return None
            found = {'rpm': best[0], 'coverage': best[1], 'source': 'learnt'}
        found.update(surface=None, grip_limited=None, engine_rpm=found['rpm'])
        if surface is None:
            return found
        grip = self.grip(gear, surface)
        if grip is None:
            return found
        limited, cap, share, _count = grip
        found.update(surface=surface, grip_limited=limited, grip_share=share)
        if limited:
            lowered = self._scan(gear, cap, GRIP_TOLERANCE)
            if lowered is not None and lowered < found['rpm']:
                found['rpm'] = lowered
        return found

    def game_ratio(self, gear, surface=None):
        """rpm per m/s in `gear` from the game's gearing and the tyre of
        `surface` (its driven axle's), or None."""
        gear_set = self.gear_set()[0]
        if gear_set is None or not 1 <= gear <= len(gear_set['gears']):
            return None
        radii = self.shipped.get('tyre_radius_m') or {}
        radius = radii.get(surface) or radii.get('gravel') or next(iter(radii.values()), None)
        if not radius:
            return None
        r = radius[1] if self.shipped.get('drivetrain') == 'rwd' else radius[0]
        return (gear_set['gears'][gear - 1] * gear_set.get('primary', 1.0) * self.shipped['final_drive']
                * 60.0 / (2 * math.pi * r))

    def grip_limited_anywhere(self, gear):
        """Whether `gear` is measured grip-limited on any surface."""
        for surface in {s for s, g in self.drive if g == gear}:
            grip = self.grip(gear, surface)
            if grip is not None and grip[0]:
                return True
        return False

    def snapshot(self, bands=None, surface=None):
        """A plain dict for the GUI, its best changes up for `surface`
        (None: whatever the surface); `bands` from best_bands() give each
        learnt best change up its range (best_low, best_high)."""
        from . import coach_context as cc
        gears = self.gears()
        top = self.top_gear()
        bands = bands or {}
        gear_set, miss = self.gear_set()
        data = self.game_data()
        lights = (self.shipped or {}).get('shift_lights_rpm')
        context = {'limiter': self.known_limiter() or None, 'lights': lights, 'surface': surface,
                   'best_for': lambda gear: self.best_for(gear, surface if surface in SURFACES else None),
                   'pulls': lambda gear: len({v[3] for v in self.drive.get(
                       (surface if surface in SURFACES else None, gear), ())})}
        rows = []
        for gear in (gears if data is None else sorted(set(gears) | set(range(1, top + 1)))):
            last = gear + 1 not in gears if data is None else gear >= top
            best = self.best_for(gear, surface) if not last else None
            average = self.average_upshift(gear)
            band = bands.get(gear) if best and best['source'] == 'learnt' and not best['grip_limited'] else None
            # The game's lights band the coach judges the change up against (shift_band), where the game ships lights
            lit = cc.shift_band(context, gear) if best and lights and lights.get('shift') else None
            rows.append({
                'lights_low': lit[0] if lit else None,
                'lights_high': lit[1] if lit else None,
                'gear': gear,
                'ratio': self.ratio(gear),
                'ratio_samples': len(self.ratios.get(gear, [])),
                'best': best['rpm'] if best else None,
                'engine_best': best['engine_rpm'] if best else None,
                'best_low': band[0] if band else None,
                'best_high': band[1] if band else None,
                'coverage': best['coverage'] if best else 0.0,
                'source': best['source'] if best else None,
                'grip_limited': best['grip_limited'] if best else None,
                'average_shift': average[0] if average else None,
                'shifts': average[1] if average else 0,
                'last': last,
            })
        return {'key': self.key, 'name': self.name, 'limiter': self.ceiling(), 'gears': rows,
                'limiter_source': 'game data' if self.shipped and self.limiter_source != 'launch'
                else self.limiter_source,
                'power_bands': self.known_bands(), 'limiter_time': self.limiter_time,
                'power_source': 'game data' if data else self.power_source, 'retuned': dict(self.retuned),
                'gearing_aside': self.gearing_aside(),
                'surface': surface, 'gear_set': gear_set['id'] if gear_set else None,
                'ratio_miss': miss}

    def advice(self, held_limiter_time=0.0, surface=None):
        """Coaching from what has been learnt, for `surface` (None:
        whatever the surface): a list of sentences. The changes up of each
        gear (the last SHIFTS_KEEP flat out) are judged against the game's
        lights band the way the coach judges a run's
        (coach_context.shift_band, docs/coach-techniques.md section 7.3 R1):
        a share on the limiter cut (a tip from a quarter of them), a share
        below the band (a tip from 60 %, on tarmac, or on a loose surface in
        3rd and up where the gear is measured not grip-limited), both, or on
        the band ("Spot on", from 60 % with at most a tenth on the cut). At
        most TIPS_SHOWN tips, one line of praise and one line each about
        grip-limited gears, re-tuned gears and what is still being learnt,
        so the same list is not repeated gear by gear. `held_limiter_time`
        is the seconds of the session held on the limiter on straights."""
        from . import coach_context as cc
        tips = []                        # (weight, sentence): the biggest first
        spot_on = []
        grip_limited = []
        waiting = []                     # early on a loose surface whose grip in the gear is not measured
        ceiling = self.ceiling()
        limiter = self.known_limiter() or None
        top = self.top_gear()
        lights = (self.shipped or {}).get('shift_lights_rpm')
        word = 'lights band' if lights else 'shift band'
        context = {'limiter': limiter, 'lights': lights, 'surface': surface,
                   'best_for': lambda gear: self.best_for(gear, surface if surface in SURFACES else None),
                   'pulls': lambda gear: len({v[3] for v in self.drive.get(
                       (surface if surface in SURFACES else None, gear), ())})}
        for gear in range(1, (top or 0)):
            best = self.best_for(gear, surface if surface in SURFACES else None)
            peaks = self.upshifts.get(gear) or []
            if best is None or best['coverage'] < SHIFT_COVERAGE or len(peaks) < ADVICE_MIN or not self.step(gear):
                continue
            band = cc.shift_band(context, gear)
            if band is None:
                continue
            low, high, _ = band
            n = len(peaks)
            cut = sum(1 for p in peaks if limiter and p >= limiter * LIMITER_BAND) / n
            early = sum(1 for p in peaks if p < low - cc.EARLY_BELOW and not (limiter and p >= limiter * LIMITER_BAND)) / n
            on = sum(1 for p in peaks if low - cc.EARLY_BELOW <= p <= high + cc.BAND_SLACK
                     and not (limiter and p >= limiter * LIMITER_BAND)) / n
            change = '{}→{}'.format(gear, gear + 1)
            # The plausibility gate shift_band applies to a lowered band: measured over enough pulls, not above
            # the engine's own drive, and never 3rd and up on tarmac
            share = best.get('grip_share')
            if (best['grip_limited'] and best['engine_rpm'] > best['rpm']
                    and context['pulls'](gear) >= cc.GRIP_PULLS and share is not None and share <= cc.GRIP_SHARE_MAX
                    and not (gear >= 3 and surface == 'tarmac')):
                grip_limited.append('{} from {:.0f} to {:.0f} rpm'.format(change, best['rpm'], best['engine_rpm']))
            # The cut and early shares are not told here: the coach judges them per surface, without the
            # launch and by what they cost a stage (coach.Coach._shift_tips), and a pooled live share
            # nagged about changes it calls technique or too cheap to coach. Grip-limited and unmeasured
            # gears are still named below, for the coach's lines to be read by.
            if early >= ADVICE_EARLY and not cut >= ADVICE_CUT and surface not in ('tarmac',) \
                    and surface in SURFACES and surface not in ('snow', 'ice') and gear >= 3 \
                    and best['grip_limited'] is None:
                waiting.append(change)
            elif on >= ADVICE_ON and cut <= ADVICE_ON_CUT and gear > 1:
                # 1st's changes up include the launch's, which the learnt peaks cannot tell from the rest
                spot_on.append((change, n))
        if held_limiter_time > 3:
            # Seconds held on the limiter on a straight weigh like a few hundred rpm of error
            tips.append((held_limiter_time * 0.1, '{:.0f} s held on the limiter on straights this session, with no '
                         'corner to use it for: the engine makes nothing there. Change up as the lights '
                         'flash.'.format(held_limiter_time)))
        lines = [text for _, text in sorted(tips, key=lambda tip: -tip[0])[:TIPS_SHOWN]]
        if spot_on:
            names = [change for change, _ in spot_on]
            lines.append("Spot on: {} on the {} ({} changes).".format(
                _listed(names), word, sum(count for _, count in spot_on)))
        if grip_limited:
            lines.append('On {} the lower gear is grip-limited, so changing up anywhere in the range gives the same '
                         'drive: {}.'.format(surface, _listed(grip_limited)))
        if waiting:
            lines.append('{} on {}: early, but not coached until the grip of the lower gear there is measured (a few '
                         'full-throttle pulls in it): on a loose surface short-shifting can be right.'.format(
                             _listed(waiting), surface))
        if self.retuned:
            retuned = sorted(self.retuned)
            lines.append('{} re-tuned; {} shift points are being learnt again.'.format(
                'Gear {} was'.format(retuned[0]) if len(retuned) == 1 else
                'Gears {} were'.format(', '.join(str(g) for g in retuned)),
                'its' if len(retuned) == 1 else 'their'))
        needed = int(ceiling * 0.5 / POWER_BIN) if ceiling else 0
        bands = self.known_bands()
        if not self.game_data() and needed and bands < needed * 0.8:
            lines.append('Still learning the engine ({} of about {} rev bands known): full-throttle pulls from '
                         'low revs, out of slow corners, fill it in fastest.'.format(bands, needed))
        return lines

    def drive_lost(self, gear, rpm):
        """' At that speed gear 3 gives 9 % less drive than staying in 2.'
        when changing up from `gear` at `rpm` loses drive, else ''."""
        step = self.step(gear)
        if not step:
            return ''
        if self.game_data():
            stay, after = self.engine_drive(gear, rpm), self.engine_drive(gear + 1, rpm * step)
        else:
            stay = self.power_at(rpm, gear) or self.power_at(rpm)
            after = self.power_at(rpm * step, gear + 1) or self.power_at(rpm * step)
        if not stay or not after or after >= stay:
            return ''
        return ' At that speed gear {} gives {:.0f} % less drive than staying in {}.'.format(
            gear + 1, (1 - after / stay) * 100, gear)


def measured_against(best, rpm):
    """(the rpm a change up at `rpm` is measured against, low, high) for a
    best from best_for(). Where the gear is grip-limited, anything from the
    lowered best to the engine's is on target: the change itself there,
    else the nearer end, and (low, high) is that range; otherwise the best,
    and None, None."""
    low, high = best['rpm'], best.get('engine_rpm') or best['rpm']
    if high <= low:
        return low, None, None
    return min(max(rpm, low), high), low, high


def _listed(names):
    return names[0] if len(names) == 1 else '{} and {}'.format(', '.join(names[:-1]), names[-1])


def classify_tune(old, new, changed):
    """What changed between two gearings ({gear: rpm per m/s}), given the
    gears seen to change: 'final-drive' when two or more changed by the
    same factor (within 1 %), else 'gears:<list>'."""
    factors = [new[g] / old[g] for g in changed if g in old and old[g]]
    if len(factors) >= 2 and max(factors) / min(factors) <= 1.01:
        return 'final-drive'
    return 'gears:' + ','.join(str(g) for g in changed)


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


WHEEL_CHECK = 60                 # clean samples of wheel against car speed before wheel speeds are believed
WHEEL_AGREE = 0.02               # the wheels' median within this of the car's speed at low slip
WHEEL_EVERY = 20                 # samples between working the medians out again
SPIN_EVIDENCE = 0.03             # a wheel this much faster than the car: which ones drive shows
DRIVETRAIN_VOTES = 60            # such samples before the driven wheels are decided
DRIVETRAIN_SHARE = 0.8


class DrivenWheels:
    """The driven wheels' speed, where the game sends wheel speeds (or
    Forza's wheel rotation, once each tyre's radius is learnt) and they
    agree with the car's own speed when nothing slips: a unit or sign
    the research could not confirm must not become a ratio. Which wheels
    drive comes from the game, or from which axle's speed stays locked to
    the engine while the wheels spin. What it learns lives in the
    CarModel, so it carries over sessions."""

    def __init__(self):
        self._count = 0

    def feed(self, car, sample, gear, rpm, speed, known, low_slip, flat_out):
        """The driven wheels' mean speed (m/s), or None."""
        speeds = self._speeds(car, sample, speed, low_slip)
        if speeds is None:
            return None
        if low_slip:
            self._count += 1
            car.wheel_check.append(sorted(speeds)[1] / speed)       # a middle wheel: corners spread them
            del car.wheel_check[:-WHEEL_CHECK]
        if car.wheels_ok is None or (low_slip and self._count % WHEEL_EVERY == 0):
            car.wheels_ok = (len(car.wheel_check) >= WHEEL_CHECK
                             and abs(_median(car.wheel_check) - 1.0) <= WHEEL_AGREE)
        if not car.wheels_ok:
            return None
        drivetrain = car.drivetrain
        if drivetrain is None:
            drivetrain = self._vote(car, speeds, rpm, speed, known, flat_out)
            if drivetrain is None:
                return None
        wheels = DRIVEN[drivetrain]
        return sum(speeds[i] for i in wheels) / len(wheels)

    def _speeds(self, car, sample, speed, low_slip):
        if sample.wheel_speed is not None:
            return [abs(v) for v in sample.wheel_speed]
        if sample.wheel_rot is None:
            return None
        rot = [abs(w) for w in sample.wheel_rot]
        if low_slip and min(rot) > 1.0:
            for i, w in enumerate(rot):
                radii = car.tyre_radius.setdefault(i, [])
                radii.append(speed / w)
                del radii[:-WHEEL_CHECK]
            if car.radii is None or len(car.tyre_radius[0]) % WHEEL_EVERY == 0:
                if all(len(car.tyre_radius.get(i, ())) >= WHEEL_CHECK for i in range(4)):
                    car.radii = tuple(_median(car.tyre_radius[i]) for i in range(4))
        if car.radii is None:
            return None
        return [w * r for w, r in zip(rot, car.radii)]

    @staticmethod
    def _vote(car, speeds, rpm, speed, known, flat_out):
        """Wheels spinning at full throttle: the axle whose speed still
        matches the engine's through the gear ratio is the driven one."""
        if known is None or not flat_out or max(speeds) / speed - 1.0 < SPIN_EVIDENCE:
            return None
        votes = car.drivetrain_votes
        for name, wheels in DRIVEN.items():
            axle = sum(speeds[i] for i in wheels) / len(wheels)
            if axle > 0 and abs(rpm / axle - known) <= known * 0.01:
                votes[name] = votes.get(name, 0) + 1
        votes['n'] = votes.get('n', 0) + 1
        if votes['n'] < DRIVETRAIN_VOTES:
            return None
        share = {name: votes.get(name, 0) / votes['n'] for name in DRIVEN}
        front, rear = share['fwd'] >= DRIVETRAIN_SHARE, share['rwd'] >= DRIVETRAIN_SHARE
        if share['awd'] >= DRIVETRAIN_SHARE and front == rear:
            found = 'awd'
        elif front and not rear:
            found = 'fwd'
        elif rear and not front:
            found = 'rwd'
        else:
            if votes['n'] >= DRIVETRAIN_VOTES * 4:
                car.drivetrain_votes = {}            # no answer: start the count again
            return None
        logging.info("shift learner: %s drives its %s wheels (%d samples of wheelspin)", car.key,
                     {'fwd': 'front', 'rwd': 'rear', 'awd': 'four'}[found], votes['n'])
        car.drivetrain = found
        return found


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
        self._session_batch = {}            # session number -> the drive log's batch its row was written in
        self._session_lost = False          # its row was rolled back: start the session again
        self._last_feed = None              # monotonic time of the last packet fed
        self._pending = collections.deque()  # changes of gear waiting DOUBLE_TAP_REVERT before they are written
        self._wheels = DrivenWheels()
        from .drive_log import RunTracker
        self.runs = RunTracker(self)
        self._session_moving = 0.0          # s on the move this session, for the re-tune window
        self._session_distance = 0.0        # m
        self._retuned = set()               # gears re-tuned this session
        self.session_limiter_time = 0.0
        self.session_held_time = 0.0         # s of held-straight limiter episodes of the session's runs (coach_context)
        self.history_changed = 0            # counts up when history readers should query again
        self._loaded = None                 # load_snapshot(): ((profile, key, updated), snapshot)
        self._shift_cache = {}
        self._shift_cache_at = 0.0
        self._lights = None                 # publish(): (car key, surface, {gear: best_for()}) for the listener
        self._bands = {}                    # key -> (monotonic time, best_bands())
        self.run_surface = {}               # run number -> its surface, once its stage is known (drive-log thread)
        if database is not None:
            self.open(database)

    @property
    def surface(self):
        """The surface of the run going on ('gravel', 'tarmac', ...), or
        None while it is not known: from the stage's table or the stage's
        earlier runs, set on the drive-log thread when the run starts."""
        run = self.runs.run
        return self.run_surface.get(run) if run is not None else None

    @property
    def db(self):
        """The writer's connection, or None before open()."""
        return self.log.store.db if self.log is not None else None

    def open(self, path):
        from .drive_log import DriveLog
        log = DriveLog(path, threaded=self.threaded)
        log.on_rollback.append(self._rolled_back)
        if self.threaded:
            log.ticks.append(self.publish)
            log.ticks.append(self._backfill_tick)
        with self.lock:
            self.log = log

    BACKFILL_BATCH = 3                  # runs worked over per batch
    BACKFILL_QUIET = 2.0                # s without a packet before a batch is started

    _backfilled = False                 # nothing left to work over (the drive-log thread's belief)
    _backfill_queued = False

    def _backfill_tick(self):
        """Drive-log thread, once a second: queue a batch of the backfill of
        runs from before the context layer (drive_log.backfill_step) when no
        run is on and the game has been quiet a moment: a batch takes the
        thread for a second or two, and the first live run after an upgrade
        must not wait behind sixty traces."""
        if self._backfilled or self._backfill_queued or self.log is None:
            return
        if self.runs.run is not None or (self._last_feed is not None
                                         and time.monotonic() - self._last_feed < self.BACKFILL_QUIET):
            return
        self._backfill_queued = self.log.post(self._backfill_batch)

    def _backfill_batch(self):
        from .drive_log import backfill_step
        self._backfill_queued = False
        if backfill_step(self, self.BACKFILL_BATCH) == 0:
            self._backfilled = True
        return True

    def backfill(self):
        """Work every run from before the context layer over now (tests,
        replays and the command line; the app does it a few runs at a time
        from its drive-log thread). Returns the number of runs."""
        from .drive_log import backfill_step
        total = 0
        while self.log is not None:
            done = backfill_step(self, self.BACKFILL_BATCH)
            self.log.store.commit()
            total += done
            if not done:
                break
        return total

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
        self._positions = collections.deque()    # world positions over the same window, for the slope
        self._gear = None
        self._gear_since = 0.0
        self._last = None                        # previous (t, rpm, gear, throttle)
        self._left = None                        # (gear, rpm, throttle, t, via neutral) when a forward gear was left
        self._off_ratio = {}                     # gear -> part-throttle ratios off the known one
        self._recent = collections.deque()       # (t, rpm, throttle) over SHIFT_WINDOW in a forward gear
        self._slip = None                        # driven-wheel slip of the last sample in a forward gear
        self._flat_since = None                  # when the throttle went to the floor
        self._drive_at = None                    # when the last drive sample was kept
        self._drive_pull = None                  # the _flat_since of the pull it came from

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
        self.session_held_time = 0.0

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
        self.session_held_time = 0.0
        self._session_moving, self._session_distance, self._retuned = 0.0, 0.0, set()
        if self.log is not None:
            self.log.post(self._write_session_start, self.session, self.profile, self.car, self.wall(now),
                          sample.track, sample.stage)

    def _write_session_start(self, number, profile, car, started, track, stage):
        store = self.log.store
        with self.lock:
            data = car.to_dict()
        car_id = store.car_id(profile, car.key, car.game or 'unknown', car.name, data)
        self._session_rows[number] = (store.start_session(profile, car_id, car.game, started, track, stage), car_id)
        self._session_batch[number] = self.log.batch

    def _rolled_back(self, batch):
        """Drive-log thread: the writes of `batch` were rolled back (a full
        disk, a lock held too long). SQLite hands their row ids out again,
        so the sessions and runs written in it are forgotten here, or later
        shifts and runs would land in whatever row gets the id next; a
        session that lost its row starts again with the next packet."""
        runs = self.runs
        lost = [n for n, b in self._session_batch.items() if b == batch]
        for number in lost:
            self._session_rows.pop(number, None)
            del self._session_batch[number]
        for number in [n for n, b in runs.run_batch.items() if b == batch]:
            runs.run_rows.pop(number, None)
            del runs.run_batch[number]
        if lost:
            self._session_lost = True

    def _end_session_locked(self):
        if self.session is None:
            return
        if self.car is not None:
            self._flush_shifts_locked(self.car)
        if self.log is not None:
            self.runs.end(self._last_feed or 0.0, 'session')
        number, self.session = self.session, None
        if self.log is None:
            self.history_changed += 1
            return
        self._save_locked(force=True)
        car = self.car
        ratios = {g: car.ratio(g) for g in car.gears()} if car is not None else {}
        radius = sum(car.radii) / 4 if car is not None and car.radii else None
        self.log.post(self._write_session_end, number, self.wall(self._last_feed), self.session_limiter_time,
                      ratios, set(self._retuned), radius)

    def _write_session_end(self, number, ended, limiter_time, ratios, retuned, radius):
        row = self._session_rows.pop(number, None)
        self._session_batch.pop(number, None)
        if row is not None:
            store = self.log.store
            session, car = row
            tune = self._write_tune(car, ended, ratios, retuned, radius)
            store.update_session(session, ended=ended, limiter_time=limiter_time,
                                 shifter=store.session_shifter(session), tune=tune)
            store.summarise_session(session)
            if store.drop_session_if_empty(session):
                store.drop_car_if_empty(row[1])
            store.commit()
        self.history_changed += 1
        return True

    def _write_tune(self, car, at, ratios, retuned, radius):
        """Drive-log thread: the tune (one gearing) this session was driven
        on. A gear re-tuned in the session opens a new one; otherwise the
        current one is touched and takes gears learnt since."""
        if len(ratios) < 2:
            return None
        store = self.log.store
        current = store.current_tune(car)
        if current is None:
            return store.add_tune(car, at, ratios, 'first', radius)
        tune, known = current
        changed = sorted(g for g in retuned if g in ratios)
        if changed:
            return store.add_tune(car, at, ratios, classify_tune(known, ratios, changed), radius)
        merged = dict(known)
        merged.update({g: r for g, r in ratios.items() if g not in known})
        store.touch_tune(tune, at, merged, radius)
        return tune

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
            self._lights = None
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
            data = self._snapshot_of(copy, bands=self._bands_of(copy), surface=copy.last_surface)
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
        data = self._snapshot_of(car, bands=self._bands_of(car), surface=car.last_surface)
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
            copy, limiter_time, surface = self.car.copy(), self.session_limiter_time, self._shown_surface()
            held = self.session_held_time
        return self._snapshot_of(copy, limiter_time, self._bands_of(copy), surface, held)

    published = None                    # the drive-log thread's last snapshot of the current car

    def publish(self):
        """Drive-log thread, once a second: work the snapshot out here so
        readers never do it under the lock, and the best changes up for
        the surface driven on now, which the listener reads for the rev
        lights and each change up (_best_now) instead of working them out
        per packet."""
        with self.lock:
            if self.car is None:
                self.published = self._lights = None
                return
            copy, limiter_time, surface = self.car.copy(), self.session_limiter_time, self._shown_surface()
            held = self.session_held_time
            now = self.surface
        top = copy.top_gear() or 0
        self._lights = (copy.key, now, {g: copy.best_for(g, now) for g in range(1, top)})
        self.published = self._snapshot_of(copy, limiter_time, self._bands_of(copy), surface, held)

    def _shown_surface(self):
        """The surface the live snapshot is for: the run's, else the one
        last driven on."""
        return self.surface or (self.car.last_surface if self.car is not None else None)

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
    def _snapshot_of(car, session_limiter_time=0.0, bands=None, surface=None, held_time=None):
        """`session_limiter_time` is every second on the cut this session (what the tab shows);
        the advice only counts `held_time` of them, the held-straight ones (coach_context), where
        given: the rest is the shift, the corner and the wheelspin."""
        data = car.snapshot(bands, surface)
        data['session_limiter_time'] = session_limiter_time
        data['advice'] = car.advice(session_limiter_time if held_time is None else held_time, surface)
        return data

    def shift_rpm(self, gear):
        """The best upshift for `gear` on the surface driven on now, for
        the rev lights (from the game's data where shipped, else learnt);
        None to fall back to the percentage rule."""
        with self.lock:
            if self.car is None or gear is None or gear < 1:
                return None
            if self.threaded:
                best = self._best_now(self.car, gear)
                return best['rpm'] if best and best['coverage'] >= SHIFT_COVERAGE else None
            # Without a drive-log thread (tests, replays): worked out here, at
            # most every couple of seconds
            now = time.monotonic()
            if now - self._shift_cache_at > 2.0:
                self._shift_cache, self._shift_cache_at = {}, now
            key = (gear, self.surface)
            if key not in self._shift_cache:
                best = self._best_now(self.car, gear)
                self._shift_cache[key] = best['rpm'] if best and best['coverage'] >= SHIFT_COVERAGE else None
            return self._shift_cache[key]

    def _best_now(self, car, gear):
        """best_for(gear) on the surface driven on now. With a drive-log
        thread, what publish() last worked out there (None until it has,
        for this car and surface: at most a second), so the listener never
        works a best out under the lock; without one, worked out here."""
        if shared_car(car.key):
            return None
        surface = self.surface
        if not self.threaded:
            return car.best_for(gear, surface)
        lights = self._lights
        if lights is None or lights[0] != car.key or lights[1] != surface:
            return None
        return lights[2].get(gear)

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
            if self._session_lost:
                self._session_lost = False
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
            shipped_drivetrain = car.shipped.get('drivetrain') if car.shipped else None
            if shipped_drivetrain in DRIVEN:
                car.drivetrain = shipped_drivetrain          # the game's files beat a vote (a Fabia is AWD)
            elif sample.drivetrain is not None:
                car.drivetrain = sample.drivetrain
            if self.session is None:
                self._start_session_locked(now, sample)
            if self.log is not None:
                self.runs.feed(now, sample, throttle, self.session, self.profile)
            self._press = press
            if not shared_car(car.key) and car.set_limiter(limiter, limiter_source):
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
        if previous is not None and speed > MIN_SPEED:
            dt = min(0.2, max(0.0, now - previous[0]))
            self._session_distance += speed * dt
            self._session_moving += dt
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
            self._positions.clear()
            recent.clear()
        if gear >= 1:
            recent.append((now, rpm, throttle))
            while now - recent[0][0] > SHIFT_WINDOW:
                recent.popleft()
            slip = drive_slip(sample, car.drivetrain, car.radii)
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
        positions = self._positions
        if sample.pos is None:
            positions.clear()
        else:
            positions.append(sample.pos)
            while len(positions) > len(self._speeds):
                positions.popleft()

        clutch_out = clutch is None or clutch <= CLUTCH_OUT
        settled = now - self._gear_since >= SETTLED
        if gear < 1 or speed < MIN_SPEED or not clutch_out or not settled or shared_car(car.key):
            return
        if flat_out and rpm > car.top_seen:
            car.top_seen = rpm
        limiter = car.known_limiter()
        if flat_out and limiter and rpm >= limiter * LIMITER_BAND and previous is not None:
            dt = min(0.2, max(0.0, now - previous[0]))
            car.limiter_time += dt
            self.session_limiter_time += dt

        ratio = rpm / speed
        known = car.ratio(gear)
        # A ratio is learnt only where the tyres barely slip: part throttle,
        # but some (coasting, the engine brakes the wheels and they slip
        # the other way), off the brake, clutch out, at a steady speed.
        # Full throttle on gravel spins the wheels a steady few percent,
        # which would look like a ratio of its own and then reject every
        # clean sample as off-ratio for good. Where the driven wheels'
        # speed is known (and checked against the car's), rpm over it is
        # the gearing itself, which spin cannot distort: any throttle from
        # RATIO_THROTTLE up will do.
        brake = sample.brake
        brake_off = brake <= BRAKE_OFF if brake is not None else (throttle is not None and throttle >= COASTING)
        pulling = throttle is not None and throttle >= RATIO_THROTTLE and brake_off
        low_slip = pulling and throttle < LOW_SLIP_THROTTLE
        if low_slip:
            accel = self._acceleration()
            low_slip = accel is not None and abs(accel) <= STEADY_ACCEL
        wheels = self._wheels.feed(car, sample, gear, rpm, speed, known, low_slip, flat_out)
        if wheels is not None and pulling:
            learnt, clean = rpm / wheels, True
        else:
            learnt, clean = ratio, low_slip
        on_ratio = known is not None and abs(ratio - known) <= known * RATIO_TOLERANCE
        if clean and (known is None or abs(learnt - known) <= known * RATIO_TOLERANCE):
            samples = car.ratios.setdefault(gear, [])
            samples.append(learnt)
            del samples[:-RATIO_KEEP]
            self._dirty = True
            self._off_ratio.pop(gear, None)
        elif clean:
            # Off the gear's ratio without spin to blame: the gear was
            # re-tuned, if it stays that way
            self._check_retune(car, gear, learnt, speed, now, wheels is not None)
            return
        if not flat_out or now - self._flat_since < car.boost_hold:
            # Power counts once the throttle has been floored a moment: a
            # turbo builds boost after the pedal goes down
            return
        if car.limiter and rpm > car.limiter * 1.01:
            return
        if brake is not None and brake >= BRAKE_POWER:
            return                                   # left-foot braking: the engine is fighting it
        accel, slope_free = self._drive_acceleration(sample)
        drive = None
        if accel is not None:
            # The acceleration is the window's, so it belongs to the window's
            # middle: at 3000 rpm a second in 2nd gear, the last sample's rpm
            # would be 500 too high
            points = self._speeds
            mid_rpm = sum(p[3] for p in points) / len(points)
            mid_speed = sum(p[1] for p in points) / len(points)
            c0, c2 = car.drag
            drive = accel + c0 + c2 * mid_speed * mid_speed
            self._drive_sample(car, now, gear, mid_rpm, mid_speed, drive)
        if not on_ratio:
            # Wheelspin or a jump, or a gear not learnt yet: no power from it
            return
        slip = drive_slip(sample, car.drivetrain, car.radii)
        if slip is not None and slip[0] > SLIP_POWER[slip[1]]:
            return                                   # spinning: the drive is not reaching the road
        if sample.power is not None:
            if car.power_source != 'game':
                car.power, car.power_g, car.power_source = {}, {}, 'game'   # the real figure beats the estimate
            power, pooled = sample.power, True
        else:
            if car.power_source == 'game' or drive is None or accel <= 0:
                return
            if slope_free is None:
                if car.slope_free:
                    # The positions give this car's slope, only not while it
                    # goes this slowly: no sample, rather than one with a
                    # hill in it and the car's pooling flickering off
                    return
                slope_free = False
            car.power_source = 'accel'
            car.slope_free = bool(slope_free)
            rpm = mid_rpm
            power, pooled = drive * mid_speed, bool(slope_free)
        if power <= 0:
            return
        band = int(rpm // POWER_BIN)
        # Pooled over the gears only with the slope taken out: a hill in the
        # bands one gear was driven in and not in the next one's would show
        # as a power drop between them
        for values in ((car.power.setdefault(band, []),) if pooled else ()) + (
                car.power_g.setdefault((gear, band), []),):
            values.append(power)
            del values[:-POWER_KEEP]

    def _drive_sample(self, car, now, gear, rpm, speed, drive):
        """What the drive achieves on the surface driven on, wheelspin and
        slides included (taken before a sample off the gear's ratio is
        dropped): whether the gear is grip-limited there. Kept every
        DRIVE_EVERY seconds (the acceleration is a window's, so the
        samples of one moment say the same) with the pull they came from:
        grip is judged from several pulls, not one run up the revs."""
        surface = self.surface
        if surface not in SURFACES or (self._drive_at is not None and now - self._drive_at < DRIVE_EVERY):
            return
        if self._drive_pull != self._flat_since:
            self._drive_pull = self._flat_since
            car.pulls += 1
        self._drive_at = now
        samples = car.drive.setdefault((surface, gear), [])
        samples.append((rpm, speed, drive, car.pulls))
        del samples[:-DRIVE_KEEP]
        car.last_surface = surface

    def _shift_locked(self, car, left, to, engage_rpm, now):
        """A change from a forward gear to another: kept pending for
        DOUBLE_TAP_REVERT seconds, so a quick correction can flag it and
        the revs the new gear brought are known, then written."""
        start, peak, throttle, left_at, via_neutral, slip = left
        up = to > start
        flat_out = up and (throttle is None or throttle >= SHIFT_THROTTLE)
        if up and to == start + 1 and flat_out:
            shifts = car.upshifts.setdefault(start, [])
            shifts.append(peak)
            del shifts[:-SHIFTS_KEEP]
            self._dirty = True
        # Measured against a best the lights would trust, or none: a half
        # learnt curve would coach "600 rpm early" from noise
        best = self._best_now(car, start) if up and to == start + 1 else None
        best = best if best is not None and best['coverage'] >= SHIFT_COVERAGE else None
        band = (self._bands.get(car.key, (0, {}))[1].get(start)
                if best and best['source'] == 'learnt' and not best['grip_limited'] else None)
        against = None
        if best is not None:
            against, low, high = measured_against(best, peak)
            band = (low, high) if low is not None else band
        shift = {'_t': now, 'at': self.wall(now), 'gear': start, 'gear_to': to, 'direction': 'up' if up else 'down',
                 'rpm': peak, 'best': against, 'best_low': band[0] if band else None,
                 'best_high': band[1] if band else None, 'throttle': throttle,
                 'method': shift_method(getattr(self, '_press', None), left_at, now, via_neutral),
                 'neutral_time': now - left_at if via_neutral else 0.0, 'engage_rpm': engage_rpm,
                 'flat_out': int(flat_out), 'slip': slip, 'flags': [], '_run': self.runs.run}
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
            limiter = car.known_limiter()
            if shift['direction'] == 'up':
                if (shift['neutral_time'] > MISSED_NEUTRAL and shift['throttle'] is not None
                        and shift['throttle'] >= SHIFT_THROTTLE):
                    flags.append('missed')           # stuck in neutral, foot down: a missed gate
                if shift['gear_to'] >= shift['gear'] + 2:
                    flags.append('skip')
            else:
                if (shift['gear_to'] == shift['gear'] - 1 and shift['throttle'] is not None
                        and shift['throttle'] >= SHIFT_THROTTLE and limiter and shift['rpm'] >= 0.9 * limiter):
                    flags.append('skip')             # flat out near the limiter and down a gear: meant to go up
                if limiter and shift['engage_rpm'] > OVER_REV * limiter:
                    flags.append('over-rev')
            shift['flags'] = ','.join(flags) or None
            if self.log is not None and self.session is not None:
                self.log.post(self._write_shift, self.session, shift)

    def _write_shift(self, number, shift):
        row = self._session_rows.get(number)
        run = self.runs.run_rows.get(shift.pop('_run'))
        if row is not None:
            self.log.store.add_shift(row[0], run, shift)

    def _check_retune(self, car, gear, ratio, speed, now, from_wheels):
        """A clean sample off the gear's ratio. Setups change in menus, which
        end sessions in every game, so a new ratio is only believed in
        the first RETUNE_WINDOW seconds on the move or RETUNE_DISTANCE
        metres of a session, whichever ends first, from samples agreeing within RETUNE_SPREAD over at least
        two speeds RETUNE_SPEED_BIN apart. A shorter ratio (more rpm per
        m/s) is what steady wheelspin also looks like, so it needs twice
        the samples unless the driven wheels' speed gave them."""
        if self._session_moving > RETUNE_WINDOW or self._session_distance > RETUNE_DISTANCE:
            return
        needed = RETUNE_SAMPLES
        if ratio > car.ratio(gear) and not from_wheels:
            needed *= 2
        off = self._off_ratio.setdefault(gear, [])
        off.append((ratio, speed))
        del off[:-RETUNE_SAMPLES * 2]
        recent = off[-needed:]
        if len(recent) < needed:
            return
        ratios = [r for r, _ in recent]
        middle = _median(ratios)
        speeds = {int(v // RETUNE_SPEED_BIN) for _, v in recent}
        if len(speeds) >= 2 and all(abs(r - middle) <= middle * RETUNE_SPREAD for r in ratios):
            car.ratios[gear] = ratios
            car.upshifts.pop(gear, None)             # shifts with the old gearing
            car.upshifts.pop(gear - 1, None)
            car.retuned[gear] = self.wall(now)
            car.top_seen = 0.0                       # where the data ends, with the old gearing
            self._off_ratio.pop(gear, None)
            self._retuned.add(gear)
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
        vector (world y up) or from how much its position climbed over the
        distance it travelled; otherwise the change of speed alone (False:
        a hill is in it; None: positions came, but the car went too short
        a way for its climb to be a slope)."""
        if sample.accel_kind == 'specific' and sample.accel is not None:
            return sample.accel[0], True
        accel = self._acceleration()
        if accel is None:
            return None, False
        if sample.forward is not None:
            return accel + G * sample.forward[1], True
        grade = self._grade()
        if grade is not None:
            return accel + G * grade, True
        return accel, None if len(self._positions) >= ACCEL_MIN_POINTS else False

    def _grade(self):
        """The sine of the road's slope over the recent window, from the
        positions (y up): the climb over the path length. None without
        positions or when the car barely moved."""
        points = self._positions
        if len(points) < ACCEL_MIN_POINTS:
            return None
        path = sum(math.sqrt(sum((a - b) ** 2 for a, b in zip(p, q))) for p, q in zip(points, list(points)[1:]))
        if path < GRADE_PATH:
            return None
        return max(-1.0, min(1.0, (points[-1][1] - points[0][1]) / path))
