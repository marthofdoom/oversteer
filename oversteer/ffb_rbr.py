"""Countersteer FFB for Richard Burns Rally (NGP): the game's own force with
NGP's saturating curve taken out.

What the game sends (rerbr, a bit-exact NGP reimplementation: ngp::ffb::curve,
rbr_gameplay::ffb::magnitude; ~/.cache/ffb-routes/report.md):

    x = sign(T) * (1 - 0.975 ** (|T| * 100 / S))      NGP's curve, -1..1
    m = floor(-10000 * x * strength)                  the DirectInput magnitude (STRENGTH 0..1, negated if INVERSE)

one constant force about every 1.15 ms (Sleep(1) under Wine, ~810 a second,
the same value again 78 % of the time). T is the steered wheels' kingpin
torque times the rack ratio through a one-pole filter; S is the car's
ForceFeedbackSensitivity_NGP (common.lsp) times the ini's surface % (NGP's
sensitivity(): a % above 0 other than 100 scales it, then S >= 1). Wine turns
m into an FF_CONSTANT level (direction 0x4000: as sent) of about m * 32767 /
10000, which the driver's game_ffb_raw shows before any gain.

The curve is a soft limiter: its slope at a level is (1 - |x|) of its slope
at the centre, so at half force the torque's changes already reach the wheel
at half strength, and at 80 % at a fifth: the grip building up and the
torque falling away at the front's limit are flattened exactly where they
matter. Taking the logarithm undoes it:

    u = -ln(1 - |x|)  =  |T| * 100 * (-ln 0.975) / S             (linear in T)

and v = C * u, in ext_force units (C the game's full-scale level), is the
rack torque at the same strength as the game's at small forces (the curve's
slope at 0 is 1 in these units). S cancels: the force needs neither the car
nor the ini; they only turn u into NGP's torque units (`torque`, for the
log and the texture to come).

v grows without bound, so it goes through a knee instead of the game's curve
(`shape`): linear up to K = linearity of the ceiling A (the game's full scale,
the driver's ext_force cap or the game's own peak if lower: STRENGTH below), then a knee
with the slope carried on, saturating at A:

    out = v                                     v <= K*A
    out = K*A + W * (1 - exp(-(v - K*A) / W))   above, W = (1 - K) * A

At linearity 0 that is A * (1 - exp(-v / A)): the game's own curve exactly
(with A = C). At 100 it is min(v, A): fully linear, then clipped.

Is it an improvement? At the same small-force strength and the same
ceiling, any curve that is more linear reaches the ceiling sooner. Ours has
more slope than the game's at every torque below u = 1 (the game's force
under 63 %), whatever the linearity (the curves' slopes cross there), and
less above. So it is measured, not assumed (`gain`): the mean of our slope
over the game's, over the uploads where the game's force is over FORCE_MIN
(on a straight both are 1). It plays only while that is GAIN_GO or more
(GAIN_HOLD to keep playing).

The default linearity, 70: torque is reproduced exactly up to 70 % of the
ceiling (u = 0.7, where the game's curve is at 50 % and already 28 %
compressed, its slope halved), the top 30 % is a smooth knee (slope
continuous, no clip, so a big hit is not flat-topped and there is no jolt
at the end of the linear part), and the slope above the knee is still
1 / (1 - 0.7) = 3.3 times the game's at the same output level. 90 gains
little linear range and turns the knee into nearly a clip; 50 keeps the
mid-range compressed. Above u = 1 every setting is less sensitive than the
game; the gain check stops it where a car or a sensitivity setting keeps
the force up there.

Since the rack-force path (ffb_engine.RbrRack: the column torque through the boost curve, the hand-force
mapping, the damping and lock stop) this class is the gate on the raw and the source of S: the force itself
no longer comes from `force()`/`shape()` (kept for the replay scripts), the 'no-gain' check is gone and
'saturated' is a note (`saturated`), not a refusal. The checks on the raw (`refused`):

- 'grid': the levels are not on Wine's grid of DirectInput magnitudes
  (`level_of`: floor((m * 65535 - 10000) / 20000)). An upload from another
  program lands on it about 30 % of the time; NGP's always (GRID_GO: 90 %).
  `round_trip` takes a level to its nearest magnitude and back through Wine:
  `on_grid` is the level coming back unchanged.
- 'saturated': the game's force is over SAT_X of full scale more than
  SAT_SHARE of the time: the inversion is ill-conditioned there (one
  magnitude step is a large torque step) and the knee would hold ours at
  the ceiling anyway.
- 'no-gain': the gain above.

There is no 'strength' refusal any more: ours plays from the start with STRENGTH 1 assumed (lighter), never past the
game's own peak (`ceiling`), and the applied STRENGTH glides (STRENGTH_SLEW per second) to what the evidence shows:
a clipped top (`_plateau`: the game's top level repeated for seconds) or the fit below. A new stream keeps both.

STRENGTH. The full scale C is 32767 times the game's STRENGTH (0..1), which lives in the profile, a binary file
that is not readable (the 2026-11-03 drive had 0.2: no level past 6554). `StrengthFit` finds it from the drive: x =
|level| / (32767 s), u(s) = -ln(1 - x) must be linear in the column torque Newton gives (the front's lateral force
Fy = m (b/L) a_y + I_z rdot / L through a trail prior over the ratio N), on moderate-a_y samples; the s with the best
correlation r wins (only s above the game's own peak are possible). Until it has converged s is 1: x is smaller, the force
lighter. With s fitted (or a clipped top found) C = 32767 s, applied by gliding.
"""
import json
import logging
import math
import os
import threading
import time

