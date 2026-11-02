"""Captures: write/read round trip, a truncated tail, and replays that learn
the same thing every time."""
import gzip
import json
import math
import os
import struct
import subprocess
import sys

import pytest

from oversteer.telemetry_capture import CaptureWriter, read_capture, replay
from oversteer.shift_learner import ShiftLearner

RATIOS = {1: 480.0, 2: 330.0, 3: 250.0, 4: 200.0}
LIMITER = 7500.0


def forza_dash(rpm, speed, gear, throttle, power):
    """Forza Horizon's 324-byte packet with what the learner reads."""
    data = bytearray(324)
    struct.pack_into('<i', data, 0, 1)
    struct.pack_into('<fff', data, 8, LIMITER, 900.0, rpm)
    struct.pack_into('<i', data, 212, 777)
    struct.pack_into('<ff', data, 244 + 12, speed, power)
    struct.pack_into('<BBBBB', data, 244 + 71, int(throttle * 255), 0, 0, 0, gear)
    return bytes(data)


def stage():
    """(t, packet): part throttle then a pull through the gears, twice."""
    def engine(rpm):
        return 150000.0 * math.sin(math.pi / 2 * min(rpm, 6500) / 6500) * (1 - max(0, rpm - 6500) / 4000)
    t, out = 0.0, []
    for run in range(2):
        for gear in RATIOS:
            speed = 2500.0 / RATIOS[gear]
            for _ in range(60):                              # a steady second at part throttle
                t += 1 / 60
                out.append((t, forza_dash(RATIOS[gear] * speed, speed, gear, 0.3, 20000.0)))
            while RATIOS[gear] * speed < LIMITER * 0.98:
                rpm = RATIOS[gear] * speed
                speed += engine(rpm) / 1200.0 / speed / 60
                t += 1 / 60
                out.append((t, forza_dash(rpm, speed, gear, 1.0, engine(rpm))))
        t += 130.0                                           # a gap: the session ends, as between stages
    return out


def write(path, packets, source=('192.168.1.20', 5555)):
    with CaptureWriter(str(path), port=5310, note='test') as writer:
        for t, data in packets:
            writer.write(100.0 + t, source, data)
    return writer.packets


def test_round_trip(tmp_path):
    path = tmp_path / 'a.ovcap.gz'
    packets = [(0.0, b'one'), (0.5, b'two' * 100), (1.25, b'')]
    assert write(path, packets) == 3
    meta, records = read_capture(str(path))
    assert meta['port'] == 5310 and meta['note'] == 'test'
    assert [(t, addr[0], data) for t, addr, data in records] == [(t, '192.168.1.20', d) for t, d in packets]


def test_truncated_tail_reads_up_to_the_last_whole_record(tmp_path):
    path = tmp_path / 'a.ovcap.gz'
    write(path, [(i * 0.1, bytes([i]) * 50) for i in range(100)])
    raw = gzip.decompress(path.read_bytes())
    cut = tmp_path / 'cut.ovcap.gz'
    cut.write_bytes(gzip.compress(raw[:-20]))                # the last record short
    assert len(list(read_capture(str(cut))[1])) == 99
    torn = tmp_path / 'torn.ovcap.gz'
    torn.write_bytes(path.read_bytes()[:-30])                # the gzip stream itself cut off
    assert 0 < len(list(read_capture(str(torn))[1])) < 100
    bad = tmp_path / 'bad.ovcap.gz'
    bad.write_bytes(gzip.compress(b'not a capture\n'))
    with pytest.raises(ValueError):
        read_capture(str(bad))


