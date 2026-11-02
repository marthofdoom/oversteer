"""The hands-off wheel calibration (ClickUp 1dfcuabc4pj): drives the wheel through the driver's ext_force at
the FF timer rate, reads the wheel's position back, and hands the recording to wheel_calib for the fit.
Also the per-wheel store of the results and the code that writes the correction to the driver.

The hardware is behind a small interface (DeviceIO for a real wheel, tests/wheel_sim.py for a simulated one):
open/close, now/sleep_until, read/write a sysfs attribute, force (ext_force), poll (ABS_X reports since the
last call), position_raw, get/set gain, autocenter and range, max_range and tick_s.

Safety (all enforced here, whatever the step asks for):
- the game's force feedback (and the hardware spring/damper/friction slots) is muted with game_ffb_level 0,
  the centring spring is off and the shaping is identity for the run; everything is put back in a finally,
  game_ffb_level to 100 first and last, and ext_force 0 first and last;
- forces above 60 % of full scale only for 150 ms at a time (then 300 ms at most 60 %);
- within 30 degrees of the lock (half the range set for the run): stop, brake and centre, then go on; the
  third time, or within 8 degrees, the run stops;
- aborts, with ext_force 0 at once: Cancel, window close or quit, the wheel unplugged (a read or write fails),
  turning faster than V_ABORT, a response that doesn't fit the wheel (a push that is much slower or faster
  than the others, no movement when pushed, movement with no force, a brake that doesn't slow it), and an
  overall time limit;
- the driver's watchdog fades ext_force to 0 within 130 ms if this thread stops writing (a crash), and puts
  the game's force back after 1 s.
"""
import atexit
import datetime
import errno
import json
import logging
import math
import os
import threading
import time
from collections import deque
from locale import gettext as _

from . import wheel_calib as wc

# The driver attributes the calibration and the correction need (new-lg4ff with replace mode and shaping)
ATTRS = ('ext_force', 'ext_force_cap', 'game_ffb_level', 'ffb_lut', 'ffb_deadband', 'ffb_dither', 'ffb_soft_clip')
SHAPING = ('ffb_lut', 'ffb_deadband', 'ffb_dither', 'ffb_soft_clip')


