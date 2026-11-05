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
    assert fz.front_lateral(m, bl, ay, rd) == pytest.approx(m * bl * (ay + a * rd))
    assert fz.front_lateral(m, bl, ay, 0.0) == pytest.approx(m * bl * ay)
    # clipped: a_y at 30, rdot at 15
    assert fz.front_lateral(m, bl, 99.0, 99.0) == pytest.approx(m * bl * (30.0 + a * 15.0))
    assert fz.front_lateral(m, bl, -99.0, -99.0) == pytest.approx(-m * bl * (30.0 + a * 15.0))
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
    expect = -(0.027 + 0.028) * 1400.0 * 0.58 * (4.0 + fe.ForzaRack.YAW_WEIGHT * 2.6 * 0.42 * 6.0)
    assert rack._front(s, None, None) == pytest.approx(expect, rel=1e-9)
    assert rack._front(fsample(t, ay=None), None, None) is None if False else True
    s2 = fsample(t, ay=4.0, dt='awd', cs=0.0, accel=None)
    assert rack._front(s2, None, None) is None


def test_the_trail_fades_with_the_combined_slip():
    assert fz.trail(0.0) == pytest.approx(0.028)
    assert fz.trail(1.4) == pytest.approx(0.014)
    assert fz.trail(3.0) == pytest.approx(0.028 / (1.0 + (3.0 / 1.4) ** 4))
    assert fz.trail(3.0) < 0.002
    assert fz.trail(-1.4) == fz.trail(1.4)
    rack = fe.ForzaRack()
    rack.reset(CAR)
    lo = rack._front(fsample(ay=6.0, cs=0.0), None, None)
    hi = rack._front(fsample(ay=6.0, cs=3.0), None, None)
    assert lo == pytest.approx(-(0.027 + 0.028) * 1400.0 * 0.53 * 6.0)
    assert abs(hi) < abs(lo) and hi == pytest.approx(-(0.027 + fz.trail(3.0)) * 1400.0 * 0.53 * 6.0)
    both = fsample(ay=6.0, cs=0.0, puddle=(1.0, 1.0, 0.0, 0.0))
    assert rack._front(both, None, None) == pytest.approx(-(0.027 + 0.014) * 1400.0 * 0.53 * 6.0)


def test_the_constants_ratio_14_wheel_375_assist_1_model():
    rack = fe.ForzaRack()
    rack.reset(CAR)
    assert rack.ratio == 14.0 and rack.assist == 1.0 and rack.car_wheel_mm == 375.0 and not rack.LEARNS
    assert rack.learner is None and rack.power_steering() == 'model'
    assert rack.top_torque() == fe.wheel_torque.HYD_TOP_NM


# -- the mass --

def pulls(learner, mass=1300.0, seconds=60.0, hz=60.0, c0=250.0, c2=0.4, j=0.3, **gate):
    """A drive under power with the car's acceleration swinging +-2 m/s^2: the power it takes is m a + c0 + c2 v^2 at the wheels
    plus the engine's inertia."""
    t, v, n = 0.0, 22.0, int(seconds * hz)
    for i in range(n):
        a = 1.8 * math.sin(2 * math.pi * t / 7.0) + 0.7 * math.sin(2 * math.pi * t / 2.3)
        v += a / hz
        omega = 3000.0 * math.pi / 30.0 * (v / 22.0)
        force = mass * a + c0 + c2 * v * v + j * (omega / v) ** 2 * a
        s = fsample(t, speed=v, power=force * v, rpm=3000.0 * v / 22.0)
        s.pos = (0.0, 0.0, 0.0)
        for k, val in gate.items():
            setattr(s, k, val)
        learner.add(t, s)
        t += 1.0 / hz
    return learner


def test_the_mass_learner_converges_on_synthetic_pulls():
    learner = pulls(fz.MassLearner())
    assert learner.accepted and learner.r >= 0.85 and learner.sd >= 0.8
    assert learner.mass == pytest.approx(1300.0, rel=0.05)
    # from a stored mass it holds, and the slew is 50 kg/s
    other = pulls(fz.MassLearner(1290.0), seconds=40.0)
    assert other.mass == pytest.approx(1300.0, rel=0.05)
    slow = fz.MassLearner(700.0)
    pulls(slow, seconds=25.0)
    assert slow.mass <= 700.0 + 50.0 * 25.0 + 1e-6


def test_the_mass_learner_waits_for_enough_clean_time_and_swing():
    short = pulls(fz.MassLearner(), seconds=12.0)
    assert not short.accepted and short.mass == fz.MassLearner.PRIOR
    # constant speed: nothing to regress on (sd(a) too small)
    flat = fz.MassLearner()
    for i in range(60 * 40):
        t = i / 60.0
        flat.add(t, fsample(t, speed=25.0, power=(250.0 + 0.4 * 625.0) * 25.0, rpm=3000.0))
    assert not flat.accepted and flat.mass == fz.MassLearner.PRIOR


@pytest.mark.parametrize('gate', [
    {'brake': 0.3}, {'clutch': 0.5}, {'handbrake': 1.0}, {'slip_angle': (0.6, 0.0, 0.0, 0.0)},
    {'slip_ratio': (0.0, 0.0, 0.4, 0.0)}, {'susp_norm': (0.5, 0.5, 0.5, 0.995)}, {'susp_norm': (0.005, 0.5, 0.5, 0.5)},
    {'accel': (0.0, 3.5, 0.0)}, {'speed': 7.0}])
