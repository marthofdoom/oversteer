"""The 1:1 mapping with a knee (wheel_torque.rim_share), the hydraulic top end (ffb_carprops) and the steering mapping's
modes (ffb_engine.RackForce). Nothing here touches a real wheel or /sys."""
import math
import random

import pytest

from oversteer import ffb_carprops as cp, ffb_engine as fe, wheel_torque as wt
from oversteer.telemetry_formats import Sample

NM = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 3.7)


def test_the_knee_plays_1_to_1_and_compresses_above_it():
    assert wt._gamma(0.5, 3.7, 2.1) == pytest.approx(0.413, abs=0.002)
    got = [wt.rim_share(x, 2.1, 1.0, 3.7, 0.5) for x in NM]
    assert got == pytest.approx([0.238, 0.476, 0.628, 0.713, 0.782, 0.844, 0.899, 0.920], abs=0.003)
    assert wt._gamma(0.4, 3.7, 2.1) == pytest.approx(0.490, abs=0.002)
    got = [wt.rim_share(x, 2.1, 1.0, 3.7, 0.4) for x in NM]
    assert got == pytest.approx([0.238, 0.463, 0.587, 0.679, 0.759, 0.830, 0.895, 0.920], abs=0.003)


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


def test_the_hydraulic_top_end(street):
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


def test_the_rally_top_end_is_7_and_street_3_7_and_off_is_unassisted():
    assert cp.power_class() == 'rally' and cp.hyd_top() == 7.0 and wt.HYD_TOP_NM == 7.0
    assert (cp.HYD_K, cp.HYD_R) == (3.0, 4.0)
    assert cp.driver_from_column(1e6, 25.0, 1.0) < 7.0 and cp.driver_from_column(1e6, 25.0, 1.0) > 6.99
    assert cp._hyd(2.9) == 2.9                                                     # the low and mid range are the road curve's
    rally_low = cp.driver_from_column(3.0, 25.0, 1.0)
    assert cp.set_power_class('street') == 'street' and cp.hyd_top() == 3.7 and wt.HYD_TOP_NM == 3.7
    assert cp.driver_from_column(1e6, 25.0, 1.0) < 3.7
    assert cp.driver_from_column(3.0, 25.0, 1.0) == pytest.approx(rally_low, abs=0.5)   # (the same boost curve underneath)
    assert cp.set_power_class('off') == 'off'
    assert cp.power_settings()[0] == 'off'                                         # unassisted: the column torque is played
    assert cp.set_power_class('bogus') == 'rally'


def test_the_top_torque_follows_the_class():
    rack = fe.RackForce()
    rack.assist = 1.0
    rack._boost = 1.0
    assert rack.top_torque() == 7.0
    cp.set_power_class('street')
    assert rack.top_torque() == 3.7
    cp.set_power_class('off')
    assert rack.top_torque() != 3.7 and rack.top_torque() != 7.0                    # unassisted: its own p99-based top
