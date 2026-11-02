"""Pure logic for the live force feedback meter in the Force Feedback tab:
scaling the driver's peak level, the peak-hold marker, the clip counter and
the poller that feeds them."""

import logging

FULL_SCALE = 0x7fff     # the driver's summed force at 100% of the wheel's torque
HOLD_SECONDS = 1.5      # how long the peak marker stays at its highest value
DECAY_PER_SECOND = 1.0  # then it falls this fraction of full scale per second
U32 = 0xffffffff
WRAP_NEAR = 0x40000000  # a count dropping from the top to the bottom quarter wrapped


def level_fraction(level):
    """0.0-1.0 of full scale; above full scale (clipping) stays at 1.0."""
    if not level or level < 0:
        return 0.0
    return min(level / FULL_SCALE, 1.0)


class FfbMeter:
    """Feeds on polled samples. `clips` is the driver's ffb_clip_count (a
    free-running u32 of timer ticks above full scale) or None when the
    driver has none; then clips are counted from the polled peaks above
    full scale, which misses the ones between two polls (`approximate`)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.fraction = 0.0
        self.over = False
        self.peak = 0.0
        self.clipped = 0
        self.driver = False
        self._peak_time = None
        self._base = None
        self._last_raw = None
        self._last_time = None

    @property
    def approximate(self):
        return not self.driver

    def update(self, level, now, clips=None):
        """One sample taken at `now` (seconds). Returns the fraction shown."""
        self.fraction = level_fraction(level)
        self.over = bool(level) and level > FULL_SCALE
        if clips is not None:
            if self._base is None or not self.driver:
                self._base = clips
                self.clipped = 0
            elif clips < self._last_raw and not (self._last_raw - clips > U32 - WRAP_NEAR):
                # the driver's counter restarted (reload, replug): carry on from here
                self._base = (clips - self.clipped) & U32
            self.driver = True
            self._last_raw = clips
            self.clipped = (clips - self._base) & U32
        else:
            self.driver = False
            self._base = self._last_raw = None
            if self.over:
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
        if self.driver:
            self._base = self._last_raw


class FfbPoller:
    """The timer's body without the toolkit: reads the controller while
    `mapped()` and tells the caller whether to keep the timer. An unplugged
    wheel or a failing read never stops it for good; it ends only when the
    widget is no longer mapped, and `running` then clears so the next map
    re-arms it."""

    def __init__(self, meter, controller, mapped, consumer='meter'):
        self.meter = meter
        self.controller = controller
        self.mapped = mapped
        self.consumer = consumer
        self.running = False
        self._warned = False

    def arm(self):
        """True if the caller should start its timer now."""
        if self.running:
            return False
        self.running = True
        self.controller.ffb_consumer_start(self.consumer)
        return True

    def tick(self, now):
        if not self.mapped():
            self.running = False
            return False
        try:
            level = self.controller.read_ffbmeter(self.consumer)
            if level is not None:
                self.meter.update(level, now, self.controller.read_ffb_clips())
        except Exception:
            if not self._warned:
                self._warned = True
                logging.exception("FFB meter read failed")
        return True
