"""Learning a handbrake's real travel from a few seconds of readings.

The user releases the handbrake, pulls it fully and releases it again while
the virtual (combined) device's axis is sampled. The readings are in the
axis as games receive it, through the mapping the proxy runs now; this
turns them into the `low` / `high` the proxy mapping takes, in the
source's units (see proxy.spec.one_sided_fraction)."""

# The released stretch at the start and end of the sampling window.
REST_WINDOW = 0.8
# A pull must reach this far (fraction of the range) beyond rest.
MIN_TRAVEL = 0.2
# Released readings may wander this much (sensor drift) before the
# handbrake is taken not to have been let go.
MAX_REST_SPREAD = 0.15
# Margin above the highest released reading, fraction of the range.
REST_MARGIN = 0.01


class CalibrationError(ValueError):
    pass


def output_to_source(fraction, invert, src_min, src_max, current=None):
    """The source reading (its own units) that produced an output
    `fraction` (0 rest .. 1 pulled). `current` is the (low, high, dead) of
    a calibration the proxy already applies, or None for the declared
    range. Through an existing calibration the dead band hides where rest
    really is: a fraction of 0 gives the edge of the band, so a new
    calibration made over an old one can narrow it or follow an upward
    drift but not look beyond the old travel."""
    if current is None:
        lo, hi, dead = src_min, src_max, 0.0
    else:
        lo, hi, dead = current
    t = dead + fraction * (1.0 - dead)         # along the travel, from the rest end
    t = (1.0 - t) if invert else t
    return lo + t * (hi - lo)


def learn(samples, duration, invert, src_min, src_max, out_min, out_max, current=None):
    """samples: [(seconds since start, axis value of the virtual device)].
    Returns (low, high) in source units, low < high, for a mapping with the
    given resolved `invert`. Raises CalibrationError with a user-facing
    reason when the sequence is not a release, a full pull and a release."""
    if out_max <= out_min or src_max <= src_min:
        raise CalibrationError("The axis has no range to learn.")
    span = out_max - out_min
    frac = [(t, min(1.0, max(0.0, (v - out_min) / span))) for t, v in samples]
    released = [f for t, f in frac if t <= REST_WINDOW or t >= duration - REST_WINDOW]
    if len(samples) < 20 or not released:
        raise CalibrationError("Too few readings; is the handbrake connected?")
    rest = max(released)
    if rest - min(released) > MAX_REST_SPREAD:
        raise CalibrationError("The handbrake was not released at the start and the end.")
    full = max(f for _, f in frac)
    if full - rest < MIN_TRAVEL:
        raise CalibrationError("The handbrake was not pulled fully.")
    rest = min(1.0, rest + REST_MARGIN)
    a = output_to_source(rest, invert, src_min, src_max, current)
    b = output_to_source(full, invert, src_min, src_max, current)
    return tuple(sorted((a, b)))
