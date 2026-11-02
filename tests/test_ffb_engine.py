"""Countersteer FFB (oversteer/ffb_engine.py) against a fake driver directory and synthetic OVST v5 streams:
the support classifier's states and transitions, the auto-scale and the sign check, every stop path writing
game_ffb_level 100, stall detection, and an engine that can never hold up the telemetry listener. Nothing
here touches a real wheel or /sys."""
import math
import threading
import time

import pytest

from oversteer import ffb_engine as fe
from oversteer.telemetry import NoLeds, Telemetry
from oversteer.telemetry_formats import OVST4_SIZE, Sample, decode_sample
from tests.sim import ovst_packet

HZ = 333.0
SCALE = 1.0 / 582.0


def acr(t, game_gain=1.0, rate_mz=400.0, speed=20.0, mz_sign=1.0, ff_sign=1.0, mz=True, v5=True, game='acr',
        car='acr/Skoda Fabia RS Rally2', step=None, texture=3.0):
    """A decoded ACR v5 sample at time t: steering weaving 0.3 to each side every 3.3 s, the front tyres' Mz
    following it (centring: positive to the right, as in marth's dump) and finalFF = gain * Mz / 582."""
    right = 0.3 * math.sin(2 * math.pi * 0.3 * t)
    front = mz_sign * rate_mz * right + texture * math.sin(2 * math.pi * 37.0 * t)    # a little texture
    s = Sample(6000.0, 7500.0, gear=3, speed=speed, car=car, game=game)
    s.steer = -right
    if v5:
        s.packet_id = int(t * HZ) if step is None else step
        s.game_ffb = max(-1.0, min(1.0, ff_sign * game_gain * front * SCALE))
        s.tyre_mz = (front / 2, front / 2, 0.1, 0.1) if mz else None
    return s


class FakeDriver(fe.DriverReplace):
    """The real DriverReplace on files in a temporary directory, every write logged."""
    log = []                 # (path, name, value), shared so tests see every instance's writes
    fail_force = False

    def set_game_level(self, level):
        FakeDriver.log.append((self.path, 'game_ffb_level', level))
        super().set_game_level(level)

    def force(self, value):
        if FakeDriver.fail_force:
            raise OSError(19, 'No such device')
        FakeDriver.log.append((self.path, 'ext_force', value))
        super().force(value)


def wheel(tmp_path, name='wheel', level=100, raw=False):
    d = tmp_path / name
    d.mkdir()
    (d / 'ext_force').write_text('0\n')
    (d / 'game_ffb_level').write_text('%d\n' % level)
    (d / 'ext_force_cap').write_text('80\n')
    if raw:
        (d / 'game_ffb_raw').write_text('0 0\n')
    return str(d)


def read(path, name):
    with open(path + '/' + name) as f:
        return int(f.readline())         # written at offset 0, never truncated: the first line is the value


@pytest.fixture(autouse=True)
def _clean_log():
    FakeDriver.log = []
    FakeDriver.fail_force = False
    yield


def engine_on(tmp_path, **kw):
    path = wheel(tmp_path, **kw)
    engine = fe.FfbEngine(driver=FakeDriver)
    engine.set_device(path)
    engine.set_enabled(True)
    engine.step(0.0)
    return engine, path


def run(engine, t0, seconds, hz=HZ, **kw):
    """Feed `seconds` of samples at `hz`, one engine step per sample; returns the time after the last."""
    t, n = t0, int(round(seconds * hz))
    for _ in range(n):
        engine.offer(t, acr(t, **kw))
        engine.step(t)
        t += 1.0 / hz
    return t


def levels(path=None):
    return [v for p, name, v in FakeDriver.log if name == 'game_ffb_level' and (path is None or p == path)]


def forces():
    return [v for _p, name, v in FakeDriver.log if name == 'ext_force']


# -- the classifier --

def classify(samples, holding=False):
    c = fe.SupportClassifier()
    for t, s in samples:
        c.feed(t, s)
    return c, c.verdict(samples[-1][0], holding)


def stream(seconds, hz=HZ, t0=0.0, **kw):
    n = int(round(seconds * hz))
    return [(t0 + i / hz, acr(t0 + i / hz, **kw)) for i in range(n)]


