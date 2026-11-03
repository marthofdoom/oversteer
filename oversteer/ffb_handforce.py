"""The force in the hand, in newtons: the rim torque of the played G29 force and of the car's model force, as a force at
the rim (torque / (rim_mm / 2000)), at cruise, cornering and the limit, as running p50 / p90 over a drive.

The cases follow scripts/ffb_score.py's: cruise (moving, steady, under CRUISE_AY of lateral acceleration), cornering
(over CORNER_AY, front slip under STEADY_ALPHA) and the limit (front slip over LIMIT_ALPHA, past the tyres' peak).
A sample with no lateral acceleration or slip is not counted. The summary is one log fragment per drive."""
import math

from . import ffb_carprops

CRUISE_AY = 1.5                          # m/s^2: under this (and steady) the car is cruising
CORNER_AY = 3.0                          # m/s^2: over this (and steady) the car is cornering
STEADY_ALPHA = 0.2                       # rad: front slip under which the tyres are inside their peak
LIMIT_ALPHA = 0.4                        # rad: front slip over which the tyres are past their peak
CASES = ('cruise', 'cornering', 'limit')
DRIVE_TAU = 3600.0                       # s: the quantiles forget over a long drive's worth, not a minute


def newtons(torque_nm, rim_mm):
    """The force at the rim (N) of a torque (Nm) on a wheel of `rim_mm` diameter."""
    return torque_nm / (rim_mm / 2000.0)


def case_of(speed, ay, alpha_f):
    """'cruise', 'cornering', 'limit' or None for a moving sample's speed, lateral acceleration and front slip angle."""
    if alpha_f is None or ay is None or not (math.isfinite(alpha_f) and math.isfinite(ay)):
        return None
    if abs(alpha_f) > LIMIT_ALPHA:
        return 'limit'
    if abs(alpha_f) < STEADY_ALPHA:
        if abs(ay) > CORNER_AY:
            return 'cornering'
        if abs(ay) < CRUISE_AY:
            return 'cruise'
    return None


def sample_case(sample, min_speed):
    """case_of() for a telemetry sample (None when it is slower than `min_speed` or lacks the channels)."""
    speed = sample.speed
    sa, accel = getattr(sample, 'tyre_slip_angle', None), getattr(sample, 'accel', None)
    if speed is None or speed <= min_speed or sa is None or accel is None:
        return None
    return case_of(speed, accel[1], 0.5 * (sa[0] + sa[1]))


class HandForce:
    """Running p50 / p90 of |hand force| (N) per case, for the played force and the car's model force."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.q = {(k, c): ffb_carprops.Quantile(DRIVE_TAU) for k in ('played', 'model') for c in CASES}
        self.seconds = 0.0

    def add(self, case, played_nm, model_nm, rim_mm, dt):
        """One sample of `dt` seconds in `case` (None: not counted), the torques at the rim in Nm."""
        if case is None or not dt > 0.0 or not rim_mm:
            return
        if played_nm is not None and math.isfinite(played_nm):
            self.q['played', case].add(abs(newtons(played_nm, rim_mm)), dt)
        if model_nm is not None and math.isfinite(model_nm):
            self.q['model', case].add(abs(newtons(model_nm, rim_mm)), dt)
        self.seconds += dt

    def summary(self):
        """'hand force p50/p90 N: played cruise 3/6, cornering ..., model ...' for the drive, '' when nothing was counted."""
        if self.seconds <= 0.0:
            return ''
        parts = []
        for kind in ('played', 'model'):
            cells = []
            for c in CASES:
                q = self.q[kind, c]
                p50, p90 = q.value(0.5), q.value(0.9)
                cells.append('%s %s' % (c, 'none' if p50 is None else '%.0f/%.0f' % (p50, p90)))
            parts.append('%s %s' % (kind, ', '.join(cells)))
        return 'hand force p50/p90 N: ' + '; '.join(parts)

    def take(self):
        """The summary, and a fresh start."""
        text = self.summary()
        self.reset()
        return text
