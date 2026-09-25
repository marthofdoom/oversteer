"""Game telemetry -> wheel rev LEDs.

A small UDP listener decodes the telemetry formats that cross the Proton
boundary (see :mod:`telemetry_formats`) and turns engine RPM into the
wheel's five rev LEDs, the launch limiter and the shift learner's input.

The listener runs in a daemon thread and writes the LED brightness files
through :class:`RevLeds`; when no packet arrives for a while the LEDs go
out so a stale value never stays lit.
"""

import glob
import logging
import os
import socket
import threading
import time

# Decoding lives in telemetry_formats; these stay importable from here
from .telemetry_formats import Sample, decode_sample, decode  # noqa: F401

# Forza Horizon 6 binds its own outgoing socket somewhere in 5200-5300 and
# its documentation says to keep Data Out away from that range; the game
# runs on this machine, so 5300 can collide.
DEFAULT_PORT = 5310
LEGACY_PORT = 5300                                   # the default before 0.14: games may still send there
PROBE_AFTER = 10.0                                   # seconds of nothing before looking at the other port
PROBE_EVERY = 60.0
PROBE_LISTEN = 1.0                                   # seconds the other port is held: FH6 may want 5300
DEFAULT_SHIFT = 0.97                                 # shift point as a fraction of max RPM
LED_SPACING = (0.72, 0.80, 0.89, 0.95, 1.0)          # per LED, as a fraction of the shift point
FLASH_MARGIN = 0.03                                  # above the shift point: flash (shift now)
LEARNED_DECAY = 0.01                                 # OutGauge: learnt ceiling sags this much per second
IDLE_TIMEOUT = 2.0                                   # seconds without telemetry -> LEDs off
FLASH_PERIOD = 0.08                                  # limiter flash half-period (seconds)

# Launch mode: a rally stage starts with clutch in, handbrake up and the
# throttle floored, which holds the engine on its limiter. That RPM is the
# car's real ceiling, whatever (if anything) the game reports as its max.
LAUNCH_CLUTCH = 0.9                                  # pressed at least this far
LAUNCH_THROTTLE = 0.85
LAUNCH_HANDBRAKE = 0.9
LAUNCH_HOLD = 1.0                                    # seconds held before the RPM counts
LAUNCH_SETTLE = 0.3                                  # the last this-many seconds must not climb...
LAUNCH_RISE = 0.02                                   # ...by more than this fraction: on the limiter
LAUNCH_MIN_RPM = 2000.0


class RevLeds:
    """The wheel's rev LEDs (Linux LED class), lit as a bar."""

    def __init__(self, sysfs_device_path):
        self.sysfs_path = sysfs_device_path
        pattern = os.path.join(sysfs_device_path, 'leds', '*RPM*', 'brightness')
        self.paths = sorted(glob.glob(pattern), key=lambda p: os.path.dirname(p))
        self._last = None
        self._lock = threading.Lock()
        self._held_until = 0.0         # show(): a pattern that telemetry must not overwrite yet
        self._wanted = None            # what telemetry asked for meanwhile
        self._timer = None
        self._generation = 0           # which show() a pending release belongs to

    def available(self):
        return bool(self.paths) and all(os.access(p, os.W_OK) for p in self.paths)

    def set_count(self, lit):
        """Light the first `lit` LEDs."""
        pattern = tuple(i < lit for i in range(len(self.paths)))
        self.set_pattern(pattern)

    def set_pattern(self, pattern):
        pattern = tuple(bool(x) for x in pattern)
        with self._lock:
            self._wanted = pattern
            if time.monotonic() < self._held_until:
                return
            self._write(pattern)

    def show(self, pattern, seconds=1.0):
        """Show `pattern` for a moment over whatever is lit (a hotkey's new
        level), then go back to what telemetry wants by now."""
        pattern = tuple(bool(x) for x in pattern)
        with self._lock:
            self._held_until = time.monotonic() + seconds
            self._write(pattern)
            if self._timer is not None:
                self._timer.cancel()
            # cancel() can't stop a release already waiting on the lock:
            # the generation makes that one a no-op
            self._generation += 1
            self._timer = threading.Timer(seconds, self._release, (self._generation,))
            self._timer.daemon = True
            self._timer.start()

    def show_level(self, fraction, seconds=1.0):
        """A bar for a 0..1 level; at least one LED so 'lowest' is visible."""
        n = len(self.paths)
        lit = max(1, int(round(max(0.0, min(1.0, fraction)) * n)))
        self.show(tuple(i < lit for i in range(n)), seconds)

    def show_state(self, on, seconds=1.0):
        """A switch turned on (all lit) or off (just the two ends)."""
        n = len(self.paths)
        self.show(tuple(on or i in (0, n - 1) for i in range(n)), seconds)

    def _release(self, generation):
        with self._lock:
            if generation != self._generation:
                return
            self._held_until = 0.0
            self._timer = None
            self._write(self._wanted if self._wanted is not None else (False,) * len(self.paths))

    def _write(self, pattern):
        if pattern == self._last:
            return
        for path, on in zip(self.paths, pattern):
            try:
                with open(path, 'w') as f:
                    f.write('1' if on else '0')
            except OSError as e:
                logging.debug("led %s: %s", path, e)
        self._last = pattern

    def off(self):
        self.set_pattern((False,) * len(self.paths))

    def test(self, step=0.15):
        """Light the LEDs in sequence, then all, then off (blocking)."""
        n = len(self.paths)
        for i in range(n + 1):
            self.set_count(i)
            time.sleep(step)
        for _ in range(3):
            self.set_pattern((True,) * n)
            time.sleep(step)
            self.off()
            time.sleep(step)