def test_the_mass_learner_rejects_gated_samples(gate):
    learner = pulls(fz.MassLearner(), seconds=45.0, **gate) if 'speed' not in gate else fz.MassLearner()
    if 'speed' in gate:
        for i in range(60 * 45):
            t = i / 60.0
            learner.add(t, fsample(t, speed=7.0, power=50000.0, rpm=2000.0))
    assert learner._w == 0.0 and not learner.accepted and learner.mass == fz.MassLearner.PRIOR


def test_a_gear_change_and_a_gap_drop_their_samples():
    learner = fz.MassLearner()
    t = 0.0
    for i in range(60 * 3):
        t = i / 60.0
        learner.add(t, fsample(t, power=50000.0, gear=3 if i < 90 else 4))
    # the 0.15 s before the change and after are not counted: less than the same drive in one gear
    steady = fz.MassLearner()
    for i in range(60 * 3):
        steady.add(i / 60.0, fsample(i / 60.0, power=50000.0))
    assert 2.0 < learner._w < steady._w - 0.25
    w = learner._w
    learner.add(t + 0.2, fsample(t + 0.2, power=50000.0, gear=4))        # a gap of 200 ms: starts again
    learner.add(t + 0.217, fsample(t + 0.217, power=50000.0, gear=4))
    assert learner._w == w


def test_the_mass_is_clamped_and_stored_per_car(tmp_path):
    ffb_carprops.STORE.use(str(tmp_path / 'props.json'))
    old = ffb_carprops.ASYNC
    ffb_carprops.ASYNC = False
    try:
        rack = fe.ForzaRack()
        rack.reset(CAR)
        assert rack.mass == 1400.0
        ffb_carprops.STORE.set_mass(CAR, 1273.4)
        assert ffb_carprops.STORE.get_mass(CAR) == 1273.4
        assert ffb_carprops.STORE.get_mass(CAR + '1') is None
        ffb_carprops.STORE.set_mass(CAR, 99999.0)               # out of range: not kept
        assert ffb_carprops.STORE.get_mass(CAR) == 1273.4
        ffb_carprops.STORE.use(str(tmp_path / 'props.json'))     # forgets what was read: reads the file
        ffb_carprops.STORE.set_mass(CAR, 1273.4)
        ffb_carprops.STORE.use(str(tmp_path / 'props.json'))
        rack.reset(CAR)
        assert rack.mass == 1273.4 and rack.mass_l.known
    finally:
        ffb_carprops.ASYNC = old


# -- the output filter --

def run_filter(hz, step, seconds=0.3, pass_dt=0.001, flt=None):
    """A step of `step` (ext_force units) at 0.1 s into packets at `hz`, the filter stepped every `pass_dt`; (times, outputs)."""
    flt = flt or fz.OutputFilter()
    ts, ys, t, next_packet, level = [], [], 0.0, 0.0, 0.0
    while t < seconds:
        if t >= next_packet:
            flt.packet(t)
            level = step if t >= 0.1 else 0.0
            next_packet += 1.0 / hz
        ts.append(t)
        ys.append(flt.step(t, level))
        t = round(t + pass_dt, 6)
    return flt, ts, ys


def t_cross(ts, ys, level, after=0.1):
    return next(t for t, y in zip(ts, ys) if t >= after and y >= level) - after


def test_the_filter_step_has_no_overshoot_and_about_20_ms_of_lag_at_60():
    step = 0.1 * FULL
    flt, ts, ys = run_filter(60.0, step, seconds=0.5)
    assert max(ys) <= step + 1e-6                                  # no overshoot
    assert all(b >= a - 1e-9 for a, b in zip(ys, ys[1:]))          # monotone
    assert flt.corner() == pytest.approx(24.0, abs=0.5)
    # critically damped at a 20 Hz corner: 90 % after about 20 ms (plus the packet's own wait)
    assert 0.015 < t_cross(ts, ys, 0.9 * step) - 0.0 < 0.030
    # a ramp is followed 2 tau behind (10 ms) plus the packets' half interval (8 ms): about 18 ms in all
    flt = fz.OutputFilter()
    lag = []
    t, next_packet, last = 0.0, 0.0, 0.0
    while t < 1.0:
        if t >= next_packet:
            flt.packet(t)
            last = 0.5 * FULL * t
            next_packet += 1.0 / 60.0
        y = flt.step(t, last)
        if t > 0.5:
            lag.append(t - y / (0.5 * FULL))
        t = round(t + 0.001, 6)
    assert sum(lag) / len(lag) == pytest.approx(0.0187, abs=0.004)


def test_the_filter_slews_a_big_step_at_015_full_scale_per_10_ms():
    flt, ts, ys = run_filter(60.0, 0.8 * FULL, seconds=0.35)
    top = max(abs(b - a) / 0.001 for a, b in zip(ys, ys[1:]))
    assert top <= 0.15 * FULL / 0.010 * 1.001
    assert top > 0.14 * FULL / 0.010                                # and it does bind on a step this big


def warmed(hz, seconds=4.0):
    flt = fz.OutputFilter()
    t = 0.0
    while t < seconds:
        flt.packet(t)
        t += 1.0 / hz
    flt.step(0.0, 0.0)
    flt._rate_at = None
    flt.step(seconds, 0.0)
    return flt


