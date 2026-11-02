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
import gc
import json
import logging
import math
import os
import threading
import time
from collections import deque
from locale import gettext as _

from . import wheel_calib as wc
from . import wheel_owner

# The driver attributes the calibration and the correction need (new-lg4ff with replace mode and shaping)
ATTRS = ('ext_force', 'ext_force_cap', 'game_ffb_level', 'ffb_lut', 'ffb_deadband', 'ffb_dither', 'ffb_soft_clip')
SHAPING = ('ffb_lut', 'ffb_deadband', 'ffb_dither', 'ffb_soft_clip')


class Aborted(Exception):
    """The run stopped before it could finish; `reason` is a keyword, the message is for the user."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


class ProxyLost(Exception):
    """The proxy whose virtual device the wheel is read through stopped or restarted (DeviceIO)."""


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
    BLIND_DEG = 4.0             # the reported position and the axis's current value may differ this much
    TICK_GAP_S = 0.020          # a pause between writes this long during a pulse above SWEEP_MAX stops the run
    EMERGENCY_BRAKE_S = 0.200   # stopping at the hard margin: brake this long at most, then force 0
    STALE_SAMPLES = 3           # ext_force reads taken (50 ms apart) to tell a stale value from a live writer

    def __init__(self, io, progress=None, pending_path=None, pending_key=None):
        self.io = io
        self.progress = progress
        self.pending_path = pending_path    # crash record: what to put back if this process dies mid-run
        self.pending_key = pending_key
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
        self._last_tick = None
        self._last_w = 0.0
        self.t0 = 0.0
        self.period = 0.002
        self.log = []                   # (time, event) for the report and the tests

    # --- the public side ---

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        _running.add(self)
        gc_was = gc.isenabled()
        owner = None
        try:
            # The wheel exclusively first (wheel_owner): Countersteer FFB lets go (and puts the game's force back
            # itself) or the run is refused before any write; only then may _check_free clear a stale ext_force
            owner = self._acquire_owner()
            self._check_free()              # before anything is changed: nothing to put back if it isn't
            # no collector pauses in the middle of a pulse
            gc.collect()
            gc.freeze()
            gc.disable()
            try:
                self._prepare()
                self._measure()
            except PermissionError as e:
                raise Aborted('permission', _("Oversteer may not change this wheel's settings ({}). The udev "
                                              "rules that come with Oversteer give that permission.").format(e)) from e
            except OSError as e:
                raise Aborted('unplugged', _("The wheel stopped answering (unplugged?): {}").format(e)) from e
            except _NearLock as e:
                raise Aborted('near_lock', _("The wheel is too close to its lock. Turn it towards the centre "
                                             "and try again.")) from e
            finally:
                self._restore()
                self._release_owner(owner)  # the wheel is back: free for others while the fit runs
                owner = None
                gc.unfreeze()
                if gc_was:
                    gc.enable()
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
            if gc_was:
                gc.enable()
            self._release_owner(owner)      # a safety net: normally released after the restore above
            _running.discard(self)

    OWNER = 'calibration'

    def _acquire_owner(self):
        """Hold the wheel exclusively (wheel_owner). Returns the wheel held, None for an IO without one;
        raises Aborted('busy') naming the holder if it can't be had."""
        wheel = getattr(self.io, 'wheel_id', None)
        if wheel is None:
            return None
        if not wheel_owner.acquire(wheel, self.OWNER, exclusive=True):
            held = wheel_owner.current(wheel)
            raise Aborted('busy', _("The wheel is in use by {}: try again in a moment.").format(
                held[0] if held else _("another part of Oversteer")))
        return wheel

    def _release_owner(self, wheel):
        if wheel is not None:
            wheel_owner.release(wheel, self.OWNER)

    # --- set up and put back ---

    def _check_free(self):
        """Nothing else may be driving the force. The driver's ext_force reads the last value written even
        after its watchdog faded it out: a value that stays the same while the game's force is at 100 % is
        such a leftover (an application that stopped without writing 0), and is cleared."""
        io = self.io
        try:
            samples = []
            for i in range(self.STALE_SAMPLES):
                if i:
                    io.sleep_until(io.now() + 0.05)
                samples.append((int(io.read('ext_force') or 0), int(io.read('game_ffb_level') or 100)))
        except ValueError:
            return
        except PermissionError as e:
            raise Aborted('permission', _("Oversteer may not read this wheel's settings ({}).").format(e)) from e
        except OSError as e:
            raise Aborted('unplugged', _("The wheel stopped answering (unplugged?): {}").format(e)) from e
        forces = {f for f, _l in samples}
        if any(level < 100 for _f, level in samples) or len(forces) > 1:
            raise Aborted('busy', _("Something else is driving the wheel's force right now (Countersteer FFB?)."))
        if forces != {0}:
            logging.info("calibration: ext_force %s is left over from an earlier writer; clearing it", forces)
            try:
                io.write('ext_force', '0')
            except OSError as e:
                raise Aborted('unplugged', _("The wheel stopped answering (unplugged?): {}").format(e)) from e

    def _prepare(self):
        io = self.io
        io.hold()                           # from now on nobody else changes range, gain or centring
        for name in ATTRS:
            if name != 'ext_force':
                self.saved[name] = io.read(name)
        self.saved['gain'] = io.get_gain()
        self.saved['autocenter'] = io.get_autocenter()
        self.saved['range'] = io.get_range()
        if self.pending_path:
            write_pending(self.pending_path, self.pending_key, self.saved)
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
        actual = io.get_range()
        if actual is None or abs(int(actual) - int(max_range)) > 1:
            raise Aborted('range', _("The wheel's range could not be set to {} degrees (it reads {}).")
                          .format(max_range, actual))
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
        asked = {}
        try:
            asked = io.release() or {}      # first: a request made from now on goes straight to the wheel
        except Exception as e:
            logging.debug("calibration release: %s", e)
        failed = restore_saved(io, self.saved, asked)
        try:
            io.close()
        except Exception as e:
            logging.debug("calibration close: %s", e)
        if self.pending_path and not failed:
            clear_pending(self.pending_path, self.pending_key)

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

    def _raw(self, w):
        return max(-32768, min(32767, int(round(self.sign * w * wc.FULL))))

    def _tick(self, w):
        """Write one force, wait for the next tick, read the wheel and check it. Returns the new reports."""
        if self.cancel_event.is_set():
            self._zero()
            raise Aborted('cancelled', _("Calibration cancelled."))
        now = self.io.now()
        if now - self.t0 > self.TOTAL_S:
            self._zero()
            raise Aborted('timeout', _("The calibration took too long."))
        if self._last_tick is not None and now - self._last_tick > self.TICK_GAP_S and \
                abs(self._last_w) > self.SWEEP_MAX:
            self._zero()
            raise Aborted('stall', _("The calibration was held up for {:.0f} ms during a pulse; it stopped to be "
                                     "safe.").format((now - self._last_tick) * 1000))
        w = self._limit(w, now)
        try:
            self.io.force(self._raw(w))
        except PermissionError:
            raise
        except OSError as e:
            raise Aborted('unplugged', _("The wheel stopped answering (unplugged?): {}").format(e)) from e
        self._last_tick = now
        self._last_w = w
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
            current = self.io.position_now()
        except ProxyLost as e:
            self._zero()
            raise Aborted('proxy', str(e)) from e
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
        # The reports can stop while the wheel turns (another program grabbed the input node): the axis's
        # current value, which a grab doesn't stop, must agree with them
        self.theta_now = self._deg(current)
        if abs(self.theta_now - self.theta) > self.BLIND_DEG:
            self._zero()
            raise Aborted('blind', _("The wheel's position stopped arriving (another program may have taken the "
                                     "wheel's input); the calibration stopped."))
        return events

    def _stopping_angle(self):
        """|angle| the wheel would stop at if braked now, moving towards the nearer lock (a_kick, the push's
        acceleration, is a floor for braking at SWEEP_MAX)."""
        outward = max(0.0, self.omega * _sign(self.theta))
        return abs(self.theta) + outward * outward / (2.0 * self.a_kick)

    def _check(self):
        if abs(self.omega) > self.V_ABORT:
            self._zero()
            raise Aborted('overspeed', _("The wheel turned faster than {:.0f} degrees a second.").format(self.V_ABORT))
        hard = self.recovering or not self.sign_known
        if abs(self.theta) > self.lock - self.HARD_MARGIN_DEG or \
                (hard and self._stopping_angle() > self.lock - self.HARD_MARGIN_DEG):
            self._emergency_brake()
            raise Aborted('near_lock', _("The wheel is too close to its lock. Turn it towards the centre "
                                         "and try again."))
        if not hard and self._stopping_angle() > self.lock - self.LOCK_MARGIN_DEG:
            raise _NearLock()

    def _emergency_brake(self):
        """At the hard margin: if the wheel is still heading for the lock (and which way the force turns it is
        known), brake for at most EMERGENCY_BRAKE_S at most SWEEP_MAX, then force 0."""
        try:
            if self.sign_known:
                end = self.io.now() + self.EMERGENCY_BRAKE_S
                while self.io.now() < end and self.omega * _sign(self.theta) > self.STILL_DEG_S:
                    w = -_sign(self.omega) * min(self.SWEEP_MAX, 0.8 * self.breakaway_max +
                                                 self.kv * abs(self.omega))
                    self.io.force(self._raw(w))
                    self.rec.command(self.io.now(), w)
                    self.io.sleep_until(self.io.now() + self.period)
                    self._read()
        except (OSError, Aborted):
            pass
        self._zero()

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


