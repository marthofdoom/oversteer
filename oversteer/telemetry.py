"""Game telemetry -> wheel rev LEDs.

A small UDP listener understands the telemetry formats that cross the
Proton boundary and turns engine RPM into the wheel's five rev LEDs:

- Forza Horizon / Motorsport "Data Out" (sled 232 bytes, dash 311, FH4+ dash
  324): EngineMaxRpm at 8, EngineIdleRpm at 12, CurrentEngineRpm at 16 as
  little-endian floats, IsRaceOn at 0. Enable it in the game under
  Settings > HUD and gameplay > Data Out, pointing at this machine's IP and
  the port set here.
- OutGauge (BeamNG.drive, Live for Speed): 92/96 bytes, rpm as a float at
  16, the shift-light flag in the dashlights word at 24. There is no max RPM
  in the packet, so the ceiling is learnt from the highest RPM seen.
- Codemasters extradata=3 (DiRT Rally 2.0, DiRT 4): 264 bytes of floats,
  engine rate at index 37 and max at 63.

The listener runs in a daemon thread and writes the LED brightness files
through :class:`RevLeds`; when no packet arrives for a while the LEDs go
out so a stale value never stays lit.
"""

import glob
import logging
import os
import socket
import struct
import threading
import time

DEFAULT_PORT = 5300
DEFAULT_THRESHOLDS = (0.70, 0.78, 0.86, 0.92, 0.97)   # fraction of max RPM per LED
LIMITER_FRACTION = 0.99
IDLE_TIMEOUT = 2.0                                   # seconds without telemetry -> LEDs off


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


def decode(data):
    """Return (rpm, max_rpm or None, shift_light or None) or None if the
    packet isn't a telemetry format we know."""
    n = len(data)
    if n in (232, 311, 324) and n >= 20:
        race_on = struct.unpack_from('<i', data, 0)[0]
        max_rpm, idle_rpm, rpm = struct.unpack_from('<fff', data, 8)
        if race_on == 0 or max_rpm <= 0:
            return (0.0, max_rpm if max_rpm > 0 else None, None)
        return (max(0.0, rpm), max_rpm, None)
    if n in (92, 96):
        rpm = struct.unpack_from('<f', data, 16)[0]
        dashlights, showlights = struct.unpack_from('<II', data, 20)
        shift = bool(showlights & (1 << 0))          # DL_SHIFT
        return (max(0.0, rpm), None, shift)
    if n == 264:
        floats = struct.unpack_from('<66f', data, 0)
        rpm, max_rpm = floats[37] * 10.0, floats[63] * 10.0
        if max_rpm <= 0:
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
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
        lit_state = None
        flash = False
        while self.running:
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                if self.last_packet and time.monotonic() - self.last_packet > IDLE_TIMEOUT:
                    self.leds.off()
                    self.last_packet = 0.0
                    self._status(None)
                continue
            except OSError:
                break
            decoded = decode(data)
            if decoded is None:
                continue
            rpm, max_rpm, shift = decoded
            self.last_packet = time.monotonic()
            if self.last_source != addr[0]:
                self.last_source = addr[0]
                self._status(addr[0])
            if max_rpm is None:
                # OutGauge: learn the ceiling from what we see
                self.learned_max = max(self.learned_max, rpm)
                max_rpm = self.learned_max
            fraction = rpm / max_rpm if max_rpm > 0 else 0.0
            if shift or fraction >= LIMITER_FRACTION:
                flash = not flash
                self.leds.set_pattern((flash,) * len(self.leds.paths))
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
