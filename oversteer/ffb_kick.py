"""Kickback: the sharp onset of the scrub couple at the moment of contact, in one direction only.

A one-sided kerb hit, a lifted inside wheel or a dirt edge steps Fx_FL - Fx_FR, and a real wheel tugs at that moment
the way the new drag pulls (the sign of -(the Fx step), front_torque's scrub-term convention), then releases back
toward the steady value, never past it. The steady part of the couple is "Drive pull" (RackForce.pull_scrub); this
module adds only the onset. (The first Kickback played the high-pass of the couple: 88 % of its events reversed,
a push followed by an opposite pull, and felt like tugs in the opposite direction.)

A trigger is a front contact-normal angle step of NORMAL_STEP within WINDOW, or a lift onset (one front load under
LIFT_LOW, the other over LIFT_HIGH), together with a step of Fx_FL - Fx_FR of FX_STEP within the same window, over
MIN_SPEED. The pulse is a raised-cosine rise over RISE to an amplitude A = KICK_K * SCRUB * |dFx| / ratio * (1 -
boost slope) (driver Nm), then an exponential decay with DECAY. It never crosses zero: a trigger during a pulse may
only raise the amplitude in the same direction; an opposite one is ignored until the pulse is under REVERSE_OK of
its amplitude. A pulse ends under END of its amplitude; the next fresh one waits GAP more. A gap in the samples
or a new car resets it.

A caller with no Fx and contact normals (Forza) injects its own trigger: `update(..., trigger=dfx)` takes `dfx`, the
step of Fx_FL - Fx_FR (N, signed), for the event in place of the Fx and contact-normal test; `share` asks the rack's
`kick_trigger(sample)` for it (None: no event, and then the Fx and normal test runs as usual).
"""
import collections
import math

from . import ffb_carprops
from . import wheel_torque

SCRUB = 0.0155                           # m: the physical scrub radius (ffb_engine.SCRUB)
KICK_K = 0.20                            # share of the couple's step (1 - boost slope) the driver feels (the old K_FAST)
KICK_CAP = 0.25                          # share of full scale the pulse may add (rally setups keep steady cornering under the
                                         # peak: headroom for the spikes; the 12 ms rise and 60 ms decay keep the thermal mean small)
TOTAL_CAP = wheel_torque.STEER_BUDGET + KICK_CAP   # the steering share plus the pulse is clamped here: 1.00, full scale
OVERALL_KICK = False                     # "Kickback" default
OVERALL_KICK_LEVEL = 50                  # ... and its level, % of the amplitude


