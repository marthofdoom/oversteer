"""Countersteer FFB for Richard Burns Rally (oversteer/ffb_rbr.py's gates and ffb_engine.RbrRack, the engine's RBR
mode) against synthetic NGP streams: the game's raw level made exactly as NGP and Wine make it (curve, floor,
DirectInput to level) at ~870 uploads a second, RBR's telemetry at 236 a second, a fake driver directory. The
curve's inversion round-trips, the checks (grid, strength; saturation is a note), the column torque, the sign
latch, the classifier's states and the hand-over; nothing is played without fresh raw. Nothing here touches a
real wheel or /sys."""
import math
import random
import types

import pytest

from oversteer import ffb_engine as fe, ffb_forza as fz, ffb_rbr as r
from oversteer.telemetry_formats import Sample
from tests.test_ffb_engine import FakeDriver, forces, levels, upload, wheel  # noqa: F401 (the autouse fixture)
from tests.test_ffb_engine import _clean_log  # noqa: F401

@pytest.fixture(autouse=True)
def _ngp_source(request, monkeypatch):
    """The tests below are NGP's path (countersteer_rbr_source = ngp); the derived path's own tests are named test_derived_*."""
    if not request.node.name.startswith('test_derived'):
        monkeypatch.setattr(fe.RbrRack, 'source', 'ngp')
        monkeypatch.setattr(fe, 'OVERALL_RBR_SOURCE', 'ngp')


r_latch = 0.5
CAR = 'rbr/11'                    # Ford Fiesta WRC 2019 in the shipped table (ffb_sensitivity 100)


# -- the curve --

def test_the_curve_matches_rerbr_and_inverts():
    assert r.ngp_curve(27.37, 100.0) == pytest.approx(0.5, abs=1e-3)
    assert r.ngp_curve(-90.9, 100.0) == pytest.approx(-0.9, abs=1e-3)
    assert r.ngp_curve(0.0, 100.0) == 0.0
    assert r.ngp_curve(30.0, 125.0) < r.ngp_curve(30.0, 100.0)
    for s in (1.0, 52.5, 100.0, 105.0, 275.0):
        for torque in (-150.0, -27.4, -1.0, 0.01, 3.0, 40.0, 120.0):
            x = r.ngp_curve(torque, s)
            if abs(x) < r.X_MAX:
                assert r.ngp_inverse(x, s) == pytest.approx(torque, rel=1e-9, abs=1e-9)
    # u = -ln(1 - |x|) is linear in T: 100 * K_LN / S per unit of torque
    for torque in (1.0, 10.0, 50.0):
        assert r.linear(r.ngp_curve(torque, 100.0)) == pytest.approx(torque * r.K_LN, rel=1e-9)


def test_the_sensitivity_follows_ngp():
    assert r.sensitivity(105.0, 50) == 52.5
    assert r.sensitivity(105.0, 100) == 105.0
    assert r.sensitivity(105.0, 150) == 157.5
    assert r.sensitivity(105.0, 0) == 105.0 and r.sensitivity(105.0, -20) == 105.0
    assert r.sensitivity(1.5, 10) == 1.0


def test_ngp_levels_are_on_wines_grid_and_others_mostly_not():
    for m in range(-10000, 10001, 7):
        assert r.on_grid(r.level_of(m)) and r.magnitude_of(r.level_of(m)) == m
    rng = random.Random(4)
    share = sum(r.on_grid(rng.randint(-32000, 32000)) for _ in range(20000)) / 20000
    assert 0.25 < share < 0.4 < r.GRID_GO
    assert r.magnitude(0.5) == -5000 and r.magnitude(-3.0) == 10000


# -- the knee --

def proven(linearity=r.LINEARITY):
    """An RbrForce that has seen the game's force at full scale (its STRENGTH 1.0)."""
    force = r.RbrForce(linearity=linearity)
    force.peak = float(r.FULL)
    return force


def test_linearity_0_is_the_games_curve_100_linear_70_exact_to_the_knee():
    force = proven(linearity=0)
    for m in range(0, int(r.X_MAX * r.DI_MAX), 37):        # (past X_MAX it is not inverted further)
        level = r.level_of(m)
        assert abs(force.force(level) - level) <= 1
        assert abs(force.force(-level) + level) <= 1
    force.set_linearity(100)
    for x in (0.05, 0.3, 0.6):
        level = int(x * r.FULL)
        assert force.force(level) == pytest.approx(min(r.FULL * r.linear(level / r.FULL), r.FULL), abs=1)
    assert force.force(int(0.9 * r.FULL)) == r.FULL                      # linear past full scale: clipped
    force.set_linearity(70)
    knee = 0.7 * r.FULL
    for x in (0.02, 0.2, 0.45):                                          # under the knee: exactly linear in T
        level = int(x * r.FULL)
        assert force.force(level) == pytest.approx(r.FULL * r.linear(level / r.FULL), abs=1)
    above = [force.force(int(x * r.FULL)) for x in (0.55, 0.7, 0.9, 0.99)]
    assert all(knee < a <= r.FULL for a in above) and above == sorted(above)


def test_the_same_strength_at_small_forces_and_more_above():
    force = proven()
    for level in (30, 100, 300):
        assert force.force(level) == pytest.approx(level, rel=0.02, abs=1)
    for x in (0.3, 0.5, 0.6):
        level = int(x * r.FULL)
        assert force.force(level) > level * 1.1                          # less compressed than the game's


def test_the_knee_is_continuous_and_the_slopes_cross_at_u_1():
    for knee in (0.0, 0.3, 0.7, 0.9):
        k = knee * r.FULL
        assert r.shape(k - 1e-6, knee, r.FULL) == pytest.approx(r.shape(k + 1e-6, knee, r.FULL), abs=1e-3)
        assert r.shape_slope(k - 1e-6, knee, r.FULL) == pytest.approx(r.shape_slope(k + 1e-6, knee, r.FULL),
                                                                     abs=1e-6)
        for u in (0.2, 0.9):                                             # ours steeper below u = 1 ...
            assert r.shape_slope(u * r.FULL, knee, r.FULL) >= math.exp(-u) - 1e-12
        if knee:
            assert r.shape_slope(1.5 * r.FULL, knee, r.FULL) < math.exp(-1.5)   # ... flatter above


def test_the_ceiling_follows_the_drivers_cap():
    force = proven()
    force.set_ceiling(0.8 * r.FULL)
    assert force.force(int(0.99 * r.FULL)) <= 0.8 * r.FULL + 1
    force.set_ceiling(None)
    assert force.ceiling == r.FULL
    force.peak = 0.6 * r.FULL                            # never past the game's own peak
    assert force.ceiling == pytest.approx(0.6 * r.FULL) and force.force(int(0.99 * r.FULL)) <= 0.6 * r.FULL + 1


# -- a drive --

def ngp_level(torque, s=100.0):
    return r.level_of(r.magnitude(r.ngp_curve(torque, s)))


def rbr_sample(car=CAR, speed=20.0, steer=0.0, ay=0.0):
    s = Sample(5000.0, gear=3, speed=speed, car=car, game='rbr')
    s.steer, s.accel = steer, (0.0, ay, 0.0)
    return s


class Drive:
    """1 ms engine passes, NGP uploading every 1.15 ms, RBR's telemetry every 1/236 s."""

    def __init__(self, engine, amp=36.0, s=100.0, upload_hz=870.0, tel_hz=236.0, level=None, strength=1.0,
                 hits=True, car=CAR, direction=1.0):
        self.engine, self.amp, self.s, self.upload_hz, self.tel_hz = engine, amp, s, upload_hz, tel_hz
        self.hits, self.car = hits, car
        self.direction = direction       # 1: the game's DIRECTION normal (the level opposes the steering), -1: inverted
        self.level = level or (lambda t: r.level_of(r.magnitude(r.ngp_curve(self.torque(t), self.s), strength)))
        self.t = self.next_up = self.next_tel = 0.0
        self.uploads = True
        self.telemetry = True

    def torque(self, t):
        hit = 250.0 if self.hits and (t % 4.0) > 3.94 else 0.0      # a big hit every 4 s, 60 ms: the curve's top
        return self.amp * math.sin(2 * math.pi * 0.3 * t) + 1.5 * math.sin(2 * math.pi * 23.0 * t) + hit

    def sample(self, t):
        return rbr_sample(self.car, steer=self.steer(t))

    def steer(self, t):
        return self.direction * 0.3 * math.sin(2 * math.pi * 0.3 * t)      # (the torque's slow part: it centres)

    def run(self, seconds):
        end = self.t + seconds
        while self.t < end:
            t = self.t
            if self.uploads and t >= self.next_up:
                upload(self.level(t))
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


def engine_on(tmp_path, enabled=True, raw=True, linearity=None):
    path = wheel(tmp_path, raw=raw)
    engine = fe.FfbEngine(driver=FakeDriver, beamng_source=None)
    engine.set_device(path)
    engine.set_enabled(enabled)
    if linearity is not None:
        engine.set_linearity(linearity)
    engine.step(0.0)
    return engine, path


