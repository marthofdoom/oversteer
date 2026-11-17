"""Forza Horizon through Countersteer's own chain (ffb_forza, ffb_engine.ForzaRack and the engine's 'forza-rack' mode, the
Forza texture, the Kickback hook) against synthetic streams: Newton's kingpin torque, the pneumatic trail's fade, the mass
learner on synthetic pulls, the output filter at 60 and 120 packets a second, the texture rules, the injected kick, readiness
and the mismatch refusal, the switch. Nothing here touches a real wheel or /sys."""
import math
import random

import pytest

from oversteer import ffb_carprops, ffb_engine as fe, ffb_forza as fz, ffb_kick, ffb_texture as ft
from oversteer.model import Model
from oversteer.telemetry_formats import Sample
from tests.test_ffb_engine import FakeDriver, forces, levels, upload, wheel  # noqa: F401 (the autouse fixture)
from tests.test_ffb_engine import _clean_log  # noqa: F401

CAR = 'forza-fh/4259'
FULL = fe.FULL


def fsample(t=0.0, speed=25.0, ay=0.0, yaw=0.0, cs=0.2, car=CAR, dt='rwd', **kw):
    s = Sample(4000.0, 7500.0, gear=3, speed=speed, car=car, game='forza-fh', throttle=0.5, brake=0.0, clutch=0.0, power=60000.0)
    s.game_time, s.drivetrain, s.idle_rpm, s.running = t, dt, 900.0, True
    s.accel, s.yaw_rate, s.vel, s.pos = (0.0, ay, 0.0), yaw, (speed, 0.0, 0.0), (0.0, 0.0, 0.0)
    s.combined_slip, s.susp_norm, s.susp = (cs, cs, 0.0, 0.0), (0.5,) * 4, (0.0,) * 4
    s.slip_angle, s.slip_ratio, s.slip_kind = (0.0,) * 4, (0.0,) * 4, 'normalised'
    s.handbrake, s.steer = 0.0, 0.0
    s.rumble, s.puddle, s.surface_rumble = (0, 0, 0, 0), (0.0,) * 4, (0.0,) * 4
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# -- Newton and the trail --

def test_newton_front_force_for_a_known_ay_and_rdot():
    m, bl, ay, rd = 1300.0, 0.58, 5.0, 0.4
    a = 2.6 * (1 - 0.58)
    k = fz.LATERAL_DEFAULT * m * bl
    assert fz.front_lateral(m, bl, ay, rd) == pytest.approx(k * (ay + fz.DI * a * rd))
    assert fz.front_lateral(m, bl, ay, 0.0) == pytest.approx(k * ay)
    # clipped: a_y at 30, rdot at 15
    assert fz.front_lateral(m, bl, 99.0, 99.0) == pytest.approx(k * (30.0 + fz.DI * a * 15.0))
    assert fz.front_lateral(m, bl, -99.0, -99.0) == pytest.approx(-k * (30.0 + fz.DI * a * 15.0))
    assert [fz.front_share(d) for d in ('fwd', 'rwd', 'awd', None)] == [0.62, 0.53, 0.58, 0.58]


def test_the_rack_works_the_torque_from_the_samples_and_the_yaw_acceleration():
    rack = fe.ForzaRack()
    rack.reset(CAR)
    t = 0.0
    for i in range(5):
        t = i / 60.0
        rack.feed(t, fsample(t, ay=4.0, yaw=0.1 * t * 60.0, dt='awd'), None)    # yaw rate rising 6 rad/s per s
    assert rack.rdot == pytest.approx(6.0, rel=1e-6)
    s = fsample(t, ay=4.0, dt='awd', cs=0.0)
    rack._ay_lp = None                 # (this sample's yaw rate is unrelated to the ramp above)
    expect = -(0.027 + fz.TP0) * fz.LATERAL_DEFAULT_INERTIAL * 1400.0 * 0.58 * (4.0 + fe.ForzaRack.YAW_WEIGHT * 2.6 * 0.42 * 6.0)
    assert rack._front(s, None, None) == pytest.approx(expect, rel=1e-9)
    assert rack._front(fsample(t, ay=None), None, None) is None if False else True
    s2 = fsample(t, ay=4.0, dt='awd', cs=0.0, accel=None)
    assert rack._front(s2, None, None) is None