def test_acr_v5_at_333_hz_with_live_mz_is_green_after_a_second():
    _, v = classify(stream(0.5))
    assert (v.state, v.reason) == ('none', 'measuring')
    _, v = classify(stream(1.5))
    assert (v.state, v.reason) == ('replace', 'ok')
    assert v.rate == pytest.approx(333, abs=3)


@pytest.mark.parametrize('game', ['acc', 'ac'])
def test_acc_and_ac_go_green_only_by_passing_live(game):
    assert classify(stream(1.5, game=game))[1].state == 'replace'
    assert classify(stream(1.5, game=game, mz=False))[1].reason == 'no-mz'       # AC: no Mz in the page


def test_a_rate_drop_turns_it_grey_and_holding_tolerates_a_sag():
    assert classify(stream(1.5, hz=200.0))[1].reason == 'low-rate'
    assert classify(stream(1.5, hz=220.0), holding=True)[1].state == 'replace'
    assert classify(stream(1.5, hz=150.0), holding=True)[1].reason == 'low-rate'


def test_a_gap_over_20_ms_blocks_green_and_over_50_ms_ends_a_hold():
    samples = stream(1.5)
    late = [(t + (0.03 if t > 1.0 else 0.0), s) for t, s in samples]           # one 33 ms gap
    assert classify(late)[1].reason == 'gaps'
    assert classify(late, holding=True)[1].state == 'replace'
    later = [(t + (0.06 if t > 1.0 else 0.0), s) for t, s in samples]
    assert classify(later, holding=True)[1].reason == 'gaps'
    # The gap leaves the window after a second: green again
    c, _ = classify(late)
    for t, s in stream(1.1, t0=late[-1][0] + 1 / HZ):
        c.feed(t, s)
    assert c.verdict(t).state == 'replace'


def test_missing_flat_or_old_bridge_mz_is_not_replace():
    assert classify(stream(1.5, mz=False))[1].reason == 'no-mz'
    assert classify(stream(1.5, rate_mz=0.0001, texture=0.0))[1].reason == 'mz-flat'
    # A real v4 packet (the bridge before the force fields), decoded: no packet_id, no Mz
    s = acr(0.0)
    s.accel, s.stage_length = (0.0, 0.0, 0.0), 5000.0
    data = bytearray(ovst_packet(s, 1)[:OVST4_SIZE])
    data[4] = 4
    old = decode_sample(bytes(data))
    assert old.game == 'acr' and old.packet_id is None and old.tyre_mz is None
    old.speed = 20.0
    samples = [(i / HZ, old) for i in range(int(1.5 * HZ))]
    assert classify(samples)[1].reason == 'old-bridge'
    # One old packet in the window is enough
    mixed = stream(1.5)
    mixed[300] = (mixed[300][0], old)
    assert classify(mixed)[1].reason == 'old-bridge'


def test_mz_is_judged_only_when_moving_and_a_stop_keeps_the_verdict():
    parked = stream(1.5, speed=0.5)
    assert classify(parked)[1].reason == 'drive'
    c, v = classify(stream(1.5))
    assert v.state == 'replace'
    for t, s in stream(2.0, t0=1.5, speed=0.0, rate_mz=0.0001, texture=0.0):       # stopped, Mz flat at a standstill
        c.feed(t, s)
    assert c.verdict(t).state == 'replace'


def test_other_games_are_model_add_on_or_none():
    rbr = stream(1.5, hz=236.0, game='rbr', v5=False)
    assert classify(rbr)[1][:2] == ('add-on', 'rbr-no-raw')
    c = fe.SupportClassifier()
    c.has_game_ffb_raw = True                 # the hook for the driver's game_ffb_raw
    for t, s in rbr:
        c.feed(t, s)
    assert c.verdict(t)[:2] == ('model', 'rbr-model')
    assert classify(stream(1.5, hz=60.0, game='forza-fh', v5=False))[1][:2] == ('add-on', 'no-forces')
    assert classify(stream(1.5, hz=240.0, game='forza-fh', v5=False))[1][:2] == ('model', 'fh-model')
    assert classify(stream(1.5, hz=60.0, game='dirt', v5=False))[1][:2] == ('add-on', 'no-forces')
    c, _ = classify(stream(1.5))
    assert c.verdict(1.5 + 1.1).state == 'none'      # a second of silence