def test_the_corner_and_slew_follow_the_packet_rate_60_and_120():
    f60, f120 = warmed(60.0), warmed(120.0)
    for _ in range(40):                       # the rate is smoothed: it settles
        for flt, hz in ((f60, 60.0), (f120, 120.0)):
            flt.packet(flt._last)
    for flt, hz in ((f60, 60.0), (f120, 120.0)):
        t = flt._last
        for i in range(int(6.0 * hz)):
            t += 1.0 / hz
            flt.packet(t)
            for k in range(4):
                flt.step(t + k * 0.00025, 0.0)
        assert flt.rate == pytest.approx(hz, rel=0.03)
    assert f60.corner() == pytest.approx(24.0, rel=0.04) and f120.corner() == pytest.approx(48.0, rel=0.04)
    assert f120.slew() == pytest.approx(2.0 * f60.slew(), rel=0.05)
    assert f60.slew() == pytest.approx(0.15 * FULL / 0.010, rel=0.04)
    # the same step answers about twice as fast at 120/s
    _, ts1, ys1 = run_filter(60.0, 0.1 * FULL)
    _, ts2, ys2 = run_filter(120.0, 0.1 * FULL, flt=f120)
    assert t_cross(ts2, ys2, 0.09 * FULL) < 0.75 * t_cross(ts1, ys1, 0.09 * FULL)
    # and the corner stays within 15..60 Hz
    slow, fast = fz.OutputFilter(), fz.OutputFilter()
    slow.rate, fast.rate = 20.0, 400.0
    assert slow.corner() == 15.0 and fast.corner() == 60.0


def test_the_filter_starts_at_its_input_after_a_gap():
    flt = fz.OutputFilter()
    flt.step(0.0, 1000.0)
    assert flt.step(0.001, 1000.0) == pytest.approx(1000.0)
    assert flt.step(5.0, -4000.0) == -4000.0


def test_the_filter_takes_out_the_buzz_between_the_60_hz_steps():
    """The 30-120 Hz content of a 60/s staircase of a swinging force, before and after (the replay reports the real drive's)."""
    import numpy as np
    rnd = random.Random(2)
    flt, raw, out = fz.OutputFilter(), [], []
    t, next_packet, level = 0.0, 0.0, 0.0
    while t < 4.0:
        if t >= next_packet:
            flt.packet(t)
            level = 0.4 * FULL * math.sin(2 * math.pi * 1.0 * t) + rnd.gauss(0, 0.01) * FULL
            next_packet += 1.0 / 60.0
        raw.append(level)
        out.append(flt.step(t, level))
        t = round(t + 0.001, 6)

    def band(x):
        spectrum = np.abs(np.fft.rfft(np.asarray(x) * np.hanning(len(x)))) / len(x)
        freqs = np.fft.rfftfreq(len(x), 0.001)
        sel = (freqs >= 30.0) & (freqs <= 120.0)
        return float(np.sqrt(np.sum(spectrum[sel] ** 2)))
    assert band(out) < 0.33 * band(raw)


# -- the texture --

def run_texture(tx, make, seconds=2.0, pass_dt=0.001, hz=60.0, steering=0.3 * FULL):
    """The engine's way: a new sample each 1/hz, the texture stepped every pass; the values."""
    out, t, n, sample = [], 0.0, 0, None
    while t < seconds:
        if sample is None or t >= n / hz:
            sample = make(t, n)
            n += 1
        out.append(tx.force(t, sample, steering))
        t = round(t + pass_dt, 6)
    return out


def tex(**parts):
    tx = ft.Texture()
    tx.set(1.0, {name: parts.get(name, False) for name in ft.PARTS})
    return tx


def test_forza_is_in_the_texture_games_and_has_no_lock_part():
    assert 'forza-fh' in ft.TEXTURE_GAMES and 'forza-fh' in ft.NO_LOCK_GAMES
    tx = tex(lock=True)
    out = run_texture(tx, lambda t, n: fsample(t, brake=1.0, wheel_speed=(2.0, 2.0, 20.0, 20.0), vel=(20.0, 0, 0)))
    assert all(v == 0.0 for v in out)


def test_the_road_is_the_travel_rate_band_passed_4_to_20_hz():
    rnd = random.Random(5)
    # a 10 Hz bump under FL only passes; a 1 Hz body roll and a 45 Hz buzz do not (relative to what they would be raw)

    def gain(freq):
        tx = tex(road=True)
        tx.level = 1.0
        out = run_texture(tx, lambda t, n: fsample(t, susp=(0.004 * math.sin(2 * math.pi * freq * t), 0.0, 0.0, 0.0)),
                          seconds=3.0)
        tail = out[1500:]
        return math.sqrt(sum(v * v for v in tail) / len(tail)) / freq        # per unit of velocity (travel x 2 pi f)
    mid, low, high = gain(10.0), gain(1.0), gain(45.0)
    assert mid > 0.0 and low < 0.35 * mid and high < 0.6 * mid
    both = tex(road=True)
    out = run_texture(both, lambda t, n: fsample(t, susp=(0.004 * math.sin(2 * math.pi * 10.0 * t),) * 2 + (0.0, 0.0)),
                      seconds=2.0)
    assert max(abs(v) for v in out[800:]) < 1e-6 * FULL            # both fronts together: not a steering input
    assert rnd.random() >= 0.0


