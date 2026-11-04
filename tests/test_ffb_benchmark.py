"""The FFB benchmark (scripts/ffb_benchmark.py): binning, pooling over cars, basis labels, empty cells and scoring on small
synthetic data; no capture is read."""
import importlib.util
import math
import os
from types import SimpleNamespace

import numpy as np
import pytest

_HERE = os.path.dirname(__file__)


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, '..', 'scripts', file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


B = _load('ffb_benchmark', 'ffb_benchmark.py')
pytestmark = pytest.mark.skipif(B.MISSING is not None, reason="numpy/scipy missing")


def _rows(cell, seconds, trail=0.03, ratio=10.0, fy=3000.0, beta=0.02, rate=100.0, share=0.4, jitter=0.0, seed=0):
    """Rows of one drive in one cell: `seconds` at `rate` Hz, a restoring trail of `trail` m (torque and nm/kN follow)."""
    n = int(seconds * rate)
    rng = np.random.RandomState(seed)
    t = trail + jitter * rng.randn(n)
    torque = t * fy / ratio
    return {'cell': np.full(n, cell), 'dt': np.full(n, 1.0 / rate), 'beta': np.full(n, beta), 'fy': np.full(n, fy),
            'torque_nm': torque, 'share': np.full(n, share), 'trail_m': t, 'nm_per_kn': t * 1000.0 / ratio}


def _cell(lat, slip, speed, pedal):
    return ((lat * B.N_SLIP + slip) * B.N_SPEED + speed) * B.N_PEDAL + pedal


def _cat(parts):
    return {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}


# ---------------------------------------------------------------------------------------------------- binning

def test_the_bins_follow_the_brief():
    assert B.N_LAT >= 5
    assert B.SLIP_EDGES[:6] == (0.0, 0.05, 0.1, 0.2, 0.4, 0.7) and B.SLIP_EDGES[-1] == math.inf
    assert B.SPEED_EDGES[1:4] == (15.0, 25.0, 35.0)
    assert B.PEDALS == ('on', 'off', 'brake')
    assert B.N_CELLS == B.N_LAT * B.N_SLIP * B.N_SPEED * B.N_PEDAL


def test_cell_ids_round_trip_and_edges():
    ids = B.cell_ids([0.3, 0.3], [0.05, 0.0499], [25.0, 24.9], [1.0, 0.0], [0.0, 0.0])
    lat, slip, speed, pedal = B.cell_parts(ids[0])
    assert (B.LAT_EDGES[lat], B.SLIP_EDGES[slip], B.SPEED_EDGES[speed], B.PEDALS[pedal]) == (0.25, 0.05, 25.0, 'on')
    assert B.cell_parts(ids[1])[1:] == (0, 1, 1)           # 0.0499 is in the first slip band, 24.9 m/s in 15-25, throttle off
    assert B.cell_key(ids[0]) == "lat0.25-0.4|slip0.05-0.1|v25-35|on"
    big = B.cell_ids([5.0], [3.0], [90.0], [0.0], [1.0])
    assert B.cell_parts(big[0]) == (B.N_LAT - 1, B.N_SLIP - 1, B.N_SPEED - 1, 2)      # open top bins, braking


def test_brake_wins_over_throttle_and_nan_is_off():
    assert list(B.pedal_index([1.0, 0.0, np.nan], [0.5, 0.0, np.nan])) == [2, 1, 1]


def test_conditions_are_the_single_dimension_bins():
    m = B.condition_masks([0.3, 0.8], [0.02, 0.5], [10.0, 30.0], [1.0, 0.0], [0.0, 0.5])
    assert m['all'].all()
    assert list(m['lat0.25-0.4']) == [True, False] and list(m['slip0.4-0.7']) == [False, True]
    assert list(m['v25-35']) == [False, True] and list(m['pedal-brake']) == [False, True]


# ---------------------------------------------------------------------------------------------------- pooling

def test_weighted_quantile_and_sigma():
    assert B.wquant([1, 2, 3], [1, 1, 1], [0.5])[0] == pytest.approx(2.0)
    assert B.wquant([1, 100], [1, 9], [0.5])[0] > 50                    # the heavy value carries the median
    assert B.sigma(-1.0, 1.0) == pytest.approx(2.0 / B.SPREAD_K)