def test_a_game_change_starts_the_measurement_again():
    c, _ = classify(stream(1.5))
    for t, s in stream(0.5, t0=1.5, game='acc'):
        c.feed(t, s)
    assert c.verdict(t).reason == 'measuring'


# -- the force: auto-scale and sign check --

def fitted(seconds=8.0, **kw):
    model = fe.RackForce()
    for t, s in stream(seconds, **kw):
        model.feed(t, s)
    return model


def test_it_starts_at_finalff_scale_and_follows_the_games_strength():
    assert fe.RackForce().scale == pytest.approx(SCALE)
    model = fitted(game_gain=0.5)
    assert model.ready and model.refused is None
    assert model.scale == pytest.approx(0.5 * SCALE, rel=0.05)
    s = acr(1.0, game_gain=0.5)
    assert model.force(s) == pytest.approx(32767 * s.game_ffb, rel=0.05, abs=60)       # ACR's force, from raw Mz
    strong = fitted(game_gain=2.0)
    assert strong.scale == pytest.approx(2.0 * SCALE, rel=0.1)                  # clipped finalFF left out


def test_it_waits_for_evidence_before_it_may_play():
    assert not fitted(seconds=2.0).ready          # under MIN_TIME
    assert not fitted(rate_mz=10.0).ready         # straight-line Mz: no corners yet


def test_a_finalff_against_mz_is_refused():
    model = fitted(ff_sign=-1.0)
    assert not model.ready and model.refused == 'reversed'


def test_mz_pushing_away_from_centre_is_refused():
    # finalFF agrees with Mz, but both push the wheel further into the turn: playing it would run to lock
    model = fitted(mz_sign=-1.0)
    assert not model.ready and model.refused == 'reversed'


def test_a_new_car_fits_again():
    model = fitted(ff_sign=-1.0)
    for t, s in stream(8.0, car='acr/Hyundai i20 N Rally2'):
        model.feed(t, s)
    assert model.ready and model.refused is None


# -- the engine: toggle semantics, every stop path --

def test_on_and_green_mutes_the_game_and_streams_ext_force(tmp_path):
    engine, path = engine_on(tmp_path)
    run(engine, 0.0, 2.0)
    assert levels() == [] and forces() == []           # green, but the fit is not ready: the game plays
    t = run(engine, 2.0, 6.0)
    assert levels() == [0]
    assert engine.status.active and engine.status.state == 'replace'
    assert len(forces()) > 300
    s = acr(t)
    engine.offer(t, s)
    engine.step(t)
    assert forces()[-1] == read(path, 'ext_force') == pytest.approx(32767 * s.game_ffb, rel=0.05, abs=60)
    assert read(path, 'game_ffb_level') == 0


def test_off_never_writes(tmp_path):
    engine, path = engine_on(tmp_path)
    engine.set_enabled(False)
    run(engine, 0.0, 8.0)
    assert FakeDriver.log == []
    assert engine.status.state == 'replace' and not engine.status.active


def active(tmp_path, **kw):
    engine, path = engine_on(tmp_path, **kw)
    t = run(engine, 0.0, 8.0)
    assert engine.status.active and levels() == [0]
    return engine, path, t


def test_switching_off_restores_the_game(tmp_path):
    engine, path, t = active(tmp_path)
    engine.set_enabled(False)
    n = len(forces())
    run(engine, t, 0.5)
    assert levels() == [0, 100] and len(forces()) == n
    assert read(path, 'game_ffb_level') == 100 and engine.last_stop == 'off'


@pytest.mark.parametrize('drop, why', [
    (dict(hz=150.0), 'low-rate'),
    (dict(mz=False), 'no-mz'),
    (dict(v5=False), 'old-bridge'),
])
def test_the_state_dropping_restores_the_game(tmp_path, drop, why):
    engine, path, t = active(tmp_path)
    run(engine, t, 1.5, **drop)
    assert levels() == [0, 100] and read(path, 'game_ffb_level') == 100
    assert engine.last_stop == 'state' and engine.status.reason == why


