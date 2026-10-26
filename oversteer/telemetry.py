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
import selectors
import socket
import threading
import time

from . import car_data

# Decoding lives in telemetry_formats; these stay importable from here
from .telemetry_formats import Sample, decode_sample, decode  # noqa: F401

# Forza Horizon 6 binds its own outgoing socket somewhere in 5200-5300 and
# its documentation says to keep Data Out away from that range; the game
# runs on this machine, so 5300 can collide.
DEFAULT_PORT = 5310


def N_(text):
    """Marks a string for translation; it is translated where shown."""
    return text


# Ports offered in Settings -> Telemetry: (port, label), one row per port.
# DEFAULT_PORT stays first. Only games this module decodes are listed.
#  - 5310: Oversteer's own default; oversteer-run / the shm bridge (Assetto
#    Corsa, Competizione, Rally: OVST3_GAMES) send there, and
#    docs/forza-horizon-6.md tells Forza users to point Data Out at it (Forza
#    has no fixed port and 5200-5300 belong to the game itself).
#  - 20777: the stock port in DiRT Rally 2.0's hardware_settings_config.xml
#    (docs/dirt-rally-2-parity.md) and in EA SPORTS WRC's config.json. DiRT
#    Rally and DiRT 4 share the Codemasters layout (decoded as 'dirt').
#  - 4444: BeamNG.drive's default outgaugePort (OutGauge is decoded). Live for
#    Speed has no default port (set OutGauge Port in its cfg.txt) and WRC
#    Generations' stock port is not documented, so neither gets a row.
PORT_PRESETS = [
    (DEFAULT_PORT, N_("Oversteer default (Assetto Corsa, Competizione, Rally bridge; Forza)")),
    (20777, N_("DiRT Rally 2.0, DiRT Rally, DiRT 4, EA SPORTS WRC")),
    (4444, N_("BeamNG.drive OutGauge")),
]
PORT_MIN = 1024
PORT_MAX = 65535


def parse_port(text):
    """The port typed in the port box as an int from PORT_MIN to PORT_MAX,
    or None. A preset row ("5310: Oversteer default ...") is read by its
    number."""
    if text is None:
        return None
    head = str(text).split(':', 1)[0].strip()
    if not (head.isascii() and head.isdigit()):
        return None
    port = int(head)
    return port if PORT_MIN <= port <= PORT_MAX else None


LEGACY_PORT = 5300                                   # the default before 0.14: games may still send there
AUTO = 'auto'                                        # the port setting that listens on every port of auto_ports()
# Forza Horizon 6 binds its own socket somewhere in 5200-5300 (inclusive):
# never held for good. LEGACY_PORT is in it, so Auto only probes it.
FORZA_OWN = range(5200, 5301)
LOCK_SILENCE = 3.0                                   # s the locked port must be quiet before another port takes over
BUSY_RETRY = 30.0                                    # s between tries of the Auto ports another app held


def auto_ports():
    """The UDP ports Auto holds: every preset outside Forza Horizon 6's
    own range. LEGACY_PORT (5300, where WRC Generations and bridges set up
    before 0.14 still send) is in that range: Auto probes it now and then
    instead (Telemetry._auto_probe)."""
    ports = []
    for port in [p for p, _label in PORT_PRESETS]:
        if port not in ports and port not in FORZA_OWN:
            ports.append(port)
    return ports


PROBE_AFTER = 10.0                                   # seconds of nothing before looking at the other port
PROBE_EVERY = 15.0                                   # a bridge sends only while on a stage: look often...
PROBE_OFTEN_FOR = 300.0                              # ...for the first minutes after start
PROBE_LATER = 60.0                                   # then less: 5300 is in Forza Horizon 6's range
PROBE_LISTEN = 1.0                                   # seconds the other port is held: FH6 may want 5300
DEFAULT_SHIFT = 0.97                                 # shift point as a fraction of max RPM
LED_SPACING = (0.72, 0.80, 0.89, 0.95, 1.0)          # per LED, as a fraction of the shift point
FLASH_MARGIN = 0.03                                  # above the shift point: flash (shift now)
LEARNT_FLASH = 0.995                                 # a learnt shift point at the limiter flashes by this share of it
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
LAUNCH_SPEED = 2.0                                   # m/s: a launch is made standing; a held clutch at speed is not one
LAUNCH_RAISE_HOLD = 0.5                              # s held flat out past the launch figure before it is raised
CLUTCH_OUT = 0.1
LEARNER_ERROR_EVERY = 60.0                           # s between log lines of a learner that keeps failing


