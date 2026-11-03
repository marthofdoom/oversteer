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

A capture recorded for FFB tuning has a third sidecar, `<file>.ffb.gz` (ffb_record): the engine's side of the force
chain (the game's raw force, ext_force, the driver's alignment spring and column writes, hands, live friction and
thermal readings) on the same clock, for an offline replay of the whole chain.
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

from . import ffb_record
from .telemetry_formats import OVST4_SIZE, OVST5_SIZE, OVST_MAGIC

MAGIC = b'OVCAP1\n'
RECORD = struct.Struct('<dIH')
SUFFIX = '.ovcap.gz'
WHEEL_MAGIC = b'OVWHL1\n'
WHEEL_RECORD = struct.Struct('<dHHi')
WHEEL_SUFFIX = '.wheel.gz'
FFB_SUFFIX = ffb_record.SUFFIX       # the engine's channels of an FFB tuning capture (ffb_record)
EV_KEY, EV_ABS = 0x01, 0x03          # linux/input-event-codes.h
CAPTURE_GAP = 60.0          # seconds without telemetry that close a capture file: a menu or a pause does not
RECORD_QUEUE = 4096         # packets waiting for the writer (about 34 s at 120 a second, 12 s at ACR's 333, 6 s at RBR's 720)
DEFAULT_CAP = 1 << 30       # bytes kept in the folder; the oldest unlabelled captures go first

# A format that sends faster than a capture needs is thinned when saved (the listener still gets every packet):
# packet length -> (max saved packets a second, the packet's own steps a second, struct of its head:
# steps, stage id, ?, clock, ?, distance to the end). Richard Burns Rally's NGP packet (664 bytes) comes at 720 a
# second, in bursts that share one arrival time, so its own step counter is the clock. The packets where
# something changes are always kept (CaptureWriter._keep); the file format is unchanged.
THIN = {664: (60.0, 720.0, struct.Struct('<Iiffff'))}
THIN_CLOCK_JUMP = 0.5       # s the stage clock may jump between two packets before both are kept (a penalty, a restart)
THIN_DISTANCE_JUMP = 5.0    # m the distance to the end may jump
THIN_FROZEN = 0.1           # s of the packets' own clock the stage clock must stand still to count as stopped (it steps in threes)
# Oversteer's bridge sends a packet per physics step from OVST version 5 on (333 a second in ACR), of a length
# that grows with later versions: those are thinned to OVST_THIN_RATE a second on the arrival clock (the bridge
# sends each step as it happens). Always kept, as for THIN: the first packet and every one where the session's
# state changes: the car, track, game or pit/auto-shift flags, the laps or session type, the physics step going
# back or leaping, the stage clock starting, stopping or jumping, the distance jumping. Versions 1 to 4 (60 a
# second) are saved as they come.
OVST_THIN_RATE = 60.0
# "Record for FFB tuning": a bounded exception. While it is on, THIN and OVST v5+ packets are saved at the full rate
# in files whose header says 'ffb_tuning': true; those files are never pruned (they count toward the folder's size
# and are not counted toward the folder's cap either: they are listed apart, delete them by hand). The exception
# ends by itself after FFB_MAX_SECONDS of driving (the gaps between packets, each at most CAPTURE_GAP, so a menu
# does not use it up) or FFB_MAX_BYTES written to disk (compressed), and thinning resumes in a new file.
FFB_MAX_SECONDS = 60 * 60.0
FFB_MAX_BYTES = 600 * 1000 * 1000
OVST_THIN_STEP_LEAP = 1000  # physics steps the packet id may leap forward before both packets are kept (a new session)


def rbr_meta(first):
    """Header keys for a file whose first packet is Richard Burns Rally's (664 bytes): the car RSF has put in the
    slot the packet names, `rbr_car` (key, id, name). The car is not in the packets, and a replay later reads
    today's Cars.ini: the header keeps the one that was driven. None for any other packet."""
    if first is None or len(first) != 664:
        return None
    try:
        from . import rbr_install
        car = rbr_install.car_in_slot(struct.unpack_from('<i', first, 56)[0])
    except Exception:
        return None
    return {'rbr_car': {k: car[k] for k in ('key', 'id', 'name')}} if car else None


def _ipv4(address):
    try:
        return struct.unpack('!I', socket.inet_aton(address))[0]
    except (OSError, TypeError):
        return 0


