"""Decoding the game telemetry formats Oversteer understands.

Every format becomes a :class:`Sample`; fields a format does not carry are
None. The formats:

- Forza Horizon / Motorsport "Data Out" (sled 232 bytes, FM7 dash 311,
  FH4+ dash 324, FM 2023 dash 331): EngineMaxRpm at 8, EngineIdleRpm at 12,
  CurrentEngineRpm at 16 as little-endian floats, IsRaceOn at 0. Enable it
  in the game under Settings > HUD and gameplay > Data Out, pointing at this
  machine's IP and the port set here.
- OutGauge (BeamNG.drive, Live for Speed): 92/96 bytes, rpm as a float at
  16, the shift-light flag (DL_SHIFT) in the ShowLights word at 44. There is
  no max RPM in the packet, so the ceiling is learnt from the highest RPM
  seen and forgotten when the telemetry stops.
- Codemasters extradata=3 (DiRT Rally 2.0, DiRT 4) and the games that copy
  its layout (WRC 10 / WRC Generations native telemetry): 64 or more
  little-endian floats, engine rate at index 37, max at 63 and idle at 64,
  in rad/s in DiRT Rally (see codemasters_unit()). The packet length varies
  by game, so any 4-byte-aligned length from 256 bytes up is accepted once
  the Forza sizes are excluded.
- EA SPORTS WRC's own UDP output: the game's default "wrc" structure
  (237 bytes, no header) or Oversteer's structure (eawrc_structure(),
  252 bytes, a 4CC per packet and the car and stage ids).
- Oversteer's own "OVST" datagram (24 bytes) from oversteer-shm-bridge, the
  helper that runs inside a Proton prefix and forwards shared-memory
  telemetry (Assetto Corsa, Assetto Corsa Competizione, Assetto Corsa
  Rally): rpm, max rpm (0 = unknown), gear, speed.
"""

import json
import logging
import math
import struct

RPM_LIMIT = 30000.0                                  # anything above is not an engine speed
FORZA_SIZES = (232, 311, 324, 331)
FORZA_GAMES = {232: 'forza', 311: 'forza-fm', 324: 'forza-fh', 331: 'forza-fm'}
CODEMASTERS_MIN = 64 * 4                             # DR2/DiRT 4 extradata 3 is 264, WRCG is longer
CODEMASTERS_MAX = 512
CODEMASTERS_DIRT = 264                               # DiRT Rally 1/2 and DiRT 4; other lengths are WRCG (provisional)
OVST_MAGIC = b'OVST'
OVST_SIZE = 24
OVST2_SIZE = 96                                      # + throttle, brake, car and track names

# EA SPORTS WRC: the game sends whatever a packet structure file (JSON, in
# its Documents/My Games/WRC/telemetry/udp folder) lists, channel by
# channel, packed little-endian with no padding. Its default structure,
# "wrc", sends session_update with no header: 237 bytes. Oversteer's own,
# "oversteer" (eawrc_structure()), adds a 4CC header naming each packet and
# the ids of the car and the stage. Types from the game's channels.json
# (data version 2); a boolean is 1 byte.
EAWRC_TYPES = {
    'packet_4cc': '4s', 'packet_uid': 'Q', 'game_total_time': 'f', 'game_delta_time': 'f',
    'game_frame_count': 'Q', 'shiftlights_fraction': 'f', 'shiftlights_rpm_start': 'f',
    'shiftlights_rpm_end': 'f', 'shiftlights_rpm_valid': 'B', 'vehicle_gear_index': 'B',
    'vehicle_gear_index_neutral': 'B', 'vehicle_gear_index_reverse': 'B', 'vehicle_gear_maximum': 'B',
    'stage_current_time': 'f', 'stage_current_distance': 'd', 'stage_length': 'd', 'stage_shakedown': 'B',
    'vehicle_id': 'H', 'vehicle_class_id': 'H', 'vehicle_manufacturer_id': 'H', 'location_id': 'H',
    'route_id': 'H',
}
EAWRC_WHEELS = ('bl', 'br', 'fl', 'fr')
EAWRC_DEFAULT_CHANNELS = (
    ['packet_uid', 'game_total_time', 'game_delta_time', 'game_frame_count', 'shiftlights_fraction',
     'shiftlights_rpm_start', 'shiftlights_rpm_end', 'shiftlights_rpm_valid', 'vehicle_gear_index',
     'vehicle_gear_index_neutral', 'vehicle_gear_index_reverse', 'vehicle_gear_maximum', 'vehicle_speed',
     'vehicle_transmission_speed']
    + ['vehicle_{}_{}'.format(v, a) for v in ('position', 'velocity', 'acceleration', 'left_direction',
                                              'forward_direction', 'up_direction') for a in 'xyz']
    + ['vehicle_{}_{}'.format(v, w) for v in ('hub_position', 'hub_velocity', 'cp_forward_speed',
                                              'brake_temperature') for w in EAWRC_WHEELS]
    + ['vehicle_engine_rpm_max', 'vehicle_engine_rpm_idle', 'vehicle_engine_rpm_current', 'vehicle_throttle',
       'vehicle_brake', 'vehicle_clutch', 'vehicle_steering', 'vehicle_handbrake', 'stage_current_time',
       'stage_current_distance', 'stage_length'])
