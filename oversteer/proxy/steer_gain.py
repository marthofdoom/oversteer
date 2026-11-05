"""The steering gain handed to a running proxy: Forza's "linear steering at speed" option.

Countersteer's engine works out a factor from the car's speed and writes it to a small file; the daemon scales the
wheel's steering axis about its centre by it, so the wheel's position to front-wheel angle stays the same at every
speed. The file lives beside the calibration files (`calibration.CALIBRATION_DIR`: writable from the Flatpak, read
by the root daemon) and is a fixed 16 bytes, `struct '<dd'`: the gain, then `time.monotonic()` of the write
(CLOCK_MONOTONIC is one clock for the sandbox and the host, they share the kernel). The daemon reads it with pread on a
file it holds open, about a microsecond per event: a map of a file another user can truncate could end the daemon, and with
it the virtual device a running game holds, on a bus error. Whatever is wrong with the file (missing, another
size or owner, not a number, outside [1, GAIN_MAX], older than STALE_S: the engine died or stopped writing) is a
gain of 1.0, the wheel as it is. A read is never torn into a wrong value: it is taken twice and used only if both
agree, else the last good gain stands for that event.

Imports nothing outside the standard library and `calibration` (the daemon's copy of the package is a subset)."""

import logging
import math
import mmap
import os
import stat
import struct
import time

from . import calibration

PATH = os.path.join(calibration.CALIBRATION_DIR, 'steer-gain')
LAYOUT = struct.Struct('<dd')            # gain, time.monotonic() of the write
GAIN_MAX = 5.0                           # = ffb_forza.STEER_GAIN_MAX (not imported: the daemon's copy has no ffb_forza)
STALE_S = 0.25                           # a gain older than this is not trusted
RECHECK_S = 1.0                          # how often the reader looks again for the file (created, replaced, removed)
AHEAD_S = 1.0                            # a write stamped further ahead than this is not from our clock


def scale_steer(value, gain, lo, hi):
    """The steering axis `value` scaled about the centre of [lo, hi] by `gain` and held to the range. Exactly `value`
    when the gain is 1.0."""
    if gain == 1.0:
        return value
    centre = (lo + hi) / 2.0
    return min(max(int(round(centre + (value - centre) * gain)), lo), hi)


class GainWriter:
    """The engine's end: `write(gain)` stores it with the time (an mmap store, no fsync, never blocks), `remove()`
    writes 1.0 and deletes the file. Neither raises; a file that can't be made is tried again once a second."""

    def __init__(self, path=None, clock=time.monotonic):
        self.path = path or PATH
        self._clock = clock
        self._fd = None
        self._map = None
        self._id = None                  # (st_dev, st_ino) of the file we hold
        self._next_try = 0.0
        self._next_check = 0.0

    def _open(self, now):
        if now < self._next_try:
            return False
        self._next_try = now + RECHECK_S
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
        except OSError:
            return False
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise OSError("not ours")
            if info.st_size != LAYOUT.size:
                os.ftruncate(fd, LAYOUT.size)
            self._map = mmap.mmap(fd, LAYOUT.size)
        except (OSError, ValueError):
            os.close(fd)
            return False
        self._fd, self._id = fd, (info.st_dev, info.st_ino)
        self._next_check = now + RECHECK_S
        return True

    def _close(self):
        mm, fd, self._map, self._fd, self._id = self._map, self._fd, None, None, None
        if mm is not None:
            try:
                mm.close()
            except (BufferError, ValueError):
                pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def write(self, gain):
        """True when the gain was stored."""
        try:
            now = self._clock()
            if self._map is not None and now >= self._next_check:
                self._next_check = now + RECHECK_S
                try:
                    info = os.stat(self.path)
                    gone = (info.st_dev, info.st_ino) != self._id
                except OSError:
                    gone = True          # removed under us: make it again
                if gone:
                    self._close()
            if self._map is None and not self._open(now):
                return False
            LAYOUT.pack_into(self._map, 0, float(gain), now)
            return True
        except (OSError, ValueError, struct.error, BufferError):
            self._close()
            return False

    def remove(self):
        """1.0 first (a reader holding the file sees the wheel as it is), then the file goes."""
        if self._map is not None:
            self.write(1.0)
        self._close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


class GainReader:
    """The daemon's end: `read()` is the gain to put on the steering now, 1.0 unless a fresh, valid one is there.
    `uids`: a callable giving the user ids whose file may be trusted (`calibration.allowed_uids`: root and the
    installing user), None to trust any."""

    def __init__(self, path=None, uids=None, clock=time.monotonic):
        self.path = path or PATH
        self._uids = uids
        self._clock = clock
        self._fd = None
        self._id = None
        self._refused = None             # (st_dev, st_ino, st_size) of a file turned down, logged once
        self._next_check = 0.0
        self._last = 1.0

    def _close(self):
        fd, self._fd, self._id = self._fd, None, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _check(self, now):
        """Look for the file again: made, replaced, removed or changed in size."""
        self._next_check = now + RECHECK_S
        try:
            info = os.stat(self.path, follow_symlinks=False)
        except OSError:
            self._close()
            self._last = 1.0
            return
        ident = (info.st_dev, info.st_ino)
        if self._fd is not None and ident == self._id and info.st_size == LAYOUT.size:
            return
        self._close()
        self._last = 1.0
        mark = ident + (info.st_size,)
        if mark == self._refused:
            return
        self._refused = mark
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            return
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size != LAYOUT.size:
                return
            if self._uids is not None and info.st_uid not in self._uids():
                logging.warning("steering gain file %s ignored: owned by uid %d", self.path, info.st_uid)
                return
            self._fd, fd = fd, None
            self._id = (info.st_dev, info.st_ino)
            self._refused = None
        except OSError:
            pass
        finally:
            if fd is not None:
                os.close(fd)

    @property
    def active(self):
        """A gain file is there (as of the last look): the caller keeps its loop quick enough to follow it."""
        return self._fd is not None

    def read(self):
        try:
            now = self._clock()
            if now >= self._next_check:
                self._check(now)
            fd = self._fd
            if fd is None:
                return 1.0
            first = os.pread(fd, LAYOUT.size, 0)
            if len(first) != LAYOUT.size:
                self._last = 1.0
                return 1.0
            if first != os.pread(fd, LAYOUT.size, 0):
                return self._last        # torn by a write in progress: the last good one for this event
            gain, stamp = LAYOUT.unpack(first)
            if not (math.isfinite(gain) and math.isfinite(stamp)) or not 1.0 <= gain <= GAIN_MAX \
                    or now - stamp > STALE_S or stamp - now > AHEAD_S:
                self._last = 1.0
                return 1.0
            self._last = gain
            return gain
        except Exception:
            return 1.0
