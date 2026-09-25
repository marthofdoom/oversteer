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
        t += 5.0                                             # a gap: the session ends, as between stages
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
        assert len(learner.history('forza-777')) == 2           # the gap ended the first session
    assert models[0] == models[1]
    model = json.loads(models[0][1])
    assert model['power_source'] == 'game' and abs(sorted(model['ratios']['3'])[15] - 250.0) < 0.5


def test_replay_script(tmp_path):
    path = tmp_path / 'stage.ovcap.gz'
    write(path, stage())
    script = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'telemetry-replay.py')
    out = subprocess.run([sys.executable, script, str(path)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert 'every packet decoded' in out.stdout and 'forza-777' in out.stdout and 'gear 3:' in out.stdout
    piece = tmp_path / 'piece.ovcap.gz'
    out = subprocess.run([sys.executable, script, str(path), '--cut', '1:2', '--write', str(piece)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    times = [t for t, _, _ in read_capture(str(piece))[1]]
    assert 55 <= len(times) <= 65 and times[0] == 0.0
