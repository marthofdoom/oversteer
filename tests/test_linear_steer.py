"""Forza's "Linear steering at speed": the gain curve, the file the engine hands the proxy, the proxy's scaling of the
steering axis, the engine's side and the frames the aligning spring and the lock stop work in. No uinput device, no
proxy daemon: pure functions, tmp files and fakes."""

import os
import struct

import math

import pytest
from evdev import ecodes

from oversteer import ffb_engine as fe, ffb_forza as fz
from oversteer.model import Model
from oversteer.proxy import steer_gain as sg
from oversteer.proxy.spec import ProxySpec
from tests.test_ffb_engine import FakeDriver, _clean_log  # noqa: F401 (the autouse fixture)
from tests.test_ffb_forza import Drive, engine_on, fsample


# -- the curve --

def test_gain_is_one_at_low_speed_and_about_three_at_50():
    assert fz.linear_steer_gain(0.0) == 1.0 and fz.linear_steer_gain(fz.STEER_KNEE) == 1.0
    assert fz.linear_steer_gain(3.0) == 1.0                              # never lowers the steering
    assert fz.linear_steer_gain(50.0) == pytest.approx(0.72 / 0.25, abs=0.05)      # what the recordings show
    assert fz.linear_steer_gain(-50.0) == fz.linear_steer_gain(50.0)
    speeds = [6, 10, 14, 20, 30, 40, 50, 60]
    gains = [fz.linear_steer_gain(v) for v in speeds]
    assert gains == sorted(gains)


def test_gain_is_capped_and_safe_for_nonsense():
    assert fz.linear_steer_gain(1e6) == fz.STEER_GAIN_MAX == sg.GAIN_MAX
    for bad in (float('nan'), float('inf'), None):
        assert fz.linear_steer_gain(bad) == 1.0


# -- the axis --

def test_scale_steer_about_the_centre_clamped_both_signs():
    assert sg.scale_steer(12345, 1.0, 0, 65535) == 12345                 # identity, untouched
    assert sg.scale_steer(32767, 2.0, 0, 65535) == 32766                 # (the centre of 0..65535 is 32767.5)
    assert sg.scale_steer(32768, 2.0, 0, 65535) == 32768
    assert sg.scale_steer(40000, 2.0, 0, 65535) == 47232
    assert sg.scale_steer(20000, 2.0, 0, 65535) == 7232
    assert sg.scale_steer(60000, 2.0, 0, 65535) == 65535                 # clamped
    assert sg.scale_steer(1000, 3.0, 0, 65535) == 0
    assert sg.scale_steer(-90, 2.0, -128, 127) == -128


# -- the file --

class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def pair(tmp_path):
    clock = Clock()
    path = str(tmp_path / 'steer-gain')
    writer = sg.GainWriter(path, clock=clock)
    reader = sg.GainReader(path, clock=clock)
    return writer, reader, clock, path


def test_round_trip(pair):
    writer, reader, clock, path = pair
    assert reader.read() == 1.0                                          # no file
    assert writer.write(2.5)
    assert os.path.getsize(path) == 16
    clock.t += 1.5                                                       # (past the reader's look-again)
    writer.write(2.5)
    assert reader.read() == 2.5 and reader.active
    writer.write(3.25)
    assert reader.read() == 3.25


def test_stale_gain_is_one(pair):
    writer, reader, clock, path = pair
    writer.write(2.0)
    clock.t += 1.1
    writer.write(2.0)
    assert reader.read() == 2.0
    clock.t += sg.STALE_S + 0.01
    assert reader.read() == 1.0                                          # the engine stopped writing
    writer.write(2.0)
    assert reader.read() == 2.0


def test_a_removed_file_is_forgotten_and_a_recreated_one_found(pair):
    writer, reader, clock, path = pair
    writer.write(2.0)
    clock.t += 1.1
    writer.write(2.0)
    assert reader.read() == 2.0
    writer.remove()
    assert not os.path.exists(path)
    assert reader.read() == 1.0 and reader.active                        # (1.0 was written first; it looks again once a second)
    clock.t += 1.1
    assert reader.read() == 1.0 and not reader.active
    writer.write(1.7)
    assert reader.read() == 1.0                                          # not yet
    clock.t += 1.1
    writer.write(1.7)
    assert reader.read() == 1.7