def _held_peak(samples, now, hold):
    """The peak of (time, rpm) `samples` spanning at least `hold` seconds
    whose last LAUNCH_SETTLE did not climb past the earlier peak by more
    than LAUNCH_RISE: the engine held on its limiter. None otherwise."""
    if not samples or now - samples[0][0] < hold:
        return None
    recent = [r for t, r in samples if now - t <= LAUNCH_SETTLE]
    earlier = [r for t, r in samples if now - t > LAUNCH_SETTLE]
    peak_earlier = max(earlier) if earlier else 0.0
    if not peak_earlier or max(recent) > peak_earlier * (1.0 + LAUNCH_RISE):
        return None
    return max(r for t, r in samples)


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


class NoLeds:
    """Rev lights that are not there: the listener runs to learn from the
    game's telemetry with the rev lights off or on a wheel without them
    ("Learn from game telemetry")."""

    paths = ()

    def available(self):
        return False

    def set_count(self, lit):
        pass

    def set_pattern(self, pattern):
        pass

    def off(self):
        pass


class Telemetry:
    """UDP listener thread driving a RevLeds.

    One thread waits on every socket with a selector. With a port it
    listens there alone; with `port=AUTO` it listens on every port of
    auto_ports() that is free (one that is not is skipped, reported in
    `busy` and tried again every BUSY_RETRY), and the first port to bring
    a packet of a known game becomes the active one: only its packets are
    decoded and passed on, the others are counted and dropped, and another
    port can take over only once the active one has been quiet for
    LOCK_SILENCE. LEGACY_PORT, in Forza Horizon 6's range, is only probed
    while nothing is locked, and held only while a game heard there holds
    the lock."""

    def __init__(self, leds, port=DEFAULT_PORT, shift=DEFAULT_SHIFT, shift_rpm=None, on_status=None,
                 launch=False, inputs=None, on_limiter=None, learner=None, use_learnt=False):
        """`port` is a UDP port, or AUTO.
        `shift` is the shift point as a fraction of the game's max RPM;
        `shift_rpm`, when given, is an absolute shift point instead. With
        `launch`, `shift` is a fraction of the limiter learnt at the last
        launch (see LAUNCH_*): `inputs()` gives the pedals as pressed
        fractions, {'clutch', 'throttle', 'handbrake'} (None = unknown),
        and `on_limiter(rpm)` hears each limiter learnt. `learner` (a
        ShiftLearner) is fed every packet; with `use_learnt` its shift
        point for the current gear replaces the percentage once known."""
        self.leds = leds
        self.auto = port == AUTO
        self.port = None if self.auto else int(port)      # fixed mode: the one port (tests set it before start())
        self.listening = []               # the ports held, in order (Auto: LEGACY_PORT only while it is locked)
        self.busy = {}                    # port -> OSError: the Auto ports that could not be bound (yet)
        self.legacy_heard = False         # Auto: a game was heard on LEGACY_PORT (its status asks to move it)
        self.active_port = None           # the port of the source locked on, None while none is
        self.dropped = {}                 # port -> packets dropped there while another port was active
        self._socks = {}                  # port -> socket, while running
        self._selector = None
        self._released = None             # (port, its last packet's time) of the lock idle let go last
        self._last_source = None          # the source (port, address, game) the previous lock was on
        self._busy_at = 0.0               # Auto: when to try the busy ports again
        self._probe = None                # Auto: (socket, until) while LEGACY_PORT is being probed
        self._ports_lock = threading.Lock()   # stop()'s swap against the thread adding or closing a port
        self._ignored_ports = set()       # ports whose dropped packets were logged since the lock was taken
        self.set_shift(shift, shift_rpm, launch)
        self.inputs = inputs
        self.on_limiter = on_limiter
        self.learner = learner
        self.use_learnt = use_learnt
        self.recorder = None              # a telemetry_capture.Recorder while "Record raw telemetry" is on
        self.live = None                  # the last Sample, for the GUI
        self.using_learnt = None          # the learnt shift point in use, or None
        self.launch_max = 0.0             # limiter from the last launch; 0 = none yet
        self._launch_samples = []         # (time, rpm) while a launch is held
        self._raise_samples = []          # (time, rpm) while held flat out past the launch figure
        self._carless = False             # the last packet had no car (Forza's menus)
        self._learner_error_at = None
        self.last_max_rpm = 0.0
        self._learned_at = 0.0
        self.on_status = on_status
        self.running = False
        self.last_packet = 0.0
        self.last_source = None
        self.learned_max = 0.0
        self.seen_max = 0.0               # the highest rpm seen in this car (what 'seen' tells the learner)
        self._seen_car = None
        self._thread = None
        self._unknown_sizes = set()
        self.heard = False                # anything decoded on our port since start()
        self.elsewhere = None             # fixed mode, probe_other_port(): True something sends to other_port
        self._probe_at = 0.0
        self._source = None               # (port, address, game) the listener is locked to
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

    def _pedals(self):
        """Our pedals as pressed fractions, {} when unknown."""
        if self.inputs is None:
            return {}
        try:
            return self.inputs() or {}
        except Exception:
            return {}

    @staticmethod
    def _launch_held(pedals):
        clutch, throttle, handbrake = pedals.get('clutch'), pedals.get('throttle'), pedals.get('handbrake')
        if clutch is None or throttle is None:
            return False
        # No handbrake fitted: clutch in and throttle floored is the launch
        return (clutch >= LAUNCH_CLUTCH and throttle >= LAUNCH_THROTTLE
                and (handbrake is None or handbrake >= LAUNCH_HANDBRAKE))

    def _learn_launch(self, now, rpm, pedals=None, speed=None):
        """Watch a launch hold; once the RPM has stopped climbing, its
        peak is the limiter. A clutch kick that never reaches the limiter
        is still climbing when released, and teaches nothing. Each launch
        replaces the figure, lower or higher: it measures this car now."""
        if pedals is None:
            pedals = self._pedals()
        if not self._launch_held(pedals) or (speed is not None and speed > LAUNCH_SPEED):
            self._launch_samples = []
            return
        samples = self._launch_samples
        samples.append((now, rpm))
        limiter = _held_peak(samples, now, LAUNCH_HOLD)
        if limiter is None or limiter < LAUNCH_MIN_RPM:
            return
        # Keep only the settle window: a long hold stays cheap
        self._launch_samples = [(t, r) for t, r in samples if now - t <= LAUNCH_HOLD]
        if abs(limiter - self.launch_max) > 1.0:
            self._set_launch_max(limiter)

    def _raise_launch(self, now, sample, throttle, clutch):
        """Past the launch figure on the move (a launch control that caps
        the revs at the line): it was not the top. Raised only once the
        engine has been held there flat out, clutch out, in gear, for
        LAUNCH_RAISE_HOLD: a missed change down, a money shift or one bad
        packet goes past it for a moment and must not move it for the
        rest of the stage."""
        held = (self.launch_max and sample.rpm > self.launch_max and throttle is not None
                and throttle >= LAUNCH_THROTTLE and (clutch is None or clutch <= CLUTCH_OUT)
                and sample.gear is not None and sample.gear >= 1)
        if not held:
            self._raise_samples = []
            return
        samples = self._raise_samples
        samples.append((now, sample.rpm))
        peak = _held_peak(samples, now, LAUNCH_RAISE_HOLD)
        if peak is None:
            return
        self._raise_samples = [(t, r) for t, r in samples if now - t <= LAUNCH_RAISE_HOLD]
        if peak > self.launch_max * 1.001:
            self._set_launch_max(peak)

    def _set_launch_max(self, rpm):
        self.launch_max = rpm
        if self.on_limiter is not None:
            try:
                self.on_limiter(rpm)
            except Exception:
                pass

    @staticmethod
    def _bind(port):
        """A non-blocking UDP socket bound to `port` on every address.
        No SO_REUSEADDR: UDP has no TIME_WAIT, and with it a second
        listener would silently share the port instead of failing."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(('0.0.0.0', port))
            sock.setblocking(False)
        except BaseException:
            sock.close()
            raise
        return sock

    def wanted_ports(self):
        """The ports start() binds: Auto's, or the one port."""
        return auto_ports() if self.auto else [self.port]

    def start(self):
        """Bind and start listening. False when no port could be bound
        (fixed mode: the port; Auto: all of them)."""
        if self._thread is not None:
            return True
        socks, busy = {}, {}
        for port in self.wanted_ports():
            try:
                socks[port] = self._bind(port)
            except (OSError, OverflowError, TypeError) as e:
                if self.auto:
                    logging.info("telemetry: UDP %d skipped: %s", port, e)
                else:
                    logging.warning("telemetry: cannot listen on UDP %s: %s", port, e)
                busy[port] = e
        self.busy = busy
        if not socks:
            self.listening = []
            return False
        try:
            selector = selectors.DefaultSelector()
            for port, sock in socks.items():
                selector.register(sock, selectors.EVENT_READ, port)
        except (OSError, ValueError) as e:
            logging.warning("telemetry: cannot wait on the sockets: %s", e)
            for sock in socks.values():
                sock.close()
            self.listening = []
            return False
        self._socks, self._selector = socks, selector
        self.listening = list(socks)
        if self._source is not None and self._source[0] not in socks:
            # Started again on other ports: a lock on a port no longer heard would hold for good
            self._source = self.active_port = None
        if self.auto:
            logging.info("telemetry: Auto listens on UDP %s", ', '.join(str(p) for p in self.listening))
        self.running = True
        self.heard = False
        self.elsewhere = None
        self.legacy_heard = False
        self._started_at = time.monotonic()
        self._probe_at = self._started_at + PROBE_AFTER
        self._busy_at = self._started_at + BUSY_RETRY
        self._thread = threading.Thread(target=self._run, name='telemetry', daemon=True)
        self._thread.start()
        return True

    def stop(self):
        """Stop the thread and close every socket: they are free again when
        this returns, so a listener started next can bind them."""
        self.running = False
        if self._thread is not None:
            # A select() lasts 0.25 s; a probe (fixed mode) holds its port up to PROBE_LISTEN
            self._thread.join(PROBE_LISTEN + 1.0)
            self._thread = None
        with self._ports_lock:
            # A thread that outlived the join can no longer add a port (it checks the selector under the lock)
            selector, self._selector = self._selector, None
            socks, self._socks = self._socks, {}
            probe, self._probe = self._probe, None
        if selector is not None:
            selector.close()
        for sock in socks.values():
            sock.close()
        if probe is not None:
            probe[0].close()
        self.listening = []
        self.leds.off()

    def _run(self):
        selector = self._selector
        while self.running:
            try:
                events = selector.select(0.25)
            except (OSError, ValueError):
                break                     # closed under us by stop()
            for key, _mask in events:
                sock, port = key.fileobj, key.data
                # A burst costs one select: read until the socket is empty
                while self.running:
                    if sock.fileno() < 0:
                        break             # closed by this loop (LEGACY_PORT let go)
                    try:
                        data, addr = sock.recvfrom(2048)
                    except (BlockingIOError, InterruptedError):
                        break
                    except OSError as e:
                        if self.running:
                            # Never seen on a bound UDP socket: drop that port rather than spin on it
                            logging.warning("telemetry: UDP %s stops: %s", port, e)
                            self._close_port(port, selector)
                        break
                    self.handle(time.monotonic(), data, addr, port)
            if not self.running:
                break
            now = time.monotonic()
            if not events:
                self.check_idle(now)
                if self.learner is not None:
                    self.learner.tick(now)
                # Fixed mode, and not once the answer is known (a game heard there)
                if not self.auto and not self.heard and not self.elsewhere and now >= self._probe_at:
                    self._probe_at = now + self._probe_gap(now)
                    self.probe_other_port()
            if self.auto:
                self._auto_tick(now, selector)

    def _probe_gap(self, now):
        # Auto, once a game was heard on LEGACY_PORT: keep looking often, it will be back after its menus
        often = now - self._started_at < PROBE_OFTEN_FOR or (self.auto and self.legacy_heard)
        return PROBE_EVERY if often else PROBE_LATER

    def _close_port(self, port, selector):
        """Stop listening on `port` (the listener thread waiting on `selector`)."""
        with self._ports_lock:
            if selector is not self._selector:
                return                    # stopped meanwhile: stop() closed it, or it is another listener's
            sock = self._socks.pop(port, None)
            if sock is None:
                return
            try:
                selector.unregister(sock)
            except (KeyError, ValueError, OSError):
                pass
            sock.close()
            if self._probe is not None and self._probe[0] is sock:
                self._probe = None
            if port in self.listening:
                self.listening = [p for p in self.listening if p != port]

    def _add_port(self, port, sock, selector, listed=True):
        """Listen on `port` too, with `sock` already bound (the listener
        thread waiting on `selector`); `listed`: shown in `listening` (a
        probe is not). False, `sock` closed, once stop() has run."""
        with self._ports_lock:
            if selector is not self._selector or not self.running:
                sock.close()
                return False
            try:
                selector.register(sock, selectors.EVENT_READ, port)
            except (KeyError, ValueError, OSError):
                sock.close()
                return False
            self._socks[port] = sock
            if listed:
                self._list_port(port)
        return True

    def _list_port(self, port):
        if port in self.listening:
            return
        order = auto_ports() + [LEGACY_PORT]
        self.listening = sorted(self.listening + [port], key=lambda p: order.index(p) if p in order else len(order))

    def _auto_tick(self, now, selector):
        """Auto, after each wait (cheap unless something is due): try the
        busy ports again, and probe LEGACY_PORT while nothing is locked."""
        if self.busy and now >= self._busy_at:
            self._busy_at = now + BUSY_RETRY
            busy = dict(self.busy)
            for port in list(busy):
                try:
                    sock = self._bind(port)
                except (OSError, OverflowError) as e:
                    busy[port] = e
                    continue
                if self._add_port(port, sock, selector):
                    del busy[port]
                    logging.info("telemetry: UDP %d is free now: listening there too", port)
            if busy != self.busy:
                self.busy = busy
                self._status(self.last_source)
        probe = self._probe
        if probe is not None:
            if now >= probe[1]:
                # Nothing locked there in time: let it go again, Forza Horizon 6 may want it
                self._close_port(LEGACY_PORT, selector)
            return
        if LEGACY_PORT in self._socks:
            # Held while the game heard there holds the lock, and through
            # LOCK_SILENCE after it is let go, in case it only paused
            locked, released = self._source, self._released
            if not (locked is not None and locked[0] == LEGACY_PORT) and not (
                    locked is None and released is not None and released[0] == LEGACY_PORT
                    and now - released[1] < LOCK_SILENCE):
                logging.info("telemetry: UDP %d let go again (Forza Horizon 6's range)", LEGACY_PORT)
                self._close_port(LEGACY_PORT, selector)
                self._status(self.last_source)
            return
        # As in fixed mode: not once a game was heard on Auto's own ports
        # (Forza Horizon 6 starting must not land in a probe second), unless
        # it was heard on LEGACY_PORT itself
        if self.heard and not self.legacy_heard:
            return
        if self._source is None and now >= self._probe_at and LEGACY_PORT not in self._socks:
            self._probe_at = now + self._probe_gap(now)
            try:
                sock = self._bind(LEGACY_PORT)
            except (OSError, OverflowError):
                return                    # in use: someone else's port, nothing to learn from it
            if self._add_port(LEGACY_PORT, sock, selector, listed=False):
                self._probe = (sock, now + PROBE_LISTEN)

    @property
    def locked(self):
        """(port, address, game) of the source passed on, or None."""
        return self._source

    @property
    def udp_port(self):
        """The port telemetry is taken from: the active one, else the fixed
        port; None in Auto while nothing is locked."""
        active = self.active_port
        return active if active is not None else self.port

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
        logging.info("telemetry: stopped (nothing from %s for %.0f s)", self._source[2] if self._source else '?',
                     IDLE_TIMEOUT)
        # Another port may take over only once this one has been quiet for LOCK_SILENCE
        self._released = (self._source[0], self.last_packet) if self._source else None
        self.last_packet = 0.0
        self.last_source = None
        self._source = None
        self.active_port = None
        self._ignored_ports = set()
        self.learned_max = self.seen_max = 0.0
        self._learned_at = 0.0
        # Menus or a loading screen: the next stage may be another car,
        # and it starts with a launch anyway
        self.launch_max = 0.0
        self._launch_samples = []
        self._raise_samples = []
        self.live = None
        if self.learner is not None:
            self.learner.idle()
        self._status(None)

    def _lock(self, source, size, now):
        """Take `source` (port, address, game): its packets are the ones
        passed on until it goes quiet."""
        port = source[0]
        self._source = source
        self.active_port = port
        self._released = None
        self._ignored_ports = set()
        last = self._last_source
        if last is not None and port != last[0] and source[1:] != last[1:]:
            # Another port and another stream (another game, or another
            # machine): the drive log must see a new session there, not one
            # run jumping from one car to another. One game sending to two
            # ports is one stream, whichever port it comes back on.
            new_source = getattr(self.learner, 'new_source', None)
            if new_source is not None:
                try:
                    new_source()
                except Exception:
                    logging.exception("telemetry: new source")
        self._last_source = source
        if self.auto and port == LEGACY_PORT:
            # Heard on the probe: hold the port while this lock lasts
            self.legacy_heard = True
            self._probe = None
            self._list_port(port)
        recorder = self.recorder
        if self.auto and recorder is not None:
            # The capture's main file (with the wheel's sidecar) is this port's from here on
            recorder.main(port, now)
        logging.info("telemetry: game %s detected (%d-byte packets from %s, UDP %s)", source[2] or 'unknown',
                     size, source[1], port)

    def _drop(self, port, data):
        """A packet on a port other than the active one: counted, logged
        once per port while the lock lasts, otherwise ignored."""
        self.dropped[port] = self.dropped.get(port, 0) + 1
        if port not in self._ignored_ports:
            self._ignored_ports.add(port)
            if self._source is not None:
                logging.info("telemetry: ignoring UDP %s (%d-byte packets) while %s arrives on UDP %s", port,
                             len(data), self._source[2] or 'telemetry', self._source[0])
            else:
                logging.info("telemetry: UDP %s waits until UDP %s has been quiet for %.0f s", port,
                             self._released[0], LOCK_SILENCE)

    def handle(self, now, data, addr, port=None):
        """One datagram from `addr` received at `now` (monotonic seconds) on
        UDP `port` (None: the fixed port): the live path, also driven
        directly by tests and replays."""
        if port is None:
            port = self.port
        recorder = self.recorder
        if not self.auto:
            if recorder is not None:
                # Byte for byte, before anything is decided about it: a
                # capture must show what arrived, unknown packets included
                recorder.packet(now, addr, data, port)
            # A source that went quiet frees the lock even while another one
            # keeps sending (the socket then never times out)
            self.check_idle(now)
        else:
            self.check_idle(now)
            if self._source is not None and port != self._source[0]:
                self._drop(port, data)                  # another port while one is active: not recorded or decoded
                return
            if recorder is not None:
                # The active port, all of it as in fixed mode; with nothing
                # locked every port, unknown packets included (a file per
                # port): a game Oversteer does not decode yet is captured too
                recorder.packet(now, addr, data, port)
            if self._source is None and self._released is not None and port != self._released[0] \
                    and now - self._released[1] < LOCK_SILENCE:
                self._drop(port, data)                  # the last port may only be pausing
                return
        sample = decode_sample(data)
        if sample is None:
            if len(data) not in self._unknown_sizes:
                self._unknown_sizes.add(len(data))
                logging.info("telemetry: unknown %d-byte packet from %s", len(data), addr[0])
            return
        # One source at a time: a stale bridge next to a game, or a replay
        # sent while a game runs, would otherwise alternate cars packet by
        # packet and end and start a learning session on each
        source = (port, addr[0], sample.game)
        if self._source is None:
            self._lock(source, len(data), now)
        elif source != self._source:
            if source not in self._ignored:
                self._ignored.add(source)
                logging.info("telemetry: ignoring %s from %s while %s from %s is arriving",
                             source[2] or 'telemetry', source[1], self._source[2] or 'telemetry', self._source[1])
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
        pedals = self._pedals()
        # The game's view of the pedals when it sends one (it includes an
        # automatic clutch and traction control); ours otherwise
        throttle = sample.throttle if sample.throttle is not None else pedals.get('throttle')
        clutch = sample.clutch if sample.clutch is not None else pedals.get('clutch')
        if self.launch:
            self._learn_launch(now, rpm, pedals, sample.speed)
            self._raise_launch(now, sample, throttle, clutch)
        # What the learner is told the limiter is, and where from
        learn_limiter, learn_source = max_rpm, 'game'
        if max_rpm is None and sample.car is not None:
            # No maximum sent (ACR sends 0): the car's limiter from the game's files, where it has them
            shipped = (car_data.entry(sample.car) or {}).get('limiter_rpm')
            if shipped:
                max_rpm = learn_limiter = float(shipped)
                self.last_max_rpm = max_rpm
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
            # The learner gets the highest rpm seen, not the lights' ceiling (which is rpm / shift fraction
            # above it and would be learnt as the engine's limiter)
            if sample.car != self._seen_car:
                self._seen_car, self.seen_max = sample.car, 0.0
            self.seen_max = max(self.seen_max, rpm)
            learn_limiter, learn_source = self.seen_max, 'seen'
        else:
            self.last_max_rpm = max_rpm
        # Everything is relative to the shift point: the bar completes
        # there and flashes above it.
        if self.launch and self.launch_max:
            max_rpm = self.launch_max
        learnt = self._feed_learner(now, sample, learn_limiter, learn_source, pedals, throttle, clutch)
        if learnt:
            # A gear that pulls to the limiter learns the limiter itself as
            # its shift point: the engine could never reach 3 % past it,
            # and bouncing off it must still flash
            reference = min(learnt, max_rpm * LEARNT_FLASH / (1.0 + FLASH_MARGIN)) if max_rpm else learnt
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

    def _feed_learner(self, now, sample, limiter, source, pedals, throttle, clutch):
        """Teach the learner; the learnt shift point for this gear when
        the rev lights should use it."""
        learner = self.learner
        if learner is None:
            return None
        if self.launch and self.launch_max:
            limiter, source = self.launch_max, 'launch'
        try:
            if sample.car is None:
                # Forza's menus send packets at full rate with no car: the
                # socket never times out, so the pause (pending changes of
                # gear, the model's save) and the session's end are seen here
                if not self._carless:
                    learner.idle()
                learner.tick(now)
            self._carless = sample.car is None
            learner.feed(now, sample, limiter, throttle, clutch, pedals.get('shift_press'), source)
            learnt = learner.shift_rpm(sample.gear) if self.use_learnt else None
        except Exception:
            # Once a minute at most: a learner that keeps failing would
            # otherwise log at packet rate
            if self._learner_error_at is None or now - self._learner_error_at >= LEARNER_ERROR_EVERY:
                self._learner_error_at = now
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
