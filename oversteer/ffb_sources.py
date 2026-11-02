"""Side streams for force feedback: small receivers beside the telemetry listener (telemetry.py), which keeps one
game source at a time. The FFB engine reads `latest()`; nothing here touches the coaching telemetry.

BeamNGForceSource takes the Countersteer BeamNG mod's "CSB1" packets (data/beamng/countersteer_ffb) on
127.0.0.1:PORT. The rack force is the mean over the physics steps since the last packet (up to 500 Hz); the
wheels' load, slip and side slip are written by the game at its graphics rate (about 60 Hz), so they repeat across
packets and must not be differentiated or counted as new at the packet rate.
"""

import logging
import socket
import threading
import time

from .telemetry_formats import CSB_SIZE, decode_sample

PORT = 5320                     # free of every game's default, of the presets and of Forza's 5200-5300
STALE_S = 0.050                 # a sample older than this is not the car's now: latest() gives None
RECV_TIMEOUT = 0.1              # s a recv waits, so stop() is noticed


class BeamNGForceSource:
    """Receives CSB1 on its own daemon thread. start() returns False (and logs) if the port is held;
    latest() is the newest Sample or None when there is none or it is stale; seq counts packets taken."""

    def __init__(self, port=PORT, host='127.0.0.1', stale=STALE_S):
        self.port, self.host, self.stale = port, host, stale
        self.seq = 0
        self.bound_port = None
        self._sample = None
        self._at = 0.0
        self._sock = None
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        if self._thread is not None:
            return True
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.host, self.port))
        except OSError as e:
            sock.close()
            logging.warning("ffb: BeamNG force source: can't bind %s:%s: %s", self.host, self.port, e)
            return False
        sock.settimeout(RECV_TIMEOUT)
        self._sock = sock
        self.bound_port = sock.getsockname()[1]
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='beamng-force', daemon=True)
        self._thread.start()
        return True

    def stop(self):
        thread, self._thread = self._thread, None
        self._stop.set()
        if thread is not None:
            thread.join(1.0)
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self._sample = None

    def latest(self, now=None):
        """(The newest Sample, or None if none arrived or the last is older than `stale`)."""
        sample = self._sample
        if sample is None:
            return None
        now = time.monotonic() if now is None else now
        return sample if now - self._at <= self.stale else None

    def _run(self):
        sock = self._sock
        while not self._stop.is_set():
            try:
                data = sock.recv(512)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) != CSB_SIZE:
                continue
            sample = decode_sample(data)
            if sample is not None and sample.game == 'beamng-ffb':
                self._sample = sample
                self._at = time.monotonic()
                self.seq += 1
