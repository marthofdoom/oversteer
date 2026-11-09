"""Forza Horizon through Countersteer's own chain (ffb_engine.ForzaRack): the parts that are not the chain.

Forza sends no tyre forces, so the front kingpin torque comes from Newton (analysis ~/.cache/ffb-fh6, capture
20261030-205514: 4 cars, 89,000 packets at ~60/s):

- front_lateral: Fy_f = m (b/L) (a_y + DI a rdot), the front axle's share of the lateral force plus the yaw
  acceleration's (a = L (1 - b/L), the CG-to-front-axle distance; DI 1.0, the whole rdot term). a_y is the lateral
  acceleration to the left (clipped to +-30), rdot the yaw acceleration (central difference of the yaw rate over the
  game's clock, clipped to +-15 rad/s^2). b/L is a prior per drivetrain (the weight on the front axle).
- kingpin: T = -(caster + tp(s)) Fy_f in front_torque's sign convention, caster 0.027 m, tp the pneumatic trail that
  fades with Forza's normalised combined slip s (the mean of the two fronts): tp(s) = 0.028 / (1 + (s / 1.4)^4).
- MassLearner: the car's mass from its own acceleration, per car ordinal, stored with ffb_carprops.STORE. Under power
  the wheels' force is P / v = m a + c0 + c2 v^2 (+ the engine's rotating inertia): with the clean samples (steady
  gear, no brake or clutch, little slip, on the springs, little lateral acceleration) the regression of P / v on a,
  after partialling out [1, v^2], gives m. Errors-in-variables: the geometric mean of the regression of y on a and
  of a on y.
- OutputFilter: Forza's packets come at the game's frame rate (a staircase at 60/s), the wheel plays every 1-2 ms. A
  critically damped second-order low-pass stepped every engine pass, its corner following the measured packet rate
  (a third of it, 15 to 60 Hz: 20 Hz at 60/s), then a slew limit proportional to the rate (0.15 of full scale per
  10 ms at 60/s). No extrapolation: it lags the packets by about a packet's interval and is smooth."""
import collections
import math
import re

from . import ffb_carprops

G = 9.81
WHEELBASE = ffb_carprops.WHEELBASE       # m: ASSUMED (telemetry has none)
BL = {'fwd': 0.62, 'rwd': 0.53, 'awd': 0.58}     # b/L priors: the weight share on the front axle, by drivetrain
BL_DEFAULT = 0.58
DI = 1.0                                 # the yaw-acceleration term's weight
AY_MAX = 30.0                            # m/s^2
RDOT_MAX = 15.0                          # rad/s^2
CASTER = 0.027                           # m: the mechanical trail
# ACR, Skoda Fabia, 15 stretches of 60 s: the four tyres' summed side force over m a_y (m from the tyre loads) follows the grip,
# ratio = 0.654 + 0.240 mu (r 0.79), mu the stretch's p95 of |a_y| / g: about 0.85 on loose (mu .8-.95), 0.96 on tarmac (mu 1.1-1.35).
# Not the speed (aero under 5 %), not the steering angle. The rack feels only the tyres' force, so Fy_f carries that ratio.
LATERAL_FIT = (0.654, 0.240)
LATERAL_RANGE = (0.85, 0.98)
LATERAL_DEFAULT = 0.92                   # until the grip is known
TP0 = 0.042                              # m: the pneumatic trail at no slip (37 + 42 mm matched ACR once the wheel curve stopped over-delivering)
TP_S_HALF = 0.7                          # normalised combined slip at which it has halved
TP_N = 4.0
FORZA_TP_S_HALF = 1.1                   # Forza's own slip scale (its grip peaks at 0.7-0.9 there): halved at 1.1 its rim torque per Fy_f
                                         # follows ACR's tyres (Fabia) within a few % from 0 to 3 times the peak slip; at 0.7 it faded 15 % early


def power_index(sample):
    """The tune's PI from the packet's `car_class` ('class:5 pi:884 group:26'), None without one. A tune edit changes it."""
    m = re.search(r'\bpi:(\d+)', sample.car_class or '')
    return int(m.group(1)) if m else None


def finite(x):
    return x is not None and math.isfinite(x)


