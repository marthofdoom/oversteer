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
    assert fit['r2_moving'] > 0.88 and fit['r_moving'] > 0.94
    assert 0.55 < fit['p50_p90_p99_ours'][2] <= 0.75      # the rim torque on a G29-class wheel: within the 75 % budget
    assert abs(fit['lag_s']) < 0.1                     # (snapshots 0.26 s apart: a lag under ~0.1 s is not resolved)
    assert fit['clip_share_ours'] < 0.003                # the knee keeps it under the budget: nothing clips
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
    assert c['full lock (|steer| > 0.99)']['r'] > 0.95                         # finalFF has no lock term to miss
    assert c['front lock-up (brake, front slip ratio > 0.3)']['sign_agree'] > 0.9
    assert c['standing (< 0.5 km/h)']['ours_p95_abs'] < 0.01                   # no buzz standing
    land = c['landing (front load > 2.5x its median)']
    assert land['ours_mean_abs'] < 1.25 * land['ff_mean_abs']


@needs_dump
def test_acr_level_and_thermal_headroom_in_normal_driving(acr):
    d, rep, _c, _fit = acr
    res = R.load_wheel()
    shaped = np.array([R.driver_shape(int(v), res['lut'], res['deadband']) for v in rep['force']]) / FULL
    mean = R.thermal_mean(d['t'], shaped)
    assert mean.max() < R.THERMAL_LIMIT * (1 - R.THERMAL_BAND) / 2          # 2x headroom before the derate starts
    moving = d['speed'] / 3.6 > fe.MIN_SPEED
    assert np.mean(np.abs(rep['force'][moving]) / FULL > 0.8) == 0.0       # the steering budget is 75 %


@needs_dump
def test_the_learner_fits_every_car(acr):
    # every stretch fits finalFF with R^2 0.946-0.995 by its own least squares (trail 6-46 mm, scrub 13-35 mm)
    _d, _rep, _c, fit = acr
    assert min(fit['r2_per_stretch'].values()) > 0.92, fit['r2_per_stretch']
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
    assert st0['peak_share'] <= ft.FLOOR * ft.LEVEL + 1e-6
    off, st_fh = R.texture_synthetic(seconds=5.0, game='forza-fh')
    assert not np.any(off)
    # no pull with the steering force: its sign does not bias the texture
    pos, _ = R.texture_synthetic(steering_force=0.3 * FULL)
    neg, _ = R.texture_synthetic(steering_force=-0.3 * FULL)
    assert np.allclose(pos, neg)
    assert math.isfinite(float(out.sum()))
