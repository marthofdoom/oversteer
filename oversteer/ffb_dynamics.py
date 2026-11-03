"""The new-lg4ff driver's virtual steering dynamics stage (README "ffb_dyn_damping, ffb_dyn_friction, ffb_dyn_wheel,
ffb_align, ffb_hands, ffb_dyn_live"), seen from the application: the unit conversions, the sysfs access and the
engine's controller for it. Every attribute is optional (an older driver has none, a newer one may have only some):
absent ones are skipped, never an error.

Units (the driver's): FS is full scale (an ext_force of 32767). Damping in uFS per deg/s, inertia in 1e-7 FS per
deg/s^2, friction and torques in 1e-4 FS, stiffness in uFS per deg, latency in us. The alignment target is in
-32767..32767 over the current range, 0 the centre, + toward increasing ABS_X (the opposite of ext_force's sign,
which pushes toward decreasing ABS_X, and of the engine's left-positive angles).

The stage plays only in replace mode: the engine keeps writing ext_force (even 0) while it is on, writes the
alignment spring at the telemetry rate (the driver fades a spring not written for 20 ms), and sets everything back to
0 when it stops playing and during a calibration.
"""
import logging
import math
import os

from . import wheel_torque

# ffb_dyn_wheel "damping inertia friction breakaway latency": the driver's defaults (a G29 as Countersteer measured it)
DEFAULT_WHEEL = (470, 670, 500, 1250, 5000)
LATENCY_FLOOR_US = 5000                  # the calibration's latency is never taken below the driver's default
# What the driver accepts; a calibrated value is clamped into it (a wheel with more friction than 8 % is refused by the
# driver, so it is written as 8 %: a lower figure keeps the stage passive)
DAMPING_RANGE = (100, 2000)
INERTIA_RANGE = (100, 5000)
FRICTION_RANGE = (0, 800)
BREAKAWAY_MAX = 3000
LATENCY_RANGE = (2000, 20000)
COLUMN_DAMPING_MAX = 10000               # ffb_dyn_damping
COLUMN_FRICTION_MAX = 2000               # ffb_dyn_friction
ALIGN_K_MAX = 60000                      # ffb_align k, uFS per degree
ALIGN_TAU_MAX = 8000                     # ffb_align tau_max, 1e-4 FS (80 %)
ALIGN_TARGET_MAX = 32767

# A real car's steering column at the rim (the verification report, item 7: damping 0.14-0.2 N m s/rad; Coulomb
# friction 0.9-1.6 N m, drivers prefer 0.5-1): the middle of what the wheel can carry, scaled by the car's
# impedance scale s and the wheel's peak torque like the rest of the steering
COLUMN_DAMPING_NMS_RAD = 0.17
COLUMN_COULOMB_NM = 0.75

K_LIMIT_EVERY = 1.0                      # s between reads of ffb_align's k_limit and the wheel's range
COLUMN_MIN_STEP = 0.02                   # share a column target must change by before it is written again
ALIGN_MIN_GAP = 0.002                    # s between ffb_align writes (the driver's tick)
ALIGN_KEEP = 0.012                       # s: the spring is written again after this long without a sample (the driver
                                         # fades it after 20 ms)


def _clamp(value, low, high):
    return max(low, min(high, value))


