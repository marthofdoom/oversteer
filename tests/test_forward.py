"""Forwarding of the received datagrams: the "Forward to" parsing and the relay, on 127.0.0.1 ephemeral ports."""
import socket
import threading
from unittest import mock

import pytest

from oversteer import telemetry as module
from oversteer.telemetry import Forwarder, Telemetry, parse_forward
from tests.test_telemetry import FakeLeds, forza


def _receiver():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', 0))
    sock.settimeout(2.0)
    return sock


def test_parse_ipv4_and_several(no_dns):
    targets, error = parse_forward("192.168.1.20:20777, 10.0.0.2:9996")
    assert error is None
    assert targets == [('192.168.1.20', 20777), ('10.0.0.2', 9996)]


@pytest.fixture
def no_dns(monkeypatch):
    """getaddrinfo answers from a table: no real lookups in the tests."""
    table = {'localhost': '127.0.0.1', 'mypc': '192.168.1.5'}

    def fake(host, port, family=0, type=0):
        if host in table:
            return [(socket.AF_INET, socket.SOCK_DGRAM, 0, '', (table[host], port))]
        if host.replace('.', '').isdigit():
            return [(socket.AF_INET, socket.SOCK_DGRAM, 0, '', (host, port))]
        raise socket.gaierror("unknown host")
    monkeypatch.setattr(module.socket, 'getaddrinfo', fake)
    return table


def test_parse_hostname_resolved_once(no_dns):
    targets, error = parse_forward("localhost:40000", listening=set())
    assert error is None
    assert targets == [('127.0.0.1', 40000)]


def test_parse_empty_is_off():
    assert parse_forward("") == ([], None)
    assert parse_forward(None) == ([], None)


def test_parse_errors(no_dns):
    for text in ("192.168.1.20", ":5000", "192.168.1.20:0", "192.168.1.20:70000", "192.168.1.20:abc",
                 "1.1.1.1:1, 1.1.1.2:2, 1.1.1.3:3, 1.1.1.4:4", "no-such-host.invalid:5000"):
        targets, error = parse_forward(text)
        assert targets == [] and error, text


def test_loop_guard(no_dns):
    for host in ("127.0.0.1", "0.0.0.0", "localhost"):
        targets, error = parse_forward("{}:5310".format(host))
        assert targets == [] and error, host
    assert parse_forward("127.0.0.1:5310", listening={5555})[1] is None      # not a listening port
    assert parse_forward("127.0.0.1:5555", listening={5555})[1]
    assert parse_forward("192.168.1.20:5310")[1] is None                     # another computer


def test_forwarder_sends_identical_bytes():
    rx = _receiver()
    fwd = Forwarder()
    fwd.set_targets([rx.getsockname()])
    payload = bytes(range(256)) * 2
    fwd.send(payload)
    assert rx.recv(2048) == payload
    fwd.close()
    rx.close()


def test_forwarder_without_targets_or_unreachable_does_not_raise():
    fwd = Forwarder()
    fwd.send(b'x')                                    # no socket, no targets
    fwd.set_targets([('127.0.0.1', 9)])               # nobody listens: ICMP refused on a later send
    for _ in range(5):
        fwd.send(b'x')
    fwd.set_targets([('203.0.113.1', 9)])
    fwd.send(b'x')
    fwd.close()
    fwd.send(b'x')


def test_listener_relays_received_datagram():
    rx = _receiver()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    telemetry = Telemetry(FakeLeds(), port)
    telemetry.forwarder.set_targets([rx.getsockname()])
    assert telemetry.start()
    try:
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        packet = forza(rpm=5000)
        out.sendto(packet, ('127.0.0.1', port))
        assert rx.recv(2048) == packet
        out.close()
    finally:
        telemetry.stop()
        rx.close()


def test_preference_roundtrip():
    from oversteer.telemetry_view import read_preferences, write_preferences
    assert read_preferences({})['telemetry_forward'] == ''
    values = read_preferences({'telemetry_forward': ' 10.0.0.2:9996 '})
    assert values['telemetry_forward'] == '10.0.0.2:9996'
    assert write_preferences(values)['telemetry_forward'] == '10.0.0.2:9996'


def test_loop_guard_uses_the_held_ports(no_dns):
    held = module.held_ports(False, 20777)
    assert held == {20777, 5300}                       # the port and its other_port
    assert parse_forward("127.0.0.1:20777", held)[1]
    assert parse_forward("127.0.0.1:9996", held)[1] is None
    assert 5310 in module.held_ports(True, None) and 5300 in module.held_ports(True, None)