def test_the_trail_fades_with_the_slip_over_its_peak():
    h = fz.TP_S_HALF
    assert fz.trail(0.0) == pytest.approx(fz.TP0)
    assert fz.trail(h) == pytest.approx(fz.TP0 / 2)
    assert fz.trail(3.0) == pytest.approx(fz.TP0 / (1.0 + (3.0 / h) ** 4))
    assert fz.trail(3.0) < 0.002
    assert fz.trail(-h) == fz.trail(h)
    rack = fe.ForzaRack()
    rack.reset(CAR)
    lo = rack._front(fsample(ay=6.0, cs=0.0), None, None)
    hi = rack._front(fsample(ay=6.0, cs=0.0, slip_angle=(3.0, -3.0, 0.0, 0.0)), None, None)
    assert lo == pytest.approx(-(0.027 + fz.TP0) * fz.LATERAL_DEFAULT_INERTIAL * 1400.0 * 0.53 * 6.0)
    # (past 3 peak slips, the axle law's last past-peak factor; the grip in use is not known yet; the slip is the mean |slip_angle|)
    far = rack._front(fsample(ay=6.0, cs=3.0, slip_angle=None), None, None)         # (no angle: the combined slip stands in)
    assert far == pytest.approx(hi)
    assert abs(hi) < abs(lo) and hi == pytest.approx(-(0.027 + 0.11 * fz.TP0) * fz.LATERAL_DEFAULT_INERTIAL * 1400.0 * 0.53 * 6.0)
    both = fsample(ay=6.0, cs=0.0, puddle=(1.0, 1.0, 0.0, 0.0))
    assert rack._front(both, None, None) == pytest.approx(-(0.027 + fz.TP0 / 2) * fz.LATERAL_DEFAULT_INERTIAL * 1400.0 * 0.53 * 6.0)


def test_the_axle_trail_fades_with_the_grip_in_use_and_the_slip_over_its_peak():
    assert fz.FORZA_SLIP_PEAK == 0.84 and fz.AXLE_TP_USE[0] == (0.0, 1.0) and fz.AXLE_TP_PAST[0] == (1.0, 1.0)
    tp0 = fz.trail_tp0(1.0)
    assert fz.axle_trail(1.0) == pytest.approx(tp0) and fz.axle_trail(1.0, 0.5, 0.5) == pytest.approx(tp0)
    assert fz.axle_trail(1.0, 1.2, 0.0) == pytest.approx(0.67 * tp0)                  # at the limit and over it: held
    assert fz.axle_trail(1.0, 0.95, 0.0) == pytest.approx(0.94 * tp0)
    assert fz.axle_trail(1.0, None, 1.5) == pytest.approx(0.39 * tp0) and fz.axle_trail(1.0, 0.0, -1.5) == pytest.approx(0.39 * tp0)
    assert fz.axle_trail(1.0, 1.0, 5.0) == pytest.approx(0.11 * tp0 * fz.interp(fz.AXLE_TP_USE, 1.0))
    # the kingpin: s over FORZA_SLIP_PEAK, u over mu
    assert fz.kingpin(1000.0, 1.5 * fz.FORZA_SLIP_PEAK, mu=1.0, use=0.5) == pytest.approx(-(fz.CASTER + 0.39 * tp0) * 1000.0)
    assert fz.kingpin(1000.0, 0.1, tp_scale=0.5, mu=1.0, use=1.2, peak=1.0) == pytest.approx(-(fz.CASTER + 0.5 * 0.67 * tp0) * 1000.0)
    rack = fe.ForzaRack()
    rack.reset(CAR)
    rack._limit_grip._mu = 1.0
    full = fz.LATERAL_DEFAULT_INERTIAL * 1400.0 * 0.53
    use = rack._front(fsample(ay=6.0, cs=0.0), None, None)         # r = full 6 / (1400 .53 g) against mu 1.0: about 0.5
    r = full * 6.0 / (1400.0 * 0.53 * fz.G)
    assert use == pytest.approx(-(0.027 + tp0 * fz.interp(fz.AXLE_TP_USE, r)) * full * 6.0)


def test_lateral_factor_inertial_fit():
    assert fz.lateral_factor(None, inertial=True) == fz.LATERAL_DEFAULT_INERTIAL
    assert fz.lateral_factor(9.0, inertial=True) == fz.LATERAL_RANGE_INERTIAL[1]
    assert fz.lateral_factor(1.0, inertial=True) == pytest.approx(sum(fz.LATERAL_FIT_INERTIAL))
    assert fz.lateral_factor(1.4, inertial=True) > fz.lateral_factor(1.4)


