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
TRAIL_SLEW = 0.001                       # m per second at most
SCRUB_SLEW = 0.0005
FY_MIN = 800.0                           # N: a sample with less front side force and ...
DFX_MIN = 300.0                          # N: ... less left-right difference of Fx says nothing about either
                                         # (a straight): it is not added and does not age the window, so the
                                         # learnt values hold through a long straight instead of eroding to the prior
# The steering model: rack (kingpin) torque -> column torque -> the driver's torque at the rim.
# Column torque T_c = front torque / STEER_RATIO (the overall ratio steering wheel : road wheel; a rally car's
# typical 12-15, 13 as the default; where a game gives lock and wheel rotation the ratio is their quotient).
# Power assist is a boost curve: the driver's torque T_d and the column torque are related by
#     T_c = T_d + A(T_d, v),   A = k(v) * T_d^2 / (T0 + T_d)          (T_d >= 0, odd in the sign)
# The assist share of the column torque grows with T_d (a hydraulic valve / EPS map is near-zero boost around the
# centre, then ramps up with the torsion-bar torque), so the driver feels a stiffness of about 1 / (1 + k) of the
# rack's: the usual "boost curve" shape of HPS / EPS (e.g. Bosch / ZF EPS characteristic maps, parametrised by
# driver torque and vehicle speed; here a smooth analytic form, assumed - no car's real map is known). k(v) falls
# with speed (heavy assist parked, light on the road). Parameters are fitted so that marth's ACR dump (front
# torque / 13: p50 6.5 Nm, p90 22 Nm, p99 43 Nm at speed) gives about 0.5-1 Nm at the rim for an easy corner and
# 2-3 Nm near the grip limit, the range a driver reports from a real power-steered car.
# The map T_d -> T_c is monotone, so it inverts exactly (a quadratic in T_d, solved in closed form).
STEER_RATIO = 13.0
BOOST_T0 = 0.5                           # Nm: the driver torque where the boost reaches half of its slope
BOOST_K_PARK = 40.0                      # boost slope k when parked ...
BOOST_K_ROAD = 15.0                      # ... falling to this at speed
BOOST_V0 = 8.0                           # m/s: the speed scale of that fall: k = ROAD + (PARK - ROAD) / (1 + (v/V0)^2)
P99_RANGE = (5.0, 4000.0)                # Nm: the front steering torque's p99 kept per car
SAVE_P99_STEP = 0.03                     # a p99 moved this share from what was saved is worth saving
MIN_A = 0.05                             # share of the strength hint: a fit with a lower Mz weight says nothing


class Store:
    """{car: (trail, scrub)} in a JSON file in the data folder; read once, written atomically. Per car it also keeps
    the learnt p99 of the front steering torque (Nm, `p99`, a diagnostic) and the steering ratio and power-assist
    class the rim torque is worked out with (`ratio`, `assist`: 1.0 = a road / rally car's power steering, 0.0 =
    unassisted; absent = the defaults below, set_rack() for a refined one)."""

    def __init__(self):
        self.path = None                 # None: the data folder's file
        self._cars = None
        self._p99 = {}
        self._rack = {}
        self._lock = threading.Lock()
        self._saved = {}                 # what the file holds, to tell what changed
        self._saved_p99 = {}
        self._last_save = 0.0

    def _file(self):
        return self.path or os.path.join(diagnostics.data_home(), 'oversteer', FILE)

    def _load(self):
        if self._cars is None:
            self._cars = {}
            self._p99, self._rack = {}, {}
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
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass
            self._saved = dict(self._cars)
            self._saved_p99 = dict(self._p99)

    def use(self, path):
        """A file of its own (tests); forgets what was read."""
        with self._lock:
            self.path, self._cars, self._saved, self._last_save = path, None, {}, 0.0
            self._saved_p99 = {}

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
            self._rack[car] = (float(ratio), float(assist))

    def put(self, car, trail, scrub, force=False, p99=None):
        """The car's learnt values; written when they moved enough and SAVE_EVERY s have passed (or `force`)."""
        if car is None:
            return
        with self._lock:
            self._load()
            self._cars[car] = (trail, scrub)
            old = self._saved.get(car)
            moved = old is None or abs(old[0] - trail) >= SAVE_STEP[0] or abs(old[1] - scrub) >= SAVE_STEP[1]
            if p99 is not None:
                old99 = self._saved_p99.get(car)
                self._p99[car] = p99
                moved = moved or old99 is None or abs(old99 - p99) >= SAVE_P99_STEP * old99
            now = time.monotonic()
            if not moved or (not force and now - self._last_save < SAVE_EVERY):
                return
            self._last_save = now
            self._saved[car] = (trail, scrub)
            self._saved_p99 = dict(self._p99)
            data = {}
            for c, v in self._cars.items():
                data[c] = {'trail': round(v[0], 5), 'scrub': round(v[1], 5)}
                if c in self._p99:
                    data[c]['p99'] = round(self._p99[c], 1)
                if c in self._rack:
                    data[c]['ratio'], data[c]['assist'] = self._rack[c]
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


