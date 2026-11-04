"""The FFB parity report (scripts/ffb_parity.py, scripts/ffb-parity.py): band energy, falls, steps, flags, stale-reference
and too-little-data logic on small synthetic signals; no capture is read."""
import importlib.util
import os

import numpy as np
import pytest

_HERE = os.path.dirname(__file__)


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, '..', 'scripts', file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = _load('ffb_parity', 'ffb_parity.py')
pytestmark = pytest.mark.skipif(P.MISSING is not None, reason="numpy/scipy missing")


def test_band_rms_puts_a_sine_in_its_band():
    fs = 400.0
    t = np.arange(0, 20, 1 / fs)
    x = 0.5 * np.sin(2 * np.pi * 20 * t)
    bands, secs = P.band_rms(x, np.ones(len(t), bool), fs, ((3, 15), (15, 30), (30, 60)))
    assert secs == pytest.approx(20.0)
    assert bands[1] == pytest.approx(0.5 / np.sqrt(2), rel=0.03)
    assert bands[0] < 0.01 and bands[2] < 0.01


def test_band_rms_without_a_long_enough_stretch_is_none():
    fs = 200.0
    mask = np.zeros(2000, bool)
    mask[100:160] = True                              # 0.3 s: shorter than a segment
    bands, secs = P.band_rms(np.random.RandomState(1).randn(2000), mask, fs, ((3, 15),))
    assert bands == [None] and secs == 0.0


def test_band_stats_needs_seconds_of_data():
    v = np.linspace(0, 1, 1000)
    dt = np.full(1000, 0.004)                          # 4 s in all
    full = np.ones(1000, bool)
    assert P.band_stats(v, full, dt, min_s=3.0)[0] == pytest.approx(0.5, abs=0.01)
    assert P.band_stats(v, full, dt, min_s=5.0) is None
    few = np.zeros(1000, bool)
    few[:10] = True
    assert P.band_stats(v, few, np.full(1000, 1.0), min_s=1.0) is None      # seconds enough, samples not


def _force(fs, drops):
    """0.5 FS with a fall to `low` at each (second, low, hold seconds): a 40 ms ramp down, a 20 ms ramp back after."""
    n = int(30 * fs)
    x = np.full(n, 0.5)
    for sec, low, hold in drops:
        a = int(sec * fs)
        ramp = int(0.04 * fs)
        x[a:a + ramp] = np.linspace(0.5, low, ramp)
        x[a + ramp:a + ramp + int(hold * fs)] = low
        b = a + ramp + int(hold * fs)
        x[b:b + int(0.02 * fs)] = np.linspace(low, 0.5, int(0.02 * fs))
    return x


def test_falls_found_and_sustained_told_apart():
    fs = 250.0
    x = _force(fs, [(5, 0.05, 0.05), (10, 0.05, 0.6), (15, 0.40, 0.3)])      # the last drops under .12 FS: no fall
    falls = P.find_falls(x, np.ones(len(x), bool), fs)
    assert len(falls) == 2
    assert [f[2] for f in falls] == [False, True]
    assert falls[0][0] / fs == pytest.approx(5.0, abs=0.1)


def test_falls_respect_the_mask_and_the_start_level():
    fs = 250.0
    x = _force(fs, [(5, 0.05, 0.3)])
    assert P.find_falls(x, np.zeros(len(x), bool), fs) == []
    assert P.find_falls(x * 0.2, np.ones(len(x), bool), fs) == []             # starts under .15 FS


def test_falls_cross_zero_is_visible_in_the_signs():
    fs = 250.0
    x = np.full(int(10 * fs), 0.5)
    x[int(5 * fs):] = -0.1                               # a step through zero
    (s, j, _u), = P.find_falls(x, np.ones(len(x), bool), fs)
    assert np.sign(x[s]) != np.sign(x[j])


