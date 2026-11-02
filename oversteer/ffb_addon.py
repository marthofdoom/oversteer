"""Countersteer FFB add-on for Forza Horizon (FH6's 324-byte Data Out): the
game keeps its own force feedback (game_ffb_level stays 100, it is never
written) and the wheel gets g x the game's constant force: lighter as the
front tyres run out of grip. With a driver that has game_const_scale the
driver does the scaling (game_const_scale = round(100 g), no lag, never the
other way: ffb_engine); an older driver gets ext_force = (g - 1) x the
game's constant force now (the driver's game_ffb_raw, fresh), force() below.

Why an add-on and not a replacement (~/.cache/ffb-slip/report.md,
2026-11-01): Forza sends no tyre forces and only ~60 packets a second, and
a slip model alone explains too little of the steering torque to stand in
for the game's force. What it does track is the collapse of pneumatic
trail: exp(-s^q) against ACR's true trail, r 0.80-0.89, sign right 98-99 %.
So it scales the game's force by that and nothing else:

- s = the mean over FL, FR of hypot(slip angle / A, slip ratio / K), from
  Forza's normalised slip channels (|1| about the grip limit). Forza's own
  TireCombinedSlip weighs the two alike; the trail wants slip ratio
  weighed more, so s is computed here.
- g = exp(-s^Q), clamped to [G_MIN, 1]: never stronger than the game, never
  lighter than a real steering with only its caster left (G_MIN, below). The user's "Understeer lightness" (0..1, default STRENGTH)
  scales how much of the drop is played: g_eff = 1 - strength x (1 - g).
- Only above MIN_SPEED and with both front slip channels there: g_eff 1
  otherwise.
- Smoothed in time (Forza's ~60 Hz arrives in steps): a first-order
  low-pass at LOWPASS_HZ, then a slew limit of SLEW per second, advanced
  each engine pass (the force is written at the game's upload rate, not
  the telemetry's).
- ext_force (older drivers only):
  ext =-sign(raw) x (1 - g_eff) x max(0, |raw| - D): always against the
  game's level, never more than it (force()). D (`margin`) is the largest
  change of the game's level from one upload to the next over the last
  MARGIN_WINDOW s: ours is written after the upload it answers, so the
  game's next one lands on top of it; with D off, a next level within D of
  this one can neither flip the total's sign nor be exceeded by ours.
  ext_force and game_ffb_raw share units and sign (the driver applies the
  effect's direction to raw), so no sign is learnt for the force itself.
- A held raw level says nothing of a force that is gone when the game
  publishes nothing (quitting, losing focus); the game stopping or erasing
  its effect publishes a 0 (with a count step), so ext = 0 there. The
  engine cuts ext_force to 0 at once when raw goes stale, the telemetry
  stalls or two effects show, and fades it only while raw is fresh.

That ext_force path is the fallback for drivers without game_const_scale
(new-lg4ff), which scales the game's constant effects in the FF timer: no
upload lag and nothing played against a level that is gone. The same gates
(fresh raw, the upload rate, one effect, the fit below) hold for both; the
engine writes game_const_scale 100 on every stop.

What is learnt live, per car (`ready`, `refused`, `waiting`): that the game's
constant force is its steering force. Forza could drive the wheel through
the hardware spring and damper (game_ffb_raw sees only constant effects):
then there is nothing to lighten. Over TAU seconds of moving samples with
a fresh raw level: raw must move (spread over FLAT) and follow the car's
lateral acceleration or the steering (|r| of FIT or more, KEEP once
ready). `sign`: the sign of r(raw, lateral acceleration to the left), the
game's convention, logged and kept for diagnosis (a capture confirms it).
The engine checks the rest: the raw upload rate, its freshness, app_gain,
two constant effects."""
import collections
import logging
import math

A = 0.7                                  # Forza-normalised slip angle at which the trail has gone (report: 0.6-0.8)
K = 0.7                                  # ... slip ratio. FH6 drive (34 min, 4 cars): the front tyres' lateral grip falls only
                                         # from a slip ratio of ~0.5 (median lateral g 1.51 / 1.29 / 1.24 / 0.93 for
                                         # ratio <0.05 / 0.05-0.2 / 0.2-0.5 / >0.5); 0.35 lightened AWD straights under
                                         # power (front ratio p90 0.27) and hard braking. scripts/ffb-addon-replay.py
