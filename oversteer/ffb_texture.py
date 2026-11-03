"""Countersteer FFB texture: small vibrations from the telemetry, added on
top of the steering force (ffb_engine.RackForce) while Countersteer plays
it. marth: "any rumble very subtle, it is disruptive if strong": every
part is capped at a few % of full scale, their sum at TOTAL_CAP and at a
quarter of the steering force now (plus a small floor), and it all goes
through a master level (default 30 %).

Four parts, each from the sample in hand (ACR through bridge version 5,
333 a second, one per physics step):

- road: the front suspension's velocity (d/dt travel, FL minus FR: a bump
  under one wheel steers, both at once mostly does not), band-passed
  8 to 60 Hz so body roll and pitch never reach the wheel, through tanh
  to its cap.
- impact: a landing or a hit, from the front wheels' load rising by more
  than its mean within 35 ms, either out of the air (the load under 15 %
  of its mean first) or with the contact normals stepping (a ledge, a
  rock): one pulse, rate-limited, of zero net area (it never pulls): a short half-sine toward the wheel
  that came down (the step of Fx_FL - Fx_FR gives the side) and a slow return.
- lock: the front wheels locking under braking (slip ratio past 0.3 with
  the brake on): a 40 Hz sine only while they lock. From the wheels'
  speed against the car's (1 - wheel speed / speed, a ratio whatever the
  game's slip unit; the radius learnt while they roll free), the game's
  own slip ratio until then. ACR leaves its ABS and kerb vibrations at 0:
  not used.
- engine: the engine's firing vibration through the column, from the rpm
  (class EngineHum). Rev-aware: its frequency is the firing frequency
  rpm / 60 * cylinders / 2 (a four-stroke fires every cylinder once in two
  turns), its amplitude highest at idle and with the throttle open, lowest
  at the top of the revs on a closed throttle. It stays while the car
  stands (a real idle shakes the column) and goes when the engine is off.
- surface (gravel, kerbs): not built; ACR sends no surface. It would be
  one more part here.

No DC: the sum of the parts goes through a DC blocker (DC_HZ high-pass)
before the limiter. The parts alone need not average to zero: tanh after
the road's band-pass turns a lopsided input (a kerb's sawtooth under one
wheel) into a steady pull, which the blocker takes out. The limiter is a
gain (a peak follower), not clipping, so it adds no DC of its own; its
limit is also kept within the headroom FULL - |steering force|, so the
engine's clamp never cuts one side of it. No steps: it ramps in over ONSET after any start (or a gap), and the level
and the parts' switches slew over SLEW.

Games: TEXTURE_GAMES only. The hook for others is that list: a game whose
telemetry arrives at 60 a second would have it 16 ms late and alias the
road band, so it needs its own parts (or none) first. Richard Burns Rally:
the road part from the strut velocity (susp_vel, FL minus FR) and the impact
from the tyre load, stepped with each telemetry sample; no lock part (it has
no wheel speeds); the engine hum from the rpm."""
import math

FULL = 32767                             # ext_force full scale (ffb_engine.FULL)

TEXTURE_GAMES = ('acr', 'rbr')           # the games it is tuned for (the hook for others)
NO_LOCK_GAMES = ('rbr',)                 # ... of which these send no wheel speeds: the lock part is off
SUSP_VEL_GAMES = ('rbr',)                # ... and these send the strut velocity itself (no travel to differentiate)

PARTS = ('road', 'impact', 'lock', 'engine')
LEVEL = 0.30                             # the master level's default (0..1)
TOTAL_CAP = 0.08                         # share of full scale: the sum of every part, at level 100 %
STEER_SHARE = 0.25                       # ... and never past this share of |steering force| now ...
FLOOR = 0.015                            # ... plus this (of full scale), so a straight keeps a little
DC_HZ = 4.0                              # Hz: the sum's DC blocker (after every nonlinearity but the limiter)
LIMIT_RELEASE = 0.3                      # s: the limiter's peak follower lets go over this
ONSET = 0.3                              # s: it ramps in after a start or a gap
SLEW = 0.3                               # s: the level and the switches move 0 -> 1 in this
GAP = 0.05                               # s between samples past which the filters start again
MIN_SPEED = 2.0                          # m/s: no texture standing (it ramps with speed up to 2x this)


def _finite_num(x):
    return x is not None and math.isfinite(x)


