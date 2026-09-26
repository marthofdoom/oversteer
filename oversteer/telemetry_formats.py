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
OVST3_FORMAT = '<BBH' + 'f' * 2 + 'f' * 9 + 'f' * 16 + 'f' * 2 + 'f' * 16 + 'f' * 2 + 'f' * 4 + 'ii' + 'fff'
OVST3_SIZE = OVST2_SIZE + struct.calcsize(OVST3_FORMAT)   # + wheels, suspension, the stage (324)
OVST3_GAMES = {1: 'ac', 2: 'acc', 3: 'acr'}

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


G = 9.80665


class Sample:
    """One decoded packet, in one set of units and conventions whatever the
    game (docs/telemetry-coaching.md, section 5.2). Everything but rpm is
    None where the format does not carry it.

    Car frame: x forward, y left, z up (ISO 8855); m, m/s, m/s^2, rad/s.
    yaw_rate and steer are positive to the left. Per-wheel tuples are
    ordered FL, FR, RL, RR. Signs and units marked "verify" in the design
    are converted as the research says and are to be confirmed on a
    capture.

    - Engine and car: rpm, max_rpm, idle_rpm, shift (the game's shift
      light), gear (1.. forward, 0 neutral, -1 reverse), gears (forward
      gear count), power (W, Forza), boost, game_shift_rpm (where the
      game's own shift lights end), car ('<game>/<id>': a key naming the
      car within its game), car_name, car_class, drivetrain ('fwd', 'rwd', 'awd').
    - Game: game ('forza-fh', 'forza-fm', 'forza', 'dirt', 'wrcg',
      'eawrc', 'acpmf', 'beamng', 'lfs'), track (as the game names it),
      stage (a key where the game identifies the stage or route),
      stage_length (m), game_time (the game's clock, s), running (False
      in menus or paused, where the game says), packet (EA SPORTS WRC:
      'start', 'update', 'end', 'pause', 'resume').
    - Inputs as the game sees them, 0..1: throttle, brake, clutch,
      handbrake; steer -1..1, positive left.
    - Motion: speed (m/s), pos (world, y up), vel and accel (car frame),
      accel_kind ('kinematic': the change of velocity; 'specific': what an
      accelerometer reads), yaw_rate, forward and up (world unit vectors).
    - Wheels: wheel_speed (m/s at the tread), wheel_rot (rad/s), slip_ratio
      and slip_kind ('raw' or 'normalised'), slip_angle, susp (m,
      compression positive), susp_vel, susp_norm (0..1 of travel).
    - Surface hints: puddle, rumble (per wheel), surface_rumble.
    - Structure: lap, laps, lap_distance, distance (m), progress (0..1),
      stage_time (s), race_position."""

    __slots__ = ('rpm', 'max_rpm', 'shift', 'gear', 'speed', 'car', 'car_name', 'throttle', 'clutch', 'power',
                 'track', 'game', 'brake', 'stage', 'packet',
                 'idle_rpm', 'gears', 'car_class', 'drivetrain', 'stage_length', 'game_time', 'running',
                 'handbrake', 'steer', 'pos', 'vel', 'accel', 'accel_kind', 'yaw_rate', 'forward', 'up',
                 'wheel_speed', 'wheel_rot', 'slip_ratio', 'slip_kind', 'slip_angle', 'susp', 'susp_vel',
                 'susp_norm', 'puddle', 'rumble', 'surface_rumble', 'lap', 'laps', 'lap_distance', 'distance',
                 'progress', 'stage_time', 'race_position', 'game_shift_rpm', 'boost')

    def __init__(self, rpm, max_rpm=None, shift=None, gear=None, speed=None, car=None, car_name=None,
                 throttle=None, clutch=None, power=None, game=None, brake=None):
        self.rpm, self.max_rpm, self.shift = rpm, max_rpm, shift
        self.gear, self.speed, self.car, self.car_name = gear, speed, car, car_name
        self.throttle, self.clutch, self.power, self.brake = throttle, clutch, power, brake
        self.game = game
        self.track = self.stage = self.packet = None
        self.idle_rpm = self.gears = self.car_class = self.drivetrain = self.stage_length = None
        self.game_time = self.running = self.handbrake = self.steer = None
        self.pos = self.vel = self.accel = self.accel_kind = self.yaw_rate = self.forward = self.up = None
        self.wheel_speed = self.wheel_rot = self.slip_ratio = self.slip_kind = self.slip_angle = None
        self.susp = self.susp_vel = self.susp_norm = self.puddle = self.rumble = self.surface_rumble = None
        self.lap = self.laps = self.lap_distance = self.distance = self.progress = None
        self.stage_time = self.race_position = self.game_shift_rpm = self.boost = None