Q = 3.0                                  # exp(-s^Q)'s shape (report: 2-4)
# The lightest the game's force gets is what a real steering keeps when the tyre's pneumatic trail has gone:
# the mechanical (caster) part, which does not fade. Steering torque ~ Fy x (pneumatic trail + caster trail), so the
# share left is caster / (caster + pneumatic trail at low slip). ACR (marth's dump, 14,247 moving snapshots,
# ffb_engine.CASTER_TRAIL): caster trail 27 mm from the fit of finalFF to Mz and Fy; pneumatic trail at low
# slip (|slip angle| < 0.06 rad, load 1500 N+) the median of -Mz/Fy, 38 mm (FL) and 43 mm (FR), 41 mm. A road car
# typically has 0.35-0.45 here. Past the grip peak ACR's force stays with Fy (r -0.94 at slip angles over 0.4 rad,
# against Mz's 0.65), the same effect.
CASTER_TRAIL = 0.027                     # m
PNEUMATIC_TRAIL = 0.041                  # m
G_MIN = CASTER_TRAIL / (CASTER_TRAIL + PNEUMATIC_TRAIL)     # 0.40: the physical amount of lightening, "Understeer
                                         # lightness" 100; the slider scales from none (0) to this
STRENGTH = 0.6                           # "Understeer lightness" default
MIN_SPEED = 5.0                          # m/s: none below
LOWPASS_HZ = 20.0
SLEW = 6.0                               # g_eff per second at most (1 -> 0.4 in 100 ms)
SLIP_GONE = 0.5                          # s without the front slip channels: `no_slip`
MARGIN_WINDOW = 0.5                      # s: the upload-to-upload change D is the max over this

TAU = 10.0                               # s: the window the game's force is judged over
MIN_TIME = 3.0                           # s of moving samples with a fresh raw level before judging
MIN_LAT = 1.5                            # m/s^2: lateral acceleration spread that counts as corners
MIN_STEER = 0.04                         # ... or steering spread (-1..1)
FLAT = 300.0                             # raw spread (ext_force units, ~1 % of full scale) under which it is flat
FIT = 0.3                                # |r| to start
KEEP = 0.2                               # |r| to stay ready


def lightness(sample, a=A, k=K, q=Q):
    """exp(-s^q) for the sample's front tyres, clamped to [G_MIN, 1]; None without the channels."""
    angles, ratios = sample.slip_angle, sample.slip_ratio
    if angles is None or ratios is None or len(angles) < 2 or len(ratios) < 2:
        return None
    s = 0.5 * (math.hypot(angles[0] / a, ratios[0] / k) + math.hypot(angles[1] / a, ratios[1] / k))
    if not math.isfinite(s):
        return None
    return min(max(math.exp(-s ** q), G_MIN), 1.0)


class _Corr:
    """Exponentially weighted correlation of (x, y) over `tau` seconds, and y's spread."""

    def __init__(self, tau):
        self.tau = tau
        self.s = [0.0] * 6               # w, x, y, xx, yy, xy

    def add(self, x, y, dt):
        s = self.s
        decay = math.exp(-dt / self.tau)
        for i in range(6):
            s[i] *= decay
        for i, v in enumerate((1.0, x, y, x * x, y * y, x * y)):
            s[i] += v

    def _var(self, i, mean):
        return max(self.s[i] / self.s[0] - mean * mean, 0.0)

    def spread(self, which='y'):
        if not self.s[0]:
            return 0.0
        if which == 'x':
            return math.sqrt(self._var(3, self.s[1] / self.s[0]))
        return math.sqrt(self._var(4, self.s[2] / self.s[0]))

    def r(self):
        w = self.s[0]
        if not w:
            return None
        mx, my = self.s[1] / w, self.s[2] / w
        vx, vy = self._var(3, mx), self._var(4, my)
        if vx <= 0.0 or vy <= 0.0:
            return None
        return (self.s[5] / w - mx * my) / math.sqrt(vx * vy)


