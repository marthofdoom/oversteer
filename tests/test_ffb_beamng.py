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


def csb(gt, seq, rack, steer, speed=20.0, side_slip=0.3):
    s = Sample(0.0, speed=speed, car='beamng/unknown', game='beamng-ffb')
    s.game_time, s.packet_id, s.rack_force, s.steer_input = gt, seq, rack, steer
    s.tyre_side_slip = (side_slip, side_slip, 0.2, 0.2)
    return s


def outgauge():
    return Sample(3000.0, speed=20.0, car='beamng/unknown', game='beamng')


class Drive:
    """1 ms engine passes; the game's physics in a burst of 8 CSB1 packets (2 ms of game time each) per 16 ms
    frame; the game's own force uploaded at 400 a second; OutGauge at 60 a second."""

    def __init__(self, engine, sign=-1.0, frame=0.016, per_frame=8, raw=True, raw_fn=None, csb_on=True,
                 outgauge_on=True, paused=False, rack_scale=2500.0):
        self.engine, self.sign, self.frame, self.per_frame = engine, sign, frame, per_frame
        self.raw, self.raw_fn, self.csb_on, self.outgauge_on, self.paused = raw, raw_fn, csb_on, outgauge_on, paused
        self.t = self.gt = 0.0
        self.seq = 0
        self.next_frame = self.next_up = self.next_og = 0.0
        self.steer_override = None
        self.rack_scale = rack_scale
        self.side_slip = 0.3
        self.drift = False             # donuts: big side slip, the steering against the force's usual relation

    def steer(self, gt):
        return 0.6 * math.sin(2 * math.pi * 0.3 * gt) if self.steer_override is None else self.steer_override

    def rack(self, gt):
        # BeamNG's rack force pushes back against the steering, with a little road on it; in a drift it pulls
        # toward the countersteer: with the steering, against the usual relation
        side = 1.0 if self.drift else -1.0
        return side * self.rack_scale * self.steer(gt) + 60.0 * math.sin(2 * math.pi * 31.0 * gt)

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
                        src.push(t, csb(self.gt, self.seq, self.rack(self.gt), self.steer(self.gt),
                                        side_slip=self.side_slip))
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
    b.steer_rate = 0.0
    assert b.softlock(csb(drive.gt, 1, 0.0, 0.9)) == 0.0                 # inside the zone
    at_lock = csb(drive.gt, 1, 0.0, 1.0)
    assert b.softlock(at_lock) == pytest.approx(b.centre_sign * b.SOFTLOCK * fe.FULL)
    assert b.softlock(csb(drive.gt, 1, 0.0, -1.0)) == -b.softlock(at_lock)
    assert 0 < abs(b.softlock(csb(drive.gt, 1, 0.0, 0.97))) < abs(b.softlock(at_lock))    # the ramp
    assert b.softlock(csb(drive.gt, 1, 0.0, 1.0, speed=0.0)) == 0.0     # standing
    assert abs(b.softlock(csb(drive.gt, 1, 0.0, 1.0, speed=1.0))) == pytest.approx(abs(b.softlock(at_lock)) / 2)
    b.steer_rate = 2.0                                                  # moving outward: damped harder
    assert abs(b.softlock(at_lock)) > b.SOFTLOCK * fe.FULL
    assert abs(b.softlock(at_lock)) <= b.SOFTLOCK_MAX * fe.FULL
    b.steer_rate = -2.0                                                 # coming back: no damping
    assert abs(b.softlock(at_lock)) == pytest.approx(b.SOFTLOCK * fe.FULL)
    drive.steer_override = 1.0
    drive.run(0.1)
    s = engine._csb_sample
    assert forces()[-1] == pytest.approx(min(max(b.force(s) + b.softlock(s), -fe.FULL), fe.FULL), abs=1)


def test_donuts_never_push_toward_the_lock(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(7.0)
    b = engine.beamng
    assert engine.status.active and b.centre_sign is not None
    sign = b.centre_sign
    drive.drift, drive.side_slip = True, 6.0            # sliding: the force pulls with the steering
    for _ in range(80):
        drive.run(0.1)
        s = engine._csb_sample
        push = b.softlock(s)
        if push:
            assert push * sign * math.copysign(1.0, s.steer_input) > 0       # toward the centre only
    assert b.centre_sign == sign
    drive.steer_override = 0.99                         # held at the lock in the donut
    for _ in range(30):
        drive.run(0.1)
        s = engine._csb_sample
        assert b.softlock(s) * sign > 0 and b.centre_sign == sign


def test_the_centring_turning_at_low_slip_is_a_mismatch(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(7.0)
    assert engine.status.active
    drive.drift = True                                  # the same relation reversed with the tyres gripping
    drive.run(5.0)
    assert not engine.status.active and levels() == [0, 100] and engine.last_stop == 'state'


def test_a_new_vehicle_is_learnt_again(tmp_path):
    engine, path = engine_on(tmp_path)
    steer_force = lambda drive: lambda t: -1.0 * K * -2500.0 * drive.steer(drive.gt)    # the game's own, per steer
    drive = Drive(engine)
    drive.raw_fn = steer_force(drive)
    drive.run(7.0)
    assert engine.status.active and engine.beamng.scale == pytest.approx(K, rel=0.05)
    drive.seq, drive.gt, drive.rack_scale = 0, 0.0, 800.0      # another vehicle: the mod starts again
    drive.run(0.05)
    b = engine.beamng
    assert not b.ready and b.sign is None and b.centre_sign is None and b.moving_time < 0.1
    drive.run(0.3)
    assert not engine.status.active and levels() == [0, 100]
    drive.run(fe.REARM + 7.0)
    assert engine.status.active and b.scale == pytest.approx(K * 2500.0 / 800.0, rel=0.05)


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


def test_quit_while_playing_gives_the_game_its_force_and_stops_listening(tmp_path):
    engine, path = engine_on(tmp_path)
    Drive(engine).run(7.0)
    src = engine._source
    assert engine.status.active
    engine.stop()
    assert levels() == [0, 100] and forces()[-1] == 0 and src.stopped


def test_a_failed_write_while_playing_restores_the_game(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(7.0)
    assert engine.status.active
    FakeDriver.fail_force = True
    drive.run(0.05)
    assert not engine.status.active and levels() == [0, 100] and engine.last_stop == 'write'


def test_an_error_in_the_loop_while_playing_restores_the_game(tmp_path, monkeypatch):
    engine, path = engine_on(tmp_path)
    Drive(engine).run(7.0)
    assert engine.status.active

    def broken(now=None):
        engine._running = False
        raise RuntimeError('boom')
    monkeypatch.setattr(engine, 'step', broken)
    engine._running = True
    engine._run()
    assert levels() == [0, 100] and engine.last_stop == 'error' and engine._broken


def test_writes_are_a_millisecond_apart_in_bursts_and_fades(tmp_path, monkeypatch):
    times = []
    real = FakeDriver.force

    def timed(self, value):
        times.append(engine._written_at)
        real(self, value)
    monkeypatch.setattr(FakeDriver, 'force', timed)
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(7.0)
    engine.set_enabled(False)
    drive.run(0.4)
    assert len(times) > 500 and all(b - a >= fe.MIN_WRITE for a, b in zip(times, times[1:]))
    assert all(b - a <= fe.KEEP_WRITE + 0.0011 for a, b in zip(times, times[1:]))    # never left 30 ms
    assert forces()[-1] == 0 and levels() == [0, 100]