FULL = 32767                             # ext_force full scale (ffb_engine.FULL)
DI_MAX = 10000                           # DirectInput's magnitude range
DI_LEVEL = 65535.0 / (2 * DI_MAX)        # Wine: FF_CONSTANT level per DirectInput magnitude (see `level_of`)
BASE = 0.975
K_LN = -math.log(BASE)                   # 0.025318: u = |T| * 100 * K_LN / S
X_MAX = 0.995                            # |x| past this is not inverted further (u <= 5.3)

LINEARITY = 70                           # % (see the docstring)
FORCE_MIN = 0.10                         # |x| under which an upload says nothing about the curve (straights)
GRID_GO, GRID_HOLD = 0.90, 0.80          # share of changed levels on the grid
SAT_X = 0.98
SAT_SHARE, SAT_HOLD = 0.05, 0.10         # share of the time over SAT_X
GAIN_GO, GAIN_HOLD = 1.10, 1.00          # our mean slope over the game's (forces over FORCE_MIN)
MIN_CHANGES = 200                        # changed levels before the grid is judged
MIN_FORCE_TIME = 2.0                     # s of uploads over FORCE_MIN before the gain is judged
GRID_EVENTS = 500                        # changed levels the grid share is weighted over
TAU_SAT, TAU_GAIN = 5.0, 10.0              # s
MAX_DT = 0.05                            # s: one upload never counts for more than this (a pause)
STRENGTH_PROVEN = 0.85                   # the game's own peak, share of full scale, that shows its STRENGTH is near 1
STRENGTH_SLEW = 0.2                      # 1/s: the applied strength moves this share of its way to the fitted one per second
PLATEAU_BAND = 8.0                       # ext_force units under the game's top that count as at the top (a clipped force
                                         # repeats its top level; the grid's steps there are 3.3 apart at most)
