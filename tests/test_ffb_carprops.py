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


def straight(rack, seconds, a=30.0):
    """A straight: no side force or Fx couple worth the name; the game's force is only a * Mz."""
    rng = random.Random(5)
    t0 = rack._last or 0.0
    for n in range(int(seconds * 333)):
        s = Sample(6000.0, 7500.0, gear=3, speed=30.0, car=rack.car, game='acr')
        mz = rng.gauss(0, 3)
        s.tyre_mz, s.tyre_fy, s.tyre_fx = (mz / 2, mz / 2, 0, 0), (rng.gauss(0, 100), 0, 0, 0), (rng.gauss(0, 50), 0, 0, 0)
        rack.feed(t0 + (n + 1) / 333.0, s, a * mz)


def test_a_long_straight_keeps_what_was_learnt():
    rack = drive(fe.RackForce(), 0.008, 0.012, 90.0)
    trail, scrub = rack.trail, rack.scrub
    straight(rack, 120.0)
    assert rack.trail == pytest.approx(trail, abs=0.0005) and rack.scrub == pytest.approx(scrub, abs=0.0005)


def test_the_strength_fit_is_not_thrown_by_the_trail_stepping():
    rack = drive(fe.RackForce(), 0.008, 0.012, 90.0)           # the trail walks from 27 to 8 mm on the way
    assert rack.scale == pytest.approx(30.0, rel=0.07)         # (1 mm/s against a 10 s window: harmless; the rest
    # is the ridge's 1.6 mm of trail still to come, which moves the ratio fit by 5 %)


# -- the assist detector on the verification report's curve set --

def _detector_ratio(curve, p99=43.0, seconds=300.0, hz=50.0):
    """The detector on a column-torque -> driver-torque `curve`, torque uniform in log between 0.05 and 1.5 of p99 (time
    weighted), the game's force = the driver's torque scaled by 30 (the gain is what the detector sees)."""
    det = cp.AssistDetector()
    lo, hi = math.log(0.05), math.log(1.5)
    for i in range(int(seconds * hz)):
        x = p99 * math.exp(lo + (hi - lo) * ((i * 0.6180339887) % 1.0))
        det.add(x, 30.0 * curve(x), p99, 1.0 / hz)
    return det


def _lin_db(gain, dead, amax=1e9):
    """T_c = T_d + min(gain max(T_d - dead, 0), amax), inverted by bisection."""
    def f(tc):
        lo, hi = 0.0, tc
        for _ in range(60):
            m = 0.5 * (lo + hi)
            lo, hi = (m, hi) if m + min(gain * max(m - dead, 0.0), amax) < tc else (lo, m)
        return lo
    return f


def _hps(c, relief=1e9):
    def f(tc):
        lo, hi = 0.0, tc
        for _ in range(60):
            m = 0.5 * (lo + hi)
            lo, hi = (m, hi) if m + min(c * m ** 2, relief) < tc else (lo, m)
        return lo
    return f


ASSIST_CURVES = {
    'straight boost, 1.0 Nm dead band': _lin_db(5.0, 1.0),
    'straight boost, 0.3 Nm dead band': _lin_db(5.0, 0.3),
    'saturating EPS': _lin_db(10.0, 1.0, 30.0),
    'hydraulic': _hps(2.0),
    'hydraulic with relief': _hps(2.0, 25.0),
    'our boost curve at 100 km/h': lambda tc: cp.driver_from_column(tc, 27.8),
}
UNASSISTED_CURVES = {
    'unassisted': lambda tc: tc,
    'unassisted with tyre fall-off': lambda tc: tc * (1 - 0.2 * (tc / 43.0) ** 2),
    'unassisted with 15 % model error': lambda tc: tc / (1 + 0.15 * min(tc / 43.0, 1.5)),
}


@pytest.mark.parametrize('name', sorted(ASSIST_CURVES))
def test_the_detector_calls_every_assist_curve_of_the_report_present(name):
    det = _detector_ratio(ASSIST_CURVES[name])
    assert det.ratio() < cp.AssistDetector.ASSIST_BELOW and det.state is True, det.ratio()


@pytest.mark.parametrize('name', sorted(UNASSISTED_CURVES))
def test_the_detector_calls_every_unassisted_curve_of_the_report_absent(name):
    det = _detector_ratio(UNASSISTED_CURVES[name])
    assert det.ratio() > cp.AssistDetector.ABSENT_ABOVE and det.state is False, det.ratio()


# -- the steering ratio from telemetry --

def test_the_table_ratio_is_the_wheel_lock_over_the_road_wheel_lock_of_a_0_13_m_arm():
    entry = {'steering_wheel_lock_deg': 270.0, 'rack_travel_m': 0.042}
    assert cp.table_ratio(entry) == pytest.approx(270.0 / math.degrees(math.asin(0.042 / 0.13)))
    assert 14.0 < cp.table_ratio(entry) < 14.7
    assert cp.table_ratio(entry, arm=0.15) > cp.table_ratio(entry)             # a longer arm: less road-wheel lock ... more ratio
    assert cp.table_ratio({}) is None and cp.table_ratio(None) is None
    assert cp.table_ratio({'steering_wheel_lock_deg': 270.0, 'rack_travel_m': 0.2}) is None     # travel beyond the arm


