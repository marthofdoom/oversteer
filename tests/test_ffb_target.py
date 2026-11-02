"""Countersteer FFB's strength: independent of the user's master gain (the driver multiplies ext_force by it, so the
engine divides it out) and of the game's FFB setting, and set by the car's own steering torque mapped to the wheel
(the p99 of |front torque| per car, ffb_engine.RackForce). Nothing here touches a real wheel or /sys."""
import math

import pytest

from oversteer import ffb_carprops, ffb_engine as fe, ffb_texture, wheel_torque
from oversteer.telemetry_formats import Sample
from tests.test_ffb_engine import FakeDriver, acr, forces, run, upload, wheel, game_raw, HZ, fitted  # noqa: F401
from tests.test_ffb_engine import _clean_log  # noqa: F401 (the autouse fixture)


def engine_with_gain(tmp_path, gain, strength=40):
    path = wheel(tmp_path)
    with open(path + '/gain', 'w') as f:
        f.write('%d\n' % gain)
    engine = fe.FfbEngine(clock=lambda: 0.0, driver=FakeDriver)
    engine.set_device(path)
    engine.set_enabled(True)
    engine.set_strength(strength)
    engine.step(0.0)
    return engine, path


def delivered_at_gain(tmp_path, gain):
    """The last ext_force write times the driver's gain (what the wheel gets), after 8 s of the same drive."""
    tmp_path.mkdir()
    engine, path = engine_with_gain(tmp_path, gain)
    FakeDriver.log.clear()
    run(engine, 0.0, 8.0)
    return forces()[-1] * gain / 65535.0, engine


@pytest.mark.parametrize('percent', [50, 100, 115, 150])
def test_the_delivered_force_is_the_same_at_any_master_gain(tmp_path, percent):
    ref, _ = delivered_at_gain(tmp_path / 'a', 65535)
    got, engine = delivered_at_gain(tmp_path / 'b', int(round(65535 * percent / 100.0)))
    assert abs(ref) > 500
    assert got == pytest.approx(ref, rel=0.02, abs=3)
    assert engine._clipped == 0.0


def test_a_division_past_full_scale_clips_and_is_counted(tmp_path):
    engine, path = engine_with_gain(tmp_path, 3277, strength=100)        # 5 %: 20x
    run(engine, 0.0, 8.0)
    assert max(abs(v) for v in forces()) <= fe.FULL
    assert engine._clipped > 0.0


def test_a_gain_change_in_play_does_not_step_what_is_written(tmp_path):
    def drive(sub, change):
        (tmp_path / sub).mkdir()
        engine, path = engine_with_gain(tmp_path / sub, 65535)
        t = run(engine, 0.0, 6.0)
        FakeDriver.log.clear()
        if change:
            with open(path + '/gain', 'w') as f:
                f.write('%d\n' % 32768)               # the slider to 50 %: the written value doubles, smoothly
        run(engine, t, 2.0)
        out = forces()
        return max(abs(b - a) for a, b in zip(out, out[1:]))
    # the same drive without the change shows the drive's own sample-to-sample movement
    assert drive('a', True) < 2.3 * drive('b', False) + 50      # (the written value doubles with the gain halved)


def rack_for(peak=None, assist=None, ratio=None):
    rack = fe.RackForce()
    rack.LEARNS = False
    rack.wheel_peak = peak
    rack.reset('acr/x')
    if assist is not None:
        rack.assist = assist
    if ratio is not None:
        rack.ratio = ratio
    return rack


def sample_at(front_nm, speed=25.0):
    s = Sample(6000.0, 7500.0, gear=3, speed=speed, car='acr/x', game='acr')
    s.tyre_mz = (front_nm / 2, front_nm / 2, 0, 0)
    return s


def force_at(rack, nm, speed=25.0):
    return abs(rack.force(sample_at(nm, speed))) / fe.FULL


