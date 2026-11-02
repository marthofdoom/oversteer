"""An ACR stage through bridge v5 at 333 packets a second (one per physics step): the drive log fed every packet
live, and fed the 60 Hz capture saved of the same stream, time the run and its sectors the same."""
import math

import pytest

from oversteer import coach_context as cc, run_analysis
from oversteer import telemetry_capture as tc
from oversteer.telemetry import Sample
from oversteer.telemetry_formats import decode_sample
from tests.sim import LIMITER, ovst_packet
from tests.test_drive_log import drive_runs

HZ = 333.0
TRACK = 'Wales Afon Bidno'          # Afon Bidno - Severn: lines 242.9, 1985.1, 3655.2, 5294.1 (the flying finish)
FINISH = 5294.1


def bidno_stream(start=238.0, stand=3.0, roll_on=10.0):
    """(t, packet): standing at the line with the clock at 0, then driving (18 to 26 m/s, so the sectors differ)
    with the clock running; the clock stops where the distance crosses the finish, the car rolls on."""
    out, t, d, step, clock, go = [], 0.0, start, 7000, 0.0, None
    stopped_at = None
    while stopped_at is None or t < stopped_at + roll_on:
        moving = t >= stand
        speed = 22.0 + 4.0 * math.sin(d / 400.0) if moving else 0.0
        if moving and go is None:
            go = t
        if go is not None and stopped_at is None:
            clock = t - go
        s = Sample(6000.0 if moving else 1500.0, LIMITER, gear=3 if moving else 1, speed=speed,
                   car='acr/Skoda Fabia RS Rally2', game='acr', throttle=0.8 if moving else 0.0)
        s.lap_distance, s.stage_length, s.stage_time = d, 5599.8, clock
        s.accel = (0.0, 0.0, 0.0)
        out.append((t, ovst_packet(s, step, car=b'Skoda Fabia RS Rally2', track=TRACK.encode())))
        step += 1
        t += 1 / HZ
        d += speed / HZ
        if stopped_at is None and d >= FINISH:
            stopped_at = t
    return out


def _timed(tmp_path, packets, name):
    learner, reader, session = drive_runs(tmp_path, [(t, decode_sample(d), 0.8) for t, d in packets], name=name)
    [run] = [r for r in session['runs'] if r['distance'] > 300]
    full = reader.run(run['id'])
    rows = cc.stage_rows(reader.trace(run['id']), full['course'], True, full['result_time'])
    sectors = run_analysis._sector_rows(run['stage'], rows, None, reader, (full, None))
    learner.close()
    return run, sectors


def test_a_333_hz_acr_stage_times_the_same_live_and_from_its_60_hz_capture(tmp_path):
    packets = bidno_stream()
    path = str(tmp_path / 'v5.ovcap.gz')
    with tc.CaptureWriter(path, port=5300) as writer:
        for t, data in packets:
            writer.write(t, ('127.0.0.1', 1), data)
    thin = [(t, d) for t, _a, d in tc.read_capture(path)[1]]
    assert 58.0 <= len(thin) / (packets[-1][0] - packets[0][0]) <= 62.0       # saved at 60 a second
    live, live_sectors = _timed(tmp_path, packets, 'live.db')
    saved, saved_sectors = _timed(tmp_path, thin, 'thin.db')
    assert live['stage'] == saved['stage'] == 'acr:wales:afon-bidno-severn'
    assert live['finished'] == saved['finished'] == 1
    assert saved['result_time'] == pytest.approx(live['result_time'], abs=1e-3)      # the game's clock at the line
    assert live_sectors and [r['name'] for r in live_sectors] == [r['name'] for r in saved_sectors] == ['S1', 'S2', 'S3']
    for a, b in zip(live_sectors, saved_sectors):
        assert b['time'] == pytest.approx(a['time'], abs=0.02), a['name']
    assert sum(r['time'] for r in live_sectors) == pytest.approx(live['result_time'], abs=0.05)
