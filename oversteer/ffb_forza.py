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

from . import ffb_carprops

G = 9.81
WHEELBASE = ffb_carprops.WHEELBASE       # m: ASSUMED (telemetry has none)
BL = {'fwd': 0.62, 'rwd': 0.53, 'awd': 0.58}     # b/L priors: the weight share on the front axle, by drivetrain
BL_DEFAULT = 0.58
DI = 1.0                                 # the yaw-acceleration term's weight
AY_MAX = 30.0                            # m/s^2
RDOT_MAX = 15.0                          # rad/s^2
CASTER = 0.027                           # m: the mechanical trail
TP0 = 0.028                              # m: the pneumatic trail at no slip
TP_S_HALF = 1.4                          # normalised combined slip at which it has halved
TP_N = 4.0


def finite(x):
    return x is not None and math.isfinite(x)


def trail(s):
    """The pneumatic trail (m) at the normalised combined slip s of the fronts: 0.028 / (1 + (s / 1.4)^4)."""
    return TP0 / (1.0 + (abs(s) / TP_S_HALF) ** TP_N)


def front_share(drivetrain):
    """b/L, the weight share on the front axle, from the drivetrain."""
    return BL.get(drivetrain, BL_DEFAULT)


def front_lateral(mass, bl, ay, rdot, wheelbase=WHEELBASE, di=DI):
    """Fy_f (N, left positive): m (b/L) (a_y + di a rdot), a = wheelbase (1 - b/L); a_y and rdot clipped."""
    ay = min(max(ay, -AY_MAX), AY_MAX)
    rdot = min(max(rdot, -RDOT_MAX), RDOT_MAX)
    return mass * bl * (ay + di * wheelbase * (1.0 - bl) * rdot)


def kingpin(fy, s, tp_scale=1.0, caster=CASTER):
    """The kingpin torque (Nm) in front_torque's sign: -(caster + tp(s)) Fy_f. `tp_scale` scales the pneumatic part
    (0.5 with a puddle under both fronts)."""
    return -(caster + tp_scale * trail(s)) * fy


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
    clamped to LIMITS, slewed SLEW kg/s toward it, from `prior` (the stored mass, else PRIOR)."""

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
    AY_MAX_G = 3.0                       # m/s^2
    GAP = 0.05                           # s
    GEAR_WINDOW = 0.15                   # s
    GRADE_MAX = 0.3
    R_MIN = 0.85
    SD_MIN = 0.8                         # m/s^2: the spread of a (partialled): without it the slope is nothing
    QUALIFY_S = 20.0                     # s of qualifying samples (weighted)
    SOLVE_EVERY = 0.25                   # s

    def __init__(self, mass=None):
        self.mass = float(mass) if mass else self.PRIOR
        self.known = bool(mass)
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
        step = self.SLEW * elapsed
        self.mass += min(max(target - self.mass, -step), step)
        self.known = True
        self.accepted = True


class OutputFilter:
    """Forza's 60/s steps to what the wheel plays (see the module's docstring). `packet(t)` for each telemetry packet
    (its arrival time), `step(t, x)` every engine pass: x the steering force (ext_force units), returns the filtered,
    slew-limited value."""

    FC_RATIO = 3.0                       # the corner is the packet rate over this ...
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
