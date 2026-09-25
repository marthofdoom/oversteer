"""Raw telemetry captures: every datagram as it arrived, to replay later.

A capture keeps what the game sent, byte for byte, so the decoders and
learners can be run again on it when they improve, bugs can be reported
with the data that shows them, and tests can use real drives. The file is
a gzip stream:

    b'OVCAP1\\n'
    one JSON line: {"started": <epoch>, "port": <udp port>, "oversteer": <version>, "note": ""}
    records: struct '<dIH' (seconds since the first packet, source IPv4 as
             an unsigned 32-bit number, length) followed by the bytes

A capture cut short (a crash while writing) reads up to its last whole
record. The app records into a folder (`Recorder`), a file per stretch of
driving; the labels given to a session are kept next to its captures in
a `<file>.json` sidecar, so a capture copied elsewhere (a test fixture, a
bug report) still says what it was.
"""

import glob
import gzip
import json
import logging
import os
import queue
import socket
import struct
import threading
import time
import zlib

MAGIC = b'OVCAP1\n'
RECORD = struct.Struct('<dIH')
SUFFIX = '.ovcap.gz'
CAPTURE_GAP = 60.0          # seconds without telemetry that close a capture file: a menu or a pause does not
RECORD_QUEUE = 4096         # packets waiting for the writer (about 30 s at 120 a second)
DEFAULT_CAP = 1 << 30       # bytes kept in the folder; the oldest unlabelled captures go first


def _ipv4(address):
    try:
        return struct.unpack('!I', socket.inet_aton(address))[0]
    except (OSError, TypeError):
        return 0


class CaptureWriter:
    """Writes one capture file. `write(now, addr, data)` takes any clock in
    seconds (the listener's monotonic one); times are stored from the first
    packet on."""

    def __init__(self, path, port=None, note='', version=None, started=None):
        self.path = path
        self._file = gzip.open(path, 'wb')
        self._file.write(MAGIC)
        meta = {'started': time.time() if started is None else started, 'port': port, 'oversteer': version,
                'note': note}
        self._file.write(json.dumps(meta).encode() + b'\n')
        self._first = None
        self.packets = 0

    def write(self, now, addr, data):
        if self._first is None:
            self._first = now
        self._file.write(RECORD.pack(now - self._first, _ipv4(addr[0]), len(data)))
        self._file.write(data)
        self.packets += 1

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def read_capture(path):
    """(meta dict, iterator of (t, (address, 0), data)). The iterator ends
    quietly at a truncated tail; a file that is not a capture raises
    ValueError at once."""
    stream = gzip.open(path, 'rb')
    try:
        if stream.readline() != MAGIC:
            raise ValueError("{}: not an Oversteer capture".format(path))
        meta = json.loads(stream.readline().decode() or '{}')
    except (OSError, EOFError, ValueError, zlib.error):
        stream.close()
        raise ValueError("{}: not an Oversteer capture".format(path))

    def records():
        try:
            while True:
                head = stream.read(RECORD.size)
                if len(head) < RECORD.size:
                    return
                t, source, length = RECORD.unpack(head)
                data = stream.read(length)
                if len(data) < length:
                    return
                yield t, (socket.inet_ntoa(struct.pack('!I', source)), 0), data
        except (EOFError, OSError, zlib.error):
            return                       # cut short while it was being written
        finally:
            stream.close()

    return meta, records()


def list_captures(folder):
    """[(path, bytes, labelled)] of the captures in `folder`, oldest
    first (by when they were last written)."""
    found = []
    for path in glob.glob(os.path.join(glob.escape(folder), '*' + SUFFIX)):
        try:
            found.append((os.path.getmtime(path), path, os.path.getsize(path), os.path.exists(path + '.json')))
        except OSError:
            continue                     # deleted meanwhile
    return [(path, size, labelled) for _mtime, path, size, labelled in sorted(found)]


def capture_summary(folder):
    """(captures, bytes, labelled) in `folder`."""
    captures = list_captures(folder)
    return len(captures), sum(size for _p, size, _l in captures), sum(1 for _p, _s, labelled in captures if labelled)


def prune(folder, cap, keep=None):
    """Delete the oldest unlabelled captures until the folder holds at
    most `cap` bytes; labelled ones and `keep` (the file being written)
    stay. The paths deleted."""
    captures = list_captures(folder)
    total = sum(size for _p, size, _l in captures)
    deleted = []
    for path, size, labelled in captures:
        if total <= cap:
            break
        if labelled or path == keep:
            continue
        try:
            os.remove(path)
        except OSError:
            continue
        total -= size
        deleted.append(path)
    return deleted


def capture_span(path):
    """(started, ended) wall-clock seconds of a capture: its header's
    start and the file's last write. None if it is not a capture."""
    try:
        with gzip.open(path, 'rb') as stream:
            if stream.readline() != MAGIC:
                return None
            started = json.loads(stream.readline().decode() or '{}').get('started')
        return (started if started is not None else os.path.getmtime(path)), os.path.getmtime(path)
    except (OSError, EOFError, ValueError, zlib.error):
        return None


