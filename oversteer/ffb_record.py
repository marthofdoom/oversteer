"""The engine's side of a "Record for FFB tuning" capture: everything an offline replay of the whole force chain needs
besides the telemetry (the capture itself) and the wheel's position (the capture's `.wheel.gz` sidecar, ABS_X).
A third sidecar, `<capture>.ffb.gz`: gzip of

    b'OVFFB1\\n'
    one JSON line: {"capture": <file name>, "started": <epoch>, "channels": {<name>: <id>}, "oversteer": <version>, "build": <commit>,
                    "settings": {the countersteer_* values in effect, "learnt": {car, ratio, lock, trail, ...}},
                    "clock": "seconds since the capture's first packet", "fields": "t channel a b c d e"}
    records: struct '<dB5d' (seconds since the capture's first packet, channel id, five values; unused ones 0)

Channels (values): game_ffb_raw (level, count, effect id), ext_force (value written), ffb_align (target, k, tau_max
as written, uFS and 1e-4 FS units of the driver), ffb_dyn_damping / ffb_dyn_friction (value written), ffb_dyn_wheel
(damping, inertia, friction, breakaway, latency as written), ffb_align_read (k_limit, range), ffb_hands (torque
per mille, hands on), ffb_dyn_live (friction, damping, seconds), ffb_thermal (mean square, derating, gain), off
(the stage set back to 0), game_ffb_level / game_const_scale (the driver's value read back, percent; on change and at
least once a second; files from before have neither, readers skip names they do not know). `load()` reads one back.

Settings: the header's "settings" is the engine's snapshot (`settings_source`) when the file starts. A value that
changes later cannot be a record (the format is numbers only), so `FfbSampler.check_settings` logs an INFO line with the
seconds since the capture's first packet and keeps the change in `changes` (not in the file). Old files have no
"settings"/"build": readers take what is there.

The engine calls `log()`; with no recording running it does nothing (one attribute test). Times are on the engine's
clock, which is the listener's (time.monotonic), minus the capture's origin."""
import collections
import gzip
import json
import logging
import struct
import threading
import time
import zlib

MAGIC = b'OVFFB1\n'
RECORD = struct.Struct('<dB5d')
SUFFIX = '.ffb.gz'
CHANNELS = {name: i for i, name in enumerate((
    'game_ffb_raw', 'ext_force', 'ffb_align', 'ffb_dyn_damping', 'ffb_dyn_friction', 'ffb_dyn_wheel', 'ffb_align_read',
    'ffb_hands', 'ffb_dyn_live', 'ffb_thermal', 'off', 'game_ffb_level', 'game_const_scale'), 1)}
NAMES = {i: name for name, i in CHANNELS.items()}

current = None                           # the running FfbSampler, None when nothing is recorded
remote = None                            # (path, origin, header) -> a sampler in Countersteer FFB's engine process
                                         # (ffb_process), None while the engine runs in this one
settings_source = None                   # () -> {"profile": {...}, "learnt": {...}} the engine's settings now (FfbEngine sets it)


def active():
    return current is not None


def log(t, channel, *values):
    """One record at engine time `t`; `values` are numbers, or the single text a driver attribute was written with."""
    sampler = current
    if sampler is not None:
        sampler.add(t, channel, values)


def sampler(path, origin, header=None):
    """The sidecar's sampler, with start() and stop(): where the engine runs. With the engine in its own process
    (ffb_process) the sidecar is written there, on the same clock (CLOCK_MONOTONIC is the machine's): its records never
    cross the process boundary and the engine never waits for the GUI."""
    make = remote
    return FfbSampler(path, origin, header) if make is None else make(path, origin, header)


def check(t):
    """The engine's pass: log a changed setting of the running recording (one attribute test with none)."""
    sampler = current
    if sampler is not None:
        sampler.check_settings(t)


def _snapshot():
    source = settings_source
    if source is None:
        return None
    try:
        return source()
    except Exception:
        logging.exception("ffb record: settings")
        return None