def restore_saved(io, saved, asked=None):
    """Put a wheel back after a run: ext_force 0 and game_ffb_level 100 first and last, the shaping and cap as
    they were, and the range, gain and centring spring as asked for meanwhile (`asked`) or as they were. Each
    write on its own, so one failing does not skip the rest. Returns True if any failed."""
    asked = asked or {}
    failed = []

    def attempt(fn, *args):
        try:
            fn(*args)
        except Exception as e:
            failed.append(e)
            logging.debug("calibration restore %s%s: %s", getattr(fn, '__name__', fn), args, e)

    attempt(io.force, 0)
    attempt(io.write, 'game_ffb_level', '100')
    for name in SHAPING + ('ext_force_cap',):
        if saved.get(name) is not None:
            attempt(io.write, name, saved[name])
    for key, setter in (('gain', io.set_gain), ('range', io.set_range), ('autocenter', io.set_autocenter)):
        value = asked.get(key, saved.get(key))
        if value is not None:
            attempt(setter, value)
    attempt(io.force, 0)
    attempt(io.write, 'game_ffb_level', '100')
    return bool(failed)


# --- the crash record: what to put back if the process died during a run (SIGKILL, a crash, power) ---

PENDING_NAME = 'wheel_calibration.pending.json'
BOOT_ID_FILE = '/proc/sys/kernel/random/boot_id'


