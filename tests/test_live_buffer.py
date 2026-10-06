"""The live run (oversteer/live_buffer.py): the ring and its seq, the delta
to a reference, the states, the threads, the web endpoint, and a real
Assetto Corsa Rally run replayed against itself."""
import os
import threading

import pytest

from oversteer import live_buffer
from oversteer.live_buffer import CAPACITY, CHANNELS, LiveBuffer, Reference
from oversteer.telemetry_store import TRACE_CHANNELS

CAPTURE = os.path.join(os.path.dirname(__file__), 'data', 'acr-elatia-run.ovcap.gz')
NAN = float('nan')


def row(t, d, speed=20.0, gear=3):
    """A trace row as RunTracker keeps it (TRACE_CHANNELS, NaN unknown)."""
    values = dict.fromkeys(TRACE_CHANNELS, NAN)
    values.update(t=t, distance=d, speed=speed, rpm=6000.0, gear=gear, throttle=1.0, brake=0.0, clutch=1.0,
                  steer=0.0, a_long=0.1, a_lat=0.0, yaw_rate=0.0)
    return tuple(values[c] for c in TRACE_CHANNELS)


def reference(speed=20.0, seconds=100.0, splits=(('A', 0.0, 500.0), ('B', 500.0, 1000.0), ('C', 1000.0, 2000.0))):
    """A run at a constant `speed` m/s for `seconds`, 10 Hz."""
    n = int(seconds * 10)
    return Reference(41, 'acr:test', seconds, speed * seconds, [row(i / 10.0, speed * i / 10.0) for i in range(n + 1)],
                     splits=splits)


def col(sample, name):
    return sample[CHANNELS.index(name)]


def drive(buffer, number, speed, until, start=1000.0, t0=0.0):
    """Push 10 Hz rows of run `number` at `speed` m/s up to `until` m."""
    t = t0
    while speed * t <= until:
        buffer.push(number, start + t, row(t, speed * t, speed))
        t = round(t + 0.1, 6)
    return start + t - 0.1


def test_since_returns_only_the_new_rows():
    buffer = LiveBuffer()
    assert buffer.read(0, now=0.0)['state'] == 'idle'
    buffer.start_run(1, 100.0, 'acr', None, 'Greece Elatia', 6500.0)
    for i in range(5):
        buffer.push(1, 100.0 + i / 10, row(i / 10, 2.0 * i))
    first = buffer.read(0, now=100.5)
    assert first['seq'] == 5 and first['state'] == 'live' and len(first['samples']) == 5
    assert [col(s, 'distance') for s in first['samples']] == [0.0, 2.0, 4.0, 6.0, 8.0]
    assert first['channels'] == list(CHANNELS) and first['stage']['name'] == 'Greece Elatia'
    assert first['run'] == {'n': 1, 'id': None} and first['ref_status'] == 'pending'
    assert buffer.read(5, now=100.5)['samples'] == []
    buffer.push(1, 100.5, row(0.5, 10.0))
    buffer.push(1, 100.6, row(0.6, 12.0))
    again = buffer.read(first['seq'], now=100.6)
    assert again['seq'] == 7 and [col(s, 'distance') for s in again['samples']] == [10.0, 12.0]
    assert [col(s, 'distance') for s in buffer.read(5, now=100.6, limit=1)['samples']] == [12.0]
    # A seq this process never gave (the page outlived an Oversteer restart): from the start
    stale_client = buffer.read(9999, now=100.6)
    assert stale_client['reset'] and len(stale_client['samples']) == 7
    assert buffer.read('junk', now=100.6)['seq'] == 7
    # NaN goes out as null, values rounded
    assert col(again['samples'][0], 'x') is None and col(again['samples'][0], 'delta') is None


def test_the_ring_keeps_thirty_seconds():
    buffer = LiveBuffer()
    buffer.start_run(1, 0.0)
    for i in range(CAPACITY + 100):
        buffer.push(1, i / 10, row(i / 10, float(i)))
    body = buffer.read(0, now=(CAPACITY + 99) / 10)
    assert len(body['samples']) == CAPACITY
    assert col(body['samples'][0], 'distance') == 100.0 and col(body['samples'][-1], 'distance') == CAPACITY + 99


def test_delta_split_and_prediction_against_a_reference():
    buffer = LiveBuffer()
    buffer.start_run(3, 1000.0, 'acr', 'acr:test')
    buffer.offer_reference(3, reference(), {'key': 'acr:test', 'name': 'Test', 'length': 2000.0}, 77)
    end = drive(buffer, 3, 19.0, 750.0)                     # slower than the reference's 20 m/s
    body = buffer.read(0, now=end)
    assert body['ref_status'] == 'ready' and body['run'] == {'n': 3, 'id': 77}
    assert body['ref']['run'] == 41 and body['ref']['time'] == 100.0
    assert [s['name'] for s in body['ref']['splits']] == ['A', 'B', 'C']
    d = body['distance']
    assert body['delta'] == pytest.approx(d / 19 - d / 20, abs=1e-6)
    assert body['predicted'] == pytest.approx(100.0 + body['delta'])
    # Every row has its own delta, growing as the run falls behind
    deltas = [col(s, 'delta') for s in body['samples']]
    assert all(b >= a for a, b in zip(deltas, deltas[1:]))
    split = body['split']
    assert split['index'] == 1 and split['n'] == 3 and split['name'] == 'B'
    assert split['delta'] == pytest.approx(d / 380 - 500 / 380, abs=2e-3)       # lost since B began
    assert split['prev']['index'] == 0 and split['prev']['delta'] == pytest.approx(500 / 380, abs=2e-3)
    assert body['sector'] is None                          # the reference has no sectors