def _finite(values, n):
    """The first n values as a tuple of finite floats, or None."""
    if values is None or len(values) < n:
        return None
    out = tuple(float(v) for v in values[:n])
    return out if all(math.isfinite(v) for v in out) else None


class _HighPass:
    """First-order high-pass, dt-aware (the engine may skip samples)."""

    def __init__(self, hz):
        self.rc = 1.0 / (2.0 * math.pi * hz)
        self.reset()

    def reset(self):
        self.x = None
        self.y = 0.0

    def step(self, x, dt):
        if self.x is None:
            self.x, self.y = x, 0.0
            return 0.0
        a = self.rc / (self.rc + dt)
        self.y = a * (self.y + x - self.x)
        self.x = x
        return self.y


class _LowPass:
    def __init__(self, hz):
        self.rc = 1.0 / (2.0 * math.pi * hz)
        self.reset()

    def reset(self):
        self.y = None

    def step(self, x, dt):
        if self.y is None:
            self.y = x
        else:
            self.y += (x - self.y) * dt / (self.rc + dt)
        return self.y


class RoadTexture:
    """Front suspension velocity, FL minus FR, band-passed: ext_force units."""

    HP_HZ = 8.0                          # two first-order stages: body motion (1-3 Hz) is 20+ dB down
    LP_HZ = 60.0                         # the float travel's step noise, differentiated, above this
    V_MAX = 3.0                          # m/s: a per-sample velocity past this is a reset or a teleport (seen: 89)
    V_REF = 0.25                         # m/s band-passed for tanh(1) of the cap
    CAP = 0.04

    def __init__(self):
        self._hp1, self._hp2, self._lp = _HighPass(self.HP_HZ), _HighPass(self.HP_HZ), _LowPass(self.LP_HZ)
        self.reset()

    def reset(self):
        self._prev = None
        self._hp1.reset()
        self._hp2.reset()
        self._lp.reset()

    def step(self, dt, sample):
        if sample.game in SUSP_VEL_GAMES:
            vel = _finite(sample.susp_vel, 2)
            if vel is None:
                self.reset()
                return 0.0
            vl = min(max(vel[0], -self.V_MAX), self.V_MAX)
            vr = min(max(vel[1], -self.V_MAX), self.V_MAX)
        else:
            travel = _finite(sample.susp, 2)
            if travel is None:
                self.reset()
                return 0.0
            prev, self._prev = self._prev, travel
            if prev is None:
                return 0.0
            vl = min(max((travel[0] - prev[0]) / dt, -self.V_MAX), self.V_MAX)
            vr = min(max((travel[1] - prev[1]) / dt, -self.V_MAX), self.V_MAX)
        x = self._lp.step(self._hp2.step(self._hp1.step(vl - vr, dt), dt), dt)
        return self.CAP * FULL * math.tanh(x / self.V_REF)


