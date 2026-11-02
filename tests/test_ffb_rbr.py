"""Countersteer FFB for Richard Burns Rally (oversteer/ffb_rbr.py, the engine's RBR mode) against synthetic NGP
streams: the game's raw level made exactly as NGP and Wine make it (curve, floor, DirectInput to level) at ~870
uploads a second, RBR's telemetry at 236 a second, a fake driver directory. The curve's inversion round-trips, the
knee's endpoints (the game's curve at 0, linear at 100), the checks (grid, saturation, gain), the classifier's
states and the hand-over; nothing is played without fresh raw. Nothing here touches a real wheel or /sys."""
import math
import random

import pytest

from oversteer import ffb_engine as fe, ffb_rbr as r
from oversteer.telemetry_formats import Sample
from tests.test_ffb_engine import FakeDriver, forces, levels, upload, wheel  # noqa: F401 (the autouse fixture)
from tests.test_ffb_engine import _clean_log  # noqa: F401

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
        assert r.on_grid(r.level_of(m)) and r.on_grid(round(m * r.DI_LEVEL))
    rng = random.Random(4)
    share = sum(r.on_grid(rng.randint(-32000, 32000)) for _ in range(20000)) / 20000
    assert 0.5 < share < 0.7 < r.GRID_GO
    assert r.magnitude(0.5) == -5000 and r.magnitude(-3.0) == 10000


# -- the knee --

def test_linearity_0_is_the_games_curve_100_linear_70_exact_to_the_knee():
    force = r.RbrForce(linearity=0)
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
    force = r.RbrForce()
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
    force = r.RbrForce(linearity=70)
    force.set_ceiling(0.8 * r.FULL)
    assert force.force(int(0.99 * r.FULL)) <= 0.8 * r.FULL + 1
    force.set_ceiling(None)
    assert force.ceiling == r.FULL


# -- a drive --

def ngp_level(torque, s=100.0):
    return r.level_of(r.magnitude(r.ngp_curve(torque, s)))


def rbr_sample(car=CAR, speed=20.0):
    s = Sample(5000.0, gear=3, speed=speed, car=car, game='rbr')
    return s


class Drive:
    """1 ms engine passes, NGP uploading every 1.15 ms, RBR's telemetry every 1/236 s."""

    def __init__(self, engine, amp=36.0, s=100.0, upload_hz=870.0, tel_hz=236.0, level=None):
        self.engine, self.amp, self.s, self.upload_hz, self.tel_hz = engine, amp, s, upload_hz, tel_hz
        self.level = level or (lambda t: ngp_level(self.torque(t), self.s))
        self.t = self.next_up = self.next_tel = 0.0
        self.uploads = True
        self.telemetry = True

    def torque(self, t):
        return self.amp * math.sin(2 * math.pi * 0.3 * t) + 1.5 * math.sin(2 * math.pi * 23.0 * t)

    def run(self, seconds):
        end = self.t + seconds
        while self.t < end:
            t = self.t
            if self.uploads and t >= self.next_up:
                upload(self.level(t))
                self.next_up += 1.0 / self.upload_hz
            if self.telemetry and t >= self.next_tel:
                self.engine.offer(t, rbr_sample())
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


def test_rbr_goes_green_and_plays_the_curve_undone(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(1.5)
    assert engine.status.state == 'replace' and not engine.status.active    # learning: drive a few corners
    assert engine.status.waiting in ('raw', 'corners')
    drive.run(4.0)
    st = engine.status
    assert st.state == 'replace' and st.active and st.ready and st.game == 'rbr'
    assert st.raw_rate is not None and 700 < st.raw_rate < 1000
    assert st.gain is not None and st.gain >= r.GAIN_GO
    assert levels() == [0]
    # Played at the upload rate (every pass with a new upload), not the telemetry's
    n = len(forces())
    drive.run(1.0)
    assert len(forces()) - n > 700
    level = FakeDriver.raw[0]
    assert forces()[-1] == engine.rbr.force(level)
    assert engine.rbr.torque == pytest.approx(r.ngp_inverse(-level / r.FULL, 100.0), rel=1e-6)
    assert engine.rbr.torque == pytest.approx(drive.torque(drive.next_up - 1 / 870.0), abs=0.2)
    light, text = fe.status_text(engine.status)
    assert light == 'green' and 'curve undone' in text


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
    assert engine.status[:2] == ('model', 'rbr-curve') and FakeDriver.log == []
    assert 'curve' in fe.status_text(engine.status)[1]


def test_a_saturated_force_is_left_to_the_game(tmp_path):
    engine, path = engine_on(tmp_path)
    Drive(engine, amp=900.0).run(6.0)                    # |x| past 0.98 most of the time
    assert engine.status[:2] == ('model', 'rbr-saturated') and FakeDriver.log == []


def test_linearity_0_is_no_improvement(tmp_path):
    engine, path = engine_on(tmp_path, linearity=0)
    Drive(engine).run(6.0)
    assert engine.rbr.linearity == 0
    assert engine.status[:2] == ('model', 'rbr-no-gain') and FakeDriver.log == []
    assert engine.status.gain == pytest.approx(1.0, abs=0.01)


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
    assert engine.rbr.ceiling == pytest.approx(0.8 * r.FULL)
    Drive(engine, amp=45.0).run(8.0)
    assert engine.status.active and max(abs(v) for v in forces()) <= 0.8 * r.FULL + 1
    assert not engine.status.clipping
    assert max(abs(v) for v in forces()) > 0.7 * r.FULL                # the knee, under the cap


def test_the_cap_counts_against_the_gain(tmp_path):
    # The game's own force is not under ext_force_cap: with ours capped at 80 % and the forces high, the game's
    # curve keeps more detail and it keeps its force
    engine, path = engine_on(tmp_path)
    with open(path + '/gain', 'w') as f:
        f.write('65535\n')
    engine.set_device(None)
    engine.step(0.0)
    engine.set_device(path)
    engine.step(0.0)
    Drive(engine, amp=55.0).run(8.0)
    assert engine.status[:2] == ('model', 'rbr-no-gain')
