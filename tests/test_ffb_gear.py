"""Gear guard: the crossing stage's settings and mirror (oversteer/ffb_gear.py), the engine's ffb_gear_cross writes (on at a
take-over, off on every way out, a driver without it), the drive types and the profile defaults per type."""
import logging
import math
import os
import random
import types

import pytest

from oversteer import ffb_engine as fe, ffb_gear as fg, ffb_process as fp, wheel_ids as wid, wheel_torque
from oversteer.model import Model
from tests.test_ffb_engine import FakeDriver, _clean_log, run as run_engine  # noqa: F401
from tests.test_ffb_full_range import play, setup  # noqa: F401

DT = 0.002


def run(stage, force, speed=0.0, hands=True):
    return [stage.step(f, DT, speed, hands) for f in force]


def reversals(x, swing=0.0):
    n, last = 0, 0
    for v in x:
        s = 1 if v > swing else -1 if v < -swing else 0
        if s and last and s != last:
            n += 1
        if s:
            last = s
    return n


def test_the_defaults_and_100_about_2_fs_per_s():
    assert fg.text(fg.OVERALL_GEAR_GUARD_LEVEL) == '200 1500 800 3300 40000'
    p, flip, band, rate, rejoin = fg.params(100)
    assert (p, flip, band, rejoin) == (0.02, 0.15, 0.08, 40.0) and 1.95 < rate < 2.05


def test_the_flip_point_is_its_own_setting():
    assert fg.text(61, flip_pct=3) == '200 300 800 3300 40000' and fg.text(61, flip_pct=30) == '200 3000 800 3300 40000'
    assert fg.params(61)[3] == fg.params(61, flip_pct=30)[3]
    assert fg.reversal_late_ms(61, flip_pct=30) > fg.reversal_late_ms(61) > fg.reversal_late_ms(61, flip_pct=3)
    assert fg.reversal_late_ms(61) == pytest.approx(26, abs=2)


def test_level_is_continuous_in_steps_of_1_percent():
    rates = [fg.params(level)[3] for level in range(10, 101)]
    assert len(set(rates)) == len(rates) and all(a > b for a, b in zip(rates, rates[1:]))
    assert all(abs(a / b - 1.0) < 0.12 for a, b in zip(rates, rates[1:]))


def test_level_zero_is_off_and_low_levels_stay_inside_the_drivers_limits():
    assert fg.params(0) is None and fg.text(0) == fg.OFF == '0 0 0 0 0'
    for level in range(1, 101):
        for removal in (False, True):
            for flip_pct in fg.GEAR_FLIP_RANGE:
                preload, flip, band, rate, rejoin = (int(v) for v in fg.text(level, removal, flip_pct).split())
                assert 1 <= preload <= 1000 and preload <= flip <= 5000 and preload <= band <= 5000
                assert 100 <= rate <= 100000 and rate <= rejoin <= 1000000


def test_friction_removal_halves_the_preload():
    assert fg.text(fg.OVERALL_GEAR_GUARD_LEVEL, True) == '100 1500 800 3300 40000'


def test_small_noise_does_not_reverse_and_the_preload_holds():
    rng = random.Random(1)
    force = [0.012 * math.sin(2 * math.pi * 12 * i * DT) + rng.gauss(0, 0.01) for i in range(3000)]
    assert reversals(force) > 50
    out = run(fg.Crossing(), force)
    assert reversals(out) == 0 and all(abs(v) >= fg.PRELOAD - 1e-9 for v in out)


def test_the_held_side_passes_unchanged():
    force = [0.2 + 0.1 * math.sin(2 * math.pi * 8 * i * DT) for i in range(1500)]
    assert run(fg.Crossing(), force) == force


def test_a_small_excursion_past_zero_does_not_flip():
    stage = fg.Crossing()
    out = run(stage, [0.2] * 10 + [-0.05] * 200)
    assert stage.side == 1 and out[-1] == pytest.approx(fg.PRELOAD)
    assert run(stage, [-0.14] * 50)[-1] == pytest.approx(fg.PRELOAD)         # still under the flip


def test_a_flip_goes_through_zero_at_the_rate_then_rejoins_fast():
    stage = fg.Crossing()
    run(stage, [0.5] * 10)
    out = run(stage, [-0.5] * 200)
    assert stage.side == -1 and out[0] == pytest.approx(fg.PRELOAD - fg.RATE * DT)
    limited = [(a, b) for a, b in zip(out, out[1:]) if b > -fg.BAND]
    assert limited and all(abs(a - b) <= fg.RATE * DT + 1e-9 for a, b in limited)
    fast = [a - b for a, b in zip(out, out[1:]) if a <= -fg.BAND and b > -0.5]
    assert fast and all(abs(step - fg.REJOIN * DT) < 1e-9 for step in fast)
    assert out[-1] == -0.5


def test_tick_rate_aware():
    def crossing_time(dt):
        stage = fg.Crossing()
        stage.step(0.5, dt)
        t = 0.0
        while stage.step(-0.5, dt) > -fg.BAND:
            t += dt
        return t
    assert crossing_time(0.001) == pytest.approx(crossing_time(0.004), abs=0.006)