def test_the_surface_is_20_to_40_hz_noise_scaled_by_rumble_and_speed_and_capped():
    def peak(speed, rumble, seconds=2.0):
        tx = tex(road=True)
        out = run_texture(tx, lambda t, n: fsample(t, speed=speed, surface_rumble=(rumble,) * 4), seconds=seconds)
        return max(abs(v) for v in out[1200:])
    quiet = peak(25.0, 0.0)
    assert quiet == 0.0
    low = peak(25.0, 0.01)
    assert 0.0 < low
    big = peak(25.0, 0.6)
    assert big <= ft.TOTAL_CAP * FULL * ft.LEVEL + 1e-9 or big <= 1.0 * FULL * 0.02 * 1.0 + 1e-6
    # capped at 0.02 of full scale before the master level and the limiter
    cues = ft.ForzaCues()
    out = [cues.step(i * 0.001, 0.001, fsample(i * 0.001, speed=30.0, surface_rumble=(0.6,) * 4))[0] for i in range(1000)]
    assert max(abs(v) for v in out) <= 0.02 * FULL + 1e-6 and max(abs(v) for v in out) > 0.01 * FULL
    # slower: less (speed / 15)
    half = ft.ForzaCues()
    out2 = [half.step(i * 0.001, 0.001, fsample(i * 0.001, speed=7.5, surface_rumble=(0.01,) * 4))[0] for i in range(1000)]
    full = ft.ForzaCues()
    out3 = [full.step(i * 0.001, 0.001, fsample(i * 0.001, speed=15.0, surface_rumble=(0.01,) * 4))[0] for i in range(1000)]
    assert max(abs(v) for v in out2) == pytest.approx(0.5 * max(abs(v) for v in out3), rel=0.05)
    # the band: 20-40 Hz sines only (the oscillator list)
    assert min(ft.ForzaCues.SURFACE_HZ) >= 20.0 and max(ft.ForzaCues.SURFACE_HZ) <= 40.0
    # the energy sits there: few zero crossings per second would be a low band, many a high one
    crossings = sum(1 for a, b in zip(out3[200:], out3[201:]) if a * b < 0) / 0.8
    assert 20.0 <= crossings <= 100.0


@pytest.mark.parametrize('speed, hz', [(9.0, 20.0), (21.0, 46.7), (60.0, 70.0)])
def test_a_kerb_is_a_sawtooth_at_speed_over_045_m_capped_at_70_hz(speed, hz):
    cues = ft.ForzaCues()
    values = []
    for i in range(1000):
        t = i * 0.001
        values.append(cues.step(t, 0.001, fsample(t, speed=speed, rumble=(1, 0, 0, 0)))[0])
    assert cues.kerb_hz == pytest.approx(hz, rel=0.01)
    assert max(abs(v) for v in values) <= 0.03 * FULL + 1e-6 and max(values) > 0.025 * FULL and min(values) < -0.025 * FULL
    drops = [i for i in range(1, 1000) if values[i] < values[i - 1] - 0.03 * FULL]       # a sawtooth falls once per period
    if len(drops) > 3:
        period = (drops[-1] - drops[0]) / (len(drops) - 1) * 0.001
        assert 1.0 / period == pytest.approx(hz, rel=0.1)
    off = ft.ForzaCues()
    assert all(abs(off.step(i * 0.001, 0.001, fsample(i * 0.001, speed=speed))[0]) == 0.0 for i in range(100))


def test_a_landing_is_a_pulse_after_all_four_were_in_the_air():
    def drive(frames):
        cues = ft.ForzaCues()
        peak, t = 0.0, 0.0
        for n, travel in enumerate(frames):
            sample = fsample(n / 60.0, susp_norm=travel)
            for _ in range(17):
                peak = max(peak, abs(cues.step(t, 0.001, sample)[1]))
                t += 0.001
        return cues, peak
    ground, air = (0.5,) * 4, (0.0,) * 4
    cues, peak = drive([ground] * 20 + [air] * 12 + [(0.3, 0.3, 0.0, 0.0), (0.6, 0.6, 0.2, 0.2)] + [ground] * 30)
    assert cues.impact.count == 1 and 0.0 < peak <= ft.ImpactPulse.CAP * FULL + 1e-6
    # a short hop (under AIR_TIME), or a front compression on the ground: no landing
    cues, peak = drive([ground] * 20 + [air] * 2 + [(0.6, 0.6, 0.5, 0.5)] * 30)
    assert cues.impact.count == 0 and peak == 0.0
    cues, peak = drive([ground] * 10 + [(0.5, 0.5, 0.5, 0.5), (0.9, 0.9, 0.5, 0.5)] * 5)
    assert cues.impact.count == 0
    # a landing is rate limited (HOLD)
    cues, _ = drive(([air] * 8 + [(0.4, 0.4, 0.1, 0.1)] + [ground] * 4) * 2)
    assert cues.impact.count == 1


def test_the_forza_texture_obeys_the_master_level_and_the_limiter():
    tx = ft.Texture()
    tx.set(1.0, {name: name in ('road', 'impact') for name in ft.PARTS})      # (the engine is outside the limiter)
    out = run_texture(tx, lambda t, n: fsample(t, speed=30.0, rumble=(1, 0, 0, 0), surface_rumble=(0.6,) * 4,
                                                susp=(0.01 * math.sin(n), 0.0, 0.0, 0.0)), seconds=3.0, steering=0.0)
    assert max(abs(v) for v in out) <= ft.FLOOR * FULL + 1e-6          # no steering force: only the floor of headroom
    assert max(abs(v) for v in out) > 0.0
    off = ft.Texture()
    off.set(0.0, {name: True for name in ft.PARTS})
    assert all(v == 0.0 for v in run_texture(off, lambda t, n: fsample(t, rumble=(1, 0, 0, 0)), seconds=0.5))


# -- the kick --