RAD_S = 30.0 / math.pi                               # rpm per rad/s
_codemasters_units = {}                              # (game, raw max) -> rpm per unit of the engine fields
_eawrc_odd_gears = set()                             # EA SPORTS WRC (gear index, gear count) logged as not understood


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
        if len(_codemasters_units) >= 1000:
            _codemasters_units.clear()   # a maximum that jitters: start over rather than log every packet
        _codemasters_units[(game, raw_max)] = unit
        logging.info("telemetry: %s engine max %.3f read as %s (%.0f rpm)", game, raw_max,
                     {1.0: 'rpm', 10.0: 'rpm / 10'}.get(unit, 'rad/s'), raw_max * unit)
    return unit


def _finite(x):
    return x if math.isfinite(x) else None


def _vector(values):
    """A tuple of floats, or None when any of them is not finite."""
    return tuple(values) if all(math.isfinite(v) for v in values) else None


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _to_car(world, forward, left, up):
    """A world vector in the car frame (x forward, y left, z up)."""
    if world is None or forward is None or left is None or up is None:
        return None
    return (_dot(world, forward), _dot(world, left), _dot(world, up))


def _ascii(raw):
    text = raw.split(b'\0', 1)[0].decode('ascii', 'replace').strip()
    return text if text and all(0x20 <= ord(c) < 0x7f for c in text) else None


def _ovst(data, n):
    """Oversteer's own datagram from oversteer-shm-bridge (AC, ACC, ACR)."""
    version, source, flags, rpm, max_rpm, gear, speed = struct.unpack_from('<BBHffif', data, 4)
    if version not in (1, 2, 3) or {1: OVST_SIZE, 2: OVST2_SIZE, 3: OVST3_SIZE}[version] != n:
        return None
    if not (_plausible(rpm) and _plausible(max_rpm)):
        return None
    sample = Sample(max(0.0, rpm), max_rpm if max_rpm > 0 else None, bool(flags & 1),
                    gear=gear if -1 <= gear <= 12 else None,
                    speed=_finite(speed / 3.6), car='acpmf/unknown', game='acpmf')
    if version >= 2:
        gas, brake = struct.unpack_from('<ff', data, 24)
        sample.throttle, sample.brake = _finite(gas), _finite(brake)
        name = _ascii(data[32:64])
        if name:
            sample.car, sample.car_name = 'acpmf/' + name, name
        sample.track = _ascii(data[64:96])
    if version == 3:
        _ovst3(sample, struct.unpack_from(OVST3_FORMAT, data, OVST2_SIZE))
    return sample


