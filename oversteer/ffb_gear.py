"""The gear-crossing stage's settings: new-lg4ff's `ffb_gear_cross` (hid-lg4ff.c lg4ff_gear_cross) on a gear-drive wheel.

The stage runs in the driver on the final summed force, in every FF tick: it holds a side, and small force reversals
(about 14 a second on a straight on marth's capture) can't rattle the backlash. This module turns the "Gear guard level"
slider into what the engine writes at a take-over, says how late a big reversal comes at those values (ffb_delay), and
has a mirror of the stage (Crossing) for the replay check (scripts/ffb-gear-replay.py) and the slide model
(scripts/ffb_score.py).

The research's values (~/.cache/ffb-gear-research, sweep at 2026-11-04, marth's flip of 15 %) are what the defaults play:
PRELOAD, FLIP, BAND, RATE, REJOIN. Two sliders: "Gear guard level" is the crossing's speed, in steps of 1 % through a
continuous constant, RATE inversely (about 2 FS/s at 100 %, faster toward 0, 0 is off); "Gear flip point" is FLIP in % of
full scale. The preload and the band stay; the preload halves with friction removal on, as the wheel's own friction is
what stops it creeping.
"""

PRELOAD = 0.02           # share of full scale, below the G29's friction (0.044)
FLIP = 0.15              # share of full scale the force itself must pass the other side by for the side to change
BAND = 0.08              # share of full scale on the new side up to which the crossing is limited
RATE = 3.3               # FS/s through the band
REJOIN = 40.0            # FS/s from the band back to the force
OVERALL_GEAR_GUARD_LEVEL = 61        # "Gear guard level" default, %: exactly the research's RATE
OVERALL_GEAR_FLIP = 15   # "Gear flip point" default, % of full scale
GEAR_FLIP_RANGE = (3, 30)
RATE_MAX = 100.0         # FS/s, the driver's ceiling (LG4FF_GEAR_RATE_MAX)
OFF = '0 0 0 0 0'        # ffb_gear_cross off

# The reference reversal of the estimate, as in the research's sweep: +0.5 to -0.5 FS over 40 ms, late being when the
# output is at 90 % of the new force against when the force is all the way there
REF_AMP = 0.5
REF_RAMP_S = 0.040


def params(level, removal=False, flip_pct=OVERALL_GEAR_FLIP):
    """(preload, flip, band, rate, rejoin) in FS and FS/s at `level` % (continuous) and a flip point in % of full scale, None
    at level 0 or under: off."""
    if level is None or level <= 0.0:
        return None
    level = min(float(level), 100.0)
    preload = PRELOAD / 2.0 if removal else PRELOAD
    rate = min(RATE * OVERALL_GEAR_GUARD_LEVEL / level, RATE_MAX)
    return preload, max(flip_pct / 100.0, preload), BAND, rate, max(REJOIN, rate)


def text(level, removal=False, flip_pct=OVERALL_GEAR_FLIP):
    """What ffb_gear_cross gets: 1e-4 FS for preload, flip and band, 1e-3 FS/s for rate and rejoin."""
    values = params(level, removal, flip_pct)
    if values is None:
        return OFF
    p, f, b, r, j = values
    return '%d %d %d %d %d' % (round(p * 1e4), round(f * 1e4), round(b * 1e4), round(r * 1e3), round(j * 1e3))


def reversal_late_ms(level, removal=False, flip_pct=OVERALL_GEAR_FLIP):
    """How late the crossing makes the reference reversal, ms, in closed form from the constants: the wait for the force to
    pass the flip, the output from the old preload through the band at the rate, then from the band to 90 % of the new
    force at the rejoin, against the force's own ramp. Sweep at flip 6 %: 22 ms at 3.3 FS/s, 40 at 2, 90 at 1, 190 at 0.5; at
    flip 15 %: 26 at 3.3, 44 at 2, 94 at 1. 0: off."""
    values = params(level, removal, flip_pct)
    if values is None:
        return 0.0
    p, f, b, r, j = values
    late = (REF_AMP + f) * REF_RAMP_S / (2.0 * REF_AMP) + (p + b) / r + max(0.9 * REF_AMP - b, 0.0) / j - REF_RAMP_S
    return max(late * 1e3, 0.0)


