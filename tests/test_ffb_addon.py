"""Countersteer FFB's Forza Horizon add-on (oversteer/ffb_addon.py, the engine's 'forza' mode) against synthetic
FH6 streams: 324-byte Data Out packets built here and decoded by the real decoder at 60 a second, the game's
constant force as game_ffb_raw (a fake driver directory) at its own upload rate. The gain follows the front
slip, ext_force always takes away from the game's force and never more than it, the clamps, the stops (stale raw,
a slow upload rate, a spring-only game, no slip channels, app_gain, two effects), mode switches, and that no path
ever writes game_ffb_level (the game's level stays 100). Nothing here touches a real wheel or /sys."""
import math
import random
import struct

import pytest

from oversteer import ffb_addon as fa, ffb_engine as fe
from oversteer.telemetry_formats import decode_sample
from tests.test_ffb_engine import FakeDriver, forces, levels, read, upload, wheel  # noqa: F401
from tests.test_ffb_engine import _clean_log  # noqa: F401 (the autouse fixture)


def fh6_packet(speed=25.0, slip_angle=(0.0, 0.0, 0.0, 0.0), slip_ratio=(0.0, 0.0, 0.0, 0.0), lat=0.0,
               steer=0.0, ordinal=4242):
    """Forza Horizon's 324-byte Data Out with what the add-on reads. `lat`: m/s^2 to the left (Forza's x is right),
    `steer` -1..1 positive left (Forza's is negative left)."""
    data = bytearray(324)
    struct.pack_into('<iI', data, 0, 1, 1000)
    struct.pack_into('<fff', data, 8, 8000.0, 900.0, 5000.0)
    struct.pack_into('<3f', data, 20, -lat, 0.0, 0.0)
    struct.pack_into('<3f', data, 32, 0.0, 0.0, speed)
    struct.pack_into('<4f', data, 84, *slip_ratio)
    struct.pack_into('<4f', data, 164, *slip_angle)
    struct.pack_into('<4f', data, 180, *(math.hypot(a, r) for a, r in zip(slip_angle, slip_ratio)))
    struct.pack_into('<iiii', data, 212, ordinal, 3, 800, 1)
    struct.pack_into('<f', data, 244 + 12, speed)
    struct.pack_into('<b', data, 244 + 76, int(round(-steer * 127)))
    return bytes(data)


def fh6(**kw):
    s = decode_sample(fh6_packet(**kw))
    assert s.game == 'forza-fh'
    return s


def expected_g(alpha, kappa=0.0, strength=fa.STRENGTH):
    g = min(max(math.exp(-math.hypot(alpha / fa.A, kappa / fa.K) ** fa.Q), fa.G_MIN), 1.0)
    return 1.0 - strength * (1.0 - g)


class RecordingDriver(FakeDriver):
    """Every ext_force write with the game's raw level at that moment."""
    writes = []

    def force(self, value):
        super().force(value)
        RecordingDriver.writes.append((value, FakeDriver.raw[0] if FakeDriver.raw else 0))


class Drive:
    """1 ms engine passes; FH6 telemetry at 60 a second; the game uploading its constant force at `upload_hz`.
    The car weaves: lateral acceleration 8 sin(2 pi 0.2 t) m/s^2, the front slip angle following it up to
    `peak` (Forza-normalised: past ~0.7 the trail goes), the game's force `raw_sign` x 2500 x lat."""

    def __init__(self, engine, upload_hz=60.0, tel_hz=60.0, peak=0.9, raw_sign=1.0, speed=25.0, kappa=0.0,
                 level=None, slip=True, effects=(0,)):
        self.engine, self.upload_hz, self.tel_hz = engine, upload_hz, tel_hz
        self.peak, self.raw_sign, self.speed, self.kappa, self.slip = peak, raw_sign, speed, kappa, slip
        self.level = level or (lambda t: self.raw_sign * 2500.0 * self.lat(t))
        self.effects = effects
        self.t = self.next_up = self.next_tel = 0.0
        self.uploads = self.telemetry = True
        self.n_up = 0

    @staticmethod
    def lat(t):
        return 8.0 * math.sin(2 * math.pi * 0.2 * t)

    def alpha(self, t):
        return self.peak * self.lat(t) / 8.0

    def sample(self, t):
        a = self.alpha(t)
        if not self.slip:
            s = fh6(speed=self.speed, lat=self.lat(t), steer=self.lat(t) / 40.0)
            s.slip_angle = s.slip_ratio = None
            return s
        return fh6(speed=self.speed, slip_angle=(a, a, 0.5 * a, 0.5 * a),
                   slip_ratio=(self.kappa, self.kappa, 0.0, 0.0), lat=self.lat(t), steer=self.lat(t) / 40.0)

    def run(self, seconds):
        end = self.t + seconds
        while self.t < end:
            t = self.t
            if self.uploads and t >= self.next_up:
                upload(self.level(t), effect=self.effects[self.n_up % len(self.effects)])
                self.n_up += 1
                self.next_up += 1.0 / self.upload_hz
            if self.telemetry and t >= self.next_tel:
                self.engine.offer(t, self.sample(t))
                self.next_tel += 1.0 / self.tel_hz
            if not self.uploads:
                self.next_up = t
            if not self.telemetry:
                self.next_tel = t
            self.engine.step(t)
            self.t = round(t + 0.001, 6)
        return self