class Telemetry:
    """UDP listener thread driving a RevLeds."""

    def __init__(self, leds, port=DEFAULT_PORT, shift=DEFAULT_SHIFT, shift_rpm=None, on_status=None,
                 launch=False, inputs=None, on_limiter=None, learner=None, use_learnt=False):
        """`shift` is the shift point as a fraction of the game's max RPM;
        `shift_rpm`, when given, is an absolute shift point instead. With
        `launch`, `shift` is a fraction of the limiter learnt at the last
        launch (see LAUNCH_*): `inputs()` gives the pedals as pressed
        fractions, {'clutch', 'throttle', 'handbrake'} (None = unknown),
        and `on_limiter(rpm)` hears each limiter learnt. `learner` (a
        ShiftLearner) is fed every packet; with `use_learnt` its shift
        point for the current gear replaces the percentage once known."""
        self.leds = leds
        self.port = int(port)
        self.set_shift(shift, shift_rpm, launch)
        self.inputs = inputs
        self.on_limiter = on_limiter
        self.learner = learner
        self.use_learnt = use_learnt
        self.live = None                  # the last Sample, for the GUI
        self.using_learnt = None          # the learnt shift point in use, or None
        self.launch_max = 0.0             # limiter from the last launch; 0 = none yet
        self._launch_samples = []         # (time, rpm) while a launch is held
        self.last_max_rpm = 0.0
        self._learned_at = 0.0
        self.on_status = on_status
        self.running = False
        self.last_packet = 0.0
        self.last_source = None
        self.learned_max = 0.0
        self._thread = None
        self._sock = None
        self._unknown_sizes = set()
        self.heard = False                # anything decoded on our port since start()
        self.elsewhere = None             # probe_other_port(): True something sends to other_port, False nothing
        self._probe_at = 0.0
        self._source = None               # (address, game) the listener is locked to
        self._ignored = set()             # other sources, logged once each
        self._lit = None                  # LEDs lit as a bar; None = unknown, rewrite on next packet
        self._flash = False
        self._flash_at = 0.0

    def set_shift(self, shift=DEFAULT_SHIFT, shift_rpm=None, launch=False):
        """Change the shift point while running (plain attribute writes:
        the listener thread reads them once per packet)."""
        self.shift = max(0.5, min(1.0, float(shift)))
        self.shift_rpm = float(shift_rpm) if shift_rpm else None
        self.launch = bool(launch) and not self.shift_rpm

    def reference_max(self):
        """The RPM the shift fraction applies to right now (0 = unknown)."""
        if self.launch and self.launch_max:
            return self.launch_max
        return self.last_max_rpm or self.learned_max

    def _launch_held(self):
        if self.inputs is None:
            return False
        try:
            state = self.inputs()
        except Exception:
            return False
        clutch, throttle, handbrake = state.get('clutch'), state.get('throttle'), state.get('handbrake')
        if clutch is None or throttle is None:
            return False
        # No handbrake fitted: clutch in and throttle floored is the launch
        return (clutch >= LAUNCH_CLUTCH and throttle >= LAUNCH_THROTTLE
                and (handbrake is None or handbrake >= LAUNCH_HANDBRAKE))

    def _learn_launch(self, now, rpm):
        """Watch a launch hold; once the RPM has stopped climbing, its
        peak is the limiter. A clutch kick that never reaches the limiter
        is still climbing when released, and teaches nothing."""
        if not self._launch_held():
            self._launch_samples = []
            return
        samples = self._launch_samples
        samples.append((now, rpm))
        if now - samples[0][0] < LAUNCH_HOLD:
            return
        recent = [r for t, r in samples if now - t <= LAUNCH_SETTLE]
        earlier = [r for t, r in samples if now - t > LAUNCH_SETTLE]
        peak_earlier = max(earlier) if earlier else 0.0
        if peak_earlier < LAUNCH_MIN_RPM or max(recent) > peak_earlier * (1.0 + LAUNCH_RISE):
            return
        limiter = max(r for t, r in samples)
        # Keep only the settle window: a long hold stays cheap
        self._launch_samples = [(t, r) for t, r in samples if now - t <= LAUNCH_HOLD]
        if abs(limiter - self.launch_max) > 1.0:
            self.launch_max = limiter
            if self.on_limiter is not None:
                try:
                    self.on_limiter(limiter)
                except Exception:
                    pass

    def start(self):
        if self._thread is not None:
            return True
        try:
            # No SO_REUSEADDR: UDP has no TIME_WAIT, and with it a second
            # listener would silently share the port instead of failing.
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.bind(('0.0.0.0', self.port))
            self._sock.settimeout(0.25)
        except OSError as e:
            logging.warning("telemetry: cannot listen on UDP %d: %s", self.port, e)
            self._sock = None
            return False
        self.running = True
        self.heard = False
        self.elsewhere = None
        self._probe_at = time.monotonic() + PROBE_AFTER
        self._thread = threading.Thread(target=self._run, name='telemetry', daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self.running = False
        if self._thread is not None:
            self._thread.join(1.0)
            self._thread = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self.leds.off()

    def _run(self):
        while self.running:
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                now = time.monotonic()
                self.check_idle(now)
                if self.learner is not None:
                    self.learner.tick(now)
                if not self.heard and now >= self._probe_at:
                    self._probe_at = now + PROBE_EVERY
                    self.probe_other_port()
                continue
            except OSError:
                break
            self.handle(time.monotonic(), data, addr)

    @property
    def other_port(self):
        """The default we are not on: games and oversteer-run set up before
        the move to 5310 send to 5300, and a profile saved then listens
        there while oversteer-run now sends to 5310."""
        return DEFAULT_PORT if self.port == LEGACY_PORT else LEGACY_PORT

    def probe_other_port(self):
        """Nothing has arrived on our port: listen on other_port for a
        moment, if it is free, to tell a game sending to the other default
        from no game at all. It is let go again at once: 5300 is in the
        range Forza Horizon 6 may need for its own socket. Sets
        `elsewhere`: True (something sends there), False (nothing)."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError:
            return
        try:
            sock.bind(('0.0.0.0', self.other_port))
            sock.settimeout(PROBE_LISTEN)
            data, addr = sock.recvfrom(2048)
            found = decode_sample(data) is not None
            if found:
                logging.info("telemetry: %s sends to UDP %d, Oversteer listens on %d", addr[0], self.other_port,
                             self.port)
        except socket.timeout:
            found = False
        except OSError:
            return                       # in use: someone else's port, nothing to learn from it
        finally:
            sock.close()
        if found != self.elsewhere:
            self.elsewhere = found
            self._status(None)

    def check_idle(self, now):
        """Telemetry stopped for a while: LEDs off and forget the source."""
        if not self.last_packet or now - self.last_packet <= IDLE_TIMEOUT:
            return
        self.leds.off()
        self._lit = None
        self.last_packet = 0.0
        self.last_source = None
        self._source = None
        self.learned_max = 0.0
        self._learned_at = 0.0
        # Menus or a loading screen: the next stage may be another car,
        # and it starts with a launch anyway
        self.launch_max = 0.0
        self._launch_samples = []
        self.live = None
        if self.learner is not None:
            self.learner.idle()
        self._status(None)

    def handle(self, now, data, addr):
        """One datagram from `addr` received at `now` (monotonic seconds):
        the live path, also driven directly by tests and replays."""
        # A source that went quiet frees the lock even while another one
        # keeps sending (the socket then never times out)
        self.check_idle(now)
        sample = decode_sample(data)
        if sample is None:
            if len(data) not in self._unknown_sizes:
                self._unknown_sizes.add(len(data))
                logging.info("telemetry: unknown %d-byte packet from %s", len(data), addr[0])
            return
        # One source at a time: a stale bridge next to a game, or a replay
        # sent while a game runs, would otherwise alternate cars packet by
        # packet and end and start a learning session on each
        source = (addr[0], sample.game)
        if self._source is None:
            self._source = source
        elif source != self._source:
            if source not in self._ignored:
                self._ignored.add(source)
                logging.info("telemetry: ignoring %s from %s while %s from %s is arriving",
                             source[1] or 'telemetry', source[0], self._source[1] or 'telemetry', self._source[0])
            return
        rpm, max_rpm, shift = sample.rpm, sample.max_rpm, sample.shift
        self.live = sample
        self.last_packet = now
        if not self.heard:
            self.heard = True
            self.elsewhere = None
        if self.last_source != addr[0]:
            self.last_source = addr[0]
            self._status(addr[0])
        shift_rpm, shift_fraction = self.shift_rpm, self.shift
        if self.launch:
            self._learn_launch(now, rpm)
            if self.launch_max and rpm > self.launch_max:
                # Past the launch figure on the move (a launch control
                # that caps the revs at the line): it was not the top
                report = rpm > self.launch_max * 1.01
                self.launch_max = rpm
                if report and self.on_limiter is not None:
                    try:
                        self.on_limiter(rpm)
                    except Exception:
                        pass
        if max_rpm is None:
            # OutGauge: learn the ceiling from the highest RPM seen. It
            # sags slowly (per second, not per packet) so a change of
            # car with a lower redline still fills the bar, but never
            # below what keeps the current RPM at "all on": a steady
            # cruise must not turn into a limiter flash (OutGauge has
            # its own shift-light flag for that).
            if self._learned_at:
                self.learned_max *= max(0.0, 1.0 - LEARNED_DECAY * (now - self._learned_at))
            self._learned_at = now
            self.learned_max = max(self.learned_max, rpm, rpm / shift_fraction if not shift_rpm else 0.0)
            max_rpm = self.learned_max
        else:
            self.last_max_rpm = max_rpm
        # Everything is relative to the shift point: the bar completes
        # there and flashes above it.
        if self.launch and self.launch_max:
            max_rpm = self.launch_max
        learnt = self._feed_learner(now, sample, max_rpm)
        if learnt:
            reference = learnt
        else:
            reference = shift_rpm if shift_rpm else shift_fraction * max_rpm
        fraction = rpm / reference if reference > 0 else 0.0
        if shift or fraction >= 1.0 + FLASH_MARGIN:
            if now - self._flash_at >= FLASH_PERIOD:
                self._flash = not self._flash
                self._flash_at = now
                self.leds.set_pattern((self._flash,) * len(self.leds.paths))
            self._lit = None
            return
        lit = sum(1 for t in LED_SPACING if fraction >= t)
        if lit != self._lit:
            self.leds.set_count(lit)
            self._lit = lit

    def _feed_learner(self, now, sample, max_rpm):
        """Teach the learner; the learnt shift point for this gear when
        the rev lights should use it."""
        learner = self.learner
        if learner is None:
            return None
        pedals = {}
        if self.inputs is not None:
            try:
                pedals = self.inputs() or {}
            except Exception:
                pedals = {}
        # The game's view of the pedals when it sends one (it includes an
        # automatic clutch and traction control); ours otherwise
        throttle = sample.throttle if sample.throttle is not None else pedals.get('throttle')
        clutch = sample.clutch if sample.clutch is not None else pedals.get('clutch')
        limiter = self.launch_max if self.launch_max else max_rpm
        try:
            learner.feed(now, sample, limiter, throttle, clutch, pedals.get('shift_press'))
            learnt = learner.shift_rpm(sample.gear) if self.use_learnt else None
        except Exception:
            logging.exception("shift learner")
            learnt = None
        self.using_learnt = learnt
        return learnt

    def _status(self, source):
        if self.on_status is not None:
            try:
                self.on_status(source)
            except Exception:
                pass