def test_pooling_counts_drives_cars_and_keeps_the_spread_across_cars():
    c = _cell(3, 0, 1, 0)
    drives = [(_rows(c, 5, trail=0.020, seed=1), 'a', 'a1', 10.0), (_rows(c, 5, trail=0.022, seed=2), 'a', 'a2', 10.0),
              (_rows(c, 5, trail=0.040, seed=3), 'b', 'b1', 10.0)]
    cells = B.pool_cells(drives)
    r = cells[c]
    assert (r['n_drives'], r['n_cars'], r['basis']) == (3, 2, 'measured')
    assert r['seconds'] == pytest.approx(15.0)
    assert r['trail_m']['p50'] == pytest.approx(0.022, abs=1e-6)
    assert r['spread_cars']['trail_m'] > 0.004                          # the cars' medians (0.021, 0.040) are far apart
    assert r['spread']['trail_m'] >= r['spread_cars']['trail_m']
    assert r['range']['trail_m'] == [pytest.approx(0.020), pytest.approx(0.040)]
    one = B.pool_cells(drives[:2])[c]
    assert one['n_cars'] == 1 and one['spread_cars']['trail_m'] is None   # one car: no spread across cars to report


def test_a_long_drive_cannot_swamp_the_cell():
    c = _cell(2, 1, 0, 1)
    long_ = _rows(c, 600, trail=0.010, seed=1)
    short = _rows(c, 30, trail=0.050, seed=2)
    capped = B.pool_cells([(long_, 'a', 'x', 10.0), (short, 'b', 'y', 10.0)], cap_s=30.0)[c]
    uncapped = B.pool_cells([(long_, 'a', 'x', 10.0), (short, 'b', 'y', 10.0)], cap_s=1e9)[c]
    assert uncapped['trail_m']['p50'] == pytest.approx(0.010, abs=1e-6)
    assert capped['trail_m']['p50'] > 0.010 + 1e-6 and capped['seconds'] == pytest.approx(630.0)


# ---------------------------------------------------------------------------------------------------- basis and empty cells

def _truth(beta, c=-0.005, p=0.040, b0=0.15):
    return c + p / (1.0 + (beta / b0) ** 2)


def _pool_with_sparse():
    """Measured cells along slip 0..5 (several lateral bands, two drives each), plus sparse ones: inside, outside, and tiny."""
    betas = [0.02, 0.07, 0.15, 0.3, 0.55]
    parts, names = [], []
    for lat in (2, 3):
        for si, b in enumerate(betas):
            for k in range(2):
                parts.append((_rows(_cell(lat, si, 1, 0), 6, trail=_truth(b), beta=b, seed=k), 'a', 'd%d' % k, 10.0))
    sparse_in = _cell(4, 2, 1, 0)                 # lateral band 4 is outside the measured 2-3: extrapolated
    sparse_ok = _cell(3, 2, 2, 0)                 # lateral and slip inside: interpolated
    tiny = _cell(3, 3, 3, 2)                      # 0.1 s: stays empty
    parts.append((_rows(sparse_in, 1.0, trail=0.0, beta=0.15), 'a', 'd0', 10.0))
    parts.append((_rows(sparse_ok, 1.0, trail=0.0, beta=0.15), 'a', 'd0', 10.0))
    parts.append((_rows(tiny, 0.1, trail=0.0, beta=0.3), 'a', 'd0', 10.0))
    return B.pool_cells(parts), sparse_in, sparse_ok, tiny


def test_the_fit_recovers_the_trail_curve():
    cells, *_ = _pool_with_sparse()
    fit = B.fit_trail(cells)
    assert fit is not None and fit['b0'] == 0.15
    assert fit['c'] == pytest.approx(-0.005, abs=1e-4) and fit['p'] == pytest.approx(0.040, abs=1e-4)
    assert fit['resid'] < 1e-4


def test_basis_labels_and_a_cell_with_too_little_data_stays_empty():
    cells, extra, inside, tiny = _pool_with_sparse()
    assert tiny not in cells                                            # under MIN_SPARSE_S: no record at all
    assert cells[extra]['basis'] is None and cells[inside]['basis'] is None
    B.apply_fit(cells, B.fit_trail(cells))
    assert cells[inside]['basis'] == 'interpolated'
    assert cells[extra]['basis'] == 'extrapolated'
    assert cells[inside]['trail_m']['p50'] == pytest.approx(_truth(0.15), abs=1e-4)
    assert cells[extra]['spread']['trail_m'] == pytest.approx(2.0 * max(B.fit_trail(cells)['resid'], B.SPREAD_FLOOR['trail_m']))
    assert cells[inside]['share']['p50'] is None                        # the share is not fitted
    measured = [c for c in cells.values() if c['basis'] == 'measured']
    assert len(measured) == 10 and all(c['n_drives'] == 2 for c in measured)