def boot_id():
    try:
        with open(BOOT_ID_FILE) as f:
            return f.read().strip()
    except OSError:
        return None


def instance_key(device):
    """The crash record's key: the model and which one (the serial, else the USB port), so an identical
    wheel is never given another's record. Not split by the active device: the record is about the physical
    wheel's settings, which must be found whichever device is active when it is read."""
    which = getattr(device, 'serial', None)
    if not which:
        dev_path = getattr(device, 'dev_path', None) or ''
        # .../usb1/1-3/1-3:1.0/0003:046D:C24F.0005: the interface (port) stays, the HID number does not
        which = os.path.basename(os.path.dirname(dev_path)) if dev_path else ''
    return '{}@{}'.format(base_key(device), which)


def write_pending(path, key, saved):
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'key': key, 'boot_id': boot_id(), 'time': time.time(), 'saved': saved}, f)
        os.replace(tmp, path)
    except OSError as e:
        logging.warning("calibration crash record: %s", e)


def clear_pending(path, key):
    try:
        with open(path) as f:
            if json.load(f).get('key') != key:
                return
    except (OSError, ValueError, AttributeError):
        pass
    try:
        os.remove(path)
    except OSError:
        pass


def _read_pending(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get('saved'), dict):
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    return data


def take_pending(path, key):
    """The settings a run that never finished had saved for this wheel, or None. A record from another boot
    is dropped: the driver started every wheel from its defaults since, and the settings in it are old."""
    data = _read_pending(path)
    if data is None or data.get('key') != key:
        return None
    if data.get('boot_id') != boot_id():
        clear_pending(path, key)
        return None
    return data['saved']