def test_a_stall_over_50_ms_restores_the_game(tmp_path):
    engine, path, t = active(tmp_path)
    last = t - 1 / HZ
    engine.step(last + 0.04)
    assert levels() == [0]                      # 40 ms: still ours (the driver holds the last value)
    engine.step(last + 0.055)
    assert levels() == [0, 100] and engine.last_stop == 'stall'
    # Back, it needs a clean second (the gap is in the window) before it takes over again
    t = run(engine, last + 0.1, 0.5)
    assert levels() == [0, 100]
    run(engine, t, 1.0)
    assert levels() == [0, 100, 0]


def test_a_device_change_restores_the_old_wheel(tmp_path):
    engine, path, t = active(tmp_path)
    other = wheel(tmp_path, 'other')
    engine.set_device(other)
    run(engine, t, 0.5)
    assert levels(path) == [0, 100] and read(path, 'game_ffb_level') == 100
    engine.set_device(None)
    engine.step(t + 1.0)
    assert levels(other) in ([], [0, 100])


def test_quitting_restores_the_game(tmp_path):
    engine, path, t = active(tmp_path)
    engine.stop()
    assert levels() == [0, 100] and read(path, 'game_ffb_level') == 100


def test_a_failed_write_restores_the_game(tmp_path):
    engine, path, t = active(tmp_path)
    FakeDriver.fail_force = True
    run(engine, t, 0.1)
    assert levels() == [0, 100] and engine.last_stop == 'write'
    assert not engine.status.available
    FakeDriver.fail_force = False
    run(engine, t + 0.1, 2.0)
    assert levels() == [0, 100]                 # not again until the device is set again


def test_a_game_left_muted_is_restored_when_the_wheel_is_taken(tmp_path):
    engine = fe.FfbEngine(driver=FakeDriver)
    path = wheel(tmp_path, level=0)
    engine.set_device(path)
    engine.step(0.0)
    assert levels() == [100] and read(path, 'game_ffb_level') == 100


def test_not_replace_does_nothing_but_says_why(tmp_path):
    engine, path = engine_on(tmp_path, raw=True)
    t = 0.0
    for i in range(int(8 * 236)):
        engine.offer(t, acr(t, game='rbr', v5=False))
        engine.step(t)
        t += 1 / 236.0
    assert FakeDriver.log == []
    assert engine.status[:2] == ('model', 'rbr-model')
    light, text = fe.status_text(engine.status)
    assert light == 'amber' and 'force feedback' in text


def test_a_driver_without_replace_mode_is_never_written(tmp_path):
    d = tmp_path / 'old'
    d.mkdir()
    engine = fe.FfbEngine(driver=FakeDriver)
    engine.set_device(str(d))
    engine.set_enabled(True)
    run(engine, 0.0, 8.0)
    assert FakeDriver.log == [] and not engine.status.available
    assert fe.status_text(engine.status)[0] == 'off'


def test_status_text_per_state(tmp_path):
    engine, path = engine_on(tmp_path)
    assert fe.status_text(engine.status, listening=True)[1] == 'No game telemetry'
    t = run(engine, 0.0, 2.0)
    light, text = fe.status_text(engine.status)
    assert light == 'green' and 'drive a few corners' in text
    run(engine, t, 6.0)
    light, text = fe.status_text(engine.status)
    assert light == 'green' and "Countersteer's force" in text
    assert fe.status_text(fe.IDLE_STATUS)[1] == 'No wheel'


# -- the driver files --

def test_ext_force_is_one_open_file_written_at_offset_0(tmp_path, monkeypatch):
    path = wheel(tmp_path)
    opened = []
    real_open = fe.os.open
    monkeypatch.setattr(fe.os, 'open', lambda p, *a: (opened.append(p), real_open(p, *a))[1])
    driver = fe.DriverReplace(path)
    assert driver.available() and not driver.has_game_ffb_raw()
    for v in (-12000, 5, 32767, -3):
        driver.force(v)
        assert read(path, 'ext_force') == v
    assert opened == [path + '/ext_force']
    driver.set_game_level(0)
    assert read(path, 'game_ffb_level') == 0 and driver.game_level() == 0
    driver.close()