def _drive_ratio(learner, lock_rad, seconds=60.0, speed=20.0, hz=100.0):
    rng = random.Random(4)
    for i in range(int(seconds * hz)):
        steer = 0.4 * math.sin(i / hz * 1.3) + rng.gauss(0, 0.01)
        yaw = speed * steer * lock_rad / cp.WHEELBASE            # Ackermann, as the learner assumes
        learner.add(steer, yaw, speed, 1.0 / hz)
    return learner


def test_the_ratio_is_learnt_from_the_road_wheel_angle():
    lock = math.radians(18.8)
    learner = _drive_ratio(cp.RatioLearner(), lock, speed=8.0)
    assert learner.lock() == pytest.approx(lock, rel=0.02)
    assert learner.ratio(270.0) == pytest.approx(270.0 / 18.8, rel=0.02)
    assert learner.ratio(None) is None


def test_the_ratio_waits_for_evidence_and_ignores_hard_cornering_and_standing():
    learner = cp.RatioLearner()
    assert learner.lock() is None
    for _ in range(3000):
        learner.add(0.5, 0.9, 3.0, 0.01)                 # slow
        learner.add(0.5, 0.9, 30.0, 0.01)                # 27 m/s^2 lateral
    assert learner.lock() is None


def test_the_ratio_ignores_understeer_and_driven_wheels():
    # gravel: the car does not turn as steered (front slip angle) and the driven wheels spin: no Ackermann angle
    learner = cp.RatioLearner()
    for i in range(6000):
        steer = 0.3 * math.sin(i / 100.0 * 1.3)
        learner.add(steer, 0.02 * steer, 20.0, 0.01, alpha=0.08, wheels=(20.0, 20.0, 20.0, 20.0))        # understeer
        learner.add(steer, 0.02 * steer, 20.0, 0.01, alpha=0.01, wheels=(20.0, 20.0, 24.0, 24.0))        # wheelspin
    assert learner.sxx == 0.0 and learner.lock() is None
    lock = 0.4
    for i in range(6000):
        steer = 0.08 * math.sin(i / 100.0 * 1.3)
        learner.add(steer, 8.0 * steer * lock / cp.WHEELBASE, 8.0, 0.01, alpha=0.01, wheels=(8.0, 8.0, 8.1, 7.9))
    assert learner.lock() == pytest.approx(lock, rel=0.02)


def test_a_lock_outside_the_physical_range_is_not_believed_or_loaded(tmp_path):
    learner = cp.RatioLearner()
    _drive_ratio(learner, 0.0422)                                       # what gravel understeer once gave
    assert learner.lock() is None
    path = tmp_path / 'props.json'
    path.write_text(json.dumps({'acr/Skoda Fabia RS Rally2': {'trail': 0.05, 'scrub': 0.03, 'lock': 0.0422},
                                'acr/other': {'trail': 0.03, 'scrub': 0.02, 'lock': 0.45}}))
    cp.STORE.use(str(path))
    assert cp.STORE.get_lock('acr/Skoda Fabia RS Rally2') is None       # discarded: relearnt
    assert cp.STORE.get_lock('acr/other') == 0.45
    assert json.loads(path.read_text())['acr/Skoda Fabia RS Rally2']['lock'] == 0.0422      # the file is left alone


def test_the_engine_learns_the_lock_and_the_ratio_and_keeps_them(tmp_path):
    cp.STORE.use(str(tmp_path / 'props.json'))
    cp.ASYNC = False
    entry = {'steering_wheel_lock_deg': 270.0, 'rack_travel_m': 0.042}
    from oversteer import car_data
    car_data.set_cars({'rbr/test': entry})
    try:
        rack = fe.RackForce()
        rack.reset('rbr/test')
        assert rack.ratio == pytest.approx(cp.table_ratio(entry))          # the table until it is learnt
        true_lock = math.radians(22.0)                                      # != the table's 18.8 deg
        rng = random.Random(2)
        for n in range(int(90 * 100)):
            steer = 0.08 * math.sin(n / 100.0 * 1.3) + rng.gauss(0, 0.002)    # gentle: under the learner's lateral limit
            s = Sample(6000.0, 7500.0, gear=3, speed=12.0, car='rbr/test', game='rbr')
            s.steer, s.yaw_rate = steer, 12.0 * steer * true_lock / cp.WHEELBASE
            s.tyre_mz, s.tyre_fy, s.tyre_fx = (5.0, 5.0, 0, 0), (500.0, 500.0, 0, 0), (0, 0, 0, 0)
            rack.feed(n / 100.0, s, 1000.0)
        assert rack.lock == pytest.approx(true_lock, rel=0.03)
        assert rack.ratio == pytest.approx(270.0 / 22.0, rel=0.05)          # slewed from 14.4 toward 12.3
        rack.reset('other')                                                 # saved on the way out
        cp.STORE.use(cp.STORE.path)
        assert cp.STORE.get_lock('rbr/test') == pytest.approx(true_lock, rel=0.03)
        assert cp.STORE.get_rack('rbr/test')[0] == pytest.approx(270.0 / 22.0, rel=0.05)
    finally:
        car_data.set_cars(None)