def trail(s, half=None):
    """The pneumatic trail (m) at the normalised slip s: TP0 / (1 + (s / half)^TP_N), half TP_S_HALF unless given
    (read at the call, so a changed constant reaches it)."""
    return TP0 / (1.0 + (abs(s) / (TP_S_HALF if half is None else half)) ** TP_N)


def front_share(drivetrain):
    """b/L, the weight share on the front axle, from the drivetrain."""
    return BL.get(drivetrain, BL_DEFAULT)


def lateral_factor(mu):
    """The tyres' side force over m a_y at the grip mu (LATERAL_FIT, clipped to LATERAL_RANGE); LATERAL_DEFAULT without mu."""
    if not finite(mu):
        return LATERAL_DEFAULT
    return min(max(LATERAL_FIT[0] + LATERAL_FIT[1] * mu, LATERAL_RANGE[0]), LATERAL_RANGE[1])


class GripScale:
    """The grip mu the car is being driven at: the p95 of 1 s maxima of |a_y| over the last WINDOW s, over g. None until HOLD s of
    maxima are held. Not fed under V_MIN; a gap over GAP s clears it."""

    WINDOW = 60.0                        # s
    HOLD = 20.0                          # s of maxima before mu() answers
    GAP = 2.0                            # s
    V_MIN = 8.0                          # m/s
    PCT = 95.0

    def __init__(self):
        self.reset()

    def reset(self):
        self._max = collections.deque()  # [second, max |a_y|], oldest first
        self._at = None

    def feed(self, t, ay, speed=None):
        if not (finite(t) and finite(ay)) or (speed is not None and (not finite(speed) or speed < self.V_MIN)):
            return
        if self._at is not None and (t - self._at > self.GAP or t < self._at):
            self._max.clear()
        self._at = t
        sec = math.floor(t)
        a = abs(ay)
        if self._max and self._max[-1][0] == sec:
            if a > self._max[-1][1]:
                self._max[-1][1] = a
        else:
            self._max.append([sec, a])
        while self._max and self._max[0][0] < sec - self.WINDOW:
            self._max.popleft()

    def mu(self):
        if len(self._max) < self.HOLD:
            return None
        v = sorted(m for _s, m in self._max)
        k = (len(v) - 1) * self.PCT / 100.0
        i = int(k)
        x = v[i] + (v[min(i + 1, len(v) - 1)] - v[i]) * (k - i)
        return x / G


def front_lateral(mass, bl, ay, rdot, wheelbase=WHEELBASE, di=DI, factor=LATERAL_DEFAULT):
    """Fy_f (N, left positive): factor m (b/L) (a_y + di a rdot), a = wheelbase (1 - b/L); a_y and rdot clipped."""
    ay = min(max(ay, -AY_MAX), AY_MAX)
    rdot = min(max(rdot, -RDOT_MAX), RDOT_MAX)
    return factor * mass * bl * (ay + di * wheelbase * (1.0 - bl) * rdot)


def kingpin(fy, s, tp_scale=1.0, caster=CASTER, half=None):
    """The kingpin torque (Nm) in front_torque's sign: -(caster + tp(s)) Fy_f. `tp_scale` scales the pneumatic part
    (0.5 with a puddle under both fronts)."""
    return -(caster + tp_scale * trail(s, FORZA_TP_S_HALF if half is None else half)) * fy


class LowPass:
    """A causal low-pass at `hz`, first or second order (Butterworth), exact for an input held over each (irregular) step dt
    (the same closed form as RbrRack._low_pass). A gap over `gap` s (or time going back) starts it again at the input."""

    def __init__(self, hz, order=2, gap=0.1):
        self.hz, self.order, self.gap = float(hz), int(order), gap
        self.reset()

    def reset(self):
        self.y = self.v = self.at = None

    def step(self, t, u):
        last, self.at = self.at, t
        dt = 0.0 if last is None else t - last
        if self.y is None or dt < 0.0 or dt > self.gap:
            self.y, self.v = u, 0.0
            return u
        if dt == 0.0:
            return self.y
        w = 2.0 * math.pi * self.hz
        if self.order == 1:
            self.y += (1.0 - math.exp(-w * dt)) * (u - self.y)
            return self.y
        a = w / math.sqrt(2.0)                    # z w, and w_d: the same at z = 1/sqrt 2
        decay = math.exp(-a * dt)
        c, sn = math.cos(a * dt), math.sin(a * dt)
        e0, v0 = self.y - u, self.v
        b = (v0 + a * e0) / a
        self.y = u + decay * (e0 * c + b * sn)
        self.v = decay * (v0 * c - (w * w * e0 + a * v0) / a * sn)
        return self.y