def _ovst3(sample, v):
    """The bridge's version 3 fields. Taken here: what has one meaning in
    every AC game (the wheels' speeds and suspension, the stage's length,
    the progress along it); for ACR also steer, clutch, acceleration,
    local velocity and yaw, whose signs its captures confirmed. Left for
    captures to confirm: those in AC and ACC, and ride height, which ACR
    leaves empty."""
    game = OVST3_GAMES.get(v[0])
    if game is not None:
        sample.game = game
        if sample.car and sample.car.startswith('acpmf/') and sample.car != 'acpmf/unknown':
            sample.car = '{}/{}'.format(game, sample.car_name)
    if sample.car == 'acpmf/unknown':
        # The first packets of a stage come before the game has filled in
        # the car's name: no car yet, rather than a session started for an
        # unknown one (seen on marth's ACR captures)
        sample.car = None
    if game == 'acr':
        # Confirmed on marth's ACR captures: steer is -1 at full left lock;
        # in a left-hand corner accG x and the angular velocity about y are
        # positive, so its car frame is x left, y up, z forward; accG reads
        # 0 at rest (no gravity); clutch is 1 engaged, 0 disengaged
        clutch, steer = v[3], v[4]
        accg, local_vel, ang = v[5:8], v[8:11], v[11:14]
        sample.steer = _finite(-steer)
        sample.clutch = _finite(1.0 - clutch)
        if all(math.isfinite(a) for a in accg):
            sample.accel = (accg[2] * G, accg[0] * G, accg[1] * G)
            sample.accel_kind = 'kinematic'
        sample.vel = _vector((local_vel[2], local_vel[0], local_vel[1]))
        sample.yaw_rate = _finite(ang[1])
    at = 3 + 2 + 9                                   # past game, flags2, reserved, clutch/steer, three vectors
    slip, rot, travel = v[at:at + 4], v[at + 4:at + 8], v[at + 8:at + 12]
    at += 16                                         # and the wheel loads
    radius, travel_max = v[at + 2:at + 6], v[at + 6:at + 10]
    at += 10 + 8                                     # past ride height, radius, max travel, fx, fy
    current_max_rpm, track_length, spline_pos, distance, grip, bias, laps, session = v[at:at + 8]
    world = v[at + 8:at + 11]
    sample.wheel_rot = _vector(rot)
    if sample.wheel_rot is not None and all(math.isfinite(r) and r > 0 for r in radius):
        sample.wheel_speed = tuple(w * r for w, r in zip(sample.wheel_rot, radius))
    sample.susp = _vector(travel)
    if sample.susp is not None and all(math.isfinite(m) and m > 0 for m in travel_max):
        sample.susp_norm = tuple(t / m for t, m in zip(sample.susp, travel_max))
    if math.isfinite(current_max_rpm) and current_max_rpm > 0 and _plausible(current_max_rpm):
        sample.max_rpm = current_max_rpm
    if math.isfinite(track_length) and track_length > 100:
        sample.stage_length = track_length
    if math.isfinite(spline_pos) and 0.0 <= spline_pos <= 1.0 and not (game == 'acr' and spline_pos == 0.0):
        sample.progress = spline_pos                 # ACR leaves it at 0 throughout
    # ACR's distanceTraveled is the position along the stage's road spline
    # (a capture of Wales Afon Bidno: 238 m at the start line, rising to
    # 5518 m): the distance along the stage. Elsewhere, the progress along
    # the spline when the game fills it in.
    if game == 'acr' and math.isfinite(distance) and distance > 0.0:
        sample.lap_distance = distance
    elif sample.progress is not None and sample.stage_length:
        sample.lap_distance = sample.progress * sample.stage_length
    sample.laps = laps if laps >= 0 else None
    sample.pos = _vector(world)


FORZA_DRIVETRAINS = {0: 'fwd', 1: 'rwd', 2: 'awd'}


