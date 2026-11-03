"""How far the handbrake is pulled, for the Controls tab's bar.

One object per axis the bar reads: every update, from an event or from a
poll, goes through the same scaling, so the bar cannot disagree with
itself. The value is the one the virtual (combined) device reports, in the
direction the game receives it; nothing is flipped here.

An analog handbrake rarely travels its whole declared range: the proxy maps
the sensor's declared range onto 0..65535, so the axis the game sees may
rest above its minimum and top out below its maximum (the game calibrates,
a fixed 0..1 bar cannot). The bar learns the travel from what the axis
reports: the lowest reading is rest, the highest is full once the handle
has moved a fair way."""

# The handle must have moved this far (fraction of the declared range)
# before the highest reading counts as the top of the travel; until then the
# declared top is used, so a nudge does not read as a full pull.
MIN_TRAVEL = 0.2
# Within this of either end of the learned travel the bar shows the end.
SNAP = 0.015
# A change smaller than this is sensor noise, not movement.
DEADBAND = 0.01


class HandbrakeDisplay:
    def __init__(self, code, low, high):
        self.code = code
        self.low = low
        self.high = high
        self.seen_low = None
        self.seen_high = None
        self.shown = 0.0

    @classmethod
    def for_axis(cls, axis):
        """From a Device.handbrake_axis() tuple (code, min, max)."""
        return cls(*axis) if axis is not None else None

    def update(self, value):
        """The fraction (0 at rest, 1 fully pulled) for a raw reading."""
        if value is None or self.high <= self.low:
            return self.shown
        span = self.high - self.low
        value = min(self.high, max(self.low, value))
        if self.seen_low is None:
            self.seen_low = self.seen_high = value
        else:
            self.seen_low = min(self.seen_low, value)
            self.seen_high = max(self.seen_high, value)
        floor = self.seen_low
        top = self.seen_high if (self.seen_high - floor) >= MIN_TRAVEL * span else self.high
        if top <= floor:
            return self.shown
        fraction = min(1.0, max(0.0, (value - floor) / (top - floor)))
        if fraction < SNAP:
            fraction = 0.0
        elif fraction > 1.0 - SNAP:
            fraction = 1.0
        if fraction not in (0.0, 1.0) and abs(fraction - self.shown) < DEADBAND:
            return self.shown
        self.shown = fraction
        return fraction
