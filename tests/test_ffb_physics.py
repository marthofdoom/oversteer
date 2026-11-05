"""Countersteer FFB against physics, the game's own force and the calibrated wheel (scripts/ffb_physics_report.py
prints the same numbers). Data-driven checks skip when the data is not on this machine: marth's ACR dump
(~/.cache/ffb-slip/dump.npz) and the FH6 capture. The wheel is the real G29's fit (tests/data/g29-fit-result.json).
Tests marked xfail(strict) pin defects found by this check: they start passing (and so fail) once fixed."""
import importlib.util
import math
import os

import numpy as np
import pytest

from oversteer import ffb_addon as fa
from oversteer import ffb_engine as fe
from oversteer import ffb_texture as ft
from oversteer import wheel_calibration as wcal
from oversteer import wheel_torque

_spec = importlib.util.spec_from_file_location(
    'ffb_physics_report', os.path.join(os.path.dirname(__file__), '..', 'scripts', 'ffb_physics_report.py'))
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

FULL = fe.FULL
HAVE_DUMP = os.path.exists(R.DUMP)
HAVE_FH6 = os.path.exists(R.FH6)
needs_dump = pytest.mark.skipif(not HAVE_DUMP, reason="marth's ACR dump is not on this machine")
needs_fh6 = pytest.mark.skipif(not HAVE_FH6, reason="marth's FH6 capture is not on this machine")


# ---------------------------------------------------------------- 1. signs, synthetic

def test_acr_torque_signs_follow_the_kingpin_physics():
    rows = {name: torque for name, torque, _want in R.acr_synthetic()}
    left, right = rows['left corner, linear'], rows['right corner, linear']
    assert left < 0 < right and left == pytest.approx(-right)          # aligning, mirror-symmetric
    assert rows['left drift, countersteer right, Mz gone'] < 0          # toward the countersteer
    assert rows['reverse, steered left (Fy -, Mz 0)'] > 0               # toward the lock: caster backwards
    assert rows['airborne (no forces)'] == 0.0
    assert rows['drive, right front pulls harder'] > 0                  # positive scrub: away from the stronger
    assert rows['brake, left front harder'] > 0                         # ... toward the harder braked wheel
    assert abs(rows['lock-up both fronts, equal']) < 10.0               # equal Fx: no couple


def test_rbr_keeps_the_games_sign_and_is_linear_in_kingpin_torque_below_the_knee():
    rows = R.rbr_synthetic()
    assert all((lv == 0 and o == 0) or lv * o > 0 for _T, lv, o in rows)
    half = rows[80:]
    assert all(abs(b[2]) >= abs(a[2]) for a, b in zip(half, half[1:]))
    lin = [(T, o) for T, _lv, o in rows if 0 < abs(o) < 0.69 * FULL]
    assert np.corrcoef(*zip(*lin))[0, 1] ** 2 > 0.9999
    assert max(abs(o) for _T, _lv, o in rows) <= FULL


def test_beamng_soft_lock_pushes_back_toward_the_centre():
    push = dict(R.beamng_softlock())
    assert push[0.0] == 0.0
    assert push[1.0] < push[0.97] < 0 < push[-0.97] < push[-1.0]


# ---------------------------------------------------------------- 1/2. ACR dump

@pytest.fixture(scope='module')
def acr():
    d = R.load_dump()
    rep = R.replay_acr(d, hz=50.0)
    return d, rep, R.acr_cases(d, rep), R.acr_fit(d, rep)


@needs_dump
def test_acr_replay_follows_finalff(acr):
    _d, _rep, _cases, fit = acr
    # the boost curve bends finalFF's linear shape (monotone, but not linear): R^2 0.90 where a linear scale gave 0.97
    assert fit['r2_moving'] > 0.77 and fit['r_moving'] > 0.88      # (the knee's compression and no scrub couple bend it from finalFF's line)
    assert 0.55 < fit['p50_p90_p99_ours'][2] <= wheel_torque.TOP      # the 1:1 / compressed rim torque on a G29-class wheel: p99 within the budget
    assert abs(fit['lag_s']) < 0.1                     # (snapshots 0.26 s apart: a lag under ~0.1 s is not resolved)
    assert fit['clip_share_ours'] < 0.003                # the hard clamp at the budget (75 %): nothing reaches full scale
    assert fit['ready_share'] > 0.95


