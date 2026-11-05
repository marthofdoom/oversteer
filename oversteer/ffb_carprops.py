"""What Countersteer FFB learns about each car's steering from the game's own force: the caster (mechanical)
trail and the scrub radius, per car, kept between runs.

The game's constant force is fitted live, per car, as
    raw ~ a * Mz + b * Fy + c * (Fx_FL - Fx_FR)        (front axle sums, game units)
and the car's trail is -b / a and its scrub radius -c / a (ffb_engine's constants, CASTER_TRAIL and SCRUB, say why
the signs). marth's ACR dump has two kinds of car, trail 6-10 mm and 32-46 mm, so one pooled number is wrong
for both. The fit is a ridge regression over TAU seconds of unclipped moving samples, with the pooled values
(or what the car showed last time) as the prior of the b and c terms, so a car starts physical and converges as
it is driven; the learnt trail and scrub are bounded to physical ranges and slew-limited, so the force never
jumps. Nothing here is on the force's timing: add() is a few multiplies per sample and the 3x3 solve runs
every SOLVE_EVERY seconds of sample time. The file is written from a thread of its own."""
import configparser
import json
import logging
import math
import os
import tempfile
import threading
import time

from . import diagnostics

FILE = 'ffb_car_props.json'
ASYNC = True                             # the file is written from a thread of its own (tests turn it off)
SAVE_EVERY = 20.0                        # s: at most this often per run
SAVE_STEP = (0.0005, 0.0005)             # m: a trail or scrub moved this far from what was saved is worth saving

TAU = 30.0                               # s: the window the fit is over
PRIOR_W = 300.0                          # the prior's weight, in samples (about 1 s at 333 Hz)
SIGMA = (100.0, 3000.0, 600.0)           # Nm, N, N: the features' usual size (the fit is made in these units)
SOLVE_EVERY = 0.25                       # s of sample time
TRAIL_RANGE = (0.0, 0.060)               # m
SCRUB_RANGE = (0.0, 0.040)
TRAIL_DEFAULT, SCRUB_DEFAULT = 0.027, 0.0155    # m: ffb_engine.CASTER_TRAIL, SCRUB (a car stored for its mass alone)
MASS_RANGE = (700.0, 2600.0)             # kg: a learnt mass (ffb_forza.MassLearner) is kept in this
MASS_SAVE_STEP = 10.0                    # kg: a mass moved this far from what was saved is worth saving
TRAIL_SLEW = 0.001                       # m per second at most
SCRUB_SLEW = 0.0005
FY_MIN = 800.0                           # N: a sample with less front side force and ...
DFX_MIN = 300.0                          # N: ... less left-right difference of Fx says nothing about either
                                         # (a straight): it is not added and does not age the window, so the
                                         # learnt values hold through a long straight instead of eroding to the prior
# The steering model: rack (kingpin) torque -> column torque -> the driver's torque at the rim.
# Column torque T_c = front torque / STEER_RATIO (the overall ratio steering wheel : road wheel; a rally car's
# typical 12-15, 13 as the default; where telemetry gives the road-wheel angle the ratio is learnt, RatioLearner).
# Power assist is a boost curve: the driver's torque T_d and the column torque are related by
#     T_c = T_d + A(T_d, v),   A = softmin(raw, ASSIST_SAT),   raw = Kc T_d + (K(v) - Kc) T_d^2 / (T0(v) + T_d)
# (T_d >= 0, odd in the sign). The boost gain is Kc right at the centre and rises to K(v) with the torsion-bar
# torque over a centre zone of T0(v) Nm; K(v) and T0(v) fall / rise with speed; the assist saturates (a motor or a
# relief valve has a limit), past which the driver feels the rest. The sources behind the figures are the
# verification report (ffb-verify-dyn/report.md, items 7 and 9): real steering at the rim has an on-centre
# gradient of about 0.2 N.m per degree of steering wheel (20 N.m/g; Chugh et al. 2024, Gröndahl 2018, Niu 2021,
# Balachandran & Gerdes 2014, Li et al. AMEE 2017), 2.4-2.8 N.m at 0.1 g and about 3.6 N.m at 0.3 g, and falls
# past about 0.65 g (the tyres', not the boost's); EPS / hydraulic maps (UN R79; patents US 6282472, 5458153,
# 7136732, 6382345) have a dead band of 0.3-1.0 N.m about the centre, a highway boost gain of 3-6 and a
# saturating assist (30 N.m in the report's curve set). Kc: the ACR dump's tyre torque gives 0.589 N.m per
# degree of wheel on centre (K0 5700 N.m/rad over ratio^2), so Kc = 0.589 / 0.2 - 1 = 2 brings it to the
# measured 0.2. K(v) = 6 at speed (the top of 3-6; the first model's 15-17 was too heavy), T0 0.3 N.m parked
# to 1.0 N.m at speed (the dead band's range); the park gain 40 is the first model's, unchanged (the app plays nothing
# below MIN_SPEED). ASSIST_SAT is assumed: set past the dump's usual loads so that it only
# bites beyond a car's p99 and the p90-p99 range keeps its shape (the report: the old knee squashed it into 5.5 % FS).
# The sim's tyre torque at 0.1 g is weaker than the measured rim torque (which includes the column's own friction),
# so the absolute level is not matched here: the uniform impedance scale (wheel_torque.impedance_scale) sets it
# from the car's own p99.
# The map T_d -> T_c is monotone, so it inverts (by bisection: the softmin has no closed form).
STEER_RATIO = 13.0
BOOST_K_CENTRE = 2.0                     # boost gain at the centre: T_d / T_c = 1 / (1 + 2) there
BOOST_K_PARK = 40.0                      # boost gain when parked ...
BOOST_K_ROAD = 6.0                       # ... falling to this at speed: K = ROAD + (PARK - ROAD) / (1 + (v/V0)^2)
BOOST_T0_PARK = 0.3                      # Nm: the centre zone's width (driver torque where the gain is half way
BOOST_T0_ROAD = 1.0                      # from Kc to K) parked, widening to this at speed
BOOST_V0 = 8.0                           # m/s: the speed scale of that fall
ASSIST_SAT = 100.0                       # Nm at the column: the assist's limit (softmin of order SAT_ORDER)
SAT_ORDER = 4
P99_RANGE = (5.0, 4000.0)                # Nm: the front steering torque's p99 kept per car
SAVE_P99_STEP = 0.03                     # a p99 moved this share from what was saved is worth saving
FRAME_SLOPE_RANGE = (0.3, 2.0)           # the wheel's position per expected degree kept per car (ffb_dynamics.FrameCheck.SLOPE)
LOCK_RANGE = (0.3, 0.8)                  # rad: the road wheels' steering lock per unit of steer, kept in this
MIN_A = 0.05                             # share of the strength hint: a fit with a lower Mz weight says nothing