class _Box:
    """A causal moving average over the last `width` s."""

    def __init__(self, width):
        self.width = width
        self.clear()

    def clear(self):
        self.items = collections.deque()
        self.sum = 0.0

    def add(self, t, x):
        self.items.append((t, x))
        self.sum += x
        while len(self.items) > 1 and t - self.items[0][0] > self.width:
            self.sum -= self.items.popleft()[1]

    def mean(self):
        return self.sum / len(self.items) if self.items else 0.0


def _solve2(a, b, c, rhs0, rhs1):
    """[[a, b], [b, c]]^-1 [rhs0, rhs1], None when singular."""
    det = a * c - b * b
    if abs(det) < 1e-18 * max(abs(a * c), 1e-300):
        return None
    return ((c * rhs0 - b * rhs1) / det, (a * rhs1 - b * rhs0) / det)


class MassLearner:
    """The car's mass from its own acceleration (see the module's docstring), updated with every sample.

    Gates (a sample counts only when all hold, and its smoothing window of SMOOTH s is clean): speed over V_MIN;
    brake, clutch and handbrake 0; the gear unchanged within +-GEAR_WINDOW s (the samples wait that long before they
    count, so the future half is seen); every wheel's normalised slip angle under SLIP_ANGLE and slip ratio under
    SLIP_RATIO; every normalised suspension travel inside SUSP; |a_y| under AY_MAX_G; no gap over GAP s. P / v and
    a = dv/dt + g grade (grade from the height, clipped to +-0.3) are averaged causally over SMOOTH s. The moments
    are exponentially weighted over TAU s; [1, v^2] is partialled out, the engine's inertia J (w_e / v)^2 a taken
    off, and m = sqrt(m_ols m_reverse). Accepted when r >= R_MIN, sd(a) >= SD_MIN and QUALIFY_S s have qualified (counted
    without the decay);
    clamped to LIMITS. Without a mass the car has not been learnt (from `prior`: PRIOR): the mass is slewed SLEW kg/s toward the
    accepted estimate until it gets there. From then on, and from the start with a stored mass, the mass is held, and every
    UPDATE_S s of driving (speed over V_MIN) the next accepted estimate moves it by at most UPDATE_STEP of itself toward the
    estimate (`moved` then holds (old, new, estimate) for the caller to log and save): one bad stretch cannot swing it."""

    PRIOR = 1400.0
    LIMITS = (700.0, 2600.0)
    SLEW = 50.0                          # kg/s
    TAU = 120.0                          # s: the moments' memory
    SMOOTH = 0.15                        # s
    J = 0.3                              # kg m^2: the engine's inertia, a prior
    V_MIN = 8.0                          # m/s
    SLIP_ANGLE = 0.5
    SLIP_RATIO = 0.3
    SUSP = (0.01, 0.99)
    AY_MAX_G = 6.0                       # m/s^2: (3 left 10 s of 20 qualifying in 215 s of 20261105-202640's car 2470 and 8 s of ACR's Fabia, 20261105-162134; the estimate is flat from 6 to 10: 2470 1417-1423 kg, ACR 925-940 kg on its tyre-force input with J 0.3, wrongly: tyre Fx is past the engine, and with J 0 three ACR drives read 1268-1314 kg against 1307 from the tyres' Fy over ay; 6 is the first to qualify both)
    GAP = 0.05                           # s
    GEAR_WINDOW = 0.15                   # s
    GRADE_MAX = 0.3
    R_MIN = 0.75                         # (0.85 refused the 207, r 0.76, leaving it on the 1400 kg prior: 23 % heavy)
    SD_MIN = 0.8                         # m/s^2: the spread of a (partialled): without it the slope is nothing
    QUALIFY_S = 20.0                     # s of qualifying samples (weighted)
    SOLVE_EVERY = 0.25                   # s
    UPDATE_S = 300.0                     # s of driving between two updates of a held mass
    UPDATE_STEP = 0.05                   # of the mass: the most one update moves it

    def __init__(self, mass=None):
        self.mass = float(mass) if mass else self.PRIOR
        self.known = bool(mass)
        self._prior = float(mass) if mass else self.PRIOR
        self.holding = bool(mass)        # the mass is held and moves only at the updates (a learnt car), else it slews to the first estimate
        self.moved = None                # (old, new, estimate) of the last update, until the caller takes it
        self.driven = 0.0                # s of driving since the last update (or the start)
        self.r = self.sd = self.m_ols = self.m_rev = None
        self.estimate = None             # sqrt(m_ols m_rev) of the last solve, accepted or not (diagnostic)
        self.qualified = 0.0             # s of qualifying samples seen (not decayed)
        self.accepted = False
        self.reset()

    def reset(self):
        """The window starts again (a gap, a new car's samples); the mass stays."""
        self._boxes = (_Box(self.SMOOTH), _Box(self.SMOOTH), _Box(self.SMOOTH))     # dv/dt, dh/dt, P / v
        self._prev = None                # (t, v, height)
        self._clean_since = None
        self._gear = None
        self._gear_at = None
        self._pending = collections.deque()
        self._s = [[0.0] * 4 for _ in range(4)]      # sums of w z z^T, z = (a, 1, v^2 / 1000, y)
        self._w = 0.0
        self._last_solve = None
        self._moment_t = None

    def _clear_window(self, t):
        for b in self._boxes:
            b.clear()
        self._pending.clear()
        self._clean_since = t

    def _gates(self, sample, v):
        brake, clutch, hand = sample.brake, sample.clutch, sample.handbrake
        if not v > self.V_MIN or brake or clutch or hand:
            return False
        angles, ratios, susp = sample.slip_angle, sample.slip_ratio, sample.susp_norm
        if angles is None or ratios is None or susp is None or len(angles) < 4 or len(ratios) < 4 or len(susp) < 4:
            return False
        if max(abs(x) for x in angles[:4]) >= self.SLIP_ANGLE or max(abs(x) for x in ratios[:4]) >= self.SLIP_RATIO:
            return False
        if not all(self.SUSP[0] < x < self.SUSP[1] for x in susp[:4]):
            return False
        accel = sample.accel
        return accel is not None and len(accel) > 1 and abs(accel[1]) < self.AY_MAX_G

    def add(self, t, sample):
        """One telemetry sample at time t (the game's clock where it has one); True when the mass was re-solved."""
        gt = sample.game_time if finite(sample.game_time) else t
        v, pos, power, rpm = sample.speed, sample.pos, sample.power, sample.rpm
        if not (finite(v) and pos is not None and len(pos) > 1 and finite(pos[1]) and finite(power) and finite(rpm)):
            self._prev = None
            return False
        height = pos[1]
        prev, self._prev = self._prev, (gt, v, height)
        if prev is None or not 0.0 < gt - prev[0] <= self.GAP:
            self._clear_window(gt)       # a gap (or the first sample): the window starts again
            self._gear = sample.gear
            return False
        dt = gt - prev[0]
        if v > self.V_MIN:
            self.driven += dt
        if sample.gear != self._gear:
            self._gear, self._gear_at = sample.gear, gt
            self._pending.clear()        # the samples within GEAR_WINDOW before it do not count either
        if not self._gates(sample, v):
            self._clear_window(gt)
            return False
        boxes = self._boxes
        boxes[0].add(gt, (v - prev[1]) / dt)
        boxes[1].add(gt, (height - prev[2]) / dt)
        boxes[2].add(gt, power / max(v, 1.0))
        solved = False
        if gt - self._clean_since >= self.SMOOTH and (self._gear_at is None or gt - self._gear_at >= self.GEAR_WINDOW):
            grade = min(max(boxes[1].mean() / max(v, 1.0), -self.GRADE_MAX), self.GRADE_MAX)
            a = boxes[0].mean() + G * grade
            w_e = rpm * math.pi / 30.0
            y = boxes[2].mean() - self.J * (w_e / max(v, 1.0)) ** 2 * a
            self._pending.append((gt, dt, (a, 1.0, v * v / 1000.0, y)))
        while self._pending and self._pending[0][0] <= gt - self.GEAR_WINDOW:
            self._moments(*self._pending.popleft())
            solved = True
        if solved and (self._last_solve is None or gt - self._last_solve >= self.SOLVE_EVERY):
            self._solve(gt)
        return solved

    def _moments(self, gt, dt, z):
        last, self._moment_t = self._moment_t, gt
        decay = math.exp(-min(max(gt - last, 0.0), 1.0) / self.TAU) if last is not None else 1.0
        s = self._s
        for i in range(4):
            row = s[i]
            for j in range(4):
                row[j] = row[j] * decay + dt * z[i] * z[j]
        self._w = self._w * decay + dt
        self.qualified += dt

    def _partial(self, i, j):
        """The (i, j) moment with columns 1 and 2 (the constant and v^2) partialled out of both."""
        s = self._s
        ci = _solve2(s[1][1], s[1][2], s[2][2], s[1][i], s[2][i])
        if ci is None:
            return None
        return s[i][j] - ci[0] * s[1][j] - ci[1] * s[2][j]

    def _solve(self, gt):
        last, self._last_solve = self._last_solve, gt
        elapsed = 0.0 if last is None else min(max(gt - last, 0.0), 1.0)
        self.accepted = False
        if self.qualified < self.QUALIFY_S or self._w <= 0.0:
            return
        saa, say, syy = self._partial(0, 0), self._partial(0, 3), self._partial(3, 3)
        if saa is None or say is None or syy is None or saa <= 0.0 or syy <= 0.0:
            return
        self.r = say / math.sqrt(saa * syy)
        self.sd = math.sqrt(saa / self._w)
        self.m_ols, self.m_rev = say / saa, syy / say if say else None
        if self.m_ols > 0.0 and self.m_rev and self.m_rev > 0.0:
            self.estimate = math.sqrt(self.m_ols * self.m_rev)
        if not (self.r >= self.R_MIN and self.sd >= self.SD_MIN and self.m_ols > 0.0 and self.m_rev and self.m_rev > 0.0):
            return
        target = min(max(math.sqrt(self.m_ols * self.m_rev), self.LIMITS[0]), self.LIMITS[1])
        self.accepted = True
        if self.holding:
            if self.driven >= self.UPDATE_S:
                step = self.UPDATE_STEP * self.mass
                old = self.mass
                self.mass += min(max(target - old, -step), step)
                self.driven = 0.0
                self.moved = (old, self.mass, target)
            return
        step = self.SLEW * elapsed
        self.mass += min(max(target - self.mass, -step), step)
        self.known = True
        if abs(target - self.mass) < 1e-6:
            self.holding, self.driven = True, 0.0
            self.moved = (self._prior, self.mass, target)


