"""The listener's UDP sockets handed to Countersteer FFB's engine process (telemetry.Telemetry's relay,
ffb_process.EngineProcess.relay_ports): every Auto port stays bound at every step, and its datagrams reach the
listener. Real sockets, a real engine process; ports are free ones picked here, never the app's own."""
import errno
import socket
import time

import pytest

from oversteer import telemetry as tl
from tests.test_ffb_engine import wheel
from tests.test_telemetry import ovst
from tests.test_ffb_process import proxy, until, _own_data_home  # noqa: F401 (fixtures)
from tests.test_ffb_engine import _clean_log  # noqa: F401


def free_ports(n):
    socks = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(n)]
    for s in socks:
        s.bind(('0.0.0.0', 0))
    ports = [s.getsockname()[1] for s in socks]
    for s in socks:
        s.close()
    return ports


def bound(port):
    """True if the port is held by someone: a third socket cannot bind it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(('0.0.0.0', port))
    except OSError as e:
        return e.errno == errno.EADDRINUSE
    finally:
        s.close()
    return False


def send(port, payload=b'x' * 64):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(payload, ('127.0.0.1', port))
    s.close()


class Heard(tl.Telemetry):
    """Records the (port, data) of every datagram that reaches the listener, from the sockets or the relay."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.seen = []

    def _datagram(self, now, data, addr, port, relayed=False):
        self.seen.append((port, data))
        super()._datagram(now, data, addr, port, relayed=relayed)


def make(monkeypatch, ports):
    monkeypatch.setattr(tl, 'auto_ports', lambda: list(ports))
    return Heard(tl.NoLeds(), tl.AUTO)


def check_all(t, ports, tag):
    for port in ports:
        assert bound(port), '%s: UDP %d is not bound' % (tag, port)
    for port in ports:
        marker = ('%s-%d' % (tag, port)).encode()
        send(port, marker)
        assert until(lambda: (port, marker) in t.seen, 5.0), '%s: UDP %d did not reach the listener' % (tag, port)


def engine_holds(p, ports):
    """The ports the engine process holds, from its latest figures (about every second)."""
    return p.stats.get('udp_ports') == sorted(ports)


def started(tmp_path, proxy):
    p = proxy()
    p.start()
    path = wheel(tmp_path)
    p.set_device(path)
    p.set_enabled(True)
    assert until(lambda: p._ready)
    return p


def test_every_auto_port_stays_bound_across_handovers(tmp_path, proxy, monkeypatch):
    ports = free_ports(4)
    p = started(tmp_path, proxy)
    t = make(monkeypatch, ports)
    t.ffb = p
    assert t.start()
    try:
        assert until(lambda: t._relay is not None, 5.0)
        assert until(lambda: engine_holds(p, ports), 5.0)          # all four, not one of them
        check_all(t, ports, 'start')
        # a game's first packets on one port: the listener locks on it (and tells the engine process)
        game = ovst(5000.0, 8000.0)
        for _ in range(5):
            send(ports[0], game)
            time.sleep(0.05)
        assert until(lambda: t.locked is not None, 5.0)
        time.sleep(0.6)
        check_all(t, ports, 'locked')
        assert until(lambda: engine_holds(p, ports), 3.0)
        # the first packets on one port (what the game's start does), the probe on a fifth port, the port set growing
        extra = free_ports(1)[0]
        monkeypatch.setattr(tl, 'LEGACY_PORT', extra)
        sock = t._bind(extra)
        assert t._add_port(extra, sock, t._selector, listed=False)
        time.sleep(0.6)
        check_all(t, ports, 'probe')
        t._close_port(extra, t._selector)
        time.sleep(0.6)
        check_all(t, ports, 'probe-gone')
        # the switch off and on
        p.set_enabled(False)
        assert until(lambda: t._relay is None, 5.0)
        check_all(t, ports, 'off')
        p.set_enabled(True)
        assert until(lambda: p._ready and t._relay is not None, 10.0)
        assert until(lambda: engine_holds(p, ports), 5.0)
        check_all(t, ports, 'on')
        # an engine restart
        p._kill()
        assert until(lambda: t._relay is None, 5.0)
        check_all(t, ports, 'dead')
        assert until(lambda: p._ready and t._relay is not None, 15.0)
        assert until(lambda: engine_holds(p, ports), 5.0)
        check_all(t, ports, 'restarted')
    finally:
        t.stop()
    for port in ports:
        assert not bound(port)