class Store:
    """{car: (trail, scrub)} in a JSON file in the data folder; read once, written atomically. Per car it also keeps
    the learnt p99 of the front steering torque (Nm, `p99`, a diagnostic) and the steering ratio and power-assist
    class the rim torque is worked out with (`ratio`, `assist`: 1.0 = a road / rally car's power steering, 0.0 =
    unassisted; absent = the defaults below, set_rack() for a refined one) and the road wheels' steering lock (`lock`,
    rad, centre to full lock of the game's -1..1 steer; RatioLearner), and the verified wheel mapping (`frame`:
    {"range|sensitivity": slope}, the wheel's position per degree of the game's steering: it depends on the wheel's
    range and sensitivity as well as the car; ffb_dynamics.FrameCheck)."""

    def __init__(self):
        self.path = None                 # None: the data folder's file
        self._cars = None
        self._p99 = {}
        self._rack = {}
        self._steer_lock = {}
        self._frame = {}
        self._mass = {}
        self._dirty = False
        self._lock = threading.Lock()
        self._saved = {}                 # what the file holds, to tell what changed
        self._saved_p99 = {}
        self._last_save = 0.0

    def _file(self):
        return self.path or os.path.join(diagnostics.data_home(), 'oversteer', FILE)

    def _load(self):
        if self._cars is None:
            self._cars = {}
            self._p99, self._rack, self._steer_lock, self._frame, self._mass = {}, {}, {}, {}, {}
            try:
                with open(self._file()) as f:
                    for car, v in json.load(f).items():
                        trail, scrub = float(v['trail']), float(v['scrub'])
                        if not (math.isfinite(trail) and math.isfinite(scrub)):
                            continue         # a damaged file must not put NaN or inf into the force
                        self._cars[car] = (min(max(trail, TRAIL_RANGE[0]), TRAIL_RANGE[1]),
                                           min(max(scrub, SCRUB_RANGE[0]), SCRUB_RANGE[1]))
                        p99 = v.get('p99')
                        if isinstance(p99, (int, float)) and math.isfinite(p99) and P99_RANGE[0] <= p99 <= P99_RANGE[1]:
                            self._p99[car] = float(p99)
                        ratio, assist = v.get('ratio'), v.get('assist')
                        if isinstance(ratio, (int, float)) and isinstance(assist, (int, float)) \
                                and 3.0 <= ratio <= 40.0 and 0.0 <= assist <= 2.0:
                            self._rack[car] = (float(ratio), float(assist))
                        mass = v.get('mass')
                        if isinstance(mass, (int, float)) and math.isfinite(mass) and MASS_RANGE[0] <= mass <= MASS_RANGE[1]:
                            self._mass[car] = float(mass)
                        lock = v.get('lock')
                        if isinstance(lock, (int, float)) and math.isfinite(lock) and LOCK_RANGE[0] <= lock <= LOCK_RANGE[1]:
                            self._steer_lock[car] = float(lock)
                        frames = v.get('frame')
                        if isinstance(frames, dict):
                            for key, slope in frames.items():
                                if isinstance(key, str) and isinstance(slope, (int, float)) and math.isfinite(slope) \
                                        and FRAME_SLOPE_RANGE[0] <= slope <= FRAME_SLOPE_RANGE[1]:
                                    self._frame.setdefault(car, {})[key] = float(slope)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass
            self._saved = dict(self._cars)
            self._saved_p99 = dict(self._p99)

    def use(self, path):
        """A file of its own (tests); forgets what was read."""
        with self._lock:
            self.path, self._cars, self._saved, self._last_save = path, None, {}, 0.0
            self._saved_p99 = {}
            self._dirty = False

    def get(self, car):
        with self._lock:
            self._load()
            return self._cars.get(car)

    def get_p99(self, car):
        """The car's learnt p99 of |front steering torque| (Nm), None if not learnt."""
        with self._lock:
            self._load()
            return self._p99.get(car)

    def get_rack(self, car):
        """(steering ratio, assist class) kept for the car, None for the defaults."""
        with self._lock:
            self._load()
            return self._rack.get(car)

    def set_rack(self, car, ratio, assist):
        with self._lock:
            self._load()
            if self._rack.get(car) != (float(ratio), float(assist)):
                self._dirty = True
            self._rack[car] = (float(ratio), float(assist))

    def get_mass(self, car):
        """The car's learnt mass (kg, ForzaRack's MassLearner), None if not learnt."""
        with self._lock:
            self._load()
            return self._mass.get(car)

    def set_mass(self, car, mass):
        """Keeps a learnt mass; written (with the car's other values) when it moved MASS_SAVE_STEP kg from the stored."""
        if car is None or not (math.isfinite(mass) and MASS_RANGE[0] <= mass <= MASS_RANGE[1]):
            return
        with self._lock:
            self._load()
            old = self._mass.get(car)
            moved = old is None or abs(old - mass) >= MASS_SAVE_STEP
            if moved:
                self._dirty = True
                self._mass[car] = float(mass)
            trail, scrub = self._cars.get(car) or (TRAIL_DEFAULT, SCRUB_DEFAULT)
            pending = self._dirty
        if pending:                      # (put() throttles to SAVE_EVERY: the next call after it flushes)
            self.put(car, trail, scrub)

    def get_lock(self, car):
        """The road wheels' steering lock kept for the car (rad per unit of steer), None if not learnt."""
        with self._lock:
            self._load()
            return self._steer_lock.get(car)

    def set_lock(self, car, lock):
        with self._lock:
            self._load()
            old = self._steer_lock.get(car)
            if old is None or abs(old - lock) > 0.02 * old:
                self._dirty = True
            self._steer_lock[car] = float(lock)

    @staticmethod
    def _frame_key(wheel_range, sensitivity):
        return "%g|%s" % (wheel_range, sensitivity)

    def get_frame(self, car, wheel_range, sensitivity):
        """The verified wheel mapping (slope) kept for the car under this wheel range and sensitivity, None if none
        (or one outside FRAME_SLOPE_RANGE)."""
        if car is None or not wheel_range:
            return None
        with self._lock:
            self._load()
            slope = self._frame.get(car, {}).get(self._frame_key(wheel_range, sensitivity))
        return slope if slope is not None and FRAME_SLOPE_RANGE[0] <= slope <= FRAME_SLOPE_RANGE[1] else None

    def set_frame(self, car, wheel_range, sensitivity, slope):
        """Keeps a verified slope (written with the car's next put); None drops it. Out-of-range slopes are not kept."""
        if car is None or not wheel_range:
            return
        key = self._frame_key(wheel_range, sensitivity)
        with self._lock:
            self._load()
            frames = self._frame.setdefault(car, {})
            if slope is None:
                if frames.pop(key, None) is not None:
                    self._dirty = True
            elif FRAME_SLOPE_RANGE[0] <= slope <= FRAME_SLOPE_RANGE[1]:
                old = frames.get(key)
                if old is None or abs(old - slope) > 0.02 * old:
                    self._dirty = True
                frames[key] = float(slope)

    def put(self, car, trail, scrub, force=False, p99=None):
        """The car's learnt values; written when they moved enough and SAVE_EVERY s have passed (or `force`)."""
        if car is None:
            return
        with self._lock:
            self._load()
            self._cars[car] = (trail, scrub)
            old = self._saved.get(car)
            moved = old is None or abs(old[0] - trail) >= SAVE_STEP[0] or abs(old[1] - scrub) >= SAVE_STEP[1] or self._dirty
            if p99 is not None:
                old99 = self._saved_p99.get(car)
                self._p99[car] = p99
                moved = moved or old99 is None or abs(old99 - p99) >= SAVE_P99_STEP * old99
            now = time.monotonic()
            if not moved or (not force and now - self._last_save < SAVE_EVERY):
                return
            self._last_save = now
            self._dirty = False
            self._saved[car] = (trail, scrub)
            self._saved_p99 = dict(self._p99)
            data = {}
            for c, v in self._cars.items():
                data[c] = {'trail': round(v[0], 5), 'scrub': round(v[1], 5)}
                if c in self._p99:
                    data[c]['p99'] = round(self._p99[c], 1)
                if c in self._rack:
                    data[c]['ratio'], data[c]['assist'] = self._rack[c]
                if c in self._mass:
                    data[c]['mass'] = round(self._mass[c], 1)
                if c in self._steer_lock:
                    data[c]['lock'] = round(self._steer_lock[c], 4)
                if self._frame.get(c):
                    data[c]['frame'] = {k: round(x, 4) for k, x in self._frame[c].items()}
        if ASYNC:
            threading.Thread(target=self._write, args=(data,), daemon=True).start()
        else:
            self._write(data)

    def _write(self, data):
        path = self._file()
        tmp = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=FILE + '.', suffix='.tmp', dir=os.path.dirname(path))
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, sort_keys=True)
            os.replace(tmp, path)
            tmp = None
        except OSError as e:
            logging.debug("Countersteer FFB: car properties not saved: %s", e)
        finally:
            if tmp is not None:
                try:
                    os.remove(tmp)
                except OSError:
                    pass


