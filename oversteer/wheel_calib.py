"""Wheel calibration math (ClickUp 1dfcuabc4pj): from what a calibration run recorded (the forces written to
the driver's ext_force and the wheel positions read back) to the driver's correction (ffb_lut, ffb_deadband)
and a model of the wheel (inertia, friction, damping, latency, encoder step).

Nothing here touches a device: it works on recorded samples, so the same code runs on a real run, a simulated
wheel (tests/wheel_sim.py) or a saved recording.

The model. While the wheel turns, its angular acceleration (deg/s^2) is

    a(t) = tau(w(t - L)) / J  -  f * sgn(omega)  -  beta * omega  +  bias

w is the commanded force in full-scale units (-1..1, positive turns the wheel towards positive angles), L the
latency from the write to the torque, tau the motor's torque curve (symmetric, zero below the motor's dead
zone), f the Coulomb (sliding) friction and beta the viscous damping, both per unit inertia. tau/J is a
piecewise-linear function of |w| over the KNOTS, so every unknown enters linearly and one least-squares fit
over all the moving samples finds them all; L is found by trying each value. The acceleration is never
differentiated out of the position samples on its own: the same Savitzky-Golay filter that turns positions
into accelerations is applied to the double integral of every regressor, so both sides of the fit see exactly
the same smoothing (the filter is linear and blind to constants and slopes, which hold the unknown starting
position and speed). Several window lengths are used together, each weighted by its noise gain: the long ones
read the slow movements, the short ones the brief hard pulses a strong wheel is stopped and turned in.

Units. tau/J at full scale (a_fs, deg/s^2 per full-scale command) is what one command does to this wheel; the
torque curve is reported relative to it (1.0 at full scale). Friction is reported in full-scale command units
(the force that just balances it), damping in full-scale per deg/s, inertia as full-scale per deg/s^2: the
units an FFB loop needs for feed-forward. The wheel's torque in Nm is not measured.

The correction. The driver shapes the force x (0..32767, sign kept) with the LUT and then lifts it past the
dead zone d: out = d + (32767 - d) * LUT(x) / 32767. With the motor command u = out / 32767 the wheel's torque
is tau(u), so a LUT with tau(d + (1 - d) * LUT(x)) proportional to x makes the torque linear in what the game
asked for, and full scale stays full scale.
"""
import numpy as np

FULL = 32767                 # full-scale force (driver units)
LUT_POINTS = 33              # ffb_lut values: input i * 32767 / 32
# Command levels (full-scale units) the pulses are played at; the torque curve is solved at these
KNOTS = (0.0, 0.02, 0.04, 0.06, 0.08, 0.10, 0.13, 0.16, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.0)

GRID_S = 0.001               # positions are resampled to 1 ms
SG_HALVES = (3, 5, 8, 12, 20)        # Savitzky-Golay half windows in samples (7 to 41 ms), all used together...
SG_HALVES_SLOW = (8, 12, 20, 30)     # ...and these when that leaves the curve uncertain: a heavy wheel's small
                                     # accelerations drown in the encoder's step, and change slowly enough for
                                     # the longer windows
MOVING_DEG_S = 8.0           # a sample is used when the wheel turned faster than this throughout its window
MIN_ROWS = 200               # rows a window length needs to take part
BLOCKS = 12                  # groups of stretches the uncertainty is judged over
CHUNK = 250                  # samples (ms) in a stretch
LATENCIES_S = tuple(i / 1000 for i in range(0, 101, 5))   # latency candidates (a proxy hop adds to the driver's),
                                                          # refined to 1 ms around the best
DEADBAND_FLOOR = 0.02        # below 2 % of the full-scale torque the motor counts as giving nothing
MIN_R2 = 0.3                 # a fit explaining less of the measured acceleration than this is not trusted (the
                             # rest is mostly the encoder's step, large for a heavy wheel)...
MAX_UNCERTAINTY = 0.05       # ...nor one leaving a point of the torque curve this uncertain (share of full)
MAX_DEAD_ZONE = 0.15         # a motor dead zone above 15 % of full scale is a measurement gone wrong
LUT_SLOPE_MAX = 4.0          # no LUT point above 4 times the identity (plus LUT_MARGIN): a curve that steep
LUT_MARGIN = 1024            # means a torque curve measured wrong at the bottom
DITHER_RATIO = 1.5           # dither when one 8-bit step spans this many native steps of the game's force