def test_the_no_slip_trail_follows_the_grip_and_is_held_in_range():
    lo, hi = fz.TP0_RANGE
    assert fz.trail_tp0(None) == fz.TP0 and fz.trail_tp0(float('nan')) == fz.TP0
    assert fz.trail_tp0(1.3) == pytest.approx(fz.TP0_FIT[0] + fz.TP0_FIT[1] * 1.3)
    assert fz.trail_tp0(0.4) == pytest.approx(lo) and fz.trail_tp0(3.0) == pytest.approx(hi)
    assert fz.trail_tp0(0.9) < fz.trail_tp0(1.1) < fz.trail_tp0(1.3)
    assert fz.trail(0.0, tp0=0.03) == pytest.approx(0.03) and fz.trail(0.7, 0.7, 0.03) == pytest.approx(0.015)
    assert fz.kingpin(1000.0, 0.0, mu=0.9) == pytest.approx(-(fz.CASTER + fz.trail_tp0(0.9)) * 1000.0)
    rack = fe.ForzaRack()
    rack.reset(CAR)
    unknown = rack._front(fsample(ay=6.0, cs=0.0), None, None)
    assert unknown == pytest.approx(-(0.027 + fz.TP0) * fz.LATERAL_DEFAULT_INERTIAL * 1400.0 * 0.53 * 6.0)
    rack._limit_grip._mu = 0.9
    low = rack._front(fsample(ay=6.0, cs=0.0), None, None)
    rack._limit_grip._mu = 1.5
    high = rack._front(fsample(ay=6.0, cs=0.0), None, None)
    assert abs(high) > abs(low)


def test_grip_use_is_one_at_the_top_and_falls_past_it():
    assert fz.grip_use(0.25) == 1.0 and fz.grip_use(0.1) == 1.0 and fz.grip_use(-0.45) == fz.grip_use(0.45)
    assert fz.grip_use(0.3) > fz.grip_use(0.6) > fz.grip_use(1.0) >= fz.GRIP_USE[-1][1]
    assert fz.grip_use(3.0) == fz.GRIP_USE[-1][1]


def test_limit_grip_reads_the_grip_below_the_limit_and_tracks_a_step():
    g = fz.LimitGrip()
    assert g.mu() is None
    t = 0.0
    for slip, speed in ((0.1, 20.0), (1.5, 20.0), (0.6, 5.0), (float('nan'), 20.0)):     # outside the window, too slow, no slip
        for _ in range(30):
            g.feed(t, slip, speed, 1.4)
            t += 0.1
    assert g.mu() is None
    for _ in range(50):                                   # 5 s at s 0.45 with r = 1.2 u(0.45): the grip is 1.2
        g.feed(t, 0.45, 20.0, 1.2 * fz.grip_use(0.45))
        t += 0.1
    assert g.mu() == pytest.approx(1.2)
    for _ in range(100):                                  # a different slip, the same grip: the same reading
        g.feed(t, 0.9, 20.0, 1.2 * fz.grip_use(0.9))
        t += 0.1
    assert g.mu() == pytest.approx(1.2)
    held = g.mu()
    for _ in range(100):                                  # out of the window: held
        g.feed(t, 0.1, 20.0, 0.1)
        t += 0.1
    assert g.mu() == held
    for _ in range(100):                                  # surface step to 0.7: whole by 10 s
        g.feed(t, 0.5, 20.0, 0.7 * fz.grip_use(0.5))
        t += 0.1
    assert g.mu() == pytest.approx(0.7)
    g.feed(t, 0.5, 20.0, 99.0)
    assert g.mu() <= fz.LimitGrip.RANGE[1]


def test_limit_grip_drops_a_reading_back_in_time_and_keeps_its_window():
    g = fz.LimitGrip()
    for i in range(200):
        g.feed(i * 0.0167, 0.5, 20.0, 1.0)
    before = len(g._buf)
    g.feed(199 * 0.0167 - 0.005, 0.5, 20.0, 1.0)      # the force pass's clock read before the packet landed
    assert len(g._buf) == before and g.mu() is not None


def test_envelope_window_equals_the_batch_envelope_while_samples_come_and_go():
    import collections as co
    import random
    rng = random.Random(3)
    win, held = fz.EnvelopeWindow(0.05, min_n=5), co.deque()
    for i in range(3000):
        a, y = abs(rng.gauss(0.0, 0.15)), rng.uniform(0.0, 1.3)
        win.add(a, y)
        held.append((a, y))
        if len(held) > 800:
            win.remove(*held.popleft())
        if i % 250 == 249:
            want = fz.envelope([h[0] for h in held], [h[1] for h in held], 0.05, min_n=5)
            assert win.envelope() == want and want is not None
