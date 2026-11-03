"""The FFB scorer (scripts/ffb_score.py, scripts/ffb-score.py): metrics on a small synthetic drive (finite, the same
twice, the four sections present, a mirrored drive mirrors its signs) and, skipped when the dump is absent, on a
slice of marth's ACR dump."""
import importlib.util
import json
import math
import os

import numpy as np
import pytest

from oversteer.telemetry_formats import Sample

_HERE = os.path.dirname(__file__)


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, '..', 'scripts', file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


S = _load('ffb_score', 'ffb_score.py')
needs_dump = pytest.mark.skipif(not os.path.exists(S.DUMP), reason="marth's ACR dump is not on this machine")


def synthetic(seconds=24.0, hz=333.0, mirror=1.0):
    """A slalom at 25 m/s: Mz and Fy follow the steering, the game's force is Mz / 582 (finalFF), a stop at the end."""
    items = []
    for i in range(int(seconds * hz)):
        t = i / hz
        phase = math.sin(2 * math.pi * 0.25 * t)
        speed = 25.0 if t < seconds - 3.0 else max(25.0 * (seconds - t) / 3.0, 0.0)
        mz = -mirror * 130.0 * phase
        fy = mirror * 2600.0 * phase
        s = Sample(6000.0, 7500.0, gear=3, speed=speed, car='acr/synthetic', game='acr')
        s.steer = mirror * 0.3 * phase
        s.game_ffb = (mz - 0.027 * fy) / 582.0
        s.tyre_mz = (mz / 2, mz / 2, 0.0, 0.0)
        s.tyre_fy = (fy / 2, fy / 2, fy / 2, fy / 2)
        s.tyre_fx = (0.0,) * 4
        alpha = mirror * 0.05 * phase
        s.tyre_slip_angle = (alpha, alpha, alpha, alpha)
        s.tyre_slip_ratio = (0.0,) * 4
        s.tyre_load = (3500.0,) * 4
        s.brake, s.throttle = 0.0, 0.5
        s.vel = (speed, 0.0, 0.0)
        s.accel = (0.0, mirror * 9.0 * phase, 0.0)
        s.yaw_rate = mirror * 0.4 * phase
        items.append((t, s))
    return S.ListDrive(items, 'synthetic', 'synthetic')


def all_numbers(d):
    return list(S.flatten(d).values())


def test_synthetic_drive_scores_every_section_with_finite_numbers():
    r = S.score(S.ListDrive(synthetic().items, 'synthetic', 'synthetic'), events=3)
    assert r['ready_share'] > 0.5
    assert r['real_rate_hz'] == pytest.approx(333.0, rel=0.02)
    assert all(math.isfinite(v) for v in all_numbers(r))
    c = r['physics']['consistency']
    assert c['rms_fs'] < 1e-9 and c['rows'] > 1000                  # exact unless gated
    left, right = r['physics']['cases']['cornering_left'], r['physics']['cases']['cornering_right']
    assert left['sign_agree'] > 0.95 and right['sign_agree'] > 0.95
    assert r['physics']['lag_ms'] is not None                       # full rate: the lag is judged
    assert r['wheel_fit']['tracking_rms_fs'] < 0.02 and r['wheel_fit']['delay_ms'] >= 4.0
    assert r['safety']['thermal_ratio'] < 1.0 and r['safety']['standing_rows'] >= 0
    assert 'sign_reversals_per_s' in r['safety'] and r['safety']['slew_violation_share'] == 0.0


def test_the_same_drive_scores_the_same_twice():
    a = S.score(synthetic(12.0), events=2)
    b = S.score(synthetic(12.0), events=2)
    assert a == b


def test_a_mirrored_drive_mirrors_the_signs_and_keeps_the_magnitudes():
    a = S.score(synthetic(16.0), events=2)
    b = S.score(synthetic(16.0, mirror=-1.0), events=2)
    ca, cb = a['physics']['cases'], b['physics']['cases']
    assert ca['cornering_left']['rows'] == cb['cornering_right']['rows']
    assert a['safety']['thermal_mean_square_max'] == pytest.approx(b['safety']['thermal_mean_square_max'], rel=0.05)


def test_report_compare_and_json_round_trip():
    r = S.score(synthetic(10.0), events=2)
    assert json.loads(json.dumps(r)) == r
    text = S.report(r)
    for word in ('PHYSICS', 'WHEEL', 'DYNAMICS', 'SAFETY'):
        assert word in text
    other = json.loads(json.dumps(r))
    other['safety']['clip_share'] = 0.5
    table = S.compare(r, other, 'a', 'b')
    assert 'safety.clip_share' in table and 'unchanged' in table


def test_a_drive_without_front_forces_is_noted_not_a_crash():
    items = [(i / 100.0, Sample(6000.0, 7500.0, speed=20.0, car='acr/x', game='acr')) for i in range(400)]
    for _t, s in items:
        s.game_ffb = 0.1
    r = S.score(S.ListDrive(items, 'bare', 'capture'), events=1)
    assert r['ready_share'] == 0.0
    assert any('never ready' in n for n in r['notes'])
    assert S.report(r)


@needs_dump
def test_dump_slice_scores_and_is_stable():
    a = S.score(S.DumpDrive(limit=2500), hz=20.0, events=3)
    b = S.score(S.DumpDrive(limit=2500), hz=20.0, events=3)
    assert a == b
    assert all(math.isfinite(v) for v in all_numbers(a))
    assert a['physics']['lag_ms'] is None and 'lag_note' in a['physics']       # 4 Hz snapshots: no timing
    assert 'sign_reversals_per_s' not in a['safety'] and 'rate_note' in a['safety']
    assert a['physics']['cases']['all_moving']['sign_agree'] > 0.9