def test_a_reference_for_another_run_is_not_taken():
    buffer = LiveBuffer()
    buffer.start_run(1, 0.0)
    buffer.offer_reference(2, reference())                 # late, for a run not yet started
    buffer.push(1, 0.0, row(0.0, 0.0))
    body = buffer.read(0, now=0.0)
    assert body['ref'] is None and body['ref_status'] == 'pending' and body['delta'] is None


def test_without_a_reference():
    buffer = LiveBuffer()
    buffer.start_run(1, 0.0, 'acr', 'acr:test')
    buffer.offer_reference(1, None, {'key': 'acr:test', 'name': 'Test', 'length': None}, 5)
    end = drive(buffer, 1, 20.0, 300.0, start=0.0)
    body = buffer.read(0, now=end)
    assert body['state'] == 'live' and body['ref_status'] == 'none' and body['ref'] is None
    assert body['delta'] is None and body['predicted'] is None and body['split'] is None
    assert all(col(s, 'delta') is None for s in body['samples']) and len(body['samples']) > 100
    buffer.finish(1, end, 15.2)
    assert buffer.read(0, now=end)['final'] == {'time': 15.2, 'delta': None}


def test_a_reference_arriving_part_way_does_not_time_that_split():
    buffer = LiveBuffer()
    buffer.start_run(1, 1000.0)
    drive(buffer, 1, 20.0, 600.0)
    buffer.offer_reference(1, reference())
    end = drive(buffer, 1, 20.0, 1200.0, t0=30.1)
    body = buffer.read(0, now=end)
    assert body['delta'] == pytest.approx(0.0, abs=1e-6)
    assert body['split']['index'] == 2 and body['split']['delta'] == pytest.approx(0.0, abs=1e-6)
    assert body['split']['prev'] is None                   # B was joined part way


def test_restart_finish_and_stale():
    buffer = LiveBuffer()
    buffer.start_run(1, 0.0)
    buffer.offer_reference(1, reference())
    end = drive(buffer, 1, 20.0, 200.0, start=0.0)
    assert buffer.read(0, now=end + 1.9)['state'] == 'live'
    assert buffer.read(0, now=end + 2.1)['state'] == 'stale'   # a pause: no rows
    # A restart: back to idle, the old run's rows are gone
    buffer.end_run(1, end + 3, 'restart')
    idle = buffer.read(0, now=end + 3)
    assert idle['state'] == 'idle' and idle['samples'] == [] and idle['run'] is None and idle['delta'] is None
    buffer.start_run(2, end + 5)
    buffer.offer_reference(2, reference())
    end = drive(buffer, 2, 25.0, 2010.0, start=end + 5)        # faster than the reference, past its finish
    body = buffer.read(0, now=end)
    assert body['run']['n'] == 2 and body['first'] == idle['seq'] + 1
    assert len(body['samples']) == CAPACITY or col(body['samples'][0], 't') == 0.0
    buffer.finish(2, end, 80.0)
    done = buffer.read(0, now=end + 60)                         # not stale: finished stays finished
    assert done['state'] == 'finished' and done['final'] == {'time': 80.0, 'delta': -20.0}
    assert done['delta'] == -20.0 and done['predicted'] == 80.0
    seq = done['seq']
    buffer.push(2, end + 1, row(80.1, 2002.0))                  # the roll-out is not shown
    assert buffer.read(0, now=end + 1)['seq'] == seq
    buffer.end_run(2, end + 10, 'finish')
    assert buffer.read(0, now=end + 10)['state'] == 'finished'
    assert done['split']['prev']['index'] == 2                  # the last split timed at its end
    buffer.start_run(3, end + 20)
    assert buffer.read(seq, now=end + 20)['state'] == 'live'
    assert buffer.read(0, now=end + 20)['final'] is None


def test_rows_of_an_ended_run_are_not_taken():
    buffer = LiveBuffer()
    buffer.start_run(1, 0.0)
    buffer.end_run(1, 1.0, 'teleport')
    buffer.push(1, 1.1, row(1.1, 20.0))
    buffer.finish(1, 1.2, 10.0)
    assert buffer.read(0, now=1.2)['seq'] == 0