def _num(result, key):
    value = result.get(key) if isinstance(result, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def wheel_values(result):
    """The five ffb_dyn_wheel integers (damping, inertia, friction, breakaway, latency) of a stored wheel calibration
    result, in the driver's units and ranges; None when the result lacks the figures (never calibrated, an old
    store). Inertia J = 1 / a_fs (FS per deg/s^2) with a_fs the acceleration at full scale; damping, friction and
    breakaway are shares of full scale (damping per deg/s). The latency is the fitted one, never below the driver's
    default (a loop delay measured elsewhere is not read)."""
    a_fs, damping = _num(result, 'a_fs'), _num(result, 'damping')
    friction, breakaway = _num(result, 'friction'), _num(result, 'breakaway')
    if not (a_fs and a_fs > 0.0 and damping and damping > 0.0 and friction is not None and friction >= 0.0
            and breakaway is not None and breakaway > 0.0):
        return None
    fit = _num(result, 'fit_latency_ms')
    latency = LATENCY_FLOOR_US if fit is None else max(LATENCY_FLOOR_US, fit * 1000.0)
    b = _clamp(int(round(damping * 1e6)), *DAMPING_RANGE)
    j = _clamp(int(round(1e7 / a_fs)), *INERTIA_RANGE)
    f = _clamp(int(round(friction * 1e4)), *FRICTION_RANGE)
    br = _clamp(int(round(breakaway * 1e4)), f, BREAKAWAY_MAX)
    return b, j, f, br, _clamp(int(round(latency)), *LATENCY_RANGE)


def wheel_text(values):
    return ' '.join('%d' % v for v in values)


def column_targets(scale, peak_nm, own=DEFAULT_WHEEL, removal=False):
    """(ffb_dyn_damping, ffb_dyn_friction) for a car on a wheel: the car's column values at the rim times the
    impedance scale s, as shares of the wheel's peak torque (None: the 2.1 Nm class), in the driver's units. They are
    the column's TOTAL, the wheel's own included; below the wheel's own the driver would take friction out (the
    wheel lighter than the column: opt-in, `removal`), so without it neither goes under the wheel's own."""
    peak = peak_nm or wheel_torque.DEFAULT_PEAK_NM
    damping = COLUMN_DAMPING_NMS_RAD * math.pi / 180.0 * scale / peak * 1e6
    friction = COLUMN_COULOMB_NM * scale / peak * 1e4
    damping = max(damping, own[0])
    if not removal:
        friction = max(friction, own[2])
    return (int(_clamp(round(damping), 0, COLUMN_DAMPING_MAX)), int(_clamp(round(friction), 0, COLUMN_FRICTION_MAX)))


def align_target(theta_a_left_deg, range_deg):
    """ffb_align's target for an angle in degrees of the wheel's own position, positive to the left (the sign of
    ext_force): + toward increasing ABS_X is the other way, -32767..32767 over the range, clamped to it."""
    half = range_deg / 2.0
    theta = _clamp(theta_a_left_deg, -half, half)
    return int(round(-theta / half * ALIGN_TARGET_MAX))


def align_text(theta_a_left_deg, k_fs_per_deg, tau_max_fs, range_deg):
    """ffb_align's "target k tau_max" for the engine's spring (FS per degree, FS)."""
    k = int(_clamp(round(k_fs_per_deg * 1e6), 0, ALIGN_K_MAX))
    tau = int(_clamp(round(tau_max_fs * 1e4), 0, ALIGN_TAU_MAX))
    return '%d %d %d' % (align_target(theta_a_left_deg, range_deg), k, tau)


class Dynamics:
    """The attributes of one wheel (its sysfs directory). Each write opens the file: they are rare (the spring at the
    telemetry rate at most) and a failure is the caller's to handle (OSError)."""

    RW = ('ffb_dyn_wheel', 'ffb_dyn_damping', 'ffb_dyn_friction', 'ffb_align')
    RO = ('ffb_hands', 'ffb_dyn_live')

    def __init__(self, path):
        self.path = path

    def _file(self, name):
        return os.path.join(self.path, name)

    def has(self, name):
        mode = os.R_OK | os.W_OK if name in self.RW else os.R_OK
        return bool(self.path) and os.access(self._file(name), mode)

    def available(self):
        """The stage's column or spring can be driven."""
        return any(self.has(name) for name in ('ffb_dyn_damping', 'ffb_dyn_friction', 'ffb_align'))

    def write(self, name, text):
        with open(self._file(name), 'w') as f:
            f.write(str(text))

    def fields(self, name):
        """The whitespace separated words of an attribute, None when it is absent or unreadable."""
        if not self.path:
            return None
        try:
            with open(self._file(name)) as f:
                return f.readline().split()
        except OSError:
            return None

    def numbers(self, name, count):
        """The first `count` numbers of an attribute (floats), None when absent, short or not numbers."""
        words = self.fields(name)
        if words is None or len(words) < count:
            return None
        try:
            values = [float(w) for w in words[:count]]
        except ValueError:
            return None
        return values if all(math.isfinite(v) for v in values) else None

    def range(self):
        """The wheel's current range in degrees, None unknown."""
        value = self.numbers('range', 1)
        return value[0] if value and value[0] > 0 else None

    def align_k_limit(self):
        """ffb_align's 4th field (k_limit) in FS per degree, None when the driver does not report it."""
        value = self.numbers('ffb_align', 4)
        return value[3] * 1e-6 if value and value[3] >= 0 else None

    def hands(self):
        """ffb_hands as (torque in per mille of full scale, hands on), None when absent."""
        value = self.numbers('ffb_hands', 2)
        return None if value is None else (value[0], bool(value[1]))

    def live(self):
        """ffb_dyn_live as (friction 1e-4 FS, damping uFS per deg/s, seconds of samples), None when absent."""
        value = self.numbers('ffb_dyn_live', 3)
        return None if value is None else (value[0], value[1], value[2])

    def off(self):
        """Every target back to 0 (the driver slews them out); an absent attribute is skipped, the first error
        raised after the rest were tried."""
        error = None
        for name, text in (('ffb_align', '0 0 0'), ('ffb_dyn_damping', '0'), ('ffb_dyn_friction', '0')):
            if not self.has(name):
                continue
            try:
                self.write(name, text)
            except OSError as e:
                error = error or e
        if error is not None:
            raise error


class DynStage:
    """The engine's side: what to write and when. `column`/`align`/`removal` are the profile's toggles (column feel,
    local alignment spring, friction removal); `record(t, channel, *values)` an optional hook (ffb_record)."""

    def __init__(self, path, record=None):
        self.dyn = Dynamics(path)
        self.column_on, self.align_pref, self.removal = True, True, False
        self.own = DEFAULT_WHEEL
        self.result = None               # the calibration result `wheel` comes from
        self.wheel = None                # the wheel's calibrated ffb_dyn_wheel values, None until there is a result
        self.range = None                # degrees
        self.k_limit = None              # FS per degree, as the driver reports it
        self.record = record
        self.on = False                  # something is written and not yet set back
        self._column = None              # (damping, friction) last written
        self._align = None               # (text, time) of the last ffb_align write
        self._align_args = None          # the last (theta, k, tau) asked for, kept for the rewrite
        self._poll_at = None
        self.errors = 0

    def set_wheel_result(self, result):
        self.result = result
        self.wheel = wheel_values(result)
        self.own = self.wheel or DEFAULT_WHEEL

    def usable(self):
        return self.dyn.available()

    def can_align(self):
        return self.align_pref and self.dyn.has('ffb_align') and self.range is not None

    def _write(self, name, text, t):
        try:
            self.dyn.write(name, text)
        except OSError as e:
            self.errors += 1
            if self.errors <= 3:
                logging.warning("Countersteer FFB: %s: %s", name, e)
            return False
        if self.record is not None:
            self.record(t, name, text)
        return True

    def start(self, t):
        """Playing begins: the calibrated wheel to the driver (when there is one), the range and k_limit read."""
        if not self.usable():
            return
        if self.wheel is not None and self.dyn.has('ffb_dyn_wheel'):
            self._write('ffb_dyn_wheel', wheel_text(self.wheel), t)
        self._poll_at = None
        self.poll(t)

    def poll(self, t):
        """The range and ffb_align's k_limit, once a second."""
        if self._poll_at is not None and 0.0 <= t - self._poll_at < K_LIMIT_EVERY:
            return
        self._poll_at = t
        self.range = self.dyn.range()
        self.k_limit = self.dyn.align_k_limit()
        if self.record is not None and self.dyn.has('ffb_align'):
            self.record(t, 'ffb_align_read', (self.k_limit or 0.0) * 1e6, self.range or 0.0)

    def column(self, t, scale, peak_nm):
        """Column damping and friction for the car (impedance scale `scale`), written when they change; 0 with the
        feel off."""
        if not (self.dyn.has('ffb_dyn_damping') or self.dyn.has('ffb_dyn_friction')):
            return
        want = column_targets(scale, peak_nm, self.own, self.removal) if self.column_on else (0, 0)
        last = self._column
        if last is not None and all(abs(w - l) <= COLUMN_MIN_STEP * max(l, 1) and (w == 0) == (l == 0)
                                    for w, l in zip(want, last)):
            return
        ok = True
        if self.dyn.has('ffb_dyn_damping'):
            ok = self._write('ffb_dyn_damping', '%d' % want[0], t) and ok
        if self.dyn.has('ffb_dyn_friction'):
            ok = self._write('ffb_dyn_friction', '%d' % want[1], t) and ok
        if ok:
            self._column = want
            self.on = self.on or any(want)

    def align(self, t, theta_a_deg, k_fs, tau_fs):
        """The spring (angles left-positive degrees of the wheel's own position). Written at most every
        ALIGN_MIN_GAP; k = 0 always goes through."""
        if not self.can_align():
            return
        self._align_args = (theta_a_deg, k_fs, tau_fs)
        text = align_text(theta_a_deg, k_fs, tau_fs, self.range)
        last = self._align
        if last is not None and k_fs > 0.0 and t - last[1] < ALIGN_MIN_GAP and last[0] == text:
            return
        if self._write('ffb_align', text, t):
            self._align = (text, t)
            self.on = self.on or k_fs > 0.0

    def keep(self, t):
        """No sample for a while: the spring is written again so the driver does not fade it (a stale target is the
        driver's to drop at 20 ms, ours to hold while samples are merely late)."""
        last = self._align
        if last is None or not self._align_args or self._align_args[1] <= 0.0:
            return
        if t - last[1] >= ALIGN_KEEP and self.can_align():
            self._write('ffb_align', last[0], t)
            self._align = (last[0], t)

    def align_off(self, t):
        if self._align_args is not None and self._align_args[1] > 0.0 and self.dyn.has('ffb_align'):
            self._write('ffb_align', '0 0 0', t)
        self._align = self._align_args = None

    def stop(self, t):
        """Everything to 0, once, when something was on."""
        was = self.on or self._column is not None or self._align_args is not None
        self._column = self._align = self._align_args = None
        self.on = False
        if not was or not self.usable():
            return
        try:
            self.dyn.off()
        except OSError as e:
            logging.warning("Countersteer FFB: virtual steering dynamics off: %s", e)
        if self.record is not None:
            self.record(t, 'off', '')