def recover_pending(path, key, io, wheel=None):
    """At start or when the wheel comes back (before any profile is applied to it, so a later choice of the
    user's always wins): undo a run that never got to put the wheel back. The record is used once: if that
    attempt fails, only its safe part (ext_force 0, game_ffb_level 100) is tried again later. True if there
    was one and it is undone. Nothing is written while anyone holds the wheel (wheel_owner): the caller tries
    again once it is free."""
    if wheel is not None and wheel_owner.current(wheel) is not None:
        return False
    saved = take_pending(path, key)
    if saved is None:
        return False
    logging.warning("wheel calibration: an earlier run did not finish; putting the wheel back")
    if not restore_saved(io, saved):
        clear_pending(path, key)
        return True
    if saved:
        write_pending(path, key, {})            # the next attempt: force 0 and the game's force only
    return False


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
        self.wheel_id = device.dev_path         # the wheel_owner key
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

    def hold(self):
        self.device.hold_settings()

    def release(self):
        """Stop holding the settings; the range, gain and centring spring asked for while they were held."""
        names = {'range': 'range', 'ff_gain': 'gain', 'autocenter': 'autocenter'}
        return {names[k]: v for k, v in self.device.release_settings().items() if k in names}

    PROXY_CHECK_S = 0.25            # how often a proxied node is checked for a restarted or stopped proxy

    def _proxy_id_of(self, node):
        """The id of the proxy presenting `node` for this wheel, None if the status doesn't list one."""
        from .proxy.manager import ProxyManager
        status = ProxyManager.read_status() or {}
        return next((p.get('id') for p in status.get('proxies', [])
                     if p.get('devnode') == node and self.device.dev_name in p.get('sources', {}).values()), None)

    def open(self):
        from evdev import InputDevice, ecodes
        node = self.device._input_node()
        if node is None:
            raise PermissionError(errno.EACCES, "the wheel's input device can't be read")
        self._proxy_id = None
        if node != self.device.dev_name:
            # Behind a proxy: its virtual device is the active one and what is calibrated, as it reports (an
            # inverted axis is learnt as the force's direction). A stall or restart there would look like a
            # wheel standing still, so the proxy is watched (_check_proxy)
            self._proxy_id = self._proxy_id_of(node)
            if self._proxy_id is None:
                raise Aborted('proxy', _("The combined device holding the wheel can't be found."))
        self._force_fd = os.open(self.device.device_file('ext_force'), os.O_WRONLY)
        self._input = InputDevice(node)
        self._node = node
        self._ino = os.fstat(self._input.fd).st_ino
        self._abs_x = ecodes.ABS_X
        self._ev_abs = ecodes.EV_ABS
        if self._proxy_id is not None:
            info = self._input.absinfo(ecodes.ABS_X)
            self._abs_min, self._abs_max = info.min, info.max
            if self._abs_max <= self._abs_min:
                raise Aborted('proxy', _("The combined device has no usable steering axis."))
        self._next_check = 0.0
        self._lost = 0
        # Reports carry the wall clock; the loop runs on the monotonic one
        self._clock_offset = time.time() - time.monotonic()

    def _check_proxy(self):
        """Raise ProxyLost when the proxy presenting the wheel stopped or restarted (its virtual device is a
        new node, or gone, or the proxy no longer lists this wheel). Checked a few times a second."""
        if self._proxy_id is None:
            return
        now = time.monotonic()
        if now < self._next_check:
            return
        self._next_check = now + self.PROXY_CHECK_S
        lost = _("The combined device stopped or restarted during the calibration; it was stopped.")
        try:
            if os.stat(self._node).st_ino != self._ino:
                raise ProxyLost(lost)
        except OSError as e:
            raise ProxyLost(lost) from e
        from .proxy.manager import ProxyManager
        status = ProxyManager.read_status() or {}
        listed = any(p.get('id') == self._proxy_id and p.get('devnode') == self._node and
                     self.device.dev_name in p.get('sources', {}).values() for p in status.get('proxies', []))
        # one missed look is allowed: the status file is a heartbeat the daemon refreshes
        self._lost = 0 if listed else self._lost + 1
        if self._lost >= 2:
            raise ProxyLost(lost)

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

    def _from_proxy(self, value):
        """A virtual device's ABS_X as the 0..65535 the calibration works in, from its own range."""
        span = self._abs_max - self._abs_min
        return int(round(min(1.0, max(0.0, (value - self._abs_min) / span)) * 65535))

    def position_raw(self):
        self._check_proxy()
        if self._proxy_id is not None:
            return self._from_proxy(self._input.absinfo(self._abs_x).value)
        code, value = self.device._normalized_axis(self._abs_x, self._input.absinfo(self._abs_x).value)
        return value

    def position_now(self):
        """The axis's current value (EVIOCGABS: it keeps up even while another program grabs the node and
        the reports stop arriving here)."""
        return self.position_raw()

    def poll(self):
        self._check_proxy()
        out = []
        now = time.monotonic()
        while True:
            event = self._input.read_one()
            if event is None:
                return out
            if event.type != self._ev_abs:
                continue
            if self._proxy_id is not None:
                if event.code != self._abs_x:
                    continue
                value = self._from_proxy(event.value)
            else:
                event = self.device.normalize_event(event)
                if event.code != self._abs_x:
                    continue
                value = event.value
            t = event.timestamp() - self._clock_offset
            if not now - 1.0 < t <= now + 0.005:      # the wall clock jumped: take the read time
                t = now
            out.append((t, value))

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


def base_key(device):
    """The wheel's USB vendor:product (the model), so it survives another port."""
    return getattr(device, 'usb_id', None) or getattr(device, 'name', None) or str(device.get_id())