STORE = Store()


def _speed(speed):
    return abs(speed) if speed is not None and math.isfinite(speed) else 0.0


def boost_k(speed, assist=1.0):
    """The boost curve's gain K at `speed` (m/s) for an assist class (1.0 road / rally, 0.0 unassisted)."""
    f = 1.0 / (1.0 + (_speed(speed) / BOOST_V0) ** 2)
    return assist * (BOOST_K_ROAD + (BOOST_K_PARK - BOOST_K_ROAD) * f)


def boost_t0(speed):
    """The centre zone's width (Nm of driver torque) at `speed`."""
    f = 1.0 / (1.0 + (_speed(speed) / BOOST_V0) ** 2)
    return BOOST_T0_ROAD + (BOOST_T0_PARK - BOOST_T0_ROAD) * f


def _assist_raw(mag, speed, assist):
    """(A_raw, dA_raw/dT_d) before the saturation, at driver torque `mag` >= 0."""
    kc, k, t0 = BOOST_K_CENTRE * assist, boost_k(speed, assist), boost_t0(speed)
    raw = kc * mag + (k - kc) * mag * mag / (t0 + mag)
    slope = kc + (k - kc) * mag * (mag + 2.0 * t0) / (t0 + mag) ** 2
    return raw, slope


def _assist(mag, speed, assist):
    """(A, dA/dT_d) at driver torque `mag` >= 0: the saturated assist torque and its slope."""
    raw, slope = _assist_raw(mag, speed, assist)
    q = 1.0 + (raw / ASSIST_SAT) ** SAT_ORDER
    return raw * q ** (-1.0 / SAT_ORDER), slope * q ** (-1.0 / SAT_ORDER - 1.0)