EAWRC_CHANNELS = EAWRC_DEFAULT_CHANNELS + ['stage_shakedown', 'vehicle_id', 'vehicle_class_id',
                                           'vehicle_manufacturer_id', 'location_id', 'route_id']
EAWRC_PACKETS = {b'SESS': 'start', b'SESU': 'update', b'SESE': 'end', b'SESP': 'pause', b'SESR': 'resume'}
EAWRC_PACKET_IDS = {'start': 'session_start', 'update': 'session_update', 'end': 'session_end',
                    'pause': 'session_pause', 'resume': 'session_resume'}
EAWRC_STRUCTURE = 'oversteer'
_eawrc_mismatches = set()                            # packet lengths already logged


def _struct_format(channels):
    return '<' + ''.join(EAWRC_TYPES.get(c, 'f') for c in channels)


EAWRC_DEFAULT_FORMAT = _struct_format(EAWRC_DEFAULT_CHANNELS)
EAWRC_DEFAULT_SIZE = struct.calcsize(EAWRC_DEFAULT_FORMAT)                   # 237
EAWRC_FORMAT = _struct_format(['packet_4cc'] + EAWRC_CHANNELS)
EAWRC_SIZE = struct.calcsize(EAWRC_FORMAT)                                   # 252


def eawrc_structure():
    """Oversteer's packet structure for EA SPORTS WRC, as the JSON text the
    game reads from telemetry/udp/oversteer.json."""
    return json.dumps({
        'versions': {'schema': 1, 'data': 2},
        'id': EAWRC_STRUCTURE,
        'header': {'channels': ['packet_4cc']},
        'packets': [{'id': packet, 'channels': EAWRC_CHANNELS} for packet in EAWRC_PACKET_IDS.values()],
    }, indent=4) + '\n'


def eawrc_config_lines(port, ip='127.0.0.1'):
    """The entries for the "packets" list of the game's telemetry
    config.json that send Oversteer's structure to `port`."""
    return ',\n'.join(json.dumps({'structure': EAWRC_STRUCTURE, 'packet': packet, 'ip': ip, 'port': int(port),
                                  'frequencyHz': 60, 'bEnabled': True}, indent=4)
                      for packet in EAWRC_PACKET_IDS.values()) + '\n'


def _plausible(x):
    return math.isfinite(x) and -RPM_LIMIT < x < RPM_LIMIT


