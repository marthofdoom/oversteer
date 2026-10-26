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

With the wheel's own inputs recorded too, a capture has a second sidecar,
`<file>.wheel.gz` (see `WheelSampler`): gzip of

    b'OVWHL1\n'
    one JSON line: {"capture": <capture file name>, "started": <epoch of the capture's first packet>,
                    "clock": "seconds since the capture's first packet", "fields": "t type code value",
                    "device": <name>, "range": <degrees, sysfs>, "sensitivity": <sysfs>,
                    "axes": {<evdev code>: [min, max, value at the start]}, "oversteer": <version>}
    records: struct '<dHHi' (seconds since the capture's first packet, evdev
             event type, event code, value) for every EV_ABS and EV_KEY event

The times are on the capture records' own clock (the listener's monotonic
one, taken from the first packet), so a steering sample lines up with the
telemetry packet beside it. Read-only: the wheel's node is opened without a
grab and nothing is written to it. The .ovcap format is unchanged.
"""

import glob
import gzip
import json
import logging
import os
import queue
import select
import socket
import struct
import threading
import time
import zlib

MAGIC = b'OVCAP1\n'
RECORD = struct.Struct('<dIH')
SUFFIX = '.ovcap.gz'
WHEEL_MAGIC = b'OVWHL1\n'
WHEEL_RECORD = struct.Struct('<dHHi')
WHEEL_SUFFIX = '.wheel.gz'
EV_KEY, EV_ABS = 0x01, 0x03          # linux/input-event-codes.h
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
        # Readable at once: labelling a session reads when a capture still
        # being written started
        self._file.flush()
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


def sidecars(path):
    """The sidecar files of a capture that exist: its labels and its wheel inputs."""
    return [p for p in (path + '.json', path + WHEEL_SUFFIX) if os.path.exists(p)]


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
        try:
            os.remove(path + WHEEL_SUFFIX)       # the wheel's inputs go with the capture
        except OSError:
            pass
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


class EvdevWheelSource:
    """The wheel's own events from its evdev node: read-only, no grab, a
    descriptor of its own (the app's own reading of the wheel is not
    disturbed). `read(timeout)` returns [(type, code, value)] for the
    EV_ABS and EV_KEY events that arrived; OSError when the device is gone.
    Anything with the same methods stands in for it in tests."""

    def __init__(self, node):
        from evdev import InputDevice
        self.device = InputDevice(node)

    def info(self):
        from evdev import ecodes
        caps = self.device.capabilities(absinfo=True)
        axes = {int(code): [i.min, i.max, i.value] for code, i in caps.get(ecodes.EV_ABS, [])}
        return {'device': self.device.name, 'axes': axes}

    def read(self, timeout):
        ready, _, _ = select.select([self.device.fd], [], [], timeout)
        if not ready:
            return []
        return [(e.type, e.code, e.value) for e in self.device.read() if e.type in (EV_ABS, EV_KEY)]

    def close(self):
        try:
            self.device.close()
        except OSError:
            pass


def wheel_from_device(device):
    """For `Recorder(wheel=...)`: (source, header fields) for a Device, or
    None when its node can't be read. The range and sensitivity come from
    sysfs, read only."""
    if device is None:
        return None
    node = device._input_node()
    if node is None:
        return None
    extra = {}
    for name in ('range', 'sensitivity'):
        try:
            extra[name] = getattr(device, 'get_' + name)()
        except (OSError, ValueError):
            extra[name] = None
    return EvdevWheelSource(node), extra


class WheelSampler:
    """Writes the wheel's events to the capture's `.wheel.gz` sidecar on a
    thread of its own. `origin` is the capture's time zero on `clock` (the
    first packet's monotonic time). `pump()` does one read (tests drive
    it directly); `start()` runs it on the thread until `stop()`."""

    def __init__(self, source, path, origin, header=None, clock=time.monotonic, poll=0.2):
        self.source = source
        self.path = path
        self.origin = origin
        self.clock = clock
        self.poll = poll
        self.events = 0
        self._stop = threading.Event()
        self._thread = None
        self._file = gzip.open(path, 'wb')
        self._file.write(WHEEL_MAGIC)
        meta = {'clock': "seconds since the capture's first packet", 'fields': 't type code value'}
        try:
            meta.update(source.info())
        except (OSError, AttributeError):
            pass
        meta.update(header or {})
        self._file.write(json.dumps(meta).encode() + b'\n')
        self._file.flush()

    def pump(self):
        """One read from the source, written. False when the source is gone."""
        try:
            events = self.source.read(self.poll)
        except OSError as e:
            logging.info("wheel sidecar: the wheel went away: %s", e)
            return False
        now = self.clock() - self.origin
        for etype, code, value in events:
            self._file.write(WHEEL_RECORD.pack(now, etype, code, value))
            self.events += 1
        return True

    def start(self):
        self._thread = threading.Thread(target=self._run, name='wheel-sidecar', daemon=True)
        self._thread.start()

    def _run(self):
        try:
            while not self._stop.is_set():
                if not self.pump():
                    break
        except Exception:
            logging.exception("wheel sidecar")
        self._finish()

    def stop(self, timeout=2.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        else:
            self._finish()

    def _finish(self):
        file, self._file = self._file, None
        if file is not None:
            try:
                file.close()
            except OSError:
                pass
        try:
            self.source.close()
        except OSError:
            pass


def read_wheel(path):
    """(meta dict, iterator of (t, type, code, value)) of a `.wheel.gz`
    sidecar; ends quietly at a truncated tail, ValueError if it is not one."""
    stream = gzip.open(path, 'rb')
    try:
        if stream.readline() != WHEEL_MAGIC:
            raise ValueError("{}: not an Oversteer wheel sidecar".format(path))
        meta = json.loads(stream.readline().decode() or '{}')
    except (OSError, EOFError, ValueError, zlib.error):
        stream.close()
        raise ValueError("{}: not an Oversteer wheel sidecar".format(path))

    def records():
        try:
            while True:
                head = stream.read(WHEEL_RECORD.size)
                if len(head) < WHEEL_RECORD.size:
                    return
                yield WHEEL_RECORD.unpack(head)
        except (EOFError, OSError, zlib.error):
            return
        finally:
            stream.close()

    return meta, records()


class Recorder:
    """Records every datagram the listener receives into `folder`: a file
    per stretch of driving (a new one after CAPTURE_GAP without
    telemetry), named by when it started. The listener only queues each
    packet (`packet()` never blocks: when the queue is full the packet is
    dropped and counted); gzip and the disk are this object's own thread,
    so a slow disk never holds up the rev lights, and recording works
    whether or not the learner has a database. `threaded=False` writes
    inline (tests); `check(now)` then closes a file gone quiet."""

    def __init__(self, folder, cap=DEFAULT_CAP, port=None, version=None, threaded=True, clock=time.time,
                 wheel=None):
        self.folder = folder
        self.wheel = wheel                # () -> (source, header fields) or None: the wheel's inputs, beside each capture
        self._sampler = None
        self._started = None
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
        self._threaded = threaded
        self._closed = False
        if threaded:
            self._thread = threading.Thread(target=self._run, name='telemetry-capture', daemon=True)
            self._thread.start()

    def packet(self, now, addr, data):
        """The listener: one datagram as it arrived, at `now` (monotonic)."""
        if self._closed:
            return                        # the listener read its recorder just before it was switched off
        if not self._threaded:
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
        self._closed = True
        if self._thread is not None:
            try:
                self._queue.put(None, timeout=timeout)
            except queue.Full:
                pass
            self._thread.join(timeout)
            alive, self._thread = self._thread.is_alive(), None
            if alive:
                return                    # still writing: it closes the file itself when it gets to the end
        self._close_file()

    def _run(self):
        while True:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                self.check(time.monotonic())
                continue
            if item is None:
                self._close_file()
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
            first = self._writer.packets == 0
            self._writer.write(now, addr, data)
            if first:
                self._start_wheel(now)
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
        self._started = started
        self.path = path
        logging.info("telemetry capture: recording to %s", os.path.basename(path))

    def _start_wheel(self, origin):
        if self.wheel is None:
            return
        try:
            found = self.wheel()
            if found is None:
                return
            source, extra = found
            header = dict(extra, capture=os.path.basename(self.path), oversteer=self.version, started=self._started)
            self._sampler = WheelSampler(source, self.path + WHEEL_SUFFIX, origin, header)
            self._sampler.start()
        except Exception as e:
            logging.warning("wheel sidecar: %s", e)
            self._sampler = None

    def _close_file(self):
        sampler, self._sampler = self._sampler, None
        if sampler is not None:
            sampler.stop()
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