def test_readers_on_other_threads_see_whole_rows_in_order():
    """One writer, the listener; readers on web threads poll as fast as they
    can. Every response's rows are whole (each value written from one
    counter), in order, end at the response's seq, and follow the reader's
    last seq without a gap unless it fell a whole ring behind."""
    buffer = LiveBuffer()
    total = 30000
    errors = []
    done = threading.Event()

    def counted(k):
        values = dict.fromkeys(TRACE_CHANNELS, float(k))
        return tuple(values[c] for c in TRACE_CHANNELS)

    def write():
        buffer.start_run(1, 0.0)
        for k in range(1, total + 1):
            buffer.push(1, k / 10.0, counted(k))
        done.set()

    def read():
        since, polls = 0, 0
        while not done.is_set() or since < total:
            body = buffer.read(since, now=0.0)
            polls += 1
            ks = [col(s, 't') for s in body['samples']]
            for s in body['samples']:
                if len({v for v in s[:-1]}) != 1:
                    errors.append(('torn', s))
            if ks:
                if ks != list(range(int(ks[0]), int(ks[0]) + len(ks))) or ks[-1] != body['seq']:
                    errors.append(('order', since, ks[:3], ks[-1], body['seq']))
                if ks[0] != since + 1 and body['seq'] - since <= CAPACITY:
                    errors.append(('gap', since, ks[0], body['seq']))
            if body['seq'] < since:
                errors.append(('back', since, body['seq']))
            since = body['seq']
            if errors:
                return
        assert polls > 1

    readers = [threading.Thread(target=read) for _ in range(4)]
    for r in readers:
        r.start()
    writer = threading.Thread(target=write)
    writer.start()
    writer.join(60)
    for r in readers:
        r.join(60)
    assert not errors, errors[:5]
    assert buffer.read(total - 5, now=0.0)['samples'][-1][0] == total


def test_the_web_endpoint():
    from oversteer.telemetry_web import TelemetryWeb

    class Learner:
        live_run = LiveBuffer()

    learner = Learner()
    learner.live_run.start_run(1, 0.0)
    learner.live_run.push(1, 0.0, row(0.0, 0.0))
    dash = {'gear': 3, 'rpm': 6500.0}
    web = TelemetryWeb(port=0, bind='local', live=lambda: dash, learner=learner)
    assert web.api('live', {}) == (200, dash)                  # the page as before phase 3
    status, body = web.api('live', {'since': '0', 'limit': '50'})
    assert status == 200 and body['seq'] == 1 and len(body['samples']) == 1 and body['dash'] == dash
    assert body['state'] in ('live', 'stale')
    status, body = TelemetryWeb(port=0, bind='local', live=lambda: None).api('live', {'since': '0'})
    assert status == 200 and body['state'] == 'idle' and body['samples'] == [] and body['dash'] is None


@pytest.mark.skipif(not os.path.exists(CAPTURE), reason='no capture')
def test_a_real_acr_run_against_itself(tmp_path):
    """An ACR run (Greece Elatia, 243 s, clean) replayed twice into one
    database: the second time its reference is the first, the same run, so
    the delta is about 0 all the way and at the finish."""
    from oversteer.shift_learner import ShiftLearner
    from oversteer.telemetry_capture import read_capture, replay

    learner = ShiftLearner(str(tmp_path / 'telemetry.db'))
    meta, records = read_capture(CAPTURE)
    replay(records, learner, started=meta['started'])
    first = learner.live_run.read(0, now=0.0)
    assert first['state'] == 'finished' and first['ref_status'] == 'none' and first['final']['delta'] is None
    run_id = learner.db.execute('SELECT id FROM runs WHERE finished = 1').fetchone()[0]

    seen = []
    buffer = learner.live_run
    push = buffer.push

    def watched(number, now, values):
        push(number, now, values)
        seen.append(dict(buffer.state))
    buffer.push = watched
    meta, records = read_capture(CAPTURE)
    replay(records, learner, started=meta['started'])

    body = buffer.read(0, now=0.0)
    assert body['state'] == 'finished' and body['ref_status'] == 'ready' and body['ref']['run'] == run_id
    assert body['stage']['key'] == 'acr:greece:elatia' and body['stage']['name']
    assert body['final']['time'] == pytest.approx(243.2, abs=0.5)
    assert body['final']['delta'] == pytest.approx(0.0, abs=0.05)
    deltas = [s['delta'] for s in seen if s['delta'] is not None]
    assert len(deltas) > 0.9 * len(seen) and len(seen) > 2000           # 10 Hz over 4 minutes
    assert max(abs(x) for x in deltas) < 0.2
    assert all(abs(s['predicted'] - body['ref']['time']) < 0.2 for s in seen if s['predicted'] is not None)
    splits = body['ref']['splits']
    assert len(splits) > 5
    indexes = [s['split']['index'] for s in seen if s['split'] and s['split']['index'] is not None]
    assert indexes == sorted(indexes) and indexes[-1] == len(splits) - 1
    timed = [s['split']['prev']['delta'] for s in seen if s['split'] and s['split']['prev']]
    assert timed and max(abs(x) for x in timed) < 0.15
    assert len(body['samples']) == CAPACITY


def test_load_reference_without_runs(tmp_path):
    from oversteer.telemetry_store import open_store
    store = open_store(str(tmp_path / 'telemetry.db'))
    assert live_buffer.load_reference(store, 'acr:greece:elatia', 1) is None
    assert live_buffer.load_reference(store, None, 1) is None
