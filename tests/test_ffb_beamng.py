"""Countersteer FFB for BeamNG (the engine's BeamNG mode, ffb_engine.BeamNGRack) against a fake CSB1 source and
a fake driver directory: the source's lifecycle (started for BeamNG with a wheel, stopped otherwise), the classifier's
states from CSB1 (rate per second of game time, a paused clock, no rack, no raw), the fit against the game's own
force with its sign learnt, the soft lock, and no activation without fresh raw. Physics arrive in bursts per frame
as the game sends them. Nothing here touches a real wheel, /sys or a socket."""
import math

import pytest

from oversteer import ffb_engine as fe
from oversteer.telemetry_formats import Sample
from tests.test_ffb_engine import FakeDriver, forces, levels, upload, wheel  # noqa: F401
from tests.test_ffb_engine import _clean_log  # noqa: F401 (the autouse fixture)

K = 32767.0 / 3000.0               # the game's own force per N of rack force (BeamNG's, made up)


class FakeSource:
    """BeamNGForceSource's interface: start/stop, latest(now) (None when stale), seq."""
    made = []
    bind_ok = True

    def __init__(self):
        self.seq = 0
        self.sample = None
        self.at = None
        self.running = False
        self.stopped = False
        FakeSource.made.append(self)

    def start(self):
        self.running = FakeSource.bind_ok
        return self.running

    def stop(self):
        self.running, self.stopped = False, True

    def latest(self, now=None):
        if self.sample is None or now - self.at > 0.1:
            return None
        return self.sample

    def push(self, t, sample):
        self.seq += 1
        self.sample, self.at = sample, t


@pytest.fixture(autouse=True)
def _sources():
    FakeSource.made = []
    FakeSource.bind_ok = True
    yield


def csb(gt, seq, rack, steer, speed=20.0):
    s = Sample(0.0, speed=speed, car='beamng/unknown', game='beamng-ffb')
    s.game_time, s.packet_id, s.rack_force, s.steer_input = gt, seq, rack, steer
    return s


def outgauge():
    return Sample(3000.0, speed=20.0, car='beamng/unknown', game='beamng')


class Drive:
    """1 ms engine passes; the game's physics in a burst of 8 CSB1 packets (2 ms of game time each) per 16 ms
    frame; the game's own force uploaded at 400 a second; OutGauge at 60 a second."""

    def __init__(self, engine, sign=-1.0, frame=0.016, per_frame=8, raw=True, raw_fn=None, csb_on=True,
                 outgauge_on=True, paused=False):
        self.engine, self.sign, self.frame, self.per_frame = engine, sign, frame, per_frame
        self.raw, self.raw_fn, self.csb_on, self.outgauge_on, self.paused = raw, raw_fn, csb_on, outgauge_on, paused
        self.t = self.gt = 0.0
        self.seq = 0
        self.next_frame = self.next_up = self.next_og = 0.0
        self.steer_override = None

    def steer(self, gt):
        return 0.6 * math.sin(2 * math.pi * 0.3 * gt) if self.steer_override is None else self.steer_override

    def rack(self, gt):
        # BeamNG's rack force pushes back against the steering, with a little road on it
        return -2500.0 * self.steer(gt) + 60.0 * math.sin(2 * math.pi * 31.0 * gt)

    def run(self, seconds):
        end = self.t + seconds
        while self.t < end:
            t = self.t
            src = self.engine._source
            if self.csb_on and t >= self.next_frame:
                for _ in range(self.per_frame):
                    if not self.paused:
                        self.gt += self.frame / self.per_frame
                    self.seq += 1
                    if src is not None and src.running:
                        src.push(t, csb(self.gt, self.seq, self.rack(self.gt), self.steer(self.gt)))
                self.next_frame += self.frame
            if self.raw and t >= self.next_up:
                level = self.raw_fn(t) if self.raw_fn else self.sign * K * self.rack(self.gt)
                upload(max(-32767, min(32767, level)))
                self.next_up += 1 / 400.0
            if self.outgauge_on and t >= self.next_og:
                self.engine.offer(t, outgauge())
                self.next_og += 1 / 60.0
            self.engine.step(t)
            self.t = round(t + 0.001, 6)
        return self


def engine_on(tmp_path, enabled=True, raw=True):
    path = wheel(tmp_path, raw=raw)
    engine = fe.FfbEngine(driver=FakeDriver, beamng_source=FakeSource)
    engine.set_device(path)
    engine.set_enabled(enabled)
    engine.step(0.0)
    return engine, path


# -- the source's lifecycle --

