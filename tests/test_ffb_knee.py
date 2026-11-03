"""The 1:1 mapping with a knee (wheel_torque.rim_share), the hydraulic top end (ffb_carprops) and the gated kickback
(ffb_engine.Kickback). Nothing here touches a real wheel or /sys."""
import math
import random

import pytest

from oversteer import ffb_carprops as cp, ffb_engine as fe, wheel_torque as wt
from oversteer.telemetry_formats import Sample

NM = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 3.7)


def test_the_knee_plays_1_to_1_and_compresses_above_it():
    assert wt._gamma(0.5, 3.7, 2.1) == pytest.approx(0.263, abs=0.002)
    got = [wt.rim_share(x, 2.1, 1.0, 3.7, 0.5) for x in NM]
    assert got == pytest.approx([0.238, 0.476, 0.590, 0.638, 0.677, 0.710, 0.739, 0.750], abs=0.003)
    assert wt._gamma(0.4, 3.7, 2.1) == pytest.approx(0.356, abs=0.002)
    got = [wt.rim_share(x, 2.1, 1.0, 3.7, 0.4) for x in NM]
    assert got == pytest.approx([0.238, 0.455, 0.543, 0.602, 0.652, 0.696, 0.735, 0.750], abs=0.003)


def test_below_the_knee_it_is_a_straight_line_and_the_slope_is_continuous_at_it():
    for x in (0.0, 0.3, 1.0, 1.05):
        assert wt.rim_share(x, 2.1, 1.0, 3.7, 0.5) == pytest.approx(x / 2.1)
    eps = 1e-6
    below = wt.rim_share(1.05 - eps, 2.1, 1.0, 3.7, 0.5)
    above = wt.rim_share(1.05 + eps, 2.1, 1.0, 3.7, 0.5)
    assert (above - below) / (2 * eps) == pytest.approx(1.0 / 2.1, rel=1e-3)         # C1 at the knee
    assert wt.rim_share_slope(1.0, 2.1, 1.0, 3.7, 0.5) == 1.0
    assert wt.rim_share_slope(1.05 + eps, 2.1, 1.0, 3.7, 0.5) == pytest.approx(1.0, rel=1e-3)


def test_monotone_odd_clamped_and_the_slope_is_the_derivative():
    xs = [i * 0.01 for i in range(0, 1200)]
    shares = [wt.rim_share(x, 2.1, 1.0, 3.7, 0.5) for x in xs]
    assert all(b >= a for a, b in zip(shares, shares[1:])) and max(shares) == wt.TOP
    for x in (0.2, 1.4, 2.6, 3.4):
        assert wt.rim_share(-x, 2.1, 1.0, 3.7, 0.5) == -wt.rim_share(x, 2.1, 1.0, 3.7, 0.5)
        h = 1e-5
        d = (wt.rim_share(x + h, 2.1, 1.0, 3.7, 0.5) - wt.rim_share(x - h, 2.1, 1.0, 3.7, 0.5)) / (2 * h) * 2.1
        assert wt.rim_share_slope(x, 2.1, 1.0, 3.7, 0.5) == pytest.approx(d, rel=1e-3)
    assert wt.rim_share_slope(5.0, 2.1, 1.0, 3.7, 0.5) == 0.0


def test_a_car_within_the_budget_is_played_1_to_1_clamped_and_the_strength_acts_before_the_mapping():
    assert wt._gamma(0.5, None, 2.1) == 1.0 and wt._gamma(0.5, 1.5, 2.1) == 1.0
    assert wt.rim_share(1.2, 2.1, 1.0, 1.5, 0.5) == pytest.approx(1.2 / 2.1)
    assert wt.rim_share(9.0, 2.1) == wt.TOP
    half = wt.rim_share(1.5, 2.1, 0.5, 3.7, 0.5)
    assert half == pytest.approx(wt.rim_share(0.75, 2.1, 1.0, 3.7, 0.5))
    assert wt.rim_share(1.0, 2.1, 0.0, 3.7) == 0.0


def test_the_hydraulic_top_end():
    got = [cp.driver_from_column(x, 25.0, 0.0) for x in (2.0, 3.0)]
    assert got == [2.0, 3.0]                                                       # unassisted: untouched
    for x, want in ((2.0, 1.91), (3.0, 2.39), (4.0, 2.67), (6.0, 2.98), (9.0, 3.20)):
        assert cp._hyd(x) == pytest.approx(want, abs=0.01)
        assert cp._hyd(-x) == -cp._hyd(x)
        assert cp._hyd_inverse(cp._hyd(x)) == pytest.approx(x, rel=1e-6)
    assert cp._hyd(1.2) == 1.2 and cp._hyd(1e6) < wt.HYD_TOP_NM
    assert cp.HYD_K + cp.HYD_R == wt.HYD_TOP_NM
    assert cp._hyd_inverse(5.0) > 1e5                                              # past the asymptote: held just under it


