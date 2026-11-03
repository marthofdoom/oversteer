"""The game's UDP received first by Countersteer FFB's engine process (oversteer/ffb_process.py, the relay in
oversteer/telemetry.py): the engine plays what it receives without the GUI process's GIL in between, every datagram
still reaches the GUI's listener in order with its original receive time, and handing the ports over and back (the
switch, a restart, stop()) never loses or doubles one and never lets the port go. Datagrams come from a sender
process on 127.0.0.1; nothing here touches a real wheel or /sys."""
import errno
import os
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

from oversteer import ffb_process as fp
from oversteer.telemetry import NoLeds, Telemetry
from oversteer.telemetry_formats import decode_sample
from tests.test_ffb_engine import wheel
from tests.test_ffb_engine import _clean_log  # noqa: F401 (the autouse fixture)
from tests.test_ffb_process import DRIVER, SETUP, _burn, until
from tests.test_ffb_process import _own_data_home  # noqa: F401 (the autouse fixture)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENDER = r'''
import sys, time, socket
sys.path[:0] = [%r]
from tests.sim import ovst_packet
from tests.test_ffb_engine import acr
port, first, count, hz = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])
packets = [ovst_packet(acr(i / 333.0), i) for i in range(first, first + count)]
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
due = time.monotonic()
for data in packets:
    sock.sendto(data, ('127.0.0.1', port))
    due += 1.0 / hz
    wait = due - time.monotonic()
    if wait > 0:
        time.sleep(wait)