class Sample:
    """One decoded packet. Everything but rpm may be None (not in this
    format): max_rpm, shift (the game's shift light), gear (1.. forward,
    0 neutral, -1 reverse), speed (m/s), car (a key naming the car within
    its game), car_name, throttle, brake and clutch (0..1, the game's
    view), power (W, Forza only), game (which game or family sent it:
    'forza-fh', 'forza-fm', 'forza', 'dirt', 'wrcg', 'eawrc', 'acpmf',
    'beamng', 'lfs'), track (as the game names it) and stage (a key for
    the stage or route where the game identifies one)."""

    __slots__ = ('rpm', 'max_rpm', 'shift', 'gear', 'speed', 'car', 'car_name', 'throttle', 'clutch', 'power',
                 'track', 'game', 'brake', 'stage', 'packet')

    def __init__(self, rpm, max_rpm=None, shift=None, gear=None, speed=None, car=None, car_name=None,
                 throttle=None, clutch=None, power=None, game=None, brake=None):
        self.rpm, self.max_rpm, self.shift = rpm, max_rpm, shift
        self.gear, self.speed, self.car, self.car_name = gear, speed, car, car_name
        self.throttle, self.clutch, self.power, self.brake = throttle, clutch, power, brake
        self.game = game
        self.track = None
        self.stage = None
        self.packet = None                # EA SPORTS WRC: 'start', 'update', 'end', 'pause', 'resume'


RAD_S = 30.0 / math.pi                               # rpm per rad/s
_codemasters_units = {}                              # (game, raw max) -> rpm per unit of the engine fields


def _near_multiple(x, step, tolerance):
    return abs(x - round(x / step) * step) <= tolerance


def codemasters_unit(raw_max):
    """rpm per unit of the Codemasters engine rate fields (37, 63, 64),
    decided from the raw maximum. DiRT Rally 1/2 send rad/s (every car's
    max is then a round rpm times pi/30), WRC Generations copies the
    layout with a unit nobody has verified, and the format was long
    documented as rpm / 10. A real maximum is a round figure in the true
    unit, so the first unit that makes it one wins; rad/s when none does."""
    if _near_multiple(raw_max * RAD_S, 50.0, 1.0):
        return RAD_S
    if 3000.0 <= raw_max < RPM_LIMIT and _near_multiple(raw_max, 50.0, 1.0):
        return 1.0
    if _near_multiple(raw_max * 10.0, 50.0, 1.0):
        return 10.0
    return RAD_S


def _codemasters_unit(game, raw_max):
    """codemasters_unit(), decided once per car and logged."""
    unit = _codemasters_units.get((game, raw_max))
    if unit is None:
        unit = codemasters_unit(raw_max)
        if len(_codemasters_units) < 1000:
            _codemasters_units[(game, raw_max)] = unit
        logging.info("telemetry: %s engine max %.3f read as %s (%.0f rpm)", game, raw_max,
                     {1.0: 'rpm', 10.0: 'rpm / 10'}.get(unit, 'rad/s'), raw_max * unit)
    return unit


def _finite(x):
    return x if math.isfinite(x) else None


def _ascii(raw):
    text = raw.split(b'\0', 1)[0].decode('ascii', 'replace').strip()
    return text if text and all(0x20 <= ord(c) < 0x7f for c in text) else None