class SlipLead:
    """The fast part of the front lateral force from the fronts' slip. Forza's Newton Fy_f (front_lateral) rises only when the car
    has answered; the front tyres' normalised slip angle moves with the steering at once. A complementary filter: Newton's
    force low-passed, plus the slip estimate (the slip through a saturating tyre curve, scaled online to agree with Newton's
    at low frequency) less its own low-pass: Fy = LP(Fy_N) + g (f(s) - LP(f(s))). Steady, f(s) = LP(f(s)) and the output
    is Newton's whatever g is (a step in the slip adds no level); without the slip (None), or g not yet known, plain Newton."""

    HZ = 6.0                             # the complement's corner (Hz): below it Newton's level, above it the slip's change
    FIT_TAU = 4.0                        # s: the gain's regression remembers this long (moving samples)
    PEAK = 1.0                           # normalised slip angle at the tyre's peak force
    SHAPE_C = 1.4                        # the curve sin(C atan(B x)), B so that the peak sits at PEAK
    G_MIN_SXX = 0.02                     # the slip curve's energy needed before g is trusted
    GAP = 0.1                            # s: a longer pause starts it again
    SIGN = -1.0                          # Forza's slip angle is positive for a car turned right of its travel: Newton's Fy_f is to the left

    def __init__(self):
        self.reset()

    def reset(self):
        self.y1 = self.y2 = self.at = None
        self.sxx = self.sxy = 0.0
        self.g = None

    def curve(self, x):
        b = math.tan(math.pi / (2.0 * self.SHAPE_C)) / self.PEAK
        return math.sin(self.SHAPE_C * math.atan(b * abs(x))) * math.copysign(1.0, x) * self.SIGN

    def step(self, t, fy, slip):
        """Fy_f (N) for Newton's `fy` and the fronts' normalised slip angle `slip` (None: not known)."""
        last, self.at = self.at, t
        if slip is None or not finite(slip):
            self.at = None
            return fy
        f = self.curve(slip)
        dt = 0.0 if last is None else t - last
        if self.y1 is None or dt < 0.0 or dt > self.GAP:
            self.y1, self.y2 = fy, f
            return fy
        a = 1.0 - math.exp(-2.0 * math.pi * self.HZ * dt)
        self.y1 += a * (fy - self.y1)
        self.y2 += a * (f - self.y2)
        decay = math.exp(-dt / self.FIT_TAU)
        self.sxx = self.sxx * decay + self.y2 * self.y2 * dt
        self.sxy = self.sxy * decay + self.y2 * self.y1 * dt
        if self.sxx >= self.G_MIN_SXX:
            self.g = max(self.sxy / self.sxx, 0.0)
        if not self.g:
            return fy
        return self.y1 + self.g * (f - self.y2)