class Recording:
    """What a run measured. Commands are (time, w) as written, w in full-scale units in the wheel's frame
    (positive turns towards positive angles); positions are (time, degrees) as the wheel reported them, one
    per change. `breakaway` holds (angle, direction, w) of each slow ramp, `onsets` (command time, first
    movement time) of each push from rest."""

    def __init__(self):
        self.cmd_t = []
        self.cmd = []
        self.pos_t = []
        self.pos = []
        self.breakaway = []
        self.onsets = []
        self.raw_steps = []           # |change| of the raw axis between consecutive reports (counts)
        self.counts_per_deg = None

    def command(self, t, w):
        self.cmd_t.append(t)
        self.cmd.append(w)

    def position(self, t, deg):
        self.pos_t.append(t)
        self.pos.append(deg)

    FIELDS = ('cmd_t', 'cmd', 'pos_t', 'pos', 'breakaway', 'onsets', 'raw_steps', 'counts_per_deg')

    def to_dict(self):
        """Plain lists and numbers (JSON), times to the microsecond: everything analyse() reads."""
        return {'cmd_t': [round(float(t), 6) for t in self.cmd_t], 'cmd': [round(float(w), 5) for w in self.cmd],
                'pos_t': [round(float(t), 6) for t in self.pos_t], 'pos': [round(float(p), 4) for p in self.pos],
                'breakaway': [list(b) for b in self.breakaway], 'onsets': [list(o) for o in self.onsets],
                'raw_steps': list(self.raw_steps), 'counts_per_deg': self.counts_per_deg}

    @classmethod
    def from_dict(cls, d):
        rec = cls()
        rec.cmd_t, rec.cmd = list(d['cmd_t']), list(d['cmd'])
        rec.pos_t, rec.pos = list(d['pos_t']), list(d['pos'])
        rec.breakaway = [tuple(b) for b in d.get('breakaway', [])]
        rec.onsets = [tuple(o) for o in d.get('onsets', [])]
        rec.raw_steps = list(d.get('raw_steps', []))
        rec.counts_per_deg = d.get('counts_per_deg')
        return rec


class FitError(Exception):
    """The run's data can't give a trustworthy correction; the message says why."""


# --- signal helpers ---

def resample(pos_t, pos, h=GRID_S, gap=0.010, report=0.002):
    """Positions on a uniform grid. Reports only come when the reading changes, so a long gap means the wheel
    stood still: it is held until one report period before the next change instead of being interpolated
    into a slow creep."""
    t = np.asarray(pos_t, dtype=float)
    p = np.asarray(pos, dtype=float)
    if len(t) < 2:
        raise FitError("no wheel movement was recorded")
    order = np.argsort(t, kind='stable')
    t, p = t[order], p[order]
    # Reports that reached us together (a proxy hop delivers them in bunches) carry one time: only the last
    # of a bunch is the wheel's position at that time, the earlier ones would be steps in the signal
    last = np.append(t[1:] > t[:-1], True)
    t, p = t[last], p[last]
    if len(t) < 2:
        raise FitError("no wheel movement was recorded")
    # Bunched, the spacing of the bunches is the normal gap: only a longer pause means the wheel stood still
    spacing = np.diff(t)
    gap = max(gap, 1.5 * float(np.percentile(spacing, 25)))
    gaps = np.flatnonzero(np.diff(t) > gap)
    if len(gaps):
        t = np.insert(t, gaps + 1, t[gaps + 1] - report)
        p = np.insert(p, gaps + 1, p[gaps])
    grid = np.arange(t[0], t[-1], h)
    return grid, np.interp(grid, t, p)


def savgol(half, h):
    """Quadratic Savitzky-Golay coefficients (value, first and second derivative) over 2*half+1 samples
    spaced h: estimate = sum(coef[j] * x[i - half + j])."""
    j = np.arange(-half, half + 1, dtype=float)
    a = np.vstack([np.ones_like(j), j, j * j]).T
    pinv = np.linalg.pinv(a)
    return pinv[0], pinv[1] / h, 2 * pinv[2] / (h * h)


