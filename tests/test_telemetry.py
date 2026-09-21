import struct
from oversteer.telemetry import decode, RevLeds, Telemetry, DEFAULT_THRESHOLDS


def forza(rpm, max_rpm=8000.0, race_on=1, size=324):
    data = bytearray(size)
    struct.pack_into('<i', data, 0, race_on)
    struct.pack_into('<fff', data, 8, max_rpm, 900.0, rpm)
    return bytes(data)


def outgauge(rpm, shift=False):
    data = bytearray(96)
    struct.pack_into('<f', data, 16, rpm)
    struct.pack_into('<II', data, 20, 0, 1 if shift else 0)
    return bytes(data)


def codemasters(rpm, max_rpm):
    floats = [0.0] * 66
    floats[37] = rpm / 10.0
    floats[63] = max_rpm / 10.0
    return struct.pack('<66f', *floats)


def test_decode_formats():
    assert decode(forza(4000)) == (4000.0, 8000.0, None)
    assert decode(forza(4000, size=232))[0] == 4000.0
    assert decode(forza(4000, race_on=0))[0] == 0.0          # menus: no revs
    assert decode(outgauge(3000)) == (3000.0, None, False)
    assert decode(outgauge(7000, shift=True))[2] is True
    rpm, mx, _ = decode(codemasters(5000, 7500))
    assert abs(rpm - 5000) < 1e-3 and abs(mx - 7500) < 1e-3
    assert decode(b'\x00' * 50) is None


def test_thresholds_are_monotonic():
    assert list(DEFAULT_THRESHOLDS) == sorted(DEFAULT_THRESHOLDS)
