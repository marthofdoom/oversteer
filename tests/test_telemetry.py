import struct
import socket
import time
from oversteer.telemetry import decode, RevLeds, Telemetry, LED_SPACING


def forza(rpm, max_rpm=8000.0, race_on=1, size=324):
    data = bytearray(size)
    struct.pack_into('<i', data, 0, race_on)
    struct.pack_into('<fff', data, 8, max_rpm, 900.0, rpm)
    return bytes(data)


def outgauge(rpm, shift=False, eng_temp=89.73):
    data = bytearray(96)
    data[4:8] = b'XRG\0'
    struct.pack_into('<f', data, 16, rpm)
    struct.pack_into('<ff', data, 20, 0.5, eng_temp)          # Turbo, EngTemp
    struct.pack_into('<II', data, 40, 0, 1 if shift else 0)   # DashLights, ShowLights
    return bytes(data)


def codemasters(rpm, max_rpm, count=66):
    floats = [0.0] * count
    floats[37] = rpm / 10.0
    floats[63] = max_rpm / 10.0
    return struct.pack('<%df' % count, *floats)


def ovst(rpm, max_rpm, shift=False, version=1):
    return b'OVST' + struct.pack('<BBHffif', version, 1, 1 if shift else 0, rpm, max_rpm, 3, 120.0)


def test_decode_formats():
    assert decode(ovst(6000, 8500)) == (6000.0, 8500.0, False)
    assert decode(ovst(6000, 0)) == (6000.0, None, False)         # redline unknown: learnt
    assert decode(ovst(6000, 8500, shift=True))[2] is True
    assert decode(ovst(6000, 8500, version=2)) is None
    assert decode(forza(4000)) == (4000.0, 8000.0, None)
    assert decode(forza(4000, size=232))[0] == 4000.0
    assert decode(forza(4000, race_on=0))[0] == 0.0          # menus: no revs
    assert decode(forza(4000, size=331))[0] == 4000.0         # Forza Motorsport (2023)
    assert decode(outgauge(3000)) == (3000.0, None, False)    # odd EngTemp mantissa is not a shift light
    assert decode(outgauge(7000, shift=True))[2] is True
    assert decode(forza(float('inf'))) is None
    assert decode(forza(4000, max_rpm=float('nan'))) is None
    assert decode(outgauge(1e9)) is None
    assert decode(codemasters(5000, float('inf'))) is None
    rpm, mx, _ = decode(codemasters(5000, 7500))
    assert abs(rpm - 5000) < 1e-3 and abs(mx - 7500) < 1e-3
    rpm, mx, _ = decode(codemasters(6000, 7000, count=70))    # WRC Generations (longer packet)
    assert abs(rpm - 6000) < 1e-3 and abs(mx - 7000) < 1e-3
    assert decode(codemasters(6000, 7000, count=64))[1] == 7000.0
    assert decode(b'\x00' * 50) is None
    assert decode(b'\x00' * 258) is None                     # not float-aligned


def test_led_spacing_is_monotonic():
    assert list(LED_SPACING) == sorted(LED_SPACING) and LED_SPACING[-1] == 1.0


class FakeLeds:
    """Records what the listener would write to the LEDs."""

    def __init__(self, n=5):
        self.paths = ['led%d' % i for i in range(n)]
        self.writes = []

    def available(self):
        return True

    def set_count(self, lit):
        self.writes.append(('count', lit))

    def set_pattern(self, pattern):
        self.writes.append(('pattern', tuple(pattern)))

    def off(self):
        self.writes.append(('off',))