class ImpactPulse:
    """A landing or a hit: one pulse of zero net area. Its direction is the step of Fx_FL - Fx_FR at the event
    (the landing wheel's spin-up drag pulls the rack toward that wheel: the sign of the front torque's scrub term,
    -SCRUB * (Fx_FL - Fx_FR), with SCRUB > 0): a one-sided half-sine of LEAD s, then a slow half-sine of RETURN s
    the other way with the same area, so the opposite part is spread thin (its peak LEAD / RETURN of the first
    lobe). Without a step of at least FX_STEP N (or without Fx) the sign is not known: a symmetric pair of
    half-sines, NEUTRAL of the size. (The earlier damped 25 Hz zero-area shape had its largest lobe against its
    sign, and the sign was chance: audit of 20261102-235822.)"""

    WINDOW = 0.035                       # s: the load rise is measured over this
    RISE = 1.0                           # front load rising by its mean (x1) within WINDOW ...
    AIR = 0.15                           # ... from under this share of its mean (a landing) ...
    NORMAL_STEP = 0.15                   # ... or with the contact normals turning this far (rad): a ledge, a rock
    MEAN_TAU = 2.0                       # s: the front load's mean
    MIN_LOAD = 500.0                     # N: no mean this low is a car on its wheels
    HOLD = 0.4                           # s from one pulse to the next at the soonest
    LEAD = 0.018                         # s: the first lobe
    RETURN = 0.10                        # s: the slow return (peak LEAD / RETURN of the first lobe)
    LENGTH = LEAD + RETURN
    FX_STEP = 150.0                      # N: the step of Fx_FL - Fx_FR that gives the pulse a direction
    NEUTRAL = 0.5                        # share of the size of the symmetric pulse without one
    CAP = 0.05
    LOW = 0.3                            # share of the cap at RISE; full at RISE + 2

    def __init__(self):
        self._car = None
        self.reset()
        self.count = 0                   # pulses (tests, the log)

    def reset(self):
        self._window = []                # (t, front load, FL, FR, normal, Fx_FL - Fx_FR or None) over WINDOW
        self._mean = None
        self._last = None
        self._t = 0.0
        self._pulse = None               # (start, amplitude with its sign)

    def step(self, dt, sample):
        if sample.car != self._car:
            self._car = sample.car
            self.reset()                 # another car: another mean front load
        self._t = t = self._t + dt
        load = _finite(sample.tyre_load, 2)
        normals = sample.contact_normal
        normal = None
        if normals is not None and len(normals) >= 2:
            a, b = _finite(normals[0], 3), _finite(normals[1], 3)
            if a is not None and b is not None:
                n = tuple(x + y for x, y in zip(a, b))
                size = math.sqrt(sum(x * x for x in n))
                if size > 1e-6:
                    normal = tuple(x / size for x in n)
        fx = _finite(sample.tyre_fx, 2) if getattr(sample, 'tyre_fx', None) is not None else None
        couple = fx[0] - fx[1] if fx is not None else None
        if load is not None:
            front = max(load[0], 0.0) + max(load[1], 0.0)
            if front > 1e6:              # past any car: a glitch (NaN, a reset)
                load = None
        if load is None:
            self._window = []
        else:
            self._mean = front if self._mean is None else self._mean + (front - self._mean) * dt / (self.MEAN_TAU + dt)
            window = self._window
            window.append((t, front, load[0], load[1], normal, couple))
            while window[0][0] < t - self.WINDOW:
                window.pop(0)
            self._detect(t, front, load)
        return self._value(t)

    def _detect(self, t, front, load):
        mean = self._mean
        if mean is None or mean < self.MIN_LOAD or (self._last is not None and t - self._last < self.HOLD):
            return
        window = self._window
        low = min(w[1] for w in window)
        rise = (front - low) / mean
        if rise < self.RISE:
            return
        landing = low < self.AIR * mean
        edge = False
        first, now = window[0][4], window[-1][4]
        if first is not None and now is not None:
            dot = min(max(sum(x * y for x, y in zip(first, now)), -1.0), 1.0)
            edge = math.acos(dot) >= self.NORMAL_STEP
        if not (landing or edge):
            return
        share = min(max(self.LOW + (1.0 - self.LOW) * (rise - self.RISE) / 2.0, self.LOW), 1.0)
        # Its direction from the step of Fx_FL - Fx_FR over the window (the largest departure from its start): the
        # landing wheel's spin-up drag, sign -SCRUB * step like the front torque's scrub term
        sign = 0.0
        couples = [w[5] for w in window]
        if couples[0] is not None:
            steps = [c - couples[0] for c in couples if c is not None]
            step = max(steps, key=abs)
            if abs(step) >= self.FX_STEP:
                sign = -1.0 if step > 0 else 1.0
        self._pulse = (t, sign, share * self.CAP * FULL)
        self._last = t
        self.count += 1

    def _value(self, t):
        if self._pulse is None:
            return 0.0
        start, sign, amplitude = self._pulse
        age = t - start
        if age >= self.LENGTH:
            self._pulse = None
            return 0.0
        if sign == 0.0:                  # direction unknown: two equal lobes, one each way
            amplitude *= self.NEUTRAL
            if age < self.LEAD:
                return amplitude * math.sin(math.pi * age / self.LEAD)
            return -amplitude * math.sin(math.pi * (age - self.LEAD) / self.LEAD) if age < 2 * self.LEAD else 0.0
        if age < self.LEAD:
            return sign * amplitude * math.sin(math.pi * age / self.LEAD)
        # Same area as the first lobe, over RETURN
        return -sign * amplitude * (self.LEAD / self.RETURN) * math.sin(math.pi * (age - self.LEAD) / self.RETURN)