def _forza(data, n):
    """Forza "Data Out": the sled (232 bytes), and the dash after it in
    FM7 (311), FH4 and later (324, 12 more bytes first) and FM 2023 (331).
    Its car space is x right, y up, z forward (left-handed)."""
    race_on, timestamp = struct.unpack_from('<iI', data, 0)
    max_rpm, idle_rpm, rpm = struct.unpack_from('<fff', data, 8)
    if not (_plausible(max_rpm) and _plausible(rpm)):
        return None
    game = FORZA_GAMES[n]
    if race_on == 0 or max_rpm <= 0:
        # Menus and pause: no car, so no learning session starts
        sample = Sample(0.0, max_rpm if max_rpm > 0 else None, game=game)
        sample.running = False
        return sample
    ordinal, car_class, pi, drivetrain = struct.unpack_from('<iiii', data, 212)
    sample = Sample(max(0.0, rpm), max_rpm, car='{}/{}'.format(game, ordinal),
                    car_name='Forza car {}'.format(ordinal), game=game)
    sample.running = True
    sample.game_time = timestamp / 1000.0
    sample.idle_rpm = _finite(idle_rpm)
    sample.drivetrain = FORZA_DRIVETRAINS.get(drivetrain)
    sample.car_class = 'class:{} pi:{}'.format(car_class, pi)
    ax, ay, az, vx, vy, vz, wx, wy, wz = struct.unpack_from('<9f', data, 20)
    sample.accel = _vector((az, -ax, ay))
    sample.accel_kind = 'kinematic'                        # verify on a capture (a hill at constant speed)
    sample.vel = _vector((vz, -vx, vy))
    # Left-handed axes: a positive turn about y is to the right (verify: a left turn must give yaw_rate > 0)
    sample.yaw_rate = _finite(-wy)
    sample.susp_norm = _vector(struct.unpack_from('<4f', data, 68))
    sample.slip_ratio = _vector(struct.unpack_from('<4f', data, 84))
    sample.slip_kind = 'normalised'
    sample.wheel_rot = _vector(struct.unpack_from('<4f', data, 100))
    sample.rumble = struct.unpack_from('<4i', data, 116)
    if game == 'forza-fh':
        sample.puddle = tuple(float(x) for x in struct.unpack_from('<4i', data, 132))    # 0 or 1
    else:
        sample.puddle = _vector(struct.unpack_from('<4f', data, 132))                    # depth 0..1
    sample.surface_rumble = _vector(struct.unpack_from('<4f', data, 148))
    sample.slip_angle = _vector(struct.unpack_from('<4f', data, 164))
    sample.susp = _vector(struct.unpack_from('<4f', data, 196))
    if n == 232:
        sample.speed = _finite(math.sqrt(vx * vx + vy * vy + vz * vz))
        return sample
    if game == 'forza-fh':
        sample.car_class += ' group:{}'.format(struct.unpack_from('<I', data, 232)[0])   # undocumented: a hint
    # The dash block follows the sled; Horizon puts 12 more bytes first
    base = 244 if n == 324 else 232
    px, py, pz, speed, power = struct.unpack_from('<5f', data, base)
    boost, distance = struct.unpack_from('<f4xf', data, base + 40)
    race_time, lap, position = struct.unpack_from('<fHB', data, base + 64)
    accel, brake, clutch, handbrake, gear, steer = struct.unpack_from('<BBBBBb', data, base + 71)
    sample.speed, sample.power = _finite(speed), _finite(power)
    sample.pos = _vector((px, py, pz))
    sample.boost, sample.distance, sample.stage_time = _finite(boost), _finite(distance), _finite(race_time)
    sample.lap, sample.race_position = lap, position
    sample.throttle, sample.clutch, sample.brake = accel / 255.0, clutch / 255.0, brake / 255.0
    sample.handbrake = handbrake / 255.0
    sample.steer = -steer / 127.0                            # the game's is negative left (verify)
    # 0 is reverse and 11 neutral (an H-pattern box shows it between gears)
    sample.gear = gear if 1 <= gear <= 10 else {0: -1, 11: 0}.get(gear)
    if n == 331:
        sample.stage = 'fm:{}'.format(struct.unpack_from('<i', data, 327)[0])
    return sample


def _outgauge(data, n):
    """OutGauge (Live for Speed's layout; BeamNG.drive sends it too)."""
    car = data[4:8]
    if any(b and not 0x20 <= b < 0x7f for b in car):     # Car[4]: short ASCII name
        return None
    speed, rpm, boost = struct.unpack_from('<fff', data, 12)
    if not _plausible(rpm):
        return None
    dashlights, showlights = struct.unpack_from('<II', data, 40)
    shift = bool(showlights & (1 << 0))          # DL_SHIFT
    gear = data[10]                              # 0 reverse, 1 neutral, 2 first
    throttle, brake, clutch = struct.unpack_from('<fff', data, 48)
    name = _ascii(car)
    # BeamNG always sends "beam": every BeamNG car is one car until a
    # fingerprint can tell them apart
    beamng = name == 'beam'
    sample = Sample(max(0.0, rpm), None, shift, gear=gear - 1 if gear >= 1 else -1,
                    speed=_finite(speed), car='beamng/unknown' if beamng else 'lfs/' + (name or 'car'), car_name=name,
                    throttle=_finite(throttle), clutch=_finite(clutch), brake=_finite(brake),
                    game='beamng' if beamng else 'lfs')
    sample.game_time = struct.unpack_from('<I', data, 0)[0] / 1000.0
    sample.boost = _finite(boost)
    return sample


