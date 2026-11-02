#!/usr/bin/env python3
"""Independent check of Countersteer FFB against physics, the game's own force and the calibrated wheel, as
numbers (tests/test_ffb_physics.py asserts on the same functions).

    scripts/ffb_physics_report.py [--dump ~/.cache/ffb-slip/dump.npz] [--hz 100] [--fh6 FILE.ovcap.gz]
                                  [--wheel tests/data/g29-fit-result.json]

Sections:
  1. Signs and directions per game (ACR from the dump where it covers the case, synthetic otherwise; RBR and
     BeamNG synthetic: no game force recorded for them).
  2. ACR: the dump replayed through RackForce (with the per-car learner) against finalFF: R^2, lag, clipping,
     the force's level against the wheel's capacity and the driver's thermal limit.
  3. The calibrated G29: requested torque -> app LUT / deadband (+ friction compensation) -> the driver's
     shaping (deadband fade-in) -> motor command -> the fitted torque curve: linearity error; a hands-off
     wheel in a loop with a centring game force and latency, with and without friction compensation.
  4. Forza add-on (FH6 capture) and the texture: levels, zero mean, no pull.

Nothing here touches a wheel, /sys or the running app; the capture and the dump are only read. The car
properties the learner keeps go to a temporary file.
"""
import argparse
import gzip
import json
import math
import types
import os
import random
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from oversteer import ffb_addon as fa  # noqa: E402
from oversteer import ffb_carprops as cp  # noqa: E402
from oversteer import ffb_engine as fe  # noqa: E402
from oversteer import ffb_rbr as fr  # noqa: E402
from oversteer import ffb_texture as ft  # noqa: E402
from oversteer import wheel_calib as wc  # noqa: E402
from oversteer import wheel_calibration as wcal  # noqa: E402
from oversteer.telemetry_formats import Sample  # noqa: E402

FULL = fe.FULL
HERE = os.path.dirname(os.path.abspath(__file__))
DUMP = os.path.expanduser('~/.cache/ffb-slip/dump.npz')
FH6 = os.path.expanduser('~/.var/app/io.github.berarma.Oversteer/data/oversteer/captures/20261030-205514.ovcap.gz')
WHEEL = os.path.join(HERE, '..', 'tests', 'data', 'g29-fit-result.json')

# The driver (new-lg4ff hid-lg4ff.c): LG4FF_DEADBAND_FADE, LG4FF_THERMAL_TAU_MS, ..._LIMIT_DEFAULT, ..._BAND
DEADBAND_FADE = 512
THERMAL_TAU = 90.0
THERMAL_LIMIT = 0.35
THERMAL_BAND = 0.10


# ---------------------------------------------------------------- the ACR dump

def load_dump(path=DUMP):
    d = dict(np.load(path))
    t = d['t']
    gap = np.diff(t)
    seg = np.concatenate([[0], np.cumsum((gap > 2.0) | (gap < 0.0))])   # 8 stretches: gaps of 51 s+, a clock step back
    d['seg'] = seg
    return d


def acr_sample(d, i, car, interp=None):
    """A Sample as the bridge decoder makes it (telemetry_formats._ovst3/_ovst5): game units, steer positive left,
    speed in m/s. `interp`: (j, w) to take w of the way from row i to row j."""
    def v(k):
        a = d[k][i]
        if interp is not None:
            j, w = interp
            a = a + (d[k][j] - a) * w
        return a
    s = Sample(6000.0, 7500.0, gear=int(d['gear'][i]), speed=float(v('speed')) / 3.6, car=car, game='acr')
    s.steer = -float(v('steer'))
    s.game_ffb = float(v('ffb'))
    s.tyre_mz, s.tyre_fx, s.tyre_fy = tuple(v('mz')), tuple(v('fx')), tuple(v('fy'))
    s.tyre_slip_angle, s.tyre_slip_ratio, s.tyre_load = tuple(v('sa')), tuple(v('sr')), tuple(v('load'))
    s.brake, s.throttle = float(v('brake')), float(v('gas'))
    return s


