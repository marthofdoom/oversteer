"""scripts/ffb_laws.py and ffb-new-game.py on synthetic drives: no capture, wheel or user file is read."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import ffb_laws as laws_mod  # noqa: E402

DT = 0.01
TP0 = (-0.009, 0.039)


def _drive(mus, peaks, trail, seed=0):
    """Stretches of 60 s, one grip each: a tyre whose r rises to mu at the peak slip and stays; `trail(mu, u, x, slip)` in m."""
    rng = np.random.default_rng(seed)
    n = int(60 / DT)
    cols = {k: [] for k in ('t', 'slip', 'fyf', 'pneu')}
    for i, (mu, peak) in enumerate(zip(mus, peaks)):
        slip = np.abs(rng.normal(0, 0.8, n)) * peak
        s = np.minimum(slip / peak, 1.0)
        r = mu * (1 - (1 - s) ** 2)
        fyf = r * 4000.0
        cols['t'].append(i * 60 + np.arange(n) * DT)
        cols['slip'].append(slip)
        cols['fyf'].append(fyf)
        cols['pneu'].append(trail(mu, r / mu, slip / peak, slip) * fyf)
    cols = {k: np.concatenate(v) for k, v in cols.items()}
    m = len(cols['t'])
    cols.update(mass=np.full(m, np.nan), bl=np.full(m, 0.6), fzf=np.full(m, 4000.0))
    return cols


def _tyre(mu, u, x, slip):
    return (TP0[0] + TP0[1] * mu) * np.where(x < 1, 1 - 0.4 * u ** 2, 0.5 / np.maximum(x, 1) ** 2)


def _laws():
    cols = _drive([0.85] * 3 + [1.3] * 3, [0.3] * 3 + [0.15] * 3, _tyre)
    return laws_mod.fit_laws([(cols, laws_mod.causes(cols, DT))], ['synthetic'])


def test_causes_find_each_stretchs_grip_and_the_bands_peak_slip():
    cols = _drive([0.85] * 2 + [1.3] * 2, [0.3] * 2 + [0.15] * 2, _tyre)
    c = laws_mod.causes(cols, DT)
    assert c['reached'].all()
    assert abs(c['mu'][0] - 0.85) < 0.03 and abs(c['mu'][-1] - 1.3) < 0.04
    assert 0.2 < c['peak'][0] < 0.36 and 0.1 < c['peak'][-1] < 0.18


def test_fit_laws_recovers_the_no_slip_line_and_the_fade():
    laws = _laws()
    a, b = laws['tp0']
    assert abs((a + b * 1.3) - (TP0[0] + TP0[1] * 1.3)) < 0.003 and abs((a + b * 0.85) - (TP0[0] + TP0[1] * 0.85)) < 0.003
    assert laws['cells']['u 0.0-0.5'] > laws['cells']['u 0.9-1.0'] > laws['cells']['x 1.7-2.5']


def test_a_rack_following_the_tyre_is_not_flagged_on_another_drive():
    laws = _laws()
    cols = _drive([0.9] * 3 + [1.25] * 3, [0.3] * 3 + [0.15] * 3, _tyre, seed=5)
    c = laws_mod.causes(cols, DT)
    rows = laws_mod.trail_check(laws, c, DT)
    assert rows and not any(r[6] for r in rows if r[1].startswith('u'))
    assert 'FLAG' not in laws_mod.grip_response(laws, c)['verdict']


def test_a_fade_at_a_fixed_slip_is_flagged_on_low_grip_and_a_fixed_trail_does_not_follow_the_grip():
    laws = _laws()
    old_rbr = lambda mu, u, x, slip: 0.042 / (1 + (slip / 0.105) ** 4)          # noqa: E731
    cols = _drive([0.85] * 3 + [1.3] * 3, [0.3] * 3 + [0.15] * 3, old_rbr)
    c = laws_mod.causes(cols, DT)
    low = [r for r in laws_mod.trail_check(laws, c, DT) if r[0] == '.7-1' and r[1] in ('u 0.8-0.9', 'u 0.9-1.0')]
    assert low and all(r[6] and r[5] < 0.6 for r in low)
    fixed = _drive([0.85] * 3 + [1.3] * 3, [0.3] * 3 + [0.15] * 3, lambda mu, u, x, slip: 0.042 + 0 * slip)
    assert 'does not follow the grip' in laws_mod.grip_response(laws, laws_mod.causes(fixed, DT))['verdict']


def test_lock_fit_takes_the_understeer_out():
    rng = np.random.default_rng(1)
    n = 20000
    t = np.arange(n) * DT
    vx = 12 + 20 * rng.random(n)
    ay = np.clip(np.cumsum(rng.normal(0, 0.02, n)), -2.5, 2.5)
    yaw = ay / vx
    lock, k, wheelbase = 0.5, 0.004, 2.6
    cols = {'t': t, 'steer': (wheelbase * yaw / vx + k * ay) / lock, 'vx': vx, 'vy': np.zeros(n), 'yaw': yaw, 'ay_s': ay}
    fit = laws_mod.lock_fit(cols, wheelbase)
    assert abs(fit['lock'] - lock) < 0.01 and abs(fit['K'] - k) < 0.0005
    assert fit['naive'] < 0.9 * lock          # understeer read as a smaller lock


def test_routes_follow_the_channels():
    acr = dict.fromkeys(('tyre_fy', 'tyre_load', 'tyre_mz', 'tyre_slip_angle', 'accel', 'yaw_rate', 'steer', 'vel'), 1.0)
    r = dict((s, (route, needs)) for s, route, needs in laws_mod.routes(acr))
    assert 'own Fy' in r['front side force'][0] and 'own Mz' in r['pneumatic trail'][0] and 'mass' not in r
    bare = dict.fromkeys(('accel', 'yaw_rate', 'steer', 'vel', 'car'), 1.0)
    r = dict((s, (route, needs)) for s, route, needs in laws_mod.routes(bare))
    assert 'Newton' in r['front side force'][0] and 'kinematic' in r['front slip'][0] and 'car table' in r['mass'][0]
    assert 'the law' in r['pneumatic trail'][0]
    assert 'NONE' in dict((s, route) for s, route, _n in laws_mod.routes({}))['front side force']