PLATEAU_UPLOADS, PLATEAU_SECONDS = 100, 4    # uploads at the top, in this many different seconds: the top is a clip
PEAK_MARGIN = 1.0                        # ours never past the game's own peak times this
FIT_STEPS = tuple(round(0.1 + 0.01 * i, 2) for i in range(91))     # candidate strengths 0.10 .. 1.00
FIT_HZ = 20.0                            # the fit takes telemetry at this rate
FIT_TAU = 90.0                           # s: its exponential window
FIT_SPEED, FIT_AY_LO, FIT_AY_HI = 8.0, 1.0, 6.0     # m/s, m/s^2: the samples it looks at
FIT_MIN_TIME = 25.0                      # s of such samples before it is believed
FIT_MARGIN = 0.03                        # r over r at s = 1 needed to leave s = 1
FIT_SWITCH = 0.015                       # r a new s must beat the held one by
FIT_SMOOTH = 0.1                         # s: the one-pole filter on Fy and the level
CAR_M, CAR_L, CAR_B, CAR_IZ, CAR_N = 1390.0, 2.53, 1.24, 1751.0, 14.3   # a WRC (Fiesta/C3/Yaris/i20): the scale k is free
TRAIL_PRIOR = 0.040                      # m
STRENGTH_FILE = 'rbr_strength.json'      # the confirmed STRENGTH, kept between sessions (the game's, not the car's)
STRENGTH_PATH = None                     # None: the data folder's file (tests point it elsewhere)
INI_RETRY = 30.0                         # s between looks for RichardBurnsRally.ini while none was found
INI_CHECK = 5.0                          # s between looks at its mtime


def ngp_curve(torque, sensitivity):
    """NGP's x for a torque T and sensitivity S (rerbr ngp::ffb::curve)."""
    if torque == 0.0:
        return 0.0
    return math.copysign(1.0 - BASE ** (abs(torque) * 100.0 / sensitivity), torque)


def ngp_inverse(x, sensitivity):
    """T for NGP's x (|x| capped at X_MAX)."""
    return math.copysign(linear(abs(x)) * sensitivity / (100.0 * K_LN), x) if x else 0.0


def linear(x):
    """u = -ln(1 - |x|): NGP's curve undone, in units where its slope at 0 is 1."""
    return -math.log(1.0 - min(abs(x), X_MAX))


def magnitude(x, strength=1.0):
    """The DirectInput magnitude NGP sends for x (rbr_gameplay::ffb::magnitude)."""
    return max(-DI_MAX, min(DI_MAX, math.floor(-DI_MAX * x * strength)))


def level_of(m):
    """The FF_CONSTANT level Wine makes of a magnitude (game_ffb_raw's units and sign: m's). Measured on a drive
    (20261103-133906, 99.98 % of 4000 levels): the -32768..32767 range's mapping, floor((m * 65535 - 10000) / 20000),
    so m = 0 is -1 and the positive and negative sides are one level apart (not int(m * 32767 / 10000))."""
    return (m * 65535 - 10000) // (2 * DI_MAX)


def magnitude_of(level):
    """The DirectInput magnitude whose level is nearest to `level` (the inverse of `level_of` on its grid)."""
    return int(round((2 * level + 1) * DI_MAX / 65535.0))


def round_trip(level):
    """A raw level taken to its nearest magnitude and back through Wine: NGP's own levels come back as they went."""
    return level_of(magnitude_of(level))


def on_grid(level):
    """The level is exactly one Wine makes of a DirectInput magnitude (an upload from another program lands on it
    1 time in 3.3)."""
    return round_trip(level) == level


def shape(v, knee, ceiling):
    """The knee on a linear level v >= 0 (`knee` 0..1 of `ceiling`)."""
    k = knee * ceiling
    if v <= k:
        return v
    w = ceiling - k
    if w <= 0.0:
        return ceiling
    return k + w * (1.0 - math.exp(-(v - k) / w))


def shape_slope(v, knee, ceiling):
    """d shape / dv."""
    k = knee * ceiling
    if v <= k:
        return 1.0
    w = ceiling - k
    return 0.0 if w <= 0.0 else math.exp(-(v - k) / w)


def sensitivity(base, percent):
    """NGP's S for the car's ForceFeedbackSensitivity_NGP and the ini's % for the surface (rerbr ngp::ffb::sensitivity)."""
    s = base * percent / 100.0 if percent and percent > 0 and percent != 100 else base
    return max(s, 1.0)