@pytest.fixture(autouse=True)
def _clean_writes():
    RecordingDriver.writes = []
    yield


def engine_on(tmp_path, enabled=True, raw=True, strength=None, app_gain=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = wheel(tmp_path, raw=raw)
    if app_gain:
        with open(path + '/app_gain', 'w') as f:
            f.write('1\n')
    engine = fe.FfbEngine(driver=RecordingDriver, beamng_source=None)
    engine.set_device(path)
    engine.set_enabled(enabled)
    if strength is not None:
        engine.set_lightness(strength)
    engine.step(0.0)
    return engine, path


def never_lowered(path):
    """No game_ffb_level write at all, and the file still the game's 100."""
    return levels(path) == [] and read(path, 'game_ffb_level') == 100


# -- the gain --

def test_the_gain_follows_the_combined_front_slip():
    assert fa.lightness(fh6()) == 1.0
    gs = [fa.lightness(fh6(slip_angle=(a, a, 0.0, 0.0))) for a in (0.1, 0.3, 0.5, 0.6)]
    assert all(x > y for x, y in zip(gs, gs[1:]))
    assert gs[0] > 0.99 and gs[3] == pytest.approx(math.exp(-(0.6 / fa.A) ** fa.Q), rel=1e-5)
    assert fa.lightness(fh6(slip_angle=(0.7, 0.7, 0, 0))) == fa.G_MIN          # exp(-1) is under the clamp
    # The sign of the slip does not matter, the slip ratio counts (weighted more than the angle)
    assert fa.lightness(fh6(slip_angle=(-0.5, -0.5, 0, 0))) == gs[2]
    assert fa.lightness(fh6(slip_angle=(0.3, 0.3, 0, 0), slip_ratio=(0.1, 0.1, 0, 0))) < gs[1]
    assert fa.lightness(fh6(slip_ratio=(0.1, 0.1, 0, 0))) < fa.lightness(fh6(slip_angle=(0.1, 0.1, 0, 0)))
    # The mean of the two fronts; the rears do not count
    assert fa.lightness(fh6(slip_angle=(0.0, 0.0, 2.0, 2.0))) == 1.0
    one = fa.lightness(fh6(slip_angle=(0.6, 0.0, 0, 0)))
    assert one == pytest.approx(math.exp(-(0.5 * 0.6 / fa.A) ** fa.Q))
    # Clamped: never fully dead
    assert fa.lightness(fh6(slip_angle=(5.0, 5.0, 0, 0), slip_ratio=(1.0, 1.0, 0, 0))) == fa.G_MIN
    s = fh6()
    s.slip_angle = None
    assert fa.lightness(s) is None


def test_the_force_always_takes_away_and_never_more_than_the_game():
    rng = random.Random(3)
    for _ in range(5000):
        raw = rng.randint(-32768, 32768)
        g = rng.uniform(-0.5, 1.5)
        ext = fa.ForzaLightness.force(raw, g)
        assert abs(ext) <= abs(raw)
        assert ext == 0 or (ext > 0) != (raw > 0)
        assert abs(raw + ext) <= abs(raw)
        if 0.0 <= g <= 1.0:
            assert raw + ext == pytest.approx(g * raw, abs=1.0)
    assert fa.ForzaLightness.force(0, 0.4) == 0 and fa.ForzaLightness.force(1000, 1.0) == 0


def test_the_gain_is_smoothed_and_slew_limited():
    a = fa.ForzaLightness(strength=1.0)
    a.gain(0.0)
    a.target = fa.G_MIN
    assert a.gain(0.010) >= 1.0 - fa.SLEW * 0.010 - 1e-9          # a 60 Hz step does not reach the wheel as one
    t = 0.010
    while t < 0.3:
        t += 0.001
        g = a.gain(t)
    assert g == pytest.approx(fa.G_MIN, abs=0.01)
    a.target = 1.0
    assert a.gain(t + 0.005) <= g + fa.SLEW * 0.005 + 1e-9
    a.target = 0.0                                                   # never under G_MIN whatever asks
    for i in range(500):
        assert a.gain(t + 0.01 + i * 0.001) >= fa.G_MIN


def test_strength_scales_the_drop():
    s = fh6(slip_angle=(2.0, 2.0, 0, 0))
    for strength, g in ((0.0, 1.0), (0.6, 1.0 - 0.6 * 0.6), (1.0, fa.G_MIN)):
        a = fa.ForzaLightness(strength=strength)
        a.feed(0.0, s, 1000)
        assert a.target == pytest.approx(g)


# -- the engine --

def test_classifier_says_add_on_for_forza_horizon():
    c = fe.SupportClassifier()
    for i in range(90):
        c.feed(i / 60.0, fh6())
    assert c.verdict(89 / 60.0)[:2] == ('add-on', 'fh-addon')
    c = fe.SupportClassifier()
    for i in range(15):
        c.feed(i / 10.0, fh6())
    assert c.verdict(1.4)[:2] == ('add-on', 'fh-rate')


@pytest.mark.parametrize('raw_sign', [1.0, -1.0])
def test_forza_adds_lightness_on_top_of_its_own_force(tmp_path, raw_sign):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine, raw_sign=raw_sign).run(1.0)
    assert engine.status.reason == 'fh-addon' and not engine.status.active
    assert fe.status_text(engine.status)[0] == 'amber'
    drive.run(5.0)
    st = engine.status
    assert st.state == 'add-on' and st.reason == 'fh-addon' and st.active and st.ready and st.game == 'forza-fh'
    assert engine.addon.sign == raw_sign                     # the game's convention, learnt (diagnosis only)
    light, text = fe.status_text(st)
    assert light == 'amber' and 'front tyres run out of grip' in text
    n = len(RecordingDriver.writes)
    drive.run(5.0)
    writes = RecordingDriver.writes[n:]
    assert len(writes) > 400                                 # on each upload and as g moves, not only 60/s
    for ext, raw in writes:
        assert abs(ext) <= 0.6 * abs(raw) + 1               # never past the clamp's share of the game's own
        assert ext == 0 or (ext > 0) != (raw > 0)            # always against it: |total| only ever drops
    # The total follows g x the game's force, g from the front slip (smoothed: compared where it moves slowly)
    ratios = [(raw + ext) / raw for ext, raw in writes if abs(raw) > 4000]
    assert min(ratios) == pytest.approx(expected_g(0.9), abs=0.03)        # at the top of each weave
    assert max(ratios) > 0.99                                              # little slip: the game's force alone
    assert min(ratios) >= 1.0 - fa.STRENGTH * (1.0 - fa.G_MIN) - 0.01
    assert never_lowered(path)


