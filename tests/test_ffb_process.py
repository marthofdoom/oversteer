"""Countersteer FFB in its own process (oversteer/ffb_process.py): the GUI's EngineProcess and the child's host, against
a fake driver directory (tests/test_ffb_engine.py's wheel()). The setters reach the child, the status comes back, the
newest sample wins and every arrival counts, the listener never waits, the child stops (and gives the game its force
back) when the GUI closes its channel, stops pinging or dies, the GUI survives the child dying, an exclusive owner
in the GUI still gets the wheel, the FFB record is written by the child, and a GIL-heavy GUI does not hold up the
child's passes. Nothing here touches a real wheel or /sys."""
import os
import pickle
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from oversteer import ffb_dynamics, ffb_engine as fe, ffb_process as fp, ffb_record, wheel_owner
from tests.test_ffb_engine import acr, game_raw, read, wheel
from tests.test_ffb_engine import _clean_log  # noqa: F401 (the autouse fixture)

DRIVER = {'driver': 'tests.test_ffb_engine:FakeDriver'}
SETUP = 'tests.test_ffb_process:child_setup'
HZ = 333.0


def child_setup():
    """In the child: no real RBR install looked at (the parent's conftest does the same in-process)."""
    from oversteer import rbr_install
    rbr_install.CANDIDATES = rbr_install.STEAM_SHORTCUTS = rbr_install.LUTRIS_GAMES = ()
    rbr_install.ASYNC = False


@pytest.fixture(autouse=True)
def _own_data_home(tmp_path, monkeypatch):
    """The child's files (the learnt car properties, any log) in the test's folder: it inherits the environment."""
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))