@needs_dump
def test_acr_cases_against_the_games_force(acr):
    _d, _rep, c, _fit = acr
    for name in ('corner left (accG > 0.3 g, low slip)', 'corner right (accG < -0.3 g, low slip)'):
        assert c[name]['sign_agree'] > 0.95 and c[name]['aligning'] > 0.8
    drift = c['drift (rear slip > 0.15 rad, countersteer)']
    assert drift['sign_agree'] > 0.93 and drift['toward_countersteer'] > 0.8
    assert drift['toward_countersteer'] > drift['mz_only_toward_countersteer'] + 0.15   # the caster arm does it
    assert c['front past peak (front slip > 0.3 rad)']['sign_agree'] > 0.93
    assert c['airborne fronts (load < 300 N)']['ours_p95_abs'] < 0.02          # no force in the air
    assert c['full lock (|steer| > 0.99)']['r'] > 0.90                         # finalFF has no lock term to miss (the boost and finalFF's own scrub couple bend the shape a little)
    assert c['front lock-up (brake, front slip ratio > 0.3)']['sign_agree'] > 0.8     # (finalFF's scrub couple is largest locked; ours plays none)
    assert c['standing (< 0.5 km/h)']['ours_p95_abs'] < 0.01                   # no buzz standing
    land = c['landing (front load > 2.5x its median)']
    assert land['ours_mean_abs'] < 1.25 * land['ff_mean_abs']


@needs_dump
def test_acr_level_and_thermal_headroom_in_normal_driving(acr):
    d, rep, _c, _fit = acr
    res = R.load_wheel()
    shaped = np.array([R.driver_shape(int(v), res['lut'], res['deadband']) for v in rep['force']]) / FULL
    mean = R.thermal_mean(d['t'], shaped)
    assert mean.max() < R.THERMAL_LIMIT * (1 - R.THERMAL_BAND) / 1.5        # 1.5x headroom before the derate starts (2x at the old 75 % top)
    moving = d['speed'] / 3.6 > fe.MIN_SPEED
    assert np.mean(np.abs(rep['force'][moving]) / FULL > wheel_torque.TOP + 0.001) == 0.0       # the knee mapping's top is 92 %


@needs_dump
def test_the_knee_mapping_plays_the_drive_inside_the_budget_on_the_g29(acr):
    d, rep, _c, _fit = acr
    moving = d['speed'] / 3.6 > fe.MIN_SPEED
    out = np.abs(rep['force']) / FULL
    assert 0.55 < np.percentile(out[moving], 99) <= wheel_torque.TOP
    scales = [float(np.median(rep['scale'][moving & (d['seg'] == seg)])) for seg in np.unique(d['seg'])
              if (moving & (d['seg'] == seg)).sum() > 20]
    assert all(s == fe.RackForce.COLUMN_SCALE for s in scales)                         # the column scale is frozen
    assert np.mean(out[moving] >= wheel_torque.TOP - 1e-4) < 0.02                                       # the clamp is rare
    p50, p90, p99 = np.percentile(out[moving], [50, 90, 99])
    assert p99 > p90 > 1.5 * p50                                                       # p90-p99 not squashed


@needs_dump
def test_acr_slip_angle_follows_steer_minus_flow(acr):
    d, _rep, _c, _fit = acr
    check = R.slip_sign_check(d)
    assert check['r_alpha_steer_cornering'] > 0.5 and check['sign_agree_cornering'] > 0.75
    assert check['r_dalpha_dsteer_fast'] > 0.4 and check['n_fast'] > 1000      # the fast changes are the steering's


@needs_dump
def test_the_aligning_spring_the_driver_renders_is_a_part_of_the_model_torque(acr):
    d, rep, _c, _fit = acr
    al = rep['align']
    ok = np.isfinite(al['k']) & (d['speed'] / 3.6 > fe.MIN_SPEED)
    assert np.all(al['k'][ok] > 0)
    spring = R.rendered_spring_nm(al['k'], al['theta_a_deg'] - al['theta_deg'], rep['scale'], rep['peak'])
    rim = rep['rim']
    same = np.sign(spring[ok]) == np.sign(rim[ok])
    big = np.abs(rim[ok]) > 0.5
    assert np.mean(same[big]) > 0.7                      # the spring pushes the way the model's torque does
    assert np.mean(np.abs(spring[ok]) <= np.abs(rim[ok]) * 1.5 + 0.2) > 0.9