# -- the listener is never held up --

def test_the_listener_never_waits_for_the_engine(tmp_path):
    """The real thread, made to take 5 ms a sample (15 times slower than the stream): offer() stays O(1),
    nothing queues, the engine skips to the newest sample and still counts every arrival for the rate."""
    engine = fe.FfbEngine(driver=FakeDriver)
    engine.set_device(wheel(tmp_path))
    engine.set_enabled(True)
    feed = engine.classifier.feed
    seen = []

    def slow(t, sample, arrivals=1):
        seen.append(arrivals)
        time.sleep(0.005)
        feed(t, sample, arrivals)
    engine.classifier.feed = slow
    engine.start()
    try:
        samples = [acr(i / HZ) for i in range(20000)]
        worst = 0.0
        start = time.perf_counter()
        for sample in samples:
            a = time.perf_counter()
            engine.offer(time.monotonic(), sample)
            worst = max(worst, time.perf_counter() - a)
        spent = time.perf_counter() - start
        deadline = time.monotonic() + 2.0
        while engine._seen != engine._seq and time.monotonic() < deadline:
            time.sleep(0.005)
    finally:
        engine.stop()
    assert worst < 0.005                         # never the engine's 5 ms
    assert spent < 2.0
    assert engine.skipped > 10000 and len(seen) < 10000
    assert sum(seen) == 20000                     # every arrival counted, though most were never looked at


def test_the_listener_hands_every_decoded_sample_over():
    telemetry = Telemetry(NoLeds(), 5310)
    engine = fe.FfbEngine()
    telemetry.ffb = engine
    for i in range(5):
        s = acr(i / HZ)
        s.accel, s.stage_length = (0.0, 0.0, 0.0), 5000.0
        telemetry.handle(100.0 + i / HZ, ovst_packet(s, 1000 + i), ('127.0.0.1', 1))
    seq, t, sample = engine._slot
    assert seq == 5 and t == pytest.approx(100.0 + 4 / HZ)
    assert sample.packet_id == 1004 and sample.tyre_mz is not None


def test_the_thread_stops_writing_on_a_stall(tmp_path):
    """The real thread on a clock the test holds: samples stop, 30 ms on it is still ours, past 50 ms the
    game's force is back within a few polls."""
    clock = [0.0]
    path = wheel(tmp_path)
    engine = fe.FfbEngine(clock=lambda: clock[0], driver=FakeDriver)
    engine.set_device(path)
    engine.set_enabled(True)
    last = run(engine, 0.0, 8.0) - 1 / HZ
    assert engine.status.active
    clock[0] = last + 0.03
    engine.start()
    try:
        time.sleep(0.1)                          # ten polls
        assert engine.status.active and levels() == [0]
        clock[0] = last + 0.06
        deadline = time.monotonic() + 1.0
        while engine.last_stop != 'stall' and time.monotonic() < deadline:
            time.sleep(0.001)
        assert engine.last_stop == 'stall'
        assert time.monotonic() < deadline - 0.8  # within 200 ms of the stall
        assert levels() == [0, 100] and read(path, 'game_ffb_level') == 100
    finally:
        engine.stop()


def test_profiles_keep_the_switch(tmp_path):
    from oversteer.model import Model
    from tests.fake_wheel import FakeDevice, FakeUi
    device = FakeDevice(ext_force_support=True)
    model = Model(device, None)
    assert model.data['countersteer_ffb'] is False
    model.data['countersteer_ffb'] = True
    profile = str(tmp_path / 'p.ini')
    model.save(profile)
    fresh = Model(device, None)
    fresh.load(profile)
    assert fresh.get_countersteer_ffb() is True
    (tmp_path / 'old.ini').write_text('[DEFAULT]\nrange = 900\n')
    fresh.load(str(tmp_path / 'old.ini'))
    assert fresh.data['countersteer_ffb'] is False
    none = Model(FakeDevice(), None)
    assert none.data['countersteer_ffb'] is None