def replay_acr(d, hz=100.0, learn=True):
    """Feed the dump through RackForce at `hz` (linear interpolation between the ~4 Hz snapshots, so the learner's
    sample-count prior weighs as it does live), the game's raw level = finalFF x FULL (ACR's FF gain 100 %).
    Returns per snapshot: our force (ext_force units, full strength), ready flag, trail, scrub."""
    tmp = tempfile.mkdtemp(prefix='ffb-verify-')
    kept = (cp.STORE.path, cp.ASYNC)
    cp.STORE.use(os.path.join(tmp, 'props.json'))
    cp.ASYNC = False
    rack = fe.RackForce()
    rack.LEARNS = learn
    n = len(d['t'])
    out = np.zeros(n)
    ready = np.zeros(n, dtype=bool)
    trail = np.zeros(n)
    scrub = np.zeros(n)
    t0 = d['t'][0]
    for i in range(n):
        car = 'acr/seg%d' % d['seg'][i]
        s = acr_sample(d, i, car)
        t = d['t'][i] - t0
        rack.feed(t, s, s.game_ffb * FULL)
        out[i] = rack.force(s)
        ready[i] = rack.ready
        trail[i], scrub[i] = rack.trail, rack.scrub
        if i + 1 < n and d['seg'][i + 1] == d['seg'][i] and hz:
            gap = d['t'][i + 1] - d['t'][i]
            k = int(gap * hz)
            for j in range(1, k):
                w = j / k
                si = acr_sample(d, i, car, (i + 1, w))
                rack.feed(t + gap * w, si, si.game_ffb * FULL)
    rack.reset(None)
    cp.STORE.use(kept[0])
    cp.ASYNC = kept[1]
    return {'force': out, 'ready': ready, 'trail': trail, 'scrub': scrub}


def r2(y, x):
    """1 - SSE / SST of x as a prediction of y (no refit)."""
    y, x = np.asarray(y), np.asarray(x)
    return 1.0 - float(np.sum((y - x) ** 2)) / float(np.sum((y - y.mean()) ** 2))


def lag_s(d, a, b, mask, max_lag=4):
    """The lag (s) of b behind a by cross-correlation per stretch (snapshots ~0.26 s apart), parabolic peak."""
    dt = float(np.median(np.diff(d['t'])))
    tot = np.zeros(2 * max_lag + 1)
    for seg in np.unique(d['seg']):
        m = (d['seg'] == seg) & mask
        x, y = a[m] - a[m].mean(), b[m] - b[m].mean()
        if len(x) < 50:
            continue
        for k, lag in enumerate(range(-max_lag, max_lag + 1)):
            if lag >= 0:
                tot[k] += np.dot(x[:len(x) - lag], y[lag:])
            else:
                tot[k] += np.dot(x[-lag:], y[:len(y) + lag])
    k = int(np.argmax(tot))
    off = 0.0
    if 0 < k < len(tot) - 1:
        den = tot[k - 1] - 2 * tot[k] + tot[k + 1]
        off = 0.5 * (tot[k - 1] - tot[k + 1]) / den if den else 0.0
    return (k - max_lag + off) * dt


def front_torque_arrays(d, trail=fe.CASTER_TRAIL, scrub=fe.SCRUB):
    mz = d['mz'][:, 0] + d['mz'][:, 1]
    fy = d['fy'][:, 0] + d['fy'][:, 1]
    dfx = d['fx'][:, 0] - d['fx'][:, 1]
    return mz - trail * fy - scrub * dfx, mz, fy, dfx