def test_what_is_above_the_old_preload_goes_at_once():
    stage = fg.Crossing()
    run(stage, [0.107])
    assert stage.step(-0.16, DT) == pytest.approx(fg.PRELOAD - fg.RATE * DT)


def test_the_gate_lets_a_swinging_wheel_through_and_keeps_a_held_straight():
    stage = fg.Crossing()
    run(stage, [0.3] * 10)
    assert run(stage, [-0.3] * 10, speed=400.0, hands=False)[-1] == -0.3            # hands off, fast
    assert stage.bypassed > 0
    stage = fg.Crossing()
    run(stage, [0.3] * 10)
    assert run(stage, [-0.3], speed=400.0, hands=True)[-1] > 0.0                     # a hand on it, a real force: kept
    stage = fg.Crossing()
    run(stage, [0.3] * 10)
    assert run(stage, [-0.3], speed=50.0, hands=False)[-1] > 0.0                     # the observer's false hands off, slow: kept
    assert stage.bypassed == 0


def test_a_regular_swing_takes_the_stage_out_whatever_the_observer_says():
    stage = fg.Crossing()
    out = []
    for i in range(2000):
        speed = 150.0 * math.sin(2 * math.pi * 3.5 * i * DT)
        out.append(stage.step(0.3 * math.sin(2 * math.pi * 3.5 * i * DT - 1.5), DT, speed, True))
    assert stage.bypassed > 500
    assert out[-1] == 0.3 * math.sin(2 * math.pi * 3.5 * 1999 * DT - 1.5)


def test_slow_steering_does_not_count_as_a_swing():
    stage = fg.Crossing()
    run(stage, [0.0])
    for i in range(2000):
        stage.step(0.0, DT, 60.0 * math.sin(2 * math.pi * 3.5 * i * DT), True)
    assert stage.bypassed == 0


def on_mirror(path, text='0 0 0 0 0'):
    with open(path + '/ffb_gear_cross', 'w') as f:
        f.write(text + '\n')


def gear(path):
    with open(path + '/ffb_gear_cross') as f:
        return f.readline().strip()


def gear_play(tmp_path, **kw):
    """The engine playing on a fake driver with the attribute (see test_ffb_full_range.play)."""
    import tests.test_ffb_engine as tfe
    orig = tfe.wheel

    def wheel(tmp, name='wheel', **k):
        path = orig(tmp, name, **k)
        on_mirror(path)
        return path
    tfe.wheel = wheel
    try:
        engine, path, t = play(tmp_path)
    finally:
        tfe.wheel = orig
    return engine, path, t


def step_for(engine, t, seconds=0.2):
    return run_engine(engine, t, seconds)


def test_written_while_our_force_plays_and_off_at_hand_back(tmp_path):
    engine, path, t = gear_play(tmp_path)
    engine.set_gear_guard(True, 61, 'gear')
    t = step_for(engine, t)
    assert gear(path) == '200 1500 800 3300 40000'
    engine._fade_out(t, 'test')
    assert gear(path) == '0 0 0 0 0'
    engine.stop()


def test_the_slider_follows_and_off_writes_off(tmp_path):
    engine, path, t = gear_play(tmp_path)
    engine.set_gear_guard(True, 61, 'gear')
    t = step_for(engine, t)
    engine.set_gear_guard(True, 100, 'gear')
    t = step_for(engine, t)
    assert gear(path) == fg.text(100)
    engine.set_gear_guard(True, 100, 'gear', 8)
    t = step_for(engine, t)
    assert gear(path) == fg.text(100, flip_pct=8)
    engine.set_gear_guard(False, 100, 'gear')
    t = step_for(engine, t)
    assert gear(path) == '0 0 0 0 0'
    engine.stop()


@pytest.mark.parametrize('drive', ['belt', 'direct'])
def test_belt_and_direct_are_off(tmp_path, drive):
    engine, path, t = gear_play(tmp_path)
    engine.set_gear_guard(True, 61, drive)
    step_for(engine, t)
    assert gear(path) == '0 0 0 0 0'
    engine.stop()


def test_removal_halves_what_is_written(tmp_path):
    engine, path, t = gear_play(tmp_path)
    engine.set_gear_guard(True, 61, 'gear')
    engine.set_dynamics(True, True, True)
    step_for(engine, t)
    assert gear(path) == '100 1500 800 3300 40000'
    engine.stop()


def test_stop_and_a_device_change_write_off(tmp_path):
    engine, path, t = gear_play(tmp_path)
    engine.set_gear_guard(True, 61, 'gear')
    t = step_for(engine, t)
    assert gear(path) != '0 0 0 0 0'
    engine.stop()
    assert gear(path) == '0 0 0 0 0'