class Aborted(Exception):
    """The run stopped before it could finish; `reason` is a keyword, the message is for the user."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


class _NearLock(Exception):
    """Within the lock margin: the current step is dropped and the wheel centred."""


def _sign(x):
    return 1.0 if x > 0 else -1.0 if x < 0 else 0.0


class Calibration:
    """One run on one wheel. run() blocks for about two minutes and returns the analysis (wheel_calib.analyse
    plus the run's date and range) or raises Aborted; cancel() may be called from any thread."""

    SWEEP_MAX = 0.6             # largest force outside the short pulses (full-scale share)
    PULSE_MAX_S = 0.150         # forces above SWEEP_MAX last at most this long...
    PULSE_COOLDOWN_S = 0.300    # ...and the next one waits this long
    SWEEP_PULSE_S = 0.400       # a pulse at most SWEEP_MAX lasts at most this long
    LOCK_MARGIN_DEG = 30.0      # stop and centre this close to the lock
    HARD_MARGIN_DEG = 8.0       # stop the run this close (or this close while centring)
    NEAR_LOCK_LIMIT = 3
    V_ABORT = 1000.0            # deg/s: faster than this, the run stops (force 0)
    V_STOP = 500.0              # deg/s: a pulse ends here
    V_KICK = 100.0              # deg/s: the push that starts each pulse runs until this
    V_RUN = 300.0               # deg/s: ...and then on to this before the pulse
    V_MOVE = 180.0              # deg/s: moving between positions
    KICK = 0.35                 # the push, unless twice the breakaway is more
    KICK_START_S = 0.200        # a push that moves nothing in this long: held
    KICK_MAX_S = 0.500
    MOVE_MAX = 0.35             # largest force while moving between positions
    RAMP_RATE = 0.15            # full scale per second, the slow breakaway ramps
    RAMP_MOVE_DEG = 0.2         # movement that counts as breaking away
    SETTLE_TOL_DEG = 2.0        # a still wheel with no force must not move more than this
    STILL_DEG_S = 15.0
    VEL_WINDOW_S = 0.010
    TOTAL_S = 300.0
    PREP_S = 0.35               # game force ramps to 0 over 200 ms
    BREAKAWAY_ANGLES = 8
    REPEATS = 2
    GRAB_RATIO = 2.0            # a push accelerating the wheel this much less (hands on it) or more (pushed
                                # along) than the others stops the run

    def __init__(self, io, progress=None):
        self.io = io
        self.progress = progress
        self.cancel_event = threading.Event()
        self.rec = wc.Recording()
        self.saved = {}
        self.sign = 1.0                 # ext_force = sign * w * 32767
        self.sign_known = False
        self.lock = 450.0
        self.theta = 0.0
        self.omega = 0.0
        self.hist = deque()
        self.last_raw = None
        self.near_lock_events = 0
        self.recovering = False
        self.breakaway_max = 0.05
        self.kick = self.KICK
        self.a_fs = 20000.0             # deg/s^2 at full scale: high (soft loops) until the first pushes
        self.a_kick = 300.0             # deg/s^2 the push gives (a floor for braking)
        self.kick_accels = {1.0: [], -1.0: []}
        self._latency = 0.03            # command to movement, raised to what the pushes show
        self.done_units = 0
        self.total_units = 4 + self.BREAKAWAY_ANGLES + len(wc.KNOTS) * 2 * self.REPEATS + 1
        self._high_since = None
        self._high_end = -1e9
        self._next = 0.0
        self.t0 = 0.0
        self.period = 0.002
        self.log = []                   # (time, event) for the report and the tests

    # --- the public side ---

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        _running.add(self)
        try:
            self._check_free()              # before anything is changed: nothing to put back if it isn't
            try:
                self._prepare()
                self._measure()
            except OSError as e:
                raise Aborted('unplugged', _("The wheel stopped answering (unplugged?): {}").format(e)) from e
            except _NearLock as e:
                raise Aborted('near_lock', _("The wheel is too close to its lock. Turn it towards the centre "
                                             "and try again.")) from e
            finally:
                self._restore()
            self._report(_("Analysing"))
            try:
                result = wc.analyse(self.rec)
            except wc.FitError as e:
                raise Aborted('fit', _("The measurement could not be used: {}").format(e)) from e
            result['date'] = datetime.datetime.now().isoformat(timespec='seconds')
            result['range'] = int(round(self.lock * 2))
            result['near_lock_events'] = self.near_lock_events
            result['duration_s'] = round(self.rec.cmd_t[-1] - self.rec.cmd_t[0], 1) if self.rec.cmd_t else 0
            return result
        finally:
            _running.discard(self)

    # --- set up and put back ---

    def _check_free(self):
        try:
            busy = int(self.io.read('ext_force') or 0)
            muted = int(self.io.read('game_ffb_level') or 100) < 100
        except ValueError:
            busy, muted = 0, False
        except OSError as e:
            raise Aborted('unplugged', _("The wheel stopped answering (unplugged?): {}").format(e)) from e
        if busy or muted:
            raise Aborted('busy', _("Something else is driving the wheel's force right now (Countersteer FFB?)."))

    def _prepare(self):
        io = self.io
        for name in ATTRS:
            if name != 'ext_force':
                self.saved[name] = io.read(name)
        self.saved['gain'] = io.get_gain()
        self.saved['autocenter'] = io.get_autocenter()
        self.saved['range'] = io.get_range()
        io.open()
        io.force(0)
        io.write('game_ffb_level', '0')
        io.set_autocenter(0)
        io.set_gain(100)
        io.write('ext_force_cap', '100')
        io.write('ffb_lut', 'identity')
        io.write('ffb_deadband', '0')
        io.write('ffb_dither', '0')
        io.write('ffb_soft_clip', '0')
        max_range = io.max_range()
        io.set_range(max_range)
        self.lock = max_range / 2.0
        self.rec.counts_per_deg = 65536.0 / max_range
        self.period = max(io.tick_s, 0.001)
        self.t0 = io.now()
        self._next = self.t0
        raw = io.position_raw()
        self.last_raw = raw
        self.theta = self._deg(raw)
        self.hist.append((self.t0, self.theta))
        self.rec.position(self.t0, self.theta)
        self._hold(self.PREP_S)

    def _restore(self):
        io = self.io

        def attempt(fn, *args):
            try:
                fn(*args)
            except Exception as e:      # each one on its own: one failing must not skip the rest
                logging.debug("calibration restore %s%s: %s", getattr(fn, '__name__', fn), args, e)

        attempt(io.force, 0)
        attempt(io.write, 'game_ffb_level', '100')
        for name in SHAPING + ('ext_force_cap',):
            if name in self.saved:
                attempt(io.write, name, self.saved[name])
        # what was asked for during the run (a profile loaded meanwhile), else what was there before
        asked = {}
        try:
            asked = io.deferred()
        except Exception:
            pass
        for key, setter in (('gain', io.set_gain), ('range', io.set_range), ('autocenter', io.set_autocenter)):
            value = asked.get(key, self.saved.get(key))
            if value is not None:
                attempt(setter, value)
        attempt(io.force, 0)
        attempt(io.write, 'game_ffb_level', '100')
        attempt(io.close)

    # --- one tick ---

    def _deg(self, raw):
        return (raw - 32768) / 32768.0 * self.lock

    def _limit(self, w, now):
        w = max(-1.0, min(1.0, float(w)))
        if abs(w) <= self.SWEEP_MAX:
            if self._high_since is not None:
                self._high_end = now
                self._high_since = None
            return w
        if self._high_since is None:
            if now - self._high_end < self.PULSE_COOLDOWN_S:
                return _sign(w) * self.SWEEP_MAX
            self._high_since = now
        if now - self._high_since > self.PULSE_MAX_S:
            self._high_end = now
            self._high_since = None
            return _sign(w) * self.SWEEP_MAX
        return w

    def _zero(self):
        try:
            self.io.force(0)
        except OSError:
            pass

    def _tick(self, w):
        """Write one force, wait for the next tick, read the wheel and check it. Returns the new reports."""
        if self.cancel_event.is_set():
            self._zero()
            raise Aborted('cancelled', _("Calibration cancelled."))
        now = self.io.now()
        if now - self.t0 > self.TOTAL_S:
            self._zero()
            raise Aborted('timeout', _("The calibration took too long."))
        w = self._limit(w, now)
        raw = int(round(self.sign * w * wc.FULL))
        raw = max(-32768, min(32767, raw))
        try:
            self.io.force(raw)
        except OSError as e:
            raise Aborted('unplugged', _("The wheel stopped answering (unplugged?): {}").format(e)) from e
        self.rec.command(now, w)
        self._next += self.period
        if self._next < now:
            self._next = now + self.period
        self.io.sleep_until(self._next)
        events = self._read()
        self._check()
        return events

    def _read(self):
        try:
            reports = self.io.poll()
        except OSError as e:
            self._zero()
            raise Aborted('unplugged', _("The wheel stopped answering (unplugged?): {}").format(e)) from e
        events = []
        for t, raw in reports:
            if self.last_raw is not None and raw != self.last_raw:
                self.rec.raw_steps.append(abs(raw - self.last_raw))
            self.last_raw = raw
            deg = self._deg(raw)
            self.rec.position(t, deg)
            self.hist.append((t, deg))
            events.append((t, deg))
            self.theta = deg
        now = self.io.now()
        while len(self.hist) > 2 and self.hist[1][0] < now - 0.1:
            self.hist.popleft()
        ref_t = now - self.VEL_WINDOW_S
        ref = self.hist[0][1]
        for t, deg in self.hist:
            if t > ref_t:
                break
            ref = deg
        self.omega = (self.theta - ref) / self.VEL_WINDOW_S
        return events

    def _check(self):
        if abs(self.omega) > self.V_ABORT:
            self._zero()
            raise Aborted('overspeed', _("The wheel turned faster than {:.0f} degrees a second.").format(self.V_ABORT))
        hard = self.recovering or not self.sign_known
        margin = self.HARD_MARGIN_DEG if hard else self.LOCK_MARGIN_DEG
        if abs(self.theta) > self.lock - margin:
            if hard:
                self._zero()
                raise Aborted('near_lock', _("The wheel is too close to its lock. Turn it towards the centre "
                                             "and try again."))
            raise _NearLock()

    def _now(self):
        return self.io.now()

    def _report(self, text):
        if self.progress is not None:
            try:
                self.progress(min(self.done_units / self.total_units, 1.0), text)
            except Exception:
                logging.exception("calibration progress")

    def _unit(self, text):
        self.done_units += 1
        self._report(text)

    def _grabbed(self, why):
        self._zero()
        self.log.append((self._now(), 'grabbed: ' + why))
        return Aborted('grabbed', _("The wheel did not respond the way it did before ({}). Keep your hands off "
                                    "the wheel during the calibration.").format(why))

    # --- motions ---

    def _hold(self, seconds):
        end = self._now() + seconds
        while self._now() < end:
            self._tick(0.0)

    @property
    def kv(self):
        """Velocity feedback (full scale per deg/s): a 12/s velocity loop for this wheel, slow enough for 40 ms
        of latency and a strength estimate three times too low."""
        return 12.0 / self.a_fs

    def _brake(self):
        """Stop the wheel: velocity feedback over a floor just under the breakaway force (which brakes even
        before the wheel's strength is known), at most SWEEP_MAX."""
        start = self._now()
        w0 = abs(self.omega)
        still = 0
        while still < 2:
            if abs(self.omega) < self.STILL_DEG_S:
                still += 1
                self._tick(0.0)
                continue
            still = 0
            floor = 0.8 * self.breakaway_max if abs(self.omega) > 2 * self.STILL_DEG_S else 0.0
            w = -_sign(self.omega) * min(self.SWEEP_MAX, floor + self.kv * abs(self.omega))
            self._tick(w)
            elapsed = self._now() - start
            if elapsed > 0.4 and w0 > 100 and abs(self.omega) > 0.7 * w0:
                raise self._grabbed(_("it did not slow down when braked"))
            if elapsed > 1.2:
                raise self._grabbed(_("it kept turning when braked"))

    def _settle(self, seconds, check=True):
        if abs(self.omega) >= self.STILL_DEG_S:
            self._brake()
        begin = self._now()
        end = begin + seconds
        start = None
        while self._now() < end:
            self._tick(0.0)
            if start is None:
                if self._now() - begin >= 0.04:            # what was left of the movement has stopped
                    start = self.theta
            elif check and abs(self.theta - start) > self.SETTLE_TOL_DEG:
                raise self._grabbed(_("it moved with no force on it"))

    def _move_to(self, target, tol=3.0, timeout=3.0, fatal=False):
        """Turn the wheel to `target` degrees under a velocity loop (at most MOVE_MAX force, V_MOVE)."""
        start = self._now()
        still_since = None
        stalled_since = None
        boost = 0.0
        a_dec = 0.5 * self.a_kick
        while True:
            e = target - self.theta
            now = self._now()
            if abs(e) < tol or abs(self.omega) > 1.5 * self.V_MOVE:
                # there (or too fast): brake to a stop
                w = -_sign(self.omega) * min(self.MOVE_MAX, self.kv * abs(self.omega) +
                                             (0.8 * self.breakaway_max if abs(self.omega) > 30 else 0.0))
                if abs(e) < tol and abs(self.omega) < self.STILL_DEG_S:
                    still_since = now if still_since is None else still_since
                    if now - still_since >= 0.06:
                        self._tick(0.0)
                        return True
                else:
                    still_since = None
                boost = 0.0
            else:
                still_since = None
                v_ref = _sign(e) * min(self.V_MOVE, math.sqrt(2 * a_dec * abs(e)), 6.0 * abs(e))
                if abs(self.omega) < 5.0:
                    boost = min(boost + 0.1 * self.period, self.MOVE_MAX)    # stuck: more push, slowly
                    ff = 0.9 * self.breakaway_max + boost
                else:
                    boost = max(boost - 0.5 * self.period, 0.0)
                    ff = 0.6 * self.breakaway_max + boost                     # sliding: less than breakaway
                w = self.kv * (v_ref - self.omega) + _sign(v_ref) * ff
                w = max(-self.MOVE_MAX, min(self.MOVE_MAX, w))
                # pushed well past its breakaway and still not turning, or only creeping: held
                if abs(w) >= min(self.MOVE_MAX, 1.5 * self.breakaway_max + 0.05) and \
                        abs(self.omega) < max(5.0, 0.25 * abs(v_ref)):
                    stalled_since = now if stalled_since is None else stalled_since
                    if now - stalled_since > 0.4 and not self.recovering:
                        raise self._grabbed(_("it did not turn when pushed"))
                else:
                    stalled_since = None
            self._tick(w)
            if self._now() - start > timeout:
                if fatal:
                    self._zero()
                    raise Aborted('stalled', _("The wheel could not be turned back to the centre."))
                self.log.append((self._now(), 'move timeout'))
                return False

    def _ramp(self, direction):
        """Slowly raise the force in one direction (wheel frame, or ext_force's before the sign is known) until
        the wheel moves. Returns (degrees moved, force then)."""
        start = self._now()
        theta0 = self.theta
        while True:
            w = self.RAMP_RATE * (self._now() - start)
            if w > self.SWEEP_MAX:
                self._zero()
                raise Aborted('stalled', _("The wheel did not move at {:.0f}% force: is it held or blocked?")
                              .format(self.SWEEP_MAX * 100))
            self._tick(direction * w)
            moved = self.theta - theta0
            if abs(moved) >= self.RAMP_MOVE_DEG:
                self._tick(0.0)
                return moved, w

    def _breakaway(self, direction):
        angle = self.theta
        moved, w = self._ramp(direction)
        if moved * direction < 0:
            raise self._grabbed(_("it turned the wrong way"))
        before = sorted(b for _a, _d, b in self.rec.breakaway)
        if len(before) >= 2:
            median = before[len(before) // 2]
            if w > max(2.5 * median, median + 0.1):
                raise self._grabbed(_("it took {:.0f}% force to move instead of {:.0f}%").format(w * 100, median * 100))
        self.rec.breakaway.append((angle, int(direction), w))
        self.breakaway_max = max(self.breakaway_max, w)
        self._settle(0.15, check=False)

    def _kick(self, d):
        """The push that starts every pulse: KICK from rest until V_KICK (or KICK_MAX_S). Its acceleration
        from the first movement is the reference that tells a free wheel from a held one. Returns it."""
        t_on = self._now()
        theta0 = self.theta
        first = None
        while True:
            events = self._tick(d * self.kick)
            now = self._now()
            if first is None:
                first = next((t for t, deg in events if deg != theta0), None)
                if first is None and now - t_on > self.KICK_START_S:
                    raise self._grabbed(_("it did not turn when pushed"))
            if d * self.omega >= self.V_KICK or now - t_on > self.KICK_MAX_S:
                break
        self.rec.onsets.append((t_on, first))
        self._latency = max(self._latency, min(first - t_on, 0.1))
        accel = d * self.omega / max(self._now() - first, self.period)
        if accel <= 0:
            raise self._grabbed(_("it turned the wrong way when pushed"))
        refs = self.kick_accels[d]
        if len(refs) >= 2:
            ref = sorted(refs)[len(refs) // 2]
            if not ref / self.GRAB_RATIO < accel < ref * self.GRAB_RATIO:
                raise self._grabbed(_("a push accelerated it at {:.0f} instead of {:.0f} degrees/s²")
                                    .format(accel, ref))
        refs.append(accel)
        return accel

    def _learn_strength(self, accel):
        """Tune the movement loops from a push. Its acceleration is a floor for braking at SWEEP_MAX (more
        force, and friction helps); the full-scale figure only sets the loop gains, where an error costs
        speed, not safety."""
        self.a_kick = max(300.0, accel)
        self.a_fs = max(500.0, self.a_kick / max(self.kick - 0.8 * self.breakaway_max, 0.1))
        self.log.append((self._now(), 'push {:.0f} deg/s2, full scale estimate {:.0f}'.format(self.a_kick, self.a_fs)))

    def _usable(self):
        return self.lock - self.LOCK_MARGIN_DEG

    def _shot_start(self, d):
        """Where a pulse in direction d starts: on the other side, so it runs through the centre."""
        return -d * min(90.0, 0.25 * self._usable())

    def _shot(self, level, d):
        """One pulse at `level`. The wheel is pushed from the far side (KICK, a sweep-level force) up to a run-up
        speed, then the pulse is played against the movement: it brakes the wheel and, past the breakaway,
        turns it back until it is as fast the other way. Speed never exceeds the run-up's, and one pulse gives
        the level's acceleration with and against the movement."""
        start = self._shot_start(d)
        self._move_to(start)
        self._settle(0.15)
        self._kick(d)
        v_run = min(self.V_RUN, math.sqrt(self.a_kick * abs(start)))
        end = min(abs(start) + 90.0, 0.6 * self._usable())
        lead = self._latency + self.VEL_WINDOW_S
        history = deque([(self._now(), self.omega)])

        def peak():
            now = self._now()
            history.append((now, self.omega))
            while len(history) > 2 and history[1][0] < now - 0.01:
                history.popleft()
            span = now - history[0][0]
            accel = (self.omega - history[0][1]) / span if span > 0 else 0.0
            # the speed once a force stopped now has stopped acting
            return self.omega + accel * lead

        def too_far(p):
            # where the wheel would stop if braked now (KICK's acceleration is a floor for the brake)
            return _sign(self.omega) * self.theta + abs(self.omega) * lead + p * p / (2 * self.a_kick) > end

        while True:                                          # run up
            self._tick(d * self.kick)
            p = peak()
            if d * p >= v_run or too_far(p):
                break
        t_pulse = self._now()
        # Above SWEEP_MAX the 150 ms pulse limit; at sweep levels up to 400 ms, which is where the slow end of
        # the coast (friction against damping) and the small forces' data come from
        window = self.PULSE_MAX_S - 0.01 if level > self.SWEEP_MAX else self.SWEEP_PULSE_S
        while True:                                          # the pulse, against the movement
            self._tick(-d * level)
            p = peak()
            elapsed = self._now() - t_pulse
            if elapsed > window or -d * p >= v_run or abs(p) > self.V_STOP or too_far(p):
                break
            if elapsed > 0.03 and abs(self.omega) < 20.0:
                break                                        # stopped, and the pulse can't turn it back
        self._brake()
        self._settle(0.1, check=False)

    def _step(self, fn, *args):
        """One step; near the lock it is dropped and the wheel centred (the third time, the run stops)."""
        try:
            return fn(*args)
        except _NearLock:
            self.near_lock_events += 1
            self.log.append((self._now(), 'near lock at {:.0f}'.format(self.theta)))
            logging.warning("calibration: %.0f degrees, within %.0f of the lock; centring", self.theta,
                            self.LOCK_MARGIN_DEG)
            self._zero()
            self.recovering = True
            try:
                self._brake()
                self._move_to(0.0, tol=10.0, timeout=8.0, fatal=True)
                self._settle(0.2, check=False)
            finally:
                self.recovering = False
            if self.near_lock_events >= self.NEAR_LOCK_LIMIT:
                raise Aborted('near_lock', _("The wheel kept getting close to its lock; the calibration stopped."))
            return None

    # --- the procedure ---

    def _measure(self):
        # Which way ext_force turns this wheel, and the breakaway where it stands
        self._report(_("Finding the direction"))
        moved, w = self._ramp(1.0)
        self.sign = _sign(moved)
        self.sign_known = True
        self.recovering = True              # still near where it stood: only the hard margin
        try:
            self.rec.breakaway.append((self.theta - moved, int(self.sign), w))
            self.breakaway_max = max(self.breakaway_max, w)
            self._settle(0.15, check=False)
            self._unit(_("Breakaway"))
            # the other way: away from the nearer lock
            towards_centre = -_sign(self.theta) or -self.sign
            self._breakaway(towards_centre)
            self._unit(_("Breakaway"))
            self.kick = max(self.KICK, min(self.SWEEP_MAX, 2.0 * self.breakaway_max))
            # One push towards the centre for the wheel's strength: the movement loops are tuned from it
            self._settle(0.15, check=False)
            self._learn_strength(self._kick(towards_centre))
            self._brake()
            self._settle(0.1, check=False)
        finally:
            self.recovering = False
        self._unit(_("Measuring the wheel's strength"))
        # From the centre, a push each way (the other's reference)
        accels = []
        for d in (-towards_centre, towards_centre):
            self._step(self._move_to, 0.0)
            self._step(self._settle, 0.15)
            a = self._step(self._kick, d)
            self._step(self._brake)
            self._step(self._settle, 0.1, False)
            if a:
                accels.append(a)
        if accels:
            self._learn_strength(sorted(accels)[len(accels) // 2])
        self._unit(_("Measuring the wheel's strength"))

        # Breakaway around the rim
        span = min(270.0, 0.6 * self._usable())
        n = self.BREAKAWAY_ANGLES
        for i in range(n):
            angle = -span + 2 * span * i / (n - 1)
            if self._step(self._move_to, angle) is not None:
                self._step(self._settle, 0.15)
                self._step(self._breakaway, 1.0)
                self._step(self._breakaway, -1.0)
            self._unit(_("Breakaway at {:.0f} degrees").format(angle))
        self._step(self._move_to, 0.0)

        # Pulses at each level, both ways, up then down
        for rep in range(self.REPEATS):
            levels = wc.KNOTS if rep % 2 == 0 else tuple(reversed(wc.KNOTS))
            for level in levels:
                for d in (1.0, -1.0):
                    self._step(self._shot, level, d)
                    self._unit(_("Force response {:.0f}%").format(level * 100))

        self._step(self._move_to, 0.0)
        self._step(self._settle, 0.2, False)
        self._unit(_("Done"))


# Calibrations running now: the process stops none of them without putting the wheel back
_running = set()


def _cancel_all():
    for cal in list(_running):
        cal.cancel()
    deadline = time.monotonic() + 1.5
    while _running and time.monotonic() < deadline:
        time.sleep(0.01)


atexit.register(_cancel_all)


class DeviceIO:
    """A real wheel (oversteer.device.Device) for Calibration: sysfs attributes next to the wheel's other
    settings, ext_force kept open and written at offset 0, and an input node of its own (each open node gets
    every report, so the window's own reading is not disturbed)."""

    def __init__(self, device):
        self.device = device
        self.tick_s = device.get_ffb_tick_ms() / 1000.0
        self._force_fd = None
        self._input = None
        self._clock_offset = 0.0

    @staticmethod
    def supported(device):
        try:
            return bool(device.dev_path) and all(device.checked_device_file(n) for n in ATTRS)
        except Exception:
            return False

    def read(self, name):
        with open(self.device.device_file(name), 'r') as f:
            return f.read().strip()

    def write(self, name, value):
        with open(self.device.device_file(name), 'w') as f:
            f.write(str(value))

    def open(self):
        from evdev import InputDevice, ecodes
        self.device.hold_settings()
        self._force_fd = os.open(self.device.device_file('ext_force'), os.O_WRONLY)
        node = self.device._input_node()
        if node is None:
            raise OSError(errno.ENODEV, "the wheel's input device can't be read")
        self._input = InputDevice(node)
        self._abs_x = ecodes.ABS_X
        self._ev_abs = ecodes.EV_ABS
        # Reports carry the wall clock; the loop runs on the monotonic one
        self._clock_offset = time.time() - time.monotonic()

    def close(self):
        try:
            if self._force_fd is not None:
                try:
                    os.close(self._force_fd)
                finally:
                    self._force_fd = None
            if self._input is not None:
                try:
                    self._input.close()
                finally:
                    self._input = None
        finally:
            self.device.release_settings()

    def deferred(self):
        """The range, gain and centring spring asked for while the run held them."""
        held = self.device._held or {}
        names = {'range': 'range', 'ff_gain': 'gain', 'autocenter': 'autocenter'}
        return {names[k]: v for k, v in held.items() if k in names}

    def now(self):
        return time.monotonic()

    def sleep_until(self, t):
        delay = t - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def force(self, raw):
        if self._force_fd is None:
            self.write('ext_force', int(raw))
        else:
            os.pwrite(self._force_fd, str(int(raw)).encode(), 0)

    def position_raw(self):
        code, value = self.device._normalized_axis(self._abs_x, self._input.absinfo(self._abs_x).value)
        return value

    def poll(self):
        out = []
        now = time.monotonic()
        while True:
            event = self._input.read_one()
            if event is None:
                return out
            if event.type != self._ev_abs:
                continue
            event = self.device.normalize_event(event)
            if event.code != self._abs_x:
                continue
            t = event.timestamp() - self._clock_offset
            if not now - 1.0 < t <= now + 0.005:      # the wall clock jumped: take the read time
                t = now
            out.append((t, event.value))

    def get_gain(self):
        return self.device.get_ff_gain()

    def set_gain(self, value):
        self.device._set_ff_gain(value)

    def get_autocenter(self):
        return self.device.get_autocenter()

    def set_autocenter(self, value):
        self.device._set_autocenter(value)

    def get_range(self):
        return self.device.get_range()

    def set_range(self, value):
        self.device._set_range(value)

    def max_range(self):
        return self.device.get_max_range() or 900


# --- per-wheel store and the correction ---

STORE_NAME = 'wheel_calibration.json'


def wheel_key(device):
    """The store's key for a wheel: its USB vendor:product (the model), so it survives another port."""
    return getattr(device, 'usb_id', None) or getattr(device, 'name', None) or str(device.get_id())


def load_store(path):
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_store(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def new_entry(result, old=None):
    """The store entry after a successful run: the correction on, dither as recommended unless the user
    chose, the soft clip kept."""
    old = old or {}
    entry = {
        'version': 1,
        'result': result,
        'use': True,
        'dither': old['dither'] if old.get('dither_chosen') else bool(result.get('dither')),
        'dither_chosen': bool(old.get('dither_chosen')),
        'soft_clip': int(old.get('soft_clip', 0)),
    }
    return entry


def valid_lut(lut):
    return (isinstance(lut, list) and 2 <= len(lut) <= 65 and all(isinstance(v, int) and 0 <= v <= wc.FULL
            for v in lut) and all(b >= a for a, b in zip(lut, lut[1:])))


def correction_values(entry):
    """{attribute: text} to write for a store entry: the LUT and deadband when the correction is on
    (identity and 0 when off), the dither and soft clip as chosen."""
    result = entry.get('result') or {}
    lut = result.get('lut')
    use = bool(entry.get('use')) and valid_lut(lut)
    deadband = int(result.get('deadband') or 0) if use else 0
    return {
        'ffb_lut': ' '.join(str(v) for v in lut) if use else 'identity',
        'ffb_deadband': str(max(0, min(wc.FULL, deadband))),
        'ffb_dither': '1' if entry.get('dither') else '0',
        'ffb_soft_clip': str(max(0, min(100, int(entry.get('soft_clip') or 0)))),
    }


def apply_correction(write, entry):
    """Write an entry's correction with write(name, text); False if any write failed."""
    ok = True
    for name, value in correction_values(entry).items():
        try:
            write(name, value)
        except (OSError, ValueError) as e:
            logging.warning("wheel correction %s: %s", name, e)
            ok = False
    return ok


def summary(result):
    """One line of what a run measured, for the FFB tab and the dialog after it."""
    parts = []
    if result.get('deadband') is not None:
        parts.append(_("dead zone {:.1f} %").format(result['deadband'] / wc.FULL * 100))
    if result.get('breakaway') is not None:
        parts.append(_("breakaway {:.1f} %").format(result['breakaway'] * 100))
    if result.get('friction') is not None:
        parts.append(_("friction {:.1f} %").format(result['friction'] * 100))
    if result.get('fit_latency_ms') is not None:
        parts.append(_("latency {:.0f} ms").format(result['fit_latency_ms']))
    if result.get('encoder_deg'):
        parts.append(_("encoder step {:.3f}°").format(result['encoder_deg']))
    return ', '.join(parts)
