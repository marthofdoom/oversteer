"""A small Python mirror of the new-lg4ff driver's local aligning spring (`ffb_align`), so the engine can subtract
the force the driver will actually render from the force it wants and send only the rest through ext_force.

What the driver does (lg4ff_dyn_force as ported in ffb-verify-dyn/sim.py, from hid-lg4ff.c on branch ffb-dynamics,
116e900; units FS = share of full scale, degrees of steering wheel):

    cur     follows the target at SLEW deg/s at most (a target that jumps more than JUMP deg restarts the fade-in)
    fade    0 -> 1 over RAMP s (FADE_IN; JUMP restarts take JUMP_RAMP s)
    k_eff   = min(k, k_limit)                   k_limit is what the driver reports as ffb_align's 4th field
    tau_m   = min(tau_max, TAU_CAP) * fade
    force   = tau_m * sat(k_eff * (cur - pos) / tau_m)       toward the target; sat() is the driver's cubic limiter

The driver also runs a hands-on observer and settle damping; neither changes the spring, so neither is mirrored.
"""

SLEW = 1000.0                            # deg/s: how fast the spring's target may move
JUMP = 90.0                              # deg: a bigger step of the target restarts the fade-in, slowly
FADE_IN = 0.2                            # s
JUMP_RAMP = 1.0                          # s
TAU_CAP = 0.8                            # FS: the driver's cap on tau_max


def sat(x):
    """The driver's limiter: x (27 + x^2) / (27 + 9 x^2) up to |x| = 3, then 1."""
    if x >= 3.0:
        return 1.0
    if x <= -3.0:
        return -1.0
    return x * (27.0 + x * x) / (27.0 + 9.0 * x * x)


class DriverSpring:
    """The spring's state (the slewed target and the fade) and force."""

    def __init__(self):
        self.cur = None
        self.fade = 0.0
        self.ramp = FADE_IN

    def reset(self):
        self.cur, self.fade, self.ramp = None, 0.0, FADE_IN

    def force(self, target_deg, pos_deg, k, tau_max, k_limit, dt):
        """The spring's force (FS, positive toward higher angles) `dt` s after the last call, for a target and the
        wheel at `pos_deg`; k in FS per degree, tau_max in FS, k_limit in FS per degree (None: no limit)."""
        if self.cur is None:
            self.cur = target_deg
        step = SLEW * max(dt, 0.0)
        self.cur += max(-step, min(step, target_deg - self.cur))
        if abs(target_deg - self.cur) > JUMP:
            self.fade, self.ramp = 0.0, JUMP_RAMP
        else:
            self.fade = min(1.0, self.fade + max(dt, 0.0) / self.ramp)
        k_eff = k if k_limit is None else min(k, max(k_limit, 0.0))
        tm = min(tau_max, TAU_CAP) * self.fade
        if not (tm > 0.0 and k_eff > 0.0):
            return 0.0
        return tm * sat(k_eff * (self.cur - pos_deg) / tm)
