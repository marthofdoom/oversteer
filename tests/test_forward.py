"""Forwarding of the received datagrams: the "Forward to" parsing and the relay, on 127.0.0.1 ephemeral ports."""
import socket

from oversteer import telemetry as module
from oversteer.telemetry import Forwarder, Telemetry, parse_forward
from tests.test_telemetry import FakeLeds, forza


def _receiver():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', 0))
    sock.settimeout(2.0)
    return sock


def test_parse_ipv4_and_several():
    targets, error = parse_forward("192.168.1.20:20777, 10.0.0.2:9996")
    assert error is None
    assert targets == [('192.168.1.20', 20777), ('10.0.0.2', 9996)]


def test_parse_hostname_resolved_once():
    targets, error = parse_forward("localhost:40000", listening=set())
    assert error is None
    assert targets == [('127.0.0.1', 40000)]


def test_parse_empty_is_off():
    assert parse_forward("") == ([], None)
    assert parse_forward(None) == ([], None)


def test_parse_errors():
    for text in ("192.168.1.20", ":5000", "192.168.1.20:0", "192.168.1.20:70000", "192.168.1.20:abc",
                 "1.1.1.1:1, 1.1.1.2:2, 1.1.1.3:3, 1.1.1.4:4", "no-such-host.invalid:5000"):
        targets, error = parse_forward(text)
        assert targets == [] and error, text


def test_loop_guard():
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