# The assist class: (HYD_K, HYD_R) of the hydraulic top end. 'rally' (the default) keeps the road curve's low and mid
# range and runs to 7.0 Nm (marth's WRC research: cruise about 0.5 Nm, corners 2-3 Nm, a held maximum 6-8 Nm, no damping
# or isolation filters); 'street' is the commuter's 3.7 Nm top; 'off' is unassisted (the column torque). INI only.
POWER_CLASSES = ('rally', 'street', 'off')
DEFAULT_POWER_CLASS = 'rally'
_CLASS_HYD = {'rally': (3.0, 4.0), 'street': (1.5, 0.5), 'off': (1.5, 2.2)}
# Street: full assist is reached early and the force stops building: risen within 15-25 degrees of wheel at speed (the owner's
# experience of road cars), a top of 2.0 Nm. The on-centre gain before the top end, so it gets there that soon.
_CLASS_GAIN = {'rally': 1.0, 'street': 2.0, 'off': 1.0}
HYD_GAIN = _CLASS_GAIN[DEFAULT_POWER_CLASS]
_power_class = DEFAULT_POWER_CLASS
HYD_K, HYD_R = _CLASS_HYD[DEFAULT_POWER_CLASS]   # Nm: the active class's top end: the driver's torque follows the boost curve 1:1
                                         # up to HYD_K, then bends toward HYD_K + HYD_R (the rack runs out of assist flow)


def set_power_class(name):
    """Make `name` (rally / street / off; anything else: rally) the active assist class; returns the class in effect."""
    global _power_class, HYD_K, HYD_R, HYD_GAIN
    _power_class = name if name in POWER_CLASSES else DEFAULT_POWER_CLASS
    HYD_K, HYD_R = _CLASS_HYD[_power_class]
    HYD_GAIN = _CLASS_GAIN[_power_class]
    return _power_class


def power_class():
    return _power_class


RALLY_TRAIL = 0.037                      # m: the rally prior for the caster trail: high positive caster, the wheel snaps back
                                         # fast (what ACR learnt for the Fabia); the street prior is ffb_engine.CASTER_TRAIL


def default_trail(street_trail):
    """The caster trail (m) used where none has been learnt: RALLY_TRAIL on the rally class, else `street_trail`."""
    return RALLY_TRAIL if _power_class == 'rally' else street_trail


def hyd_top():
    """The active class's asymptote of the driver's torque (Nm): HYD_K + HYD_R."""
    return HYD_K + HYD_R


def _hyd(x):
    """The driver's torque after the hydraulic top end (signed): x up to HYD_K, then HYD_K + R o / (R + o) for the
    excess o over HYD_K, tending to HYD_K + HYD_R."""
    mag = abs(x) * HYD_GAIN
    if mag <= HYD_K:
        return x * HYD_GAIN
    over = mag - HYD_K
    return math.copysign(HYD_K + HYD_R * over / (HYD_R + over), x)