def test_rbr_goes_green_and_plays_the_column_torque(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(1.5)
    assert engine.status.state == 'replace' and not engine.status.active    # learning: drive a few corners
    assert engine.status.waiting in ('raw', 'corners', 'direction')
    drive.run(4.0)
    st = engine.status
    assert st.state == 'replace' and st.active and st.ready and st.game == 'rbr'
    assert st.raw_rate is not None and 700 < st.raw_rate < 1000
    assert engine.rbr_rack.direction == 'normal' and engine.rbr_rack.sign_r < -r_latch
    assert levels() == [0]
    # Played at the upload rate (every pass with a new upload), not the telemetry's
    n = len(forces())
    drive.run(1.0)
    assert len(forces()) - n > 700
    level = FakeDriver.raw[0]
    rack = engine.rbr_rack
    assert rack.column_raw == pytest.approx(math.copysign(0.062862 * r.linear(abs(level) / r.FULL) * 100.0, level), rel=1e-6)
    assert engine.rbr.torque == pytest.approx(r.ngp_inverse(-level / r.FULL, 100.0), rel=1e-6)
    assert engine.rbr.torque == pytest.approx(drive.torque(drive.next_up - 1 / 870.0), abs=0.2)
    light, text = fe.status_text(engine.status)
    assert light == 'green' and 'column torque' in text


def test_no_fresh_raw_no_force(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine)
    drive.uploads = False
    drive.run(6.0)
    assert FakeDriver.log == [] and engine.status[:2] == ('model', 'rbr-raw')
    assert fe.status_text(engine.status)[0] == 'amber'
    # Playing, then the uploads stop (a pause, a hitch): handed back, the game's level 100
    drive.uploads = True
    drive.run(6.0)
    assert engine.status.active
    drive.uploads = False
    drive.run(0.5)
    assert not engine.status.active and levels() == [0, 100]
    assert engine.last_stop in ('raw', 'state')


def test_a_slow_upload_rate_is_amber(tmp_path):
    engine, path = engine_on(tmp_path)
    Drive(engine, upload_hz=100.0).run(6.0)
    assert engine.status[:2] == ('model', 'rbr-raw') and FakeDriver.log == []


def test_levels_off_the_grid_do_not_follow_the_curve(tmp_path):
    engine, path = engine_on(tmp_path)
    rng = random.Random(1)
    Drive(engine, level=lambda t: rng.randint(-20000, 20000)).run(6.0)
    assert engine.status[:2] == ('model', 'rbr-grid') and FakeDriver.log == []
    assert 'DirectInput' in fe.status_text(engine.status)[1]


def test_a_saturated_force_is_a_note_not_a_refusal(tmp_path):
    engine, path = engine_on(tmp_path)
    Drive(engine, amp=900.0).run(8.0)                    # |x| past 0.98 most of the time
    assert engine.rbr.saturated and engine.status.active and engine.status.note == 'saturated'
    assert 'limit' in fe.status_text(engine.status)[1]


def test_the_games_linearity_and_gain_no_longer_gate_it(tmp_path):
    engine, path = engine_on(tmp_path, linearity=0)
    Drive(engine).run(8.0)
    assert engine.rbr.linearity == 0 and engine.status.active and engine.status.gain is None


def test_a_driver_without_game_ffb_raw_is_grey(tmp_path):
    engine, path = engine_on(tmp_path, raw=False)
    Drive(engine).run(3.0)
    assert engine.status[:2] == ('add-on', 'rbr-no-raw') and FakeDriver.log == []


def test_off_it_measures_but_plays_nothing(tmp_path):
    engine, path = engine_on(tmp_path, enabled=False)
    Drive(engine).run(6.0)
    assert FakeDriver.log == [] and engine.status.state == 'replace' and engine.status.ready
    assert not engine._fast()


def test_switch_off_crossfades_back(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(6.0)
    assert engine.status.active
    engine.set_enabled(False)
    drive.run(0.5)
    assert levels() == [0, 100] and forces()[-1] == 0 and engine.last_stop == 'off'


def test_the_ini_sets_s_only_for_the_torque(tmp_path, monkeypatch):
    root = tmp_path / 'rbr'
    root.mkdir()
    (root / 'RichardBurnsRally.ini').write_text('[NGP]\r\nForceFeedbackSensitivityGravel=50\r\n'
                                                'ForceFeedbackSensitivityTarmac=50\r\n'
                                                'ForceFeedbackSensitivitySnow=50\r\n')
    force = r.RbrForce()
    force.set_car('rbr/11', None, root=str(root))
    assert force.base_s == 100.0 and force.s == 50.0 and force.s_note is None
    without = r.RbrForce()
    without.set_car('rbr/11', None, root=str(tmp_path / 'none'))
    assert without.s == 100.0 and 'unreadable' in without.s_note
    unknown = r.RbrForce()
    unknown.set_car('rbr/unknown')
    assert unknown.s is None and 'unknown' in unknown.describe()
    # The force does not depend on S
    assert force.force(12345) == without.force(12345) == unknown.force(12345)


def test_under_the_drivers_cap_ours_rounds_off_below_it(tmp_path):
    engine, path = engine_on(tmp_path)
    with open(path + '/gain', 'w') as f:
        f.write('65535\n')                                # ext_force_cap 80 (wheel()): flattened past 26214
    engine.set_device(None)
    engine.step(0.0)
    engine.set_device(path)
    engine.step(0.0)
    assert engine.rbr.cap == pytest.approx(0.8 * r.FULL)
    Drive(engine, amp=30.0).run(9.0)
    assert engine.status.active and max(abs(v) for v in forces()) <= 0.8 * r.FULL + 1
    assert not engine.status.clipping
    assert max(abs(v) for v in forces()) > 0.2 * r.FULL                # the hits get through


# -- the game's STRENGTH, the car key, app_gain, the write rate, every way out while playing --

@pytest.mark.parametrize('strength', [0.7, 0.5])
def test_a_strength_under_1_never_makes_ours_the_stronger(tmp_path, strength):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine, amp=60.0, strength=strength).run(12.0)
    game_peak = engine.rbr.peak
    assert game_peak <= strength * r.FULL + 4
    assert engine.rbr.ceiling <= game_peak + 1          # ours never past the game's own top, the strength still assumed 1
    assert drive


def test_a_strength_near_1_plays(tmp_path):
    engine, path = engine_on(tmp_path)
    Drive(engine, amp=36.0, strength=0.9).run(12.0)
    assert engine.status.active


def test_the_car_key_narrowing_keeps_the_fit_and_the_force(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine, car='rbr/unknown').run(7.0)
    assert engine.status.active and engine.rbr.s is None
    changes, peak = engine.rbr.changes, engine.rbr.peak
    drive.car = CAR                                       # told from its gearing
    drive.run(0.5)
    assert engine.status.active and levels() == [0]
    assert engine.rbr.changes > changes and engine.rbr.peak >= peak and engine.rbr.s == 100.0


def test_app_gain_on_does_not_refuse_rbr(tmp_path):
    # the raw level is read before any gain and RbrRack computes its own force: the driver's app_gain is no matter
    engine, path = engine_on(tmp_path)
    with open(path + '/app_gain', 'w') as f:
        f.write('1\n')
    engine.set_device(None)
    engine.step(0.0)
    engine.set_device(path)
    engine.step(0.0)
    Drive(engine).run(7.0)
    assert engine.status.active and engine.status.reason != 'rbr-app-gain'


def test_never_two_writes_in_a_millisecond_and_fades_keep_writing(tmp_path, monkeypatch):
    times = []
    real = FakeDriver.force

    def timed(self, value):
        times.append(engine._written_at)
        real(self, value)
    monkeypatch.setattr(FakeDriver, 'force', timed)
    engine, path = engine_on(tmp_path)
    drive = Drive(engine, upload_hz=2000.0).run(7.0)     # uploads faster than the passes
    assert engine.status.active
    engine.set_enabled(False)
    n = len(times)
    drive.run(0.4)
    assert len(times) - n >= 150                          # the 200 ms fade, written each pass
    assert all(b - a >= fe.MIN_WRITE for a, b in zip(times, times[1:]))
    assert forces()[-1] == 0 and levels() == [0, 100]


def test_quit_while_playing_gives_the_game_its_force(tmp_path):
    engine, path = engine_on(tmp_path)
    Drive(engine).run(7.0)
    assert engine.status.active
    engine.stop()
    assert levels() == [0, 100] and forces()[-1] == 0


def test_another_owner_takes_the_wheel_while_playing(tmp_path):
    from oversteer import wheel_owner
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(7.0)
    assert engine.status.active
    assert wheel_owner.acquire(path, 'wheel-test', exclusive=True, timeout=0.0) is False   # asked to let go
    drive.run(0.01)
    assert not engine.status.active and levels() == [0, 100] and forces()[-1] == 0
    assert wheel_owner.acquire(path, 'wheel-test', exclusive=True)
    wheel_owner.release(path, 'wheel-test')


def test_a_failed_write_while_playing_restores_the_game(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(7.0)
    assert engine.status.active
    FakeDriver.fail_force = True
    drive.run(0.05)
    assert not engine.status.active and levels() == [0, 100] and engine.last_stop == 'write'


def test_the_ini_is_read_off_the_engine_thread(tmp_path, monkeypatch):
    from oversteer import rbr_install
    monkeypatch.setattr(rbr_install, 'ASYNC', True)
    root = tmp_path / 'rbr'
    root.mkdir()
    (root / 'RichardBurnsRally.ini').write_text('[NGP]\r\nForceFeedbackSensitivityGravel=50\r\n'
                                                'ForceFeedbackSensitivityTarmac=50\r\n'
                                                'ForceFeedbackSensitivitySnow=50\r\n')
    force = r.RbrForce()
    force.set_car(CAR, None, root=str(root))
    assert force.s is None                                # being read
    force._lookup.join(2.0)
    force.set_car(CAR, None, root=str(root))
    assert force.s == 50.0


def test_profiles_keep_the_linearity(tmp_path):
    from oversteer.model import Model
    from tests.fake_wheel import FakeDevice
    device = FakeDevice(ext_force_support=True)
    model = Model(device, None)
    assert model.data['countersteer_linearity'] == 70 and model.get_countersteer_linearity() == 70
    model.data['countersteer_linearity'] = 40
    profile = str(tmp_path / 'p.ini')
    model.save(profile)
    fresh = Model(device, None)
    fresh.load(profile)
    assert fresh.get_countersteer_linearity() == 40
    (tmp_path / 'old.ini').write_text('[DEFAULT]\nrange = 900\ncountersteer_ffb = 1\n')
    fresh.load(str(tmp_path / 'old.ini'))
    assert fresh.get_countersteer_linearity() == 70
    assert Model(FakeDevice(), None).data['countersteer_linearity'] is None


def test_profiles_keep_the_steering_toggles(tmp_path):
    from oversteer.model import Model
    from tests.fake_wheel import FakeDevice
    device = FakeDevice(ext_force_support=True)
    model = Model(device, None)
    assert model.data['countersteer_column'] is True and model.data['countersteer_align'] is True
    assert model.data['countersteer_friction_removal'] is False
    assert model.get_countersteer_dynamics() == (True, True, False)
    model.data.update(countersteer_column=False, countersteer_align=False, countersteer_friction_removal=True)
    profile = str(tmp_path / 'p.ini')
    model.save(profile)
    fresh = Model(device, None)
    fresh.load(profile)
    assert fresh.get_countersteer_dynamics() == (False, False, True)
    (tmp_path / 'old.ini').write_text('[DEFAULT]\nrange = 900\ncountersteer_ffb = 1\n')
    fresh.load(str(tmp_path / 'old.ini'))
    assert fresh.get_countersteer_dynamics() == (True, True, False)
    plain = Model(FakeDevice(), None)
    assert plain.data['countersteer_column'] is None and plain.data['countersteer_friction_removal'] is None


# -- re-review: the peak per stream, pending writes, app_gain read again, the ini's mtime --

def test_the_peak_counts_only_the_games_own_levels():
    force = r.RbrForce()
    force.feed(0.0, 32002)                               # off DirectInput's grid: not NGP's
    assert not r.on_grid(32002) and force.peak == 0.0
    force.feed(0.001, r.level_of(-9000))
    assert force.peak == abs(r.level_of(-9000))
    force.new_stream('test')
    assert force.peak == 0.0


@pytest.mark.parametrize('how', ['count', 'gap', 'stage'])
def test_a_restarted_stream_measures_the_games_peak_again(tmp_path, how):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(9.0)
    assert engine.status.active and engine.rbr.peak > r.STRENGTH_PROVEN * r.FULL
    if how == 'count':
        FakeDriver.raw = (FakeDriver.raw[0], 0, 0)       # the wheel bound again: the count starts over
        drive.run(0.05)
    elif how == 'gap':
        drive.uploads = False
        drive.run(fe.RBR_STREAM_GAP + 0.1)
        drive.uploads = True
        drive.run(0.05)
    else:
        engine.offer(drive.t, Sample(5000.0, gear=3, speed=20.0, car=CAR, game='rbr'))
        stage = Sample(5000.0, gear=3, speed=20.0, car=CAR, game='rbr')
        stage.stage = 'rbr:10'
        engine.offer(drive.t + 0.0005, stage)
        drive.run(0.01)
    assert engine.rbr.peak < r.STRENGTH_PROVEN * r.FULL
    assert engine.status.reason in ('rbr-strength', 'rbr-raw') or not engine.status.active


def test_a_value_within_a_millisecond_is_written_on_the_next_pass(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(7.0)
    assert engine.status.active
    t = drive.t
    upload(1000)
    engine.step(t)
    n = len(forces())
    upload(-2000)
    engine.step(t + 0.0004)                              # too soon after the last write: nothing written
    assert len(forces()) == n
    engine.step(t + 0.0011)                              # (the value itself goes through the spike gate and slew)
    assert len(forces()) == n + 1


def test_app_gain_turned_on_while_playing_goes_on_playing(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(7.0)
    assert engine.status.active
    with open(path + '/app_gain', 'w') as f:
        f.write('1\n')
    drive.run(1.5)
    assert engine.status.active and engine.status.reason != 'rbr-app-gain'


def test_the_ini_is_read_again_when_it_changes(tmp_path):
    root = tmp_path / 'rbr'
    root.mkdir()
    ini = root / 'RichardBurnsRally.ini'
    ini.write_text('[NGP]\r\nForceFeedbackSensitivityGravel=50\r\nForceFeedbackSensitivityTarmac=50\r\n'
                   'ForceFeedbackSensitivitySnow=50\r\n')
    force = r.RbrForce()
    force.set_car(CAR, None, root=str(root), now=0.0)
    assert force.s == 50.0
    ini.write_text('[NGP]\r\nForceFeedbackSensitivityGravel=200\r\nForceFeedbackSensitivityTarmac=200\r\n'
                   'ForceFeedbackSensitivitySnow=200\r\n')
    import os
    os.utime(ini, (1e9, 1e9))
    force.set_car(CAR, None, root=str(root), now=1.0)
    assert force.s == 50.0                               # not looked at yet
    force.set_car(CAR, None, root=str(root), now=1.0 + r.INI_CHECK)
    assert force.s == 200.0


# -- the rack force: column torque, the sign latch, the chain --

def level_for(x):
    """The raw level NGP sends for a force level x (+ = T positive: the level is negative)."""
    return r.level_of(r.magnitude(x))


@pytest.mark.parametrize('x, s, nm', [(0.5, 100.0, 4.36), (0.9, 100.0, 14.47), (0.5, 105.0, 4.57), (-0.5, 100.0, -4.36)])
def test_the_levels_force_is_the_column_torque(x, s, nm):
    rack = fe.RbrRack()
    rack.set_level(0.0, level_for(x), s)
    # (the level's sign is the column torque's: ext_force follows it)
    assert rack.column == pytest.approx(-nm, abs=0.02)
    assert rack.s_note is None
    assert abs(rack.column) == pytest.approx(0.062862 * -math.log(1 - abs(x)) * s, rel=2e-3)


def test_an_unknown_s_is_100_with_a_note_and_no_level_no_torque():
    rack = fe.RbrRack()
    rack.set_level(0.0, level_for(0.5), None)
    assert rack.column == pytest.approx(-4.36, abs=0.02) and rack.s_note
    rack.set_level(0.001, None, None)
    assert rack.column is None and rack.rim_torque(rbr_sample()) is None
    rack.set_level(0.002, 0, 100.0)
    assert rack.column == 0.0


def test_the_torque_goes_through_the_boost_curve_and_the_mapping():
    rack = fe.RbrRack()
    rack.wheel_peak = 3.5
    rack.set_level(0.0, level_for(0.5), 100.0)
    sample = rbr_sample()
    from oversteer import ffb_carprops as cp
    assert rack.power_steering() == 'model'
    assert rack.rim_torque(sample) == pytest.approx(cp.driver_from_column(rack.column, 20.0, 1.0, True))
    assert abs(rack.rim_torque(sample)) < abs(rack.column)              # the boost took part of it
    assert rack.force(sample) < 0 and abs(rack.force(sample)) <= fe.FULL    # the level's sign, inside the budget
    rack.cut_hz = None                                                 # (the low-pass would glide through the sign change)
    rack.set_level(0.001, -level_for(0.5), 100.0)
    assert rack.force(sample) > 0
    assert not rack.LEARNS and rack.learner is None and rack.assist == 1.0


def drive_rack(rack, seconds, direction=1.0, ay=0.0, speed=20.0, hz=236.0, start=0.0):
    t = start
    while t < start + seconds:
        steer = 0.3 * math.sin(2 * math.pi * 0.3 * t)
        level = level_for(direction * steer * 2.0)          # (NGP's level opposes the steering when it centres)
        rack.feed(t, rbr_sample(speed=speed, steer=steer, ay=ay), level)
        t += 1.0 / hz
    return rack


def test_the_sign_latches_normal_after_2_s_of_straight_samples():
    rack = fe.RbrRack()
    assert not rack.ready and rack.waiting == 'direction'
    drive_rack(rack, 1.5)
    assert rack.direction is None and not rack.ready
    drive_rack(rack, 4.0, start=1.5)
    assert rack.direction == 'normal' and rack.ready and rack.refused is None
    assert rack.sign_r <= -0.5 and rack.sign_time >= rack.SIGN_TIME


def test_an_inverted_direction_is_refused():
    rack = drive_rack(fe.RbrRack(), 4.0, direction=-1.0)
    assert rack.direction == 'inverse' and not rack.ready and rack.refused == 'inverse'


@pytest.mark.parametrize('kw', [dict(ay=2.0), dict(speed=3.0)])
def test_the_latch_only_looks_at_straight_samples_at_speed(kw):
    rack = drive_rack(fe.RbrRack(), 6.0, **kw)
    assert rack.direction is None and rack.sign_time == 0.0 and not rack.ready


def test_a_restarted_stream_judges_the_sign_again():
    rack = drive_rack(fe.RbrRack(), 4.0)
    assert rack.ready
    rack.restart_sign()
    assert rack.direction is None and rack.sign_time == 0.0
    assert rack.ready and rack.waiting is None            # a latched 'normal' goes on playing while it is judged again
    fresh = fe.RbrRack()
    fresh.restart_sign()
    assert not fresh.ready and fresh.waiting == 'direction'


def test_the_engine_refuses_an_inverted_direction(tmp_path):
    engine, path = engine_on(tmp_path)
    Drive(engine, direction=-1.0).run(8.0)
    assert engine.status[:2] == ('model', 'rbr-inverse') and FakeDriver.log == []
    assert 'DIRECTION' in fe.status_text(engine.status)[1]


def test_ready_is_the_gates_and_the_sign_latch_alone_no_fit_to_the_tyres(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(1.5)
    assert not engine.status.ready                                       # the latch / the gates are still waiting
    drive.run(7.0)
    rack = engine.rbr_rack
    assert engine.status.ready and engine.status.active and rack.ready and rack.fit is None
    assert engine.model.moving_time == 0.0 and not engine.model.ready    # the ACR model fitted nothing
    assert rack.moving_time == 0.0 and rack.learner is None


def test_drive_pull_and_kickback_are_forced_off_for_rbr(tmp_path):
    engine, path = engine_on(tmp_path)
    engine.set_drive_pull(True, 100.0)
    engine.set_kick(True, 100.0)
    engine.set_knee(40.0)
    engine.set_column_damping(30.0)
    engine.step(0.0)
    assert engine.model.pull_on and engine.model.kick_on                 # ACR has them
    rack = engine.rbr_rack
    assert not rack.pull_on and not rack.kick_on and rack.pull_scrub() == 0.0
    assert rack.knee == engine.model.knee and rack.damping_pct == 30.0   # the rest of the settings are shared


def test_the_settings_reach_the_rbr_rack(tmp_path):
    engine, path = engine_on(tmp_path)
    engine.set_rim_mm(300.0)
    engine.set_car_wheel_mm(360.0)
    engine.set_mapping('classic')
    engine.step(0.0)
    rack = engine.rbr_rack
    assert rack.rim_user == 300.0 and rack.car_wheel_mm == 360.0 and rack.classic() and rack.rho() == pytest.approx(300.0 / 360.0)


def dyn_engine(tmp_path):
    from tests.test_ffb_dynamics import dyn_wheel, POS
    path = dyn_wheel(tmp_path)
    engine = fe.FfbEngine(driver=FakeDriver, beamng_source=None)
    engine.set_position_source(POS)
    engine.set_device(path)
    engine.set_enabled(True)
    engine.step(0.0)
    return engine, path, POS


def test_the_lock_stop_is_allowed_for_rbr_at_half_the_range(tmp_path):
    engine, path, pos = dyn_engine(tmp_path)
    drive = Drive(engine).run(8.0)
    assert engine.status.active and engine._played_mode == 'rbr'
    t = drive.t

    def push_at(deg):
        pos.raw = int(round((deg / 900.0 + 0.5) * 65535))
        engine._pos_cache = None
        return engine._lock_stop_force(t, rbr_sample())
    assert push_at(440.0) == 0.0 and not engine._lock_stop.active          # inside range / 2 = 450 degrees
    push = push_at(470.0)
    assert engine._lock_stop.active and push > 0.3 * fe.FULL
    assert push_at(-470.0) < 0


def test_the_column_damping_is_written_for_rbr(tmp_path):
    engine, path, pos = dyn_engine(tmp_path)
    Drive(engine).run(8.0)
    assert engine.status.active
    assert int(open(path + '/ffb_dyn_damping').read()) > 0


def test_the_texture_runs_for_rbr_without_the_lock_part(tmp_path):
    from oversteer import ffb_texture as ft
    assert 'rbr' in ft.TEXTURE_GAMES
    rnd = random.Random(3)
    tx = ft.Texture()
    tx.set(1.0, {name: name in ('road', 'impact') for name in ft.PARTS})
    peak = 0.0
    for i in range(400):
        s = rbr_sample()
        s.susp_vel = (rnd.gauss(0, 0.3), rnd.gauss(0, 0.3), 0.0, 0.0)           # a rough road: the struts' velocity
        s.tyre_load = (3000.0, 3000.0, 2500.0, 2500.0)
        peak = max(peak, abs(tx.force(i / 236.0, s, 4000.0)))
    assert peak > 0.0 and peak <= ft.TOTAL_CAP * ft.FULL
    # the lock part is off: wheels locked under braking say nothing for RBR
    lock = ft.Texture()
    lock.set(1.0, {name: name == 'lock' for name in ft.PARTS})
    for i in range(400):
        s = rbr_sample()
        s.brake, s.wheel_speed, s.vel = 1.0, (2.0, 2.0, 20.0, 20.0), (20.0, 0.0, 0.0)
        assert lock.force(i / 236.0, s, 4000.0) == 0.0
    # the engine hum stays (the rpm)
    hum = ft.Texture()
    hum.set(1.0, {name: name == 'engine' for name in ft.PARTS})
    assert max(abs(hum.force(i / 236.0, rbr_sample(), 4000.0)) for i in range(600)) > 0.0


def test_the_engine_steps_the_texture_on_rbr_samples(tmp_path):
    engine, path = engine_on(tmp_path)
    engine.set_texture(1.0, {'road': True, 'impact': False, 'lock': False, 'engine': True})
    rnd = random.Random(4)

    class Rough(Drive):
        def sample(self, t):
            s = super().sample(t)
            s.susp_vel = (rnd.gauss(0, 0.3), rnd.gauss(0, 0.3), 0.0, 0.0)
            return s
    Rough(engine).run(9.0)
    assert engine.status.active and engine.texture.peak > 0.0


def test_the_take_over_logs_car_s_torque_p99_rho_and_knee(tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO)
    engine, path = engine_on(tmp_path)
    Drive(engine).run(8.0)
    assert engine.status.active
    line = [m for m in caplog.messages if 'replacing Richard Burns Rally' in m]
    assert line and CAR in line[0] and 'S 100.0' in line[0] and 'column torque p99' in line[0]
    assert 'hand-force scale' in line[0] and '1:1 up to' in line[0]


def test_the_glide_in_starts_from_the_raw_level(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine)
    n = len(forces())
    while not engine._phase in ('in', 'on') and drive.t < 12.0:
        drive.run(0.05)
    assert engine._phase in ('in', 'on') and engine._w < 0.2
    new = forces()[n:]
    assert new and abs(new[-1]) <= abs(FakeDriver.raw[0]) * 1.2 + 2000      # near the game's own level, not a step to ours


# -- Wine's measured mapping and the self-calibrated STRENGTH (20261103-133906: a game at 20 %) --

def test_wines_mapping_matches_the_measured_levels():
    # the drive's levels: m = 1, 2, 3 -> 2, 6, 9; m = 0 -> -1; m = -1, -2, -3 -> -4, -8, -11; the ends of 20 %
    assert [r.level_of(m) for m in (1, 2, 3, 0, -1, -2, -3)] == [2, 6, 9, -1, -4, -8, -11]
    assert r.level_of(1999) == 6549 and r.level_of(-2000) == -6554
    for level in (-6554, -6532, -6496, 6549, 2, -1):
        assert r.on_grid(level)
    assert not r.on_grid(3) and not r.on_grid(-5)


def fit_drive(strength, seconds=120.0, trail=0.060, hz=100.0):
    """StrengthFit fed a corner-weaving drive whose level NGP makes at `strength` from Newton's torque."""
    fit = r.StrengthFit()
    speed, n = 20.0, int(seconds * hz)
    for i in range(n):
        t = i / hz
        ay = 3.5 + 3.0 * math.sin(2 * math.pi * 0.2 * t) + 0.5 * math.sin(2 * math.pi * 0.77 * t)
        yaw = ay / speed
        fy = r.CAR_M * (r.CAR_B / r.CAR_L) * ay + r.CAR_IZ * (3.0 * 2 * math.pi * 0.2 * math.cos(2 * math.pi * 0.2 * t) / speed) / r.CAR_L
        torque = 2 * math.pi * trail * fy / r.CAR_N
        level = r.level_of(r.magnitude(r.ngp_curve(torque, 100.0), strength))
        fit.feed(t, ay, yaw, speed, level)
    return fit


def test_the_fit_finds_a_strength_of_0_2():
    fit = fit_drive(0.2, trail=0.040)
    assert fit.converged and abs(fit.s - 0.2) <= 0.02 and fit.r > 0.95
    assert 35 < fit.trail_mm(100.0) < 45                 # the 40 mm trail it was made with


def test_the_fit_finds_a_strength_of_1():
    fit = fit_drive(1.0, trail=0.030)
    assert fit.s == 1.0 and fit.r > 0.95


def test_the_fit_picks_by_trail_100_and_25_percent():
    fit = fit_drive(1.0, trail=0.040, seconds=40.0)
    assert fit.s == 1.0 and fit.plausible and fit.stable
    fit = fit_drive(0.25, trail=0.040, seconds=40.0)
    assert fit.s == 0.25 and fit.plausible and fit.stable
    assert 30 < fit.trail_mm(100.0) < 50


def test_a_light_early_drive_at_100_percent_stays_at_1():
    """A low peak (a gentle drive at STRENGTH 1) must not read as a low STRENGTH: s < 1 would put the trail past 60 mm."""
    light = r.StrengthFit()
    speed, hz = 20.0, 100.0
    for i in range(int(40 * hz)):
        t = i / hz
        ay = 2.0 + 0.8 * math.sin(2 * math.pi * 0.2 * t) + 0.3 * math.sin(2 * math.pi * 0.77 * t)
        fy = r.CAR_M * (r.CAR_B / r.CAR_L) * ay + r.CAR_IZ * (0.8 * 2 * math.pi * 0.2 * math.cos(2 * math.pi * 0.2 * t) / speed) / r.CAR_L
        torque = 2 * math.pi * 0.030 * fy / r.CAR_N
        light.feed(t, ay, ay / speed, speed, r.level_of(r.magnitude(r.ngp_curve(torque, 100.0), 1.0)))
    assert light.peak < 0.6 and light.s == 1.0 and light.plausible
    f = r.RbrForce()
    for i in range(400):
        f.feed_physics(i * 0.1, moving_sample(), 0)
    assert f.s_applied == 1.0


def test_the_strength_is_1_until_converged_and_the_force_never_heavier():
    fit = fit_drive(0.2, seconds=10.0)
    assert not fit.converged and fit.s == 1.0
    force = r.RbrForce()
    assert force.strength == 1.0
    level = r.level_of(-1000)
    assert force._x(level) == abs(level) / r.FULL
    force.full = r.FULL * 0.2                            # a fitted 20 % makes the same level a bigger x
    assert force._x(level) > abs(level) / r.FULL


def test_a_20_percent_game_is_not_held_back_by_its_peak():
    force = r.RbrForce()
    force.peak = 0.2 * r.FULL
    force.changes, force.force_time = 500, 0.0
    force._grid.add(True, 1.0)
    force._judge(False)
    assert force.ready and force.refused is None and force.waiting is None


def moving_sample(speed=20.0):
    sample = Sample(5000.0, gear=3, speed=speed, car=CAR, game='rbr')
    sample.steer, sample.accel, sample.yaw_rate = 0.0, (0.0, 0.0, 0.0), 0.0
    return sample


def clip_drive(force, top, sample, seconds, t0=0.0, rise=True, dt=0.004):
    """Uploads that rise from half the top and clip at it (rise False: the top alone, a constant value); returns t."""
    half = abs(r.round_trip(-(top // 2)))                 # (on the grid, or it is not counted)
    t = t0
    while t < t0 + seconds:
        level = -top if (not rise or (t % 1.0) < 0.6) else -half
        force.feed(t, level, holding=True)
        force.feed_physics(t, sample, level)
        t += dt
    return t


def test_the_strength_glides_to_a_clipped_top_and_a_restart_keeps_it():
    force = r.RbrForce()
    top = -r.level_of(r.magnitude(1.0, 0.2))
    half = r.level_of(r.magnitude(0.5, 0.2))
    t = 0.0
    sample = Sample(5000.0, gear=3, speed=20.0, car=CAR, game='rbr')
    sample.steer, sample.accel, sample.yaw_rate = 0.0, (0.0, 0.0, 0.0), 0.0
    applied = []
    while t < 40.0:
        force.feed(t, -top if (t % 1.0) < 0.6 else half, holding=True)
        force.feed_physics(t, sample, -top)
        applied.append(force.s_applied)
        t += 0.004
    assert force.s_plateau == pytest.approx(0.2, abs=0.01)
    assert applied[0] == 1.0 and applied[-1] == pytest.approx(0.2, abs=0.02)
    steps = [abs(b - a) for a, b in zip(applied, applied[1:])]
    assert max(steps) < 0.001                              # no step: a smooth glide
    assert 0.3 < applied[int(10.0 / 0.004)] < 0.9 and applied[int(25.0 / 0.004)] < 0.25
    force.new_stream('the uploads restarted')
    assert force.peak == 0.0 and force.s_plateau is not None and force.target_s() == force.s_plateau
    assert force.ceiling >= 0.99 * top                     # (not 1: a restart does not leave the wheel limp)


def test_the_engine_takes_over_in_seconds_on_a_20_percent_game(tmp_path):
    engine, path = engine_on(tmp_path)
    Drive(engine, amp=60.0, strength=0.2).run(8.0)
    assert engine.status.active
    assert engine.rbr.strength > 0.9                       # still the prior 1: light until the evidence glides it


def test_the_confirmed_strength_is_kept_and_starts_the_next_session(caplog):
    caplog.set_level('INFO')
    first = r.RbrForce()
    assert first.s_applied == 1.0 and r.kept_strength() is None
    top = -r.level_of(r.magnitude(1.0, 0.2))
    sample = Sample(5000.0, gear=3, speed=20.0, car=CAR, game='rbr')
    sample.steer, sample.accel, sample.yaw_rate = 0.0, (0.0, 0.0, 0.0), 0.0
    clip_drive(first, top, sample, 8.0)
    assert first.s_plateau == pytest.approx(0.2, abs=0.01) and r.kept_strength() == pytest.approx(0.2, abs=0.01)
    second = r.RbrForce()                                    # the next session
    assert second.s_applied == pytest.approx(0.2, abs=0.01) and second.full < 0.25 * r.FULL
    assert second.target_s() == second.s_kept                # (the fit saying 1 does not undo it)
    assert 'kept from the last session' in caplog.text
    other = -r.level_of(r.magnitude(1.0, 0.5))               # another top: glide to it and overwrite
    clip_drive(second, other, sample, 8.0)
    assert second.s_plateau == pytest.approx(0.5, abs=0.01) and r.kept_strength() == pytest.approx(0.5, abs=0.01)
    assert 0.5 <= second.s_applied < 1.0                       # (back to the light prior on the first impossible level, then down to it)


def test_the_kept_strength_beats_the_fit_until_a_plateau_or_a_raised_level():
    from oversteer import ffb_rbr
    f = ffb_rbr.RbrForce()
    f.s_kept, f.s_applied, f.s_plateau = 0.25, 0.25, None
    f.fit.converged, f.fit.plausible, f.fit.s = True, True, 0.30      # a fit that says 0.30 does not override the kept 0.25
    assert f.target_s() == 0.25
    f.s_plateau = 0.4                                                  # a clipped top comes first
    assert f.target_s() == 0.4
    f.s_plateau, f.s_kept = None, None                                 # nothing kept: the fit
    assert f.target_s() == 0.30
    f.s_kept, f.peak = 0.25, int(0.5 * ffb_rbr.FULL)                   # a level over the kept top: dropped, the fit takes over
    f.feed_physics(1.0, None, 0)
    assert f.s_kept is None and f.target_s() == 0.30


def test_a_level_over_the_kept_strength_drops_it_at_once():
    from oversteer import ffb_rbr
    f = ffb_rbr.RbrForce()
    f.s_kept, f.s_applied, f.s_plateau = 0.2, 0.2, None
    f.peak = int(0.5 * ffb_rbr.FULL)           # only a STRENGTH of 0.5 or more makes this level
    f.feed_physics(1.0, None, 0)
    assert f.s_kept is None and f.s_applied > 0.9


# -- the clipped-top detector's gates and the asymmetric strength (2026-11-03 17:26: 0.10 from a stage load) --

def test_a_plateau_needs_the_car_moving():
    top = -r.level_of(r.magnitude(1.0, 0.2))
    f = r.RbrForce()
    clip_drive(f, top, moving_sample(speed=2.0), 8.0)
    assert f.s_plateau is None                               # a held force at rest (a stage load) is no clip
    f = r.RbrForce()
    clip_drive(f, top, moving_sample(speed=20.0), 8.0)
    assert f.s_plateau == pytest.approx(0.2, abs=0.01)


def test_a_plateau_needs_telemetry():
    top = -r.level_of(r.magnitude(1.0, 0.2))
    f = r.RbrForce()
    t = 0.0
    sample = moving_sample()
    f.feed_physics(0.0, sample, -top)                        # one sample, then only uploads
    half = r.round_trip(-(top // 2))
    for i in range(2500):
        t = i * 0.004
        f.feed(t, -top if (t % 1.0) < 0.6 else half, holding=True)
    assert f.s_plateau is None


def test_no_plateau_within_10_s_of_a_restart_or_a_stage_change():
    top = -r.level_of(r.magnitude(1.0, 0.2))
    for why in ('the uploads restarted', 'a new stage or a restart'):
        f = r.RbrForce()
        sample = moving_sample()
        f.feed_physics(0.0, sample, 0)
        f.new_stream(why)
        t = clip_drive(f, top, sample, 9.0, t0=0.1)
        assert f.s_plateau is None, why                      # (the 2.9 s after the restart in the real log)
        clip_drive(f, top, sample, 8.0, t0=t)
        assert f.s_plateau == pytest.approx(0.2, abs=0.01)


def test_a_constant_top_is_no_plateau_but_a_rise_to_it_is():
    top = -r.level_of(r.magnitude(1.0, 0.2))
    f = r.RbrForce()
    clip_drive(f, top, moving_sample(), 20.0, rise=False)
    assert f.s_plateau is None
    f = r.RbrForce()
    clip_drive(f, top, moving_sample(), 8.0)
    assert f.s_plateau is not None


def test_a_top_under_15_percent_of_full_scale_is_no_plateau():
    top = -r.level_of(r.magnitude(1.0, 0.1))
    f = r.RbrForce()
    clip_drive(f, top, moving_sample(), 12.0)
    assert f.s_plateau is None and abs(top) < 0.15 * r.FULL


def test_a_stage_load_at_strength_100_writes_nothing(tmp_path):
    # 17:26:17..20 of the real session: uploads restarted, a held level, a speed that is not a drive
    f = r.RbrForce()
    sample = moving_sample(speed=0.0)
    t = clip_drive(f, 6918, sample, 6.0, rise=False)
    assert f.s_plateau is None and r.kept_strength() is None


def test_the_applied_strength_rises_at_once_and_only_a_plateau_makes_it_heavier():
    f = r.RbrForce()
    sample = moving_sample()
    f.s_applied = 0.3
    f.fit.converged, f.fit.plausible, f.fit.s = True, True, 0.6
    f.feed_physics(1.0, sample, 0)
    assert f.s_applied == pytest.approx(0.6)                 # lighter: at once, on the fit
    f.fit.s = 0.4
    for i in range(200):
        f.feed_physics(1.0 + 0.01 * i, sample, 0)
    assert f.s_applied == pytest.approx(0.6)                 # a fit alone never makes it heavier
    f.s_plateau = 0.4
    f.feed_physics(10.0, sample, 0)
    f.feed_physics(10.1, sample, 0)
    assert 0.59 > f.s_applied > 0.55                         # a plateau does, gliding
    assert f.s_applied > 0.5


def test_a_converged_fit_alone_keeps_the_kept_strength_from_being_lowered():
    r.keep_strength(0.5)
    f = r.RbrForce()
    f.fit.converged, f.fit.plausible, f.fit.s = True, True, 0.2
    for i in range(100):
        f.feed_physics(i * 0.1, moving_sample(), 0)
    assert f.s_applied == pytest.approx(0.5)


def test_keep_strength_is_written_for_a_plateau_or_a_fit_not_for_the_drop():
    r.keep_strength(0.2)
    f = r.RbrForce()
    f.peak = int(0.5 * r.FULL)
    f.feed_physics(1.0, None, 0)
    assert f.s_kept is None and f.s_applied == 1.0
    assert r.kept_strength() == pytest.approx(0.2)           # the drop leaves the file alone
    for i in range(50):
        f.feed_physics(1.0 + i, None, 0)
    assert r.kept_strength() == pytest.approx(0.2)           # ... and so does what follows it
    f.fit.converged, f.fit.plausible, f.fit.s = True, True, 0.7
    f.fit._since, f.fit.time = 0.0, 20.0
    f.feed_physics(100.0, None, 0)
    assert r.kept_strength() == pytest.approx(0.7)           # only a later confirmed value overwrites it


def test_an_undecided_sign_plays_on_the_normal_prior_and_an_inverted_one_is_still_refused():
    rack = fe.RbrRack()
    t = 0.0
    while t < 3.0:                                          # the level says nothing about the steering: |r| stays low
        rack.feed(t, rbr_sample(speed=20.0, steer=0.3 * math.sin(2 * math.pi * 0.3 * t), ay=0.0),
                  level_for(math.sin(2 * math.pi * 11.0 * t)))
        t += 1.0 / 236.0
    assert rack.direction is None and rack.ready and rack.waiting is None
    drive_rack(rack, 30.0, direction=-1.0, start=t)
    assert rack.direction == 'inverse' and not rack.ready and rack.refused == 'inverse'


# -- the RBR force cut: the column torque's low-pass --

def cut_rack(cut=30.0, **kw):
    rack = fe.RbrRack()
    rack.cut_hz = cut
    return rack


def run_cut(rack, fn, seconds, dts):
    """(t, y) of the filtered column for the raw column fn(t), sampled at the steps cycling through dts."""
    out, t, i = [], 0.0, 0
    while t < seconds:
        rack.set_level(t, level_for(fn(t)), 100.0)
        out.append((t, rack.column, rack.column_raw))
        t += dts[i % len(dts)]
        i += 1
    return out


def amp(rows, f, skip=0.5):
    """Amplitude of the component at f Hz in column (index 1) or column_raw (2), after `skip` s."""
    import numpy as np
    t = np.array([r[0] for r in rows if r[0] >= skip])
    return [2 * abs(np.mean(np.array([r[k] for r in rows if r[0] >= skip]) * np.exp(-2j * np.pi * f * t))) for k in (1, 2)]


@pytest.mark.parametrize('f, lo, hi', [(100.0, 0.0, 0.15), (5.0, 0.95, 1.05)])
def test_the_cut_removes_vibration_and_passes_the_slow_force(f, lo, hi):
    rows = run_cut(cut_rack(), lambda t: 0.2 + 0.1 * math.sin(2 * math.pi * f * t), 2.0, [0.001])
    filtered, raw = amp(rows, f)
    assert lo <= filtered / raw <= hi


def test_a_step_through_the_cut_does_not_overshoot():
    rack = cut_rack()
    rack.set_level(0.0, level_for(0.1), 100.0)
    rows = run_cut(rack, lambda t: 0.1 if t < 0.05 else 0.4, 0.4, [0.001])
    start, end = abs(rows[0][2]), abs(rows[-1][2])
    assert max(abs(r[1]) for r in rows) - end < 0.1 * (end - start) and abs(abs(rows[-1][1]) - end) < 0.01 * end


def test_irregular_steps_are_stable():
    rack = cut_rack()
    rows = run_cut(rack, lambda t: 0.3, 3.0, [0.0004, 0.0031, 0.0009, 0.02, 0.0013, 0.09])
    assert all(math.isfinite(r[1]) and abs(r[1] - r[2]) < 1e-6 for r in rows)
    rows = run_cut(cut_rack(), lambda t: 0.3 + 0.2 * (random.Random(int(t * 1e4)).random() - 0.5), 3.0, [0.0004, 0.0031, 0.0009, 0.02, 0.0013, 0.09])
    assert max(abs(r[1]) for r in rows) < 1.2 * max(abs(r[2]) for r in rows)


def test_a_restart_or_a_gap_does_not_step():
    rack = cut_rack()
    for i in range(50):
        rack.set_level(i * 0.001, level_for(0.3), 100.0)
    rack.set_level(0.05 + 0.3, level_for(0.6), 100.0)               # a gap over 0.1 s
    assert rack.column == pytest.approx(rack.column_raw)
    for i in range(1, 5):
        rack.set_level(0.35 + i * 0.001, level_for(0.6), 100.0)
    rack.restart_sign()                                              # a new stream
    rack.set_level(0.36, level_for(-0.2), 100.0)
    assert rack.column == pytest.approx(rack.column_raw) and rack.column > 0


def test_the_cut_off_passes_the_column_unchanged():
    for cut in (None, fe.RBR_CUT_OFF):
        rack = cut_rack(cut)
        rows = run_cut(rack, lambda t: 0.2 + 0.1 * math.sin(2 * math.pi * 100 * t), 0.2, [0.001])
        assert all(r[1] == r[2] for r in rows)


def test_the_cut_setting_reaches_the_rack_and_the_snapshot(tmp_path):
    engine, path = engine_on(tmp_path)
    assert engine.rbr_rack.cut_hz == 30.0
    engine.set_rbr_cut(60)
    engine.step(0.01)
    assert engine.rbr_rack.cut_hz == 60.0 and engine.settings_snapshot()['profile']['rbr_cut_hz'] == 60.0
    engine.set_rbr_cut(120)
    engine.step(0.02)
    assert engine.rbr_rack.cut_hz is None and engine.settings_snapshot()['profile']['rbr_cut_hz'] is None


# -- the caster floor: in a slide the column torque never falls below what mechanical caster alone gives --

def slide_sample(ay=6.0, vx=15.0, vy=4.0, yaw=0.0, t=0.0, car=CAR):
    s = rbr_sample(car=car, speed=math.hypot(vx, vy), ay=ay)
    s.vel, s.yaw_rate, s.game_time, s.drivetrain = (vx, vy, 0.0), yaw, t, 'awd'
    return s


def run_floor(rack, level, seconds=1.0, hz=236.0, start=0.0, **kw):
    """Feed the rack a level and a slide sample for `seconds`; the rows are (t, column, lift, rim torque)."""
    rows, t = [], start
    while t < start + seconds:
        rack.set_level(t, level, 100.0)
        sample = slide_sample(t=t, **kw)
        rack.feed(t, sample, None)
        rows.append((t, rack.column, rack.floor_lift, rack.rim_torque(sample)))
        t += 1.0 / hz
    return rows


def floor_nm(rack, ay=6.0):
    fy = fz.front_lateral(rack.FLOOR_MASS, fz.front_share('awd'), ay, 0.0)
    return fe.ffb_carprops.RALLY_TRAIL * min(abs(fy), fz.front_share(None) * rack.FLOOR_MASS * rack.FLOOR_AY) / rack.ratio


def test_grip_leaves_the_column_torque_exactly():
    rack = fe.RbrRack()
    rows = run_floor(rack, level_for(0.5), vy=0.2)       # body slip 0.013 rad: grip
    assert all(row[2] == 0.0 for row in rows) and rack.floor_lift == 0.0
    plain = fe.RbrRack()
    plain.floor_level = 0.0
    assert [row[3] for row in rows] == [row[3] for row in run_floor(plain, level_for(0.5), vy=0.2)]


def test_a_slide_with_a_low_column_torque_is_lifted_to_the_floor_against_fy():
    rack = fe.RbrRack()
    rows = run_floor(rack, level_for(0.02), seconds=1.5)         # NGP is almost silent
    floor = floor_nm(rack)
    assert floor > 1.0 and abs(rack.column) < 0.2 * floor
    assert rack.floor_sign == -1.0 and rack.floor_lift == pytest.approx(-floor + rack.column, abs=0.05 * floor)   # (ay > 0: Fy_f left, floor right)
    assert rack.column + rack.floor_lift == pytest.approx(-floor, rel=0.05)
    assert rack.rim_torque(slide_sample()) != fe.RbrRack().rim_torque(slide_sample())
    rack2 = fe.RbrRack()
    run_floor(rack2, level_for(0.02), seconds=1.5, ay=-6.0)
    assert rack2.floor_sign == 1.0 and rack2.floor_lift > 0.0


def test_a_zero_level_setting_gives_no_lift():
    rack = fe.RbrRack()
    rack.floor_level = 0.0
    rows = run_floor(rack, level_for(0.02))
    assert all(row[2] == 0.0 for row in rows)


def test_a_column_torque_above_the_floor_gets_no_lift():
    rack = fe.RbrRack()
    rows = run_floor(rack, level_for(0.95), seconds=1.5)         # the strong force on the floor's side
    assert abs(rack.column) > floor_nm(rack) and rack.column * rack.floor_sign > 0.0
    assert all(abs(row[2]) < 1e-6 for row in rows)


def test_the_floor_ramps_with_body_slip_and_speed():
    rack = fe.RbrRack()
    run_floor(rack, level_for(0.02), seconds=1.0, vx=15.0, vy=15.0 * math.tan(0.10))
    half = abs(rack.column + rack.floor_lift)
    run_floor(rack, level_for(0.02), seconds=1.0, start=1.0)
    full = abs(rack.column + rack.floor_lift)
    assert 0.0 < half < full
    slow = fe.RbrRack()
    run_floor(slow, level_for(0.02), seconds=1.0, vx=3.0, vy=1.0)
    assert slow.floor_lift == 0.0


def test_the_lift_does_not_step_across_a_sign_change():
    rack = fe.RbrRack()
    run_floor(rack, level_for(0.02), seconds=1.0)
    out, t = [], 1.0
    while t < 2.0:                       # the lateral acceleration swings from +6 to -6 m/s^2 over half a second
        rows = run_floor(rack, level_for(0.02), seconds=1.0 / 236.0, start=t, ay=max(6.0 - 24.0 * (t - 1.0), -6.0))
        out.append(rows[0][1] + rows[0][2])
        t += 1.0 / 236.0
    assert max(abs(b - a) for a, b in zip(out, out[1:])) < 1.0
    assert out[0] < 0.0 < out[-1]


def test_missing_or_bad_fields_give_no_lift_and_do_not_fail():
    rack = fe.RbrRack()
    run_floor(rack, level_for(0.02), seconds=1.0)
    assert rack.floor_lift != 0.0
    t = 1.0
    for field, value in (('accel', None), ('vel', None), ('yaw_rate', None), ('speed', float('nan'))):
        sample = slide_sample(t=t)
        setattr(sample, field, value)
        rack.set_level(t, level_for(0.02), 100.0)
        rack.feed(t, sample, None)
        assert math.isfinite(rack.rim_torque(sample))
        t += 0.004
    rack.feed(t, None, None)
    for _i in range(200):
        t += 0.004
        rack.set_level(t, level_for(0.02), 100.0)
        rack.feed(t, rbr_sample(), None)
    assert abs(rack.floor_lift) < 1e-6


def test_the_floor_state_resets_and_no_level_is_no_lift():
    rack = fe.RbrRack()
    run_floor(rack, level_for(0.02))
    assert rack.floor_lift != 0.0
    rack.set_level(2.0, None, 100.0)
    assert rack.floor_lift == 0.0 and rack.rim_torque(slide_sample()) is None
    run_floor(rack, level_for(0.02), start=3.0)
    rack.restart_sign()
    assert rack.floor_lift == 0.0 and rack.floor_sign is None and rack._slow is None
    run_floor(rack, level_for(0.02), start=5.0)
    rack.reset(CAR)
    assert rack.floor_lift == 0.0 and rack.floor_sign is None and rack.column is None


def test_the_caster_setting_reaches_the_rack_and_the_snapshot(tmp_path):
    engine, path = engine_on(tmp_path)
    assert engine.rbr_rack.floor_level == 1.0
    engine.set_caster_floor(40)
    engine.step(0.01)
    assert engine.rbr_rack.floor_level == pytest.approx(0.4) and engine.settings_snapshot()['profile']['caster_floor'] == 40.0


def test_the_engine_feel_setting_reaches_the_hum_and_the_snapshot(tmp_path):
    engine, path = engine_on(tmp_path)
    engine.set_engine_feel(150)
    engine.step(0.01)
    assert engine.texture.engine.feel == pytest.approx(1.5) and engine.settings_snapshot()['profile']['engine_feel'] == 150.0
    engine.set_engine_feel(900)                                        # clamped to 600 %
    engine.step(0.02)
    assert engine.texture.engine.feel == pytest.approx(6.0)


def test_a_big_slide_fades_the_floor_as_acr_does():
    """Past 0.4 rad the floor falls to half by 0.8 rad, not lower."""
    rack = fe.RbrRack()
    rack.reset(None)
    sizes = {}
    for beta in (0.3, 0.6, 0.8, 1.2):
        rack._restart_floor()
        sample = types.SimpleNamespace(speed=20.0, accel=(0.0, 6.0, 0.0), vel=(20.0 * math.cos(beta), 20.0 * math.sin(beta), 0.0), yaw_rate=0.0, game_time=None, drivetrain=None)
        sizes[beta] = rack._floor_size(1.0, sample)
    assert sizes[0.6] == pytest.approx(sizes[0.3] * 0.75)
    assert sizes[0.8] == pytest.approx(sizes[0.3] * 0.5)
    assert sizes[1.2] == pytest.approx(sizes[0.3] * 0.5)


def test_the_floor_never_cancels_ngp_when_it_pulls_the_other_way():
    """Field: NGP at -13.8 Nm with the floor's sign +: the lift of +13 Nm left the wheel at no force for 0.23 s."""
    rack = fe.RbrRack()
    rack.reset(None)
    t, beta = 1.0, 0.3
    for _ in range(400):
        t += 1.0 / 236.0
        rack.column = -13.8
        rack.feed(t, types.SimpleNamespace(speed=25.0, accel=(0.0, -6.0, 0.0), vel=(25.0 * math.cos(beta), 25.0 * math.sin(beta), 0.0), yaw_rate=0.0, game_time=None, drivetrain=None, gear=3, rpm=5000.0), None)
    assert rack.floor_sign == 1.0
    assert abs(rack.floor_lift) < 1e-3


# -- the aligning spring --

def spring_sample(steer=0.3, vx=20.0, vy=0.0, yaw=0.0, speed=20.0, car=CAR, drivetrain='awd'):
    s = rbr_sample(car, speed=speed, steer=steer)
    s.vel, s.yaw_rate, s.drivetrain = (vx, vy, 0.0), yaw, drivetrain
    s.pos, s.forward = (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)
    return s


def table_rack(car=CAR):
    rack = fe.RbrRack()
    rack.wheel_peak = 3.5
    rack.reset(car)
    rack.ready = True
    return rack


def test_the_kinematic_slip_is_positive_when_steered_further_left_than_the_car_travels():
    from oversteer import ffb_carprops as cp
    rack = table_rack()
    lock = math.radians(270.0) / rack.ratio
    assert rack.ratio_known and rack.lock_known and rack.lock == pytest.approx(lock)
    assert rack._slip(spring_sample(steer=0.0)) == pytest.approx(0.0)
    assert rack._slip(spring_sample(steer=0.3)) == pytest.approx(0.3 * lock)           # straight on, steered left
    a = fz.WHEELBASE * (1.0 - fz.front_share('awd'))
    got = rack._slip(spring_sample(steer=0.3, vy=1.2, yaw=0.15))
    assert got == pytest.approx(0.3 * lock - math.atan2(1.2 + a * 0.15, 20.0))
    assert rack._slip(spring_sample(steer=0.0, vy=1.0)) < 0.0                           # sliding left of the heading, wheels straight
    assert rack._rear_slip(spring_sample()) is None and cp.table_ratio(fe.car_data.entry(CAR)) == pytest.approx(rack.ratio)


@pytest.mark.parametrize('field', ['steer', 'yaw_rate', 'vel', 'speed', 'nan', 'slow', 'short_vel'])
def test_no_kinematic_slip_without_the_fields_or_under_5_ms(field):
    rack = table_rack()
    s = spring_sample()
    assert rack._slip(s) is not None
    if field == 'nan':
        s.yaw_rate = float('nan')
    elif field == 'slow':
        s.speed = 4.9
    elif field == 'short_vel':
        s.vel = (20.0,)
    else:
        setattr(s, field, None)
    assert rack._slip(s) is None


def test_the_spring_frame_is_known_only_from_the_table_and_not_stored(tmp_path):
    from oversteer import ffb_carprops as cp
    cp.STORE.use(str(tmp_path / 'props.json'))
    cp.STORE.set_lock('rbr/999999', 0.4)
    rack = table_rack('rbr/999999')
    assert not rack.ratio_known and not rack.lock_known and not rack.align_ready()
    assert rack._slip(spring_sample(car='rbr/999999')) is not None            # (the slip is there, the spring waits for the frame)
    rack.set_car(CAR)
    assert rack.ratio_known and rack.lock_known and rack.align_ready()
    assert cp.STORE.get_lock(CAR) is None


def test_the_stiffness_prior_serves_until_learnt_and_ready_does_not_wait_for_it():
    rack = table_rack()
    assert fe.RbrRack.ALIGN_K0 == 2150.0 and rack.align_stiffness() == 2150.0
    assert rack.align_ready() and rack._align.s[0] == 0.0
    rack.spring_on = False
    assert not rack.align_ready()
    rack.spring_on = True
    stiff, t = 3000.0, 0.0
    for i in range(3000):
        t += 1.0 / 236.0
        steer = 0.3 * math.sin(2 * math.pi * 0.4 * t)
        s = spring_sample(steer=steer, vy=0.3 * math.sin(2 * math.pi * 0.7 * t))
        alpha = rack._slip(s)
        rack.column = -stiff * rack._sat(alpha) / rack.ratio            # the torque the learnt fit should find
        rack.feed(t, s, None)
    assert rack.align_stiffness() == pytest.approx(stiff, rel=0.02)
    assert rack.floor_lift == 0.0 and rack.learner is None


def test_a_large_slip_or_a_missing_column_is_not_learnt():
    rack = table_rack()
    t = 0.0
    for i in range(50):
        t += 0.004
        rack.column = None
        rack.feed(t, spring_sample(steer=0.1), None)
        rack.column = -10.0
        rack.feed(t + 0.002, spring_sample(steer=1.0, vy=-8.0), None)         # slip well over 0.2 rad
    assert rack._align.s[0] == 0.0


def test_the_split_plays_the_models_share_less_what_the_mirror_renders():
    from oversteer import ffb_spring
    rack = table_rack()
    rack.set_level(0.0, level_for(0.2), 100.0)
    s = spring_sample(steer=0.2)
    plain = rack.force(s)
    rack.align_on = True
    seen = []
    rack.align_writer = lambda *a: seen.append(a)
    mirror, t, last = ffb_spring.DriverSpring(), 0.0, None
    for i in range(40):
        t += 0.005
        rack._now = t
        got = rack.force(s)
        split = rack.align(s)
        theta_a, k, tau = seen[-1]
        want = mirror.force(theta_a, split['theta_deg'], k, tau, rack.align_k_limit, 0.0 if last is None else t - last)
        last = t
        assert got == pytest.approx(plain - fe.FULL * want, abs=1e-6)
    assert seen[-1][1] > 0.0 and abs(plain - got) > 0.0                      # the spring is carrying part of it
    from oversteer import ffb_carprops as cp
    column = -fe.RbrRack.ALIGN_K0 * rack._sat(rack._slip(s)) / rack.ratio      # (the model's torque, through the played boost curve)
    assert split['model'] == pytest.approx(cp.driver_from_column(column, 20.0, 1.0, True))


def spring_engine(tmp_path, monkeypatch, car=CAR, sign=1.0):
    from oversteer import ffb_dynamics as fd
    from tests.test_ffb_dynamics import POS
    monkeypatch.setattr(fd.FrameCheck, 'MIN_SECONDS', 2.0)
    POS.sign, POS.scale, POS.raw = sign, 1.0, 32767
    engine, path, pos = dyn_engine(tmp_path)

    class SpringDrive(Drive):
        def sample(self, t):
            steer = self.steer(t)
            pos.follow(steer)
            return spring_sample(steer=steer, car=car)
    return engine, path, SpringDrive(engine, car=car)


def test_the_spring_plays_for_a_table_car_and_the_total_is_the_models(tmp_path, monkeypatch):
    engine, path, drive = spring_engine(tmp_path, monkeypatch)
    drive.run(14.0)
    rack = engine.rbr_rack
    assert engine._played_mode == 'rbr' and rack.align_on and engine.align_note is None and not engine.model.align_on
    assert rack.wheel_range == 900.0 and rack.wheel_slope == pytest.approx(1.0, abs=0.05)
    words = open(path + '/ffb_align').read().split()
    assert len(words) == 3 and int(words[1]) > 0 and 0 < int(words[2]) <= 8000
    engine.set_rbr_spring(False)                                     # the INI's kill switch
    drive.run(0.3)
    assert not rack.align_on and open(path + '/ffb_align').read() == '0 0 0'
    engine.set_rbr_spring(True)
    drive.run(0.5)
    assert rack.align_on
    engine.stop()


def test_without_a_table_car_there_is_no_spring_only_the_column(tmp_path, monkeypatch):
    engine, path, drive = spring_engine(tmp_path, monkeypatch, car='rbr/999999')
    drive.run(14.0)
    assert engine._played_mode == 'rbr' and not engine.rbr_rack.align_on
    assert 'not learnt' in engine.align_note
    assert open(path + '/ffb_align').read().startswith('0 0 0') and int(open(path + '/ffb_dyn_damping').read()) > 0
    engine.stop()


def test_a_wheel_that_does_not_follow_the_games_steering_keeps_the_spring_off(tmp_path, monkeypatch, caplog):
    engine, path, drive = spring_engine(tmp_path, monkeypatch, sign=-1.0)
    with caplog.at_level('WARNING'):
        drive.run(14.0)
    assert engine._played_mode == 'rbr' and not engine.rbr_rack.align_on
    assert "doesn't match the game's steering" in engine.align_note
    assert open(path + '/ffb_align').read().startswith('0 0 0')
    engine.stop()


def test_the_spring_goes_off_without_telemetry_and_on_a_stream_restart(tmp_path, monkeypatch):
    engine, path, drive = spring_engine(tmp_path, monkeypatch)
    drive.run(14.0)
    rack = engine.rbr_rack
    assert rack.align_on
    drive.telemetry = False                                          # the level goes on, the samples stop
    drive.run(0.6)
    assert not rack.align_on and open(path + '/ffb_align').read() == '0 0 0'
    drive.telemetry = True
    drive.run(0.5)
    assert rack.align_on
    rack.restart_sign()
    assert rack._spring.cur is None and rack._spring_at is None and rack._align_prev is None and rack._align_hold is None
    engine.stop()


def test_forza_stays_column_only_and_a_stop_clears_both_springs(tmp_path, monkeypatch):
    engine, path, drive = spring_engine(tmp_path, monkeypatch)
    drive.run(14.0)
    assert engine.rbr_rack.align_on
    engine._dyn_stop(drive.t)
    assert not engine.rbr_rack.align_on and not engine.model.align_on
    engine._played_mode = 'forza-rack'
    engine.rbr_rack.align_on = engine.model.align_on = False
    engine._dyn_play(drive.t, spring_sample())
    assert not engine.rbr_rack.align_on and not engine.model.align_on and engine.forza_rack.align_writer is None
    engine.stop()


def test_the_ini_switch_reaches_the_rack_and_the_capture_settings(tmp_path):
    from oversteer import ffb_process
    from oversteer.model import Model
    engine = fe.FfbEngine(driver=FakeDriver, beamng_source=None)
    assert fe.OVERALL_RBR_SPRING is True and engine.rbr_rack.spring_on is True
    assert Model.types['countersteer_rbr_spring'] == 'boolean' and Model._texture_defaults(True)['countersteer_rbr_spring'] is True
    assert Model._texture_defaults(False)['countersteer_rbr_spring'] is None
    assert 'set_rbr_spring' in ffb_process.SETTERS and hasattr(ffb_process.EngineProcess, 'set_rbr_spring')
    assert engine.settings_snapshot()['profile']['rbr_spring'] is True
    engine.set_rbr_spring(0)
    engine.step(0.0)
    assert engine.rbr_rack.spring_on is False and engine.settings_snapshot()['profile']['rbr_spring'] is False


# -- the derived source (countersteer_rbr_source): the kingpin torque of the car's physics --

def derived_rack(car=CAR):
    rack = table_rack(car)
    rack.source = 'derived'
    return rack


def physics_sample(t, ay=5.0, speed=20.0, yaw=0.0, load=None, steer=0.0):
    """A moving car at the front axle's static load unless `load` (the fronts' sum) says otherwise."""
    s = spring_sample(steer=steer, speed=speed, vx=speed, yaw=yaw)
    s.accel, s.game_time = (0.0, ay, 0.0), t
    static = fe.RbrRack.FLOOR_MASS * fz.front_share('awd') * fz.G
    s.tyre_load = (0.5 * (static if load is None else load),) * 2 + (3000.0, 3000.0)
    return s


def run_derived(rack, seconds, hz=236.0, start=0.0, **kw):
    t, rows = start, []
    while t < start + seconds:
        s = physics_sample(t, **kw)
        rack.feed(t, s, None)
        rack.play_t = t
        rows.append((t, rack.derived, rack.rim_torque(s)))
        t += 1.0 / hz
    return rows


def test_derived_torque_opposes_fy_and_is_the_kingpin_torque_over_the_ratio():
    for ay in (5.0, -5.0, 2.0):
        rack = derived_rack()
        run_derived(rack, 0.2, ay=ay)
        entry = fe.car_data.entry(CAR)
        fy = fz.front_lateral(entry['mass_kg'], fz.front_share('awd'), ay, 0.0)
        expect = -(fe.ffb_carprops.RALLY_TRAIL + fz.TP0) * fy / rack.DERIVED_RATIO      # (no slip: the whole pneumatic trail)
        assert rack.derived == pytest.approx(expect, rel=1e-6) and rack.derived * ay < 0.0
        assert rack.fy_f == pytest.approx(fy)
    big = derived_rack()
    run_derived(big, 0.2, ay=40.0)                                  # an impact's spike is not grip: ay and Fy_f are capped
    cap = fz.front_share('awd') * fe.car_data.entry(CAR)['mass_kg'] * fe.RbrRack.FLOOR_AY
    assert big.fy_f == pytest.approx(cap)


def test_derived_torque_does_not_read_the_games_level():
    a, b = derived_rack(), derived_rack()
    t = 0.0
    while t < 0.3:
        s = physics_sample(t)
        b.set_level(t, level_for(0.9), 100.0)
        a.feed(t, s, None)
        b.feed(t, s, level_for(0.9))
        a.play_t = b.play_t = t
        assert a.rim_torque(s) == b.rim_torque(s)
        t += 1 / 236.0
    assert a.column is None and a.rim_torque(physics_sample(t)) is not None      # (no level: the physics still plays)


def test_derived_torque_fades_to_zero_at_low_speed():
    out = {}
    for speed in (1.0, 2.5, 3.75, 5.0, 12.0):
        rack = derived_rack()
        run_derived(rack, 0.2, speed=speed)
        out[speed] = rack.derived
    assert out[1.0] == 0.0 and out[2.5] == 0.0
    assert out[12.0] == pytest.approx(out[5.0]) and out[12.0] < 0.0
    assert out[12.0] < out[3.75] < 0.0 and out[3.75] == pytest.approx(0.5 * out[12.0])


def test_derived_torque_fades_out_within_50_ms_when_the_fronts_are_unloaded_and_back_in():
    rack = derived_rack()
    run_derived(rack, 0.2)
    full = rack.derived
    rows = run_derived(rack, 0.1, start=0.2, load=0.05 * fe.RbrRack.FLOOR_MASS * fz.front_share('awd') * fz.G)    # 5 % of static: the air
    by_t = {round(t - 0.2, 3): v for t, v, _ in rows}
    assert abs(by_t[0.03]) < abs(full) and abs(by_t[0.03]) > 0.0
    assert rows[-1][1] == 0.0 and abs(rows[round(0.055 * 236)][1]) < 1e-9
    rows = run_derived(rack, 0.1, start=0.3)
    assert rows[-1][1] == pytest.approx(full) and rows[0][1] != rows[-1][1]


def test_derived_torque_fades_on_stale_or_missing_telemetry_and_does_not_hold():
    rack = derived_rack()
    run_derived(rack, 0.2)
    t0 = rack._fed_at
    s = physics_sample(t0)
    seen = []
    for age in (0.0, 0.1, 0.125, 0.19, 0.25, 0.4):
        rack.play_t = t0 + age
        seen.append(rack.rim_torque(s))
    assert seen[0] == seen[1] != 0.0 == seen[-1] == seen[-2]
    assert abs(seen[2]) > abs(seen[3]) > 0.0 and abs(seen[3]) < abs(seen[0])
    # telemetry that stops carrying the lateral acceleration: the last value fades out in 50 ms, it is not held
    bad = physics_sample(0.2)
    bad.accel = None
    t = 0.2
    for _ in range(int(0.06 * 236)):
        t += 1 / 236.0
        rack.feed(t, bad, None)
    assert rack.derived == 0.0


def test_ngp_source_is_exactly_the_games_column_and_the_floor_lift():
    rack = table_rack()
    rack.source = 'ngp'
    twin = table_rack()
    twin.source = 'ngp'
    twin.DERIVED_AY_HZ, twin.DERIVED_SLIP_PEAK = 3.0, 0.02             # nothing of the derived path touches it
    for r_ in (rack, twin):
        rows = run_floor(r_, level_for(0.2), vy=4.0, seconds=0.5)
    assert rack.derived is None and rack.floor_lift != 0.0
    s = slide_sample(t=1.0)
    hyd = not rack.classic()
    expect = fe.ffb_carprops.driver_from_column(rack.column + rack.floor_lift, s.speed, rack.ASSIST * rack.assist_level(), hyd)
    assert rack.rim_torque(s) == expect == twin.rim_torque(s)
    assert rows


def test_derived_ignores_the_caster_floor_and_the_force_cut():
    plain, odd = derived_rack(), derived_rack()
    odd.floor_level, odd.cut_hz = 0.0, 1.0
    plain.floor_level, plain.cut_hz = 1.0, None
    t = 0.0
    while t < 0.6:
        s = slide_sample(t=t, vy=5.0)
        s.tyre_load = (4000.0, 4000.0, 3000.0, 3000.0)
        for r_ in (plain, odd):
            r_.set_level(t, level_for(0.1 if t < 0.3 else 0.8), 100.0)
            r_.feed(t, s, None)
            r_.play_t = t
        assert plain.rim_torque(s) == odd.rim_torque(s) and plain.floor_lift == odd.floor_lift == 0.0
        t += 1 / 236.0
    assert plain.derived != 0.0


def test_derived_leaves_the_takeover_logic_on_the_games_level():
    """The sign latch, ready and refusal read the game's level only: the same under either source."""
    results = {}
    for source, direction in (('derived', 1.0), ('ngp', 1.0), ('derived', -1.0), ('ngp', -1.0)):
        rack = table_rack()
        rack.source = source
        rack.ready = False
        rack.restart_sign()
        t = 0.0
        while t < 4.0:
            steer = 0.5 * math.sin(2.0 * math.pi * 0.7 * t)
            s = physics_sample(t, ay=0.5, steer=steer)
            level = int(-direction * 12000 * steer)
            rack.set_level(t, level, 100.0)
            rack.feed(t, s, level)
            t += 1 / 236.0
        results[(source, direction)] = (rack.direction, rack.ready, rack.refused, rack.waiting, rack.sign_r)
    assert results[('derived', 1.0)] == results[('ngp', 1.0)] and results[('derived', 1.0)][:3] == ('normal', True, None)
    assert results[('derived', -1.0)] == results[('ngp', -1.0)] and results[('derived', -1.0)][2] == 'inverse'


def test_derived_spring_follows_the_derived_shape_and_stays_on():
    rack = derived_rack()
    assert rack.align_stiffness() == rack.DERIVED_ALIGN_K0 and rack.align_ready()
    twin = table_rack()
    twin.source = 'ngp'
    assert twin.align_stiffness() == twin.ALIGN_K0
    # the shape is the saturating force times the trail's share: its slope stays positive past the peak, and k never goes negative
    for alpha in (0.0, 0.05, 0.15, 0.3, 0.5):
        assert 1.0 / rack._sat_div(alpha) >= rack.DERIVED_SLOPE_MIN - 1e-12
        s = spring_sample(steer=0.1 + alpha / rack.lock, vx=20.0, vy=0.0)
        split = rack.align(s)
        assert split is not None and split['k'] > 0.0 and math.isfinite(split['k'])
    # the learner is fed the derived column (before the fades), not NGP's
    run_derived(rack, 0.3, steer=0.1)
    assert rack.derived_raw is not None and rack._align.s[0] > 0.0 and rack.column is None


def test_derived_ini_switch_reaches_the_rack_and_the_capture_settings():
    from oversteer import ffb_process
    from oversteer.model import Model
    engine = fe.FfbEngine(driver=FakeDriver, beamng_source=None)
    assert fe.OVERALL_RBR_SOURCE == 'derived' and engine.rbr_rack.source == 'derived'
    assert Model.types['countersteer_rbr_source'] == 'string' and Model._texture_defaults(True)['countersteer_rbr_source'] == 'derived'
    assert Model._texture_defaults(False)['countersteer_rbr_source'] is None
    assert 'set_rbr_source' in ffb_process.SETTERS and hasattr(ffb_process.EngineProcess, 'set_rbr_source')
    assert engine.settings_snapshot()['profile']['rbr_source'] == 'derived'
    engine.set_rbr_source('NGP')
    engine.step(0.0)
    assert engine.rbr_rack.source == 'ngp' and engine.settings_snapshot()['profile']['rbr_source'] == 'ngp'
    engine.set_rbr_source('nonsense')
    engine.step(0.0)
    assert engine.rbr_rack.source == 'derived'
    model = Model.__new__(Model)
    model.data = {}
    assert model.get_countersteer_rbr_source() == 'derived'
    model.data = {'countersteer_rbr_source': ' NGP '}
    assert model.get_countersteer_rbr_source() == 'ngp'
    model.data = {'countersteer_rbr_source': 'x'}
    assert model.get_countersteer_rbr_source() == 'derived'
def test_the_aligning_spring_is_never_the_only_force():
    """Field: at front slip under 0.02 rad the spring was 77-105 % of the total, a force locked to the wheel's position:
    its limit follows the steering force, so ext_force carries the rest."""
    rack = table_rack()
    rack.cut_hz = None               # (each level plays at once)
    s = spring_sample(steer=0.2)
    seen = []
    rack.align_writer = lambda *a: seen.append(a)
    limits = {}
    for level in (0.3, 0.9):          # NGP force level
        rack.align_on = False
        rack.set_level(0.0, level_for(level), 100.0)
        plain = abs(rack.force(s)) / fe.FULL
        rack.align_on = True
        rack._now = 1.0
        rack.force(s)
        limits[level] = (plain, seen[-1][2])
    for plain, tau in limits.values():
        assert tau == pytest.approx(max(rack.ALIGN_SHARE_MAX * plain, rack.ALIGN_TAU_MIN), abs=0.01)
    assert limits[0.3][1] < limits[0.9][1]


def test_the_table_frame_comes_with_the_car_even_if_the_car_was_already_set():
    """Field: car 'rbr/106@6' with ratio_known and lock_known False for whole drives: the spring never played."""
    rack = fe.RbrRack()
    rack.reset(None)
    rack.car = CAR
    rack.set_car(CAR)
    assert rack.ratio_known and rack.lock_known