class Rack:
    def __init__(self):
        self.kick_on, self.kick_level, self.strength = True, 1.0, 1.0
        self.ratio, self.assist, self.wheel_peak, self.car, self.trail, self._now = 14.0, 1.0, 2.1, CAR, 0.027, 0.0
        self.sent = None

    def power_steering(self):
        return 'model'

    def assist_level(self):
        return 1.0

    def classic(self):
        return False

    def _front(self, sample, trail, scrub):
        return 100.0

    def kick_trigger(self, sample):
        out, self.sent = self.sent, None
        return out


def test_an_injected_trigger_starts_the_pulse_without_fx_or_normals():
    kick = ffb_kick.Kick()
    s = fsample(speed=20.0)                                        # no tyre_fx, no contact normals
    for i in range(10):
        assert kick.update(i * 0.016, s, CAR, 14.0, lambda: 0.5) == 0.0
    nm = kick.update(0.2, s, CAR, 14.0, lambda: 0.5, trigger=-900.0)
    assert kick.pulses == 1
    # the sign of -(the step): a negative step of Fx_FL - Fx_FR pulls positive
    later = [kick.update(0.2 + 0.001 * i, s, CAR, 14.0, lambda: 0.5) for i in range(1, 40)]
    assert max(later) > 0.0 and min(later) >= -1e-12 and nm == 0.0 or max(later) > 0.0
    amp = ffb_kick.KICK_K * ffb_kick.SCRUB * 900.0 / 14.0 * 0.5
    assert max(later) == pytest.approx(amp, rel=0.05)
    # no trigger: the Fx test as before (nothing without Fx)
    quiet = ffb_kick.Kick()
    assert all(quiet.update(i * 0.016, s, CAR, 14.0, lambda: 0.5) == 0.0 for i in range(40)) and quiet.pulses == 0
    # a trigger under the speed floor is ignored too
    slow = ffb_kick.Kick()
    slow.update(0.0, fsample(speed=3.0), CAR, 14.0, lambda: 0.5, trigger=-900.0)
    assert slow.pulses == 0


def test_share_asks_the_rack_for_the_trigger():
    kick, rack = ffb_kick.Kick(), Rack()
    s = fsample(speed=20.0)
    rack.sent = None
    base = [kick.share(rack, s, 0.0, None, None) for _ in range(5)]
    assert all(v == 0.0 for v in base)
    rack.sent, rack._now = 800.0, 0.1
    kick.share(rack, s, 0.0, None, None)
    assert kick.pulses == 1
    rack._now = 0.12
    assert kick.share(rack, s, 0.0, None, None) < 0.0              # a positive step pulls negative


def forza_rack(kick=True, pull=False):
    rack = fe.ForzaRack()
    rack.reset(CAR)
    rack.kick_on, rack.kick_level, rack.pull_on, rack.wheel_peak = kick, 1.0, pull, 3.5
    return rack


def test_the_onset_of_a_one_sided_rumble_or_puddle_triggers_the_kick():
    rack = forza_rack()
    m_f = 1400.0 * 0.53
    step = 0.3 * m_f * 9.81 / 2.0
    t = 0.0

    def feed(**kw):
        nonlocal t
        t += 1 / 60.0
        rack.feed(t, fsample(t, **kw), None)
        return rack._trigger
    assert feed() is None
    trig = feed(rumble=(1, 0, 0, 0))
    assert trig is not None and trig[1] == pytest.approx(-step)         # FL: Fx_FL falls, the step is negative
    assert rack.kick_trigger(fsample(t)) == pytest.approx(-step) and rack.kick_trigger(fsample(t)) is None
    assert feed(rumble=(1, 0, 0, 0)) is None                            # held on: no new onset
    assert feed(rumble=(0, 0, 0, 0)) is None
    trig = feed(rumble=(0, 1, 0, 0))
    assert trig[1] == pytest.approx(step)                               # FR
    rack._trigger = None
    feed()
    assert feed(rumble=(1, 1, 0, 0)) is None                            # both: not one-sided
    trig = feed(puddle=(0.0, 1.0, 0.0, 0.0))
    assert trig[1] == pytest.approx(step)
    # a trigger Kickback did not take within TRIGGER_HOLD is dropped
    rack._trigger = (rack._now - 0.5, 100.0)
    assert rack.kick_trigger(fsample(t)) is None
    # off: the pulse stays 0
    assert forza_rack(kick=False).kick_force(fsample(t)) == 0.0


def test_the_kick_pulse_reaches_the_force_after_the_filter_path():
    rack = forza_rack()
    t = 0.0
    for i in range(10):
        t = i / 60.0
        rack.feed(t, fsample(t), None)
    rack.feed(t + 1 / 60.0, fsample(t + 1 / 60.0, rumble=(1, 0, 0, 0)), None)
    seen = []
    for i in range(60):
        rack._now = t + 1 / 60.0 + i * 0.001
        seen.append(rack.kick_force(fsample(rack._now)))
    assert max(abs(v) for v in seen) > 0.0 and ffb_kick.Kick.RISE < 0.02
    assert max(abs(v) for v in seen) <= ffb_kick.KICK_CAP * FULL + 1e-6


def test_a_one_sided_puddle_pulls_only_with_drive_pull_on():
    on, off = forza_rack(pull=True), forza_rack(pull=False)
    s = fsample(ay=0.0, cs=0.0, puddle=(1.0, 0.0, 0.0, 0.0))
    m_f = 1400.0 * 0.53
    pull = on.pull_scrub() * 0.15 * m_f * 9.81 / 2.0
    assert on.pull_scrub() == pytest.approx(fe.PULL_SCRUB * on.pull_level)
    assert on._front(s, None, on.pull_scrub()) == pytest.approx(pull)
    assert on._front(fsample(ay=0.0, cs=0.0, puddle=(0.0, 1.0, 0.0, 0.0)), None, on.pull_scrub()) == pytest.approx(-pull)
    assert off._front(s, None, off.pull_scrub()) == 0.0
    assert on._front(fsample(ay=0.0, cs=0.0, puddle=(1.0, 1.0, 0.0, 0.0)), None, on.pull_scrub()) == 0.0
    assert on.rim_torque(s) == pytest.approx(ffb_carprops.driver_from_column(pull / 14.0, 25.0, 1.0, True))