def test_the_lightness_follows_the_slip_not_the_force(tmp_path):
    """Same game force, more front slip: lighter; no slip: the game's force untouched."""
    totals = {}
    for peak in (0.2, 0.6, 1.2):
        FakeDriver.log, RecordingDriver.writes, FakeDriver.raw = [], [], None
        engine, path = engine_on(tmp_path / str(peak))
        Drive(engine, peak=peak).run(8.0)
        assert engine.status.active
        top = [(raw + ext) / raw for ext, raw in RecordingDriver.writes[-2000:] if abs(raw) > 15000]
        totals[peak] = min(top)
    assert totals[0.2] == pytest.approx(expected_g(0.2), abs=0.01) and totals[0.2] > totals[0.6] > totals[1.2]
    assert totals[0.6] == pytest.approx(expected_g(0.6), abs=0.02)
    assert totals[1.2] == pytest.approx(1.0 - fa.STRENGTH * (1.0 - fa.G_MIN), abs=0.02)


def test_strength_from_the_gui_and_its_bounds(tmp_path):
    engine, path = engine_on(tmp_path, strength=100)
    Drive(engine, peak=2.0).run(8.0)
    assert engine.addon.strength == 1.0
    top = [(raw + ext) / raw for ext, raw in RecordingDriver.writes[-2000:] if abs(raw) > 15000]
    assert min(top) == pytest.approx(fa.G_MIN, abs=0.02)
    engine.set_lightness(0)
    drive = Drive(engine, peak=2.0)
    drive.t = drive.next_up = drive.next_tel = 8.0
    drive.run(1.0)
    assert RecordingDriver.writes[-1][0] == 0 and never_lowered(path)