def test_steps_counted_once_per_edge():
    fs = 500.0
    x = np.zeros(5000)
    for a in (500, 1500, 2500):
        x[a:a + 100] = 0.3                              # up and down: two edges each, close to 25 ms apart in time
    m = np.ones(5000, bool)
    assert P.count_steps(x, m, fs, 0.2) == 6
    assert P.count_steps(x, m, fs, 0.4) == 0
    assert P.count_steps(x, np.zeros(5000, bool), fs, 0.2) == 0


def _result(values):
    r = P.Result()
    for k, v in values.items():
        r.add(k, k, 'FS', 'R', v)
    return r


def test_flags_mark_what_is_more_than_25_percent_off():
    r = _result({'a': 0.30, 'b': 0.50, 'c': 1.0, 'd': None, 'time.x': 5.0, 'fit.q': 9.0})
    ref = _result({'a': 0.50, 'b': 0.45, 'c': 1.0, 'd': 0.2, 'time.x': 90.0, 'fit.q': 1.0})
    flags = P.flag_measures(r, ref)
    assert [f[0] for f in flags] == ['a']              # b is 11 % off, d has no number, time. and fit. are not compared
    assert flags[0][3] == pytest.approx(-0.4)


def test_flags_ignore_a_pair_of_zeros_and_skip_nm_when_asked():
    r = P.Result()
    r.add('x', 'x', 'FS', 'R', 0.0)
    r.add('force.p50_nm', 'n', 'Nm', 'E', 1.0)
    ref = P.Result()
    ref.add('x', 'x', 'FS', 'R', 0.0)
    ref.add('force.p50_nm', 'n', 'Nm', 'E', 2.0)
    assert [f[0] for f in P.flag_measures(r, ref)] == ['force.p50_nm']
    assert P.flag_measures(r, ref, skip_nm=True) == []


def test_no_number_for_a_value_that_is_not_finite_or_missing():
    r = P.Result()
    r.add('x', 'x', 'FS', 'R', float('nan'))
    r.add('y', 'y', 'FS', 'R', None, "no ratio in the header")
    assert r.value('x') is None and r.items['x']['why']
    assert r.items['y']['why'] == "no ratio in the header"


def test_reference_stale_names_the_fields():
    a = {'build': 'abc', 'wheel_peak': None, 'ratio': 13.0, 'power_class': 'rally', 'column_damping': 14.0,
         'mapping': 'knee', 'knee': 50.0, 'damping_written': 511.0}
    assert P.stale_fields(a, dict(a)) == []
    b = dict(a, ratio=9.7, build='def')
    assert [f[0] for f in P.stale_fields(a, b)] == ['build', 'ratio']
    old = {k: None for k in a}                           # a capture with no settings header: unknown, so not current
    fields = [f[0] for f in P.stale_fields(a, old)]
    assert 'build' in fields and 'ratio' in fields and 'wheel_peak' not in fields


class _D:
    """A synthetic drive with what measure() and steering_ratio() read."""


def _drive(seconds=80.0, fs=250.0, gain=0.3, wheel_gain=270.0, bend=0.0, header=None):
    d = _D()
    n = int(seconds * fs)
    d.t = np.arange(n) / fs
    d.fs = fs
    d.dt = np.full(n, 1 / fs)
    d.speed = np.full(n, 25.0)
    d.rpm = np.full(n, 3000.0)
    d.throttle = d.brake = np.zeros(n)
    steer = 0.4 * np.sin(2 * np.pi * 0.25 * d.t)
    d.steer = steer
    d.ay = 8.0 * steer
    d.yaw = 0.3 * steer
    d.vx, d.vy = np.full(n, 25.0), 0.1 * steer
    d.beta = np.abs(np.arctan2(d.vy, d.vx))
    d.lFL = d.lFR = np.full(n, 3000.0)
    d.svFL = d.svFR = np.zeros(n)
    d.fyf = 700.0 * d.ay                                 # N, left positive
    d.playing = np.ones(n, bool)
    d.drive = d.playing & (d.speed > 10)
    d.rec = -gain * np.sin(2 * np.pi * 0.25 * d.t) * 1.0 + 0.0
    d.wheel_deg = wheel_gain * steer * (1 + bend * np.abs(steer))
    d.wheel_range = 540.0
    d.peak, d.peak_source, d.mass, d.mass_known = 3.5, 'assumed', 1390.0, True
    d.ch = {}
    d.ext = np.zeros((10, 2))
    d.header = header or {}
    d.name = 'synthetic'
    return d


