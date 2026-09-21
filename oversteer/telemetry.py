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
- Codemasters extradata=3 (DiRT Rally 2.0, DiRT 4): 264 bytes of floats,
  engine rate at index 37 and max at 63.

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
DEFAULT_THRESHOLDS = (0.70, 0.78, 0.86, 0.92, 0.97)   # fraction of max RPM per LED
LIMITER_FRACTION = 0.99
IDLE_TIMEOUT = 2.0                                   # seconds without telemetry -> LEDs off
FLASH_PERIOD = 0.08                                  # limiter flash half-period (seconds)
RPM_LIMIT = 30000.0                                  # anything above is not an engine speed
FORZA_SIZES = (232, 311, 324, 331)


class RevLeds:
    """The wheel's rev LEDs (Linux LED class), lit as a bar."""

    def __init__(self, sysfs_device_path):
        pattern = os.path.join(sysfs_device_path, 'leds', '*RPM*', 'brightness')
        self.paths = sorted(glob.glob(pattern), key=lambda p: os.path.dirname(p))
        self._last = None

    def available(self):
        return bool(self.paths) and all(os.access(p, os.W_OK) for p in self.paths)

    def set_count(self, lit):
        """Light the first `lit` LEDs."""
        pattern = tuple(i < lit for i in range(len(self.paths)))
        self.set_pattern(pattern)

    def set_pattern(self, pattern):
        pattern = tuple(bool(x) for x in pattern)
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


def decode(data):
    """Return (rpm, max_rpm or None, shift_light or None) or None if the
    packet isn't a telemetry format we know (or carries nonsense)."""
    n = len(data)
    if n in FORZA_SIZES:
        race_on = struct.unpack_from('<i', data, 0)[0]
        max_rpm, idle_rpm, rpm = struct.unpack_from('<fff', data, 8)
        if not (_plausible(max_rpm) and _plausible(rpm)):
            return None
        if race_on == 0 or max_rpm <= 0:
            return (0.0, max_rpm if max_rpm > 0 else None, None)
        return (max(0.0, rpm), max_rpm, None)
    if n in (92, 96):
        car = data[4:8]
        if any(b and not 0x20 <= b < 0x7f for b in car):     # Car[4]: short ASCII name
            return None
        rpm = struct.unpack_from('<f', data, 16)[0]
        if not _plausible(rpm):
            return None
        dashlights, showlights = struct.unpack_from('<II', data, 40)
        shift = bool(showlights & (1 << 0))          # DL_SHIFT
        return (max(0.0, rpm), None, shift)
    if n == 264:
        floats = struct.unpack_from('<66f', data, 0)
        rpm, max_rpm = floats[37] * 10.0, floats[63] * 10.0
        if not (_plausible(max_rpm) and _plausible(rpm)) or max_rpm <= 0:
            return None
        return (max(0.0, rpm), max_rpm, None)
    return None


class Telemetry:
    """UDP listener thread driving a RevLeds."""

    def __init__(self, leds, port=DEFAULT_PORT, thresholds=DEFAULT_THRESHOLDS, on_status=None):
        self.leds = leds
        self.port = int(port)
        self.thresholds = tuple(thresholds)
        self.on_status = on_status
        self.running = False
        self.last_packet = 0.0
        self.last_source = None
        self.learned_max = 0.0
        self._thread = None
        self._sock = None

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
                    self._status(None)
                continue
            except OSError:
                break
            decoded = decode(data)
            if decoded is None:
                continue
            rpm, max_rpm, shift = decoded
            now = time.monotonic()
            self.last_packet = now
            if self.last_source != addr[0]:
                self.last_source = addr[0]
                self._status(addr[0])
            if max_rpm is None:
                # OutGauge: learn the ceiling from what we see, letting it
                # sag slowly so a change of car with a lower redline still
                # fills the bar.
                if rpm > self.learned_max:
                    self.learned_max = rpm
                else:
                    self.learned_max = max(rpm, self.learned_max * 0.9995)
                max_rpm = self.learned_max
            fraction = rpm / max_rpm if max_rpm > 0 else 0.0
            if shift or fraction >= LIMITER_FRACTION:
                if now - flash_at >= FLASH_PERIOD:
                    flash = not flash
                    flash_at = now
                    self.leds.set_pattern((flash,) * len(self.leds.paths))
                lit_state = None
                continue
            lit = sum(1 for t in self.thresholds if fraction >= t)
            if lit != lit_state:
                self.leds.set_count(lit)
                lit_state = lit

    def _status(self, source):
        if self.on_status is not None:
            try:
                self.on_status(source)
            except Exception:
                pass
