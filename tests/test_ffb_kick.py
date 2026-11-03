import math
from types import SimpleNamespace

from oversteer import ffb_kick, wheel_torque
from oversteer.ffb_kick import Kick, KICK_CAP, KICK_K, SCRUB, TOTAL_CAP

DT = 0.004
RATIO = 13.0


def sample(fx=0.0, angle=0.0, speed=30.0, load=(2000.0, 2000.0)):
    """`fx` is the pulse's direction: Fx_FL - Fx_FR = -fx (the pulse has the sign of -(the Fx step))."""
    return SimpleNamespace(speed=speed, tyre_fx=(-fx, 0.0), tyre_load=load,
                           contact_normal=((0.0, 0.0, 1.0), (math.sin(angle), 0.0, math.cos(angle))))


def run(kick, steps, slope=0.5, car='car'):
    """steps: (seconds, dFx, angle) held for that long; returns [(t, Nm)] at 250 Hz, starting with 0.2 s of steady."""
    out = []
    t = 0.0
    for secs, fx, angle, *rest in [(0.2, 0.0, 0.0)] + list(steps):
        speed = rest[0] if rest else 30.0
        for _ in range(int(round(secs / DT))):
            out.append((t, kick.update(t, sample(fx, angle, speed), car, RATIO, lambda: slope)))
            t += DT
    return out


def amp(dfx, slope=0.5):
    return KICK_K * SCRUB * abs(dfx) / RATIO * (1.0 - slope)


def test_sign_follows_minus_the_fx_step_and_the_amplitude_is_the_formula():
    raw = Kick()
    for i in range(100):                                   # Fx_FL - Fx_FR steps UP by 4000 N: the pulse is negative
        v = raw.update(i * DT, SimpleNamespace(speed=30.0, tyre_fx=(4000.0 if i >= 50 else 0.0, 0.0), tyre_load=(2000.0, 2000.0),
                                               contact_normal=((0, 0, 1), (math.sin(0.3 if i >= 50 else 0.0), 0, math.cos(0.3 if i >= 50 else 0.0)))),
                       'car', RATIO, lambda: 0.5)
    assert v < 0.0
    up = run(Kick(), [(0.4, 4000.0, 0.3)])
    down = run(Kick(), [(0.4, -4000.0, 0.3)])
    assert max(v for _, v in up) == max(abs(v) for _, v in up) > 0
    assert min(v for _, v in up) >= 0.0
    assert min(v for _, v in down) == -max(abs(v) for _, v in down) < 0
    assert abs(max(abs(v) for _, v in up) - amp(4000.0)) < 1e-6


def test_pulse_shape_peaks_about_12_ms_after_the_trigger_then_decays_monotonically_without_crossing_zero():
    out = run(Kick(), [(0.6, 4000.0, 0.3)])
    vals = [v for _, v in out]
    i_peak = max(range(len(vals)), key=lambda i: vals[i])
    t_trig = next(t for (t, v), (_, nxt) in zip(out, out[1:]) if t >= 0.2 and nxt > 0)
    assert abs(out[i_peak][0] - t_trig - Kick.RISE) <= 2 * DT
    after = vals[i_peak:]
    assert all(b <= a + 1e-12 for a, b in zip(after, after[1:]))
    assert min(vals) >= 0.0
    # exponential decay: about 1/e of the peak one tau after the rise
    j = i_peak + int(round(Kick.DECAY / DT))
    assert abs(vals[j] / vals[i_peak] - math.exp(-1.0)) < 0.05


def test_a_step_up_then_down_within_a_pulse_never_takes_the_opposite_sign_and_the_release_triggers_nothing():
    kick = Kick()
    out = run(kick, [(0.06, 4000.0, 0.3), (0.3, 0.0, 0.6)])
    assert min(v for _, v in out) >= 0.0
    assert kick.pulses == 1


def test_an_opposite_trigger_waits_until_the_pulse_is_under_10_percent():
    kick = Kick()
    out = run(kick, [(0.06, 4000.0, 0.3), (0.30, 0.0, 0.3)])      # pulse started, decays
    first_peak = max(v for _, v in out)
    # ... the release 0.25 s later (pulse under 10 % by 0.15 s): an opposite pulse is allowed
    out += run_more(kick, out[-1][0] + DT, [(0.2, -4000.0, 0.6)])
    assert kick.pulses == 2
    assert min(v for _, v in out) < 0.0
    assert min(v for _, v in out) > -first_peak - 1e-9