def test_slow_cars_get_the_games_force_untouched(tmp_path):
    engine, path = engine_on(tmp_path)
    Drive(engine, speed=3.0, peak=2.0).run(6.0)
    assert all(ext == 0 for ext, _raw in RecordingDriver.writes)


def last_nonzero_after(engine, drive, seconds):
    """Run `seconds` more passes; the time of the last non-zero ext_force write in them (None: none)."""
    last, end = None, drive.t + seconds
    while drive.t < end:
        n = len(RecordingDriver.writes)
        t = drive.t
        drive.run(0.001)
        if any(ext for ext, _raw in RecordingDriver.writes[n:]):
            last = t
    return last


def test_an_effect_stopped_with_raw_held_cuts_within_20_ms(tmp_path):
    """The game stops or erases its effect: no new level is published, the old one stays. Within three upload
    intervals (60/s: 50 ms) plus 20 ms ext_force is 0: never played, nor faded, against a level that is gone."""
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(6.0)
    assert engine.status.active and engine.raw_rate == pytest.approx(60, abs=3)
    drive.uploads = False
    stop = drive.t
    last = last_nonzero_after(engine, drive, 0.3)
    assert last is not None and last - stop <= 3.0 / 60.0 + 0.020
    assert not engine.status.active and forces()[-1] == 0 and engine._phase == 'idle'
    assert engine.last_stop == 'raw' and never_lowered(path)
    # Uploads again: back on its own
    drive.uploads = True
    drive.run(2.0)
    assert engine.status.active and never_lowered(path)


def test_a_slow_upload_rate_does_nothing(tmp_path):
    engine, path = engine_on(tmp_path)
    Drive(engine, upload_hz=10.0).run(6.0)
    assert engine.status.reason == 'fh-raw-rate' and RecordingDriver.writes == []
    assert fe.status_text(engine.status)[0] == 'grey' and never_lowered(path)


def test_a_spring_only_game_does_nothing(tmp_path):
    # No constant force at all: Forza on the hardware spring and damper
    engine, path = engine_on(tmp_path)
    drive = Drive(engine)
    drive.uploads = False
    drive.run(6.0)
    assert engine.status.reason == 'fh-no-constant' and RecordingDriver.writes == []
    light, text = fe.status_text(engine.status)
    assert light == 'grey' and 'spring and damper' in text
    # A constant force that does not move: the same
    FakeDriver.log, FakeDriver.raw = [], None
    engine, path = engine_on(tmp_path / 'flat')
    Drive(engine, level=lambda t: 600.0 + (t * 1000 % 2)).run(8.0)
    assert engine.status.reason == 'fh-flat' and RecordingDriver.writes == []
    # One that moves but not with the car (a rumble on the constant effect)
    FakeDriver.log, FakeDriver.raw = [], None
    rng = random.Random(5)
    engine, path = engine_on(tmp_path / 'noise')
    Drive(engine, level=lambda t: rng.uniform(-8000, 8000)).run(8.0)
    assert engine.status.reason == 'fh-not-steering' and RecordingDriver.writes == []
    assert never_lowered(path)


