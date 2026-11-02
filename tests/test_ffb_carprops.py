import json
import math
import os
import random

import pytest

from oversteer import ffb_carprops as cp, ffb_engine as fe
from oversteer.telemetry_formats import Sample

DUMP = os.path.expanduser('~/.cache/ffb-slip/dump.npz')


def car_sample(trail, scrub, rng, a=30.0, car='acr/test'):
    """A moving front axle whose game force is a * (Mz - trail Fy - scrub (Fx_FL - Fx_FR)), Mz = -0.04 Fy-ish."""
    fy = rng.uniform(-5000, 5000)
    mz = -0.04 * fy * rng.uniform(0.2, 1.0) + rng.gauss(0, 5)
    fx = (rng.uniform(-1500, 1500), rng.uniform(-1500, 1500))
    s = Sample(6000.0, 7500.0, gear=3, speed=20.0, car=car, game='acr')
    s.tyre_mz, s.tyre_fy, s.tyre_fx = (mz / 2, mz / 2, 0, 0), (fy / 2, fy / 2, 0, 0), (fx[0], fx[1], 0, 0)
    raw = a * (mz - trail * fy - scrub * (fx[0] - fx[1]))
    return s, raw


def drive(rack, trail, scrub, seconds, car='acr/test', seed=1):
    rng = random.Random(seed)
    for n in range(int(seconds * 333)):
        s, raw = car_sample(trail, scrub, rng, car=car)
        rack.feed(n / 333.0, s, raw)
    return rack


@pytest.mark.parametrize('trail,scrub', [(0.008, 0.012), (0.040, 0.030)])
def test_each_car_converges_to_its_own_trail_and_scrub(trail, scrub):
    rack = drive(fe.RackForce(), trail, scrub, 90.0)
    assert rack.trail == pytest.approx(trail, abs=0.003) and rack.scrub == pytest.approx(scrub, abs=0.003)
    assert rack.ready


def test_a_fresh_car_starts_at_the_pooled_values_and_moves_slowly():
    rack = drive(fe.RackForce(), 0.008, 0.012, 2.0)
    assert fe.CASTER_TRAIL - 0.008 > fe.CASTER_TRAIL - rack.trail >= 0          # on its way, not there
    assert fe.CASTER_TRAIL - rack.trail <= cp.TRAIL_SLEW * 2.0 + 1e-9           # the slew limit holds


def test_the_learnt_values_stay_in_the_physical_range():
    rack = drive(fe.RackForce(), -0.05, 0.2, 120.0)             # a force that wants a negative trail, a huge scrub
    assert rack.trail == 0.0 and rack.scrub == cp.SCRUB_RANGE[1]
    rack = drive(fe.RackForce(), 0.2, 0.0, 200.0)
    assert rack.trail == cp.TRAIL_RANGE[1]


def test_the_car_values_are_kept_and_come_back():
    rack = drive(fe.RackForce(), 0.040, 0.030, 60.0, car='acr/a')
    learnt = rack.trail
    rack.reset('acr/b')                                          # another car: saved on the way out
    assert rack.trail == fe.CASTER_TRAIL
    data = json.load(open(cp.STORE.path))
    assert data['acr/a']['trail'] == pytest.approx(learnt, abs=1e-4)
    cp.STORE.use(cp.STORE.path)                                  # a new run reads the file
    fresh = fe.RackForce()
    fresh.reset('acr/a')
    assert fresh.trail == pytest.approx(learnt, abs=1e-4)


def test_beamng_learns_nothing():
    assert fe.BeamNGRack().learner is None


def test_a_sample_without_front_forces_does_not_move_it():
    rack = fe.RackForce()
    s, raw = car_sample(0.008, 0.01, random.Random(2))
    s.tyre_fx = None
    for n in range(2000):
        rack.feed(n / 333.0, s, raw)
    assert rack.trail == fe.CASTER_TRAIL and rack.scrub == fe.SCRUB


@pytest.mark.skipif(not os.path.exists(DUMP), reason="marth's ACR dump is not on this machine")
def test_the_implemented_signs_match_the_regression_on_the_dump():
    np = pytest.importorskip('numpy')
    d = np.load(DUMP)
    m = (d['speed'] > 20) & (np.abs(d['ffb']) < 0.98)
    mz, fy, fx = d['mz'][m], d['fy'][m], d['fx'][m]
    X = np.c_[mz[:, 0] + mz[:, 1], fy[:, 0] + fy[:, 1], fx[:, 0] - fx[:, 1]]
    a, b, c = np.linalg.lstsq(X, d['ffb'][m], rcond=None)[0]
    assert a > 0 and -b / a > 0 and -c / a > 0                   # trail and scrub come out positive as -b/a, -c/a
    # ... which is what front_torque subtracts: its torque follows finalFF at least as well as Mz alone
    t = X[:, 0] - fe.CASTER_TRAIL * X[:, 1] - fe.SCRUB * X[:, 2]
    assert np.corrcoef(t, d['ffb'][m])[0, 1] > np.corrcoef(X[:, 0], d['ffb'][m])[0, 1] + 0.02
    assert -b / a == pytest.approx(fe.CASTER_TRAIL, abs=0.008) and -c / a == pytest.approx(fe.SCRUB, abs=0.015)