@pytest.mark.parametrize('content', [b'', b'\0' * 8, b'\0' * 17, struct.pack('<dd', 2.0, 1000.0) + b'x'])
def test_wrong_size_is_one(pair, content):
    _writer, reader, clock, path = pair
    with open(path, 'wb') as f:
        f.write(content)
    assert reader.read() == 1.0 and not reader.active


@pytest.mark.parametrize('gain', [float('nan'), float('inf'), 0.5, 0.0, -2.0, 5.5, 1e300])
def test_a_gain_that_cannot_be_right_is_one(pair, gain):
    _writer, reader, clock, path = pair
    with open(path, 'wb') as f:
        f.write(struct.pack('<dd', gain, clock.t))
    assert reader.read() == 1.0


def test_a_stamp_from_nowhere_is_one(pair):
    _writer, reader, clock, path = pair
    for stamp in (float('nan'), clock.t + 100.0):
        with open(path, 'wb') as f:
            f.write(struct.pack('<dd', 2.0, stamp))
        clock.t += 1.1
        assert reader.read() == 1.0


def test_wrong_owner_is_one(tmp_path):
    clock = Clock()
    path = str(tmp_path / 'g')
    writer = sg.GainWriter(path, clock=clock)
    writer.write(2.0)
    mine = os.stat(path).st_uid
    assert sg.GainReader(path, uids=lambda: {mine}, clock=clock).read() == 2.0
    assert sg.GainReader(path, uids=lambda: {mine + 1, 0 if mine else 1}, clock=clock).read() == 1.0


def test_a_symlink_is_not_followed(tmp_path):
    clock = Clock()
    real, link = str(tmp_path / 'real'), str(tmp_path / 'link')
    sg.GainWriter(real, clock=clock).write(2.0)
    os.symlink(real, link)
    assert sg.GainReader(link, clock=clock).read() == 1.0
    assert sg.GainWriter(link, clock=clock).write(2.0) is False          # nor does the writer


def test_a_torn_read_keeps_the_last_good_gain(pair, monkeypatch):
    writer, reader, clock, path = pair
    writer.write(2.0)
    assert reader.read() == 2.0

    calls = []

    def torn(fd, size, offset):
        calls.append(1)
        return sg.LAYOUT.pack(4.0 if len(calls) % 2 else 2.0, clock.t)

    monkeypatch.setattr(sg.os, 'pread', torn)
    assert reader.read() == 2.0                                          # the two reads disagree: the last good one


def test_the_writer_never_raises(tmp_path):
    blocked = tmp_path / 'file'
    blocked.write_text('x')
    writer = sg.GainWriter(str(blocked / 'sub' / 'g'))                   # a parent that is a file
    assert writer.write(2.0) is False
    writer.remove()
    assert sg.GainReader(str(blocked / 'sub' / 'g')).read() == 1.0


# -- the proxy --

def _spec(ff=True):
    return ProxySpec.from_dict({
        "id": "t", "name": "t", "passthrough": "wheel", "ff": {"source": "wheel"} if ff else None,
        "identity": {"name": "T", "vendor": "046d", "product": "c24f", "version": "0111", "bustype": "usb"},
        "sources": {"wheel": {"match": {"vendor": "046d", "product": "c24f"}}},
        "capabilities": {"abs": {}}, "mappings": []})


class Info:
    min, max = 0, 65535


class Ev:
    def __init__(self, etype, code, value):
        self.type, self.code, self.value = etype, code, value


class Source:
    path = '/dev/null'
    absinfo = {ecodes.ABS_X: Info(), ecodes.ABS_Z: Info()}

    def __init__(self):
        self.events = []
        self.device = self

    def read(self):
        events, self.events = self.events, []
        return events


class Ui:
    def __init__(self):
        self.out = []

    def write(self, etype, code, value):
        self.out.append((etype, code, value))

    def syn(self):
        self.out.append('syn')


def _device(tmp_path, ff=True):
    from oversteer.proxy.device import ProxyDevice
    dev = ProxyDevice(_spec(ff))
    dev.sources['wheel'] = Source()
    dev.ui = Ui()
    path = str(tmp_path / 'steer-gain')
    dev._steer_reader = sg.GainReader(path)
    return dev, sg.GainWriter(path), path


def _feed(dev, *events):
    dev.sources['wheel'].events = [Ev(*e) for e in events]
    dev._handle_source_events('wheel')
    out, dev.ui.out = dev.ui.out, []
    return out


X, Z, ABS = ecodes.ABS_X, ecodes.ABS_Z, ecodes.EV_ABS


