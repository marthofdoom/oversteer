"""The A/B metrics of Countersteer FFB (oversteer/ffb_metrics.py, scripts/ffb-ab.py) on synthetic signals with known
lag, clipping and band content, and on a tiny synthetic capture written with CaptureWriter."""
import json
import math
import os
import subprocess
import sys

import numpy as np
import pytest

from oversteer import ffb_metrics as fm
from oversteer.telemetry_capture import CaptureWriter
from oversteer.telemetry_formats import decode_sample

HZ = 333.0
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')


def wander(seconds, rate=HZ, seed=3):
    """Band-limited noise (a few Hz): something with a clear cross-correlation peak."""
    rnd = np.random.default_rng(seed)
    n = int(seconds * rate)
    x = np.fft.irfft(np.fft.rfft(rnd.normal(size=n)) * (np.fft.rfftfreq(n, 1 / rate) < 4.0))
    return x / np.abs(x).max()


def test_lag_is_found_in_ms_and_signed():
    mz = wander(30)
    for ms in (0, 30, 60, -45):
        k = int(round(ms / 1000 * HZ))
        late = np.roll(mz, k)
        assert fm.lag_ms(mz, late, HZ) == pytest.approx(k / HZ * 1000, abs=1.0)
    assert fm.lag_ms(mz, np.roll(mz, 20), HZ) > 0               # a later force: positive


def test_lag_is_summed_over_the_contiguous_runs_only():
    mz = wander(30)
    late = np.roll(mz, 20)
    valid = np.ones(mz.size, dtype=bool)
    valid[5000:5200] = False
    assert fm.lag_ms(mz, late, HZ, valid) == pytest.approx(20 / HZ * 1000, abs=2.0)
    assert fm.lag_ms(mz, late, HZ, np.zeros(mz.size, dtype=bool)) is None


def test_clipping_share():
    x = np.concatenate((np.full(25, 1.0), np.full(25, -0.98), np.full(50, 0.5)))
    assert fm.clipping_share(x) == pytest.approx(0.5)
    assert fm.clipping_share(x, np.arange(100) >= 50) == 0.0
    assert fm.clipping_share([]) is None


def test_band_energy_of_a_known_sine():
    t = np.arange(0, 20, 1 / HZ)
    for hz, inside in ((12.0, True), (2.0, False), (60.0, False)):
        rms_, share = fm.band_energy(0.4 * np.sin(2 * np.pi * hz * t), HZ)
        if inside:
            assert rms_ == pytest.approx(0.4 / math.sqrt(2), rel=0.05) and share > 0.95
        else:
            assert rms_ < 0.03 and share < 0.05
    mixed = 0.5 * np.sin(2 * np.pi * 1.0 * t) + 0.5 * np.sin(2 * np.pi * 15.0 * t)
    assert fm.band_energy(mixed, HZ)[1] == pytest.approx(0.5, abs=0.03)
    assert fm.band_energy(mixed[:100], HZ) == (None, None)       # shorter than a run


def test_correlation_and_rms():
    x = wander(10)
    assert fm.correlation(x, 3 * x) == pytest.approx(1.0)
    assert fm.correlation(x, -x) == pytest.approx(-1.0)
    assert fm.correlation(x, np.ones_like(x)) is None
    assert fm.rms(np.full(10, 0.3)) == pytest.approx(0.3)