def test_the_boost_curve_is_monotone_and_invertible():
    for speed in (0.0, 5.0, 15.0, 30.0, 60.0):
        for assist in (1.0, 0.5, 0.0):
            prev = -1.0
            for i in range(0, 600):
                tc = i * 0.1
                td = ffb_carprops.driver_from_column(tc, speed, assist)
                assert td >= prev - 1e-12 and td <= tc + 1e-9          # monotone, and never more than the column
                prev = td
                assert ffb_carprops.column_from_driver(td, speed, assist) == pytest.approx(tc, abs=1e-6)
            assert ffb_carprops.driver_from_column(-3.0, speed, assist) == -ffb_carprops.driver_from_column(3.0, speed, assist)


def test_assist_is_heavy_parked_and_light_at_speed():
    parked = ffb_carprops.driver_from_column(20.0, 0.0)
    fast = ffb_carprops.driver_from_column(20.0, 30.0)
    assert parked < fast < 20.0


def test_on_centre_and_at_the_limit_torques_are_a_real_cars():
    rack = rack_for()
    # the dump's p50 (85 Nm of kingpin torque) is an easy corner, its p99 (550 Nm) the grip limit, at 25 m/s
    easy = abs(rack.rim_torque(sample_at(85.0)))
    limit = abs(rack.rim_torque(sample_at(550.0)))
    assert 0.4 <= easy <= 1.0
    assert 2.0 <= limit <= 3.2
    assert abs(rack.rim_torque(sample_at(10.0))) < 0.3          # near the centre almost nothing


def test_the_rim_torque_is_played_one_to_one_up_to_the_knee_then_softly_limited():
    peak = 2.1
    rack = rack_for(peak)
    low = rack.rim_torque(sample_at(60.0))
    assert low / peak < wheel_torque.KNEE_FROM
    assert force_at(rack, 60.0) == pytest.approx(low / peak, rel=1e-6)
    shares = [force_at(rack, nm) for nm in range(0, 4000, 5)]
    assert all(b >= a for a, b in zip(shares, shares[1:]))                       # monotone, no step
    assert max(shares) <= wheel_torque.STEER_BUDGET + 1e-9
    assert force_at(rack, 5000.0) == pytest.approx(wheel_torque.STEER_BUDGET, abs=0.01)
    above = [b - a for a, b in zip(shares, shares[1:]) if a > wheel_torque.KNEE_FROM * 0.8]
    assert max(above) < 0.015                                                    # a smooth knee


def test_an_unknown_wheel_counts_as_the_g29_class():
    assert force_at(rack_for(None), 300.0) == pytest.approx(force_at(rack_for(2.1), 300.0))


def test_a_strong_wheel_plays_the_same_nm_and_never_passes_the_ceiling():
    weak, strong = rack_for(2.1), rack_for(20.0)
    nm = abs(strong.rim_torque(sample_at(300.0)))
    assert force_at(strong, 300.0) == pytest.approx(nm / 20.0, rel=1e-6)
    assert force_at(strong, 300.0) < force_at(weak, 300.0)
    unassisted = rack_for(20.0, assist=0.0)
    assert force_at(unassisted, 5000.0) == pytest.approx(fe.RackForce.RIM_SAFE_NM / 20.0, rel=0.001)


def test_an_unassisted_car_passes_the_column_torque():
    rack = rack_for(20.0, assist=0.0, ratio=13.0)
    assert rack.rim_torque(sample_at(130.0)) == pytest.approx(10.0)
    assert rack.rim_torque(sample_at(-130.0)) == pytest.approx(-10.0)


def test_the_steering_budget_leaves_the_texture_its_quarter():
    rack = rack_for(2.1)
    steering = force_at(rack, 5000.0) * fe.FULL
    assert fe.FULL - steering >= 0.24 * fe.FULL
    # the texture's own limit is FULL - |steering|: still room for its cap at the budget's edge
    assert fe.FULL - steering > ffb_texture.TOTAL_CAP * fe.FULL


def test_wheel_table_knows_only_usable_wheels():
    assert wheel_torque.peak_nm('046d:c24f') == 2.1
    assert wheel_torque.peak_nm('0eb7:0020') is None            # a direct drive: not usable yet
    assert wheel_torque.peak_nm(None) is None and wheel_torque.peak_nm('dead:beef') is None