def run_more(kick, t0, steps):
    out = []
    t = t0
    for secs, fx, angle in steps:
        for _ in range(int(round(secs / DT))):
            out.append((t, kick.update(t, sample(fx, angle), 'car', RATIO, lambda: 0.5)))
            t += DT
    return out


def test_a_bigger_step_in_the_same_direction_raises_the_pulse():
    kick = Kick()
    out = run(kick, [(0.02, 3000.0, 0.3), (0.3, 6000.0, 0.6)])
    assert kick.pulses == 1
    assert max(v for _, v in out) > amp(3000.0) * 1.2
    assert min(v for _, v in out) >= 0.0


def test_no_trigger_without_the_fx_step_or_under_8_ms():
    assert all(v == 0.0 for _, v in run(Kick(), [(0.4, 500.0, 0.3)]))                # contact, but no Fx step
    assert all(v == 0.0 for _, v in run(Kick(), [(0.4, 4000.0, 0.0)]))               # Fx step, but no contact event
    assert all(v == 0.0 for _, v in run(Kick(), [(0.4, 4000.0, 0.3, 7.0)]))          # too slow


def test_a_lift_onset_triggers():
    kick = Kick()
    out = []
    t = 0.0
    for i in range(100):
        lifted = i >= 50
        out.append(kick.update(t, sample(4000.0 if lifted else 0.0, 0.0, 30.0, (100.0, 3000.0) if lifted else (2000.0, 2000.0)),
                               'car', RATIO, lambda: 0.5))
        t += DT
    assert max(out) > 0.0


class Rack(SimpleNamespace):
    def power_steering(self):
        return 'model'

    def assist_level(self):
        return 1.0

    def classic(self):
        return False


def rack(on=True, level=1.0, strength=1.0):
    return Rack(kick_on=on, kick_level=level, strength=strength, ratio=RATIO, assist=1.0, wheel_peak=2.1, car='car',
                trail=0.027, _now=0.0)


def drive_share(r, kick, base, steps=((0.6, 6000.0, 0.3),)):
    out = []
    t = 0.0
    for secs, fx, angle in [(0.2, 0.0, 0.0)] + list(steps):
        for _ in range(int(round(secs / DT))):
            r._now = t
            s = sample(fx, angle)
            s.tyre_fy, s.tyre_mz = (0.0, 0.0), (0.0, 0.0)
            out.append(kick.share(r, s, base, None, None))
            t += DT
    return out


def test_off_and_level_zero_give_exactly_the_input():
    assert all(v == 0.2 for v in drive_share(rack(on=False), Kick(), 0.2))
    assert all(v == 0.2 for v in drive_share(rack(level=0.0), Kick(), 0.2))


def test_the_pulse_is_capped_and_the_total_clamped():
    r = rack(level=1.0, strength=10.0)
    out = drive_share(r, Kick(), 0.0)
    assert abs(max(out) - KICK_CAP) < 1e-9 and min(out) >= 0.0
    out = drive_share(r, Kick(), wheel_torque.STEER_BUDGET)
    assert abs(max(out) - TOTAL_CAP) < 1e-9
    assert TOTAL_CAP == wheel_torque.STEER_BUDGET + KICK_CAP
    out = drive_share(r, Kick(), -wheel_torque.STEER_BUDGET, steps=((0.6, -6000.0, 0.3),))
    assert abs(min(out) + TOTAL_CAP) < 1e-9


def test_the_level_scales_the_amplitude():
    full = max(drive_share(rack(level=1.0), Kick(), 0.0, steps=((0.6, 1500.0, 0.3),)))
    half = max(drive_share(rack(level=0.5), Kick(), 0.0, steps=((0.6, 1500.0, 0.3),)))
    assert 0.0 < half < KICK_CAP and abs(half - 0.5 * full) < 1e-9


def test_a_sample_gap_or_a_car_change_resets():
    kick = Kick()
    run(kick, [(0.03, 4000.0, 0.3)])
    assert kick._active
    kick.update(10.0, sample(), 'car', RATIO, lambda: 0.5)
    assert not kick._active
    run(kick, [(0.03, 4000.0, 0.3)])
    kick.update(10.1, sample(), 'other', RATIO, lambda: 0.5)
    assert not kick._active