def test_own_datagram_is_not_forwarded_again():
    """A target that loops to the listener: the copy coming back from the forwarder's own port is dropped."""
    rx = _receiver()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    telemetry = Telemetry(FakeLeds(), port)
    telemetry.forwarder.set_targets([('127.0.0.1', port), rx.getsockname()])    # bypasses the validation
    assert telemetry.start()
    try:
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        packet = forza(rpm=5000)
        out.sendto(packet, ('127.0.0.1', port))
        assert rx.recv(2048) == packet
        rx.settimeout(0.5)
        with pytest.raises(socket.timeout):
            rx.recv(2048)                             # not a second copy, no storm
        out.close()
    finally:
        telemetry.stop()
        rx.close()


def test_set_targets_binds_before_publishing():
    fwd = Forwarder()
    fwd.set_targets([('127.0.0.1', 9)])
    assert fwd.port and fwd.targets == (('127.0.0.1', 9),)
    fwd.close()
    assert fwd.port is None and fwd.targets == ()


def _gui():
    from oversteer.gui import Gui
    me = mock.MagicMock()
    me._forward_gen = 0
    me._forward_targets = []
    me._forward_resolved = None
    me._forward_held.return_value = set()
    me.telemetry = None
    me._forward_stop = threading.Event()
    me._forward_done = lambda *a: Gui._forward_done(me, *a)
    return me


def test_older_slow_lookup_cannot_overwrite_a_newer_entry(no_dns):
    from oversteer.gui import Gui
    me = _gui()
    me.ui.safe_call = lambda f, *a: f(*a)
    gate = threading.Event()
    real = module.socket.getaddrinfo

    def slow(host, port, *a):
        if host == 'mypc':
            gate.wait(5)
        return real(host, port, *a)
    with mock.patch.object(module.socket, 'getaddrinfo', slow), \
            mock.patch('oversteer.gui.GLib.idle_add', lambda f, *a: f(*a)):
        Gui._forward_start(me, 'mypc:1234', [('mypc', 1234)], save=True)        # slow hostname, thread
        Gui._forward_start(me, '10.0.0.2:9996', [('10.0.0.2', 9996)], save=True)   # newer, literal, at once
        assert me._forward_targets == [('10.0.0.2', 9996)]
        gate.set()
        for t in threading.enumerate():
            if t.name == 'forward-resolve':
                t.join(2)
    assert me._forward_targets == [('10.0.0.2', 9996)]
    assert me.telemetry_forward == '10.0.0.2:9996'


def test_forwarding_alone_starts_the_listener():
    from oversteer.gui import Gui
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    me = mock.MagicMock()
    me.device = None
    me.telemetry = None
    me.telemetry_learn = False
    me._forward_targets = [('127.0.0.1', 9)]
    me.telemetry_recorder = None
    me.telemetry_generation = 0
    me.model.get_rev_leds_port_auto.return_value = False
    me.model.get_rev_leds_port.return_value = port
    me._shift_kwargs.return_value = {}
    me._countersteer_wanted.return_value = False      # Countersteer FFB off: it would run the listener too
    Gui.apply_rev_leds(me)
    try:
        assert me.telemetry is not None and me.telemetry.forwarder.targets == (('127.0.0.1', 9),)
    finally:
        if me.telemetry is not None:
            me.telemetry.stop()
    me._forward_targets = []
    me.telemetry = None
    Gui.apply_rev_leds(me)
    assert me.telemetry is None                       # nothing to do: no listener


def test_typing_the_live_text_back_cancels_the_lookup_in_flight(no_dns):
    from oversteer.gui import Gui
    me = _gui()
    gate = threading.Event()
    real = module.socket.getaddrinfo

    def slow(host, port, *a):
        if host == 'mypc':
            gate.wait(5)
        return real(host, port, *a)
    with mock.patch.object(module.socket, 'getaddrinfo', slow), \
            mock.patch('oversteer.gui.GLib.idle_add', lambda f, *a: f(*a)):
        Gui._forward_start(me, '10.0.0.2:9996', [('10.0.0.2', 9996)], save=True)      # A is live
        assert Gui.set_telemetry_forward.__get__(me)('10.0.0.2:9996') is None         # unchanged: no lookup
        Gui._forward_start(me, 'mypc:1234', [('mypc', 1234)], save=True)              # B, in flight
        Gui.set_telemetry_forward(me, '10.0.0.2:9996')                                # A typed back
        gate.set()
        for t in threading.enumerate():
            if t.name == 'forward-resolve':
                t.join(2)
    assert me._forward_targets == [('10.0.0.2', 9996)]
    assert me.telemetry_forward == '10.0.0.2:9996'
