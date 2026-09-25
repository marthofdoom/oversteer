"""Decoders: packets built with struct per layout, one regression test per
fix the format research found (docs/telemetry-coaching.md, §5.3, §17)."""
import math
import struct

from oversteer.telemetry_formats import decode_sample, codemasters_unit, RAD_S


def forza(size=324, race_on=1, rpm=5000.0, max_rpm=8000.0, gear=3, ordinal=1234):
    data = bytearray(size)
    struct.pack_into('<i', data, 0, race_on)
    struct.pack_into('<fff', data, 8, max_rpm, 900.0, rpm)
    struct.pack_into('<i', data, 212, ordinal)
    if size != 232:
        base = 244 if size == 324 else 232
        struct.pack_into('<ff', data, base + 12, 30.0, 150000.0)             # speed, power
        struct.pack_into('<BBBBB', data, base + 71, 255, 0, 0, 0, gear)      # accel, brake, clutch, handbrake, gear
    return bytes(data)


def outgauge(gear_byte, car=b'XRG\0'):
    data = bytearray(96)
    data[4:8] = car
    data[10] = gear_byte
    struct.pack_into('<ff', data, 12, 20.0, 4000.0)                        # speed, rpm
    return bytes(data)


def codemasters(rpm, max_rpm, idle=None, gear=3.0, gears=6.0, unit=RAD_S, size=264):
    """A DiRT Rally extradata 3 packet with engine rates in `unit`s of rpm."""
    floats = [0.0] * (size // 4)
    floats[33] = gear
    floats[37] = rpm / unit
    floats[63] = max_rpm / unit
    if len(floats) > 65:
        floats[64] = (idle if idle is not None else max_rpm / 9) / unit
        floats[65] = gears
    return struct.pack('<%df' % len(floats), *floats)


def test_forza_gears():
    assert decode_sample(forza(gear=3)).gear == 3
    assert decode_sample(forza(gear=0)).gear == -1                      # reverse
    assert decode_sample(forza(gear=11)).gear == 0                      # neutral: seen between H-pattern gears
    assert decode_sample(forza(gear=12)).gear is None
    assert decode_sample(forza(size=331, gear=11)).gear == 0


def test_forza_games():
    assert decode_sample(forza(size=324)).game == 'forza-fh'
    assert decode_sample(forza(size=331)).game == 'forza-fm'
    assert decode_sample(forza(size=311)).game == 'forza-fm'
    assert decode_sample(forza(size=232)).game == 'forza'


def test_forza_menus_open_no_car():
    """IsRaceOn = 0 (menus, pause): no car key, so no learning session starts."""
    sample = decode_sample(forza(race_on=0))
    assert sample.rpm == 0.0 and sample.car is None and sample.game == 'forza-fh'


def test_outgauge_gears_and_games():
    assert decode_sample(outgauge(0)).gear == -1                        # reverse
    assert decode_sample(outgauge(1)).gear == 0                         # neutral
    assert decode_sample(outgauge(2)).gear == 1
    assert decode_sample(outgauge(2)).game == 'lfs'
    assert decode_sample(outgauge(2, car=b'beam')).game == 'beamng'


def test_codemasters_rad_s():
    """DiRT Rally sends rad/s: 7500 rpm is 785.4, which rpm / 10 read as 7854."""
    sample = decode_sample(codemasters(6000, 7500, idle=800))
    assert abs(sample.rpm - 6000) < 0.01 and abs(sample.max_rpm - 7500) < 0.01
    assert sample.car == 'codemasters-7500-800-6' and sample.car_name == '7500 rpm, 6 gears'
    assert sample.game == 'dirt'


def test_codemasters_true_rpm_and_rpm_over_10():
    """WRC Generations' unit is unverified: a round max in rpm, or in rpm / 10, is taken as it is."""
    sample = decode_sample(codemasters(6000, 7000, unit=1.0, size=280))
    assert abs(sample.max_rpm - 7000) < 0.01 and abs(sample.rpm - 6000) < 0.01 and sample.game == 'wrcg'
    sample = decode_sample(codemasters(6000, 7500, unit=10.0, size=280))
    assert abs(sample.max_rpm - 7500) < 0.01 and abs(sample.rpm - 6000) < 0.01
    assert codemasters_unit(785.398) == RAD_S
    assert codemasters_unit(7000.0) == 1.0
    assert codemasters_unit(750.0) == 10.0
    assert codemasters_unit(733.3) == RAD_S                             # nothing round: DiRT's unit


def test_codemasters_gears():
    assert decode_sample(codemasters(3000, 7500, gear=0.0)).gear == 0
    assert decode_sample(codemasters(3000, 7500, gear=10.0)).gear == -1     # reverse in DiRT Rally
    assert decode_sample(codemasters(3000, 7500, gear=-1.0)).gear == -1     # reverse in some titles
    assert decode_sample(codemasters(3000, 7500, gear=5.0)).gear == 5
    assert decode_sample(codemasters(3000, 7500, gear=float('nan'))).gear is None


def test_codemasters_nonsense():
    assert decode_sample(codemasters(3000, float('inf'))) is None
    assert decode_sample(codemasters(float('nan'), 7500)) is None
    assert decode_sample(codemasters(3000, -7500)) is None
    assert math.isfinite(decode_sample(codemasters(3000, 7500)).rpm)
