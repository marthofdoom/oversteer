"""Auto port: one listener on every preset port and the legacy one, locked on the first that brings a known game.

Real UDP sockets on localhost, on ephemeral ports put in place of the presets, so nothing here collides with a
running game or Oversteer."""
import errno
import glob
import os
import socket
import time

import pytest

from oversteer import telemetry as module
from oversteer.telemetry import AUTO, Telemetry
from tests.test_telemetry import FakeLeds, forza, ovst


def _free_ports(n):
    held = []
    for _ in range(n):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(('0.0.0.0', 0))
        held.append(s)
    ports = [s.getsockname()[1] for s in held]
    for s in held:
        s.close()
    return ports


@pytest.fixture
def ports(monkeypatch):
    """Four free ports as the three presets and the legacy port."""
    found = _free_ports(4)
    monkeypatch.setattr(module, 'PORT_PRESETS', [(p, 'preset') for p in found[:3]])
    monkeypatch.setattr(module, 'LEGACY_PORT', found[3])
    return found


def _send(port, data, times=1, gap=0.0):
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _ in range(times):
            out.sendto(data, ('127.0.0.1', port))
            if gap:
                time.sleep(gap)
    finally:
        out.close()


def _wait(condition, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def _bindable(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(('0.0.0.0', port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _fds():
    return len(glob.glob('/proc/self/fd/*'))


def test_auto_ports_are_the_presets_then_legacy_and_never_forza_or_the_web_port():
    assert module.auto_ports() == [5310, 20777, 4444, 5300]
    assert module.auto_ports(avoid=(20777,)) == [5310, 4444, 5300]
    assert not any(p in module.FORZA_OWN for p in module.auto_ports())


def test_auto_binds_every_free_port(ports):
    t = Telemetry(FakeLeds(), port=AUTO)
    assert t.port is None and t.auto
    try:
        assert t.start()
        assert t.listening == ports and t.busy == {}
        assert not any(_bindable(p) for p in ports)           # all held by the one listener
    finally:
        t.stop()


def test_a_held_port_is_skipped_and_reported(ports):
    from oversteer.telemetry_view import listening_status
    other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    other.bind(('0.0.0.0', ports[1]))                         # SimHub on 20777
    t = Telemetry(FakeLeds(), port=AUTO)
    try:
        assert t.start()
        assert t.listening == [ports[0], ports[2], ports[3]]
        assert list(t.busy) == [ports[1]] and t.busy[ports[1]].errno == errno.EADDRINUSE
        text = listening_status(t.listening, t.busy)
        assert text == "Listening on UDP {}, {}, {} ({} busy: another app has it)".format(
            ports[0], ports[2], ports[3], ports[1])
    finally:
        t.stop()
        other.close()


def test_every_port_held_fails_to_start(ports):
    held = []
    for p in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(('0.0.0.0', p))
        held.append(s)
    t = Telemetry(FakeLeds(), port=AUTO)
    try:
        assert t.start() is False and set(t.busy) == set(ports) and t.listening == []
    finally:
        t.stop()
        for s in held:
            s.close()


def test_garbage_never_locks(ports):
    t = Telemetry(FakeLeds(), port=AUTO)
    try:
        assert t.start()
        _send(ports[0], b'\x00' * 50, times=20)
        _send(ports[1], b'not telemetry', times=20)
        time.sleep(0.2)
        assert t.locked is None and t.active_port is None and t.live is None
        _send(ports[2], forza(4000), times=3, gap=0.01)       # then a game
        assert _wait(lambda: t.active_port == ports[2])
    finally:
        t.stop()


def test_the_first_game_locks_and_a_second_port_is_ignored(ports):
    statuses = []
    t = Telemetry(FakeLeds(), port=AUTO, on_status=statuses.append)
    try:
        assert t.start()
        _send(ports[1], forza(4000), times=3, gap=0.01)
        assert _wait(lambda: t.active_port == ports[1])
        assert t.locked[0] == ports[1] and t.locked[2] == 'forza-fh' and t.udp_port == ports[1]
        assert statuses == ['127.0.0.1']
        for _ in range(20):                                   # a bridge on another port, the game still going
            _send(ports[0], ovst(6000, 8500))
            _send(ports[1], forza(4100))
            time.sleep(0.005)
        assert _wait(lambda: t.dropped.get(ports[0], 0) == 20)
        assert t.active_port == ports[1] and t.live.game == 'forza-fh'
    finally:
        t.stop()


class _Learner:
    def __init__(self):
        self.fed, self.new_sources, self.idles = [], 0, 0

    def feed(self, now, sample, *args):
        self.fed.append(sample.game)

    def shift_rpm(self, gear):
        return None

    def idle(self):
        self.idles += 1

    def tick(self, now):
        pass

    def new_source(self):
        self.new_sources += 1


def test_another_port_takes_over_only_after_silence():
    """The live path with its own clock: idle frees the lock after IDLE_TIMEOUT, another port may take it once
    the locked one has been quiet for LOCK_SILENCE, and the switch ends the learner's session."""
    learner = _Learner()
    t = Telemetry(FakeLeds(), port=AUTO, learner=learner)
    game, bridge = 20777, 5310
    addr = ('127.0.0.1', 40000)
    for i in range(10):
        t.handle(100.0 + i * 0.01, forza(4000), addr, game)
        t.handle(100.0 + i * 0.01, ovst(6000, 8500), addr, bridge)     # busier or not, it is ignored
    assert t.active_port == game and t.dropped[bridge] == 10 and set(learner.fed) == {'forza-fh'}
    last = 100.09
    t.handle(last + module.IDLE_TIMEOUT + 0.1, ovst(6000, 8500), addr, bridge)    # idle: lock let go...
    assert t.locked is None and learner.idles == 1
    assert t.dropped[bridge] == 11                                               # ...but not to another port yet
    t.handle(last + module.LOCK_SILENCE - 0.1, ovst(6000, 8500), addr, bridge)
    assert t.locked is None
    t.handle(last + module.LOCK_SILENCE + 0.01, ovst(6000, 8500), addr, bridge)
    assert t.active_port == bridge and t.live.game == 'acpmf' and learner.new_sources == 1
    for i in range(5):                                                           # the game is back: ignored now
        t.handle(104.0 + i * 0.01, forza(4000), addr, game)
        t.handle(104.0 + i * 0.01, ovst(6000, 8500), addr, bridge)
    assert t.active_port == bridge and t.dropped[game] == 5


def test_the_same_port_back_after_a_pause_keeps_its_session():
    learner = _Learner()
    t = Telemetry(FakeLeds(), port=AUTO, learner=learner)
    addr = ('127.0.0.1', 40000)
    t.handle(100.0, forza(4000), addr, 20777)
    t.handle(102.5, ovst(6000, 8500), addr, 5310)             # idle at 2 s: dropped all the same
    t.handle(102.6, forza(4000), addr, 20777)                 # a pause in the menus: the game takes it again
    assert t.active_port == 20777 and learner.new_sources == 0


def test_takeover_through_the_sockets(ports, monkeypatch):
    monkeypatch.setattr(module, 'IDLE_TIMEOUT', 0.2)
    monkeypatch.setattr(module, 'LOCK_SILENCE', 0.3)
    t = Telemetry(FakeLeds(), port=AUTO)
    try:
        assert t.start()
        _send(ports[3], forza(4000), times=3, gap=0.01)
        assert _wait(lambda: t.active_port == ports[3])
        _send(ports[0], ovst(6000, 8500), times=60, gap=0.01)  # keeps sending after the game stopped
        assert _wait(lambda: t.active_port == ports[0])
        assert t.live.game == 'acpmf'
    finally:
        t.stop()


def test_fixed_mode_listens_on_its_port_alone(ports):
    t = Telemetry(FakeLeds(), port=ports[0])
    try:
        assert t.start()
        assert t.listening == [ports[0]] and not t.auto
        assert all(_bindable(p) for p in ports[1:])
        _send(ports[0], forza(4000), times=3, gap=0.01)
        assert _wait(lambda: t.live is not None)
        assert t.active_port == ports[0] and t.udp_port == ports[0]
    finally:
        t.stop()


def test_stop_closes_every_socket_and_restarts_cleanly(ports):
    before = _fds()
    for _ in range(5):
        t = Telemetry(FakeLeds(), port=AUTO)
        assert t.start()
        _send(ports[0], forza(4000))
        t.stop()
        assert t._thread is None and t._socks == {} and t._selector is None
        assert all(_bindable(p) for p in ports)
    assert _fds() == before
    t = Telemetry(FakeLeds(), port=AUTO)                       # stop/start of the same listener
    assert t.start()
    t.stop()
    assert t.start() and t.listening == ports
    t.stop()
    assert all(_bindable(p) for p in ports) and _fds() == before


def test_capture_records_the_active_port_only(tmp_path):
    from oversteer import telemetry_capture as tc
    recorder = tc.Recorder(str(tmp_path), version='test', threaded=False, clock=lambda: 1.7e9)
    t = Telemetry(FakeLeds(), port=AUTO)
    t.recorder = recorder
    addr = ('127.0.0.1', 40000)
    t.handle(100.0, b'garbage', addr, 5310)                   # before any lock: not recorded
    t.handle(100.1, forza(4000), addr, 20777)                 # locks: the capture starts here
    t.handle(100.2, ovst(6000, 8500), addr, 5310)             # another port: dropped, not recorded
    t.handle(100.3, b'unknown', addr, 20777)                  # the active port: everything on it
    t.handle(100.4, forza(4100), addr, 20777)
    t.handle(104.0, ovst(6000, 8500), addr, 5310)             # takes over: a new file, with its port
    t.handle(104.1, ovst(6100, 8500), addr, 5310)
    recorder.close()
    files = sorted(glob.glob(os.path.join(str(tmp_path), '*' + tc.SUFFIX)))
    assert len(files) == 2
    found = []
    for path in files:
        meta, records = tc.read_capture(path)
        found.append((meta['port'], [data for _t, _a, data in records]))
    found.sort(key=lambda f: len(f[1]), reverse=True)
    assert found[0] == (20777, [forza(4000), b'unknown', forza(4100)])
    assert found[1] == (5310, [ovst(6000, 8500), ovst(6100, 8500)])


def test_status_texts():
    from oversteer.telemetry_view import listening_status, receiving_status
    assert receiving_status('dirt', 20777) == "Receiving DiRT Rally on UDP 20777"
    assert listening_status([5310, 4444], {5300: OSError(errno.EADDRINUSE, 'in use')}) == \
        "Listening on UDP 5310, 4444 (5300 busy: Forza Horizon 6 may hold it)"
    assert listening_status([], {5310: OSError(errno.EACCES, 'no')}) == \
        "No telemetry port free (5310 busy: no permission)"


def _model():
    from oversteer.model import Model
    from tests.fake_wheel import FakeDevice, FakeUi
    return Model(FakeDevice(), FakeUi())


def test_profile_round_trip_auto_and_fixed(tmp_path):
    model = _model()
    assert model.get_rev_leds_port_auto()                     # a new profile: Auto
    path = str(tmp_path / 'p.ini')
    model.set_rev_leds_port(20777)
    model.save(path)
    other = _model()
    other.load(path)
    assert not other.get_rev_leds_port_auto() and other.get_rev_leds_port() == 20777
    model.set_rev_leds_port(5310)                             # 5310 picked: stays a fixed 5310
    model.save(path)
    other = _model()
    other.load(path)
    assert not other.get_rev_leds_port_auto() and other.get_rev_leds_port() == 5310
    model.set_rev_leds_port(AUTO)
    model.save(path)
    assert 'rev_leds_port_auto = 1' in open(path).read()
    other = _model()
    other.load(path)
    assert other.get_rev_leds_port_auto()


@pytest.mark.parametrize('stored, auto, port', [
    ('', True, None), ('rev_leds_port = 5310\n', True, 5310), ('rev_leds_port = 5300\n', True, 5300),
    ('rev_leds_port = 6000\n', False, 6000), ('rev_leds_port = 20777\n', False, 20777),
])
def test_old_profiles(tmp_path, stored, auto, port):
    """Before Auto: no port, or the app's own default (5310, 5300 before 0.14), opens in Auto; a typed port stays."""
    path = tmp_path / 'old.ini'
    path.write_text("[DEFAULT]\nrev_leds = 1\n" + stored)
    model = _model()
    model.load(str(path))
    assert model.get_rev_leds_port_auto() is auto and model.get_rev_leds_port() == port


def test_picking_auto_restarts_the_listener():
    from tests.fake_wheel import FakeController
    controller = FakeController('/nonexistent')
    model = controller.model
    model.data['rev_leds'] = True
    model.set_rev_leds_port(20777)
    assert controller.revs == 1
    model.set_rev_leds_port(AUTO)
    assert controller.revs == 2 and model.get_rev_leds_port() == 20777     # kept for an older Oversteer
    model.set_rev_leds_port(AUTO)
    assert controller.revs == 2