def corner_signals(seconds=40.0, light=True):
    """Mz and slip of repeated corners: slip builds past a peak and Mz falls off there; the force follows Mz
    (`light`) or stays up like a force that does not go light."""
    t = np.arange(0, seconds, 1 / HZ)
    phase = (t % 4.0) / 4.0
    slip = 0.2 * np.sin(np.pi * phase) ** 2 * 1.0 + 0.01
    slip = slip / slip.max() * 0.3
    mz = 100 * np.sin(np.pi * np.minimum(slip / 0.15, 1.0) * 0.5) * (1.0 - 0.7 * np.clip((slip - 0.15) / 0.15, 0, 1))
    # the cornering side: left then right every 4 s
    sign = np.where((t // 4) % 2 == 0, 1.0, -1.0)
    mz, slip = mz * sign, slip
    force = mz / 120.0 if light else np.sign(mz) * np.minimum(np.abs(mz) + 40, 100) / 120.0 * (np.abs(slip) > 0.02)
    return t, mz, force, slip


def test_going_light_recall_and_precision():
    t, mz, force, slip = corner_signals(light=True)
    got = fm.going_light(mz, force, slip, HZ)
    assert got['mz_events'] >= 5 and got['recall'] == pytest.approx(1.0) and got['precision'] == pytest.approx(1.0)
    flat = fm.going_light(mz, np.sign(mz) * 0.6, slip, HZ)         # a force that never goes light
    assert flat['force_events'] == 0 and flat['recall'] == 0.0 and flat['precision'] is None


def test_metrics_gathers_it_all():
    t, mz, force, slip = corner_signals()
    valid = np.ones(t.size, dtype=bool)
    out = fm.metrics(np.roll(force, 17), mz, slip, HZ, valid, ref_force=force)
    assert out['corr'] > 0.9 and out['lag_ms'] == pytest.approx(17 / HZ * 1000, abs=2.0)
    assert out['clip'] == 0.0 and out['rms_diff'] > 0
    json.dumps(out)                                              # JSON-able


def test_runs():
    assert fm.runs([0, 1, 1, 0, 1, 1, 1], 2) == [(1, 3), (4, 7)]
    assert fm.runs([1, 1, 0, 1], 2) == [(0, 2)]
    assert fm.runs([], 1) == []


def packet(i, ffb, mz, slip=0.05, speed_ok=True, clock=None):
    from tests.test_telemetry import _ovst5
    return _ovst5(clock=i / HZ + 5.0 if clock is None else clock, packet_id=1000 + i, ffb=ffb,
                  mz=(mz / 2, mz / 2, 0.1, 0.1), angle=(slip, slip, 0.0, 0.0))


def write_capture(path, seconds=40.0, ours_better=True, tuning=True):
    """A tiny ACR v5 capture: steering weaving with Mz = 300 sin, finalFF = Mz / 582 (clipped)."""
    with CaptureWriter(str(path), extra={'ffb_tuning': True} if tuning else None, full_rate=tuning) as w:
        for i in range(int(seconds * HZ)):
            t = i / HZ
            mz = 300.0 * math.sin(2 * math.pi * 0.3 * t) + 4.0 * math.sin(2 * math.pi * 12.0 * t)
            w.write(100.0 + t, ('127.0.0.1', 0), packet(i, max(-1.0, min(1.0, mz / 582.0 * 1.2)), mz))


def test_decode_of_the_synthetic_packet():
    s = decode_sample(packet(0, 0.25, 100.0))
    assert s.game == 'acr' and s.game_ffb == pytest.approx(0.25) and sum(s.tyre_mz[:2]) == pytest.approx(100.0)


def load_items(path):
    sys.path.insert(0, ROOT)
    from oversteer.telemetry_capture import read_capture
    meta, records = read_capture(str(path))
    return meta, [(t, decode_sample(d)) for t, _a, d in records]


def test_analyse_a_synthetic_capture(tmp_path):
    path = tmp_path / 'tune.ovcap.gz'
    write_capture(path)
    meta, items = load_items(path)
    assert meta['ffb_tuning'] is True
    result = fm.analyse(items, sections=2, texture_level=0.0)
    assert 'error' not in result
    assert result['meta']['capture_rate_hz'] == pytest.approx(HZ, rel=0.02) and not result['meta']['low_rate']
    assert result['meta']['ready_share'] > 0.5
    # finalFF is Mz / 582: the game follows Mz closely and with no lag; ours is the same signal scaled
    assert result['game']['corr'] > 0.99 and result['ours']['corr'] > 0.99
    assert abs(result['game']['lag_ms']) < 6 and abs(result['ours']['lag_ms']) < 6
    assert result['game']['clip'] == 0.0 and result['ours']['rms_diff'] < 0.1
    assert result['ours']['band_rms'] is not None and result['game']['band_rms'] is not None
    assert len(result['sections']) >= 1 and result['sections'][0]['label'].startswith('section')
    text = fm.table(result, 'x')
    assert 'corr with front Mz' in text and 'game' in text and 'ours' in text
    json.dumps(result)


def test_analyse_without_finalff_says_so():
    from oversteer.telemetry_formats import Sample
    items = [(i / 60.0, Sample(5000.0, 7000.0, speed=20.0, game='acr')) for i in range(100)]
    assert 'error' in fm.analyse(items)
    assert 'finalFF' in fm.table(fm.analyse(items))


def test_a_thinned_capture_is_flagged(tmp_path):
    path = tmp_path / 'thin.ovcap.gz'
    with CaptureWriter(str(path)) as w:
        for i in range(int(30 * 60)):
            t = i / 60.0
            mz = 300.0 * math.sin(2 * math.pi * 0.3 * t)
            w.write(100.0 + t, ('127.0.0.1', 0), packet(i, mz / 582.0, mz, clock=i / 60.0 + 5.0))
    _meta, items = load_items(path)
    result = fm.analyse(items, texture_level=0.0)
    assert result['meta']['low_rate'] and 'WARNING' in fm.table(result)


def script(*args, home=None):
    env = dict(os.environ, XDG_CACHE_HOME=str(home / '.cache')) if home else None
    return subprocess.run([sys.executable, os.path.join(ROOT, 'scripts', 'ffb-ab.py')] + [str(a) for a in args],
                          capture_output=True, text=True, env=env)


def test_script_table_and_json(tmp_path):
    path = tmp_path / 'tune.ovcap.gz'
    write_capture(path, seconds=15.0)
    out = script(path, '--texture-level', '0')
    assert out.returncode == 0 and 'corr with front Mz' in out.stdout
    out = script(path, '--json', '--texture-level', '0')
    data = json.loads(out.stdout)
    assert data['ffb_tuning'] is True and data['game']['corr'] > 0.99
    assert script(tmp_path / 'missing.ovcap.gz').returncode != 0


def test_blind_order_key_and_reveal(tmp_path):
    out = script('--blind', '--seed', '5', home=tmp_path)
    assert out.returncode == 0
    keys = list((tmp_path / '.cache' / 'ffb-ab').glob('ab-*.json'))
    assert len(keys) == 1
    key = json.loads(keys[0].read_text())
    assert sorted(r['switch'] for r in key['runs']) == ['off', 'on'] and str(keys[0]) in out.stdout
    for r in key['runs']:
        assert 'run %d: Countersteer FFB switch %s' % (r['run'], r['switch'].upper()) in out.stdout
    again = script('--reveal', keys[0], home=tmp_path)
    assert again.returncode == 0 and 'switch ' + key['runs'][0]['switch'] in again.stdout
    assert script('--reveal', keys[0], tmp_path / 'one.ovcap.gz', home=tmp_path).returncode == 2
