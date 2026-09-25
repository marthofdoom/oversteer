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
record.
"""

import gzip
import json
import socket
import struct
import time
import zlib

MAGIC = b'OVCAP1\n'
RECORD = struct.Struct('<dIH')


def _ipv4(address):
    try:
        return struct.unpack('!I', socket.inet_aton(address))[0]
    except (OSError, TypeError):
        return 0


class CaptureWriter:
    """Writes one capture file. `write(now, addr, data)` takes any clock in
    seconds (the listener's monotonic one); times are stored from the first
    packet on."""

    def __init__(self, path, port=None, note='', version=None):
        self.path = path
        self._file = gzip.open(path, 'wb')
        self._file.write(MAGIC)
        meta = {'started': time.time(), 'port': port, 'oversteer': version, 'note': note}
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


def replay(records, learner=None, start=1000.0, **options):
    """Feed capture records through Telemetry.handle on the capture's own
    clock (from `start`, so no packet lands on the listener's "never"
    time 0), with a NullLeds; the learner, if given, learns as it would
    live. Returns the Telemetry, idle at the end."""
    from .telemetry import Telemetry, IDLE_TIMEOUT
    telemetry = Telemetry(NullLeds(), learner=learner, **options)
    t = start
    for t_rel, addr, data in records:
        t = start + t_rel
        telemetry.handle(t, data, addr)
    telemetry.check_idle(t + IDLE_TIMEOUT + 1.0)
    return telemetry