def test_no_front_slip_channels_do_nothing(tmp_path):
    engine, path = engine_on(tmp_path)
    Drive(engine, slip=False).run(6.0)
    assert engine.status.reason == 'fh-no-slip'
    assert all(ext == 0 for ext, _raw in RecordingDriver.writes) and never_lowered(path)


def test_app_gain_and_two_effects_are_refused(tmp_path):
    engine, path = engine_on(tmp_path, app_gain=True)
    Drive(engine).run(6.0)
    assert engine.status.reason == 'fh-app-gain' and RecordingDriver.writes == []
    FakeDriver.log, FakeDriver.raw = [], None
    engine, path = engine_on(tmp_path / 'two')
    Drive(engine, effects=(0, 1)).run(6.0)
    assert engine.status.reason == 'fh-two-effects' and RecordingDriver.writes == []


def test_an_old_driver_is_grey(tmp_path):
    engine, path = engine_on(tmp_path, raw=False)
    Drive(engine).run(4.0)
    assert engine.status[:2] == ('add-on', 'old-driver') and RecordingDriver.writes == []


def test_off_it_checks_but_plays_nothing(tmp_path):
    engine, path = engine_on(tmp_path, enabled=False)
    Drive(engine).run(6.0)
    assert engine.status.reason == 'fh-addon' and engine.status.ready and RecordingDriver.writes == []
    assert 'turn on' in fe.status_text(engine.status)[1]


# -- every way out leaves the game's level alone --