def test_a_single_drive_cell_is_not_measured_and_too_few_cells_give_no_fit():
    one = B.pool_cells([(_rows(_cell(3, 0, 1, 0), 10), 'a', 'x', 10.0)])
    assert one[_cell(3, 0, 1, 0)]['basis'] is None                      # one drive is under MIN_CELL_DRIVES
    assert B.fit_trail(one) is None
    B.apply_fit(one, None)
    assert one[_cell(3, 0, 1, 0)]['basis'] is None                      # no fit: stays empty, never guessed


# ---------------------------------------------------------------------------------------------------- scoring

def _bench_pool():
    cells, *_ = _pool_with_sparse()
    B.apply_fit(cells, B.fit_trail(cells))
    return {'cells': {B.cell_key(i): B._clean(c) for i, c in cells.items() if c['basis']}, 'conditions': {}}


def _summary(cell, seconds, trail, ratio=10.0, fy=3000.0, share=0.4):
    t = trail
    q = lambda v: [v * 0.9, v, v * 1.1]                                  # noqa: E731
    return {B.cell_key(cell): {'seconds': seconds, 'n': 100, 'torque_nm': q(t * fy / ratio), 'share': q(share),
                               'trail_m': q(t), 'nm_per_kn': q(t * 1000 / ratio)}}


def test_a_drive_on_target_scores_high_and_one_far_off_scores_low():
    pool = _bench_pool()
    c = _cell(2, 0, 1, 0)
    on = B.score_cells(_summary(c, 10, _truth(0.02)), pool)[0]
    off = B.score_cells(_summary(c, 10, _truth(0.02) + 0.05), pool)[0]
    assert on[0]['basis'] == 'measured' and on[0]['mean_abs_z'] < 0.5
    assert off[0]['z']['trail_m'] == pytest.approx(B.Z_CLIP) and off[0]['mean_abs_z'] > 3
    assert B.overall(on, [])['score'] > 90 > 40 > B.overall(off, [])['score']
    assert B.overall([], [])['score'] is None


def test_cells_without_a_target_or_with_little_time_are_not_scored():
    pool = _bench_pool()
    rows, untargeted = B.score_cells({**_summary(_cell(0, 5, 3, 2), 8, 0.03), **_summary(_cell(2, 0, 1, 0), 0.3, 0.03)}, pool)
    assert rows == [] and untargeted == [(B.cell_key(_cell(0, 5, 3, 2)), 8)]      # too short: dropped; no target: listed


def test_estimated_cells_weigh_less_and_out_of_range_is_flagged():
    pool = _bench_pool()
    inside = _cell(3, 2, 2, 0)
    r = B.score_cells(_summary(inside, 10, _truth(0.15)), pool)[0][0]
    assert r['basis'] == 'interpolated' and r['weight'] == pytest.approx(5.0)
    assert set(r['z']) == {'torque_nm', 'trail_m'}                                # no fitted share to score
    c = _cell(2, 0, 1, 0)
    far = B.score_cells(_summary(c, 10, 0.2), pool)[0][0]
    assert 'trail_m' in far['out_of_range']


def test_rates_score_in_units_of_the_spread():
    pool = {'conditions': {'all': {'oppose': {'p50': 0.05, 'spread': 0.02, 'n_drives': 5}}}}
    rows, none = B.score_rates({'all': {'oppose': [0.15, 20.0], 'zero_load': [0.2, 20.0], 'seconds': 20.0},
                                'slip>0.7': {'oppose': [0.1, 9.0]}}, pool)
    assert len(rows) == 1 and rows[0]['z'] == pytest.approx(5.0)
    assert ('all', 'zero_load') in none and ('slip>0.7', 'oppose') in none


def test_rate_pooling_needs_two_drives_and_keeps_car_spread():
    per = [{'all': {'oppose': [0.05, 30.0]}}, {'all': {'oppose': [0.07, 30.0]}}, {'all': {'oppose': [0.20, 30.0]}, 'slip>0.7': {'oppose': [0.5, 9.0]}}]
    pooled = B.pool_rates(per, ['a', 'a', 'b'])
    r = pooled['all']['oppose']
    assert r['n_drives'] == 3 and r['n_cars'] == 2 and r['spread_cars'] > 0.03
    assert 'slip>0.7' not in pooled                                             # one drive only