def acr_cases(d, rep):
    """Section 1 on the dump: per case, n, our force's sign agreeing with finalFF's, r, and |ours| / |finalFF|."""
    ours = rep['force'] / FULL
    ff = d['ffb']
    sp = d['speed'] / 3.6
    load_f = d['load'][:, 0] + d['load'][:, 1]
    sa_f = np.max(np.abs(d['sa'][:, :2]), axis=1)
    sa_r = np.max(np.abs(d['sa'][:, 2:]), axis=1)
    sr_f = np.max(np.abs(d['sr'][:, :2]), axis=1)
    steer_left = -d['steer']
    yaw = d['angvel'][:, 1]                                   # ACR: positive turning left
    torque, mz, fy, dfx = front_torque_arrays(d)
    moving = sp > fe.MIN_SPEED
    lat = d['accG'][:, 0]
    cases = {
        'corner left (accG > 0.3 g, low slip)': moving & (lat > 0.3) & (sa_f < 0.1),
        'corner right (accG < -0.3 g, low slip)': moving & (lat < -0.3) & (sa_f < 0.1),
        'drift (rear slip > 0.15 rad, countersteer)': moving & (sa_r > 0.15) & (np.sign(steer_left) == -np.sign(yaw))
        & (np.abs(yaw) > 0.2) & (np.abs(steer_left) > 0.05),
        'front past peak (front slip > 0.3 rad)': moving & (sa_f > 0.3),
        'airborne fronts (load < 300 N)': moving & (load_f < 300),
        'landing (front load > 2.5x its median)': moving & (load_f > 2.5 * np.median(load_f[moving])),
        'full lock (|steer| > 0.99)': moving & (np.abs(d['steer']) > 0.99),
        'front wheelspin (throttle, front slip ratio > 0.2)': moving & (d['gas'] > 0.5) & (sr_f > 0.2) & (d['brake'] < 0.05),
        'front lock-up (brake, front slip ratio > 0.3)': moving & (d['brake'] > 0.3) & (sr_f > 0.3),
        'standing (< 0.5 km/h)': d['speed'] < 0.5,
        'creeping (0.5-5 km/h)': (d['speed'] >= 0.5) & (d['speed'] < 5),
    }
    out = {}
    for name, m in cases.items():
        n = int(m.sum())
        if n == 0:
            out[name] = {'n': 0}
            continue
        big = m & (np.abs(ff) > 0.02)
        agree = float(np.mean(np.sign(ours[big]) == np.sign(ff[big]))) if big.any() else None
        rr = float(np.corrcoef(ours[m], ff[m])[0, 1]) if n > 10 and ours[m].std() > 0 and ff[m].std() > 0 else None
        out[name] = {
            'n': n, 'sign_agree': agree, 'r': rr,
            'ours_mean_abs': float(np.mean(np.abs(ours[m]))), 'ff_mean_abs': float(np.mean(np.abs(ff[m]))),
            'ours_p95_abs': float(np.percentile(np.abs(ours[m]), 95)), 'ff_p95_abs': float(np.percentile(np.abs(ff[m]), 95)),
            # caster share of the torque: |trail Fy| / |torque|
            'caster_share': float(np.median(np.abs(fe.CASTER_TRAIL * fy[m]) / np.maximum(np.abs(torque[m]), 1e-6))),
            'scrub_share': float(np.median(np.abs(fe.SCRUB * dfx[m]) / np.maximum(np.abs(torque[m]), 1e-6))),
        }
    # Drift: does our force turn the wheels toward the countersteer (the way the car is sliding)? Sample's steer is
    # positive left; positive front torque (game frame, z up) turns the wheels left. In a drift the countersteer is
    # against the yaw: toward the countersteer = torque sign == -sign(yaw)
    m = cases['drift (rear slip > 0.15 rad, countersteer)']
    if m.any():
        out['drift (rear slip > 0.15 rad, countersteer)']['toward_countersteer'] = float(
            np.mean(np.sign(torque[m]) == -np.sign(yaw[m])))
        out['drift (rear slip > 0.15 rad, countersteer)']['mz_only_toward_countersteer'] = float(
            np.mean(np.sign(mz[m]) == -np.sign(yaw[m])))
    # Corners: the force is aligning (turns the wheels back toward straight): torque sign == -sign(steer_left)
    for name in ('corner left (accG > 0.3 g, low slip)', 'corner right (accG < -0.3 g, low slip)'):
        m = cases[name] & (np.abs(steer_left) > 0.02) & (sa_r < 0.08)
        out[name]['aligning'] = float(np.mean(np.sign(torque[m]) == -np.sign(steer_left[m])))
    return out


def acr_fit(d, rep):
    """Section 2: our force against finalFF."""
    ours = rep['force'] / FULL
    ff = d['ffb']
    moving = d['speed'] / 3.6 > fe.MIN_SPEED
    ready = rep['ready'] & moving
    torque, mz, fy, dfx = front_torque_arrays(d)
    res = {
        'n_moving': int(moving.sum()), 'ready_share': float(ready.sum() / max(moving.sum(), 1)),
        'r2_moving': r2(ff[moving], ours[moving]), 'r2_ready': r2(ff[ready], ours[ready]) if ready.any() else None,
        'r_moving': float(np.corrcoef(ours[moving], ff[moving])[0, 1]),
        'gain_ratio_moving': float(np.sum(np.abs(ours[moving])) / np.sum(np.abs(ff[moving]))),
        'lag_s': lag_s(d, ff, ours, moving),
        'clip_share_ours': float(np.mean(np.abs(ours[moving]) >= 0.999)),
        'clip_share_ff': float(np.mean(np.abs(ff[moving]) >= 0.98)),
        'p50_p90_p99_ours': [float(v) for v in np.percentile(np.abs(ours[moving]), [50, 90, 99])],
        'p50_p90_p99_ff': [float(v) for v in np.percentile(np.abs(ff[moving]), [50, 90, 99])],
        'learnt_trail_mm': {}, 'learnt_scrub_mm': {},
    }
    # Fixed physical constants with a per-stretch strength (the best a non-learning model can do)
    for name, x in (('mz_only', mz), ('pooled_constants', torque)):
        tot_sse = tot_sst = 0.0
        for seg in np.unique(d['seg']):
            m = moving & (d['seg'] == seg)
            a = np.dot(x[m], ff[m]) / np.dot(x[m], x[m])
            tot_sse += np.sum((ff[m] - a * x[m]) ** 2)
            tot_sst += np.sum((ff[m] - ff[moving].mean()) ** 2)
        res['r2_' + name + '_per_stretch_scale'] = 1.0 - tot_sse / tot_sst
    for seg in np.unique(d['seg']):
        m = d['seg'] == seg
        last = np.flatnonzero(m)[-1]
        res['learnt_trail_mm'][int(seg)] = round(rep['trail'][last] * 1000, 1)
        res['learnt_scrub_mm'][int(seg)] = round(rep['scrub'][last] * 1000, 1)
        mm = m & moving
        res.setdefault('r2_per_stretch', {})[int(seg)] = round(r2(ff[mm], ours[mm]), 3) if mm.sum() > 20 else None
    return res