def test_the_source_runs_only_for_beamng_with_a_wheel(tmp_path):
    engine, path = engine_on(tmp_path)
    for i in range(300):                                   # another game: no source
        s = Sample(3000.0, speed=20.0, car='dirt/x', game='dirt')
        engine.offer(i / 60.0, s)
        engine.step(i / 60.0)
    assert FakeSource.made == []
    drive = Drive(engine, csb_on=False)
    drive.t = drive.next_og = drive.next_up = 5.0
    drive.run(0.5)
    assert len(FakeSource.made) == 1 and engine._source.running
    assert engine.status[:2] == ('add-on', 'beamng-no-mod')
    assert 'mod' in fe.status_text(engine.status)[1]
    drive.outgauge_on = False                              # BeamNG closed: stopped a second later
    drive.raw = False
    drive.run(1.5)
    assert engine._source is None and FakeSource.made[0].stopped


def test_the_source_stops_without_a_wheel_and_at_quit(tmp_path):
    engine, path = engine_on(tmp_path)
    Drive(engine).run(1.0)
    src = engine._source
    assert src is not None
    engine.set_device(None)
    engine.step(1.1)
    assert engine._source is None and src.stopped
    engine.set_device(path)
    Drive(engine).run(0.5)
    src = engine._source
    engine.stop()
    assert src.stopped and engine._source is None


def test_a_port_in_use_is_said_and_retried(tmp_path):
    FakeSource.bind_ok = False
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(1.0)
    assert engine.status.reason == 'beamng-port' and len(FakeSource.made) == 1
    FakeSource.bind_ok = True
    drive.run(fe.SOURCE_RETRY)
    assert len(FakeSource.made) == 2 and engine._source is not None


# -- replacing --

def test_beamng_goes_green_learns_the_sign_and_plays_the_rack(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine, sign=-1.0).run(1.5)
    assert engine.status.state == 'replace' and engine.status.game == 'beamng'
    assert 450 < engine.status.rate < 550                  # per second of game time, bursts or not
    drive.run(6.0)
    st = engine.status
    assert st.active and st.ready and levels() == [0]
    b = engine.beamng
    assert b.sign == -1.0 and b.scale == pytest.approx(K, rel=0.05) and b.fit < -0.9
    assert b.centre_sign == 1.0                            # the game's force follows the steering input's sign
    s = engine._csb_sample
    assert forces()[-1] == pytest.approx(-K * s.rack_force, rel=0.05, abs=50)
    light, text = fe.status_text(st)
    assert light == 'green' and 'rack' in text


def test_the_other_sign_too(tmp_path):
    engine, path = engine_on(tmp_path)
    Drive(engine, sign=1.0).run(7.0)
    assert engine.status.active and engine.beamng.sign == 1.0 and engine.beamng.centre_sign == -1.0


def test_the_soft_lock_pushes_back_at_the_lock(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(7.0)
    assert engine.status.active
    b = engine.beamng
    inside = csb(drive.gt, 1, 0.0, 0.5)
    at_lock = csb(drive.gt, 1, 0.0, 1.0)
    assert b.softlock(inside) == 0.0
    assert b.softlock(at_lock) == pytest.approx(b.centre_sign * b.SOFTLOCK * fe.FULL)
    assert b.softlock(csb(drive.gt, 1, 0.0, -1.0)) == -b.softlock(at_lock)
    drive.steer_override = 1.0
    drive.run(0.1)
    rack = engine._csb_sample.rack_force
    assert forces()[-1] == pytest.approx(min(max(b.force(engine._csb_sample) + b.softlock(at_lock), -fe.FULL),
                                             fe.FULL), abs=1)
    assert rack == pytest.approx(-2500.0, abs=100)


def test_no_fresh_raw_no_force(tmp_path):
    engine, path = engine_on(tmp_path)
    Drive(engine, raw=False).run(7.0)
    assert FakeDriver.log == [] and not engine.beamng.ready


def test_an_unrelated_game_force_is_never_played(tmp_path):
    engine, path = engine_on(tmp_path)
    Drive(engine, raw_fn=lambda t: 8000.0 * math.sin(2 * math.pi * 1.7 * t)).run(8.0)
    assert FakeDriver.log == [] and engine.status.waiting in ('fit', 'corners')


def test_a_slow_mod_is_grey(tmp_path):
    engine, path = engine_on(tmp_path)
    Drive(engine, frame=0.016, per_frame=1).run(4.0)        # 62 a second of game time
    assert engine.status[:2] == ('add-on', 'csb-rate') and FakeDriver.log == []


def test_paused_hands_the_force_back(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(7.0)
    assert engine.status.active
    drive.paused = True
    drive.run(0.6)
    assert not engine.status.active and levels() == [0, 100]
    assert engine.status[:2] == ('add-on', 'beamng-paused')


def test_a_driver_without_game_ffb_raw_is_grey(tmp_path):
    engine, path = engine_on(tmp_path, raw=False)
    Drive(engine).run(3.0)
    assert engine.status[:2] == ('add-on', 'old-driver') and FakeDriver.log == []


def test_csb1_carries_the_steering_input():
    from oversteer.telemetry_formats import csb1_packet, decode_sample
    s = decode_sample(csb1_packet(3, 1.5, 100.0, -0.25, 9.0, [(1.0, 0.0, 0.0, 0.0)] * 4))
    assert s.steer_input == -0.25 and s.steer is None