def _eawrc(data, n):
    """EA SPORTS WRC, Oversteer's structure (4CC header) or the game's
    default one; None when the packet is not one of them."""
    if n == EAWRC_SIZE:
        fourcc = data[:4]
        packet = EAWRC_PACKETS.get(fourcc) or EAWRC_PACKETS.get(fourcc[::-1])     # byte order: verify on a capture
        if packet is None:
            return None
        values = dict(zip(['packet_4cc'] + EAWRC_CHANNELS, struct.unpack(EAWRC_FORMAT, data)))
    elif n == EAWRC_DEFAULT_SIZE:
        packet = 'update'
        values = dict(zip(EAWRC_DEFAULT_CHANNELS, struct.unpack(EAWRC_DEFAULT_FORMAT, data)))
    else:
        return None
    rpm, max_rpm = values['vehicle_engine_rpm_current'], values['vehicle_engine_rpm_max']
    speed = values['vehicle_speed']
    if not (_plausible(rpm) and _plausible(max_rpm) and math.isfinite(speed)):
        return None
    index, top = values['vehicle_gear_index'], values['vehicle_gear_maximum']
    if index == values['vehicle_gear_index_neutral']:
        gear = 0
    elif index == values['vehicle_gear_index_reverse']:
        gear = -1
    else:
        gear = index if 1 <= index <= top else None
    sample = Sample(max(0.0, rpm), max_rpm if max_rpm > 0 else None, gear=gear, speed=speed,
                    throttle=_finite(values['vehicle_throttle']), clutch=_finite(values['vehicle_clutch']),
                    brake=_finite(values['vehicle_brake']), game='eawrc')
    sample.packet = packet
    if 'vehicle_id' in values:
        sample.car = 'eawrc-{}'.format(values['vehicle_id'])
        sample.car_name = 'EA WRC car {}'.format(values['vehicle_id'])
        location, route = values['location_id'], values['route_id']
        sample.stage = 'eawrc:{}:{}'.format(location, route)
        sample.track = 'location {}, route {}'.format(location, route)
    else:
        # The default structure names no car: the engine and gearbox tell
        # cars apart, as in DiRT
        idle = values['vehicle_engine_rpm_idle']
        sample.car = 'eawrc-{:.0f}-{:.0f}-{}'.format(round(max_rpm, -1), round(idle, -1) if math.isfinite(idle) else 0,
                                                    top)
        sample.car_name = '{:.0f} rpm, {} gears'.format(round(max_rpm, -1), top)
    return sample