def thermal_mean(t, value, tau=THERMAL_TAU, gap_zero=1.0):
    """The driver's thermal mean (implicit Euler, as lg4ff_thermal_predict) of value^2 held between samples; a gap
    longer than gap_zero counts as 0 output (the driver stops, the wheel cools). Returns the mean after each."""
    m = 0.0
    out = np.zeros(len(t))
    prev = 0.0
    for i in range(len(t)):
        dt = 0.0 if i == 0 else t[i] - t[i - 1]
        p = prev if 0.0 <= dt <= gap_zero else 0.0
        dt = max(dt, 0.0)
        m = (m * tau + p * dt) / (tau + dt)
        prev = value[i] ** 2
        out[i] = m
    return out


# ---------------------------------------------------------------- the driver and the wheel

def lut_apply(lut, m):
    n = len(lut) - 1
    pos = m * n
    i = pos // FULL
    frac = pos - i * FULL
    if i >= n:
        return lut[n]
    return lut[i] + (lut[i + 1] - lut[i]) * frac // FULL


def driver_shape(level, lut=None, dead=0, derate=1.0):
    """lg4ff_shape_force for slot 0, soft clip and output cap off, no dither (its long-run mean is this)."""
    m = min(abs(int(level)), FULL)
    if derate != 1.0:
        m = int(m * derate)
    if m and lut:
        m = lut_apply(lut, m)
    if m and dead:
        lifted = dead + (FULL - dead) * m // FULL
        m = lifted * m // DEADBAND_FADE if m < DEADBAND_FADE else lifted
    return -m if level < 0 else m


def load_wheel(path=WHEEL):
    with open(path) as f:
        return json.load(f)


def torque_fn(result):
    """tau(u) of the fitted curve, as build_lut inverts it (torque_model over the knots, zero to the dead zone)."""
    d = result['deadband'] / FULL
    xs, ys = wc.torque_model(result['knots'], np.asarray(result['curve']), d)
    return lambda u: float(np.interp(abs(u), xs, ys)) * (1.0 if u >= 0 else -1.0)