def apply_at(x, coef, idx):
    """The filter `coef` centred on the samples `idx` (each at least len(coef)//2 from either end)."""
    return np.convolve(x, coef[::-1], mode='same')[idx]


def double_integral(r, h):
    """Trapezoidal double integral (no half-sample lag), from 0."""
    v = h * (np.cumsum(r) - 0.5 * r)
    return h * (np.cumsum(v) - 0.5 * v)


def command_at(cmd_t, cmd, t):
    """The command in force at each time t (written values hold until the next write; 0 before the first)."""
    i = np.searchsorted(cmd_t, t, side='right') - 1
    out = np.where(i >= 0, cmd[np.clip(i, 0, None)], 0.0)
    return out


def hats(u, knots):
    """Piecewise-linear basis over the knots, one column per knot after the first (tau(0) = 0)."""
    k = np.asarray(knots)
    cols = np.zeros((len(u), len(k) - 1))
    u = np.clip(u, 0.0, k[-1])
    for n in range(1, len(k)):
        left = (u - k[n - 1]) / (k[n] - k[n - 1])
        if n + 1 < len(k):
            right = (k[n + 1] - u) / (k[n + 1] - k[n])
            cols[:, n - 1] = np.clip(np.minimum(left, right), 0.0, 1.0)
        else:
            cols[:, n - 1] = np.clip(left, 0.0, 1.0)
    return cols


def rolling_min(x, half):
    """min over each centred window of 2*half+1 (shorter at the ends)."""
    padded = np.pad(x, half, mode='edge')
    return np.lib.stride_tricks.sliding_window_view(padded, 2 * half + 1).min(axis=1)


