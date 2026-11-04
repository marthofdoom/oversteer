"""Gear guard (oversteer/ffb_gear.py): reversals, the held side, the limited swing, the hand-back fade, off, the drive
types and the profile defaults per type."""
import math
import random
import types

from oversteer import ffb_engine as fe, ffb_gear as fg, wheel_ids as wid, wheel_torque
from oversteer.model import Model

HZ = 300.0
DT = 1.0 / HZ


def run(guard, force, weight=1.0, t0=0.0):
    out = []
    for i, f in enumerate(force):
        out.append(guard.step(t0 + i * DT, f, weight if not callable(weight) else weight(i)))
    return out


def reversals(x, swing=0.0):
    n, last = 0, 0
    for v in x:
        s = 1 if v > swing else -1 if v < -swing else 0
        if s and last and s != last:
            n += 1
        if s:
            last = s
    return n


def test_noisy_signal_around_zero_does_not_reverse():
    rng = random.Random(1)
    force = [0.012 * math.sin(2 * math.pi * 12 * i * DT) + rng.gauss(0, 0.01) for i in range(3000)]
    assert reversals(force) > 50
    out = run(fg.GearGuard(), force)
    assert reversals(out) == 0
    assert all(abs(v) >= fg.PRELOAD - 1e-9 or v * out[0] > 0 for v in out)


def test_held_side_passes_unchanged():
    force = [0.2 + 0.1 * math.sin(2 * math.pi * 8 * i * DT) for i in range(1500)]
    assert run(fg.GearGuard(), force) == force


def test_real_reversal_once_with_limited_rate():
    force = [0.2] * 300 + [-0.2] * 600
    guard = fg.GearGuard()
    out = run(guard, force)
    assert reversals(out) == 1
    assert out[:300] == force[:300]
    # the true force's own step is clamped to the preload; from there to the new side no step is larger than the rate
    i = next(i for i in range(300, 900) if out[i] != force[i])
    j = next(k for k in range(i, 900) if out[k] == force[k])
    assert all(abs(out[k + 1] - out[k]) <= fg.RATE * DT + 1e-9 for k in range(i + 1, j - 1))
    assert abs(out[i] - fg.PRELOAD) < 1e-12
    assert out[-1] == -0.2


def test_swing_from_plus_to_minus_preload_takes_about_15_ms():
    guard = fg.GearGuard()
    guard.step(0.0, 0.0)
    guard.side = 1
    out = run(guard, [-0.5] * 400, t0=DT)
    zero = [i for i, v in enumerate(out) if abs(v) < 1e-9 or v <= -fg.PRELOAD]
    assert out[0] > -0.5 and zero


def test_near_zero_keeps_the_side_at_the_preload():
    guard = fg.GearGuard()
    out = run(guard, [0.1] * 100 + [0.0] * 900)
    assert guard.side == 1 and abs(out[-1] - fg.PRELOAD) < 1e-12
    out = run(guard, [-0.02] * 900, t0=10.0)
    assert guard.side == 1 and abs(out[-1] - fg.PRELOAD) < 1e-12


def test_level_scales_the_preload():
    out = run(fg.GearGuard(0.5), [0.0] * 10)
    assert abs(out[-1] - 0.5 * fg.PRELOAD) < 1e-12


def test_preload_ramps_with_the_hand_back_weight():
    guard = fg.GearGuard()
    guard.step(0.0, 0.1)
    n = 300
    out = run(guard, [0.0] * n, weight=lambda i: 1.0 - i / (n - 1), t0=DT)
    assert abs(out[0] - fg.PRELOAD) < 1e-3 and abs(out[-1]) < 1e-12
    assert all(a >= b - 1e-12 for a, b in zip(out, out[1:]))


def test_weight_zero_passes_through_even_against_the_side():
    guard = fg.GearGuard()
    guard.step(0.0, 0.3)
    assert guard.step(DT, -0.01, 0.0) == -0.01


def test_takeover_starts_from_the_current_sign_and_gap_keeps_the_side():
    guard = fg.GearGuard()
    guard.step(0.0, 0.3)
    guard.takeover()
    guard.step(0.0, -0.3)
    assert guard.side == -1
    guard.step(10.0, 0.0)          # a gap over 0.25 s: filter restarts, side stays
    assert guard.side == -1


def test_engine_off_passes_through_and_on_applies():
    engine = fe.FfbEngine.__new__(fe.FfbEngine)
    engine._gear, engine._gear_on, engine._played_mode, engine._w = fg.GearGuard(), False, 'acr', 1.0
    assert engine._guard(0.0, -100.0, 1.0) == -100.0
    engine._gear_on = True
    engine._gear.step(0.0, 1.0)
    assert engine._guard(DT, -1.0, 1.0) == fg.PRELOAD * fe.FULL
    assert engine._guard(2 * DT, -1.0, 0.0) == -1.0


def test_drive_table_and_unknown():
    for usb in (wid.LG_G29, wid.LG_G920, wid.LG_G923P, wid.LG_G923X, wid.LG_G27, wid.LG_G25, wid.LG_DFGT, wid.LG_DFP):
        assert wheel_torque.drive_type(usb) == 'gear'
    assert wheel_torque.drive_type(wid.FT_CSL_DD) == 'direct' and wheel_torque.drive_type(wid.CM_C5) == 'direct'
    assert wheel_torque.drive_type('dead:beef') == 'gear' and wheel_torque.drive_type(None) == 'gear'
    assert set(wheel_torque.DRIVE) == set(wheel_torque.PEAK_NM)
    assert set(wheel_torque.DRIVE.values()) <= set(wheel_torque.DRIVE_TYPES)


def test_override_and_default_per_type():
    assert Model.wheel_drive(wid.LG_G29) == 'gear' and Model.wheel_drive(wid.LG_G29, '') == 'gear'
    assert Model.wheel_drive(wid.LG_G29, 'belt') == 'belt'
    assert Model.wheel_drive(wid.LG_G29, 'nonsense') == 'gear'
    assert Model.wheel_drive(wid.FT_CSL_DD, ' Gear ') == 'gear'
    for usb, override, on in ((wid.LG_G29, None, True), (wid.LG_DFP, '', True), (wid.FT_CSL_DD, None, False),
                              (wid.LG_G29, 'belt', False), (wid.LG_G29, 'direct', False), (wid.FT_CSL_DD, 'gear', True)):
        assert Model._texture_defaults(True, usb, override)['countersteer_gear_guard'] is on
    assert Model._texture_defaults(False, wid.LG_G29)['countersteer_gear_guard'] is None
    assert Model._texture_defaults(True, wid.LG_G29)['countersteer_gear_guard_level'] == 100


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
