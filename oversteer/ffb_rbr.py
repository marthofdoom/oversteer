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
  (int(m * DI_LEVEL), or one more where Wine rounds). An upload from another
  program, or a game not sending its force this way, lands on it about 61 %
  of the time; NGP's always (GRID_GO: 90 %). `round_trip` takes a level to
  its nearest magnitude, x, T and back through the curve, the floor and
  Wine: `on_grid` is the level within GRID_TOL above what comes back.
- 'saturated': the game's force is over SAT_X of full scale more than
  SAT_SHARE of the time: the inversion is ill-conditioned there (one
  magnitude step is a large torque step) and the knee would hold ours at
  the ceiling anyway.
- 'no-gain': the gain above.

- 'strength': the game's own force has not yet reached STRENGTH_PROVEN of
  full scale.

STRENGTH. The full scale C is 32767 times the game's STRENGTH (0..1), which
lives in the profile, a binary file not read here: C is taken as 32767. A
STRENGTH under 1 would leave C too high and let ours, slope-matched at small
forces, go past anything the game can send (up to -ln(1 - s) / s times it).
So the ceiling A is also the game's own highest level so far (PEAK_MARGIN 1):
ours is never stronger than what the game itself sent. Bounding ours that
low costs the gain (ours flattens where the game still rises: on a weave
peaking at 60 % the gain is 0.78), so it plays only once the game's own
force has reached STRENGTH_PROVEN of full scale (a big hit; with STRENGTH
1.0 the curve gets there, under 0.85 it never can): the STRENGTH is then
known to be 0.85 or more and the ceiling is near full scale. Until then the
state is amber 'strength' (set the game's force feedback strength to 100 %
and use the wheel's own strength).
"""
import logging
import math
import os
import threading
import time

FULL = 32767                             # ext_force full scale (ffb_engine.FULL)
DI_MAX = 10000                           # DirectInput's magnitude range
DI_LEVEL = 32767.0 / DI_MAX              # Wine: FF_CONSTANT level per DirectInput magnitude (calibrated by the grid check)
BASE = 0.975
K_LN = -math.log(BASE)                   # 0.025318: u = |T| * 100 * K_LN / S
X_MAX = 0.995                            # |x| past this is not inverted further (u <= 5.3)

LINEARITY = 70                           # % (see the docstring)
FORCE_MIN = 0.10                         # |x| under which an upload says nothing about the curve (straights)
GRID_TOL = 1.0                           # level units: on the grid of m * DI_LEVEL
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
PEAK_MARGIN = 1.0                        # ours never past the game's own peak times this
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
    """The FF_CONSTANT level Wine makes of a magnitude (game_ffb_raw's units and sign: m's)."""
    return int(m * DI_LEVEL)


def round_trip(level, sensitivity=100.0, strength=1.0):
    """A raw level taken to NGP's magnitude, x, T and back through the curve, the floor and Wine. NGP's own levels
    come back as they went (`on_grid`); the curve leg is skipped past X_MAX, where it is not inverted."""
    m = round(abs(level) / DI_LEVEL)
    x = m / DI_MAX / strength
    back = m
    if x < X_MAX:
        back = abs(magnitude(ngp_curve(ngp_inverse(x, sensitivity), sensitivity), strength))
        if abs(back - m) <= 1:               # the floor of a float product: one step either way
            back = m
    return int(math.copysign(level_of(back), level)) if level else 0


def on_grid(level):
    """The level is one Wine makes of a DirectInput magnitude: int(m * DI_LEVEL), or one more (rounded)."""
    return 0 <= abs(level) - abs(round_trip(level)) <= GRID_TOL


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
        self.full = float(FULL)          # C: the game's full-scale level (STRENGTH 1.0 assumed)
        self.peak = 0.0                  # the game's own highest |level| since the stream started
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
        return min(self.full, self.cap or self.full, max(self.peak * PEAK_MARGIN, 1.0))

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

    def _x(self, level):
        return min(abs(level) / self.full, 1.0)

    def feed(self, t, level, holding=False):
        """A new upload (game_ffb_raw's level, its count moved) at t."""
        dt = 0.0 if self._last_t is None else min(max(t - self._last_t, 0.0), MAX_DT)
        self._last_t = t
        if abs(level) > self.full:
            self.full = float(abs(level))     # the game's STRENGTH is above the assumed: follow it
        if on_grid(level):
            self.peak = max(abs(level), self.peak)    # only the game's own: another program's level is not its top
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
        if self.force_time < MIN_FORCE_TIME:
            self._set(None, 'corners')
            return
        if self.peak < STRENGTH_PROVEN * self.full:
            self._set('strength')        # the game's top not seen yet: its STRENGTH may be under 1 (docstring)
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