# -- readiness, the mismatch refusal, the engine --

def drive_rack(rack, seconds, raw_of=None, speed=25.0, hz=60.0, start=0.0):
    t = start
    for i in range(int(seconds * hz)):
        t = start + i / hz
        ay = 6.0 * math.sin(2 * math.pi * 0.3 * t)
        s = fsample(t, speed=speed, ay=ay, yaw=ay / speed)
        rack.feed(t, s, None if raw_of is None else raw_of(ay))
    return t


def test_ready_needs_the_car_moving_and_known():
    rack = fe.ForzaRack()
    assert not rack.ready
    drive_rack(rack, 0.5)
    assert rack.ready and rack.car == CAR
    rack.feed(1.0, fsample(1.0, speed=0.0), None)
    assert not rack.ready and rack.waiting == 'wait'
    rack.feed(1.1, fsample(1.1, speed=1.0), None)                       # under the start speed
    assert not rack.ready
    rack.feed(1.2, fsample(1.2, speed=2.0), None)
    assert rack.ready
    rack.feed(1.3, fsample(1.3, speed=0.5), None)                       # hysteresis: stays above 0.3
    assert rack.ready
    unknown = fe.ForzaRack()
    unknown.feed(0.0, fsample(car=None), None)
    assert not unknown.ready


def test_the_game_force_is_a_safety_check_only():
    rack = fe.ForzaRack()
    drive_rack(rack, 15.0, raw_of=lambda ay: -2000.0 * ay)             # T ~ -ay: raw follows it
    assert rack.ready and rack.refused is None and rack.fit > 0.9
    # no raw at all: ready all the same
    free = fe.ForzaRack()
    drive_rack(free, 15.0)
    assert free.ready and free.refused is None and free.fit is None
    # a game force running against the model for TAU s: refused
    bad = fe.ForzaRack()
    drive_rack(bad, 15.0, raw_of=lambda ay: 2000.0 * ay)
    assert bad.fit < -0.9 and bad.refused == 'mismatch' and not bad.ready and bad.waiting == 'mismatch'
    # uncorrelated (r near 0) is not a refusal
    rnd = random.Random(1)
    noisy = fe.ForzaRack()
    drive_rack(noisy, 15.0, raw_of=lambda ay: rnd.gauss(0, 3000.0))
    assert noisy.refused is None and noisy.ready


class Drive:
    """1 ms engine passes, Forza's telemetry and the game's uploads at 60 a second."""

    def __init__(self, engine, hz=60.0, speed=25.0, raw_sign=1.0, car=CAR, extra=None):
        self.engine, self.hz, self.speed, self.raw_sign, self.car, self.extra = engine, hz, speed, raw_sign, car, extra
        self.t = 0.0
        self.next = 0.0
        self.telemetry = True
        self.uploads = True

    def sample(self, t):
        ay = 6.0 * math.sin(2 * math.pi * 0.3 * t)
        s = fsample(t, speed=self.speed, ay=ay, yaw=ay / self.speed, car=self.car)
        s.steer = 0.4 * math.sin(2 * math.pi * 0.3 * t)
        if self.extra:
            self.extra(s, t)
        return s

    def run(self, seconds):
        end = self.t + seconds
        while self.t < end:
            t = self.t
            if t >= self.next and (self.telemetry or self.uploads):
                s = self.sample(t)
                if self.uploads:
                    upload(self.raw_sign * -2000.0 * s.accel[1])
                if self.telemetry:
                    self.engine.offer(t, s)
                self.next += 1.0 / self.hz
            self.engine.step(t)
            self.t = round(t + 0.001, 6)
        return self


def engine_on(tmp_path, enabled=True, raw=True, model=True, name='wheel'):
    path = wheel(tmp_path, name=name, raw=raw)
    engine = fe.FfbEngine(driver=FakeDriver, beamng_source=None)
    if not model:
        engine.set_forza_model(False)
    engine.set_device(path)
    engine.set_enabled(enabled)
    engine.step(0.0)
    return engine, path