def until(cond, timeout=10.0, step=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(step)
    return cond()


def upload_file(path, level, count):
    """The game's upload as the driver shows it in game_ffb_raw (the child's FakeDriver reads the file)."""
    with open(os.path.join(path, 'game_ffb_raw'), 'r+') as f:
        f.write('%7d %10d %3d\n' % (int(round(level)), count, 0))


class Stream:
    """ACR v5 samples at HZ in real time, the game's force uploaded with each; `offer(now, sample)` is the target."""

    def __init__(self, offer, path):
        self.offer, self.path = offer, path
        self.t0 = time.monotonic()
        self.count = 0
        self.last = None
        self.gap_max = 0.0                               # the longest time between two offers (the sender's own delays)

    def run(self, seconds, until_cond=None):
        end = time.monotonic() + seconds
        due = time.monotonic()
        while time.monotonic() < end:
            s = acr(time.monotonic() - self.t0)
            self.count += 1
            upload_file(self.path, game_raw(s), self.count)
            now = time.monotonic()                       # the arrival, as the listener stamps it before offer()
            if self.last is not None:
                self.gap_max = max(self.gap_max, now - self.last)
            self.last = now
            self.offer(now, s)
            if until_cond is not None and self.count % 33 == 0 and until_cond():
                return True
            due += 1.0 / HZ
            wait = due - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        return until_cond() if until_cond is not None else None


@pytest.fixture
def proxy():
    made = []

    def make(**kw):
        kw.setdefault('engine_kwargs', DRIVER)
        kw.setdefault('setup', SETUP)
        p = fp.EngineProcess(**kw)
        made.append(p)
        return p
    yield make
    for p in made:
        p.stop()


# -- the GUI's side --

def test_the_setters_reach_the_child_and_the_status_comes_back(tmp_path, proxy):
    path = wheel(tmp_path)
    p = proxy()
    p.start()
    device = SimpleNamespace(**{name: None for name in ffb_dynamics.POSITION_FIELDS})
    device.dev_name, device.usb_id = str(tmp_path / 'no-node'), '046d:c24f'
    p.usb_id = '046d:c24f'
    p.set_device(path)
    p.set_position_source(ffb_dynamics.position_source(device))
    p.set_strength(90)
    p.set_knee(45)
    p.set_mapping('classic')
    p.set_drive_pull(True, 40)
    p.set_kick(True, 60)
    p.set_lightness(70)
    p.set_linearity(30)
    p.set_texture(0.5, {'road': True, 'kerb': False})
    p.set_dynamics(False, True, True)
    p.set_wheel_result({'damping': 1})
    assert p.status.device and p.status.available and not p.status.enabled      # before a child: from the files
    p.set_enabled(True)                                  # wanted: the child starts
    assert until(lambda: p._ready)
    got = p.probe()
    assert got['enabled'] is True and got['path'] == path and got['usb_id'] == '046d:c24f'
    assert got['strength'] == 90 and got['knee'] == 45 and got['mapping'] == 'classic'
    assert got['pull'] == (True, 40.0) and got['kick'] == (True, 60.0)
    assert got['lightness'] == 70 and got['linearity'] == 30
    assert got['texture'] == (0.5, {'road': True, 'kerb': False})
    assert got['dynamics'] == (False, True, True) and got['wheel'] == {'damping': 1}
    assert got['position'] is True                       # the reader was rebuilt in the child from the Device's fields
    p.set_strength(110)                                  # a change after the start goes through too
    assert until(lambda: p.probe()['strength'] == 110, 3.0)
    stream = Stream(p.offer, path)
    assert stream.run(5.0, lambda: p.status.game == 'acr' and p.status.state == 'replace')
    assert p.status.enabled and p.status.device and p.status.available
    assert p.stats['samples'] > 0 and p.priority


def test_a_child_restarted_gets_every_setting_again(tmp_path, proxy):
    path = wheel(tmp_path)
    p = proxy(restart_delays=(0.1,))
    p.start()
    p.set_device(path)
    p.set_knee(50)
    p.set_enabled(True)
    assert until(lambda: p._ready)
    first = p.pid
    os.kill(first, signal.SIGKILL)                       # our own child
    assert until(lambda: p.pid != first and p._ready)
    got = p.probe()
    assert got['knee'] == 50 and got['enabled'] is True and got['path'] == path


def test_the_listener_never_waits_for_the_engine_process():
    """A child that reads nothing: the socket fills and offer() drops, it never blocks (the engine thread's own test,
    test_the_listener_never_waits_for_the_engine, in spirit)."""
    p = fp.EngineProcess()
    tx, rx = fp._pair()
    tx.setblocking(False)
    p._data = tx
    try:
        samples = [acr(i / HZ) for i in range(20000)]
        worst = 0.0
        start = time.perf_counter()
        for s in samples:
            a = time.perf_counter()
            p.offer(time.monotonic(), s)
            worst = max(worst, time.perf_counter() - a)
        spent = time.perf_counter() - start
    finally:
        tx.close()
        rx.close()
    assert worst < 0.005 and spent < 5.0
    assert p.dropped > 10000 and p._seq == 20000         # the full socket refused most; each was still numbered


class _Recorder:
    """The engine's side the host drains into."""

    def __init__(self):
        self.offers, self.arrivals = [], []
        self._wake = threading.Event()
        self.skipped = 0

    def offer(self, now, sample, seq=None):
        self.offers.append((now, sample, seq))

    def arrived(self, now, packet_id):
        self.arrivals.append((now, packet_id))


def _host(engine):
    data_rx, data_tx = fp._pair()
    ctrl_rx, ctrl_tx = fp._pair()
    up_rx, up_tx = fp._pair()
    host = fp._Host(engine, data_rx, ctrl_rx, up_tx)
    return host, data_tx, ctrl_tx, up_rx


def test_the_newest_sample_wins_and_every_arrival_counts():
    engine = _Recorder()
    host, data, ctrl, up = _host(engine)
    for seq in range(111, 151):                          # the listener's numbers go on across children
        s = acr(seq / HZ)
        data.send(fp.HEADER.pack(seq / 1000.0, seq, s.packet_id) + pickle.dumps(s))
    host._samples()
    assert len(engine.offers) == 1                       # only the newest is unpickled and offered ...
    now, sample, seq = engine.offers[0]
    assert now == 0.150 and sample.packet_id == acr(150 / HZ).packet_id
    assert seq == 40                                     # ... numbered from this child's first: 40 arrivals
    assert [t for t, _ in engine.arrivals] == [n / 1000.0 for n in range(111, 150)]   # the rest: their arrival times
    s = acr(1.0)
    data.send(fp.HEADER.pack(0.2, 153, fp.NO_ID) + pickle.dumps(s))   # two the GUI dropped: still counted
    host._samples()
    assert engine.offers[-1][2] == 43
    for sock in (data, ctrl, up, host.data, host.ctrl, host.up):
        sock.close()


# -- the child's lifetime --

def _child(path=None):
    data_rx, data_tx = fp._pair()
    ctrl_rx, ctrl_tx = fp._pair()
    up_rx, up_tx = fp._pair()
    options = {'parent': os.getpid(), 'engine': DRIVER, 'setup': SETUP, 'log_level': 20, 'stall_dumps': False}
    child = fp._Child(options, data_rx, ctrl_rx, up_tx)
    for sock in (data_rx, ctrl_rx, up_tx):
        sock.close()
    data_tx.setblocking(False)
    return child, data_tx, ctrl_tx, up_rx


def _messages(up, timeout=0.0):
    out = []
    up.settimeout(timeout)
    while True:
        try:
            blob = up.recv(fp.MAX_MESSAGE)
        except (socket.timeout, BlockingIOError):
            return out
        if not blob:
            return out
        out.append(pickle.loads(blob))
        up.settimeout(0.0)


def _logs(messages):
    return [m[1]['msg'] for m in messages if m[0] == 'log']


def test_the_child_stops_and_gives_the_force_back_when_the_gui_closes_its_channel(tmp_path):
    path = wheel(tmp_path)
    child, data, ctrl, up = _child()
    try:
        for message in (('call', 'set_device', (path,)), ('call', 'set_enabled', (True,))):
            ctrl.send(pickle.dumps(message))
        heard = []
        def send(now, s):
            try:
                data.send(fp.HEADER.pack(now, stream.count, s.packet_id) + pickle.dumps(s))
            except OSError:
                pass                                     # the child has gone
        stream = Stream(send, path)

        def playing():
            ctrl.send(pickle.dumps(('ping',)))
            heard.extend(_messages(up))
            status = [m[1] for m in heard if m[0] == 'status']
            return bool(status) and status[-1].active
        assert stream.run(15.0, playing)                 # taken over: the game muted, ours written
        assert read(path, 'game_ffb_level') == 0
        stream.run(0.3)
        ctrl.close()                                     # the GUI's end goes (EOF)
        gone = time.monotonic()
        child.join(3.0)
        assert not child.is_alive() and time.monotonic() - gone < 1.0
        assert read(path, 'game_ffb_level') == 100       # the game's force back
        last = read(path, 'ext_force')
        stream.run(0.2)
        assert read(path, 'ext_force') == last == 0      # faded out to 0, nothing written after
        logs = _logs(heard + _messages(up))
        assert any('closed the control channel' in line for line in logs)
    finally:
        child.kill()
        child.join(1.0)


def test_the_child_stops_without_the_guis_heartbeat(tmp_path):
    child, data, ctrl, up = _child()
    try:
        ctrl.send(pickle.dumps(('call', 'set_device', (wheel(tmp_path),))))
        sent = time.monotonic()
        child.join(5.0)
        took = time.monotonic() - sent
        assert not child.is_alive()
        assert fp.HEARTBEAT_TIMEOUT * 0.9 < took < fp.HEARTBEAT_TIMEOUT + 2.0
        assert any('no word from the GUI' in line for line in _logs(_messages(up)))
    finally:
        child.kill()
        ctrl.close()


MIDDLE = r'''
import os, sys, socket, time
sys.path[:0] = [%r]
from oversteer import ffb_process as fp
back = socket.socket(fileno=int(sys.argv[1]))
data_rx, data_tx = fp._pair(); ctrl_rx, ctrl_tx = fp._pair(); up_rx, up_tx = fp._pair()
options = {'parent': os.getpid(), 'engine': {'driver': 'tests.test_ffb_engine:FakeDriver'},
           'setup': 'tests.test_ffb_process:child_setup', 'log_level': 20, 'stall_dumps': False}
child = fp._Child(options, data_rx, ctrl_rx, up_tx)
socket.send_fds(back, [str(child.pid).encode()], [ctrl_tx.fileno(), up_rx.fileno(), data_tx.fileno()])
while True:
    time.sleep(1)
'''


def test_the_child_stops_when_the_gui_process_dies(tmp_path):
    """PR_SET_PDEATHSIG alone: the GUI's ends of the sockets stay open elsewhere (here) and keep pinging, so neither
    EOF nor the heartbeat can be what stops it."""
    here, there = socket.socketpair()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    middle = subprocess.Popen([sys.executable, '-c', MIDDLE % root, str(there.fileno())], pass_fds=[there.fileno()])
    there.close()
    try:
        msg, fds, _flags, _addr = socket.recv_fds(here, 64, 3)
        pid = int(msg)
        ctrl, up, data = (socket.socket(fileno=fd) for fd in fds)
        ready = []
        assert until(lambda: (ctrl.send(pickle.dumps(('ping',))), ready.extend(_messages(up)))
                     and any(m[0] == 'ready' for m in ready), 15.0, 0.1)
        middle.kill()                                    # our own child: the GUI process dies
        middle.wait()
        died = time.monotonic()
        lines = []

        def gone():
            try:
                ctrl.send(pickle.dumps(('ping',)))
            except OSError:
                pass
            lines.extend(_logs(_messages(up)))
            try:
                os.kill(pid, 0)
                return False
            except ProcessLookupError:
                return True
        assert until(gone, 3.0)
        assert time.monotonic() - died < fp.HEARTBEAT_TIMEOUT       # not the heartbeat: the parent-death signal
        assert any('signal %d' % signal.SIGTERM in line for line in lines)
    finally:
        if middle.poll() is None:
            middle.kill()
        here.close()


def test_the_gui_survives_the_child_dying_and_restarts_it_a_few_times(tmp_path, proxy, caplog):
    path = wheel(tmp_path)
    p = proxy(restart_delays=(0.2, 0.2), healthy=600.0)
    p.start()
    p.set_device(path)
    p.set_enabled(True)
    for n in range(3):
        assert until(lambda: p._ready and p._down is None, 15.0)
        pid = p.pid
        os.kill(pid, signal.SIGKILL)                     # our own child
        assert until(lambda: p._down is not None or p.pid != pid, 5.0)
        p.offer(time.monotonic(), acr(0.0))              # the listener carries on
        light, text = fe.status_text(p.status)
        if n < 2:
            assert until(lambda: p.pid != pid, 5.0)
    assert until(lambda: p._down == 'engine-failed', 5.0)
    assert p.status.last_stop == 'engine-failed' and not p.status.available
    light, text = fe.status_text(p.status)
    assert light == 'off' and 'kept stopping' in text
    time.sleep(0.5)
    assert p._proc is None                               # not started again
    assert 'starting it again' in caplog.text and 'is not started again' in caplog.text


def test_an_exclusive_owner_in_the_gui_still_gets_the_wheel_and_the_record_is_written(tmp_path, proxy):
    path = wheel(tmp_path)
    p = proxy()
    p.start()
    p.set_device(path)
    p.set_enabled(True)
    stream = Stream(p.offer, path)
    assert stream.run(15.0, lambda: p.status.active)
    assert until(lambda: wheel_owner.current(path) == (fe.OWNER, False), 2.0)       # mirrored into the GUI's registry
    capture = str(tmp_path / 'x.ovcap.gz')
    sampler = ffb_record.sampler(capture + ffb_record.SUFFIX, time.monotonic(), {'capture': 'x'})
    assert isinstance(sampler, fp._RemoteSampler)
    sampler.start()
    stream.run(1.0)
    start = time.monotonic()
    assert wheel_owner.acquire(path, 'wheel-test', exclusive=True)
    try:
        assert time.monotonic() - start < 0.5
        assert read(path, 'game_ffb_level') == 100
        stream.run(0.5)
        assert p.status.held_by == 'wheel-test' and not p.status.active   # the child sees the GUI's owner
        assert read(path, 'game_ffb_level') == 100                         # and never takes the wheel back
    finally:
        wheel_owner.release(path, 'wheel-test')
    sampler.stop()
    assert sampler.records > 0
    channels = {ch for _t, ch, _v in ffb_record.load(capture + ffb_record.SUFFIX)[1]}
    assert {'ext_force', 'game_ffb_raw'} <= channels
    p.stop()
    assert read(path, 'game_ffb_level') == 100
    assert wheel_owner.current(path) is None and ffb_record.remote is None


# -- latency --

def _burn(stop, seconds=0.2):
    """A GIL-heavy pure-Python loop (the shift learner's bands' kind), `seconds` at a time with short breaks."""
    while not stop.is_set():
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            sum(i * i for i in range(200))
        time.sleep(0.02)


def test_a_gil_heavy_gui_does_not_hold_up_the_childs_passes(tmp_path, proxy):
    path = wheel(tmp_path)
    p = proxy()
    p.start()
    p.set_device(path)
    p.set_enabled(True)
    stream = Stream(p.offer, path)
    assert stream.run(15.0, lambda: p.status.active)
    stream.run(1.0)
    stop = threading.Event()
    burners = [threading.Thread(target=_burn, args=(stop,), daemon=True) for _ in range(2)]
    for b in burners:
        b.start()
    p.reset_figures()
    base = {'samples': 0}
    stream.gap_max = 0.0
    try:
        stream.run(3.0)
    finally:
        stop.set()
        for b in burners:
            b.join()
    assert until(lambda: p.stats.get('samples', 0) > base['samples'], 2.0)
    stats = p.stats
    print('child under a GIL-heavy parent:', stats, 'the sender\'s longest gap: %.1f ms' % (stream.gap_max * 1000.0))
    assert stats['pass_max_ms'] < 5.0                    # no pass held up (in-process: 50-270 ms)
    assert stats['oversleep_max_ms'] < 5.0               # and no wait overslept
    assert stats['lag_p99_ms'] < 5.0                     # the newest sample taken within a few ms of its offer
    assert p.status.active                               # it kept playing throughout
