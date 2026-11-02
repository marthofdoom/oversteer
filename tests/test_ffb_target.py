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