class LockCue:
    """The front wheels locking under braking: a FREQ Hz sine whose
    envelope follows how far past SLIP_START they slip (attack, release).

    The slip is 1 - wheel speed / car speed, the larger of the fronts.
    ACR leaves the tyre radius empty, so the wheel speed is the wheels'
    rotation times a radius learnt here while they roll free (off the
    brake, the two fronts turning together); the game's own slip ratio
    (its unit unconfirmed) is used only until that is learnt."""

    BRAKE = 0.1                          # brake pedal (0..1) at or past this
    SPEED = 5.0                          # m/s: slower, a ratio to the car's speed is noise
    # Past a tyre's peak braking slip (tarmac 0.1-0.15, gravel 0.2-0.3). marth's ACR gravel capture
    # (2026-10-05, 26 min, 60/s): braking at 0.5 the median front slip is 0.30, at full brake 0.73
    SLIP_START = 0.3
    SLIP_FULL = 0.8
    FREQ = 40.0
    ATTACK = 0.03                        # s
    RELEASE = 0.06
    CAP = 0.03
    LEARN_SPEED = 8.0                    # m/s: the radius is learnt above this ...
    LEARN_BRAKE = 0.05                   # ... off the brake ...
    LEARN_SAME = 0.03                    # ... with the fronts turning within 3 % of each other
    LEARN_TAU = 3.0                      # s
    LEARN_TIME = 1.0                     # s of rolling before the learnt radius is used
    RADIUS = (0.15, 0.6)                 # m: a car's

    def __init__(self):
        self._car = None
        self._forget()
        self.reset()

    def _forget(self):
        self.radius = None
        self._learnt = 0.0

    def reset(self):
        self._env = 0.0
        self._phase = 0.0

    def _learn(self, dt, sample, speed, rot):
        brake = sample.brake
        if speed < self.LEARN_SPEED or brake is None or not brake < self.LEARN_BRAKE:
            return
        mean = (rot[0] + rot[1]) / 2.0
        if mean <= 0.0 or abs(rot[0] - rot[1]) > self.LEARN_SAME * mean:
            return
        radius = speed / mean
        if not self.RADIUS[0] <= radius <= self.RADIUS[1]:
            return
        self.radius = radius if self.radius is None else self.radius + (radius - self.radius) * dt / (self.LEARN_TAU + dt)
        self._learnt += dt

    @staticmethod
    def ground(sample):
        """The car's speed along itself (m/s): the local velocity's forward
        part where the game sends it (in a slide the wheels roll at that,
        not at the speed), else the speed; None if neither."""
        vel = _finite(sample.vel, 1)
        if vel is not None:
            return vel[0]
        speed = sample.speed
        return speed if speed is not None and math.isfinite(speed) else None

    def slip(self, sample, dt=0.0):
        """The fronts' lock as a ratio (0 rolling, 1 locked), the larger of
        the two, or None. `dt`: learn the radius from this sample too."""
        if sample.car != self._car:
            self._car = sample.car
            self._forget()
        speed = self.ground(sample)
        if speed is None or speed < self.SPEED:
            return None
        wheels = _finite(sample.wheel_speed, 2)
        if wheels is None:
            rot = _finite(sample.wheel_rot, 2)
            if rot is not None:
                if dt > 0.0:
                    self._learn(dt, sample, speed, rot)
                if self.radius is not None and self._learnt >= self.LEARN_TIME:
                    wheels = tuple(r * self.radius for r in rot)
        if wheels is not None:
            return max(1.0 - abs(w) / speed for w in wheels)
        ratio = _finite(sample.tyre_slip_ratio, 2)
        if ratio is not None:
            return max(abs(r) for r in ratio)
        return None

    def target(self, sample, dt=0.0):
        slip = self.slip(sample, dt)                 # first: it learns off the brake too
        brake = sample.brake
        if slip is None or brake is None or not math.isfinite(brake) or brake < self.BRAKE:
            return 0.0
        return min(max((slip - self.SLIP_START) / (self.SLIP_FULL - self.SLIP_START), 0.0), 1.0)

    def step(self, dt, sample):
        target = self.target(sample, dt)
        tau = self.ATTACK if target > self._env else self.RELEASE
        self._env += (target - self._env) * dt / (tau + dt)
        if self._env < 1e-3 and target == 0.0:
            self._env = 0.0
            self._phase = 0.0            # a fresh lock starts its sine at 0
            return 0.0
        self._phase = (self._phase + 2.0 * math.pi * self.FREQ * dt) % (2.0 * math.pi)
        return self.CAP * FULL * self._env * math.sin(self._phase)