class Crossing:
    """The driver's stage on floats (share of full scale, seconds, deg/s), without replace mode's weight. `gate`: the
    driver's bypass is on (a wheel swinging free, a regular swing); False: never bypassed, the worst case."""
    V_GATE, F_GATE, HOLD, G_TIME = 250.0, 0.12, 0.06, 0.01   # LG4FF_GEAR_V_GATE, _F_GATE, _HOLD_US, _G_MS
    OSC_V, OSC_V_SIGN, OSC_HALF, OSC_N, OSC_HOLD = 100.0, 20.0, 0.25, 3, 0.5  # LG4FF_GEAR_OSC_*

    def __init__(self, values=None, gate=True):
        self.p, self.flip, self.band, self.rate, self.rejoin = values or params(OVERALL_GEAR_GUARD_LEVEL)
        self.gate = gate
        self.side, self.cur, self.moving, self.g, self.hold = 0, 0.0, False, 1.0, 0.0
        self.ticks = self.bypassed = 0
        self._o_sign, self._o_peak, self._o_t, self._o_n = 0, 0.0, 0.0, 0

    def _swinging(self, speed, dt):
        sgn = 0 if abs(speed) < self.OSC_V_SIGN else (1 if speed > 0.0 else -1)
        self._o_t += dt
        if sgn and sgn != self._o_sign:
            if self._o_sign:
                self._o_n = self._o_n + 1 if self._o_t <= self.OSC_HALF and self._o_peak >= self.OSC_V else 0
            self._o_sign, self._o_peak, self._o_t = sgn, 0.0, 0.0
        self._o_peak = max(self._o_peak, abs(speed))
        if self._o_t > self.OSC_HALF:
            self._o_n = 0
        return self._o_n >= self.OSC_N

    def step(self, f, dt, speed=0.0, hands=True):
        """The output for force `f`, `dt` s after the last step; `speed` the wheel's in deg/s, signed, `hands` the observer's."""
        if not self.side:
            self.side, self.cur = (1 if f >= 0.0 else -1), f
        if self.gate and self._swinging(speed, dt):
            self.hold = self.OSC_HOLD
        elif self.gate and abs(speed) >= self.V_GATE and (not hands or abs(f) < self.F_GATE):
            self.hold = max(self.hold, self.HOLD)
        else:
            self.hold = max(self.hold - dt, 0.0)
        if self.hold > 0.0:
            if f:
                self.side = 1 if f > 0.0 else -1
            self.moving = False
        elif (self.side > 0 and f < -self.flip) or (self.side < 0 and f > self.flip):
            if self.side * self.cur > self.p:
                self.cur = self.side * self.p      # the old side's excess goes at once: the gears stay loaded
            self.side, self.moving = -self.side, True
        s = self.side
        want = max(f, self.p) if s > 0 else min(f, -self.p)
        if self.hold > 0.0 or not self.moving:
            self.cur, self.moving = want, False
        elif s * self.cur < self.band:
            self.cur += s * self.rate * dt
            if s * self.cur >= s * want:
                self.cur, self.moving = want, False
        else:
            step = self.rejoin * dt
            self.cur += min(max(want - self.cur, -step), step)
            if abs(want - self.cur) <= step:
                self.cur, self.moving = want, False
        target = 0.0 if self.hold > 0.0 else 1.0
        step = dt / self.G_TIME
        self.g = target if abs(target - self.g) <= step else self.g + (step if target > self.g else -step)
        self.ticks += 1
        self.bypassed += self.g < 1.0
        return f + self.g * (self.cur - f)