def test_a_car_change_holds_the_playing_model_through_the_fade():
    rack = rack_for(2.1, assist=0.0)
    rack.hold()
    held = rack.force(sample_at(130.0))
    rack.reset('acr/y')
    rack.ready = False
    rack._held_fit = (rack.trail, rack.scrub, 13.0, 0.0)
    s = sample_at(130.0)
    s.car = 'acr/y'
    assert rack.force(s) == pytest.approx(held)


def test_the_gates_still_use_the_games_raw_force_not_its_level():
    ok = fitted(game_gain=0.4)
    big = fitted(game_gain=3.0)
    assert ok.ready and big.ready
    s = acr(1.0)
    assert ok.force(s) == pytest.approx(big.force(s), rel=1e-6)           # the game's level does not set the magnitude
    reversed_ = fitted(raw_sign=-1.0)
    assert reversed_.refused == 'reversed' and not reversed_.ready
    none = fitted(raw=False)
    assert not none.ready and none.waiting == 'raw'


def test_the_cars_p99_is_kept_between_runs(tmp_path):
    ffb_carprops.STORE.put('acr/x', 0.02, 0.01, force=True, p99=300.0)
    ffb_carprops.STORE.use(ffb_carprops.STORE.path)           # a new run reads the file
    assert ffb_carprops.STORE.get_p99('acr/x') == pytest.approx(300.0, rel=0.01)
    again = fe.RackForce()
    again.reset('acr/x')
    assert again.p99 == pytest.approx(300.0, rel=0.01)


def test_the_engine_takes_the_wheel_model_for_the_peak():
    engine = fe.FfbEngine(clock=lambda: 0.0, driver=FakeDriver)
    assert engine.model.wheel_peak is None
    engine.usb_id = '046d:c24f'
    assert engine.model.wheel_peak == 2.1
    engine.usb_id = None
    assert engine.model.wheel_peak is None


def slipping(front_nm, alpha, steer):
    s = sample_at(front_nm)
    s.tyre_slip_angle = (alpha, alpha, 0.0, 0.0)
    s.steer = steer
    return s


def test_the_aligning_split_is_off_by_default_and_changes_nothing():
    rack = rack_for(2.1)
    s = slipping(-300.0, 0.05, 0.3)
    assert rack.align_on is False and rack.align_writer is None
    before = rack.force(s)
    seen = []
    rack.align_writer = lambda *a: seen.append(a)
    assert rack.force(s) == before and not seen


def test_the_aligning_spring_rebuilds_the_model_torque_and_points_at_zero_slip():
    rack = rack_for(2.1)
    s = slipping(-300.0, 0.05, 0.3)                        # steered left of where it goes: slip > 0
    split = rack.align(s)
    assert split['k'] > 0 and split['theta_a_deg'] < split['theta_deg']      # zero slip is further right
    assert split['k'] * (split['theta_a_deg'] - split['theta_deg']) == pytest.approx(split['model'])
    assert split['model'] < 0                                                # aligning: pushes back to the right
    drift = rack.align(slipping(100.0, -0.4, -0.3))
    assert drift['theta_a_deg'] > drift['theta_deg']


def test_with_the_split_on_ext_force_plays_the_residual_and_the_writer_gets_the_spring():
    rack = rack_for(20.0)
    s = slipping(-300.0, 0.05, 0.3)
    plain = rack.force(s)
    seen = []
    rack.align_on, rack.align_writer = True, lambda theta_a, k: seen.append((theta_a, k))
    resid = rack.force(s)
    split = rack.align(s)
    assert seen and seen[0] == pytest.approx((split['theta_a_deg'], split['k']))
    want = rack.rim_torque(s) - split['model']
    assert resid == pytest.approx(fe.FULL * wheel_torque.rim_share(want, 20.0), rel=1e-6)
    assert abs(resid) < abs(plain)


# -- power steering: detection and the INI keys --