def playing(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(6.0)
    assert engine.status.active
    return engine, path, drive


def test_switched_off_fades_out(tmp_path):
    engine, path, drive = playing(tmp_path)
    engine.set_enabled(False)
    drive.run(0.5)
    assert forces()[-1] == 0 and engine.last_stop == 'off' and never_lowered(path)


def test_the_telemetry_stopping_cuts_within_20_ms(tmp_path):
    engine, path, drive = playing(tmp_path)
    drive.telemetry = False                  # Forza quit, crashed or out of focus; its last level may stay put
    stop = drive.t
    last = last_nonzero_after(engine, drive, 0.3)
    assert last is not None and last - stop <= fe.FH_STALL + 1 / 60.0 + 0.020
    assert not engine.status.active and engine.last_stop == 'stall' and forces()[-1] == 0
    assert never_lowered(path)


def test_raw_turning_or_collapsing_between_writes_is_never_added_to():
    """Ours answers the last upload; the game's next lands on top of it. Any next level within D (the recent
    upload-to-upload change) of the last keeps the total's sign and is never exceeded."""
    rng = random.Random(8)
    for trial in range(300):
        a = fa.ForzaLightness()
        t, level = 0.0, rng.uniform(-30000, 30000)
        for i in range(60):
            t += 1 / 60.0
            level = min(max(level + rng.uniform(-3000, 3000), -32768), 32768)
            a.upload(t, level)
        g = rng.uniform(fa.G_MIN, 1.0)
        d = a.margin(t)
        ext = fa.ForzaLightness.force(level, g, d)
        for nxt in (level + d, level - d, level + rng.uniform(-d, d)):
            total = nxt + ext
            assert abs(total) <= abs(nxt) + 1e-9
            assert total == 0 or nxt == 0 or (total > 0) == (nxt > 0)
    # A collapse to 0 or a flip past D: held at most one pass, then answered against the new level
    a = fa.ForzaLightness()
    for i in range(30):
        a.upload(i / 60.0, 20000.0 + 300.0 * (i % 2))
    assert a.margin(29 / 60.0) == 300.0
    assert fa.ForzaLightness.force(20000, 0.64, 300.0) == -int((1 - 0.64) * 19700)
    assert fa.ForzaLightness.force(200, 0.64, 300.0) == 0     # within D of 0: nothing to take


def test_a_sign_flip_is_answered_on_the_next_pass(tmp_path):
    engine, path, drive = playing(tmp_path)
    flip = drive.t
    drive.level = lambda t: -20000.0 if t >= flip else 20000.0
    drive.next_up = flip                                      # the flip uploads now
    drive.run(0.003)
    after = [(ext, raw) for ext, raw in RecordingDriver.writes[-3:]]
    assert after and all(raw < 0 and ext >= 0 for ext, raw in after[-1:])
    assert never_lowered(path)


def test_a_pause_keeps_what_was_learnt(tmp_path):
    engine, path, drive = playing(tmp_path)
    fit, car = engine.addon.fit, engine.addon.car
    paused = decode_sample(bytes(324))                       # race_on 0: a menu or the pause screen
    assert paused.game == 'forza-fh' and paused.car is None
    t = drive.t
    for i in range(120):                                     # two seconds paused, the game still uploading
        engine.offer(t + i / 60.0, paused)
        upload(0.0)
        engine.step(t + i / 60.0)
    assert engine.addon.ready and engine.addon.car == car and engine.addon.fit == fit
    assert all(ext == 0 for ext, _raw in RecordingDriver.writes[-50:])
    drive.t = drive.next_up = drive.next_tel = t + 2.0
    drive.run(1.0)
    assert engine.status.active and never_lowered(path)


def test_device_change_quit_inhibit_and_write_failure(tmp_path):
    engine, path, drive = playing(tmp_path)
    engine.set_device(None)
    engine.step(drive.t)
    assert engine._phase == 'idle' and never_lowered(path)

    FakeDriver.log = []
    engine, path, drive = playing(tmp_path / 'quit')
    engine.stop()
    assert forces()[-1] == 0 and never_lowered(path)

    FakeDriver.log = []
    engine, path, drive = playing(tmp_path / 'inhibit')
    engine._on_inhibit()
    engine.step(drive.t)
    assert engine._phase == 'idle' and engine.last_stop == 'inhibited' and never_lowered(path)

    FakeDriver.log = []
    engine, path, drive = playing(tmp_path / 'fail')
    FakeDriver.fail_force = True
    drive.run(0.05)
    assert engine._phase == 'idle' and engine.last_stop == 'write' and never_lowered(path)


# -- mode switches --

def test_from_forza_to_another_game_and_back(tmp_path):
    from tests.test_ffb_engine import acr
    engine, path, drive = playing(tmp_path)
    t = drive.t
    for i in range(30):                                      # ACR's bridge starts
        engine.offer(t + i / 333.0, acr(t + i / 333.0))
        engine.step(t + i / 333.0)
    assert engine._phase == 'idle' and engine._mode == 'acr' and engine.last_stop == 'state'
    assert never_lowered(path)
    # Forza again: the add-on starts again once it is sure (its car is the same, the check is kept)
    drive.t = drive.next_up = drive.next_tel = t + 2.0
    drive.run(1.0)
    assert engine.status.active and never_lowered(path)


def test_from_rbr_to_forza_gives_rbr_its_level_back_first(tmp_path):
    from tests.test_ffb_rbr import Drive as RbrDrive
    engine, path = engine_on(tmp_path)
    rbr = RbrDrive(engine).run(6.0)
    assert engine.status.active and engine._played_mode == 'rbr' and levels() == [0]
    FakeDriver.raw = None                                    # Forza binds the wheel afresh
    drive = Drive(engine)
    drive.t = drive.next_up = drive.next_tel = rbr.t
    drive.run(0.05)
    assert levels() == [0, 100] and engine._phase == 'idle'
    drive.run(6.0)
    assert engine.status.active and engine._played_mode == 'forza' and levels() == [0, 100]
    assert read(path, 'game_ffb_level') == 100


def test_profiles_keep_the_lightness(tmp_path):
    from oversteer.model import Model
    from tests.fake_wheel import FakeDevice
    device = FakeDevice(ext_force_support=True)
    model = Model(device, None)
    assert model.data['countersteer_lightness'] == 60 and model.get_countersteer_lightness() == 60
    model.data['countersteer_lightness'] = 35
    profile = str(tmp_path / 'p.ini')
    model.save(profile)
    fresh = Model(device, None)
    fresh.load(profile)
    assert fresh.get_countersteer_lightness() == 35
    (tmp_path / 'old.ini').write_text('[DEFAULT]\nrange = 900\ncountersteer_ffb = 1\n')
    fresh.load(str(tmp_path / 'old.ini'))
    assert fresh.get_countersteer_lightness() == 60
    assert Model(FakeDevice(), None).data['countersteer_lightness'] is None