def ngp_percents(root):
    """{surface: %} of RichardBurnsRally.ini [NGP] ForceFeedbackSensitivity<Surface> (read-only), or None."""
    from . import rbr_install
    path = rbr_install.resolve(root, 'RichardBurnsRally.ini') if root else None
    if not path:
        return None
    try:
        ngp = rbr_install.read_ini(path).get('NGP', {})
    except OSError:
        return None
    out = {}
    for surface in ('gravel', 'tarmac', 'snow'):
        value = rbr_install._int(ngp.get('ForceFeedbackSensitivity' + surface.capitalize()))
        if value is not None:
            out[surface] = value
    return out or None


def _strength_path():
    if STRENGTH_PATH:
        return STRENGTH_PATH
    from . import diagnostics
    return os.path.join(diagnostics.data_home(), 'oversteer', STRENGTH_FILE)


def kept_strength():
    """The STRENGTH confirmed in an earlier session (0.10 .. 1.00), None when none is kept or the file is unreadable."""
    try:
        with open(_strength_path()) as f:
            value = float(json.load(f)['strength'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return value if math.isfinite(value) and FIT_STEPS[0] <= value <= 1.0 else None


def keep_strength(value):
    try:
        path = _strength_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'strength': round(float(value), 3)}, f)
        os.replace(tmp, path)
    except OSError as e:
        logging.warning("Countersteer FFB: RBR: the game's STRENGTH could not be kept: %s", e)


class _Share:
    """Exponentially weighted share of True over time (or over events with dt 1)."""

    def __init__(self, tau):
        self.tau = tau
        self.w = self.on = 0.0

    def add(self, flag, dt):
        decay = math.exp(-dt / self.tau)
        self.w = self.w * decay + dt
        self.on = self.on * decay + (dt if flag else 0.0)

    def share(self):
        return self.on / self.w if self.w > 0.0 else None