def feed_detector(det, gain_of_load, speed, p99=500.0, seconds=200.0, hz=50.0):
    n = int(seconds * hz)
    for i in range(n):
        torque = p99 * 1.2 * ((i * 0.6180339887) % 1.0)
        det.add(torque, 30.0 * gain_of_load(torque, speed, p99) * torque, p99, 1.0 / hz)
    return det


def linear_game(torque, speed, p99):
    return 1.0 - 0.08 * min(torque / p99, 1.0)          # ACR-like: a mild droop only


def boost_game(torque, speed, p99):
    td = ffb_carprops.driver_from_column(torque / 13.0, speed, 1.0)
    return max(td, 1e-9) / max(torque / 13.0, 1e-9)       # the gain a boost curve gives, speed-dependent


@pytest.mark.parametrize('speed', [8.0, 30.0])
def test_the_detector_calls_a_linear_game_absent_and_a_boost_curve_present(speed):
    lin = feed_detector(ffb_carprops.AssistDetector(), linear_game, speed)
    assert lin.state is False and lin.ratio() > 0.85
    boost = feed_detector(ffb_carprops.AssistDetector(), boost_game, speed)
    assert boost.state is True and boost.ratio() < 0.65
    curve = boost.curve(500.0)
    assert [c for _, c in curve] == sorted((c for _, c in curve), reverse=True) and curve[-1][1] < 0.7


def test_the_detector_waits_for_load_range_and_holds_between_thresholds():
    det = ffb_carprops.AssistDetector()
    for i in range(2000):                                  # small loads only: no decision
        det.add(10.0 + (i % 20), 300.0, 500.0, 0.02)
    assert det.state is None and det.ratio() is None
    det = feed_detector(ffb_carprops.AssistDetector(), boost_game, 20.0)
    assert det.state is True
    mid = lambda t, v, p: 1.0 - 0.3 * min(t / p, 1.0)       # ratio ~0.7: between the thresholds, no flip
    feed_detector(det, mid, 20.0, seconds=400.0)
    assert 0.6 < det.ratio() < 0.78 and det.state is True


def test_the_game_curve_is_followed_not_stacked_and_the_ini_modes_decide(tmp_path):
    ini = tmp_path / 'config.ini'
    ffb_carprops.CONFIG_PATH = str(ini)
    try:
        assert ffb_carprops.power_settings() == ('auto', 1.0)            # no file: defaults
        rack = rack_for(20.0)
        rack._assist = feed_detector(ffb_carprops.AssistDetector(), boost_game, 25.0)
        rack.p99 = 500.0
        s = sample_at(500.0)
        model = ffb_carprops.driver_from_column(500.0 / 13.0, 25.0, 1.0)
        assert rack.power_steering() == 'game'
        assert rack.rim_torque(s) == pytest.approx(model, rel=1e-6)       # levelled at the p99 on our class
        half = rack.rim_torque(sample_at(250.0))
        assert half < 0.5 * model * 1.01 or half > 0                       # follows the game's shape
        ini.write_text('[DEFAULT]\ncountersteer_power_steering = off\n')
        assert ffb_carprops.power_settings() == ('off', 1.0) and rack.power_steering() == 'off'
        assert rack.rim_torque(s) == pytest.approx(500.0 / 13.0)          # column torque
        ini.write_text('[DEFAULT]\ncountersteer_power_steering = on\ncountersteer_assist_strength = 0.5\n')
        assert rack.power_steering() == 'model'
        assert rack.rim_torque(s) == pytest.approx(ffb_carprops.driver_from_column(500.0 / 13.0, 25.0, 0.5))
        ini.write_text('[DEFAULT]\ncountersteer_power_steering = bogus\ncountersteer_assist_strength = 9\n')
        assert ffb_carprops.power_settings() == ('auto', 2.0)             # bad mode: auto; strength clamped
        ini.write_text('[DEFAULT]\ncountersteer_assist_strength = x\n')
        assert ffb_carprops.power_settings() == ('auto', 1.0)
    finally:
        ffb_carprops.CONFIG_PATH = None


def test_without_a_detection_the_model_is_applied():
    rack = rack_for(2.1)
    assert rack._assist.state is None and rack.power_steering() == 'model'