class CaptureWriter:
    """Writes one capture file. `write(now, addr, data)` takes any clock in
    seconds (the listener's monotonic one); times are stored from the first
    packet on."""

    def __init__(self, path, port=None, note='', version=None, started=None, extra=None,
                 full_rate=False):
        self.path = path
        self.full_rate = full_rate        # save every packet, THIN formats and OVST v5+ included
        self._file = gzip.open(path, 'wb')
        self._file.write(MAGIC)
        meta = {'started': time.time() if started is None else started, 'port': port, 'oversteer': version,
                'note': note}
        meta.update(extra or {})                     # more keys of the header line: a reader takes the ones it knows
        self._file.write(json.dumps(meta).encode() + b'\n')
        # Readable at once: labelling a session reads when a capture still
        # being written started
        self._file.flush()
        self._first = None
        self.packets = 0
        self.thinned = 0                  # packets not saved (THIN)
        self._seen = None                 # the last packet's (steps, stage, clock, distance to the end, frozen)
        self._moved = None                # steps at which the stage clock last changed
        self._kept = None                 # steps of the last packet saved
        self._held = None                 # the last packet not saved: written on close, the session's last stays
        self._ovst = None                 # OVST v5+: the last packet's (step, state, clock, distance, frozen)
        self._ovst_due = None             # arrival time from which the next OVST v5+ packet is saved
        self._ovst_moved = None           # arrival time at which its stage clock last changed

    def write(self, now, addr, data):
        if self.full_rate:
            pass
        elif len(data) in THIN:
            if not self._keep(now, addr, data):
                return
        elif len(data) >= OVST5_SIZE and data[:4] == OVST_MAGIC and data[4] >= 5:
            if not self._keep_ovst(now, addr, data):
                return
        self._put(now, addr, data)

    def _put(self, now, addr, data):
        if self._first is None:
            self._first = now
        self._file.write(RECORD.pack(now - self._first, _ipv4(addr[0]), len(data)))
        self._file.write(data)
        self.packets += 1

    def _keep(self, now, addr, data):
        """Whether a packet of a THIN format is saved: one in every 1/rate of its own steps, and every one where
        a lifecycle field changes: the steps going back, the stage, the clock starting or stopping (0 against
        above, standing still against running, a jump), the distance to the end jumping; the first packet."""
        rate, steps_per_s, head = THIN[len(data)]
        steps, stage, _p, clock, _d, to_end = head.unpack_from(data)
        prev = self._seen
        if prev is None or clock != prev[2] or steps < prev[0]:
            self._moved = steps
        frozen = clock > 0 and steps - self._moved >= THIN_FROZEN * steps_per_s
        keep = prev is None or self._kept is None or steps < self._kept or steps - self._kept >= steps_per_s / rate
        if prev is not None and not keep:
            keep = (steps < prev[0] or stage != prev[1] or (clock == 0) != (prev[2] == 0) or frozen != prev[4]
                    or abs(clock - prev[2]) > THIN_CLOCK_JUMP or abs(to_end - prev[3]) > THIN_DISTANCE_JUMP)
        self._seen = (steps, stage, clock, to_end, frozen)
        if keep:
            self._held = None
            self._kept = steps
            return True
        self._held = (now, addr, data)
        self.thinned += 1
        return False

    def _keep_ovst(self, now, addr, data):
        """Whether an OVST packet of version 5 or later is saved: OVST_THIN_RATE a second on average, and every
        one where the session's state changes (OVST_THIN_RATE's note)."""
        # The struct's offsets (oversteer-shm-bridge.c): packet_id at 328, stage_clock 324, distance 292, laps and
        # session_type 304, car and track names 32, game and flags2 96
        step = struct.unpack_from('<i', data, OVST4_SIZE)[0]
        clock, distance = struct.unpack_from('<f', data, OVST4_SIZE - 4)[0], struct.unpack_from('<f', data, 292)[0]
        state = (data[32:98], data[304:312], 0 if clock != clock else 1 if clock == 0 else 2)
        prev = self._ovst
        if prev is None or not (clock == prev[2] or (clock != clock and prev[2] != prev[2])):
            self._ovst_moved = now
        frozen = clock > 0 and now - self._ovst_moved >= THIN_FROZEN
        period, due = 1.0 / OVST_THIN_RATE, self._ovst_due
        on_time = due is None or now >= due
        keep = prev is None or on_time
        if not keep:
            keep = (state != prev[1] or frozen != prev[4] or step < prev[0] or step - prev[0] > OVST_THIN_STEP_LEAP
                    or abs(clock - prev[2]) > THIN_CLOCK_JUMP or abs(distance - prev[3]) > THIN_DISTANCE_JUMP)
        self._ovst = (step, state, clock, distance, frozen)
        if on_time:
            # One kept late does not push the next one back; one more than a period late starts the count again.
            # A packet kept for a change in between leaves the count alone.
            self._ovst_due = due + period if due is not None and now - due < period else now + period
        if keep:
            self._held = None
            return True
        self._held = (now, addr, data)
        self.thinned += 1
        return False

    def disk_bytes(self):
        """Compressed bytes that have reached the file so far (the gzip stream buffers a little)."""
        return self._file.fileobj.tell() if self._file is not None else 0

    def close(self):
        if self._file is not None:
            if self._held is not None:
                self._put(*self._held)
                self._held = None
                self.thinned -= 1                    # (saved after all)
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