def test_the_assisted_driver_torque_never_passes_the_top_and_round_trips():
    for speed in (0.0, 10.0, 30.0):
        prev = 0.0
        for tc in (0.5, 2.0, 8.0, 30.0, 100.0, 400.0, 3000.0):
            td = cp.driver_from_column(tc, speed, 1.0)
            assert prev <= td < wt.HYD_TOP_NM
            prev = td
            if td < 3.5:
                assert cp.column_from_driver(td, speed, 1.0) == pytest.approx(tc, rel=1e-4)
        # the slope is the derivative of the whole chain
        tc, h = 20.0, 1e-4
        d = (cp.driver_from_column(tc + h, speed, 1.0) - cp.driver_from_column(tc - h, speed, 1.0)) / (2 * h)
        assert cp.driver_slope(tc, speed, 1.0) == pytest.approx(d, rel=1e-3)


# -- the kickback --

def front(t, dfx=0.0, angle=0.0, loads=(3000.0, 3000.0), speed=25.0, car='acr/x'):
    s = Sample(6000.0, 7500.0, gear=3, speed=speed, car=car, game='acr')
    s.tyre_fx = (dfx / 2.0, -dfx / 2.0, 0.0, 0.0)
    s.tyre_load = loads + (3000.0, 3000.0)
    s.contact_normal = ((0.0, 1.0, 0.0), (math.sin(angle), math.cos(angle), 0.0), (0.0, 1.0, 0.0), (0.0, 1.0, 0.0))
    return s


def run_kick(samples, rate=60.0):
    kick = fe.Kickback()
    out, gates = [], []
    for i, s in enumerate(samples):
        out.append(kick.update(i / rate, s, 'acr/x', 13.0, lambda: 0.3))
        gates.append(kick.gate)
    return out, gates


def test_the_kick_gate_stays_closed_on_steady_gravel():
    rng = random.Random(1)
    samples = [front(0, dfx=rng.uniform(-600.0, 600.0), angle=rng.uniform(-0.03, 0.03),
                     loads=(2500.0 + rng.uniform(-400, 400), 2500.0 + rng.uniform(-400, 400))) for _ in range(600)]
    out, gates = run_kick(samples)
    assert max(gates) == 0.0 and out == [0.0] * 600


def test_the_kick_gate_opens_on_a_one_sided_step_holds_and_fades():
    samples = [front(0) for _ in range(30)]
    samples += [front(0, dfx=3000.0, angle=0.3) for _ in range(30)]               # a kerb: one normal steps, Fx differs
    samples += [front(0, dfx=3000.0, angle=0.3) for _ in range(60)]
    out, gates = run_kick(samples)
    assert max(gates[:30]) == 0.0
    opened = next(i for i, g in enumerate(gates) if g > 0.0)
    assert 30 <= opened <= 32 and gates[opened] == 1.0
    assert all(g == 1.0 for g in gates[opened:opened + 9])                          # held 0.15 s
    assert gates[opened + 9 + 6] < 1.0 and gates[opened + 40] == 0.0                # faded over 0.10 s, then closed
    fast = [o for o in out[30:50] if o]
    assert fast and max(abs(o) for o in fast) > 0.0
    # the couple c = -SCRUB (Fx_FL - Fx_FR) / ratio steps negative: the first fast part has c's sign, and it dies away
    assert fast[0] < 0.0 and abs(out[opened + 20]) < abs(max(out, key=abs))


def test_the_kick_opens_on_a_lift_onset_and_needs_the_fx_step_and_speed():
    lift = [front(0)] * 20 + [front(0, dfx=2500.0, loads=(80.0, 3500.0))] * 20
    assert max(run_kick(lift)[1]) == 1.0
    no_step = [front(0)] * 20 + [front(0, dfx=500.0, angle=0.3, loads=(80.0, 3500.0))] * 20
    assert max(run_kick(no_step)[1]) == 0.0
    slow = [front(0, speed=3.0)] * 20 + [front(0, dfx=3000.0, angle=0.3, speed=3.0)] * 20
    assert max(run_kick(slow)[1]) == 0.0


def test_a_gap_or_a_new_car_resets_the_kick():
    kick = fe.Kickback()
    for i in range(20):
        kick.update(i / 60.0, front(0), 'acr/x', 13.0, lambda: 0.3)
    for i in range(20, 24):
        kick.update(i / 60.0, front(0, dfx=3000.0, angle=0.3), 'acr/x', 13.0, lambda: 0.3)
    assert kick.gate == 1.0
    kick.update(1.0, front(0), 'acr/x', 13.0, lambda: 0.3)                          # a 0.7 s gap
    assert kick.gate == 0.0
    for i in range(4):
        kick.update(1.1 + i / 60.0, front(0, dfx=3000.0, angle=0.3), 'acr/x', 13.0, lambda: 0.3)
    kick.update(1.2, front(0), 'acr/y', 13.0, lambda: 0.3)                          # another car
    assert kick.gate == 0.0


def test_the_kick_is_nothing_without_assist_and_is_capped_in_the_rack():
    out, _ = run_kick([front(0)] * 20 + [front(0, dfx=3000.0, angle=0.3)] * 30)
    assert max(abs(o) for o in out) > 0.0
    samples = [front(0)] * 20 + [front(0, dfx=3000.0, angle=0.3)] * 30
    kick = fe.Kickback()
    assert all(kick.update(i / 60.0, s, 'acr/x', 13.0, lambda: 1.0) == 0.0 for i, s in enumerate(samples))
    rack = fe.RackForce()
    assert rack.KICK_CAP == 0.15 and rack.KICK_TOTAL == pytest.approx(0.90)