class Kick:
    RISE = 0.012                         # s: raised-cosine rise
    DECAY = 0.060                        # s: exponential decay time constant
    WINDOW = 0.050                       # s: the span the steps are taken over
    NORMAL_STEP = 0.15                   # rad: step of the front contact normals' angle
    LIFT_LOW, LIFT_HIGH = 150.0, 1500.0  # N: one front load under / the other over: a lift onset
    FX_STEP = 1500.0                     # N: step of Fx_FL - Fx_FR within the window
    MIN_SPEED = 8.0                      # m/s
    REVERSE_OK = 0.10                    # an opposite trigger counts once the pulse is under this share of its amplitude
    END = 0.01                           # the pulse has ended under this share of its amplitude
    GAP = 0.10                           # s: refractory after a pulse ends
    SAMPLE_GAP = 0.05                    # s without a sample: everything starts again

    def __init__(self):
        self._car = None
        self.pulses = 0                  # fresh pulses started (a raise or a restart in the decay is the same pulse)
        self.reset()

    def reset(self):
        self._t = None
        self._hist = collections.deque()  # (t, normal angle, Fx_FL - Fx_FR, lifted)
        self._active = False
        self._t0 = 0.0
        self._v0 = 0.0                   # the value the rise starts from (signed)
        self._amp = 0.0                  # the amplitude (signed Nm)
        self._quiet_until = -math.inf

    @staticmethod
    def _angle(sample):
        normals = getattr(sample, 'contact_normal', None)
        try:
            a, b = normals[0], normals[1]
            na, nb = math.sqrt(sum(c * c for c in a)), math.sqrt(sum(c * c for c in b))
            if na > 1e-9 and nb > 1e-9:
                return math.acos(min(max(sum(x * y for x, y in zip(a, b)) / (na * nb), -1.0), 1.0))
        except (TypeError, IndexError, ValueError):
            pass
        return None

    def _step(self, t, sample, dfx):
        """The step of Fx_FL - Fx_FR over the window if this sample is a one-sided event, else None."""
        load = getattr(sample, 'tyre_load', None)
        lifted = False
        if load is not None and len(load) >= 2 and load[0] is not None and load[1] is not None:
            lifted = (load[0] < self.LIFT_LOW and load[1] > self.LIFT_HIGH) or (load[1] < self.LIFT_LOW and load[0] > self.LIFT_HIGH)
        angle = self._angle(sample)
        hist = self._hist
        hist.append((t, angle, dfx, lifted))
        while len(hist) > 1 and t - hist[1][0] >= self.WINDOW:
            hist.popleft()               # (the oldest kept is the latest sample at least WINDOW back, if any)
        old = hist[0]
        if t - old[0] < self.WINDOW * 0.5 or t - old[0] > self.WINDOW * 3.0:
            return None
        step = dfx - old[2]
        if abs(step) < self.FX_STEP:
            return None
        normal = angle is not None and old[1] is not None and angle - old[1] >= self.NORMAL_STEP
        if normal or (lifted and not old[3]):
            return step
        return None

    def _value(self, t):
        if not self._active:
            return 0.0
        tau = t - self._t0
        if tau < self.RISE:
            return self._v0 + (self._amp - self._v0) * 0.5 * (1.0 - math.cos(math.pi * tau / self.RISE))
        return self._amp * math.exp(-(tau - self.RISE) / self.DECAY)

    def update(self, t, sample, car, ratio, slope, trigger=None):
        """The pulse (driver Nm, signed like the front torque) for this sample. `slope`: a callable returning the boost
        curve's slope at the sample's column torque (called only when a trigger is taken). `trigger`: an injected
        step of Fx_FL - Fx_FR (N) standing for the event; given, the sample's own Fx and normals are not looked at."""
        if car != self._car or self._t is None or not 0.0 <= t - self._t <= self.SAMPLE_GAP:
            self.reset()
            self._car = car
        self._t = t
        if trigger is not None:
            step = trigger if math.isfinite(trigger) and trigger else None
        else:
            fx = getattr(sample, 'tyre_fx', None)
            try:
                dfx = fx[0] - fx[1]
            except (TypeError, IndexError):
                dfx = math.nan
            if not math.isfinite(dfx):
                return self._value(t)
            step = self._step(t, sample, dfx)
        y = self._value(t)
        if self._active and t - self._t0 >= self.RISE and abs(y) < self.END * abs(self._amp):
            self._active = False
            self._quiet_until = t + self.GAP
            y = 0.0
        speed = getattr(sample, 'speed', None)
        if step is not None and speed is not None and abs(speed) > self.MIN_SPEED and ratio:
            sign = -1.0 if step > 0 else 1.0
            if self._active:
                # (same direction: may raise; opposite: only once the pulse is down)
                allowed = sign * self._amp > 0 or abs(y) < self.REVERSE_OK * abs(self._amp)
            else:
                allowed = t >= self._quiet_until
            if allowed:
                amp = KICK_K * SCRUB * abs(step) / ratio * (1.0 - slope()) * sign
                if self._active and sign * self._amp > 0:
                    if abs(amp) > abs(self._amp):
                        if t - self._t0 < self.RISE:
                            self._amp = amp          # still rising: the target moves
                        else:
                            self._v0, self._t0, self._amp = y, t, amp
                elif abs(amp) > 0.0:
                    self._v0 = y if self._active else 0.0
                    self._t0, self._amp, self._active = t, amp, True
                    self.pulses += 1
                y = self._value(t)
        return y

    def share(self, rack, sample, share, ratio, assist):
        """The steering share (of full scale) plus the pulse, clamped at TOTAL_CAP; `share` itself while the kickback
        is off, at level 0 or without a wheel peak. `rack` is the RackForce (its settings and car)."""
        level = rack.kick_level
        if not rack.kick_on or level <= 0.0:
            self.reset()
            return share
        ratio = rack.ratio if ratio is None else ratio
        cls = rack.assist if assist is None else assist
        peak = rack.wheel_peak or wheel_torque.DEFAULT_PEAK_NM
        speed = getattr(sample, 'speed', None)

        def slope():
            mz = rack_front_torque(rack, sample)
            if mz is None:
                return 1.0
            c = 0.0 if rack.power_steering() == 'off' else cls * rack.assist_level()
            return ffb_carprops.driver_slope(mz / ratio, speed, c, not rack.classic())

        kick_trigger = getattr(rack, 'kick_trigger', None)
        nm = self.update(rack._now, sample, rack.car, ratio, slope, kick_trigger(sample) if kick_trigger else None)
        add = max(-KICK_CAP, min(KICK_CAP, nm / peak * rack.strength * level))
        if add == 0.0:
            return share
        return max(-TOTAL_CAP, min(TOTAL_CAP, share + add))


def rack_front_torque(rack, sample):
    from . import ffb_engine
    front = getattr(rack, '_front', None)
    if front is not None:
        return front(sample, rack.trail, 0.0)
    return ffb_engine.front_torque(sample, rack.trail, 0.0, clamp=True)