class EngineHum:
    """The engine's firing vibration, rev-aware and zero-mean (a sine).

    - Frequency: rpm / 60 * cylinders / 2 (firing frequency of a
      four-stroke); the cylinders from the telemetry where the game sends
      them (Forza), else CYLINDERS. A 4-cylinder idles at ~27 Hz and
      reaches 200 Hz at 6000 rpm.
    - What the wheel can render: the driver writes the force on a 2 ms
      timer (250 Hz sample rate, 125 Hz Nyquist) and the telemetry steps at
      3 ms, so anything above RENDER_MAX_HZ is folded down by whole octaves
      (200 Hz plays as 100 Hz) rather than aliased to a beat; the phase is
      continuous across a fold, so there is no step. The G29's motors and
      its gears are mushy past ~100 Hz anyway, and the amplitude is already
      low there. Below RENDER_MIN_HZ (a cranking engine) nothing plays.
    - Amplitude: CAP * shape(rpm) * load. shape is 1 at idle falling to
      REV_FLOOR at the redline (idle roughness is the strongest shake a
      real column carries; at speed the mounts and the balance shafts take
      it out); load is LOAD_FLOOR with the throttle shut up to 1 fully open.
      Smoothed over AMP_TAU so a blip of the pedal is not a click.
    - Standing: it plays at speed 0 (Texture does not gate it by speed). Off
      when the sample says the engine is not running, or has no rpm."""

    CAP = 0.012                          # share of full scale at idle, throttle open (~1.2 % of the wheel's force)
    CYLINDERS = 4
    RENDER_MAX_HZ = 100.0
    RENDER_MIN_HZ = 8.0
    IDLE_FRACTION = 0.12                 # of max rpm, when the game sends no idle rpm
    MAX_RPM = 7000.0                     # when the game sends no max rpm
    REV_FLOOR = 0.35
    LOAD_FLOOR = 0.6
    AMP_TAU = 0.15                       # s

    def __init__(self):
        self.reset()
        self.freq = 0.0                  # Hz as rendered (tests, the log)

    def reset(self):
        self._phase = 0.0
        self._amp = 0.0

    def frequency(self, sample):
        """The firing frequency (Hz) of the sample's rpm, or None."""
        rpm = sample.rpm
        if not _finite_num(rpm) or rpm <= 0.0:
            return None
        cylinders = getattr(sample, 'cylinders', None)
        if not cylinders or not 0 < cylinders < 32:
            cylinders = self.CYLINDERS
        return rpm / 60.0 * cylinders / 2.0

    @classmethod
    def render_frequency(cls, hz):
        """Folded down by octaves to at most RENDER_MAX_HZ."""
        while hz > cls.RENDER_MAX_HZ:
            hz /= 2.0
        return hz

    def target(self, sample):
        """The amplitude (share of CAP) the sample asks for, 0 when the engine is off."""
        hz = self.frequency(sample)
        if hz is None or sample.running is False or self.render_frequency(hz) < self.RENDER_MIN_HZ:
            return 0.0
        rpm = sample.rpm
        top = sample.max_rpm if _finite_num(sample.max_rpm) and sample.max_rpm > 0.0 else self.MAX_RPM
        idle = sample.idle_rpm if _finite_num(sample.idle_rpm) and sample.idle_rpm > 0.0 else self.IDLE_FRACTION * top
        span = max(top - idle, 1.0)
        rev = min(max((rpm - idle) / span, 0.0), 1.0)
        shape = 1.0 - (1.0 - self.REV_FLOOR) * rev
        throttle = sample.throttle if _finite_num(sample.throttle) else 0.0
        return shape * (self.LOAD_FLOOR + (1.0 - self.LOAD_FLOOR) * min(max(throttle, 0.0), 1.0))

    def step(self, dt, sample):
        target = self.target(sample)
        self._amp += (target - self._amp) * dt / (self.AMP_TAU + dt)
        if target == 0.0 and self._amp < 1e-3:
            self._amp = 0.0
            self.freq = 0.0
            return 0.0
        hz = self.frequency(sample)
        if hz is not None:
            self.freq = self.render_frequency(hz)
        self._phase = (self._phase + 2.0 * math.pi * self.freq * dt) % (2.0 * math.pi)
        return self.CAP * FULL * self._amp * math.sin(self._phase)