def test_proxy_without_a_file_passes_everything_as_it_came(tmp_path):
    dev, _writer, _path = _device(tmp_path)
    assert _feed(dev, (ABS, X, 40000), (ABS, Z, 123)) == [(ABS, X, 40000), (ABS, Z, 123), 'syn']
    assert dev.status()['steer_gain'] is True


def test_proxy_scales_the_steering_axis_only(tmp_path):
    dev, writer, _path = _device(tmp_path)
    writer.write(2.0)
    assert _feed(dev, (ABS, X, 40000), (ABS, Z, 40000), (ecodes.EV_KEY, 300, 1)) == \
        [(ABS, X, 47232), (ABS, Z, 40000), (ecodes.EV_KEY, 300, 1), 'syn']
    assert _feed(dev, (ABS, X, 32768))[0] == (ABS, X, 32768)             # the centre stays
    writer.write(1.0)
    assert _feed(dev, (ABS, X, 40000))[0] == (ABS, X, 40000)


def test_proxy_scales_nothing_without_a_force_feedback_wheel(tmp_path):
    dev, writer, _path = _device(tmp_path, ff=False)
    writer.write(2.0)
    assert _feed(dev, (ABS, X, 40000))[0] == (ABS, X, 40000)


def test_proxy_re_emits_the_steering_when_the_gain_moves_at_rest(tmp_path, monkeypatch):
    dev, writer, _path = _device(tmp_path)
    assert dev._steer_raw is None
    dev._steer_tick()
    assert dev.ui.out == []                                              # nothing to re-emit before a first value
    writer.write(1.0)
    _feed(dev, (ABS, X, 40000))
    clock = {'t': 10_000.0}
    monkeypatch.setattr('oversteer.proxy.device.time.monotonic', lambda: clock['t'])
    writer.write(2.0)
    dev._steer_tick()
    assert dev.ui.out == [(ABS, X, 47232), 'syn']
    dev.ui.out = []
    dev._steer_tick()                                                    # not twice inside 10 ms
    clock['t'] += 0.02
    dev._steer_tick()
    assert dev.ui.out == []                                              # the same gain: nothing
    writer.write(2.002)
    clock['t'] += 0.02
    dev._steer_tick()
    assert dev.ui.out == []                                              # under 0.005: nothing
    writer.write(2.5)
    clock['t'] += 0.02
    dev._steer_tick()
    assert dev.ui.out == [(ABS, X, int(round(32767.5 + (40000 - 32767.5) * 2.5))), 'syn']
    dev.ui.out = []
    writer.remove()                                                      # file gone: back to the raw value, once the reader looks
    clock['t'] += 1.1
    dev._steer_tick()
    assert dev.ui.out == [(ABS, X, 40000), 'syn']


# -- the engine --

@pytest.fixture
def gain_file(tmp_path, monkeypatch):
    path = str(tmp_path / 'steer-gain')
    monkeypatch.setattr(sg, 'PATH', path)
    return path


def _gain(path):
    return sg.GainReader(path).read()


def test_engine_hands_the_speed_gain_over_and_cleans_up(tmp_path, gain_file):
    engine, _path = engine_on(tmp_path)
    engine.set_forza_linear_steer(True)
    drive = Drive(engine, speed=40.0).run(3.0)
    assert _gain(gain_file) == pytest.approx(fz.linear_steer_gain(40.0), abs=1e-6)
    assert engine._lsteer_applied == pytest.approx(fz.linear_steer_gain(40.0), abs=1e-6)
    engine.set_forza_linear_steer(False)
    drive.run(0.01)
    assert not os.path.exists(gain_file) and engine._lsteer_applied == 1.0


def test_engine_does_nothing_with_the_option_off(tmp_path, gain_file):
    engine, _path = engine_on(tmp_path)
    Drive(engine, speed=40.0).run(2.0)
    assert not os.path.exists(gain_file)


def test_engine_ramps_to_one_when_the_game_is_not_running(tmp_path, gain_file):
    engine, _path = engine_on(tmp_path)
    engine.set_forza_linear_steer(True)
    drive = Drive(engine, speed=50.0).run(2.0)
    high = _gain(gain_file)
    assert high > 2.5
    drive.extra = lambda s, t: setattr(s, 'running', False)              # a pause or a menu
    drive.run(0.25)
    mid = _gain(gain_file)
    assert 1.0 < mid < high and mid == pytest.approx(high - 4.0 * 0.25, abs=0.2)     # 4 per second, no jump
    drive.run(1.0)
    assert _gain(gain_file) == 1.0