def _hyd_inverse(y):
    """The torque that _hyd maps to y (signed); y' = |y| - HYD_K is held just under HYD_R (the curve's asymptote)."""
    mag = abs(y)
    if mag <= HYD_K:
        return y / HYD_GAIN
    over = min(mag - HYD_K, HYD_R * (1.0 - 1e-6))
    return math.copysign((HYD_K + HYD_R * over / (HYD_R - over)) / HYD_GAIN, y)


def _hyd_slope(x):
    """d _hyd / dx at x: 1 up to HYD_K, (R / (R + x - HYD_K))^2 above."""
    over = abs(x) * HYD_GAIN - HYD_K
    return HYD_GAIN if over <= 0.0 else HYD_GAIN * (HYD_R / (HYD_R + over)) ** 2


def column_from_driver(driver, speed, assist=1.0):
    """The column torque (signed, Nm) that a driver torque `driver` balances: T_d + A(T_d, v). With assist the
    hydraulic top end (_hyd) is undone first."""
    if assist > 0.0:
        driver = _hyd_inverse(driver)
    mag = abs(driver)
    return math.copysign(mag + _assist(mag, speed, assist)[0], driver)


def _driver_boost(column, speed, assist):
    """The driver's torque for a column torque on the boost curve alone, before the hydraulic top end."""
    mag = abs(column)
    lo, hi = 0.0, mag
    for _ in range(48):
        mid = 0.5 * (lo + hi)
        if mid + _assist(mid, speed, assist)[0] < mag:
            lo = mid
        else:
            hi = mid
    return math.copysign(0.5 * (lo + hi), column)


def driver_from_column(column, speed, assist=1.0, hyd=True):
    """The driver's torque at the rim (signed, Nm) for a column torque: the inverse of column_from_driver (T_c is
    strictly increasing in T_d, and T_d <= T_c: bisection), then through the hydraulic top end (_hyd: it tends to
    HYD_K + HYD_R). Unassisted (assist 0): the column torque. `hyd` False skips the top end (the classic mapping)."""
    if assist <= 0.0 or abs(column) <= 0.0:
        return column
    boost = _driver_boost(column, speed, assist)
    return _hyd(boost) if hyd else boost


def driver_slope(column, speed, assist=1.0, hyd=True):
    """dT_d / dT_c at a column torque: how much of a change of the column torque the driver feels (1 / (1 + dA/dT_d)
    on the boost curve, times the hydraulic top end's slope unless `hyd` is False)."""
    if assist <= 0.0:
        return 1.0
    td = abs(_driver_boost(column, speed, assist))
    return (_hyd_slope(td) if hyd else 1.0) / (1.0 + _assist(td, speed, assist)[1])


# -- the steering ratio from telemetry --
RATIO_ARM = 0.13                         # m: the steering arm (rack travel to road-wheel angle) until a car's is learnt
RATIO_RANGE = (3.0, 40.0)
WHEELBASE = 2.6                          # m: ASSUMED (a rally / GT car's 2.5-2.7); telemetry has none. An error of x %
                                         # in it is an error of x % in the learnt lock and ratio
RATIO_MIN_SPEED = 8.0                    # m/s
RATIO_MAX_AY = 2.0                       # m/s^2: lateral acceleration under which the understeer gradient is negligible
RATIO_MAX_ALPHA = 0.03                   # rad: front slip angle under which the Ackermann angle holds (on gravel the low
                                         # lateral acceleration samples are understeer: the lock came out 0.04 rad)
RATIO_ROLL = 0.04                        # front and rear wheel speeds within this share of each other: free-rolling
RATIO_ALPHA_WIDE = 0.6                   # rad: front and rear slip angles under which the kinematic identity is used (it holds
                                         # at any slip, so no lateral-acceleration or small-slip gate: on gravel those leave 8 %)
RATIO_MIN_SXX = 0.1                      # unit^2 s of steer evidence before the lock is believed
RATIO_SLEW = 0.1                         # ratio units per second at most


def table_ratio(entry, arm=RATIO_ARM):
    """The overall steering ratio of a car from its table entry (Richard Burns Rally: steering_wheel_lock_deg,
    steering wheel centre to full lock, and rack_travel_m, the rack's travel from centre to lock): the wheel's
    lock over the road wheel's, asin(travel / arm) for a steering arm of `arm` m. None without both."""
    if not entry:
        return None
    wheel, travel = entry.get('steering_wheel_lock_deg'), entry.get('rack_travel_m')
    if not (isinstance(wheel, (int, float)) and isinstance(travel, (int, float))) or wheel <= 0.0 or not 0.0 < travel < arm:
        return None
    ratio = wheel / math.degrees(math.asin(travel / arm))
    return min(max(ratio, RATIO_RANGE[0]), RATIO_RANGE[1])


