"""Countersteer FFB's strength: independent of the user's master gain (the driver multiplies ext_force by it, so the
engine divides it out) and of the game's FFB setting, and set by the car's own steering torque mapped to the wheel
(the p99 of |front torque| per car, ffb_engine.RackForce). Nothing here touches a real wheel or /sys."""
import math

import pytest

from oversteer import ffb_carprops, ffb_engine as fe, wheel_torque
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
    engine, path = engine_with_gain(tmp_path, 65535)
    t = run(engine, 0.0, 6.0)
    FakeDriver.log.clear()
    with open(path + '/gain', 'w') as f:
        f.write('%d\n' % 32768)                   # the slider to 50 %: the written value doubles, smoothly
    run(engine, t, 2.0)
    out = forces()
    steps = [abs(b - a) for a, b in zip(out, out[1:])]
    assert max(steps) < 0.05 * max(abs(v) for v in out)


def stream_rack(peak, p99_nm=400.0, seconds=900.0, hz=50.0, **kw):
    """A RackForce that drove `seconds` of torque uniform in |x| up to a known p99 (front Mz alone)."""
    rack = fe.RackForce()
    rack.LEARNS = False
    rack.wheel_peak = peak
    rack.reset('acr/x')
    top = p99_nm / 0.99
    n = int(seconds * hz)
    for i in range(n):
        s = Sample(6000.0, 7500.0, gear=3, speed=20.0, car='acr/x', game='acr')
        mz = top * (((i * 0.6180339887) % 1.0) * 2.0 - 1.0)
        s.tyre_mz = (mz / 2, mz / 2, 0, 0)
        rack.feed(i / hz, s, None)
    return rack, top


def force_at(rack, nm):
    s = Sample(6000.0, 7500.0, gear=3, speed=20.0, car='acr/x', game='acr')
    s.tyre_mz = (nm / 2, nm / 2, 0, 0)
    return abs(rack.force(s)) / fe.FULL


def test_the_cars_p99_maps_to_070_of_full_scale_on_an_unknown_wheel():
    rack, _ = stream_rack(None, 400.0)
    assert rack.p99 == pytest.approx(400.0, rel=0.08)
    assert force_at(rack, 400.0) == pytest.approx(0.70, rel=0.08)


def test_a_weak_wheel_falls_back_to_p99_at_070():
    rack, _ = stream_rack(wheel_torque.peak_nm('046d:c24f'), 540.0)       # G29: 2.1 Nm
    assert force_at(rack, rack.p99) == pytest.approx(0.70, rel=0.01)


def test_a_strong_wheel_plays_the_real_rim_torque_up_to_its_ceiling():
    # 100 Nm of kingpin torque: 100 / 13 x 0.4 = 3.1 Nm at the rim = 15 % of a 20 Nm wheel
    rack, _ = stream_rack(20.0, 100.0)
    assert force_at(rack, rack.p99) == pytest.approx(0.4 / 13.0 * rack.p99 / 20.0, rel=0.02)
    # a car whose p99 would be 16 Nm at the rim plays at 10 Nm (the safety ceiling), never above it
    rack, _ = stream_rack(20.0, 540.0)
    assert force_at(rack, rack.p99) == pytest.approx(10.0 / 20.0, rel=0.02)
    assert force_at(rack, 5000.0) == pytest.approx(10.0 / 20.0, rel=0.001)


def test_wheel_table_knows_only_usable_wheels():
    assert wheel_torque.peak_nm('046d:c24f') == 2.1
    assert wheel_torque.peak_nm('0eb7:0020') is None            # a direct drive: not usable yet
    assert wheel_torque.peak_nm(None) is None and wheel_torque.peak_nm('dead:beef') is None


def test_the_scale_slews_to_a_new_target_and_a_car_change_holds_it():
    rack, _ = stream_rack(None, 400.0, seconds=60.0)
    before = rack.play_scale
    rack.hold()
    rack.target_scale = before * 2
    s = Sample(6000.0, 7500.0, gear=3, speed=20.0, car='acr/x', game='acr')
    s.tyre_mz = (100.0, 100.0, 0, 0)
    rack.feed(100.0, s, None)
    rack.feed(100.01, s, None)
    assert rack.play_scale == pytest.approx(before * math.exp(rack.SCALE_SLEW * 0.01), rel=1e-3)   # a slew, no step
    held = rack.play_scale
    rack.reset('acr/y')
    rack.ready = False
    rack._held_fit = (held, rack.trail, rack.scrub)
    s.car = 'acr/y'
    assert rack.force(s) == pytest.approx(held * fe.front_torque(s, rack.trail, rack.scrub))


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
    rack, _ = stream_rack(None, 300.0, seconds=300.0)
    rack.LEARNS = True
    ffb_carprops.STORE.put('acr/x', 0.02, 0.01, force=True, p99=rack.p99)
    ffb_carprops.STORE.use(ffb_carprops.STORE.path)           # a new run reads the file
    assert ffb_carprops.STORE.get_p99('acr/x') == pytest.approx(rack.p99, rel=0.01)
    again = fe.RackForce()
    again.reset('acr/x')
    assert again.p99 == pytest.approx(rack.p99, rel=0.01)


def test_the_engine_takes_the_wheel_model_for_the_peak():
    engine = fe.FfbEngine(clock=lambda: 0.0, driver=FakeDriver)
    assert engine.model.wheel_peak is None
    engine.usb_id = '046d:c24f'
    assert engine.model.wheel_peak == 2.1
    engine.usb_id = None
    assert engine.model.wheel_peak is None