def test_engine_takes_the_gain_away_when_samples_stop_or_the_game_changes(tmp_path, gain_file):
    engine, _path = engine_on(tmp_path)
    engine.set_forza_linear_steer(True)
    drive = Drive(engine, speed=50.0).run(2.0)
    assert _gain(gain_file) > 2.5
    drive.telemetry = False
    drive.run(1.5)                                                       # no samples: LSTEER_QUIET, then the ramp
    assert engine._lsteer_gain == 1.0
    drive.telemetry = True
    drive.run(1.0)
    assert _gain(gain_file) > 2.5
    other = fsample(drive.t, speed=50.0)
    other.game = 'acc'
    engine._lsteer_sample(drive.t, other)
    assert not os.path.exists(gain_file) and engine._lsteer_applied == 1.0


def test_engine_stop_removes_the_file(tmp_path, gain_file):
    engine, _path = engine_on(tmp_path)
    engine.set_forza_linear_steer(True)
    Drive(engine, speed=50.0).run(1.0)
    assert os.path.exists(gain_file)
    engine._lsteer_off()
    assert not os.path.exists(gain_file)


def test_engine_with_no_writable_place_still_runs_and_assumes_no_gain(tmp_path, monkeypatch):
    blocked = tmp_path / 'f'
    blocked.write_text('x')
    monkeypatch.setattr(sg, 'PATH', str(blocked / 'g'))
    engine, _path = engine_on(tmp_path)
    engine.set_forza_linear_steer(True)
    Drive(engine, speed=50.0).run(1.0)
    assert engine._lsteer_applied == 1.0                                 # nothing stored: the frames stay the wheel's


# -- the frames --

def test_the_aligning_spring_is_in_the_wheels_frame(tmp_path, gain_file):
    from tests.test_ffb_forza import dyn_engine
    engine, _path, pos = dyn_engine(tmp_path)
    Drive(engine).run(8.0)
    rack = engine.forza_rack
    s = fsample(8.0, speed=25.0, ay=3.0, yaw=0.1)
    s.steer = 0.2
    seen = []
    rack.align_writer = lambda theta_a, k, tau: seen.append((theta_a, k))
    rack.align_on, rack.wheel_range = True, 900.0
    rack.steer_gain = 1.0
    rack.force(s)
    rack.steer_gain = 2.0
    rack.force(s)
    (a1, k1), (a2, k2) = seen[-2:]
    assert abs(a1) > 1.0 and abs(a1) < 450.0
    # only the position divides by the gain: the front wheels turn as the wheel does, so the slip's share of the target
    # and the stiffness per degree of wheel stay the low-speed ratio's
    assert k2 == pytest.approx(k1, rel=1e-6)
    al = rack.align(s)
    g = 450.0 / (rack.lock * rack.ratio * 180.0 / math.pi) * rack.wheel_slope
    assert abs(a1 - a2) == pytest.approx(abs(al['theta_deg'] * g / 2.0), rel=1e-6)


def test_forza_does_not_learn_its_lock_through_the_gain(tmp_path, gain_file):
    from tests.test_ffb_forza import dyn_engine
    engine, _path, _pos = dyn_engine(tmp_path)
    rack = engine.forza_rack
    learnt = []
    rack._learn_ratio = lambda sample, dt: learnt.append(sample)
    rack.steer_gain = 2.0
    rack._learn_lock(1.0, fsample(1.0, speed=25.0, ay=1.0, yaw=0.1), 0.01)
    assert not learnt
    rack.steer_gain = 1.0
    rack._learn_lock(1.1, fsample(1.1, speed=25.0, ay=1.0, yaw=0.1), 0.01)
    assert learnt


def test_the_engine_gives_the_rack_the_gain_only_for_forza(tmp_path, gain_file):
    from tests.test_ffb_forza import dyn_engine
    engine, _path, _pos = dyn_engine(tmp_path)
    engine.set_forza_linear_steer(True)
    Drive(engine, speed=40.0).run(3.0)
    assert engine.forza_rack.steer_gain == pytest.approx(fz.linear_steer_gain(40.0), abs=1e-6)
    assert engine.model.steer_gain == 1.0 and engine.rbr_rack.steer_gain == 1.0


