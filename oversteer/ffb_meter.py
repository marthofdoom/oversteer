"""Pure logic for the live force feedback meter in the Force Feedback tab:
scaling the driver's peak level, the peak-hold marker and the clip counter."""

FULL_SCALE = 0x7fff     # the driver's summed force at 100% of the wheel's torque
HOLD_SECONDS = 1.5      # how long the peak marker stays at its highest value
DECAY_PER_SECOND = 1.0  # then it falls this fraction of full scale per second
U32 = 0xffffffff


def level_fraction(level):
    """0.0-1.0 of full scale; above full scale (clipping) stays at 1.0."""
    if not level or level < 0:
        return 0.0
    return min(level / FULL_SCALE, 1.0)


class FfbMeter:
    """Feeds on polled samples. `clips` is the driver's ffb_clip_count (a
    free-running u32) or None when the driver has none; then clips are
    counted from the polled peaks at or above full scale, which misses the
    ones between two polls (`approximate`)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.fraction = 0.0
        self.peak = 0.0
        self.clipped = 0
        self._peak_time = None
        self._base = None
        self._last_time = None

    @property
    def approximate(self):
        return self._base is None

    def update(self, level, now, clips=None):
        """One sample taken at `now` (seconds). Returns the fraction shown."""
        self.fraction = level_fraction(level)
        if clips is not None:
            if self._base is None:
                self._base = clips
                self.clipped = 0
            else:
                self.clipped = (clips - self._base) & U32
        else:
            self._base = None
            if level is not None and level >= FULL_SCALE:
                self.clipped += 1
        if self.fraction >= self.peak or self._peak_time is None:
            self.peak = self.fraction
            self._peak_time = now
        elif now - self._peak_time > HOLD_SECONDS:
            dt = now - max(self._last_time or now, self._peak_time + HOLD_SECONDS)
            self.peak = max(self.fraction, self.peak - DECAY_PER_SECOND * dt)
        self._last_time = now
        return self.fraction

    def clear_clips(self):
        """The Reset button: count from here."""
        self.clipped = 0
        self._base = None       # a driver counter is re-based on the next sample