''' % ROOT


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(('0.0.0.0', 0))
        return s.getsockname()[1]


def port_held(port):
    """True while someone (the listener or the engine process) has the port bound."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(('0.0.0.0', port))
        return False
    except OSError as e:
        assert e.errno == errno.EADDRINUSE
        return True
    finally:
        s.close()


def send(port, first, count, hz=333.0):
    """A sender process (its own GIL): `count` ACR v5 packets numbered from `first`, at `hz`."""
    return subprocess.Popen([sys.executable, '-c', SENDER, str(port), str(first), str(count), str(hz)])


class Seen:
    """Every datagram the GUI's listener handles: (receive time, packet id, relayed, handled at)."""

    def __init__(self, telemetry):
        self.items = []
        handle = telemetry.handle

        def spy(now, data, addr, port=None, relayed=False):
            s = decode_sample(data)
            self.items.append((now, s.packet_id if s is not None else None, relayed, time.monotonic()))
            return handle(now, data, addr, port, relayed)
        telemetry.handle = spy

    def ids(self):
        return [i for _t, i, _r, _h in self.items]


@pytest.fixture
def rig(tmp_path):
    made = []

    def make(enabled=True):
        port = free_port()
        p = fp.EngineProcess(engine_kwargs=DRIVER, setup=SETUP, restart_delays=(0.2, 0.2, 0.2))
        p.start()
        p.set_device(wheel(tmp_path, name='wheel%d' % len(made)))
        p.set_enabled(enabled)
        tel = Telemetry(NoLeds(), port)
        tel.ffb = p
        seen = Seen(tel)
        assert tel.start()
        made.append((tel, p))
        return tel, p, seen, port
    yield make
    for tel, p in made:
        tel.stop()
        p.stop()


def relayed(tel):
    return tel._relay is not None


def test_a_relayed_datagram_packs_and_unpacks():
    blob = fp.relay_pack(12.5, 5310, ('192.168.1.7', 40000), b'OVST\x05data')
    assert fp.relay_unpack(blob) == ('datagram', 12.5, 5310, ('192.168.1.7', 40000), b'OVST\x05data')
    assert fp.relay_unpack(b'M' + fp.RELAY_MARK.pack(42)) == ('mark', 42)


def test_every_datagram_reaches_the_listener_in_order_with_the_engine_process_receive_time(rig):
    tel, p, seen, port = rig()
    assert until(lambda: relayed(tel), 15.0)
    sender = send(port, 1000, 1000)
    sender.wait(30)
    assert until(lambda: len(seen.items) >= 1000, 5.0)
    assert seen.ids() == list(range(1000, 2000))                # every one, in order, once
    assert all(r for _t, _i, r, _h in seen.items)               # all came through the engine process
    times = [t for t, _i, _r, _h in seen.items]
    assert times == sorted(times)
    assert all(t <= h for t, _i, _r, h in seen.items)           # stamped when received, before the GUI handled it
    assert until(lambda: p.status.game == 'acr', 2.0)           # and the engine played them
    assert p.stats['samples'] > 0 and p.stats['relayed'] >= 1000 and p.stats['relay_dropped'] == 0
    assert tel.locked is not None and tel.locked[2] == 'acr'


def test_the_engine_gets_the_datagrams_while_the_guis_gil_is_busy(rig):
    tel, p, seen, port = rig()
    assert until(lambda: relayed(tel), 15.0)
    warm = send(port, 0, 400)
    warm.wait(30)
    assert until(lambda: p.status.game == 'acr', 3.0)
    p.reset_figures()
    stop = threading.Event()
    burners = [threading.Thread(target=_burn, args=(stop,), daemon=True) for _ in range(2)]
    for b in burners:
        b.start()
    try:
        sender = send(port, 400, 1000)                          # 3 s at 333/s, from a process of its own
        sender.wait(30)
    finally:
        stop.set()
        for b in burners:
            b.join()
    assert until(lambda: p.stats.get('samples', 0) >= 900, 3.0)
    stats = p.stats
    print('engine process receiving first, GUI GIL busy:', stats)
    assert stats['lag_max_ms'] < 5.0                            # received to taken by the engine
    assert stats['pass_max_ms'] < 5.0 and stats['oversleep_max_ms'] < 5.0
    assert until(lambda: len(seen.items) >= 1400, 5.0)          # the GUI still got every one, later
    assert seen.ids() == list(range(0, 1400))


def test_switching_off_and_on_hands_the_port_back_and_over_without_losing_a_datagram(rig):
    tel, p, seen, port = rig()
    assert until(lambda: relayed(tel), 15.0)
    sender = send(port, 0, 2000, hz=500.0)                       # 4 s
    time.sleep(0.8)
    p.set_enabled(False)                                        # back to the listener ...
    assert until(lambda: not relayed(tel), 2.0)
    assert port_held(port)
    time.sleep(0.8)
    p.set_enabled(True)                                         # ... and over again
    assert until(lambda: relayed(tel), 2.0)
    sender.wait(30)
    assert until(lambda: len(seen.items) >= 2000, 5.0)
    assert seen.ids() == list(range(2000))
    kinds = [r for _t, _i, r, _h in seen.items]
    assert kinds[0] and not all(kinds) and kinds[-1]            # relayed, read here, relayed again
    times = [t for t, _i, _r, _h in seen.items]
    assert times == sorted(times)


def test_a_restarted_engine_process_takes_the_port_back_and_the_port_is_never_let_go(rig):
    tel, p, seen, port = rig()
    assert until(lambda: relayed(tel), 15.0)
    sender = send(port, 0, 2500, hz=500.0)                       # 5 s
    held = []
    watching = threading.Event()

    def watch():
        while not watching.is_set():
            held.append(port_held(port))
            time.sleep(0.01)
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    time.sleep(0.8)
    first = p.pid
    os.kill(first, signal.SIGKILL)                              # our own child
    assert until(lambda: not relayed(tel), 2.0)                 # the listener reads its sockets again
    assert until(lambda: p.pid != first and relayed(tel), 10.0)   # the new one receives first again
    sender.wait(30)
    watching.set()
    watcher.join()
    assert held and all(held)                                   # bound throughout
    assert until(lambda: len(seen.ids()) >= 2400, 5.0)
    ids = seen.ids()
    assert ids == sorted(set(ids))                              # in order, none twice
    assert len(ids) >= 2500 - 50                                # at most what the killed process held unrelayed
    assert ids[-1] == 2499


def test_stop_frees_the_port_while_the_engine_process_holds_a_copy(rig):
    tel, p, seen, port = rig()
    assert until(lambda: relayed(tel), 15.0)
    sender = send(port, 0, 100)
    sender.wait(30)
    assert until(lambda: len(seen.items) >= 100, 3.0)
    tel.stop()
    assert not port_held(port)                                  # a listener started next can bind it
    assert until(lambda: p.stats.get('udp_ports') == [], 2.0)
    again = Telemetry(NoLeds(), port)
    again.ffb = p
    seen2 = Seen(again)
    assert again.start()
    try:
        assert until(lambda: relayed(again), 3.0)
        send(port, 100, 50).wait(30)
        assert until(lambda: len(seen2.items) >= 50, 3.0)
        assert seen2.ids() == list(range(100, 150))
    finally:
        again.stop()


def test_with_the_switch_off_the_listener_reads_its_own_ports(rig):
    tel, p, seen, port = rig(enabled=False)
    time.sleep(0.5)
    send(port, 0, 50).wait(30)
    assert until(lambda: len(seen.items) >= 50, 3.0)
    assert not relayed(tel) and not any(r for _t, _i, r, _h in seen.items)
    assert seen.ids() == list(range(50))


def test_auto_with_the_engine_process_receiving_first_still_lets_forzas_range_go(tmp_path, monkeypatch):
    """Auto's LEGACY_PORT quirk (Forza Horizon 6 binds its own socket in 5200-5300): probed for a moment, held only
    while the game heard there is locked, then free again; the engine process's copy goes with the listener's."""
    from oversteer import telemetry as module
    from tests.test_port_auto import _free_ports, _bindable, _wait
    from tests.test_telemetry import FakeLeds, forza
    found = _free_ports(4)
    monkeypatch.setattr(module, 'PORT_PRESETS', [(port, 'preset') for port in found[:3]])
    monkeypatch.setattr(module, 'LEGACY_PORT', found[3])
    monkeypatch.setattr(module, 'PROBE_AFTER', 0.0)
    monkeypatch.setattr(module, 'PROBE_EVERY', 0.3)
    monkeypatch.setattr(module, 'PROBE_LISTEN', 0.3)
    monkeypatch.setattr(module, 'IDLE_TIMEOUT', 0.2)
    monkeypatch.setattr(module, 'LOCK_SILENCE', 0.3)
    legacy = found[3]
    p = fp.EngineProcess(engine_kwargs=DRIVER, setup=SETUP)
    p.start()
    t = Telemetry(FakeLeds(), port=module.AUTO)
    t.ffb = p
    try:
        p.set_device(wheel(tmp_path))
        p.set_enabled(True)
        assert t.start()
        assert until(lambda: relayed(t), 15.0)
        assert until(lambda: legacy in (p.stats.get('udp_ports') or []), 5.0)     # the probe, handed over too
        assert _wait(lambda: _bindable(legacy), 3.0)                              # ... and let go by both
        stop = threading.Event()

        def game():
            out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            while not stop.is_set():
                out.sendto(forza(4000), ('127.0.0.1', legacy))
                time.sleep(0.01)
            out.close()
        sender = threading.Thread(target=game, daemon=True)
        sender.start()
        try:
            assert _wait(lambda: t.active_port == legacy, timeout=5.0)
            assert t.legacy_heard and legacy in t.listening and not _bindable(legacy)
            assert relayed(t) and until(lambda: p.status.game is not None, 3.0)   # the engine heard it there
        finally:
            stop.set()
            sender.join()
        assert _wait(lambda: legacy not in t.listening and t.locked is None, timeout=3.0)
        assert _wait(lambda: _bindable(legacy), 1.0)                              # free again: the copy went too
    finally:
        t.stop()
        p.stop()
    for port in found:
        assert _bindable(port)
