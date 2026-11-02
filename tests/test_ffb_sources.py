import socket
import time

from oversteer.ffb_sources import BeamNGForceSource
from oversteer.telemetry_formats import csb1_packet

WHEELS = [(4000.0, 0.1, 0.2, 3.0)] * 4


def _send(port, data):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(data, ('127.0.0.1', port))
    s.close()


def _wait(cond):
    end = time.monotonic() + 2
    while time.monotonic() < end and not cond():
        time.sleep(0.005)
    return cond()


def test_receives_latest_goes_stale_and_ignores_junk():
    src = BeamNGForceSource(port=0, stale=0.2)
    assert src.start() and src.bound_port
    try:
        assert src.latest() is None
        _send(src.bound_port, b'junk')
        _send(src.bound_port, csb1_packet(1, 1.0, 12.5, 0.0, 9.0, WHEELS)[:90])
        _send(src.bound_port, csb1_packet(1, 1.0, 12.5, 0.0, 9.0, WHEELS))
        assert _wait(lambda: src.seq == 1)
        assert src.latest().rack_force == 12.5 and src.latest().tyre_load == (4000.0,) * 4
        _send(src.bound_port, csb1_packet(2, 1.002, -3.0, 0.0, 9.0, WHEELS))
        assert _wait(lambda: src.seq == 2) and src.latest().rack_force == -3.0
        assert src.latest(now=time.monotonic() + 1.0) is None            # stale
    finally:
        src.stop()
    assert src.latest() is None


def test_port_held_returns_false():
    a = BeamNGForceSource(port=0)
    assert a.start()
    try:
        b = BeamNGForceSource(port=a.bound_port)
        assert b.start() is False
    finally:
        a.stop()