def test_startup_order_engine_before_or_after_the_sockets(tmp_path, proxy, monkeypatch):
    for engine_first in (True, False):
        ports = free_ports(4)
        p = proxy()
        p.start()
        t = make(monkeypatch, ports)
        t.ffb = p
        if engine_first:
            p.set_device(wheel(tmp_path, 'a' if engine_first else 'b'))
            p.set_enabled(True)
            assert until(lambda: p._ready)
        assert t.start()
        try:
            if not engine_first:
                time.sleep(0.5)
                p.set_device(wheel(tmp_path, 'a' if engine_first else 'b'))
                p.set_enabled(True)
                assert until(lambda: p._ready)
            assert until(lambda: t._relay is not None and engine_holds(p, ports), 10.0)
            check_all(t, ports, 'order-%s' % engine_first)
        finally:
            t.stop()
            p.stop()


def test_the_listener_restarted_while_the_engine_holds_its_sockets(tmp_path, proxy, monkeypatch):
    """The GUI rebuilds its listener (apply_rev_leds: stop(), then a new Telemetry on the same engine process)."""
    ports = free_ports(4)
    p = started(tmp_path, proxy)
    t = make(monkeypatch, ports)
    t.ffb = p
    assert t.start()
    assert until(lambda: t._relay is not None and engine_holds(p, ports), 5.0)
    for n in range(3):
        t.stop()
        t = make(monkeypatch, ports)
        t.ffb = p
        assert t.start()
        assert sorted(t.listening) == sorted(ports), 'restart %d bound %s' % (n, t.listening)
        assert until(lambda: t._relay is not None and engine_holds(p, ports), 8.0)
        check_all(t, ports, 'restart-%d' % n)
    t.stop()


def test_listening_here_again_is_logged_only_while_the_sockets_are_read(tmp_path, proxy, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    ports = free_ports(2)
    p = started(tmp_path, proxy)
    t = make(monkeypatch, ports)
    t.ffb = p
    assert t.start()
    assert until(lambda: t._relay is not None, 5.0)
    p.set_enabled(False)                                 # the switch off: the listener reads its sockets again
    assert until(lambda: t._relay is None, 5.0)
    assert any('here again' in r.message for r in caplog.records)
    check_all(t, ports, 'off')
    p.set_enabled(True)
    assert until(lambda: p._ready and t._relay is not None, 10.0)
    caplog.clear()
    t.stop()                                             # the sockets close next: nothing listens "here again"
    assert not any('here again' in r.message for r in caplog.records)


def test_a_fixed_port_listener_rebuilt_while_the_engine_holds_its_socket(tmp_path, proxy):
    """apply_rev_leds (a device change, a setting): stop(), then a new Telemetry on the same fixed port and engine
    process. The engine's copy must be let go before the new bind, which never meets EADDRINUSE."""
    port = free_ports(1)[0]
    p = started(tmp_path, proxy)

    def build():
        t = Heard(tl.NoLeds(), port)
        t.ffb = p
        return t
    t = build()
    assert t.start()
    assert until(lambda: t._relay is not None and engine_holds(p, [port]), 5.0)
    for n in range(3):
        t.stop()
        t = build()
        assert t.start(), 'rebuild %d: UDP %d could not be bound again' % (n, port)
        assert t.listening == [port] and not t.busy
        assert until(lambda: t._relay is not None and engine_holds(p, [port]), 8.0)
        check_all(t, [port], 'rebuild-%d' % n)
    t.stop()
    assert not bound(port)


def test_a_new_listener_waits_for_the_engines_late_copy_of_its_port(tmp_path, proxy):
    """The engine process answers the hand back late (busy): its copy outlives the old listener's socket. The new
    listener's bind waits for it instead of failing (the port would be held by nobody once the copy closed)."""
    import threading
    port = free_ports(1)[0]
    p = started(tmp_path, proxy)
    copy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)       # the engine's copy, held past the old listener
    copy.bind(('0.0.0.0', port))
    threading.Timer(0.5, copy.close).start()
    t = Heard(tl.NoLeds(), port)
    t.ffb = p
    assert t.start() and t.listening == [port] and not t.busy
    try:
        check_all(t, [port], 'late')
    finally:
        t.stop()