def boost_k(speed, assist=1.0):
    """The boost curve's slope k at `speed` (m/s) for an assist class (1.0 road / rally, 0.0 unassisted)."""
    v = abs(speed) if speed is not None and math.isfinite(speed) else 0.0
    return assist * (BOOST_K_ROAD + (BOOST_K_PARK - BOOST_K_ROAD) / (1.0 + (v / BOOST_V0) ** 2))


def column_from_driver(driver, speed, assist=1.0):
    """The column torque (signed, Nm) that a driver torque `driver` balances: T_d + k T_d^2 / (T0 + |T_d|)."""
    mag = abs(driver)
    return math.copysign(mag + boost_k(speed, assist) * mag * mag / (BOOST_T0 + mag), driver)


def driver_from_column(column, speed, assist=1.0):
    """The driver's torque at the rim (signed, Nm) for a column torque, the exact inverse of column_from_driver:
    (1 + k) T_d^2 + (T0 - T_c) T_d - T_c T0 = 0, the positive root. Unassisted (k = 0): the column torque."""
    mag = abs(column)
    k = boost_k(speed, assist)
    b = mag - BOOST_T0
    d = (b + math.sqrt(b * b + 4.0 * (1.0 + k) * mag * BOOST_T0)) / (2.0 * (1.0 + k))
    return math.copysign(d, column)


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
    ratio over the load range; ACR's dump: the gain at 0.75-1.5 x p99 is 0.83-0.98 of the gain at 0.05-0.25 x p99
    on every car, speed or not). A boost curve is compressive: the gain falls with load (a typical car: to about
    0.07 at its p99, but the curve flattens to 1 / (1 + k) past a few Nm, so the full-range ratio is only about
    0.5-0.7). Torque is binned by its share of the car's p99 (EDGES); `ratio()` is the gain over the top two
    bins (>= 0.5 p99) over the gain in the bottom one (0.05-0.15 p99). It needs MIN_W seconds in each group before it
    says anything. `state`: None undecided (taken as absent), False / True; it flips only after the other side's
    threshold has held DWELL s (hysteresis: between ABSENT_ABOVE and ASSIST_BELOW it keeps what it had)."""
    EDGES = (0.05, 0.15, 0.3, 0.5, 0.75, 1.5)        # of the car's p99
    TAU = 300.0                                      # s of moving samples the sums remember
    MIN_W = 10.0                                     # s of weight in the low and in the high group
    ASSIST_BELOW = 0.7
    ABSENT_ABOVE = 0.8
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
        """Gain at high load over gain at low load, None until both have enough."""
        lo, hi = self._gain((0,)), self._gain((3, 4))
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
            return ('auto', 1.0)
    try:
        st = os.stat(path)
        stamp = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = (path, None, None)
    if _ini['stamp'] == stamp:
        return _ini['value']
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
    return _ini['value']
