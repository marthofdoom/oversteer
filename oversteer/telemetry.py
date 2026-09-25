"""Game telemetry -> wheel rev LEDs.

A small UDP listener understands the telemetry formats that cross the
Proton boundary and turns engine RPM into the wheel's five rev LEDs:

- Forza Horizon / Motorsport "Data Out" (sled 232 bytes, FM7 dash 311,
  FH4+ dash 324, FM 2023 dash 331): EngineMaxRpm at 8, EngineIdleRpm at 12,
  CurrentEngineRpm at 16 as little-endian floats, IsRaceOn at 0. Enable it
  in the game under Settings > HUD and gameplay > Data Out, pointing at this
  machine's IP and the port set here.
- OutGauge (BeamNG.drive, Live for Speed): 92/96 bytes, rpm as a float at
  16, the shift-light flag (DL_SHIFT) in the ShowLights word at 44. There is
  no max RPM in the packet, so the ceiling is learnt from the highest RPM
  seen and forgotten when the telemetry stops.
- Codemasters extradata=3 (DiRT Rally 2.0, DiRT 4) and the games that copy
  its layout (WRC 10 / WRC Generations native telemetry): 64 or more
  little-endian floats, engine rate at index 37 and max at 63, both in
  rpm / 10. The packet length varies by game, so any 4-byte-aligned length
  from 256 bytes up is accepted once the Forza sizes are excluded.

- Oversteer's own "OVST" datagram (24 bytes) from oversteer-shm-bridge, the
  helper that runs inside a Proton prefix and forwards shared-memory
  telemetry (Assetto Corsa, Assetto Corsa Competizione, Assetto Corsa
  Rally): rpm, max rpm (0 = unknown), gear, speed.

The listener runs in a daemon thread and writes the LED brightness files
through :class:`RevLeds`; when no packet arrives for a while the LEDs go
out so a stale value never stays lit.
"""

import glob
import logging
import math
import os
import socket
import struct
import threading
import time

DEFAULT_PORT = 5300
DEFAULT_SHIFT = 0.97                                 # shift point as a fraction of max RPM
LED_SPACING = (0.72, 0.80, 0.89, 0.95, 1.0)          # per LED, as a fraction of the shift point
FLASH_MARGIN = 0.03                                  # above the shift point: flash (shift now)
LEARNED_DECAY = 0.01                                 # OutGauge: learnt ceiling sags this much per second
IDLE_TIMEOUT = 2.0                                   # seconds without telemetry -> LEDs off
FLASH_PERIOD = 0.08                                  # limiter flash half-period (seconds)
RPM_LIMIT = 30000.0                                  # anything above is not an engine speed
FORZA_SIZES = (232, 311, 324, 331)
CODEMASTERS_MIN = 64 * 4                             # DR2/DiRT 4 extradata 3 is 264, WRCG is longer
CODEMASTERS_MAX = 512
OVST_MAGIC = b'OVST'
OVST_SIZE = 24
OVST2_SIZE = 64                                      # + throttle, brake, car name

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


def _plausible(x):
    return math.isfinite(x) and -RPM_LIMIT < x < RPM_LIMIT


class Sample:
    """One decoded packet. Everything but rpm may be None (not in this
    format): max_rpm, shift (the game's shift light), gear (1.. forward,
    0 neutral, -1 reverse), speed (m/s), car (a key naming the car within
    its game), car_name, throttle and clutch (0..1, the game's view),
    power (W, Forza only)."""

    __slots__ = ('rpm', 'max_rpm', 'shift', 'gear', 'speed', 'car', 'car_name', 'throttle', 'clutch', 'power')

    def __init__(self, rpm, max_rpm=None, shift=None, gear=None, speed=None, car=None, car_name=None,
                 throttle=None, clutch=None, power=None):
        self.rpm, self.max_rpm, self.shift = rpm, max_rpm, shift
        self.gear, self.speed, self.car, self.car_name = gear, speed, car, car_name
        self.throttle, self.clutch, self.power = throttle, clutch, power


def _finite(x):
    return x if math.isfinite(x) else None


def _ascii(raw):
    text = raw.split(b'\0', 1)[0].decode('ascii', 'replace').strip()
    return text if text and all(0x20 <= ord(c) < 0x7f for c in text) else None