def test_replay_learns_the_same_every_time(tmp_path):
    path = tmp_path / 'stage.ovcap.gz'
    write(path, stage())
    models = []
    for name in ('one.db', 'two.db'):
        learner = ShiftLearner(str(tmp_path / name))
        telemetry = replay(read_capture(str(path))[1], learner)
        assert telemetry.live is None and not telemetry._unknown_sizes
        row = learner.db.execute('SELECT key, model FROM cars').fetchone()
        models.append(row)
        assert len(learner.history('forza-fh/777')) == 2           # the gap ended the first session
    assert models[0] == models[1]
    model = json.loads(models[0][1])
    assert model['power_source'] == 'game' and abs(sorted(model['ratios']['3'])[15] - 250.0) < 0.5


def test_replay_script(tmp_path):
    path = tmp_path / 'stage.ovcap.gz'
    write(path, stage())
    script = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'telemetry-replay.py')
    out = subprocess.run([sys.executable, script, str(path)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert 'every packet decoded' in out.stdout and 'forza-fh/777' in out.stdout and 'gear 3:' in out.stdout
    piece = tmp_path / 'piece.ovcap.gz'
    out = subprocess.run([sys.executable, script, str(path), '--cut', '1:2', '--write', str(piece)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    times = [t for t, _, _ in read_capture(str(piece))[1]]
    assert 55 <= len(times) <= 65 and times[0] == 0.0


def test_recorder_writes_a_file_per_stretch_of_driving(tmp_path):
    from oversteer import telemetry_capture as tc
    from oversteer.telemetry import Telemetry, NoLeds
    wall = [1.7e9]
    recorder = tc.Recorder(str(tmp_path), port=5310, version='test', threaded=False, clock=lambda: wall[0])
    telemetry = Telemetry(NoLeds())
    telemetry.recorder = recorder
    packet = forza_dash(5000.0, 20.0, 2, 1.0, 1e5)
    for i in range(10):
        telemetry.handle(100.0 + i / 60.0, packet, ('192.168.1.5', 50000))
    telemetry.handle(100.2, b'unknown', ('192.168.1.5', 50000))     # kept too: a capture is what arrived
    recorder.check(130.0)                                           # a pause: the file stays open
    first = recorder.path
    assert first and first.endswith('.ovcap.gz')
    recorder.check(161.0)                                           # a minute of nothing: closed
    assert recorder.path is None
    wall[0] += 3600.0
    telemetry.handle(400.0, packet, ('192.168.1.5', 50000))
    recorder.close()
    names = [os.path.basename(p) for p, _size, _labelled in tc.list_captures(str(tmp_path))]
    assert len(names) == 2 and os.path.basename(first) in names
    meta, records = read_capture(first)
    records = list(records)
    assert meta['port'] == 5310 and meta['started'] == 1.7e9 and len(records) == 11
    assert records[-1][2] == b'unknown' and records[0][1][0] == '192.168.1.5'


def test_recorder_gives_up_quietly_when_it_cannot_write(tmp_path):
    from oversteer import telemetry_capture as tc
    blocked = tmp_path / 'file'
    blocked.write_text('not a folder')
    recorder = tc.Recorder(str(blocked / 'captures'), threaded=False)
    recorder.packet(1.0, ('127.0.0.1', 0), b'x')
    recorder.packet(2.0, ('127.0.0.1', 0), b'x')
    assert recorder.error and recorder.path is None
    recorder.close()


def test_recorder_thread_and_a_full_queue(tmp_path, monkeypatch):
    from oversteer import telemetry_capture as tc
    recorder = tc.Recorder(str(tmp_path))
    for i in range(50):
        recorder.packet(float(i), ('127.0.0.1', 0), b'%d' % i)
    recorder.close()
    [(path, _size, _labelled)] = tc.list_captures(str(tmp_path))
    assert [data for _t, _a, data in read_capture(path)[1]] == [b'%d' % i for i in range(50)]
    # The listener never waits: a full queue drops and counts
    monkeypatch.setattr(tc, 'RECORD_QUEUE', 2)
    stuck = tc.Recorder(str(tmp_path), threaded=False)
    stuck._threaded = True                                         # as if the writer thread were busy
    for i in range(5):
        stuck.packet(float(i), ('127.0.0.1', 0), b'x')
    assert stuck.dropped == 3
    # A packet the listener hands over after the recorder was closed is
    # not written (nor does it open a file nobody would close)
    before = tc.list_captures(str(tmp_path))
    recorder.packet(99.0, ('127.0.0.1', 0), b'late')
    assert tc.list_captures(str(tmp_path)) == before


def test_prune_keeps_labelled_captures_and_the_newest(tmp_path):
    from oversteer import telemetry_capture as tc
    paths = []
    for i in range(4):
        path = tmp_path / '2026010{}-120000.ovcap.gz'.format(i + 1)
        path.write_bytes(b'x' * 1000)
        os.utime(path, (1.7e9 + i * 100, 1.7e9 + i * 100))
        paths.append(str(path))
    (tmp_path / 'other.txt').write_bytes(b'y' * 5000)             # not a capture: not counted, not touched
    open(paths[0] + '.json', 'w').write('{}')                       # the oldest is labelled
    assert tc.capture_summary(str(tmp_path)) == (4, 4000, 1)
    assert tc.prune(str(tmp_path), 2500) == paths[1:3]
    assert [p for p, _s, _l in tc.list_captures(str(tmp_path))] == [paths[0], paths[3]]
    # Labelled captures alone over the cap: nothing more to delete
    open(paths[3] + '.json', 'w').write('{}')
    assert tc.prune(str(tmp_path), 0) == []


def test_label_captures_writes_the_sidecar_of_overlapping_captures(tmp_path):
    from oversteer import telemetry_capture as tc
    spans = [(1000.0, 2000.0), (3000.0, 4000.0), (5000.0, 6000.0)]
    for n, (started, ended) in enumerate(spans):
        path = str(tmp_path / 'c{}.ovcap.gz'.format(n))
        CaptureWriter(path, started=started).close()
        os.utime(path, (ended, ended))
    (tmp_path / 'broken.ovcap.gz').write_bytes(b'not gzip')
    label = {'surface': 'gravel', 'discipline': 'rally-stage'}
    assert tc.label_captures(str(tmp_path), 3500.0, 5200.0, label, {'game': 'eawrc'}) == 2
    assert not os.path.exists(str(tmp_path / 'c0.ovcap.gz.json'))
    with open(str(tmp_path / 'c1.ovcap.gz.json')) as f:
        side = json.load(f)
    assert side == {'label': label, 'session': {'game': 'eawrc', 'started': 3500.0, 'ended': 5200.0}}
    assert tc.capture_summary(str(tmp_path))[2] == 2


def test_a_capture_being_written_can_be_labelled(tmp_path):
    from oversteer import telemetry_capture as tc
    recorder = tc.Recorder(str(tmp_path), threaded=False, clock=lambda: 1000.0)
    recorder.packet(5.0, ('127.0.0.1', 0), b'x' * 100)
    assert tc.capture_span(recorder.path)[0] == 1000.0
    assert tc.label_captures(str(tmp_path), 900.0, 1e12, {'surface': 'snow'}) == 1
    recorder.close()
    assert tc.capture_summary(str(tmp_path))[2] == 1


# -- OVST version 5 (a packet per physics step) is saved at about 60 a second --

def _ovst5_stream(seconds=20.0, hz=333.0, start=5.0, finish=15.0, step0=5000):
    """(t, packet): ACR's bridge at `hz`, parked with the clock at 0 until `start`, then running to `finish`,
    where the clock stops (the flying finish) while the physics goes on."""
    from oversteer.telemetry_formats import OVST5_TAIL
    sys.path.insert(0, os.path.dirname(__file__))
    from test_telemetry import _ovst5
    out = []
    for i in range(int(seconds * hz)):
        t = i / hz
        clock = 0.0 if t < start else min(t, finish) - start
        data = bytearray(_ovst5(clock=clock, packet_id=step0 + i))
        struct.pack_into('<f', data, 292, 100.0 + 20.0 * max(0.0, t - start))      # the distance along the stage
        out.append((t, bytes(data)))
    assert len(out[0][1]) == 328 + OVST5_TAIL.size
    return out


def _saved(tmp_path, packets, name='v5.ovcap.gz'):
    path = tmp_path / name
    with CaptureWriter(str(path), port=5300) as writer:
        for t, data in packets:
            writer.write(100.0 + t, ('127.0.0.1', 1), data)
    return writer, [(t, d) for t, _a, d in read_capture(str(path))[1]]


def test_ovst_v5_at_333_a_second_is_saved_at_60(tmp_path):
    packets = _ovst5_stream()
    writer, kept = _saved(tmp_path, packets)
    rate = len(kept) / 20.0
    assert 58.0 <= rate <= 62.0, rate
    assert writer.thinned == len(packets) - len(kept)
    assert kept[0][1] == packets[0][1] and kept[-1][1] == packets[-1][1]      # the first and the last
    gaps = [b - a for (a, _), (b, _) in zip(kept, kept[1:])]
    assert max(gaps) < 1 / 60 + 1 / 333 + 1e-6                                # no hole
    clocks = [struct.unpack_from('<f', d, 324)[0] for _, d in kept]
    # the clock's start: its first running packet is there
    first_running = next(i for i, c in enumerate(clocks) if c > 0)
    every = [struct.unpack_from('<f', d, 324)[0] for _, d in packets]
    last_zero = max(i for i, c in enumerate(every) if c == 0.0)
    assert clocks[first_running - 1] == 0.0 and kept[first_running][1] == packets[last_zero + 1][1]
    # the stop at the finish is seen within THIN_FROZEN, on a packet kept for it
    stopped = [i for i, (t, _) in enumerate(kept) if t >= 15.0 + 0.1 - 1e-9]
    assert stopped and abs(kept[stopped[0]][0] - (15.0 + 0.1)) < 2 / 333


def test_ovst_v5_keeps_every_change_of_session(tmp_path):
    import copy
    packets = _ovst5_stream(seconds=4.0, start=1.0, finish=10.0)
    changed = copy.copy(packets)
    # a restart at 2 s: the physics step goes back, the clock goes to 0
    i = int(2.0 * 333)
    from test_telemetry import _ovst5
    for k in range(i, len(changed)):
        changed[k] = (changed[k][0], _ovst5(clock=0.0, packet_id=10 + k - i))
    j = int(3.0 * 333) + 1
    car = bytearray(changed[j][1])
    car[32:41] = b'other_car'
    changed[j] = (changed[j][0], bytes(car))
    _, kept = _saved(tmp_path, changed)
    ids = [struct.unpack_from('<i', d, 328)[0] for _, d in kept]
    saved = [d for _, d in kept]
    assert changed[i][1] in saved                                       # the step going back (and the clock to 0)
    assert changed[j][1] in saved and changed[j + 1][1] in saved        # the car's name changing, and back
    assert ids == sorted(ids[:ids.index(10)]) + sorted(ids[ids.index(10):])


def test_ovst_before_v5_and_v5_at_60_are_saved_as_they_come(tmp_path):
    packets = _ovst5_stream(seconds=3.0, hz=60.0)
    _, kept = _saved(tmp_path, packets)
    assert len(kept) == len(packets)
    v4 = [(t, d[:328]) for t, d in _ovst5_stream(seconds=3.0)]
    v4 = [(t, d[:4] + b'\x04' + d[5:]) for t, d in v4]
    _, kept = _saved(tmp_path, v4, 'v4.ovcap.gz')
    assert len(kept) == len(v4)


def _ffb_recorder(tmp_path, limits=(1200.0, 250e6)):
    from oversteer import telemetry_capture as tc
    ticks = iter(range(10 ** 6))                       # one start time a second: the files sort in the order written
    return tc.Recorder(str(tmp_path), version='t', threaded=False, clock=lambda: 1.7e9 + next(ticks), ffb_limits=limits)


def _ffb_files(tmp_path):
    from oversteer import telemetry_capture as tc
    return sorted(p for p, _s, _l in tc.list_captures(str(tmp_path)))     # (names sort by start time)


def _kept_count(path):
    return sum(1 for _ in read_capture(path)[1])


def test_ffb_tuning_saves_unthinned_and_labels_the_header(tmp_path):
    packets = _ovst5_stream(seconds=2.0)
    thin = _ffb_recorder(tmp_path / 'thin')
    for t, d in packets:
        thin.packet(100.0 + t, ('127.0.0.1', 1), d, 5300)
    thin.close()
    full = _ffb_recorder(tmp_path / 'full')
    full.set_ffb_tuning(True)
    for t, d in packets:
        full.packet(100.0 + t, ('127.0.0.1', 1), d, 5300)
    full.close()
    (path,), (thin_path,) = _ffb_files(tmp_path / 'full'), _ffb_files(tmp_path / 'thin')
    assert _kept_count(path) == len(packets) > _kept_count(thin_path) * 3
    assert read_capture(path)[0]['ffb_tuning'] is True
    assert 'ffb_tuning' not in read_capture(thin_path)[0]


def test_ffb_tuning_ends_at_the_time_limit_and_thinning_resumes(tmp_path):
    rec = _ffb_recorder(tmp_path, limits=(1.0, 1e12))
    rec.set_ffb_tuning(True)
    packets = _ovst5_stream(seconds=3.0)
    for t, d in packets:
        rec.packet(100.0 + t, ('127.0.0.1', 1), d, 5300)
    rec.close()
    assert rec.ffb_stopped == 'time' and not rec.ffb_tuning
    first, second = _ffb_files(tmp_path)
    assert read_capture(first)[0].get('ffb_tuning') is True and 'ffb_tuning' not in read_capture(second)[0]
    assert _kept_count(first) >= 333 and _kept_count(second) < 2 * 60 + 10       # thinned again after the stop


def test_ffb_tuning_ends_at_the_size_limit(tmp_path):
    rec = _ffb_recorder(tmp_path, limits=(1e9, 100000))
    rec.set_ffb_tuning(True)
    for t, d in _ovst5_stream(seconds=3.0):
        rec.packet(100.0 + t, ('127.0.0.1', 1), d, 5300)
    rec.close()
    assert rec.ffb_stopped == 'size' and not rec.ffb_tuning
    first = _ffb_files(tmp_path)[0]
    record = len(_ovst5_stream(seconds=0.01)[0][1]) + 14           # packet and its record header (tc.RECORD)
    assert 100000 / record <= _kept_count(first) <= 100000 / record + 1


def test_ffb_tuning_off_again_clears_the_reason_and_can_restart(tmp_path):
    rec = _ffb_recorder(tmp_path, limits=(1e9, 1000))
    rec.set_ffb_tuning(True)
    for t, d in _ovst5_stream(seconds=0.5):
        rec.packet(100.0 + t, ('127.0.0.1', 1), d, 5300)
    assert rec.ffb_stopped == 'size'
    rec.set_ffb_tuning(True)
    assert rec.ffb_stopped is None and rec.ffb_tuning
    rec.close()


def test_prune_skips_captures_recorded_for_ffb_tuning(tmp_path):
    from oversteer import telemetry_capture as tc
    paths = []
    for i in range(3):
        path = str(tmp_path / '2026010{}-120000.ovcap.gz'.format(i + 1))
        CaptureWriter(path, extra={'ffb_tuning': True} if i == 0 else None).close()
        with open(path, 'ab') as f:
            f.write(b'x' * 1000)
        os.utime(path, (1.7e9 + i * 100, 1.7e9 + i * 100))
        paths.append(path)
    assert tc.capture_summary(str(tmp_path))[2] == 1
    assert tc.prune(str(tmp_path), 0) == paths[1:]
    assert os.path.exists(paths[0])
