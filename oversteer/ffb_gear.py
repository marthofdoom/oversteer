"""Gear guard: keeps the G29-class wheels' gear drive loaded on one side, so small force reversals don't rattle the
backlash (about 14 sign reversals a second on a straight, 6 in corners, on marth's capture).

Applied to the final intended force (a share of full scale, signed), once per engine pass, after the steering force, lock
stop, texture and glide and just before the master-gain compensation (ffb_engine.FfbEngine._out). `slow` is the force low-passed
at SLOW_HZ; `side` (+1 / -1) changes only when `slow` passes the other side by HYST. On a held side the output is
max(F, +p) (min(F, -p) on the other side), p = PRELOAD * level, below the gear drive's own friction: the force on the held
side passes unchanged, and a fast reversal of the true force is held at the preload instead of flipping the motor. A real
change of side moves from one preload to the other at RATE (a full swing in SWING s), then the force passes normally. HYST
is above p, so a force that stays near zero keeps its last side and the output sits at the preload.

`weight` (0..1) fades the whole effect: the engine passes the glide weight times the fade, so the preload comes in with the
take-over's glide and goes out in the hand-back; at 0 the force passes unchanged. A take-over starts from the current sign
(takeover()); a gap in the samples over GAP s restarts the low-pass and the rate limit and keeps the side.

Not covered: ffb_dynamics's local aligning spring runs separately in the driver and adds its own force after this; when
it is on, the engine's force is the residual (the wanted torque less the spring), and that residual goes through the guard.
"""
import math

PRELOAD = 0.025          # share of full scale: below the G29's friction (0.044)
HYST = 0.03              # share of full scale the slow force must pass the other side by for the side to change
SLOW_HZ = 3.0            # corner of the first-order low-pass that decides the side
SWING = 0.015            # s: a full swing from +p to -p
RATE = 2.0 * PRELOAD / SWING     # share of full scale per second the output may move while it changes side
OVERALL_GEAR_GUARD_LEVEL = 100      # "Gear guard" level default, % of PRELOAD
GAP = 0.25               # s without a sample: the low-pass and the rate limit start again (the side stays)
TAU = 1.0 / (2.0 * math.pi * SLOW_HZ)


class GearGuard:
    def __init__(self, level=1.0):
        self.level = level               # 0..1 of PRELOAD
        self.reset()
        self.side = 0

    def reset(self):
        """Forget the filter and the rate limit (the side stays)."""
        self._t = None
        self._slow = 0.0
        self._out = None
        self._moving = False

    def takeover(self):
        """A fresh take-over: the side comes from the first force's sign."""
        self.reset()
        self.side = 0

    def step(self, t, force, weight=1.0):
        """`force` (share of full scale) through the guard at time t; `weight` 0..1 scales the effect."""
        if self._t is None or t - self._t > GAP or t < self._t:
            self._slow, self._out, self._moving = force, None, False
            dt = 0.0
        else:
            dt = t - self._t
            self._slow += (force - self._slow) * (1.0 - math.exp(-dt / TAU))
        self._t = t
        if self.side == 0:
            self.side = 1 if force >= 0.0 else -1
        elif self.side > 0 and self._slow < -HYST:
            self.side, self._moving = -1, True
        elif self.side < 0 and self._slow > HYST:
            self.side, self._moving = 1, True
        p = PRELOAD * self.level
        want = max(force, p) if self.side > 0 else min(force, -p)
        if self._moving and self._out is not None:
            step = RATE * dt
            out = self._out + min(max(want - self._out, -step), step)
            if abs(want - out) <= step:
                self._moving = False        # caught up with the force: it passes normally from here
                out = want
            want = out
        elif self._moving:
            self._moving = False
        self._out = want
        weight = min(max(weight, 0.0), 1.0)
        return force + weight * (want - force) if weight < 1.0 else want