class RatioLearner:
    """The road wheels' steering lock, and with it the overall steering ratio, from telemetry. At low lateral
    acceleration the road-wheel angle is the Ackermann one, delta = wheelbase * yaw rate / speed (positive left,
    like steer); regressed on the game's steer (-1..1) through the origin it gives the lock in rad per unit of
    steer. Where the steering wheel's own lock is known (a car table's steering_wheel_lock_deg) the ratio is
    their quotient. With the rear slip angle too, the exact identity delta = alpha_f - alpha_r + wheelbase * yaw
    rate / speed holds at any slip, so a gravel drive (nearly all of it sliding) teaches the lock as well. A few
    multiplies per sample."""
    TAU = 120.0                                      # s of moving samples the sums remember

    def __init__(self):
        self.sxx = self.sxy = 0.0

    def add(self, steer, yaw, speed, dt, alpha=None, wheels=None, alpha_r=None):
        """`alpha`: the mean front slip angle (rad) and `wheels`: the four wheels' tread speeds, when the game gives
        them; samples with a front slip angle of RATIO_MAX_ALPHA or more, or driven or braked wheels (front and
        rear speeds apart by RATIO_ROLL), are left out. `alpha_r`: the mean rear slip angle; with both slips the
        kinematic identity is used instead and only RATIO_MIN_SPEED and RATIO_ALPHA_WIDE gate the sample."""
        if alpha is not None and alpha_r is not None:
            if not (dt > 0.0 and speed is not None and speed >= RATIO_MIN_SPEED and steer is not None and yaw is not None
                    and math.isfinite(steer) and math.isfinite(yaw) and math.isfinite(speed)
                    and math.isfinite(alpha) and math.isfinite(alpha_r)
                    and abs(alpha) < RATIO_ALPHA_WIDE and abs(alpha_r) < RATIO_ALPHA_WIDE):
                return
            delta = alpha - alpha_r + WHEELBASE * yaw / speed
            decay = math.exp(-dt / self.TAU)
            self.sxx = self.sxx * decay + steer * steer * dt
            self.sxy = self.sxy * decay + steer * delta * dt
            return
        if alpha is not None and not abs(alpha) < RATIO_MAX_ALPHA:
            return
        if wheels is not None and len(wheels) >= 4 and all(math.isfinite(w) for w in wheels):
            front, rear = abs(wheels[0] + wheels[1]) / 2.0, abs(wheels[2] + wheels[3]) / 2.0
            if abs(front - rear) > RATIO_ROLL * max(front, rear, 1e-6):
                return
        if not (dt > 0.0 and speed is not None and speed >= RATIO_MIN_SPEED and steer is not None and yaw is not None
                and math.isfinite(steer) and math.isfinite(yaw) and math.isfinite(speed)):
            return
        if abs(speed * yaw) > RATIO_MAX_AY:
            return
        decay = math.exp(-dt / self.TAU)
        self.sxx = self.sxx * decay + steer * steer * dt
        self.sxy = self.sxy * decay + steer * (WHEELBASE * yaw / speed) * dt

    def lock(self):
        """The road wheels' lock (rad per unit of steer), None until there is enough evidence."""
        if self.sxx < RATIO_MIN_SXX or self.sxy <= 0.0:
            return None
        lock = self.sxy / self.sxx
        return lock if LOCK_RANGE[0] <= lock <= LOCK_RANGE[1] else None

    def ratio(self, wheel_lock_deg):
        """The steering ratio for a steering wheel that turns `wheel_lock_deg` to full lock, None until learnt."""
        lock = self.lock()
        if lock is None or not wheel_lock_deg or wheel_lock_deg <= 0.0:
            return None
        return min(max(math.radians(wheel_lock_deg) / lock, RATIO_RANGE[0]), RATIO_RANGE[1])


class Quantile:
    """A running quantile of positive values over about TAU seconds (a time-weighted, exponentially forgetting
    histogram on log-spaced bins: 5 % wide, so the answer is good to a few per cent). add() is O(1): the weights
    are not decayed, new ones grow by exp(t / TAU) and everything is renormalised now and then. `seed` puts a prior
    (a car's last value, or the pooled one) in as `seconds` of weight with its P at `prior`."""
    LO, HI, N = 1.0, 4000.0, 160
    TAU = 300.0                          # s of driving

    def __init__(self, tau=None):
        self.tau = tau or self.TAU
        self.w = [0.0] * self.N
        self.boost = 1.0
        self._k = self.N / math.log(self.HI / self.LO)

    def _bin(self, x):
        if not x > self.LO:
            return 0
        return min(int(math.log(x / self.LO) * self._k), self.N - 1)

    def add(self, x, dt):
        if not (dt > 0.0 and math.isfinite(x)):
            return
        self.boost *= math.exp(dt / self.tau)
        self.w[self._bin(x)] += dt * self.boost
        if self.boost > 1e9:
            self.w = [v / self.boost for v in self.w]
            self.boost = 1.0

    def seed(self, prior, seconds, p=0.99):
        """`seconds` of weight shaped so that the p-quantile is `prior`."""
        low = self._bin(prior / 4.0)
        high = self._bin(prior)
        self.w[low] += seconds * (p - 0.005) * self.boost
        self.w[high] += seconds * (1.0 - p + 0.005) * self.boost

    def value(self, p=0.99):
        total = sum(self.w)
        if total <= 0.0:
            return None
        need, acc = p * total, 0.0
        for i, v in enumerate(self.w):
            acc += v
            if acc >= need:
                return self.LO * math.exp((i + 0.5) / self._k)
        return self.HI


