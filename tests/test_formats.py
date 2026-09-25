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


def eawrc(fourcc=b'SESU', default=False, **values):
    """An EA SPORTS WRC packet in Oversteer's structure, or the game's default one."""
    from oversteer import telemetry_formats as f
    channels = f.EAWRC_DEFAULT_CHANNELS if default else ['packet_4cc'] + f.EAWRC_CHANNELS
    fmt = f.EAWRC_DEFAULT_FORMAT if default else f.EAWRC_FORMAT
    base = dict(packet_4cc=fourcc, vehicle_engine_rpm_current=5200.0, vehicle_engine_rpm_max=7600.0,
                vehicle_engine_rpm_idle=900.0, vehicle_speed=25.0, vehicle_gear_index=3,
                vehicle_gear_index_neutral=0, vehicle_gear_index_reverse=10, vehicle_gear_maximum=6,
                vehicle_throttle=0.8, vehicle_brake=0.0, vehicle_clutch=0.0, vehicle_id=17, location_id=4,
                route_id=12)
    base.update(values)
    return struct.pack(fmt, *(base.get(c, 0) for c in channels))


def test_eawrc_structure_and_sizes():
    import json
    import os
    from oversteer import telemetry_formats as f
    assert f.EAWRC_DEFAULT_SIZE == 237 and f.EAWRC_SIZE == 252
    structure = json.loads(f.eawrc_structure())
    assert structure['header']['channels'] == ['packet_4cc']
    assert {p['id'] for p in structure['packets']} == {'session_start', 'session_update', 'session_end',
                                                       'session_pause', 'session_resume'}
    shipped = os.path.join(os.path.dirname(__file__), '..', 'data', 'telemetry', 'eawrc', 'oversteer.json')
    with open(shipped) as fh:
        assert fh.read() == f.eawrc_structure()                   # the file installed is the one copied
    lines = json.loads('[' + f.eawrc_config_lines(5310) + ']')
    assert {line['port'] for line in lines} == {5310} and all(line['structure'] == 'oversteer' for line in lines)


def test_eawrc_oversteer_structure():
    sample = decode_sample(eawrc())
    assert (sample.game, sample.packet, sample.gear) == ('eawrc', 'update', 3)
    assert sample.rpm == 5200.0 and sample.max_rpm == 7600.0 and sample.speed == 25.0
    assert abs(sample.throttle - 0.8) < 1e-6 and sample.brake == 0.0
    assert sample.car == 'eawrc-17' and sample.stage == 'eawrc:4:12'
    assert decode_sample(eawrc(vehicle_gear_index=0)).gear == 0                  # the packet's neutral
    assert decode_sample(eawrc(vehicle_gear_index=10)).gear == -1                # and reverse
    assert decode_sample(eawrc(fourcc=b'SESP')).packet == 'pause'
    assert decode_sample(eawrc(fourcc=b'SESS'[::-1])).packet == 'start'          # either byte order
    assert decode_sample(eawrc(fourcc=b'XXXX')) is None
    assert decode_sample(eawrc(vehicle_engine_rpm_current=float('nan'))) is None


def test_eawrc_default_structure():
    """The game's own "wrc" structure: 237 bytes, no header, no ids."""
    packet = eawrc(default=True)
    assert len(packet) == 237
    sample = decode_sample(packet)
    assert sample.game == 'eawrc' and sample.gear == 3 and sample.rpm == 5200.0
    assert sample.car == 'eawrc-7600-900-6' and sample.stage is None


def test_eawrc_other_lengths_are_not_dirt():
    """An EA packet from another structure version must not reach the
    Codemasters catch-all (any 4-aligned length from 256 bytes)."""
    assert decode_sample(eawrc() + b'\0' * 12) is None                            # 264: DiRT's length
    assert decode_sample(codemasters(5000, 7500)).game == 'dirt'                   # which still decodes