def test_stale_when_a_watched_file_hash_differs():
    now = B.code_hashes()
    assert set(now) == set(B.WATCHED)
    assert B.stale({'hashes': now}) == []
    other = dict(now)
    other[B.WATCHED[0]] = 'deadbeef'
    assert [s[0] for s in B.stale({'hashes': other})] == [B.WATCHED[0]]


# ---------------------------------------------------------------------------------------------------- rates of a drive

def _fake_drive(fs=200.0, seconds=60.0, share=None):
    n = int(fs * seconds)
    t = np.arange(n) / fs
    d = SimpleNamespace(t=t, fs=fs, native=None, share=np.zeros(n) if share is None else share,
                        valid=np.ones(n, bool), fyf=np.full(n, 3000.0), lat=np.full(n, 0.5), beta=np.full(n, 0.02),
                        speed=np.full(n, 20.0), thr=np.ones(n), brk=np.zeros(n))
    return d


def test_drive_rates_see_a_force_against_fy_a_zero_force_and_a_band():
    fs = 200.0
    t = np.arange(int(fs * 60)) / fs
    d = _fake_drive(fs, 60.0, share=0.5 * np.sin(2 * np.pi * 20 * t) - 0.4)       # steady -0.4 (same sign as the load: restoring? no)
    r = B.drive_rates(d)['all']
    # Fy_f > 0 so a restoring force is negative: a steady -0.4 is restoring and does not oppose
    assert r['oppose'][0] == pytest.approx(0.0, abs=0.02)
    assert r['zero_load'][0] < 0.08
    assert r['band.15_30'][0] == pytest.approx(0.5 * B.PEAK_NM / math.sqrt(2.0), rel=0.05)
    assert r['band.30_60'][0] is not None and r['band.30_60'][0] < 0.05
    against = B.drive_rates(_fake_drive(fs, 60.0, share=np.full(int(fs * 60), 0.4)))['all']
    assert against['oppose'][0] == pytest.approx(1.0, abs=0.02)
    zero = B.drive_rates(_fake_drive(fs, 60.0))['all']
    assert zero['zero_load'][0] == pytest.approx(1.0)


def test_a_band_above_the_series_nyquist_is_none():
    d = _fake_drive(fs=40.0, seconds=60.0, share=np.random.RandomState(0).randn(2400) * 0.1)
    r = B.drive_rates(d)['all']
    assert r['band.30_60'][0] is None and r['band.3_15'][0] is not None


def test_falls_are_counted_per_minute():
    fs = 200.0
    n = int(fs * 60)
    x = np.full(n, 0.5)
    for k in range(10):                                                        # ten sudden falls to zero, held
        i = int(fs * (3 + 5 * k))
        x[i:i + int(fs * 1.0)] = 0.0
    r = B.drive_rates(_fake_drive(fs, 60.0, share=x))['all']
    assert r['sudden_per_min'][0] == pytest.approx(10.0, abs=1.5)
    assert r['sustained_per_min'][0] == pytest.approx(10.0, abs=1.5)


def test_summarise_cells_groups_by_cell_and_assemble_counts_bases():
    parts = []
    for lat in (2, 3):
        for si, b in enumerate([0.02, 0.07, 0.15, 0.3, 0.55]):
            for k in range(2):
                parts.append((_rows(_cell(lat, si, 1, 0), 6, trail=_truth(b), beta=b, seed=k), 'a', 'd%d' % k, 10.0))
    used = [{'drive': 'd%d' % k, 'car': 'acr/x', 'class': 'rally2', 'rates': {}} for k in range(2)]
    drives = [(_cat([p[0] for p in parts if p[2] == 'd%d' % k]), 'acr/x', 'd%d' % k, 10.0) for k in range(2)]
    out = B.assemble(drives, used, [{'drive': 'z', 'why': 'test'}])
    c = out['pools']['rally2']['counts']
    assert c['measured'] == 10 and c['empty'] == B.N_CELLS - 10
    assert out['pools']['all']['n_cars'] == 1 and out['skipped'][0]['drive'] == 'z'
    assert "what ACR would do" in out['target'] and 'surface' in out