def _codemasters(data, n):
    """Codemasters extradata 3: DiRT Rally 1/2 and DiRT 4 (264 bytes), and
    WRC Generations, which copies the layout in a longer packet. Wheels
    come rear first; the vectors are in the world frame."""
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
    key = '{}/{:.0f}-{:.0f}-{:.0f}'.format(game, top, round(idle, -1) if math.isfinite(idle) else 0,
                                           gears if math.isfinite(gears) else 0)
    name = '{:.0f} rpm, {:.0f} gears'.format(top, gears) if math.isfinite(gears) else None
    sample = Sample(max(0.0, rpm), max_rpm, gear=gear,
                    speed=_finite(floats[7]), car=key, car_name=name,
                    throttle=_finite(floats[29]), clutch=_finite(floats[32]), brake=_finite(floats[31]),
                    game=game)
    sample.idle_rpm = _finite(idle)
    sample.gears = int(gears) if math.isfinite(gears) and 0 < gears < 20 else None
    sample.game_time, sample.stage_time = _finite(floats[0]), _finite(floats[1])
    sample.lap_distance, sample.progress = _finite(floats[2]), _finite(floats[3])
    sample.pos = _vector(floats[4:7])
    # The "pitch" vector points forward and the "roll" vector sideways,
    # taken as to the left (verify: sideways velocity in a left-hand slide)
    forward, left = _vector(floats[14:17]), _vector(floats[11:14])
    sample.forward = forward
    if forward is not None and left is not None:
        sample.up = _cross(forward, left)
    sample.vel = _to_car(_vector(floats[8:11]), forward, left, sample.up)
    rl, rr, fl, fr = floats[17:21]
    sample.susp = _vector((fl / 1000.0, fr / 1000.0, rl / 1000.0, rr / 1000.0))    # mm (verify unit and sign)
    rl, rr, fl, fr = floats[21:25]
    sample.susp_vel = _vector((fl / 1000.0, fr / 1000.0, rl / 1000.0, rr / 1000.0))
    rl, rr, fl, fr = floats[25:29]
    sample.wheel_speed = _vector((fl, fr, rl, rr))                         # m/s (verify sign in reverse)
    sample.steer = _finite(-floats[30])                                    # the game's is negative left (verify)
    lateral, longitudinal = floats[34], floats[35]
    # g in DiRT Rally; WRCG probably sends m/s^2 (verify)
    scale = G if game == 'dirt' else 1.0
    sample.accel = _vector((longitudinal * scale, lateral * scale, 0.0))   # lateral sign: verify
    sample.accel_kind = 'kinematic'
    if len(floats) > 61:
        sample.lap = int(floats[36]) if math.isfinite(floats[36]) else None
        sample.laps = int(floats[60]) if math.isfinite(floats[60]) and 0 <= floats[60] < 1000 else None
        sample.stage_length = _finite(floats[61])
    if game == 'wrcg':
        # WRC Generations leaves 61 at 0 and puts the stage's length, in
        # km, where DiRT has its progress (seen on a capture of Mexico's
        # Media Luna reverse: 3.98 all the way, lap distance 0 to 3979 m)
        km = floats[3]
        sample.stage_length = km * 1000.0 if math.isfinite(km) and 0.1 < km < 100.0 else None
        sample.progress = None
        if sample.stage_length and sample.lap_distance is not None:
            sample.progress = max(0.0, min(1.0, sample.lap_distance / sample.stage_length))
    return sample