def test_measure_reads_force_against_lateral_load():
    d = _drive()
    r = P.measure(d)
    assert r.value('time.driving_s') == pytest.approx(80.0, rel=0.01)
    assert r.value('load.rim_nm_per_kn') > 0.05           # restoring: the force is opposite Fy_f
    assert r.value('oppose.corner') < 0.02
    r2 = P.measure(_drive(gain=0.6))
    assert r2.value('load.rim_nm_per_kn') == pytest.approx(2 * r.value('load.rim_nm_per_kn'), rel=0.05)
    assert r2.value('grip.force_fs') == pytest.approx(2 * r.value('grip.force_fs'), rel=0.05)


def test_measure_counts_a_force_pushing_with_the_lateral_force():
    d = _drive()
    d.rec = -d.rec
    r = P.measure(d)
    assert r.value('oppose.corner') > 0.95


def test_a_short_drive_gets_no_numbers():
    r = P.measure(_drive(seconds=10.0))
    assert [k for k, it in r.items.items() if it['value'] is not None] == ['time.driving_s']


def test_bands_with_too_little_data_say_so_not_a_number():
    d = _drive()
    d.speed[:] = 25.0
    d.drive[:] = False
    d.drive[:int(40 * d.fs)] = True                       # 40 s: enough to measure, but...
    d.beta[:] = 0.5                                       # never in grip
    r = P.measure(d)
    assert r.value('force.grip_ay2_5.p50_fs') is None
    assert r.items['force.grip_ay2_5.p50_fs']['why'] == "too little data"
    assert r.value('force.slip0.4_0.7.p50_fs') is not None


def test_effective_trail_needs_the_recorded_ratio():
    d = _drive(header={'settings': {'learnt': {'ratio': 13.0}}})
    r = P.measure(d)
    assert r.value('load.eff_trail_mm') == pytest.approx(r.value('load.rim_nm_per_kn') * 169.0)
    assert P.measure(_drive()).value('load.eff_trail_mm') is None


def test_steering_ratio_flat_and_not_flat():
    flat, why = P.steering_ratio(_drive())
    assert why is None
    assert flat['whole'] == pytest.approx(270.0, rel=0.01)
    assert all(abs(dev) < P.FLAT_TOL for key in ('speed', 'angle') for _n, g, dev in flat[key] if dev is not None)
    bent, _ = P.steering_ratio(_drive(bend=1.0))          # the wheel turns up to 40 % more at large angles
    devs = [dev for _n, g, dev in bent['angle'] if dev is not None]
    assert max(abs(x) for x in devs) > P.FLAT_TOL
    d = _drive()
    d.wheel_deg = None
    assert P.steering_ratio(d)[0] is None


def test_the_spring_follows_target_gain_and_limit():
    d = _drive(seconds=20.0)
    n = len(d.t)
    # target 0, k 1000 uFS/deg, tau 500 (0.05 FS): the spring is -k * deg until it clips
    d.ch['ffb_align'] = np.array([[0.0, 0, 1000, 500, 0, 0]] * 6)
    d.wheel_deg = np.linspace(-100, 100, n)
    s = P._spring(d)
    assert s[n // 2] == pytest.approx(0.0, abs=1e-3)
    assert s[-1] == pytest.approx(0.05) and s[0] == pytest.approx(-0.05)
    d.wheel_deg = None
    assert P._spring(d) is None


def test_run_fails_clearly_when_scipy_is_missing(monkeypatch):
    cli = _load('ffb_parity_cli', os.path.join('ffb-parity.py'))
    monkeypatch.setattr(cli.fp, 'MISSING', "ffb-parity needs numpy and scipy (test)")
    assert cli.main() == 3