class Texture:
    """The parts, summed, limited and levelled. force() per
    sample while Countersteer plays; 0.0 exactly when off (the steering
    force then goes out bit for bit as it did without it)."""

    def __init__(self):
        self.road, self.impact, self.lock = RoadTexture(), ImpactPulse(), LockCue()
        self.engine = EngineHum()
        self._env = 0.0                  # the limiter's peak follower of |sum|
        self._dc = _HighPass(DC_HZ)
        self.level = LEVEL               # requests (the GUI's thread); read here per sample
        self.parts = dict.fromkeys(PARTS, True)
        self._gains = dict.fromkeys(PARTS, 0.0)
        self._level = 0.0                # the slewed level
        self._last = None
        self._since = None               # when it last started (the onset ramp)
        self._out = 0.0                  # the last value sent
        self.peak = 0.0                  # the largest |texture| sent (tests, the log)

    def set(self, level=None, parts=None):
        """The master level (0..1) and which parts are on (a dict); applied smoothly."""
        if level is not None:
            self.level = min(max(float(level), 0.0), 1.0) if math.isfinite(level) else 0.0
        if parts is not None:
            self.parts = {name: bool(parts.get(name, self.parts.get(name, True))) for name in PARTS}

    def describe(self):
        """For the log: 'off' or the level and the parts on."""
        if not self.enabled():
            return 'off'
        return '%.0f %% (%s)' % (self.level * 100.0, ', '.join(name for name in PARTS if self.parts.get(name)))

    def enabled(self):
        return self.level > 0.0 and any(self.parts.values())

    def reset(self):
        self.road.reset()
        self.impact.reset()
        self.lock.reset()
        self.engine.reset()
        self._env = 0.0
        self._dc.reset()
        self._last = self._since = None
        self._out = 0.0
        self._level = 0.0
        self._gains = dict.fromkeys(PARTS, 0.0)

    def force(self, t, sample, steering):
        """The texture to add to `steering` (ext_force units, before the
        engine's fade gain) for this sample at time t."""
        if sample.game not in TEXTURE_GAMES or not (self.enabled() or self._level > 0.0):
            self.reset()
            return 0.0
        last, self._last = self._last, t
        dt = None if last is None else t - last
        if dt is None or not 0.0 < dt <= GAP:
            if dt is not None and dt <= 0.0 and self._since is not None:
                self._last = last
                return self._out         # the same instant again: the value stands (no drop to 0)
            self.reset()
            self._last = self._since = t
            return 0.0
        step = dt / SLEW
        self._level += min(max(self.level - self._level, -step), step)
        for name in PARTS:
            want = 1.0 if self.parts.get(name) else 0.0
            self._gains[name] += min(max(want - self._gains[name], -step), step)
        gains = self._gains
        speed = sample.speed if sample.speed is not None and math.isfinite(sample.speed) else 0.0
        moving = min(max((speed - MIN_SPEED) / MIN_SPEED, 0.0), 1.0)
        onset = min((t - self._since) / ONSET, 1.0)
        # The road, the landings and the lock need the car rolling; the engine's idle shake does not
        lock = 0.0 if sample.game in NO_LOCK_GAMES else gains['lock'] * self.lock.step(dt, sample)
        total = (gains['road'] * self.road.step(dt, sample) + gains['impact'] * self.impact.step(dt, sample)
                 + lock) * moving \
            + gains['engine'] * self.engine.step(dt, sample)
        steering = abs(steering) if math.isfinite(steering) else 0.0
        total = self._dc.step(total, dt)
        # Within the headroom left by the steering force, so the engine's clamp never cuts one side
        limit = min(TOTAL_CAP * FULL, STEER_SHARE * steering + FLOOR * FULL, max(FULL - steering, 0.0))
        # The limiter: a gain from the peak follower, so the shape is kept and |value| <= limit
        self._env = max(abs(total), self._env * math.exp(-dt / LIMIT_RELEASE))
        gain = limit / self._env if self._env > limit else 1.0     # limit 0: env > 0 gives 0, env 0 gives total 0
        value = total * gain * self._level * onset
        if not math.isfinite(value):
            self.reset()
            return 0.0
        if abs(value) > self.peak:
            self.peak = abs(value)
        self._out = value
        return value