def decode_sample(data):
    """A Sample, or None if the packet isn't a telemetry format we know
    (or carries nonsense)."""
    n = len(data)
    if n in (OVST_SIZE, OVST2_SIZE) and data[:4] == OVST_MAGIC:
        version, source, flags, rpm, max_rpm, gear, speed = struct.unpack_from('<BBHffif', data, 4)
        if version not in (1, 2) or (version == 2) != (n == OVST2_SIZE):
            return None
        if not (_plausible(rpm) and _plausible(max_rpm)):
            return None
        sample = Sample(max(0.0, rpm), max_rpm if max_rpm > 0 else None, bool(flags & 1),
                        gear=gear if -1 <= gear <= 12 else None,
                        speed=_finite(speed / 3.6), car='acpmf', game='acpmf')
        if version == 2:
            gas, brake = struct.unpack_from('<ff', data, 24)
            sample.throttle, sample.brake = _finite(gas), _finite(brake)
            name = _ascii(data[32:64])
            if name:
                sample.car, sample.car_name = 'acpmf-' + name, name
            sample.track = _ascii(data[64:96])
        return sample
    if n in FORZA_SIZES:
        race_on = struct.unpack_from('<i', data, 0)[0]
        max_rpm, idle_rpm, rpm = struct.unpack_from('<fff', data, 8)
        if not (_plausible(max_rpm) and _plausible(rpm)):
            return None
        game = FORZA_GAMES[n]
        if race_on == 0 or max_rpm <= 0:
            return Sample(0.0, max_rpm if max_rpm > 0 else None, game=game)
        ordinal = struct.unpack_from('<i', data, 212)[0]
        sample = Sample(max(0.0, rpm), max_rpm, car='forza-{}'.format(ordinal),
                        car_name='Forza car {}'.format(ordinal), game=game)
        if n == 232:
            vx, vy, vz = struct.unpack_from('<fff', data, 32)
            sample.speed = _finite(math.sqrt(vx * vx + vy * vy + vz * vz))
        else:
            # The dash block follows the sled; Horizon puts 12 more bytes first
            base = 244 if n == 324 else 232
            speed, power = struct.unpack_from('<ff', data, base + 12)
            accel, brake, clutch, handbrake, gear = struct.unpack_from('<BBBBB', data, base + 71)
            sample.speed, sample.power = _finite(speed), _finite(power)
            sample.throttle, sample.clutch, sample.brake = accel / 255.0, clutch / 255.0, brake / 255.0
            # 0 is reverse and 11 neutral (an H-pattern box shows it between gears)
            sample.gear = gear if 1 <= gear <= 10 else {0: -1, 11: 0}.get(gear)
        return sample
    if n in (92, 96):
        car = data[4:8]
        if any(b and not 0x20 <= b < 0x7f for b in car):     # Car[4]: short ASCII name
            return None
        rpm = struct.unpack_from('<f', data, 16)[0]
        if not _plausible(rpm):
            return None
        dashlights, showlights = struct.unpack_from('<II', data, 40)
        shift = bool(showlights & (1 << 0))          # DL_SHIFT
        gear = data[10]                              # 0 reverse, 1 neutral, 2 first
        speed = struct.unpack_from('<f', data, 12)[0]
        throttle, brake, clutch = struct.unpack_from('<fff', data, 48)
        name = _ascii(car)
        return Sample(max(0.0, rpm), None, shift, gear=gear - 1 if gear >= 1 else -1,
                      speed=_finite(speed), car='outgauge-' + (name or 'car'), car_name=name,
                      throttle=_finite(throttle), clutch=_finite(clutch), brake=_finite(brake),
                      game='beamng' if name == 'beam' else 'lfs')
    if n in (EAWRC_SIZE, EAWRC_DEFAULT_SIZE):
        # Before the Codemasters catch-all, which would take any 4-aligned
        # length from 256 bytes up
        return _eawrc(data, n)
    if data[:4] in EAWRC_PACKETS or data[:4][::-1] in EAWRC_PACKETS:
        # Our 4CC, another length: a structure file from another version
        if n not in _eawrc_mismatches:
            _eawrc_mismatches.add(n)
            logging.warning("telemetry: EA SPORTS WRC packet of %d bytes, Oversteer's structure makes %d: "
                            "copy the structure file into the game again", n, EAWRC_SIZE)
        return None
    if CODEMASTERS_MIN <= n <= CODEMASTERS_MAX and n % 4 == 0:
        floats = struct.unpack_from('<%df' % min(66, n // 4), data, 0)
        game = 'dirt' if n == CODEMASTERS_DIRT else 'wrcg'
        raw_max = floats[63]
        if not (math.isfinite(raw_max) and 0 < raw_max < RPM_LIMIT and math.isfinite(floats[37])):
            return None
        unit = _codemasters_unit(game, raw_max)
        rpm, max_rpm = floats[37] * unit, raw_max * unit
        if not (_plausible(max_rpm) and _plausible(rpm)):
            return None
        idle = floats[64] * unit if len(floats) > 64 else float('nan')
        gears = floats[65] if len(floats) > 65 else float('nan')
        gear = floats[33]
        if not math.isfinite(gear):
            gear = None
        elif gear < 0 or gear == 10:           # reverse: 10 in DiRT Rally and WRCG, negative in some titles
            gear = -1
        else:
            gear = int(gear) if gear <= 9 else None
        # No car name in this format: the engine and gearbox tell cars apart.
        # Rounded to 10 rpm, so the key is the same whatever the float noise.
        top = round(max_rpm, -1)
        key = 'codemasters-{:.0f}-{:.0f}-{:.0f}'.format(top, round(idle, -1) if math.isfinite(idle) else 0,
                                                       gears if math.isfinite(gears) else 0)
        name = '{:.0f} rpm, {:.0f} gears'.format(top, gears) if math.isfinite(gears) else None
        return Sample(max(0.0, rpm), max_rpm, gear=gear,
                      speed=_finite(floats[7]), car=key, car_name=name,
                      throttle=_finite(floats[29]), clutch=_finite(floats[32]), brake=_finite(floats[31]),
                      game=game)
    return None


def decode(data):
    """Return (rpm, max_rpm or None, shift_light or None) or None if the
    packet isn't a telemetry format we know (or carries nonsense)."""
    sample = decode_sample(data)
    return None if sample is None else (sample.rpm, sample.max_rpm, sample.shift)