class OutputFilter:
    """Forza's 60/s steps to what the wheel plays (see the module's docstring). `packet(t)` for each telemetry packet
    (its arrival time), `step(t, x)` every engine pass: x the steering force (ext_force units), returns the filtered,
    slew-limited value."""

    FC_RATIO = 2.5                       # the corner is the packet rate over this ...
    FC_MIN, FC_MAX = 15.0, 60.0          # ... within these (Hz)
    RATE_WINDOW = 2.0                    # s: the median interval is taken over this
    RATE_TAU = 0.5                       # s: the rate the corner follows is smoothed over this
    RATE_EVERY = 0.25                    # s between rate updates
    RATE_REF = 60.0                      # packets/s at which the slew is SLEW_FS per SLEW_S
    SLEW_FS = 0.15                       # of full scale ...
    SLEW_S = 0.010                       # ... per this long at RATE_REF
    ZETA_TAU = 0.6436                    # omega_c tau of a critically damped pair of lags (-3 dB where omega tau = 0.6436)
    GAP = 0.1                            # s between passes past which it starts again at the input
    FULL = 32767.0

    def __init__(self, full=FULL):
        self.full = full
        self.reset()

    def reset(self):
        self._arrivals = collections.deque()
        self.rate = self.RATE_REF        # packets/s, smoothed (60 until measured)
        self._rate_at = None
        self._y1 = self._y2 = self._out = None
        self._last = None

    def packet(self, t):
        a = self._arrivals
        if a and t < a[-1]:
            a.clear()
        a.append(t)
        while a and t - a[0] > self.RATE_WINDOW:
            a.popleft()

    def _update_rate(self, t):
        last, self._rate_at = self._rate_at, t
        a = self._arrivals
        if len(a) < 8:
            return
        gaps = sorted(b - c for b, c in zip(list(a)[1:], list(a)))
        median = gaps[len(gaps) // 2]
        if median <= 0.0:
            return
        measured = min(1.0 / median, 1000.0)
        dt = min(max(t - last, 0.0), 1.0) if last is not None else 1.0
        self.rate += (measured - self.rate) * dt / (self.RATE_TAU + dt)

    def corner(self):
        """The low-pass corner (Hz)."""
        return min(max(self.rate / self.FC_RATIO, self.FC_MIN), self.FC_MAX)

    def slew(self):
        """The most the output moves per second (ext_force units)."""
        return self.SLEW_FS * self.full / self.SLEW_S * (self.rate / self.RATE_REF)

    def step(self, t, x):
        last, self._last = self._last, t
        if self._rate_at is None or t - self._rate_at >= self.RATE_EVERY or t < self._rate_at:
            self._update_rate(t)
        if self._out is None or last is None or not 0.0 <= t - last <= self.GAP:
            self._y1 = self._y2 = self._out = x
            return x
        dt = t - last
        if dt <= 0.0:
            return self._out
        tau = self.ZETA_TAU / (2.0 * math.pi * self.corner())
        a = 1.0 - math.exp(-dt / tau)
        self._y1 += a * (x - self._y1)
        self._y2 += a * (self._y1 - self._y2)
        limit = self.slew() * dt
        self._out += min(max(self._y2 - self._out, -limit), limit)
        return self._out