def _wheel_size(path):
    total = 0
    for suffix in (WHEEL_SUFFIX, FFB_SUFFIX):
        try:
            total += os.path.getsize(path + suffix)
        except OSError:
            pass
    return total


def ffb_tuning(path):
    """Whether a capture's header says it was recorded for FFB tuning: True or False when the header was read,
    None when it could not be (being created, truncated, unreadable). A file that is not gzip at all, or
    whose first line is not a capture's, is a definite False."""
    try:
        with gzip.open(path, 'rb') as stream:
            first = stream.readline()
            if first and first != MAGIC:
                return False
            line = stream.readline().decode()
            if not first or not line:
                return None
            return bool(json.loads(line).get('ffb_tuning'))
    except gzip.BadGzipFile:
        return False
    except (OSError, EOFError, ValueError, zlib.error):
        return None


_TUNING = {}          # path -> whether its header says ffb_tuning: only definite answers (a header never changes)
_UNKNOWN = set()      # paths whose header could not be read at the last listing: never pruned, asked again


def _is_tuning(path):
    found = _TUNING.get(path)
    if found is None:
        found = ffb_tuning(path)
        if found is None:
            if path not in _UNKNOWN:
                logging.warning("capture %s: header unreadable, kept and checked again", path)
            _UNKNOWN.add(path)
            return False
        _UNKNOWN.discard(path)
        _TUNING[path] = found
    return found


def _forget_gone(paths):
    """Drop the cache entries of captures no longer in the folder."""
    for path in [p for p in list(_TUNING) + list(_UNKNOWN) if p not in paths and not os.path.exists(p)]:
        _TUNING.pop(path, None)
        _UNKNOWN.discard(path)


def tuning_captures(folder):
    """[(path, bytes)] of the captures recorded for FFB tuning, oldest first. They are never pruned and do
    not count toward the folder's cap."""
    return [(p, s) for p, s in _scan(folder) if _is_tuning(p)]


def list_captures(folder):
    """[(path, bytes, labelled)] of the captures in `folder`, oldest
    first (by when they were last written). The bytes include the wheel
    sidecar, which goes with the capture. Labelled: a label sidecar, or
    a label sidecar. Captures recorded for FFB tuning are not in it (see tuning_captures)."""
    found = []
    for path in glob.glob(os.path.join(glob.escape(folder), '*' + SUFFIX)):
        try:
            found.append((os.path.getmtime(path), path, os.path.getsize(path) + _wheel_size(path),
                          os.path.exists(path + '.json')))
        except OSError:
            continue                     # deleted meanwhile
    _forget_gone({f[1] for f in found})
    return [(path, size, labelled) for _mtime, path, size, labelled in sorted(found) if not _is_tuning(path)]


def _scan(folder):
    found = []
    for path in glob.glob(os.path.join(glob.escape(folder), '*' + SUFFIX)):
        try:
            found.append((os.path.getmtime(path), path, os.path.getsize(path) + _wheel_size(path)))
        except OSError:
            continue
    return [(path, size) for _mtime, path, size in sorted(found)]


def sidecars(path):
    """The sidecar files of a capture that exist: its labels and its wheel inputs."""
    return [p for p in (path + '.json', path + WHEEL_SUFFIX, path + FFB_SUFFIX) if os.path.exists(p)]


def capture_summary(folder):
    """(captures, bytes, labelled) in `folder`."""
    captures = list_captures(folder)
    return len(captures), sum(size for _p, size, _l in captures), sum(1 for _p, _s, labelled in captures if labelled)