def _numbers(values):
    if len(values) == 1 and isinstance(values[0], str):
        values = values[0].split()
    out = []
    for v in values[:5]:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            out.append(0.0)
    return out + [0.0] * (5 - len(out))


class FfbSampler:
    """Collects records from the engine thread and writes them from a thread of its own (gzip never runs on the
    engine's). `origin` is the capture's time zero on the engine's clock."""

    FLUSH = 0.2

    def __init__(self, path, origin, header=None):
        self.path = path
        self.origin = origin
        self.records = 0
        self.changes = []                # (seconds, key, old, new) of the settings changed since the start
        self._settings = None
        self._queue = collections.deque()
        self._stop = threading.Event()
        self._thread = None
        self._file = gzip.open(path, 'wb')
        try:
            self._file.write(MAGIC)
            meta = {'clock': "seconds since the capture's first packet", 'fields': 't channel a b c d e',
                    'channels': CHANNELS}
            self._settings = _snapshot()
            if self._settings is not None:
                meta['settings'] = self._settings
            meta.update(header or {})
            self._file.write(json.dumps(meta).encode() + b'\n')
            self._file.flush()
        except BaseException:
            self._file.close()
            raise

    def add(self, t, channel, values):
        channel_id = CHANNELS.get(channel)
        if channel_id is not None:
            self._queue.append((t - self.origin, channel_id, _numbers(values)))

    def check_settings(self, t):
        """Compare the profile values now with those logged last; each change is an INFO line `t` s after the
        capture's first packet (the learnt values keep changing and are only in the header). Called by the engine."""
        old = self._settings
        now = _snapshot()
        if old is None or now is None:
            return
        for key, value in now.get('profile', {}).items():
            before = old.get('profile', {}).get(key)
            if value != before:
                self.changes.append((t - self.origin, key, before, value))
                logging.info("ffb record: setting %s %r -> %r at %.2f s", key, before, value, t - self.origin)
        self._settings = now

    def pump(self):
        """Write what has been collected (tests call this directly)."""
        file = self._file
        if file is None:
            return
        queue = self._queue
        while queue:
            t, channel, values = queue.popleft()
            file.write(RECORD.pack(t, channel, *values))
            self.records += 1

    def start(self):
        global current
        current = self
        self._thread = threading.Thread(target=self._run, name='ffb-record', daemon=True)
        self._thread.start()

    def _run(self):
        try:
            while not self._stop.wait(self.FLUSH):
                self.pump()
        except Exception:
            logging.exception("ffb record")
        self._finish()

    def stop(self, timeout=2.0):
        global current
        if current is self:
            current = None
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        else:
            self._finish()

    def _finish(self):
        file, self._file = self._file, None
        if file is not None:
            try:
                while self._queue:
                    t, channel, values = self._queue.popleft()
                    file.write(RECORD.pack(t, channel, *values))
                    self.records += 1
                file.close()
            except OSError:
                pass


def load(path):
    """(meta dict, iterator of (t, channel name, (a, b, c, d, e))) of an `.ffb.gz` sidecar; ends quietly at a truncated
    tail, ValueError if the file is not one."""
    stream = gzip.open(path, 'rb')
    try:
        if stream.readline() != MAGIC:
            raise ValueError("{}: not an Oversteer FFB sidecar".format(path))
        meta = json.loads(stream.readline().decode() or '{}')
    except (OSError, EOFError, ValueError, zlib.error):
        stream.close()
        raise ValueError("{}: not an Oversteer FFB sidecar".format(path))
    names = {i: n for n, i in (meta.get('channels') or CHANNELS).items()}

    def records():
        try:
            while True:
                head = stream.read(RECORD.size)
                if len(head) < RECORD.size:
                    return
                t, channel, *values = RECORD.unpack(head)
                yield t, names.get(channel, str(channel)), tuple(values)
        except (EOFError, OSError, zlib.error):
            return
        finally:
            stream.close()

    return meta, records()
