"""The drive log: everything telemetry teaches that goes to disk.

The listener thread must never wait (docs/telemetry-coaching.md, section
4), so it only does constant-time work per packet and hands the rest to
the drive-log thread through a queue: each event is a function and its
arguments, run there with the database's only write connection. Writes
are batched into a transaction committed at most every few seconds and
whenever an event asks for it (the end of a session or a run).

Without a thread (tests, replays) events run at once, in the caller's
thread, and are committed as they go.
"""

import logging
import queue
import threading
import time

from .telemetry_store import open_store

QUEUE_SIZE = 8192
COMMIT_EVERY = 5.0               # seconds a batch of writes may wait
TICK_EVERY = 1.0                 # seconds between tick() calls on the thread


class DriveLog:
    """Owns the writer (a telemetry_store.Store) and, when `threaded`, the
    thread that uses it. `post(fn, *args)` never blocks: when the queue is
    full the event is dropped and counted in `dropped`. An event function
    returns True to have its writes committed at once. `ticks` are called
    about once a second on the thread (publishing snapshots)."""

    def __init__(self, path, threaded=True):
        self.path = path
        self.store = open_store(path)
        self.threaded = threaded
        self.dropped = 0
        self.ticks = []
        self._queue = queue.Queue(maxsize=QUEUE_SIZE)
        self._thread = None
        self._commit_at = time.monotonic()
        if threaded:
            self._thread = threading.Thread(target=self._run, name='drive-log', daemon=True)
            self._thread.start()

    def post(self, fn, *args):
        if self._thread is None:
            self._handle(fn, args)
            self.store.commit()
            return True
        try:
            self._queue.put_nowait((fn, args))
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def call(self, fn, *args, timeout=5.0):
        """Run `fn(*args)` on the drive-log thread and wait for its result
        (for the few writes the GUI asks for: a rename, a forget). None
        if it did not finish in time."""
        if self._thread is None:
            result = fn(*args)
            self.store.commit()
            return result
        done = threading.Event()
        box = []

        def run():
            box.append(fn(*args))
            done.set()
            return True
        try:
            self._queue.put((run, ()), timeout=timeout)
        except queue.Full:
            return None
        done.wait(timeout)
        return box[0] if box else None

    def sync(self, timeout=5.0):
        """Wait until everything posted so far is written and committed."""
        if self._thread is None:
            self.store.commit()
            return True
        return self.call(lambda: True, timeout=timeout) is True

    def close(self, timeout=5.0):
        if self._thread is not None:
            self.sync(timeout)
            try:
                self._queue.put((None, ()), timeout=timeout)
            except queue.Full:
                pass
            self._thread.join(timeout)
            self._thread = None
        self.store.commit()

    def _handle(self, fn, args):
        self.store.begin()
        try:
            return fn(*args)
        except Exception:
            logging.exception("drive log")
            return False

    def _run(self):
        tick_at = time.monotonic() + TICK_EVERY
        while True:
            try:
                fn, args = self._queue.get(timeout=min(TICK_EVERY, COMMIT_EVERY))
            except queue.Empty:
                fn = args = None
            now = time.monotonic()
            if fn is None and args is not None:
                break                                   # close()
            urgent = fn is not None and self._handle(fn, args)
            if urgent or now - self._commit_at >= COMMIT_EVERY:
                self._commit()
            if now >= tick_at:
                tick_at = now + TICK_EVERY
                for tick in self.ticks:
                    try:
                        tick()
                    except Exception:
                        logging.exception("drive log tick")
        self._commit()

    def _commit(self):
        try:
            self.store.commit()
        except Exception as e:
            logging.warning("drive log: can't write the telemetry database: %s", e)
            try:
                self.store.rollback()
            except Exception:
                pass
        self._commit_at = time.monotonic()