def user_numbers_result():
    """The task's numbers: torque at command 0.1..1.0 (0.6 unknown: interpolated), dead zone 8 %, friction 6.2 %;
    LUT built by this branch's wheel_calib.build_lut."""
    knots = [0.0, 0.08, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    curve = [0.0, 0.0, 0.042, 0.107, 0.194, 0.670, 0.743, 0.814, 0.885, 0.914, 1.0, 1.0]
    lut, dead = wc.build_lut(knots, np.asarray(curve), 0.08)
    return {'knots': knots, 'curve': curve, 'deadband': dead, 'lut': lut, 'friction': 0.062, 'breakaway': 0.1476}


def linearity(result, friction_comp=0, points=401):
    """Requested torque r (share of the wheel's full-scale torque) -> delivered torque, through the app's deadband
    with `friction_comp` % and the driver's shaping. Returns (r, delivered)."""
    dead = wcal.compensated_deadband(result, friction_comp)
    lut = wcal.compensated_lut(result, friction_comp)
    tau = torque_fn(result)
    rs = np.linspace(0.0, 1.0, points)
    out = np.array([tau(driver_shape(int(round(r * FULL)), lut, dead) / FULL) for r in rs])
    return rs, out, dead


def linearity_summary(result, friction_comp=0):
    rs, out, dead = linearity(result, friction_comp)
    err = out - rs
    k = int(np.argmax(np.abs(err[1:]))) + 1
    mid = (rs >= 0.05) & (rs <= 0.95)
    k2 = int(np.argmax(np.abs(np.where(mid, err, 0.0))))
    return {'deadband_cmd': dead / FULL, 'max_abs_err': float(np.abs(err[1:]).max()), 'at_request': float(rs[k]),
            'err_there': float(err[k]), 'max_abs_err_5_95': float(np.abs(err[mid]).max()), 'at_5_95': float(rs[k2]),
            'rms_err_5_95': float(np.sqrt(np.mean(err[mid] ** 2))),
            'no_torque_below_request': float(rs[np.flatnonzero(out > 0)[0] - 1])}


def hands_off(result, k_per_deg, friction_comp=0, latency=0.030, theta0=30.0, seconds=10.0, friction_scale=1.0,
              h=0.0005):
    """A hands-off wheel released at theta0 degrees, the game's force a centring spring -k theta (share of full
    scale per degree) seen `latency` s late plus the wheel's own fitted latency, through the driver's shaping and
    the fitted torque curve, against the fitted friction (sliding f, breakaway = the torque at the measured
    breakaway command), viscous damping and inertia. friction_scale: the real friction as a share of the measured
    (a warm wheel). Returns (largest |theta| over the last 3 s, zero crossings over the last 3 s, final theta)."""
    a_fs = result['a_fs']
    f = result['friction'] * friction_scale
    beta = result['damping']
    tau = torque_fn(result)
    stick = tau(result['breakaway']) * friction_scale
    dead = wcal.compensated_deadband(result, friction_comp)
    lut = wcal.compensated_lut(result, friction_comp)
    lat = latency + result.get('fit_latency_ms', 4.0) / 1000.0
    n = int(seconds / h)
    delay = int(round(lat / h))
    hist = [theta0] * (delay + 1)
    theta, omega = theta0, 0.0
    tail = []
    for i in range(n):
        seen = hist[-delay - 1] if delay else theta
        req = min(max(-k_per_deg * seen, -1.0), 1.0)
        u = driver_shape(int(round(req * FULL)), lut, dead) / FULL
        torque = tau(u)
        if omega == 0.0 and abs(torque) <= stick:
            acc = 0.0
        else:
            sgn = math.copysign(1.0, omega) if omega != 0.0 else math.copysign(1.0, torque)
            acc = a_fs * (torque - f * sgn - beta * omega)
        new = omega + acc * h
        if omega != 0.0 and new * omega < 0.0 and abs(torque) <= stick:
            new = 0.0                                     # friction stops it, it does not reverse it
        omega = new
        theta += omega * h
        hist.append(theta)
        if len(hist) > delay + 2:
            hist.pop(0)
        if i * h >= seconds - 3.0:
            tail.append(theta)
    tail = np.asarray(tail)
    crossings = int(np.sum(np.diff(np.sign(tail)) != 0))
    return float(np.abs(tail).max()), crossings, float(tail[-1])


# ---------------------------------------------------------------- synthetic signs (ACR, RBR, BeamNG)

def acr_synthetic():
    """front_torque on synthetic axles: (case, torque Nm, the direction it turns the wheels)."""
    def s(mz, fy, fx=(0.0, 0.0)):
        x = Sample(6000.0, 7500.0, gear=3, speed=20.0, car='acr/x', game='acr')
        x.tyre_mz, x.tyre_fy, x.tyre_fx = (mz / 2, mz / 2, 0, 0), (fy / 2, fy / 2, 0, 0), (fx[0], fx[1], 0, 0)
        return fe.front_torque(x)
    rows = []
    # Left corner: wheels steered left, Fy left (+), Mz = -tp Fy; the torque must turn them back right (negative)
    rows.append(('left corner, linear', s(-0.040 * 6000, 6000), 'right (aligning)'))
    rows.append(('right corner, linear', s(0.040 * 6000, -6000), 'left (aligning)'))
    # Left-hand drift, countersteered right: fronts still push left (Fy +) with Mz collapsed past the peak
    rows.append(('left drift, countersteer right, Mz gone', s(-10.0, 5000), 'right (toward the countersteer)'))
    # Reversing with the wheels steered left: the fronts' side force points right (Fy -); the caster arm now
    # turns them further left (toward the lock): a real car's steering is unstable backwards
    rows.append(('reverse, steered left (Fy -, Mz 0)', s(0.0, -1500), 'left (to lock: physical in reverse)'))
    rows.append(('airborne (no forces)', s(0.0, 0.0), 'none'))
    # Front-wheel drive, the right front pulling harder (left spinning): positive scrub turns the wheels left
    rows.append(('drive, right front pulls harder', s(0.0, 0.0, (500.0, 2500.0)), 'left (away from the stronger wheel)'))
    # Braking, the left front braking harder: positive scrub turns the wheels left (toward the harder braking)
    rows.append(('brake, left front harder', s(0.0, 0.0, (-3000.0, -1000.0)), 'left (toward the harder brake)'))
    rows.append(('lock-up both fronts, equal', s(0.0, 300.0, (-4000.0, -4000.0)), 'right, small (Fy only)'))
    return rows


def rbr_synthetic():
    """RBR: the game's level for a kingpin torque sweep and ours. Returns rows of (T, game level, ours)."""
    rbr = fr.RbrForce()
    rbr.peak = float(FULL)                      # the game's top seen (STRENGTH 1)
    S = 100.0
    rows = []
    for T in np.linspace(-400, 400, 161):
        level = fr.level_of(fr.magnitude(fr.ngp_curve(float(T), S)))
        rows.append((float(T), level, rbr.force(level)))
    return rows


def beamng_softlock():
    """BeamNG's soft lock with the game's centring learnt as raw = -c steer (centre_sign -1): (steer, push)."""
    rack = fe.BeamNGRack()
    rack.centre_sign = -1.0
    rack.sign, rack.scale = 1.0, rack.FF_SCALE
    out = []
    for st in (-1.0, -0.97, 0.0, 0.97, 1.0):
        x = Sample(6000.0, 7500.0, gear=2, speed=10.0, car='beamng/x', game='beamng')
        x.steer_input = st
        out.append((st, rack.softlock(x)))
    return out


# ---------------------------------------------------------------- Forza and the texture

def load_fh6(path=FH6, limit=None):
    from oversteer.telemetry_capture import read_capture
    from oversteer.telemetry_formats import decode_sample
    items = []
    for t, _addr, data in read_capture(path)[1]:
        s = decode_sample(data)
        if s is not None and s.game == 'forza-fh':
            items.append((t, s))
            if limit and len(items) >= limit:
                break
    return items


def forza_levels(items, strength=fa.STRENGTH, raw=10000.0):
    """The add-on over a capture (a constant raw stand-in: the game's force was not recorded): g_eff per sample."""
    addon = fa.ForzaLightness(strength)
    gs, moving, steer, rows = [], [], [], []
    for t, s in items:
        addon.feed(t, s, raw if s.car is not None else None)
        g = addon.gain(t)
        gs.append(g)
        moving.append(s.car is not None and (s.speed or 0) > fa.MIN_SPEED)
        steer.append(s.steer if s.steer is not None else 0.0)
    gs, moving = np.asarray(gs), np.asarray(moving)
    steps = np.abs(np.diff(gs))
    tt = np.asarray([t for t, _ in items])
    gm = gs[moving]
    return {
        'n': len(gs), 'moving_s': float(np.sum(np.diff(tt)[moving[1:]])),
        'share_under_0.95': float(np.mean(gm < 0.95)), 'share_under_0.8': float(np.mean(gm < 0.8)),
        'share_under_0.6': float(np.mean(gm < 0.6)), 'min': float(gs.min()), 'max': float(gs.max()),
        'mean_moving': float(gm.mean()), 'largest_step': float(steps.max()),
        'reversals_per_s': float(np.sum(np.diff(np.sign(np.diff(gs))) != 0) / max(tt[-1] - tt[0], 1e-9)),
    }


def caster_kept_lightness(sample, a=fa.A, k=fa.K, q=fa.Q):
    """What the add-on's own physics (steering torque ~ Fy (pneumatic trail + caster trail), only the pneumatic part
    fading as exp(-s^q)) gives: G_MIN + (1 - G_MIN) exp(-s^q). ffb_addon.lightness has max(exp(-s^q), G_MIN), which
    lets the caster share fade too until the floor."""
    angles, ratios = sample.slip_angle, sample.slip_ratio
    if angles is None or ratios is None or len(angles) < 2 or len(ratios) < 2:
        return None
    s = 0.5 * (math.hypot(angles[0] / a, ratios[0] / k) + math.hypot(angles[1] / a, ratios[1] / k))
    if not math.isfinite(s):
        return None
    return fa.G_MIN + (1.0 - fa.G_MIN) * math.exp(-s ** q)


def lightness_gap(points=101, q=fa.Q):
    """(largest shortfall of the shipped g below the caster-kept one, the pneumatic share where it is)."""
    worst = (-1.0, 0.0)
    for i in range(1, points):
        x = i / (points - 1)                     # exp(-s^q): the pneumatic trail left
        s = (-math.log(x)) ** (1.0 / q)
        smp = types.SimpleNamespace(slip_angle=(s * fa.A, s * fa.A), slip_ratio=(0.0, 0.0))
        gap = (fa.G_MIN + (1 - fa.G_MIN) * x) - fa.lightness(smp, q=q)
        worst = max(worst, (abs(gap), x))
    return worst


def forza_force_property(trials=20000, seed=3):
    """ForzaLightness.force over random raw / g / margin: never the other way, never more than |raw|. Also the
    next upload within margin: raw' + ours keeps raw''s sign. Returns counts of violations."""
    rng = random.Random(seed)
    flips = over = next_flip = 0
    for _ in range(trials):
        raw = rng.randint(-FULL, FULL)
        g = rng.uniform(fa.G_MIN, 1.0)
        margin = rng.uniform(0, 3000)
        ours = fa.ForzaLightness.force(raw, g, margin)
        if raw and ours and math.copysign(1, ours) == math.copysign(1, raw):
            flips += 1
        if abs(ours) > abs(raw):
            over += 1
        nxt = raw + rng.uniform(-margin, margin)
        if nxt and (nxt + ours) * nxt < 0:
            next_flip += 1
    return {'same_sign': flips, 'over_raw': over, 'next_upload_flipped': next_flip}


def texture_synthetic(seconds=60.0, steering_force=0.3 * FULL, seed=7, game='acr'):
    """The texture on a synthetic ACR stream at 333 Hz: rough road under the fronts, a landing every 10 s, front
    lock-ups under braking every 15 s. Returns its output and stats (ext_force units)."""
    rng = random.Random(seed)
    tex = ft.Texture()
    h = 1.0 / 333.0
    out = []
    travel = [0.05, 0.05]
    vel = [0.0, 0.0]
    for n in range(int(seconds / h)):
        t = n * h
        s = Sample(6000.0, 7500.0, gear=3, speed=25.0, car='acr/x', game=game)
        for w in range(2):                                   # band-limited random suspension motion
            vel[w] = 0.9 * vel[w] + rng.gauss(0, 0.08)
            travel[w] += vel[w] * h
            travel[w] = 0.05 + 0.98 * (travel[w] - 0.05)
        s.susp = (travel[0], travel[1], 0.05, 0.05)
        phase = t % 10.0
        load = 3000.0 if phase > 0.4 else (50.0 if phase < 0.3 else 9000.0)    # air, then a landing
        s.tyre_load = (load, load * 0.9, 3000.0, 3000.0)
        locking = (t % 15.0) < 1.0
        s.brake = 1.0 if locking else 0.0
        s.vel = (25.0, 0.0, 0.0)
        s.wheel_speed = (5.0, 6.0, 25.0, 25.0) if locking else (25.0, 25.0, 25.0, 25.0)
        out.append(tex.force(t, s, steering_force))
    out = np.asarray(out)
    return out, {
        'mean_share': float(out.mean() / FULL), 'rms_share': float(np.sqrt(np.mean(out ** 2)) / FULL),
        'peak_share': float(np.abs(out).max() / FULL), 'impacts': tex.impact.count,
        'max_1s_mean_share': float(np.abs(np.convolve(out, np.ones(333) / 333, 'valid')).max() / FULL),
    }


# ---------------------------------------------------------------- report

def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--dump', default=DUMP)
    ap.add_argument('--hz', type=float, default=100.0)
    ap.add_argument('--fh6', default=FH6)
    ap.add_argument('--wheel', default=WHEEL)
    args = ap.parse_args()
    np.set_printoptions(precision=3, suppress=True)

    print('== 1. Signs (synthetic) ==')
    for name, torque, want in acr_synthetic():
        print('  ACR %-42s torque %+8.1f Nm -> turns the wheels %s' % (name, torque, want))
    rbr = rbr_synthetic()
    same = all((lv == 0 and o == 0) or (lv * o > 0) for _T, lv, o in rbr)
    mono = all(abs(b[2]) >= abs(a[2]) for a, b in zip(rbr[80:], rbr[81:]))
    lin = [(T, o) for T, lv, o in rbr if 0 < abs(o) < 0.69 * FULL]
    game_lin = [(T, lv) for T, lv, o in rbr if 0 < abs(o) < 0.69 * FULL]
    print('  RBR: ours has the game level\'s sign everywhere: %s; monotone in |T|: %s; R^2 vs T below the knee: '
          'ours %.4f, the game\'s %.4f' % (same, mono, np.corrcoef(*zip(*lin))[0, 1] ** 2,
                                          np.corrcoef(*zip(*game_lin))[0, 1] ** 2))
    print('  BeamNG soft lock (centring learnt as raw ~ -steer):', ', '.join('steer %+.2f -> %+.0f' % r for r in beamng_softlock()))

    if os.path.exists(args.dump):
        d = load_dump(args.dump)
        print('\n== 1/2. ACR dump: %d snapshots, %d stretches, %.0f s; replay at %.0f Hz ==' % (
            len(d['t']), len(np.unique(d['seg'])), float(np.sum(np.clip(np.diff(d['t']), 0.0, 1.0))), args.hz))
        rep = replay_acr(d, args.hz)
        for name, c in acr_cases(d, rep).items():
            print('  %-50s %s' % (name, json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in c.items()})))
        fit = acr_fit(d, rep)
        print('  fit:', json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in fit.items()}))
        # Thermal: our force through the G29 correction, mean square over 90 s
        res = load_wheel(args.wheel) if os.path.exists(args.wheel) else None
        ours = rep['force']
        moving = d['speed'] / 3.6 > fe.MIN_SPEED
        for label, gain in (('100 %', 1.0), ('150 %', 1.5), ('200 %', 2.0)):
            req = np.clip(ours * gain, -FULL, FULL)
            plain = thermal_mean(d['t'], req / FULL)
            line = '  game force x %s: clipped %.4f of moving time; thermal max mean %.3f (raw request)' % (
                label, float(np.mean(np.abs(req[moving]) >= FULL)), plain.max())
            if res:
                shaped = np.array([driver_shape(int(v), res['lut'], res['deadband']) for v in req]) / FULL
                th = thermal_mean(d['t'], shaped)
                line += ', %.3f through the G29 LUT + deadband (derate from %.3f, limit %.2f)' % (
                    th.max(), THERMAL_LIMIT * (1 - THERMAL_BAND), THERMAL_LIMIT)
            print(line)
        print('  ours above 50 / 80 / 98 %% of full scale: %.4f / %.4f / %.4f of moving time' % tuple(
            float(np.mean(np.abs(ours[moving]) / FULL > x)) for x in (0.5, 0.8, 0.98)))
    else:
        print('\n(no ACR dump at %s)' % args.dump)

    if os.path.exists(args.wheel):
        res = load_wheel(args.wheel)
        print('\n== 3. Calibrated G29 (%s): dead zone %.1f %%, friction %.1f %%, breakaway cmd %.1f %% ==' % (
            os.path.basename(args.wheel), res['deadband'] / FULL * 100, res['friction'] * 100, res['breakaway'] * 100))
        for label, r in (('fitted', res), ('task numbers', user_numbers_result())):
            for comp in (0, 50, 100):
                print('  %-13s friction comp %3d %%: %s' % (label, comp, json.dumps(
                    {k: round(v, 4) for k, v in linearity_summary(r, comp).items()})))
        rs, out, _ = linearity(res, 0, 21)
        print('  request -> delivered (comp 0):', ' '.join('%.2f>%.3f' % (a, b) for a, b in zip(rs, out)))
        rs, out, _ = linearity(res, 100, 21)
        print('  request -> delivered (comp 100):', ' '.join('%.2f>%.3f' % (a, b) for a, b in zip(rs, out)))
        print('  hands-off wheel, centring spring k, latency 30 ms (+%.0f ms wheel): last-3-s max |deg|, crossings, final' % (
            res.get('fit_latency_ms', 4.0)))
        for fs in (1.0, 0.5):
            for k in (0.003, 0.006, 0.012, 0.025, 0.05):
                row = []
                for comp in (0, 50, 100):
                    amp, cross, final = hands_off(res, k, comp, friction_scale=fs)
                    row.append('comp %3d: %5.1f deg %3d x %+6.1f' % (comp, amp, cross, final))
                print('    friction x%.1f k %.3f/deg | %s' % (fs, k, ' | '.join(row)))
    else:
        print('\n(no wheel fit at %s)' % args.wheel)

    print('\n== 4. Forza add-on and texture ==')
    print('  force() property:', forza_force_property())
    if os.path.exists(args.fh6):
        items = load_fh6(args.fh6)
        print('  FH6 capture %s: %s' % (os.path.basename(args.fh6), json.dumps(
            {k: round(v, 4) for k, v in forza_levels(items).items()})))
        shipped = fa.lightness
        fa.lightness = caster_kept_lightness
        try:
            print('  ... with the caster share kept (G_MIN + (1 - G_MIN) exp(-s^q)): %s' % json.dumps(
                {k: round(v, 4) for k, v in forza_levels(items).items()}))
        finally:
            fa.lightness = shipped
        print('  shipped g below the caster-kept g by up to %.2f (at pneumatic share %.2f)' % lightness_gap())
        tex = ft.Texture()
        nonzero = sum(1 for t, s in items[:20000] if tex.force(t, s, 10000.0) != 0.0)
        print('  texture on FH6 samples: %d of %d non-zero (ACR only by design)' % (nonzero, min(len(items), 20000)))
    out, stats = texture_synthetic()
    print('  texture, synthetic ACR (road + landings + lock-ups, steering 30 %%): %s' % json.dumps(
        {k: round(v, 5) if isinstance(v, float) else v for k, v in stats.items()}))
    out0, stats0 = texture_synthetic(steering_force=0.0)
    print('  ... steering force 0: %s' % json.dumps({k: round(v, 5) if isinstance(v, float) else v for k, v in stats0.items()}))


if __name__ == '__main__':
    main()