class ForzaLightness:
    """The add-on's gain and its checks (the module's docstring). feed() each telemetry sample with the game's
    raw level then (None if not fresh); gain(t) each pass; force(raw, g) for ext_force."""

    def __init__(self, strength=STRENGTH):
        self.strength = strength
        self.reset()

    def reset(self, car=None):
        self.car = car
        self._lat = _Corr(TAU)           # (lateral acceleration, raw)
        self._steer = _Corr(TAU)         # (steering, raw)
        self.moving_time = 0.0
        self.ready = False
        self.refused = None              # 'flat', 'not-steering': the game's constant force is not its steering
        self.waiting = 'raw'             # why not ready: 'raw', 'corners'
        self.sign = None                 # +1: raw grows with lateral acceleration to the left
        self.fit = None
        self._last = None
        self._slip_at = None             # when the front slip channels were last there
        self._moving_at = None
        self.target = 1.0                # g_eff the last sample asks for
        self.g = 1.0                     # g_eff now (smoothed)
        self._g_at = None
        self.clear_uploads()

    def clear_uploads(self):
        """Forget the game's upload-to-upload changes (a new stream)."""
        self._deltas = collections.deque()      # (t, |change|), decreasing: the window's max first
        self._upload_level = None

    def upload(self, t, level):
        """A new upload of the game's constant force: its change from the last one counts toward `margin`."""
        if self._upload_level is not None:
            delta = abs(level - self._upload_level)
            deltas = self._deltas
            while deltas and deltas[-1][1] <= delta:
                deltas.pop()
            deltas.append((t, delta))
        self._upload_level = level
        self._expire(t)

    def _expire(self, t):
        deltas = self._deltas
        while deltas and deltas[0][0] < t - MARGIN_WINDOW:
            deltas.popleft()

    def margin(self, t):
        """D: the largest upload-to-upload change of the game's level over the last MARGIN_WINDOW s."""
        self._expire(t)
        return self._deltas[0][1] if self._deltas else 0.0

    def set_strength(self, value):
        self.strength = min(max(float(value), 0.0), 1.0)

    def no_slip(self, t):
        """Moving without the front slip channels for SLIP_GONE s."""
        return self._moving_at is not None and t - self._moving_at <= SLIP_GONE and (
            self._slip_at is None or t - self._slip_at > SLIP_GONE)

    def moving(self, t, within=1.0):
        return self._moving_at is not None and t - self._moving_at <= within

    def feed(self, t, sample, raw):
        if sample.car is None:
            # Menus and pause (race_on 0): no car named, not another car; what was learnt stands
            self.target = 1.0
            return
        if sample.car != self.car:
            self.reset(sample.car)
        dt = 0.0 if self._last is None else min(max(t - self._last, 0.0), 1.0)
        self._last = t
        moving = sample.speed is not None and sample.speed > MIN_SPEED
        g = lightness(sample) if moving else None
        if moving:
            self._moving_at = t
        if g is not None:
            self._slip_at = t
        self.target = 1.0 if g is None else 1.0 - self.strength * (1.0 - g)
        if not moving or raw is None:
            if not self.ready and self.refused is None:
                self.waiting = 'raw' if raw is None else self.waiting
            return
        self.moving_time += dt
        accel = sample.accel
        if accel is not None and math.isfinite(accel[1]):
            self._lat.add(accel[1], raw, dt)
        if sample.steer is not None:
            self._steer.add(sample.steer, raw, dt)
        self._judge()

    def _judge(self):
        lat, steer = self._lat, self._steer
        corners = lat.spread('x') >= MIN_LAT or steer.spread('x') >= MIN_STEER
        if self.moving_time < MIN_TIME or not corners:
            if not self.ready and self.refused is None:
                self.waiting = 'corners'
            return
        r_lat, r_steer = lat.r(), steer.r()
        fits = [r for r in (r_lat, r_steer) if r is not None]
        self.fit = max(fits, key=abs) if fits else None
        if r_lat is not None and abs(r_lat) >= KEEP:
            self.sign = 1.0 if r_lat > 0 else -1.0
        flat = max(lat.spread('y'), steer.spread('y')) < FLAT
        need = KEEP if self.ready else FIT
        refused = 'flat' if flat else 'not-steering' if self.fit is None or abs(self.fit) < need else None
        if refused != self.refused and refused is not None:
            logging.info("Countersteer FFB: Forza %s: its constant force is %s (r %s): nothing to lighten", self.car,
                         'flat' if refused == 'flat' else 'not its steering force',
                         'none' if self.fit is None else '%.2f' % self.fit)
        elif refused is None and not self.ready:
            logging.info("Countersteer FFB: Forza %s: its constant force follows the car (r %.2f, %s with "
                         "lateral acceleration to the left)", self.car, self.fit,
                         'rising' if self.sign == 1.0 else 'falling' if self.sign == -1.0 else 'unclear')
        self.refused = refused
        self.ready = refused is None
        self.waiting = None

    def gain(self, t):
        """g_eff at t: the last target through the low-pass and the slew limit (call each pass)."""
        if self._g_at is None:
            self._g_at = t
            return self.g
        dt = min(max(t - self._g_at, 0.0), 0.1)
        self._g_at = t
        if dt <= 0.0:
            return self.g
        want = self.g + (self.target - self.g) * (1.0 - math.exp(-dt * 2.0 * math.pi * LOWPASS_HZ))
        step = SLEW * dt
        self.g = min(max(want, self.g - step, G_MIN), self.g + step, 1.0)
        return self.g

    @staticmethod
    def force(raw, g, margin=0.0):
        """ext_force that leaves about g x raw on the wheel: against raw's sign, never more than |raw| - margin
        (a next level within `margin` of raw keeps its sign under it and is never exceeded)."""
        if not raw or g >= 1.0:
            return 0
        cut = (1.0 - min(max(g, 0.0), 1.0)) * max(abs(raw) - max(margin, 0.0), 0.0)
        cut = int(math.floor(cut))
        return -cut if raw > 0 else cut