def prune(folder, cap, keep=None):
    """Delete the oldest unlabelled captures until the folder holds at
    most `cap` bytes; labelled ones and `keep` (the file being written, or
    a collection of them) stay. The paths deleted."""
    keep = {keep} if isinstance(keep, str) else set(keep or ())
    captures = list_captures(folder)
    total = sum(size for _p, size, _l in captures)
    deleted = []
    for path, size, labelled in captures:
        if total <= cap:
            break
        if labelled or path in keep or path in _UNKNOWN:       # (a header that can't be read: not ours to delete)
            continue
        try:
            os.remove(path)
        except OSError:
            continue
        for suffix in (WHEEL_SUFFIX, FFB_SUFFIX):
            try:
                os.remove(path + suffix)         # the wheel's inputs and the engine's channels go with the capture
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
        try:
            self._file.write(WHEEL_MAGIC)
            meta = {'clock': "seconds since the capture's first packet", 'fields': 't type code value'}
            try:
                meta.update(source.info())
            except (OSError, AttributeError):
                pass
            meta.update(header or {})
            self._file.write(json.dumps(meta).encode() + b'\n')
            self._file.flush()
        except BaseException:
            self._file.close()           # the caller still owns the source
            raise

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
    inline (tests); `check(now)` then closes a file gone quiet.

    A file holds one UDP port's packets (its header names the port). The
    listener's Auto mode records every port while no game is locked, so a
    packet on another port than the open file's goes to a file of its own
    for that port (`_side`, one per port at most); the same cap and
    pruning cover them all. The wheel's sidecar goes with the main file:
    `main(port)` (the listener, when a game locks on `port`) makes that
    port's file the main one (the previous main file, with its sidecar,
    is closed) and starts the sidecar there. Only files that never had a
    sidecar are ever promoted, so a sidecar is never written twice."""

    def __init__(self, folder, cap=DEFAULT_CAP, port=None, version=None, threaded=True, clock=time.time,
                 wheel=None, ffb_limits=(FFB_MAX_SECONDS, FFB_MAX_BYTES)):
        self.folder = folder
        self.wheel = wheel                # () -> (source, header fields) or None: the wheel's inputs, beside each capture
        self._sampler = None
        self._ffb_log = None              # ffb_record.FfbSampler of an FFB tuning capture
        self._started = None
        self.cap = cap
        self.port = port
        self.version = version
        self.clock = clock
        self.dropped = 0
        self.error = None                 # why recording stopped (a full disk, no permission)
        self.path = None                  # the file being written
        self.ffb_tuning = False           # "Record for FFB tuning" is on (set_ffb_tuning)
        self.ffb_stopped = None           # 'time' or 'size': why the exception ended by itself
        self._ffb_limits = ffb_limits     # (seconds, bytes)
        self._ffb_pending = None          # a switch the writer has yet to apply (set_ffb_tuning)
        self._ffb_prev = None             # arrival time of the last packet in the mode
        self._ffb_time = 0.0              # driving seconds in the mode
        self._ffb_bytes = 0               # compressed bytes written in the mode
        self._writer = None
        self._file_port = None            # the port in the open file's header
        self._last = 0.0
        self._side = {}                   # port -> [CaptureWriter, path, last packet, started]: other ports' files
        self._queue = queue.Queue(maxsize=RECORD_QUEUE)
        self._thread = None
        self._threaded = threaded
        self._closed = False
        if threaded:
            self._thread = threading.Thread(target=self._run, name='telemetry-capture', daemon=True)
            self._thread.start()

    def packet(self, now, addr, data, port=None):
        """The listener: one datagram as it arrived, at `now` (monotonic),
        on UDP `port` (None: `self.port`)."""
        if self._closed:
            return                        # the listener read its recorder just before it was switched off
        if not self._threaded:
            self._write(now, addr, data, port)
            return
        try:
            self._queue.put_nowait((now, addr, data, port))
        except queue.Full:
            self.dropped += 1

    def main(self, port, now):
        """The listener: a game locked on UDP `port` at `now`; its file is
        the main one (with the wheel's sidecar) from here on."""
        if self._closed:
            return
        if not self._threaded:
            self._main(port, now)
            return
        try:
            self._queue.put_nowait(('main', port, now))
        except queue.Full:
            self.dropped += 1

    def set_ffb_tuning(self, on):
        """Full-rate saving in files of their own, until switched off or its
        limits are reached (`ffb_stopped`). Takes effect in a new file.
        Never blocks: the writer applies it with its next packet."""
        if self._closed:
            return
        if on:
            self.ffb_stopped = None       # now, not when the writer gets to it: the UI reads it meanwhile
        self._ffb_pending = bool(on)
        if not self._threaded:
            self._ffb_apply()
            return
        try:
            self._queue.put_nowait(('ffb',))
        except queue.Full:
            pass                          # busy writing: it looks at the flag with every packet

    def _ffb_apply(self):
        on, self._ffb_pending = self._ffb_pending, None
        if on is not None:
            self._ffb(on)

    def _ffb(self, on):
        if on == self.ffb_tuning:
            return
        try:
            self._close_all()             # a file is wholly full-rate or wholly thinned, as its header says
        except OSError as e:
            logging.warning("telemetry capture: %s", e)
        self.ffb_tuning = bool(on)
        self._ffb_prev, self._ffb_time, self._ffb_bytes = None, 0.0, 0

    def _ffb_stop(self, reason):
        logging.info("telemetry capture: FFB tuning recording ended (%s limit)", reason)
        self._ffb(False)
        self.ffb_stopped = reason

    def _ffb_advance(self, now):
        """A packet arrives: the driving time of the mode, before any file is opened for it."""
        if not self.ffb_tuning:
            return
        if self._ffb_prev is not None:
            self._ffb_time += min(max(now - self._ffb_prev, 0.0), CAPTURE_GAP)
        self._ffb_prev = now
        if self._ffb_time >= self._ffb_limits[0]:
            self._ffb_stop('time')

    def _ffb_check(self, saved):
        if self.ffb_tuning:
            self._ffb_bytes += saved
            if self._ffb_bytes >= self._ffb_limits[1]:
                self._ffb_stop('size')

    def check(self, now):
        """Close the files once telemetry has stopped for CAPTURE_GAP."""
        if self._writer is not None and now - self._last > CAPTURE_GAP:
            self._close_file()
        for port in [p for p, side in self._side.items() if now - side[2] > CAPTURE_GAP]:
            self._close_side(port)

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
        self._close_all()

    def _run(self):
        while True:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                self._ffb_apply()
                self.check(time.monotonic())
                continue
            if item is None:
                self._close_all()
                return
            if item[0] == 'main':
                self._main(item[1], item[2])
                continue
            if item[0] == 'ffb':
                self._ffb_apply()
                continue
            self._write(*item)

    def _write(self, now, addr, data, port=None):
        if self.error is not None:
            return
        if self._ffb_pending is not None:
            self._ffb_apply()
        self._ffb_advance(now)
        if port is None:
            port = self.port
        try:
            if self._writer is not None and now - self._last > CAPTURE_GAP:
                self._close_file()
            if self._writer is not None and port != self._file_port:
                self._write_side(now, addr, data, port)
                return
            if self._writer is None and port in self._side:
                self._promote(port, now)          # that port's file goes on as the main one
            if self._writer is None:
                self._open(port, data)
            first = self._writer.packets == 0
            writer = self._writer
            before = writer.disk_bytes()
            writer.write(now, addr, data)
            if first:
                self._start_wheel(now)
            self._last = now
            self._ffb_check(writer.disk_bytes() - before)
        except OSError as e:
            logging.warning("telemetry capture: %s", e)
            self.error = str(e)
            self._close_all()

    def _write_side(self, now, addr, data, port):
        side = self._side.get(port)
        if side is not None and now - side[2] > CAPTURE_GAP:
            self._close_side(port)
            side = None
        if side is None:
            writer, path, started = self._new_file(port, data)
            side = self._side[port] = [writer, path, now, started]
        before = side[0].disk_bytes()
        side[0].write(now, addr, data)
        side[2] = now
        self._ffb_check(side[0].disk_bytes() - before)

    def _main(self, port, now):
        if self.error is not None or (self._writer is not None and self._file_port == port):
            return
        try:
            if self._writer is not None:
                # The old main file ends here, with its sidecar. Not kept as a
                # side file: while another port is locked its packets never
                # reach the recorder, and promoting it again later would start
                # a new sidecar on the same path, truncating the first one.
                self._close_file()
            if port in self._side:
                self._promote(port, now)
            # else: the port's next packet opens the main file, with its sidecar
        except OSError as e:
            logging.warning("telemetry capture: %s", e)
            self.error = str(e)
            self._close_all()

    def _promote(self, port, now):
        """A side file becomes the main one (no main open): with its own
        start time, and the wheel's sidecar from here on."""
        writer, path, last, started = self._side.pop(port)
        if now - last > CAPTURE_GAP:
            self._side[port] = [writer, path, last, started]
            self._close_side(port)
            return
        self._writer, self.path, self._file_port, self._last, self._started = writer, path, port, last, started
        self._start_wheel(writer._first if writer._first is not None else now)

    def _open_paths(self):
        return [p for p in [self.path] + [side[1] for side in self._side.values()] if p]

    def _new_file(self, port, first=None):
        extra = rbr_meta(first)
        if self.ffb_tuning:
            extra = dict(extra or {}, ffb_tuning=True)
        os.makedirs(self.folder, exist_ok=True)
        started = self.clock()
        base = time.strftime('%Y%m%d-%H%M%S', time.localtime(started))
        path, n = os.path.join(self.folder, base + SUFFIX), 1
        while os.path.exists(path):
            n += 1
            path = os.path.join(self.folder, '{}-{}{}'.format(base, n, SUFFIX))
        # Room for the new file before it grows: the cap holds while recording
        prune(self.folder, self.cap, keep=self._open_paths())
        writer = CaptureWriter(path, port=port, version=self.version, started=started, extra=extra,
                               full_rate=self.ffb_tuning)
        logging.info("telemetry capture: recording UDP %s to %s", port, os.path.basename(path))
        return writer, path, started

    def _open(self, port=None, first=None):
        self._writer, self.path, self._started = self._new_file(port, first)
        self._file_port = port

    def _start_ffb_log(self, origin):
        """An FFB tuning capture also keeps the engine's side (ffb_record), on the capture's clock."""
        if not self.ffb_tuning or self._ffb_log is not None:
            return
        try:
            self._ffb_log = ffb_record.sampler(self.path + FFB_SUFFIX, origin, {
                'capture': os.path.basename(self.path), 'oversteer': self.version, 'started': self._started})
            self._ffb_log.start()
        except OSError as e:
            logging.warning("ffb sidecar: %s", e)
            self._ffb_log = None

    def _start_wheel(self, origin):
        self._start_ffb_log(origin)
        if self.wheel is None:
            return
        source = None
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
            sampler, self._sampler = self._sampler, None
            if sampler is not None:
                sampler._finish()            # closes its file and the source
            elif source is not None:
                try:
                    source.close()           # the sampler never took it over
                except Exception:
                    pass

    def _close_file(self):
        sampler, self._sampler = self._sampler, None
        if sampler is not None:
            sampler.stop()
        ffb_log, self._ffb_log = self._ffb_log, None
        if ffb_log is not None:
            ffb_log.stop()
        writer, self._writer = self._writer, None
        if writer is None:
            return
        try:
            writer.close()
        except OSError as e:
            logging.warning("telemetry capture: %s", e)
        self.path = None
        prune(self.folder, self.cap, keep=self._open_paths())

    def _close_side(self, port):
        writer = self._side.pop(port)[0]
        try:
            writer.close()
        except OSError as e:
            logging.warning("telemetry capture: %s", e)
        prune(self.folder, self.cap, keep=self._open_paths())

    def _close_all(self):
        self._close_file()
        for port in list(self._side):
            self._close_side(port)


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


def replay(records, learner=None, start=1000.0, started=None, recorded_car=None, **options):
    """Feed capture records through Telemetry.handle on the capture's own
    clock (from `start`, so no packet lands on the listener's "never"
    time 0), with a NullLeds; the learner, if given, learns as it would
    live, with its wall clock from `started` (the capture's epoch time,
    so the same capture writes the same database) and its session ended
    at the end. `recorded_car`: the capture header's `rbr_car`, which names a Richard Burns Rally car the way it
    was when recorded, not as today's Cars.ini has it. Returns the Telemetry, idle at the end."""
    from .telemetry import Telemetry, IDLE_TIMEOUT
    if isinstance(recorded_car, dict) and recorded_car.get('key'):
        options['rbr_car'] = dict({'drivetrain': None, 'gears': None}, **recorded_car)
    return _replay(records, learner, start, started, options, Telemetry, IDLE_TIMEOUT)


def _replay(records, learner, start, started, options, Telemetry, IDLE_TIMEOUT):
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
