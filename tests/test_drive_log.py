"""The drive log: writes leave the listener for their own thread, and what
the thread writes is what an inline replay writes."""
import threading
import time

from oversteer import drive_log
from oversteer.drive_log import DriveLog
from oversteer.shift_learner import ShiftLearner
from tests.sim import drive, exits


def test_events_run_on_the_thread_in_order(tmp_path):
    log = DriveLog(str(tmp_path / 'telemetry.db'))
    seen = []
    for i in range(100):
        log.post(lambda i=i: seen.append((i, threading.current_thread().name)))
    assert log.sync()
    assert [i for i, _ in seen] == list(range(100)) and {name for _, name in seen} == {'drive-log'}
    assert log.call(lambda: 42) == 42
    log.close()


def test_a_full_queue_drops_and_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_log, 'QUEUE_SIZE', 4)
    log = DriveLog(str(tmp_path / 'telemetry.db'))
    gate = threading.Event()
    log.post(gate.wait)                          # holds the thread
    time.sleep(0.05)
    posted = sum(log.post(lambda: None) for _ in range(10))
    assert posted == 4 and log.dropped == 6      # the listener never waits
    gate.set()
    assert log.sync()
    log.close()


def test_threaded_learner_writes_what_an_inline_one_does(tmp_path):
    rows = []
    for name, threaded in (('inline.db', False), ('threaded.db', True)):
        learner = ShiftLearner(str(tmp_path / name), threaded=threaded)
        learner.clock = lambda now: 1e9 + now
        exits(learner)
        drive(learner, shift_at=6000, runs=2)
        learner.save()
        history = learner.history('test-car')
        assert learner.history_changed >= 1
        rows.append(([(h['started'], h['shifts'], h['methods']) for h in history],
                     learner.db.execute('SELECT model FROM cars').fetchall(),
                     learner.db.execute('SELECT session, at, gear, rpm, method FROM shifts ORDER BY id').fetchall()))
        if threaded:
            learner.publish()                     # what the thread does once a second
            assert learner.snapshot() is learner.published
        learner.close()
    assert rows[0] == rows[1]