@needs_dump
def test_the_learner_fits_every_car(acr):
    # every stretch fits finalFF with R^2 0.946-0.995 by its own least squares (trail 6-46 mm, scrub 13-35 mm)
    _d, _rep, _c, fit = acr
    assert min(fit["r2_per_stretch"].values()) > 0.88, fit['r2_per_stretch']
    assert min(fit['learnt_scrub_mm'].values()) > 5.0, fit['learnt_scrub_mm']


# ---------------------------------------------------------------- 3. the calibrated G29

@pytest.fixture(scope='module')
def g29():
    return R.load_wheel()


def test_the_correction_makes_the_g29_linear_without_friction_compensation(g29):
    s = R.linearity_summary(g29, 0)
    assert s['rms_err_5_95'] < 0.01 and s['max_abs_err'] < 0.03
    assert s['no_torque_below_request'] < 0.03                     # the deadband fade-in's cost
    u = R.linearity_summary(R.user_numbers_result(), 0)
    assert u['rms_err_5_95'] < 0.01


def test_friction_compensation_never_lifts_past_the_breakaway(g29):
    tau = R.torque_fn(g29)
    for comp in (50, 100):
        dead = wcal.compensated_deadband(g29, comp)
        assert dead <= wcal.FRICTION_COMP_CAP * g29['breakaway'] * FULL + 1
        assert tau(dead / FULL) < tau(g29['breakaway'])           # standing, it cannot start the wheel by itself


@pytest.mark.parametrize('comp', [50, 100])
def test_friction_compensation_adds_no_more_than_the_friction(g29, comp):
    rs, out, _dead = R.linearity(g29, comp)
    extra = out - rs
    assert extra[(rs > 0.05) & (rs < 0.95)].max() <= comp / 100.0 * g29['friction'] + 0.01


@pytest.mark.parametrize('comp', [0, 50, 100])
def test_a_hands_off_wheel_settles_at_acr_like_centring(g29, comp):
    # ACR: finalFF 1.23 full scale per unit steer near the centre at 60+ km/h; 540 deg of rotation: 0.0046 / deg
    for k in (0.003, 0.006):
        amp, crossings, _final = R.hands_off(g29, k, comp)
        assert crossings == 0 and amp < 25.0


def test_friction_compensation_does_not_destabilise_a_warm_wheel(g29):
    stable = R.hands_off(g29, 0.012, 0, friction_scale=0.5)
    comp = R.hands_off(g29, 0.012, 100, friction_scale=0.5)          # (100 is held to the 50 % maximum)
    assert stable[1] == 0
    assert comp[1] == 0


# ---------------------------------------------------------------- 4. Forza and the texture

def test_forza_ext_force_never_pushes_and_never_flips_the_games_force():
    assert R.forza_force_property() == {'same_sign': 0, 'over_raw': 0, 'next_upload_flipped': 0}


@needs_fh6
def test_forza_gain_on_the_fh6_capture_stays_in_bounds():
    items = R.load_fh6()
    lv = R.forza_levels(items)
    floor = 1.0 - fa.STRENGTH * (1.0 - fa.G_MIN)
    assert floor - 1e-9 <= lv['min'] and lv['max'] <= 1.0
    assert 0.8 < lv['mean_moving'] < 1.0


def test_forza_lightness_keeps_the_caster_share():
    gap, _x = R.lightness_gap()
    assert gap < 0.02


def test_texture_is_zero_mean_subtle_and_acr_only():
    out, st = R.texture_synthetic()
    assert abs(st['mean_share']) < 1e-3 and st['max_1s_mean_share'] < 2e-3
    assert st['peak_share'] <= ft.TOTAL_CAP * ft.LEVEL + 1e-6
    assert st['impacts'] >= 5
    _out, st0 = R.texture_synthetic(steering_force=0.0)
    # the limiter holds the road/impact/lock sum to the floor; the engine hum is outside it (gain 1 when the part is on,
    # feel 1.0 as texture_synthetic runs it), so its own largest swing, CAP, adds at the same level
    assert st0['peak_share'] <= (ft.FLOOR + ft.EngineHum.CAP) * ft.LEVEL + 1e-6
    off, _st_other = R.texture_synthetic(seconds=5.0, game='acc')
    assert not np.any(off)
    # no pull with the steering force: its sign does not bias the texture
    pos, _ = R.texture_synthetic(steering_force=0.3 * FULL)
    neg, _ = R.texture_synthetic(steering_force=-0.3 * FULL)
    assert np.allclose(pos, neg)
    assert math.isfinite(float(out.sum()))