class TorqueLearner:
    """The ridge fit of the game's force on (front Mz, front Fy, Fx_FL - Fx_FR); `trail` and `scrub` follow it."""

    def __init__(self, trail, scrub):
        self.prior = (trail, scrub)              # what the ridge pulls toward
        self.trail, self.scrub = trail, scrub
        self.fit_trail = self.fit_scrub = None   # the raw fit, before bounds and slew
        self.weight = 0.0
        self._s = [[0.0] * 3 for _ in range(3)]
        self._v = [0.0] * 3
        self._since = 0.0
        self._a = None                            # the Mz weight of the last fit (ext_force per Nm)

    def add(self, mz, fy, dfx, raw, dt, a_hint):
        """One unclipped moving sample (the front torque's three parts and the game's raw level); a_hint: the
        strength fitted on the torque (ext_force per Nm), which the prior's b and c scale with."""
        if abs(fy) < FY_MIN and abs(dfx) < DFX_MIN:
            return
        decay = math.exp(-dt / TAU)
        z = (mz / SIGMA[0], fy / SIGMA[1], dfx / SIGMA[2])
        s, v = self._s, self._v
        for i in range(3):
            for j in range(3):
                s[i][j] = s[i][j] * decay + z[i] * z[j]
            v[i] = v[i] * decay + z[i] * raw
        self.weight = self.weight * decay + 1.0
        self._since += dt                        # (informative time only)
        if self._since >= SOLVE_EVERY:
            self._solve(a_hint)
            self._slew(self._since)
            self._since = 0.0

    def _solve(self, a_hint):
        a_prior = self._a if self._a else a_hint
        # normal equations in the features' own units: a ridge on a, b and c toward one point (a, -a t0, -a s0); a ridge
        # on b and c alone drags a negative when Mz and Fy are collinear, every later solve is then rejected
        lam = PRIOR_W * 0.3
        t0, s0 = self.prior
        m = [row[:] for row in self._s]
        rhs = self._v[:]
        for i, target in ((0, a_prior * SIGMA[0]), (1, -a_prior * t0 * SIGMA[1]), (2, -a_prior * s0 * SIGMA[2])):
            m[i][i] += lam
            rhs[i] += lam * target
        theta = _solve3(m, rhs)
        a = None if theta is None else theta[0] / SIGMA[0]
        if a is None or not a > MIN_A * a_hint:
            self.fit_trail = self.fit_scrub = None       # nothing slews toward a stale fit
            return
        self._a = a
        self.fit_trail = -theta[1] / SIGMA[1] / a
        self.fit_scrub = -theta[2] / SIGMA[2] / a

    def _slew(self, dt):
        if self.fit_trail is None:
            return
        held = PRIOR_W * 0.3                     # the window must carry more than the prior does, else hold
        if self._s[1][1] >= held:
            want = min(max(self.fit_trail, TRAIL_RANGE[0]), TRAIL_RANGE[1])
            step = TRAIL_SLEW * dt
            self.trail = min(max(want, self.trail - step), self.trail + step)
        if self._s[2][2] >= held:
            want = min(max(self.fit_scrub, SCRUB_RANGE[0]), SCRUB_RANGE[1])
            step = SCRUB_SLEW * dt
            self.scrub = min(max(want, self.scrub - step), self.scrub + step)


def _solve3(m, b):
    """x with m x = b (3x3, partial pivoting), None if singular."""
    a = [m[i][:] + [b[i]] for i in range(3)]
    for c in range(3):
        p = max(range(c, 3), key=lambda r: abs(a[r][c]))
        if abs(a[p][c]) < 1e-12:
            return None
        a[c], a[p] = a[p], a[c]
        for r in range(c + 1, 3):
            f = a[r][c] / a[c][c]
            for k in range(c, 4):
                a[r][k] -= f * a[c][k]
    x = [0.0] * 3
    for i in (2, 1, 0):
        x[i] = (a[i][3] - sum(a[i][j] * x[j] for j in range(i + 1, 3))) / a[i][i]
    return x


# -- power steering: does the game already model it? And the INI settings --