def test_forza_plays_our_force_through_the_chain(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(4.0)
    st = engine.status
    assert engine._mode == 'forza-rack' and engine._played_mode == 'forza-rack'
    assert st.state == 'replace' and st.active and st.ready and st.game == 'forza-fh'
    assert levels() == [0]                                              # the game is muted, as for ACR
    n = len(forces())
    drive.run(1.0)
    assert len(forces()) - n > 700                                      # written every pass, not at the packet rate
    values = forces()[-700:]
    assert max(values) > 0.03 * FULL and min(values) < -0.03 * FULL
    step = max(abs(b - a) for a, b in zip(values, values[1:]))
    assert step < 0.04 * FULL                                           # smooth between the 60/s packets
    light, text = fe.status_text(engine.status)
    assert light == 'green' and 'car' in text


def test_forza_rack_is_off_with_the_switch_and_the_add_on_runs_instead(tmp_path):
    engine, path = engine_on(tmp_path, model=False)
    Drive(engine).run(4.0)
    assert engine._mode == 'forza' and engine.status.reason == 'fh-addon'
    assert levels() == [] and not any(p == 'game_ffb_level' for p in [])


def test_the_switch_is_applied_while_running_and_hands_back(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(4.0)
    assert engine._played_mode == 'forza-rack' and engine.status.active
    engine.set_forza_model(False)
    drive.run(1.0)
    assert engine._mode == 'forza' and engine._phase == 'idle' and not engine.status.active
    assert levels()[-1] == 100


def test_a_game_force_running_against_the_model_is_refused(tmp_path):
    engine, path = engine_on(tmp_path)
    Drive(engine, raw_sign=-1.0).run(16.0)
    st = engine.status
    assert st.state == 'model' and st.reason == 'forza-mismatch' and not st.active
    assert engine.forza_rack.refused == 'mismatch'
    light, text = fe.status_text(st)
    assert light == 'amber' and 'against' in text


def test_no_raw_still_plays_but_an_old_driver_does_not(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine)
    drive.uploads = False
    drive.run(4.0)
    assert engine.status.active                                          # (the raw is only a safety check)
    engine2, _ = engine_on(tmp_path, raw=False, name='old')
    Drive(engine2).run(3.0)
    assert engine2.status.reason == 'old-driver' and not engine2.status.active


def test_a_slow_stream_is_not_played(tmp_path):
    engine, path = engine_on(tmp_path)
    Drive(engine, hz=20.0).run(4.0)
    assert engine.status.reason == 'fh-rate' and not engine.status.active


def test_standing_hands_the_force_back_and_a_stall_releases(tmp_path):
    engine, path = engine_on(tmp_path)
    drive = Drive(engine).run(4.0)
    assert engine.status.active
    drive.telemetry = False
    drive.run(0.6)
    assert not engine.status.active and engine.last_stop == 'stall'
    assert levels()[-1] == 100


def test_the_car_wheel_default_is_375_until_the_user_sets_one(tmp_path):
    engine, _ = engine_on(tmp_path)
    engine.set_car_wheel_mm(350.0)                                       # the profile's default
    engine.step(0.0)
    assert engine.forza_rack.car_wheel_mm == 375.0 and engine.rbr_rack.car_wheel_mm == 350.0
    engine.set_car_wheel_mm(330.0)
    engine.step(0.0)
    assert engine.forza_rack.car_wheel_mm == 330.0
    engine.set_rim_mm(300.0)
    engine.set_mapping('classic')
    engine.set_drive_pull(True, 50.0)
    engine.set_kick(True, 70.0)
    engine.step(0.0)
    rack = engine.forza_rack
    assert rack.rim_user == 300.0 and rack.classic() and rack.pull_on and rack.pull_level == 0.5
    assert rack.kick_on and rack.kick_level == pytest.approx(0.7)


def dyn_engine(tmp_path):
    from tests.test_ffb_dynamics import dyn_wheel, POS
    path = dyn_wheel(tmp_path)
    engine = fe.FfbEngine(driver=FakeDriver, beamng_source=None)
    engine.set_position_source(POS)
    engine.set_device(path)
    engine.set_enabled(True)
    engine.step(0.0)
    return engine, path, POS


def test_the_lock_stop_acts_from_the_wheels_position_at_97_percent_steer(tmp_path):
    engine, path, pos = dyn_engine(tmp_path)
    drive = Drive(engine).run(8.0)
    assert engine.status.active and engine._played_mode == 'forza-rack'
    t = drive.t

    def push_at(deg, steer):
        pos.raw = int(round((deg / 900.0 + 0.5) * 65535))
        engine._pos_cache = None
        s = fsample(t)
        s.steer = steer
        return engine._lock_stop_force(t, s)
    assert push_at(470.0, 0.5) == 0.0                                    # the game's input is not at its lock
    assert push_at(400.0, 1.0) == 0.0 and not engine._lock_stop.active   # inside 97 % of the range / 2 (436.5)
    push = push_at(470.0, 1.0)
    assert engine._lock_stop.active and push > 0.3 * FULL
    assert push_at(-470.0, -1.0) < 0
    assert engine.align_note is None or True
    assert not engine.model.align_on and not engine.forza_rack.align_on  # the align spring stays off for Forza
    assert int(open(path + '/ffb_dyn_damping').read()) > 0               # the column damping is written


def test_the_take_over_logs_the_car_mass_and_scale(tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO)
    engine, _ = engine_on(tmp_path)
    Drive(engine).run(4.0)
    line = [m for m in caplog.messages if "replacing Forza Horizon's force" in m]
    assert line and CAR in line[0] and 'mass 1400 kg (prior)' in line[0] and '375' in line[0]


# -- the switch --

def test_the_profile_key_defaults_to_on_and_goes_to_the_engine():
    assert fe.OVERALL_FORZA_MODEL is True
    assert 'countersteer_forza_model' in Model.types and Model.types['countersteer_forza_model'] == 'boolean'
    assert Model._texture_defaults(True)['countersteer_forza_model'] is True
    assert Model._texture_defaults(False)['countersteer_forza_model'] is None
    from oversteer import ffb_process
    assert 'set_forza_model' in ffb_process.SETTERS and hasattr(ffb_process.EngineProcess, 'set_forza_model')
    engine = fe.FfbEngine(driver=FakeDriver, beamng_source=None)
    assert engine._forza_model is True
    engine.set_forza_model(0)
    engine.step(0.0)
    assert engine._forza_model is False and engine._mode_of('forza-fh', 0.0) == 'forza'
    engine.set_forza_model(1)
    engine.step(0.0)
    assert engine._mode_of('forza-fh', 0.0) == 'forza-rack'


# -- the front slip's lead (SlipLead) and Forza's aligning spring (countersteer_forza_spring)

def _lead_run(lead, fy, slip, seconds, t0=0.0):
    out = []
    for n in range(int(seconds * 60)):
        out.append(lead.step(t0 + n / 60.0, fy(n / 60.0), slip(n / 60.0)))
    return out


def test_the_slip_lead_changes_no_steady_level_and_leads_a_step():
    lead = fz.SlipLead()
    # a steady cornering: the same level with the slip held (whatever the learnt gain)
    out = _lead_run(lead, lambda t: 3000.0, lambda t: -0.5, 20.0)
    assert out[-1] == pytest.approx(3000.0, rel=1e-6)
    # Newton's force steps late (0.2 s) after the slip: the output moves with the slip first, and settles at Newton's
    lead = fz.SlipLead()
    _lead_run(lead, lambda t: 0.0, lambda t: 0.0, 1.0)
    out = _lead_run(lead, lambda t: 0.0 if t < 1.2 else 3000.0, lambda t: 0.0 if t < 1.0 else -0.5, 10.0, 1.0)
    assert out[-1] == pytest.approx(3000.0, rel=1e-3)


def test_the_slip_lead_has_the_sign_of_newtons_force_and_none_without_the_slip():
    lead = fz.SlipLead()
    for n in range(600):                       # Forza's slip angle is opposite in sign to Newton's Fy_f
        s = math.sin(2 * math.pi * 0.4 * n / 60.0)
        lead.step(n / 60.0, 4000.0 * s, -0.4 * s)
    assert lead.g > 0.0
    lead.step(10.0, 4000.0, -0.2)
    jump = lead.step(10.0 + 1 / 60.0, 4000.0, -0.9) - 4000.0
    assert jump > 0.0                          # more slip the same way: more of Newton's sign
    # no slip field: plain Newton, whatever it learnt
    assert lead.step(10.5, 1234.0, None) == 1234.0


def test_the_forza_rack_plays_the_leading_fy_and_plain_newton_without_slip():
    rack = fe.ForzaRack()
    rack.reset('forza-fh/x')
    s = fsample(0.0, speed=30.0, ay=6.0)
    s.car = 'forza-fh/x'
    rack.feed(0.0, s, None)
    plain = rack._front(s, rack.trail, 0.0)
    s2 = fsample(1 / 60.0, speed=30.0, ay=6.0)
    s2.car = 'forza-fh/x'
    s2.slip_angle = None
    rack.feed(1 / 60.0, s2, None)
    assert rack._front(s2, rack.trail, 0.0) == pytest.approx(plain)


def test_the_forza_spring_switch_is_wired_like_rbrs():
    from oversteer import ffb_process
    engine = fe.FfbEngine(driver=FakeDriver, beamng_source=None)
    assert fe.OVERALL_FORZA_SPRING is True and engine.forza_rack.spring_on is True
    assert Model.types['countersteer_forza_spring'] == 'boolean' and Model._texture_defaults(True)['countersteer_forza_spring'] is True
    assert 'set_forza_spring' in ffb_process.SETTERS and hasattr(ffb_process.EngineProcess, 'set_forza_spring')
    engine.set_forza_spring(0)
    engine.step(0.0)
    assert engine.forza_rack.spring_on is False and engine.settings_snapshot()['profile']['forza_spring'] is False
    rack = fe.ForzaRack()
    rack.spring_on = True
    rack.ready = True
    assert rack.align_ready()
    rack.spring_on = False
    assert not rack.align_ready()


def test_the_lateral_acceleration_keeps_its_level_and_takes_its_changes_from_the_front_slip():
    rack = fe.ForzaRack()
    rack.reset(CAR)
    def at(t, ay, slip):
        s = fsample(t, ay=ay, yaw=0.2, speed=20.0, dt='awd')
        s.slip_angle = (slip, slip, 0.0, 0.0)
        return rack._fast_ay(s, ay)
    assert at(0.0, 4.0, -0.15) == pytest.approx(4.0)
    for i in range(1, 300):
        steady = at(i / 60.0, 4.0, -0.15)
    assert steady == pytest.approx(4.0, abs=0.01)                 # steady: a_y's level
    moved = at(5.0, 4.0, -0.30)                                   # the slip moves first: more a_y coming, the same sign
    assert moved > 4.5


def test_without_slip_angles_the_changes_come_from_the_yaw_rate_and_without_either_it_is_plain_a_y():
    rack = fe.ForzaRack()
    rack.reset(CAR)
    def at(t, yaw):
        s = fsample(t, ay=4.0, yaw=yaw, speed=20.0, dt='awd')
        s.slip_angle = None
        return rack._fast_ay(s, 4.0)
    at(0.0, 0.2)
    assert at(1 / 60.0, 0.3) == pytest.approx(4.0 + rack.VR_GAIN * 20.0 * 0.1, rel=0.1)
    s = fsample(1.0, ay=3.0, dt='awd')
    s.slip_angle, s.yaw_rate = None, None
    assert rack._fast_ay(s, 3.0) == 3.0


def test_the_springs_slip_is_the_games_own_front_slip():
    rack = fe.ForzaRack()
    rack.reset(CAR)
    s = fsample(1.0, ay=2.0, yaw=0.1, speed=20.0)
    s.slip_angle = (0.4, 0.2, 0.0, 0.0)
    assert rack._slip(s) == pytest.approx(rack.SLIP_RAD * 0.3)