def label_captures(folder, started, ended, label, about=None):
    """Write `label` (and `about`: the session's game, car, stage) to the
    sidecar of every capture in `folder` that overlaps [started, ended]:
    labelled captures are kept when the folder is pruned. The count."""
    count = 0
    for path, _size, _labelled in list_captures(folder):
        span = capture_span(path)
        if span is None or span[0] > ended or span[1] < started:
            continue
        try:
            with open(path + '.json', 'w') as f:
                json.dump({'label': label, 'session': dict(about or {}, started=started, ended=ended)}, f,
                          indent=1, sort_keys=True)
            count += 1
        except OSError as e:
            logging.warning("capture label %s: %s", path, e)
    return count


class Recorder:
    """Records every datagram the listener receives into `folder`: a file
    per stretch of driving (a new one after CAPTURE_GAP without
    telemetry), named by when it started. The listener only queues each
    packet (`packet()` never blocks: when the queue is full the packet is
    dropped and counted); gzip and the disk are this object's own thread,
    so a slow disk never holds up the rev lights, and recording works
    whether or not the learner has a database. `threaded=False` writes
    inline (tests); `check(now)` then closes a file gone quiet."""

    def __init__(self, folder, cap=DEFAULT_CAP, port=None, version=None, threaded=True, clock=time.time):
        self.folder = folder
        self.cap = cap
        self.port = port
        self.version = version
        self.clock = clock
        self.dropped = 0
        self.error = None                 # why recording stopped (a full disk, no permission)
        self.path = None                  # the file being written
        self._writer = None
        self._last = 0.0
        self._queue = queue.Queue(maxsize=RECORD_QUEUE)
        self._thread = None
        if threaded:
            self._thread = threading.Thread(target=self._run, name='telemetry-capture', daemon=True)
            self._thread.start()

    def packet(self, now, addr, data):
        """The listener: one datagram as it arrived, at `now` (monotonic)."""
        if self._thread is None:
            self._write(now, addr, data)
            return
        try:
            self._queue.put_nowait((now, addr, data))
        except queue.Full:
            self.dropped += 1

    def check(self, now):
        """Close the file once telemetry has stopped for CAPTURE_GAP."""
        if self._writer is not None and now - self._last > CAPTURE_GAP:
            self._close_file()

    def close(self, timeout=5.0):
        if self._thread is not None:
            try:
                self._queue.put(None, timeout=timeout)
            except queue.Full:
                pass
            self._thread.join(timeout)
            self._thread = None
        self._close_file()

    def _run(self):
        while True:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                self.check(time.monotonic())
                continue
            if item is None:
                return
            self._write(*item)

    def _write(self, now, addr, data):
        if self.error is not None:
            return
        try:
            if self._writer is not None and now - self._last > CAPTURE_GAP:
                self._close_file()
            if self._writer is None:
                self._open()
            self._writer.write(now, addr, data)
            self._last = now
        except OSError as e:
            logging.warning("telemetry capture: %s", e)
            self.error = str(e)
            self._close_file()

    def _open(self):
        os.makedirs(self.folder, exist_ok=True)
        started = self.clock()
        base = time.strftime('%Y%m%d-%H%M%S', time.localtime(started))
        path, n = os.path.join(self.folder, base + SUFFIX), 1
        while os.path.exists(path):
            n += 1
            path = os.path.join(self.folder, '{}-{}{}'.format(base, n, SUFFIX))
        # Room for the new file before it grows: the cap holds while recording
        prune(self.folder, self.cap)
        self._writer = CaptureWriter(path, port=self.port, version=self.version, started=started)
        self.path = path

    def _close_file(self):
        writer, self._writer = self._writer, None
        if writer is None:
            return
        try:
            writer.close()
        except OSError as e:
            logging.warning("telemetry capture: %s", e)
        self.path = None
        prune(self.folder, self.cap)


class NullLeds:
    """Rev LEDs that light nothing: replays run the live path without a wheel."""

    paths = ('', '', '', '', '')

    def available(self):
        return True

    def set_count(self, lit):
        pass

    def set_pattern(self, pattern):
        pass

    def off(self):
        pass


def replay(records, learner=None, start=1000.0, started=None, **options):
    """Feed capture records through Telemetry.handle on the capture's own
    clock (from `start`, so no packet lands on the listener's "never"
    time 0), with a NullLeds; the learner, if given, learns as it would
    live, with its wall clock from `started` (the capture's epoch time,
    so the same capture writes the same database) and its session ended
    at the end. Returns the Telemetry, idle at the end."""
    from .telemetry import Telemetry, IDLE_TIMEOUT
    telemetry = Telemetry(NullLeds(), learner=learner, **options)
    if learner is not None and started is not None:
        learner.clock = lambda now: started + (now - start)
    t = start
    for t_rel, addr, data in records:
        t = start + t_rel
        telemetry.handle(t, data, addr)
    telemetry.check_idle(t + IDLE_TIMEOUT + 1.0)
    if learner is not None:
        learner.save()
    return telemetry
