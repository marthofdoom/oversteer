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
MIN_A = 0.05                             # share of the strength hint: a fit with a lower Mz weight says nothing


class Store:
    """{car: (trail, scrub)} in a JSON file in the data folder; read once, written atomically."""

    def __init__(self):
        self.path = None                 # None: the data folder's file
        self._cars = None
        self._lock = threading.Lock()
        self._saved = {}                 # what the file holds, to tell what changed
        self._last_save = 0.0

    def _file(self):
        return self.path or os.path.join(diagnostics.data_home(), 'oversteer', FILE)

    def _load(self):
        if self._cars is None:
            self._cars = {}
            try:
                with open(self._file()) as f:
                    for car, v in json.load(f).items():
                        self._cars[car] = (float(v['trail']), float(v['scrub']))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                pass
            self._saved = dict(self._cars)

    def use(self, path):
        """A file of its own (tests); forgets what was read."""
        with self._lock:
            self.path, self._cars, self._saved, self._last_save = path, None, {}, 0.0

    def get(self, car):
        with self._lock:
            self._load()
            return self._cars.get(car)

    def put(self, car, trail, scrub, force=False):
        """The car's learnt values; written when they moved enough and SAVE_EVERY s have passed (or `force`)."""
        if car is None:
            return
        with self._lock:
            self._load()
            self._cars[car] = (trail, scrub)
            old = self._saved.get(car)
            moved = old is None or abs(old[0] - trail) >= SAVE_STEP[0] or abs(old[1] - scrub) >= SAVE_STEP[1]
            now = time.monotonic()
            if not moved or (not force and now - self._last_save < SAVE_EVERY):
                return
            self._last_save = now
            self._saved[car] = (trail, scrub)
            data = {c: {'trail': round(v[0], 5), 'scrub': round(v[1], 5)} for c, v in self._cars.items()}
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
        # normal equations in the features' own units: a ridge on b and c only, toward -a t0 and -a s0
        lam = PRIOR_W * 0.3
        t0, s0 = self.prior
        m = [row[:] for row in self._s]
        rhs = self._v[:]
        for i, target in ((1, -a_prior * t0 * SIGMA[1]), (2, -a_prior * s0 * SIGMA[2])):
            m[i][i] += lam
            rhs[i] += lam * target
        theta = _solve3(m, rhs)
        if theta is None:
            return
        a = theta[0] / SIGMA[0]
        if not a > MIN_A * a_hint:
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