def wheel_key(device):
    """The store's key for the wheel as it is read now: the model, plus '+combined' while a proxy's virtual
    device is the active one (its axis may differ, so it has an entry of its own; none yet means no
    correction, not the direct entry's)."""
    try:
        proxied = device._input_node() not in (None, device.dev_name)
    except Exception:
        proxied = False
    return base_key(device) + ('+combined' if proxied else '')


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
    chose, the soft clip and friction compensation kept."""
    old = old if isinstance(old, dict) else {}
    entry = {
        'version': ENTRY_VERSION,
        'result': result,
        'use': True,
        'dither': old['dither'] if old.get('dither_chosen') else bool(result.get('dither')),
        'dither_chosen': bool(old.get('dither_chosen')),
        'soft_clip': _clamped_int(old.get('soft_clip'), 0, 100),
        'friction_comp': _clamped_int(old.get('friction_comp'), 0, 100),
    }
    return entry


ENTRY_VERSION = 1
FRICTION_COMP_CAP = 0.8         # friction compensation lifts the force at most to this share of the breakaway


def _clamped_int(value, low, high, default=0):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def valid_lut(lut):
    return wc.plausible_lut(lut)


def _share(value):
    """A finite float in (0, 1], or None (NaN, inf, junk, out of range)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value) if 0.0 < value <= 1.0 else None


def valid_entry(entry):
    """True for a stored entry whose correction may be written: this version, a plausible LUT (33 points,
    0 to 32767, strictly increasing, not steeper than 4x identity), a dead zone within 15 %, and the friction
    and breakaway the friction compensation is computed from finite shares of full scale (0 < v <= 1)."""
    if not isinstance(entry, dict) or entry.get('version') != ENTRY_VERSION:
        return False
    result = entry.get('result')
    if not isinstance(result, dict) or not wc.plausible_lut(result.get('lut')):
        return False
    if _share(result.get('friction')) is None or _share(result.get('breakaway')) is None:
        return False
    deadband = result.get('deadband')
    return isinstance(deadband, int) and not isinstance(deadband, bool) and \
        0 <= deadband <= wc.MAX_DEAD_ZONE * wc.FULL


DEADBAND_CEILING = min(0.2, wc.MAX_DEAD_ZONE + 0.05)    # ffb_deadband never above this share, whatever the inputs


def compensated_deadband(result, percent):
    """ffb_deadband with `percent` of the measured Coulomb friction folded in: the lift then starts at the
    command that gives that much torque, so the smallest force just about overcomes the friction. Capped at
    FRICTION_COMP_CAP of the breakaway force (past that the wheel would move by itself), and in any case at
    DEADBAND_CEILING of full scale."""
    ceiling = int(DEADBAND_CEILING * wc.FULL)
    deadband = result.get('deadband')
    if isinstance(deadband, bool) or not isinstance(deadband, int):
        return 0
    deadband = max(0, min(deadband, ceiling))
    share = _clamped_int(percent, 0, 100) / 100.0
    friction = _share(result.get('friction'))
    breakaway = _share(result.get('breakaway'))
    if not share or friction is None or breakaway is None:
        return deadband
    target = wc.shaped(friction * wc.FULL, result['lut'], deadband)
    if not math.isfinite(target):
        return deadband
    lifted = deadband + share * (target - deadband)
    cap = min(FRICTION_COMP_CAP * breakaway * wc.FULL, ceiling)
    return int(round(max(deadband, min(lifted, cap, ceiling))))


def correction_values(entry):
    """{attribute: text} to write for a store entry: the LUT and deadband (with any friction compensation)
    when the correction is on and the entry is valid, identity and 0 otherwise; the dither and soft clip as
    chosen."""
    entry = entry if isinstance(entry, dict) else {}
    use = bool(entry.get('use')) and valid_entry(entry)
    result = entry.get('result') if use else None
    return {
        'ffb_lut': ' '.join(str(v) for v in result['lut']) if use else 'identity',
        'ffb_deadband': str(compensated_deadband(result, entry.get('friction_comp'))) if use else '0',
        'ffb_dither': '1' if entry.get('dither') is True else '0',
        'ffb_soft_clip': str(_clamped_int(entry.get('soft_clip'), 0, 100)),
    }


def apply_correction(write, entry):
    """Write an entry's correction with write(name, text); False if anything failed (a broken entry writes
    identity and 0)."""
    try:
        values = correction_values(entry)
    except Exception as e:
        logging.warning("wheel correction: %s", e)
        values = correction_values({})
    ok = True
    for name, value in values.items():
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