def test_a_driver_without_it_is_one_log_line_and_no_error(tmp_path, caplog):
    engine, path, t = play(tmp_path)
    assert not os.path.exists(path + '/ffb_gear_cross')
    engine.set_gear_guard(True, 61, 'gear')
    with caplog.at_level(logging.INFO):
        t = step_for(engine, t, 0.5)
    lines = [r for r in caplog.records if 'ffb_gear_cross' in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    assert engine.status.available
    engine.stop()


def test_the_hook_is_told_before_the_write_and_at_the_end(tmp_path):
    engine, path, t = gear_play(tmp_path)
    seen = []
    engine.gear_hook = lambda wheel, on: seen.append((wheel, on, gear(path)))
    engine.set_gear_guard(True, 61, 'gear')
    t = step_for(engine, t)
    assert seen == [(path, True, '0 0 0 0 0')]
    engine.stop()
    assert seen[-1][:2] == (path, False)


def test_a_dead_engines_stage_is_turned_off_from_the_gui(tmp_path):
    from tests.test_ffb_engine import wheel
    path = wheel(tmp_path)
    on_mirror(path, '200 1500 800 3300 40000')
    fp._restore_wheel(path)
    assert gear(path) == '0 0 0 0 0'
    path2 = wheel(tmp_path, 'old')
    fp._restore_wheel(path2)                     # an older driver: nothing to do, no error
    assert not os.path.exists(path2 + '/ffb_gear_cross')


def test_drive_table_and_unknown():
    for usb in (wid.LG_G29, wid.LG_G920, wid.LG_G923P, wid.LG_G923X, wid.LG_G27, wid.LG_G25, wid.LG_DFGT, wid.LG_DFP):
        assert wheel_torque.drive_type(usb) == 'gear'
    assert wheel_torque.drive_type(wid.FT_CSL_DD) == 'direct' and wheel_torque.drive_type(wid.CM_C5) == 'direct'
    assert wheel_torque.drive_type('dead:beef') == 'gear' and wheel_torque.drive_type(None) == 'gear'
    assert set(wheel_torque.DRIVE) == set(wheel_torque.PEAK_NM)
    assert set(wheel_torque.DRIVE.values()) <= set(wheel_torque.DRIVE_TYPES)


def test_profile_flip_default_clamp_and_the_retired_column_key(tmp_path, caplog):
    assert Model._texture_defaults(True, wid.LG_G29)['countersteer_gear_flip'] == 15
    assert Model._texture_defaults(False, wid.LG_G29)['countersteer_gear_flip'] is None
    model = object.__new__(Model)
    model.ui, model.data = None, {}
    model.set_if_changed = lambda key, value: model.data.update({key: value}) or True
    assert model.get_countersteer_gear_flip() == 15
    model.set_countersteer_gear_flip(99)
    assert model.get_countersteer_gear_flip() == 30
    model.set_countersteer_gear_flip(1)
    assert model.get_countersteer_gear_flip() == 3
    from oversteer import model as mod
    assert 'countersteer_column_damping' in mod.RETIRED_KEYS and 'countersteer_column_damping' not in Model.types
    assert 'countersteer_column_added' in Model.types


def test_a_profile_with_only_the_old_column_key_gets_the_new_default_without_a_warning(tmp_path, caplog):
    from tests.fake_wheel import FakeDevice
    profile = tmp_path / 'old.ini'
    profile.write_text('[DEFAULT]\ncountersteer_ffb = 1\ncountersteer_column_damping = 0\n')
    model = Model(FakeDevice())
    with caplog.at_level(logging.WARNING):
        model.load(str(profile), force=True)
    assert model.get_countersteer_column_added() == 15
    assert 'countersteer_column_damping' not in caplog.text and 'countersteer_column_damping' not in model.data


def test_override_and_default_per_type():
    assert Model.wheel_drive(wid.LG_G29) == 'gear' and Model.wheel_drive(wid.LG_G29, '') == 'gear'
    assert Model.wheel_drive(wid.LG_G29, 'belt') == 'belt'
    assert Model.wheel_drive(wid.LG_G29, 'nonsense') == 'gear'
    assert Model.wheel_drive(wid.FT_CSL_DD, ' Gear ') == 'gear'
    for usb, override, on in ((wid.LG_G29, None, True), (wid.LG_DFP, '', True), (wid.FT_CSL_DD, None, False),
                              (wid.LG_G29, 'belt', False), (wid.LG_G29, 'direct', False), (wid.FT_CSL_DD, 'gear', True)):
        assert Model._texture_defaults(True, usb, override)['countersteer_gear_guard'] is on
    assert Model._texture_defaults(False, wid.LG_G29)['countersteer_gear_guard'] is None
    assert Model._texture_defaults(True, wid.LG_G29)['countersteer_gear_guard_level'] == fg.OVERALL_GEAR_GUARD_LEVEL == 61


def test_explicit_value_wins_over_the_drive_default():
    model = object.__new__(Model)
    model.device = types.SimpleNamespace(usb_id=wid.FT_CSL_DD)
    model.data = {'countersteer_gear_guard': None, 'countersteer_wheel_drive': ''}
    assert model.get_countersteer_gear_guard() is False
    model.data['countersteer_gear_guard'] = True
    assert model.get_countersteer_gear_guard() is True
    model.data.update(countersteer_gear_guard=None, countersteer_wheel_drive='gear')
    assert model.get_countersteer_gear_guard() is True
    assert model.get_countersteer_wheel_drive() == 'gear'