def isotonic(y):
    """The non-decreasing sequence closest to y in least squares (pool adjacent violators)."""
    blocks = []                     # [mean, weight, count]
    for v in y:
        blocks.append([float(v), 1.0, 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            m2, w2, c2 = blocks.pop()
            m1, w1, c1 = blocks.pop()
            blocks.append([(m1 * w1 + m2 * w2) / (w1 + w2), w1 + w2, c1 + c2])
    out = []
    for m, _w, c in blocks:
        out.extend([m] * c)
    return np.array(out)


# --- the fit ---

def fit_dynamics(rec, knots=KNOTS, latencies=LATENCIES_S, halves=SG_HALVES):
    """Least-squares fit of the model above to a recording. Returns a dict: 'curve' (tau/J at each knot,
    deg/s^2), 'curve_se' (its uncertainty), 'friction' (f), 'damping' (beta), 'bias', 'latency' (s), 'r2',
    'rows' (samples used).

    Every window length in `halves` contributes its own rows, each weighted by the inverse of that filter's
    noise gain: a long window reads the slow movements (small forces) with little noise, a short one the
    brief hard pulses (a strong wheel is stopped and turned in 10-30 ms, no long window sees them moving
    throughout). Their rows are the same data seen at different scales, so the uncertainty is not taken from
    the rows' scatter but from refitting without each of a dozen groups of stretches of the run in turn."""
    if len(rec.cmd_t) < 2:
        raise FitError("no forces were recorded")
    h = GRID_S
    grid, theta = resample(rec.pos_t, rec.pos, h)
    n = len(grid)
    halves = tuple(hf for hf in halves if n >= 4 * hf + 10)
    if not halves:
        raise FitError("the recording is too short")
    cmd_t = np.asarray(rec.cmd_t, dtype=float)
    cmd = np.asarray(rec.cmd, dtype=float)

    # Per window length: the rows (moving the same way throughout the window: the friction there is sliding
    # friction of one sign), the acceleration there, the regressors that don't depend on the latency
    scales = []
    for half in halves:
        c0, c1, c2 = savgol(half, h)
        idx_all = np.arange(half, n - half)
        omega = np.zeros(n)
        omega[idx_all] = apply_at(theta, c1, idx_all)
        fwd = rolling_min(omega, half) > MOVING_DEG_S
        back = rolling_min(-omega, half) > MOVING_DEG_S
        ok = np.zeros(n, dtype=bool)
        ok[2 * half:n - 2 * half] = True
        rows = np.flatnonzero(ok & (fwd | back))
        if len(rows) < MIN_ROWS:
            continue
        sgn = np.sign(omega)
        fixed = np.column_stack([
            apply_at(double_integral(-sgn, h), c2, rows),
            apply_at(double_integral(-omega, h), c2, rows),
            apply_at(double_integral(np.ones(n), h), c2, rows),
        ])
        scales.append({'half': half, 'c2': c2, 'rows': rows, 'accel': apply_at(theta, c2, rows),
                       'fixed': fixed, 'weight': 1.0 / float(np.linalg.norm(c2))})
    if not scales:
        raise FitError("the wheel hardly moved during the run")

    def design(lat):
        w = command_at(cmd_t, cmd, grid - lat)
        basis = hats(np.abs(w), knots) * np.sign(w)[:, None]
        xs = []
        for sc in scales:
            cols = np.column_stack([apply_at(double_integral(basis[:, k], h), sc['c2'], sc['rows'])
                                    for k in range(basis.shape[1])])
            xs.append(np.column_stack([cols, sc['fixed']]) * sc['weight'])
        return np.vstack(xs)

    accel = np.concatenate([sc['accel'] * sc['weight'] for sc in scales])
    # the group (of BLOCKS) each row belongs to: stretches of CHUNK samples dealt out in turn, so every group
    # holds a share of every kind of movement
    block = np.concatenate([(sc['rows'] // CHUNK) % BLOCKS for sc in scales])

    def solve(lat):
        x = design(lat)
        p, *_ = np.linalg.lstsq(x, accel, rcond=None)
        res = accel - x @ p
        return float(res @ res), p, x

    def best_of(candidates):
        return min(((solve(l)[0], l) for l in candidates), key=lambda e: e[0])[1]

    lat = best_of(latencies)
    lat = best_of([max(0.0, lat + d / 1000) for d in range(-4, 5)])
    ss, p, x = solve(lat)
    # One pass without the outliers (a stray report, a moment the model doesn't cover), then refit
    res = accel - x @ p
    mad = np.median(np.abs(res - np.median(res))) * 1.4826 + 1e-9
    keep = np.abs(res) < 4 * mad
    xk, ak, bk = x[keep], accel[keep], block[keep]
    p, *_ = np.linalg.lstsq(xk, ak, rcond=None)
    res = ak - xk @ p
    tot = ak - ak.mean()
    r2 = 1.0 - float(res @ res) / max(float(tot @ tot), 1e-9)
    # Uncertainty: jackknife over the groups (neighbouring rows share their windows and the scales share the
    # data, so the rows' scatter says little). A knot the other groups don't reach moves a lot when its
    # group is left out.
    xtx = xk.T @ xk
    xty = xk.T @ ak
    nk = len(knots) - 1
    loo = []
    for b in range(BLOCKS):
        m = bk == b
        if not m.any():
            continue
        q, *_ = np.linalg.lstsq(xtx - xk[m].T @ xk[m], xty - xk[m].T @ ak[m], rcond=None)
        loo.append(q[:nk])
    if len(loo) < 3:
        raise FitError("the recording is too short")
    loo = np.array(loo)
    se = np.sqrt((len(loo) - 1) / len(loo) * ((loo - loo.mean(axis=0)) ** 2).sum(axis=0))
    return {'curve': np.concatenate([[0.0], p[:nk]]), 'curve_se': np.concatenate([[0.0], se]),
            'friction': float(p[nk]), 'damping': float(p[nk + 1]), 'bias': float(p[nk + 2]),
            'latency': float(lat), 'r2': r2, 'rows': int(keep.sum()),
            'window_ms': (2 * max(sc['half'] for sc in scales) + 1) * h * 1000}


# --- the correction ---

def relative_curve(curve):
    """The torque curve over the knots as a share of full scale, non-decreasing and >= 0."""
    a_fs = float(curve[-1])
    if not a_fs > 0:
        raise FitError("the wheel did not respond to full force")
    r = isotonic(np.clip(np.asarray(curve, dtype=float) / a_fs, 0.0, None))
    r[0] = 0.0
    return r / r[-1], a_fs


def dead_zone(knots, r, floor=DEADBAND_FLOOR):
    """The command below which the motor gives no torque: where the curve's first clear rise, extended
    down, meets zero (between the last knot under the floor and the first above it)."""
    k = np.asarray(knots, dtype=float)
    above = np.flatnonzero(r > floor)
    j = int(above[0]) if len(above) else len(k) - 1
    if j == 0:
        return 0.0
    if j + 1 < len(k) and r[j + 1] > r[j]:
        slope = (r[j + 1] - r[j]) / (k[j + 1] - k[j])
    else:
        slope = (r[j] - r[j - 1]) / (k[j] - k[j - 1])
    d = k[j] - r[j] / slope if slope > 0 else k[j - 1]
    return float(min(max(d, k[j - 1]), k[j]))


def torque_model(knots, r, d):
    """tau(u) for u in 0..1 (full-scale share of the full-scale torque): zero up to the dead zone d, then the
    measured curve (from 0 at d to the first knot past it)."""
    k = np.asarray(knots, dtype=float)
    j = int(np.searchsorted(k, d, side='right'))
    xs = np.concatenate([[0.0, d], k[j:]])
    ys = np.concatenate([[0.0, 0.0], r[j:]])
    if xs[1] == xs[0]:
        xs, ys = xs[1:], ys[1:]
    return xs, ys


def inverse_pl(xs, ys, y):
    """The smallest x with f(x) = y for the non-decreasing piecewise-linear f through (xs, ys)."""
    y = float(np.clip(y, ys[0], ys[-1]))
    for i in range(1, len(xs)):
        if ys[i] >= y and ys[i] > ys[i - 1]:
            return float(xs[i - 1] + (xs[i] - xs[i - 1]) * (y - ys[i - 1]) / (ys[i] - ys[i - 1]))
        if ys[i] >= y:
            return float(xs[i - 1]) if ys[i - 1] >= y else float(xs[i])
    return float(xs[-1])


def build_lut(knots, r, d, points=LUT_POINTS):
    """The ffb_lut values (input i * 32767 / (points - 1)) and ffb_deadband value that make the torque
    linear in the requested force, for the relative curve r over the knots and the dead zone d."""
    xs, ys = torque_model(knots, r, d)
    top = ys[-1]
    lut = []
    for i in range(points):
        target = top * i / (points - 1)
        u = inverse_pl(xs, ys, target)                 # motor command giving that torque
        v = 0.0 if d >= 1 else (u - d) / (1.0 - d)     # LUT output before the driver's deadband lift
        lut.append(int(round(min(max(v, 0.0), 1.0) * FULL)))
    lut[0] = 0
    lut[-1] = FULL
    for i in range(1, points):
        lut[i] = max(lut[i], lut[i - 1])
    return lut, int(round(d * FULL))


def plausible_lut(lut, points=LUT_POINTS):
    """True for a LUT a calibration can have produced: `points` integers from 0 to 32767, strictly increasing
    (no flat runs, which would make a range of forces the same), never more than LUT_SLOPE_MAX times the
    identity (plus LUT_MARGIN)."""
    if not isinstance(lut, (list, tuple)) or len(lut) != points:
        return False
    if any(isinstance(v, bool) or not isinstance(v, int) for v in lut):
        return False
    if lut[0] != 0 or lut[-1] != FULL or any(b <= a for a, b in zip(lut, lut[1:])):
        return False
    n = points - 1
    return all(v <= LUT_SLOPE_MAX * FULL * i / n + LUT_MARGIN for i, v in enumerate(lut))


def shaped(x, lut, deadband):
    """What the driver sends for a force x (0..32767): the LUT, then the deadband lift."""
    n = len(lut) - 1
    pos = min(max(x, 0), FULL) * n / FULL
    i = min(int(pos), n - 1)
    y = lut[i] + (lut[i + 1] - lut[i]) * (pos - i)
    if x <= 0:
        return 0.0
    return deadband + (FULL - deadband) * y / FULL


def recommend_dither(lut, deadband, low=0.3, ratio=DITHER_RATIO):
    """Dither on when the correction makes the 8-bit steps coarse for small forces. One step of the wheel
    (256) is a change of 256 / (d out / d x) in the game's force; where the motor is strong at the bottom of
    its curve the LUT is flatter than 1 there and a step spans more of the game's range than without the
    correction. Over the lowest `low` share of the range, the coarsest step in game-force units is compared
    with a native step: past `ratio` times, dithering (which averages the steps over a few ticks) restores
    the fine low end. Otherwise it is left off, since it means a new command most ticks. Returns
    (dither, coarsest step in native steps)."""
    n = len(lut) - 1
    worst = 0.0
    for i in range(n):
        x0, x1 = FULL * i / n, FULL * (i + 1) / n
        if x0 > low * FULL:
            break
        slope = (shaped(x1, lut, deadband) - shaped(max(x0, 1), lut, deadband)) / max(x1 - max(x0, 1), 1)
        span = 1.0 / slope if slope > 0 else float('inf')        # one output step, in native steps of x
        worst = max(worst, span)
    return worst > ratio, worst


def encoder_step(raw_steps, counts_per_deg):
    """The encoder's step in degrees: the smallest change the wheel reports, as the mean of the cluster of
    smallest steps (an encoder coarser than the axis gives steps alternating between two neighbours)."""
    s = np.asarray([v for v in raw_steps if v > 0], dtype=float)
    if not len(s) or not counts_per_deg:
        return None
    low = np.percentile(s, 2)
    cluster = s[(s >= low) & (s <= 1.5 * low)]
    return float(cluster.mean() / counts_per_deg)


def onset_latency(onsets):
    """Median time from a push's write to the first position change, in seconds."""
    d = [b - a for a, b in onsets if b is not None and b >= a]
    return float(np.median(d)) if d else None


def analyse(rec, knots=KNOTS):
    """Everything a run gives: the fitted model, the correction and the numbers stored per wheel."""
    fit = fit_dynamics(rec, knots)
    r, a_fs = relative_curve(fit['curve'])
    uncertainty = float(np.max(fit['curve_se'])) / a_fs
    if uncertainty > MAX_UNCERTAINTY:
        try:
            slow = fit_dynamics(rec, knots, halves=SG_HALVES_SLOW)
            r_slow, a_slow = relative_curve(slow['curve'])
            u_slow = float(np.max(slow['curve_se'])) / a_slow
            if u_slow < uncertainty:
                fit, r, a_fs, uncertainty = slow, r_slow, a_slow, u_slow
        except FitError:
            pass
    if fit['r2'] < MIN_R2 or uncertainty > MAX_UNCERTAINTY:
        raise FitError("the wheel's response did not follow a consistent pattern (fit {:.2f}, uncertainty {:.0f} %);"
                       " was it touched?".format(fit['r2'], uncertainty * 100))
    d = dead_zone(knots, r)
    if d > MAX_DEAD_ZONE:
        raise FitError("the motor seemed to give nothing below {:.0f} % (more than {:.0f} % is not plausible)"
                       .format(d * 100, MAX_DEAD_ZONE * 100))
    lut, deadband = build_lut(knots, r, d)
    if not plausible_lut(lut):
        raise FitError("the force curve came out implausible")
    dither, step_span = recommend_dither(lut, deadband)
    breakaway = [w for _a, _d, w in rec.breakaway]
    return {
        'lut': lut,
        'deadband': deadband,
        'dither': bool(dither),
        'dither_step_span': round(step_span, 2),
        'curve': [round(float(v), 4) for v in r],
        'knots': list(knots),
        'a_fs': round(a_fs, 1),                                   # deg/s^2 at full scale
        'inertia': round(1.0 / a_fs, 8),                          # full scale per deg/s^2
        'friction': round(fit['friction'] / a_fs, 4),             # full scale
        'damping': round(fit['damping'] / a_fs, 7),               # full scale per deg/s
        'bias': round(fit['bias'] / a_fs, 4),
        'breakaway': round(float(np.median(breakaway)), 4) if breakaway else None,
        'breakaway_by_angle': [[round(a, 1), int(dd), round(w, 4)] for a, dd, w in rec.breakaway],
        'latency_ms': (None if onset_latency(rec.onsets) is None else round(onset_latency(rec.onsets) * 1000, 1)),
        'fit_latency_ms': round(fit['latency'] * 1000, 1),
        'encoder_deg': (None if encoder_step(rec.raw_steps, rec.counts_per_deg) is None
                        else round(encoder_step(rec.raw_steps, rec.counts_per_deg), 4)),
        'fit_r2': round(fit['r2'], 3),
        'fit_uncertainty': round(uncertainty, 4),
        'fit_rows': fit['rows'],
        'fit_window_ms': round(fit['window_ms']),
    }