def decode_sample(data):
    """A Sample, or None if the packet isn't a telemetry format we know
    (or carries nonsense)."""
    n = len(data)
    if n in (OVST_SIZE, OVST2_SIZE) and data[:4] == OVST_MAGIC:
        version, source, flags, rpm, max_rpm, gear, speed = struct.unpack_from('<BBHffif', data, 4)
        if version not in (1, 2) or (version == 2) != (n == OVST2_SIZE):
            return None
        if not (_plausible(rpm) and _plausible(max_rpm)):
            return None
        sample = Sample(max(0.0, rpm), max_rpm if max_rpm > 0 else None, bool(flags & 1),
                        gear=gear if -1 <= gear <= 12 else None,
                        speed=_finite(speed / 3.6), car='acpmf')
        if version == 2:
            gas, brake = struct.unpack_from('<ff', data, 24)
            sample.throttle = _finite(gas)
            name = _ascii(data[32:64])
            if name:
                sample.car, sample.car_name = 'acpmf-' + name, name
        return sample
    if n in FORZA_SIZES:
        race_on = struct.unpack_from('<i', data, 0)[0]
        max_rpm, idle_rpm, rpm = struct.unpack_from('<fff', data, 8)
        if not (_plausible(max_rpm) and _plausible(rpm)):
            return None
        if race_on == 0 or max_rpm <= 0:
            return Sample(0.0, max_rpm if max_rpm > 0 else None)
        ordinal = struct.unpack_from('<i', data, 212)[0]
        sample = Sample(max(0.0, rpm), max_rpm, car='forza-{}'.format(ordinal),
                        car_name='Forza car {}'.format(ordinal))
        if n == 232:
            vx, vy, vz = struct.unpack_from('<fff', data, 32)
            sample.speed = _finite(math.sqrt(vx * vx + vy * vy + vz * vz))
        else:
            # The dash block follows the sled; Horizon puts 12 more bytes first
            base = 244 if n == 324 else 232
            speed, power = struct.unpack_from('<ff', data, base + 12)
            accel, brake, clutch, handbrake, gear = struct.unpack_from('<BBBBB', data, base + 71)
            sample.speed, sample.power = _finite(speed), _finite(power)
            sample.throttle, sample.clutch = accel / 255.0, clutch / 255.0
            sample.gear = gear if 1 <= gear <= 10 else (-1 if gear == 0 else None)
        return sample
    if n in (92, 96):
        car = data[4:8]
        if any(b and not 0x20 <= b < 0x7f for b in car):     # Car[4]: short ASCII name
            return None
        rpm = struct.unpack_from('<f', data, 16)[0]
        if not _plausible(rpm):
            return None
        dashlights, showlights = struct.unpack_from('<II', data, 40)
        shift = bool(showlights & (1 << 0))          # DL_SHIFT
        gear = data[10]                              # 0 reverse, 1 neutral, 2 first
        speed = struct.unpack_from('<f', data, 12)[0]
        throttle, brake, clutch = struct.unpack_from('<fff', data, 48)
        name = _ascii(car)
        return Sample(max(0.0, rpm), None, shift, gear=gear - 1 if gear >= 1 else -1,
                      speed=_finite(speed), car='outgauge-' + (name or 'car'), car_name=name,
                      throttle=_finite(throttle), clutch=_finite(clutch))
    if CODEMASTERS_MIN <= n <= CODEMASTERS_MAX and n % 4 == 0:
        floats = struct.unpack_from('<%df' % min(66, n // 4), data, 0)
        rpm, max_rpm = floats[37] * 10.0, floats[63] * 10.0
        if not (_plausible(max_rpm) and _plausible(rpm)) or max_rpm <= 0:
            return None
        idle = floats[64] * 10.0 if len(floats) > 64 else float('nan')
        gears = floats[65] if len(floats) > 65 else float('nan')
        gear = floats[33]
        # No car name in this format: the engine and gearbox tell cars apart
        key = 'codemasters-{:.0f}-{:.0f}-{:.0f}'.format(max_rpm, idle if math.isfinite(idle) else 0,
                                                       gears if math.isfinite(gears) else 0)
        name = '{:.0f} rpm, {:.0f} gears'.format(max_rpm, gears) if math.isfinite(gears) else None
        return Sample(max(0.0, rpm), max_rpm,
                      gear=int(gear) if math.isfinite(gear) and 0 <= gear <= 9 else None,
                      speed=_finite(floats[7]), car=key, car_name=name,
                      throttle=_finite(floats[29]), clutch=_finite(floats[32]))
    return None


def decode(data):
    """Return (rpm, max_rpm or None, shift_light or None) or None if the
    packet isn't a telemetry format we know (or carries nonsense)."""
    sample = decode_sample(data)
    return None if sample is None else (sample.rpm, sample.max_rpm, sample.shift)


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
        lit_state = None            # LEDs lit as a bar; None = unknown, rewrite on next packet
        flash = False
        flash_at = 0.0
        while self.running:
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                if self.last_packet and time.monotonic() - self.last_packet > IDLE_TIMEOUT:
                    self.leds.off()
                    lit_state = None
                    self.last_packet = 0.0
                    self.last_source = None
                    self.learned_max = 0.0
                    self._learned_at = 0.0
                    # Menus or a loading screen: the next stage may be
                    # another car, and it starts with a launch anyway
                    self.launch_max = 0.0
                    self._launch_samples = []
                    self.live = None
                    if self.learner is not None:
                        self.learner.idle()
                    self._status(None)
                continue
            except OSError:
                break
            sample = decode_sample(data)
            if sample is None:
                if len(data) not in self._unknown_sizes:
                    self._unknown_sizes.add(len(data))
                    logging.info("telemetry: unknown %d-byte packet from %s", len(data), addr[0])
                continue
            rpm, max_rpm, shift = sample.rpm, sample.max_rpm, sample.shift
            now = time.monotonic()
            self.live = sample
            self.last_packet = now
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
                if now - flash_at >= FLASH_PERIOD:
                    flash = not flash
                    flash_at = now
                    self.leds.set_pattern((flash,) * len(self.leds.paths))
                lit_state = None
                continue
            lit = sum(1 for t in LED_SPACING if fraction >= t)
            if lit != lit_state:
                self.leds.set_count(lit)
                lit_state = lit

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
            learner.feed(now, sample, limiter, throttle, clutch)
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