def _eawrc(data, n):
    """EA SPORTS WRC, Oversteer's structure (4CC header) or the game's
    default one; None when the packet is not one of them. Its axes are x
    left, y up, z forward; the car frame comes from projecting on the
    direction vectors the packet carries."""
    if n == EAWRC_SIZE:
        fourcc = data[:4]
        packet = EAWRC_PACKETS.get(fourcc) or EAWRC_PACKETS.get(fourcc[::-1])     # byte order: verify on a capture
        if packet is None:
            return None
        values = dict(zip(['packet_4cc'] + EAWRC_CHANNELS, struct.unpack(EAWRC_FORMAT, data)))
    else:
        packet = 'update'
        values = dict(zip(EAWRC_DEFAULT_CHANNELS, struct.unpack(EAWRC_DEFAULT_FORMAT, data)))
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
        gear = index if 1 <= index <= top else None                # forward gears from 1: verify
        if gear is None and (index, top) not in _eawrc_odd_gears and len(_eawrc_odd_gears) < 100:
            # Once each: the gear convention waits for a capture to confirm
            # it, and a car whose gears all read as unknown learns nothing
            _eawrc_odd_gears.add((index, top))
            logging.info("telemetry: EA SPORTS WRC gear index %s of %s (neutral %s, reverse %s) not understood",
                         index, top, values['vehicle_gear_index_neutral'], values['vehicle_gear_index_reverse'])
    sample = Sample(max(0.0, rpm), max_rpm if max_rpm > 0 else None, gear=gear, speed=speed,
                    throttle=_finite(values['vehicle_throttle']), clutch=_finite(values['vehicle_clutch']),
                    brake=_finite(values['vehicle_brake']), game='eawrc')
    sample.packet = packet
    sample.running = packet not in ('pause', 'end')
    sample.gears = top
    sample.idle_rpm = _finite(values['vehicle_engine_rpm_idle'])
    if values['shiftlights_rpm_valid']:
        sample.game_shift_rpm = _finite(values['shiftlights_rpm_end'])
    sample.handbrake = _finite(values['vehicle_handbrake'])
    sample.steer = _finite(-values['vehicle_steering'])             # the game's is negative left (verify)
    sample.game_time = _finite(values['game_total_time'])
    sample.stage_time = _finite(values['stage_current_time'])
    sample.distance = _finite(values['stage_current_distance'])
    length = values['stage_length']
    sample.stage_length = length if math.isfinite(length) and length > 0 else None
    if sample.stage_length and sample.distance is not None:
        sample.progress = sample.distance / sample.stage_length

    def vector(name):
        return _vector([values['vehicle_{}_{}'.format(name, axis)] for axis in 'xyz'])

    sample.pos = vector('position')
    forward, left, up = vector('forward_direction'), vector('left_direction'), vector('up_direction')
    sample.forward, sample.up = forward, up
    sample.vel = _to_car(vector('velocity'), forward, left, up)
    sample.accel = _to_car(vector('acceleration'), forward, left, up)
    sample.accel_kind = 'kinematic'                                  # verify on a capture
    bl, br, fl, fr = (values['vehicle_cp_forward_speed_' + w] for w in EAWRC_WHEELS)
    sample.wheel_speed = _vector((fl, fr, bl, br))
    if 'vehicle_id' in values:
        sample.car = 'eawrc/{}'.format(values['vehicle_id'])
        sample.car_name = 'EA WRC car {}'.format(values['vehicle_id'])
        sample.car_class = 'class:{}'.format(values['vehicle_class_id'])
        location, route = values['location_id'], values['route_id']
        sample.stage = 'eawrc:{}:{}'.format(location, route)
        sample.track = 'location {}, route {}'.format(location, route)
    else:
        # The default structure names no car: the engine and gearbox tell
        # cars apart, as in DiRT
        idle = values['vehicle_engine_rpm_idle']
        sample.car = 'eawrc/{:.0f}-{:.0f}-{}'.format(round(max_rpm, -1), round(idle, -1) if math.isfinite(idle) else 0,
                                                     top)
        sample.car_name = '{:.0f} rpm, {} gears'.format(round(max_rpm, -1), top)
    return sample


def decode_sample(data):
    """A Sample, or None if the packet isn't a telemetry format we know
    (or carries nonsense)."""
    n = len(data)
    if data[:4] == OVST_MAGIC:
        # Ours whatever its length: a cut-short one must not pass for a
        # Codemasters packet, whose sizes it falls among
        return _ovst(data, n) if n in (OVST_SIZE, OVST2_SIZE, OVST3_SIZE) else None
    if n in FORZA_SIZES:
        return _forza(data, n)
    if n in (92, 96):
        return _outgauge(data, n)
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
        return _codemasters(data, n)
    return None


def decode(data):
    """Return (rpm, max_rpm or None, shift_light or None) or None if the
    packet isn't a telemetry format we know (or carries nonsense)."""
    sample = decode_sample(data)
    return None if sample is None else (sample.rpm, sample.max_rpm, sample.shift)