class VirtualPos:
    """The proxy's device: the wheel's position times the gain, as the game sees it."""
    virtual = True

    def __init__(self, raw):
        self.raw = raw

    def __call__(self):
        return self.raw


def test_a_position_read_from_the_proxys_device_is_taken_back_to_the_wheel(tmp_path, gain_file):
    from tests.test_ffb_forza import dyn_engine
    engine, _path, _pos = dyn_engine(tmp_path)
    Drive(engine, speed=40.0).run(1.0)
    engine._pos_src = VirtualPos(int(round((100.0 / 900.0 + 0.5) * 65535)))       # 100 degrees in the game's frame
    stage = engine._dyn
    engine._pos_cache = None
    assert engine._wheel_position(50.0, stage)[0] == pytest.approx(100.0, abs=0.05)
    engine._lsteer_applied = 2.0
    engine._pos_cache = None
    assert engine._wheel_position(51.0, stage)[0] == pytest.approx(50.0, abs=0.05)
    engine._pos_src = (lambda: engine._pos_src.raw)                                # the wheel's own node: as it is
    engine._pos_src.raw = int(round((100.0 / 900.0 + 0.5) * 65535))
    engine._pos_cache = None
    assert engine._wheel_position(52.0, stage)[0] == pytest.approx(100.0, abs=0.05)


def test_the_lock_stop_stands_down_where_it_cannot_see_the_wheels_end(tmp_path, gain_file):
    from tests.test_ffb_forza import dyn_engine
    engine, _path, _pos = dyn_engine(tmp_path)
    drive = Drive(engine).run(8.0)
    s = fsample(drive.t)
    s.steer = 1.0
    engine._pos_src = VirtualPos(int(round((470.0 / 900.0 + 0.5) * 65535)))
    engine._pos_cache = None
    assert engine._lock_stop_force(drive.t, s) > 0.3 * fe.FULL           # no gain: as before
    engine._lsteer_applied = 3.0
    engine._pos_cache = None
    assert engine._lock_stop_force(drive.t + 0.01, s) == 0.0


def test_the_position_reader_says_whether_it_reads_the_proxys_device():
    from oversteer import ffb_dynamics

    class Dev:
        dev_name = '/dev/input/event3'
        proxied = '/dev/input/event9'
        node = '/dev/input/event3'

        def _input_node(self):
            return self.node

        def _normalized_axis(self, code, value):
            return (0, value)

    class Opened:
        def __init__(self, node):
            pass

        def absinfo(self, code):
            class A:
                min, max, value = 0, 65535, 32767
            return A

    dev = Dev()
    read = ffb_dynamics.position_source(dev, opener=Opened)
    assert read.virtual is False
    orig = os.path.exists
    os.path.exists = lambda p: False if str(p).startswith('/dev/input/event') else orig(p)
    try:
        read()
        assert read.virtual is False
        dev.node = dev.proxied
        read()
        assert read.virtual is True
    finally:
        os.path.exists = orig


# -- the setting --

def test_the_setting_round_trips_in_the_model(tmp_path):
    from tests.fake_wheel import FakeDevice
    assert Model.types['countersteer_forza_linear_steer'] == 'boolean'
    assert Model._texture_defaults(True)['countersteer_forza_linear_steer'] is False
    assert Model._texture_defaults(False)['countersteer_forza_linear_steer'] is None
    model = Model(FakeDevice(ext_force_support=True), None)
    assert model.data['countersteer_forza_linear_steer'] is False and model.get_countersteer_forza_linear_steer() is False
    model.set_countersteer_forza_linear_steer(True)
    assert model.get_countersteer_forza_linear_steer() is True
    profile = str(tmp_path / 'p.ini')
    model.save(profile)
    fresh = Model(FakeDevice(ext_force_support=True), None)
    fresh.load(profile)
    assert fresh.get_countersteer_forza_linear_steer() is True
    (tmp_path / 'old.ini').write_text('[DEFAULT]\nrange = 900\ncountersteer_ffb = 1\n')
    fresh.load(str(tmp_path / 'old.ini'))
    assert fresh.get_countersteer_forza_linear_steer() is False


def test_the_setting_reaches_the_engine_process():
    from oversteer import ffb_process
    assert 'set_forza_linear_steer' in ffb_process.SETTERS and hasattr(ffb_process.EngineProcess, 'set_forza_linear_steer')


def test_the_daemon_copy_carries_the_module():
    from oversteer.proxy import install
    assert 'proxy/steer_gain.py' in install.DAEMON_FILES