class AssistDetector:
    """Whether the game's own steering force already includes power assist, from the game's force against the
    front steering torque of the same samples. Without assist the game's force is linear in the torque (a constant
    ratio over the load range). A boost curve bends the gain down where it ramps up: past the centre zone, at
    low and mid loads, before the tyres' own fall-off at the limit (which makes the full-range ratio of an
    unassisted car fall, and a saturating assist's rise) can confuse it. Torque is binned by its share of the car's
    p99 (EDGES); `ratio()` is the gain over 0.15-0.5 of p99 (bins 1 and 2) over the gain at 0.05-0.15 (bin 0). The
    verification report's curve set (ffb-verify-dyn/app_model.py, mid/low): unassisted 1.0, with tyre fall-off 0.98,
    our own torque model 15 % off 0.97, a constant-ratio assist 1.0; every assist 0.51-0.81 (a straight boost with a
    0.3 Nm dead band 0.81, with 1.0 Nm 0.61, a saturating EPS 0.51, a hydraulic valve 0.60, with relief 0.60). It
    needs MIN_W seconds in each group before it says anything. `state`: None undecided (taken as absent), False /
    True; it flips only after the other side's threshold has held DWELL s (hysteresis: between ASSIST_BELOW and
    ABSENT_ABOVE it keeps what it had)."""
    EDGES = (0.05, 0.15, 0.3, 0.5, 0.75, 1.5)        # of the car's p99
    TAU = 300.0                                      # s of moving samples the sums remember
    MIN_W = 10.0                                     # s of weight in the low and in the mid group
    ASSIST_BELOW = 0.88
    ABSENT_ABOVE = 0.94
    DWELL = 3.0

    def __init__(self):
        self.clear()

    def clear(self):
        n = len(self.EDGES) - 1
        self.w, self.t, self.r = [0.0] * n, [0.0] * n, [0.0] * n
        self.state = None
        self._since = 0.0

    def _bin(self, x):
        for i in range(len(self.EDGES) - 1):
            if x < self.EDGES[i + 1]:
                return i if x >= self.EDGES[i] else None
        return None

    def add(self, torque, raw, p99, dt):
        """A moving, unclipped sample: front torque (Nm), the game's force level, the car's p99 (Nm), seconds."""
        if not (dt > 0.0 and p99 > 0.0 and math.isfinite(torque) and math.isfinite(raw)):
            return
        decay = math.exp(-dt / self.TAU)
        for i in range(len(self.w)):
            self.w[i] *= decay
            self.t[i] *= decay
            self.r[i] *= decay
        b = self._bin(abs(torque) / p99)
        if b is not None:
            self.w[b] += dt
            self.t[b] += dt * abs(torque)
            self.r[b] += dt * abs(raw)
        self._judge(dt)

    def _gain(self, bins):
        w = sum(self.w[i] for i in bins)
        t = sum(self.t[i] for i in bins)
        return None if w < self.MIN_W or t <= 0.0 else sum(self.r[i] for i in bins) / t

    def ratio(self):
        """Gain at mid load over gain at low load, None until both have enough."""
        lo, hi = self._gain((0,)), self._gain((1, 2))
        return None if lo is None or hi is None or lo <= 0.0 else hi / lo

    def _judge(self, dt):
        ratio = self.ratio()
        if ratio is None:
            return
        want = True if ratio < self.ASSIST_BELOW else False if ratio > self.ABSENT_ABOVE else self.state
        if want is None or want == self.state:
            self._since = 0.0
            return
        self._since += dt
        if self._since >= self.DWELL:
            self.state, self._since = want, 0.0

    def curve(self, p99):
        """The game's gain against load, [(torque Nm, gain relative to the lowest bins)], non-increasing; the
        bins with too little weight are left out. [] before there is any."""
        lo = self._gain((0,))
        if lo is None or lo <= 0.0:
            return []
        pts, last = [], 1.0
        for i in range(len(self.w)):
            if self.w[i] < self.MIN_W / 3.0 or self.t[i] <= 0.0:
                continue
            c = min(self.r[i] / self.t[i] / lo, last)
            last = c
            pts.append((p99 * math.sqrt(self.EDGES[i] * self.EDGES[i + 1]), max(c, 0.02)))
        return pts


def game_shape(torque, curve):
    """The game's gain at |torque| from `curve` (piecewise linear in log torque, flat beyond the ends)."""
    x = abs(torque)
    if not curve:
        return 1.0
    if x <= curve[0][0]:
        return curve[0][1]
    for (x0, c0), (x1, c1) in zip(curve, curve[1:]):
        if x <= x1:
            return c0 + (c1 - c0) * math.log(x / x0) / math.log(x1 / x0)
    return curve[-1][1]


CONFIG_PATH = None                       # None: the user's config.ini (tests set a file)
POWER_MODES = ('auto', 'on', 'off')
ASSIST_STRENGTH_RANGE = (0.0, 2.0)
_ini = {'stamp': None, 'value': ('auto', 1.0)}


def power_settings():
    """(countersteer_power_steering, countersteer_assist_strength) from config.ini's DEFAULT section: INI only, no
    GUI. 'auto' (default): our power-steering model where the game's force has no assist, the game's own curve
    where it has; 'on': always ours; 'off': never (the column torque is played). The strength multiplies the
    car class's assist (1.0 default, 0..2). A missing file or a bad value gives the default."""
    path = CONFIG_PATH
    if path is None:
        try:
            from xdg.BaseDirectory import xdg_config_home
            path = os.path.join(xdg_config_home, 'oversteer', 'config.ini')
        except ImportError:
            return _off_class(('auto', 1.0))
    try:
        st = os.stat(path)
        stamp = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = (path, None, None)
    if _ini['stamp'] == stamp:
        return _off_class(_ini['value'])
    mode, strength = 'auto', 1.0
    if stamp[1] is not None:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(path)
            section = parser['DEFAULT']
            m = section.get('countersteer_power_steering', 'auto').strip().lower()
            mode = m if m in POWER_MODES else 'auto'
            v = float(section.get('countersteer_assist_strength', '1.0'))
            if math.isfinite(v):
                strength = min(max(v, ASSIST_STRENGTH_RANGE[0]), ASSIST_STRENGTH_RANGE[1])
        except (configparser.Error, OSError, ValueError):
            pass
    _ini['stamp'], _ini['value'] = stamp, (mode, strength)
    return _off_class(_ini['value'])


def _off_class(value):
    """The INI's (mode, strength), with the power class 'off' (countersteer_power_class) forcing the mode to 'off'."""
    return ('off', value[1]) if _power_class == 'off' else value