class StrengthFit:
    """The game's STRENGTH from the drive (module docstring): per candidate s, exponentially weighted moments of u(s)
    and the Newton front force over moderate-a_y samples; r(u, Fy) picks s, the slope gives the trail it implies."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.peak = 0.0                  # the highest |level| / 32767 seen: s below it is impossible
        self.n = 0
        self.time = 0.0
        self.w = 0.0
        self.mom = {c: [0.0] * 5 for c in FIT_STEPS}     # per s: sum u, u^2, uF, and shared F, F^2 below
        self.f1 = self.f2 = 0.0
        self.s = 1.0                     # the strength in use
        self.r = self.trail = None
        self.converged = False
        self.source = 'assumed 1'
        self._t = None
        self._yaw = None                 # (t, yaw rate)
        self._rdot = 0.0
        self._fy = self._lev = self._ay = None
        self._due = 0.0
        self._last = None

    def feed(self, t, ay, yaw, speed, level):
        """A telemetry sample (a_y m/s^2, yaw rate rad/s, speed m/s) with the raw level now. Returns True when s moved."""
        if level is None or ay is None or yaw is None or speed is None or not all(map(math.isfinite, (ay, yaw, speed))):
            return False
        y = abs(level) / FULL
        self.peak = max(self.peak, y)
        dt = 0.0 if self._t is None else t - self._t
        self._t = t
        if dt <= 0.0 or dt > 0.25:
            self._fy = self._lev = self._ay = self._yaw = None
            return False
        if self._yaw is not None and t > self._yaw[0]:
            rd = (yaw - self._yaw[1]) / (t - self._yaw[0])
            self._rdot += (1.0 - math.exp(-dt / FIT_SMOOTH)) * (rd - self._rdot)
        self._yaw = (t, yaw)
        fy = CAR_M * (CAR_B / CAR_L) * ay + CAR_IZ * self._rdot / CAR_L
        if self._fy is None:
            self._fy, self._lev, self._ay = fy, y, ay
        else:
            a = 1.0 - math.exp(-dt / FIT_SMOOTH)
            self._fy += a * (fy - self._fy)
            self._lev += a * (y - self._lev)
            self._ay += a * (ay - self._ay)
        if t < self._due:
            return False
        self._due = t + 1.0 / FIT_HZ
        if speed < FIT_SPEED or not FIT_AY_LO < abs(self._ay) < FIT_AY_HI:
            return False
        step = 1.0 / FIT_HZ
        decay = math.exp(-step / FIT_TAU)
        f, lev = abs(self._fy), self._lev
        self.f1 = self.f1 * decay + step * f
        self.f2 = self.f2 * decay + step * f * f
        self.w = self.w * decay + step
        for c, m in self.mom.items():
            x = min(lev / c, X_MAX)
            u = -math.log(1.0 - x)
            m[0] = m[0] * decay + step * u
            m[1] = m[1] * decay + step * u * u
            m[2] = m[2] * decay + step * u * f
        self.n += 1
        self.time += step
        return self._decide()

    def _r(self, c):
        m, w = self.mom[c], self.w
        vu = m[1] / w - (m[0] / w) ** 2
        vf = self.f2 / w - (self.f1 / w) ** 2
        if vu <= 1e-12 or vf <= 1e-12:
            return None
        return (m[2] / w - m[0] / w * self.f1 / w) / math.sqrt(vu * vf)

    def _decide(self):
        if self.time < FIT_MIN_TIME or self.w <= 0.0:
            return False
        rs = {c: self._r(c) for c in FIT_STEPS if c >= self.peak - 0.001}     # (a strength under the game's own peak is impossible)
        rs = {c: v for c, v in rs.items() if v is not None}
        if not rs or 1.0 not in rs:
            return False
        best = max(rs, key=rs.get)
        held = self.s if self.s in rs else 1.0
        need = FIT_MARGIN if held == 1.0 else FIT_SWITCH
        new = best if best != held and rs[best] >= rs[held] + need else held
        self.r = rs[new]
        m = self.mom[new]
        self.trail = m[2] / self.f2 if self.f2 > 0.0 else None        # u per N of Fy
        moved = new != self.s or not self.converged
        self.s, self.converged = new, True
        self.source = 'fitted' if new != 1.0 else 'fitted (1)'
        return moved

    def trail_mm(self, sens):
        """The trail the fit implies (mm) for sensitivity `sens`: tau_col = 0.062862 u S Nm, trail = tau_col N / Fy."""
        return None if self.trail is None else 0.062862 * sens * CAR_N * self.trail * 1000.0

    def describe(self):
        return "strength %.3f (%s, r %s, %d samples, implied trail %s mm at S 100)" % (
            self.s, self.source, 'n/a' if self.r is None else '%.2f' % self.r, self.n,
            'n/a' if self.trail is None else '%.0f' % (self.trail_mm(100.0)))


class RbrForce:
    """Undoes NGP's curve on the game's raw level and plays it through the knee (module docstring).
    feed() every new upload; force() the ext_force for a level."""

    def __init__(self, linearity=LINEARITY):
        self.linearity = linearity
        self.cap = None                  # the driver's ext_force cap in ext_force units (None: none known)
        self._percents = None            # (ini path, mtime, {surface: %}) read off the engine thread
        self._lookup = None              # the thread reading the ini
        self._lookup_at = None
        self.reset()

    def reset(self, car=None):
        """Everything learnt: a new stream (the device set again). A car key narrowing keeps all but S (set_car)."""
        self.car = car
        self.fit = StrengthFit()
        self.s_kept = kept_strength()    # confirmed in an earlier session: the start, re-confirmed by the evidence
        self.s_applied = self.s_kept or 1.0      # C / FULL, glided toward the target (feed_physics)
        self.full = FULL * self.s_applied        # C: the game's full-scale level in use (STRENGTH 1.0 until the evidence has it)
        if self.s_kept is not None:
            logging.info("Countersteer FFB: RBR: game STRENGTH %.2f (kept from the last session)", self.s_kept)
        self.s_plateau = None            # the STRENGTH a clipped top shows (kept through new streams)
        self._phys_t = None
        self._glide_log = None
        self.peak = 0.0                  # the game's own highest |level| since the stream started
        self.peak_kept = 0.0             # ... and since the reset (kept through new streams, as the fit's moments)
        self._top = 0.0                  # the level the plateau is counted at, its uploads and the seconds they fell in
        self._top_n = 0
        self._top_secs = set()
        self._clear_s()
        self._last_level = None
        self._last_t = None
        self._grid = _Share(GRID_EVENTS)
        self._sat = _Share(TAU_SAT)
        self._ours = self._game = 0.0    # EW sums of our slope and the game's over forces past FORCE_MIN
        self.changes = 0
        self.force_time = 0.0
        self.ready = False
        self.refused = None
        self.waiting = 'raw'
        self.gain = None
        self.saturated = False           # the game's force sits at its limit too often (a status note)

    def new_stream(self, why):
        """RBR's stream started again (the uploads restarted, a new stage): its STRENGTH may have been changed
        in the menus, so its top is to be seen again before ours plays."""
        if self.peak:
            logging.info("Countersteer FFB: RBR: %s: the game's own peak is measured again", why)
        self.peak = 0.0
        self._top, self._top_n, self._top_secs = 0.0, 0, set()
        self.fit.peak = 0.0              # (the fit's moments and a plateau found stay: a restart keeps the STRENGTH)

    def _clear_s(self):
        self.base_s = None               # the car's ForceFeedbackSensitivity_NGP (None: not in the car table)
        self.s = None                    # S with the surface's %
        self.s_note = None
        self.surface = None
        self.torque = None               # NGP's T of the last upload (None without S): the texture's hook

    @property
    def ceiling(self):
        """A: the game's full scale, the driver's cap and the game's own peak, whichever is lowest: ours is never
        stronger than anything the game itself sent (a STRENGTH under 1, not readable, cannot make it so)."""
        known = self.peak_kept if (self.s_plateau is not None or self.fit.converged) else 0.0      # (a restart keeps what was learnt)
        return min(self.full, self.cap or self.full, max(self.peak * PEAK_MARGIN, known, 1.0))

    def set_linearity(self, value):
        self.linearity = min(max(int(value), 0), 100)

    def set_car(self, car, stage=None, root=None, now=None):
        """A telemetry sample's car and stage: S from the car table and the ini (only for `torque`). A new key
        (the car told from its gearing, a candidate set narrowing) clears S alone: the curve and the fit are
        the game's, not the car's."""
        if car != self.car:
            self.car = car
            self._clear_s()
        if self.base_s is None and car:
            from . import car_data
            self.base_s = (car_data.entry(car) or {}).get('ffb_sensitivity')
        if self.base_s is None:
            return
        percents = self._ini(root, now)
        if percents is False:
            return                       # being read
        if self.surface is not None and self.surface[0] == stage and self.surface[2] == percents:
            return
        surface = None
        if percents and len(set(percents.values())) > 1 and stage:
            from . import stage_tables
            surface = stage_tables.surface_of(stage_tables.entry(stage) or {})[0]
        if not percents:
            percent, self.s_note = 100, "RichardBurnsRally.ini unreadable: surface sensitivity taken as 100 %"
        elif len(set(percents.values())) == 1:
            percent, self.s_note = next(iter(percents.values())), None
        else:
            percent = percents.get(surface or 'gravel', 100)
            self.s_note = None if surface else "stage surface unknown: gravel's sensitivity used"
        self.surface = (stage, surface, percents)
        self.s = sensitivity(self.base_s, percent)

    def _ini(self, root, now):
        """{surface: %}, None (no install, or unreadable), or False while a read runs. Read on a thread of its
        own (rbr_install.ASYNC), never on the engine's: again when the file changed (its mtime, looked at every
        INI_CHECK s) and, while none was found, every INI_RETRY s (the install search finds it later)."""
        from . import rbr_install
        now = time.monotonic() if now is None else now
        cached = self._percents          # (root asked, path, {surface: %} or None, read at, mtime)
        if self._lookup is not None and self._lookup.is_alive():
            return False
        if cached is not None and (root is None or cached[0] == root):
            if cached[1] is None:
                if now - cached[3] < INI_RETRY:
                    return cached[2]
            elif now - cached[3] < INI_CHECK:
                return cached[2]
            else:
                try:
                    mtime = os.stat(cached[1]).st_mtime
                except OSError:
                    mtime = None
                if mtime == cached[4]:
                    self._percents = cached[:3] + (now, mtime)
                    return cached[2]

        def read():
            base = root or rbr_install.current_root()
            path = rbr_install.resolve(base, 'RichardBurnsRally.ini') if base else None
            try:
                mtime = os.stat(path).st_mtime if path else None
            except OSError:
                mtime = None
            self._percents = (root, path, ngp_percents(base) if path else None, now, mtime)
        if rbr_install.ASYNC:
            self._lookup = threading.Thread(target=read, name='rbr-ffb-ini', daemon=True)
            self._lookup.start()
            return False
        read()
        return self._percents[2]

    @property
    def strength(self):
        """The game's STRENGTH in use: fitted, else 1 (conservative: x smaller, the force lighter)."""
        return self.full / FULL

    def target_s(self):
        """The STRENGTH the evidence shows: a clipped top, else the fit once it left 1, else the conservative prior 1."""
        if self.s_plateau is not None:
            return self.s_plateau
        if self.fit.converged and (self.fit.s != 1.0 or self.s_kept is None):
            return self.fit.s
        return self.s_kept or 1.0

    def feed_physics(self, t, sample, level):
        """A telemetry sample with the raw level now: the strength fit (StrengthFit), and the applied strength gliding
        toward what the evidence shows (never a step: STRENGTH_SLEW of the way per second)."""
        accel = getattr(sample, 'accel', None)
        ay = accel[1] if accel is not None and len(accel) > 1 else None
        moved = self.fit.feed(t, ay, getattr(sample, 'yaw_rate', None), getattr(sample, 'speed', None), level)
        if self.s_kept is not None and self.s_plateau is None and self.peak > self.s_kept * FULL * 1.05:
            # a level the kept STRENGTH cannot produce: it was raised in the menus. Back to the conservative prior at
            # once (a lighter force: the safe direction for a step), until a clipped top shows the new one
            logging.info("Countersteer FFB: RBR %s: a level of %d is over the kept STRENGTH %.2f: back to 1 until it "
                         "is seen again", self.car, self.peak, self.s_kept)
            self.s_kept = None
            self.s_applied = 1.0
        target = self.target_s()
        if (self.s_plateau is not None or (self.fit.converged and self.fit.s != 1.0)) and (
                self.s_kept is None or abs(target - self.s_kept) > 0.005):
            self.s_kept = target
            keep_strength(target)
        dt = 0.0 if self._phys_t is None else min(max(t - self._phys_t, 0.0), 0.25)
        self._phys_t = t
        before = self.s_applied
        self.s_applied += (1.0 - math.exp(-STRENGTH_SLEW * dt)) * (target - self.s_applied)
        full = max(FULL * self.s_applied, self.peak, abs(level or 0))
        if moved:
            logging.info("Countersteer FFB: RBR %s: game STRENGTH %s", self.car, self.fit.describe())
        if abs(self.s_applied - target) > 0.02:
            if self._glide_log is None or t - self._glide_log >= 30.0:
                self._glide_log = t
                logging.info("Countersteer FFB: RBR %s: game STRENGTH applied %.2f, evidence %.2f (%s)", self.car,
                             self.s_applied, target, 'a clipped top' if self.s_plateau is not None else self.fit.source)
        else:
            self._glide_log = None
        self.full = full

    def _plateau(self, t, a):
        """Count the uploads at the game's top: a force that clips repeats its top level, and that level is C (a game at
        STRENGTH 1 whose top is far under full scale does not sit on it for seconds on end)."""
        if a > self._top + PLATEAU_BAND:
            self._top, self._top_n, self._top_secs = a, 0, set()
        if a >= self._top - PLATEAU_BAND:
            self._top = max(self._top, a)
            self._top_n += 1
            self._top_secs.add(int(t))
            if (self.s_plateau is None and self._top_n >= PLATEAU_UPLOADS and len(self._top_secs) >= PLATEAU_SECONDS
                    and self._top < 0.9 * FULL):
                self.s_plateau = min(max(round(self._top / FULL, 2), FIT_STEPS[0]), 1.0)
                logging.info("Countersteer FFB: RBR %s: game STRENGTH %.2f: its force clips at %.0f", self.car,
                             self.s_plateau, self._top)

    def _x(self, level):
        return min(abs(level) / self.full, 1.0)

    def feed(self, t, level, holding=False):
        """A new upload (game_ffb_raw's level, its count moved) at t."""
        dt = 0.0 if self._last_t is None else min(max(t - self._last_t, 0.0), MAX_DT)
        self._last_t = t
        if abs(level) > self.full:
            self.full = float(abs(level))     # the game's STRENGTH is above the assumed: follow it
        if on_grid(level):
            self.peak = max(abs(level), self.peak)
            self.peak_kept = max(self.peak_kept, self.peak)    # only the game's own: another program's level is not its top
            self._plateau(t, abs(level))
        x = self._x(level)
        if level != self._last_level:
            if level:
                self._grid.add(on_grid(level), 1.0)
                self.changes += 1
            self._last_level = level
        self._sat.add(x > SAT_X, dt)
        if x > FORCE_MIN and dt:
            v = self.full * linear(x)
            decay = math.exp(-dt / TAU_GAIN)
            # Judged at the ceiling it would play at: once the game's top is seen (it plays only then)
            ceiling = min(self.full, self.cap or self.full, max(self.peak, STRENGTH_PROVEN * self.full) * PEAK_MARGIN)
            self._ours = self._ours * decay + dt * shape_slope(v, self.linearity / 100.0, ceiling)
            self._game = self._game * decay + dt * (1.0 - min(x, X_MAX))
            self.force_time += dt
        # m = floor(-10000 x): the level's sign is -T's (with the game's DIRECTION normal, not INVERSE)
        self.torque = None if self.s is None else ngp_inverse(math.copysign(x, -level), self.s)
        self._judge(holding)

    def _judge(self, holding):
        """The gates that stay with the rack force (ffb_engine.RbrRack): the uploads are NGP's (the grid) and the
        game's STRENGTH is near 1. The saturation is a note (`saturated`), not a refusal, and the gain is no longer
        judged: the force is the column torque through our own chain, not this class's knee."""
        grid, sat = self._grid.share(), self._sat.share()
        self.gain = self._ours / self._game if self._game > 0.0 else None
        self.saturated = sat is not None and sat > (SAT_HOLD if holding else SAT_SHARE)
        if self.changes < MIN_CHANGES or grid is None:
            self._set(None, 'raw')
            return
        if grid < (GRID_HOLD if holding else GRID_GO):
            self._set('grid')
            return
        self._set(None, None)

    def _set(self, refused, waiting=None):
        if refused != self.refused and refused and refused != 'strength':
            logging.info("Countersteer FFB: RBR %s: %s (grid %.2f, over %.0f %% %.3f, gain %s)", self.car, refused,
                         self._grid.share() or 0.0, SAT_X * 100, self._sat.share() or 0.0,
                         'none' if self.gain is None else '%.2f' % self.gain)
        self.refused, self.waiting = refused, waiting
        self.ready = refused is None and waiting is None

    def set_ceiling(self, clip_at):
        """The driver's ext_force cap (ext_force units), None for none."""
        self.cap = clip_at

    def force(self, level):
        """ext_force for the game's raw level (0 for 0)."""
        if not level:
            return 0
        v = self.full * linear(self._x(level))
        out = shape(v, self.linearity / 100.0, self.ceiling)
        return int(round(math.copysign(min(out, FULL), level)))

    def describe(self):
        return ("linearity %d %%, full scale %.0f, ceiling %.0f (the game's peak %.0f), gain %s, S %s%s" % (
            self.linearity, self.full, self.ceiling, self.peak, 'none' if self.gain is None else '%.2f' % self.gain,
            'unknown (not in the car table)' if self.s is None else '%.1f' % self.s,
            '; ' + self.s_note if self.s_note else ''))