def _feed(telemetry, packets, gap=0.01):
    """Start the listener on a free port, send packets, return the LED writes."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('127.0.0.1', 0))
    telemetry.port = sock.getsockname()[1]
    sock.close()
    assert telemetry.start()
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for p in packets:
        out.sendto(p, ('127.0.0.1', telemetry.port))
        time.sleep(gap)
    time.sleep(0.1)
    telemetry.stop()
    return telemetry.leds.writes


def test_bar_relative_to_shift_point():
    leds = FakeLeds()
    writes = _feed(Telemetry(leds, shift=0.90), [forza(r, max_rpm=8000) for r in (4000, 5300, 6500, 7200, 7400)])
    counts = [w[1] for w in writes if w[0] == 'count']
    assert counts == [0, 1, 3, 5][:len(counts)] or counts == [0, 1, 3, 5]      # 90 % of 8000 = 7200: all on there
    assert not any(w[0] == 'pattern' for w in writes)                          # 7400 < 7200 * 1.03: no flash


def test_shift_rpm_mode_and_flash():
    leds = FakeLeds()
    writes = _feed(Telemetry(leds, shift_rpm=6500), [forza(r, max_rpm=9000) for r in (4700, 6500, 6800, 6800, 6800)], gap=0.1)
    assert ('count', 1) in writes and ('count', 5) in writes
    assert any(w[0] == 'pattern' for w in writes)                              # above 6500 * 1.03: flashing


def test_outgauge_learned_max_never_flashes_a_steady_cruise():
    leds = FakeLeds()
    packets = [outgauge(7000)] + [outgauge(4000)] * 30
    writes = _feed(Telemetry(leds, shift=0.8), packets)
    assert not any(w[0] == 'pattern' for w in writes)


def _launch(telemetry, profile, inputs):
    """Drive _learn_launch with (seconds, rpm) samples and fixed pedal inputs."""
    telemetry.inputs = lambda: inputs
    for t, rpm in profile:
        telemetry._learn_launch(t, rpm)


def _hold_on_limiter(limiter=7450.0, seconds=1.8):
    """Revs climbing to the limiter, then bouncing just under it."""
    samples, t = [], 0.0
    while t < seconds:
        rpm = min(limiter, 2500 + t * 12000) if t < 0.5 else limiter - (0 if int(t * 50) % 2 else 120)
        samples.append((t, rpm))
        t += 0.02
    return samples


LAUNCH = {'clutch': 1.0, 'throttle': 1.0, 'handbrake': 1.0}


def test_launch_learns_the_limiter():
    learnt = []
    telemetry = Telemetry(FakeLeds(), shift=0.95, launch=True, on_limiter=learnt.append)
    telemetry.last_max_rpm = 9000.0                          # what the game claims
    assert telemetry.reference_max() == 9000.0               # before a launch: the game's figure
    _launch(telemetry, _hold_on_limiter(7450), LAUNCH)
    assert telemetry.launch_max == 7450.0 and learnt[-1] == 7450.0
    assert telemetry.reference_max() == 7450.0


def test_launch_without_a_handbrake_fitted():
    telemetry = Telemetry(FakeLeds(), launch=True)
    _launch(telemetry, _hold_on_limiter(6900), {'clutch': 0.95, 'throttle': 0.95, 'handbrake': None})
    assert telemetry.launch_max == 6900.0


def test_no_launch_no_learning():
    telemetry = Telemetry(FakeLeds(), launch=True)
    # handbrake down: driving, not a launch
    _launch(telemetry, _hold_on_limiter(), dict(LAUNCH, handbrake=0.0))
    assert telemetry.launch_max == 0.0
    # clutch or handbrake only part way: not a launch
    _launch(telemetry, _hold_on_limiter(), dict(LAUNCH, clutch=0.85))
    _launch(telemetry, _hold_on_limiter(), dict(LAUNCH, handbrake=0.85))
    assert telemetry.launch_max == 0.0
    # held under a second: too short to count
    _launch(telemetry, _hold_on_limiter(seconds=0.9), LAUNCH)
    assert telemetry.launch_max == 0.0
    # a clutch kick: too short to count
    _launch(telemetry, _hold_on_limiter(seconds=0.4), LAUNCH)
    assert telemetry.launch_max == 0.0
    # revs still climbing the whole time: never reached the limiter
    telemetry._launch_samples = []
    _launch(telemetry, [(i * 0.02, 2500 + i * 60) for i in range(75)], LAUNCH)
    assert telemetry.launch_max == 0.0
    # other modes don't learn at all
    assert Telemetry(FakeLeds(), shift=0.95).launch is False
    assert Telemetry(FakeLeds(), shift_rpm=7000, launch=True).launch is False


def test_launch_limiter_drives_the_bar_and_is_raised_on_the_move():
    inputs = dict(LAUNCH)
    telemetry = Telemetry(FakeLeds(), shift=0.9, launch=True, inputs=lambda: inputs)
    writes = _feed(telemetry, [ovst(rpm, 9000) for _, rpm in _hold_on_limiter(7000, seconds=1.6)], gap=0.02)
    assert telemetry.launch_max == 7000.0                    # learnt through the listener
    assert any(w[0] == 'pattern' for w in writes)            # on the limiter: past 90 % of 7000, flashing
    inputs.update(clutch=0.0, handbrake=0.0)                 # driving away, past a capped launch
    writes = _feed(telemetry, [ovst(7300, 9000)], gap=0.02)
    assert telemetry.launch_max == 7300.0


def test_ovst_v2_names_the_car_and_track():
    from oversteer.telemetry import decode_sample
    body = struct.pack('<BBHffif', 2, 1, 0, 6200.0, 0.0, 3, 108.0) + struct.pack('<ff', 1.0, 0.0)
    packet = b'OVST' + body + b'ks_rally_car'.ljust(32, b'\0') + b'rally_stage_07'.ljust(32, b'\0')
    sample = decode_sample(packet)
    assert (sample.car, sample.car_name, sample.track) == ('acpmf/ks_rally_car', 'ks_rally_car', 'rally_stage_07')
    assert sample.gear == 3 and abs(sample.speed - 30.0) < 0.01 and sample.throttle == 1.0
    assert decode(packet) == (6200.0, None, False)
    assert decode_sample(packet[:24]) is None                 # a v2 header on a v1-sized packet


def test_handle_is_the_live_path():
    """Tests and replays drive Telemetry.handle directly, with their own clock."""
    leds = FakeLeds()
    telemetry = Telemetry(leds, shift=0.90)
    for i, rpm in enumerate((4000, 5300, 6500, 7200, 7400)):
        telemetry.handle(100.0 + i * 0.01, forza(rpm, max_rpm=8000), ('127.0.0.1', 40000))
    assert [w[1] for w in leds.writes if w[0] == 'count'] == [0, 1, 3, 5]
    assert telemetry.live.rpm == 7400.0 and telemetry.last_source == '127.0.0.1'
    telemetry.check_idle(100.1)                               # not quiet long enough
    assert telemetry.live is not None
    telemetry.check_idle(103.0)
    assert telemetry.live is None and leds.writes[-1] == ('off',)


def test_one_source_at_a_time():
    telemetry = Telemetry(FakeLeds())
    game, bridge, other = ('127.0.0.1', 40000), ('127.0.0.1', 40001), ('192.168.1.5', 40000)
    telemetry.handle(100.00, forza(4000), game)
    telemetry.handle(100.01, ovst(6000, 8500), bridge)       # a stale bridge next to the game
    telemetry.handle(100.02, forza(4500), other)             # the same game on another machine
    assert telemetry.live.game == 'forza-fh' and telemetry.live.rpm == 4000.0
    telemetry.handle(100.03, forza(4100), game)
    assert telemetry.live.rpm == 4100.0
    for i in range(300):                                      # the bridge keeps sending; the game went quiet
        telemetry.handle(100.04 + i * 0.01, ovst(6000, 8500), bridge)
    assert telemetry.live.game == 'acpmf'                     # idle freed the lock for it


def test_probe_finds_a_game_on_the_other_port(monkeypatch):
    """Nothing on our port: a game still sending to the other default is
    found by listening there for a moment, and let go again."""
    import threading
    from oversteer import telemetry as module

    def free_port():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        return port

    ours, theirs = free_port(), free_port()
    monkeypatch.setattr(module, 'DEFAULT_PORT', ours)
    monkeypatch.setattr(module, 'LEGACY_PORT', theirs)
    monkeypatch.setattr(module, 'PROBE_LISTEN', 0.5)
    telemetry = Telemetry(FakeLeds(), port=ours)
    assert telemetry.other_port == theirs
    stop = threading.Event()

    def game():
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while not stop.is_set():
            out.sendto(forza(4000), ('127.0.0.1', theirs))
            time.sleep(0.02)

    sender = threading.Thread(target=game, daemon=True)
    sender.start()
    try:
        telemetry.probe_other_port()
    finally:
        stop.set()
        sender.join()
    assert telemetry.elsewhere is True
    telemetry.probe_other_port()                               # the game was set right
    assert telemetry.elsewhere is False
    check = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    check.bind(('0.0.0.0', theirs))                            # let go again
    check.close()


def test_learning_without_rev_lights():
    """"Learn from game telemetry": the listener runs with no LEDs and
    still feeds the learner."""
    from oversteer.telemetry import NoLeds

    class Learner:
        fed = 0

        def feed(self, *args):
            Learner.fed += 1

        def shift_rpm(self, gear):
            return None
    telemetry = Telemetry(NoLeds(), learner=Learner())
    for i, rpm in enumerate((4000, 6000, 7400)):
        telemetry.handle(i * 0.1, forza(rpm, max_rpm=8000), ('127.0.0.1', 5555))
    assert Learner.fed == 3 and telemetry.live.rpm == 7400
