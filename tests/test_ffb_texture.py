"""Countersteer FFB texture (oversteer/ffb_texture.py) and its mix into the engine's replace path: zero mean,
the caps under any input, the steering force bit for bit when off, the band-pass on the suspension, the lock
cue only braking with the fronts locked, the landing pulse, and the engine's fade gain over it. Nothing here
touches a real wheel or /sys."""
import math
import random

import pytest

from oversteer import ffb_engine as fe, ffb_texture as ft
from oversteer.telemetry_formats import Sample
from tests.test_ffb_engine import FakeDriver, _clean_log, acr, engine_on, forces, game_raw, upload  # noqa: F401

HZ = 333.0
DT = 1.0 / HZ
FULL = ft.FULL
UP = (0.0, 1.0, 0.0)


def car(speed=20.0, susp=(0.1, 0.1, 0.1, 0.1), load=(3000.0, 3000.0, 2500.0, 2500.0), normal=UP, brake=0.0,
        rot=None, game='acr', vel=True):
    s = Sample(6000.0, 7500.0, gear=3, speed=speed, car='acr/Skoda Fabia RS Rally2', game=game, brake=brake)
    s.susp, s.tyre_load = susp, load
    s.contact_normal = None if normal is None else (normal, normal, UP, UP)
    radius = 0.32
    s.wheel_rot = rot if rot is not None else (speed / radius,) * 4
    s.vel = (speed, 0.0, 0.0) if vel else None
    return s


def texture(level=1.0, **parts):
    tx = ft.Texture()
    tx.set(level, {name: parts.get(name, False) for name in ft.PARTS})
    return tx


def drive(tx, samples, steering=8000.0, t0=0.0):
    """Texture values for (sample) at 333 Hz, from t0."""
    out = []
    for i, s in enumerate(samples):
        out.append(tx.force(t0 + i * DT, s, steering))
    return out


def rough(seconds, seed=1, amp=0.002, roll=0.0, roll_hz=1.0):
    """Gravel: independent random bumps under each front wheel (amp m per step, smoothed a little) and, if
    asked, body roll of `roll` m at roll_hz."""
    rnd = random.Random(seed)
    fl = fr = 0.0
    out = []
    for i in range(int(seconds * HZ)):
        t = i * DT
        fl = 0.7 * fl + rnd.gauss(0.0, amp)
        fr = 0.7 * fr + rnd.gauss(0.0, amp)
        body = roll * math.sin(2 * math.pi * roll_hz * t)
        out.append(car(susp=(0.1 + fl + body, 0.1 + fr - body, 0.1, 0.1)))
    return out


def mean_rms(values):
    mean = sum(values) / len(values)
    rms = math.sqrt(sum(v * v for v in values) / len(values))
    return mean, rms


# -- the parts --

def test_road_texture_averages_to_zero_and_stays_under_its_cap():
    tx = texture(road=True)
    out = drive(tx, rough(20.0))
    mean, rms = mean_rms(out[int(HZ):])
    assert rms > 0.004 * FULL                          # there is texture ...
    assert abs(mean) < 0.02 * rms                      # ... and it never pulls
    assert max(abs(v) for v in out) <= ft.TOTAL_CAP * FULL
    road = ft.RoadTexture()
    assert max(abs(road.step(DT, x)) for x in rough(5.0, amp=0.05)) <= ft.RoadTexture.CAP * FULL


def kerb(seconds, hz=12.0, depth=0.03):
    """A kerb's sawtooth under FL only: pushed up quickly (20 % of the period), let down slowly."""
    out = []
    for i in range(int(seconds * HZ)):
        phase = (i * DT * hz) % 1.0
        up = phase / 0.2 if phase < 0.2 else 1.0 - (phase - 0.2) / 0.8
        out.append(car(susp=(0.1 + depth * up, 0.1, 0.1, 0.1)))
    return out


def test_a_lopsided_input_never_pulls():
    """The road part alone (tanh after the band-pass) turns the sawtooth into a steady pull; the sum's DC
    blocker takes it out."""
    road = ft.RoadTexture()
    raw = [road.step(DT, x) for x in kerb(6.0)][int(HZ):]
    raw_mean, raw_rms = mean_rms(raw)
    assert abs(raw_mean) > 0.05 * raw_rms                       # the pull is there before the blocker
    out = drive(texture(1.0, road=True), kerb(6.0))[int(2 * HZ):]
    mean, rms = mean_rms(out)
    assert rms > 0.002 * FULL
    assert abs(mean) < 0.01 * rms and abs(mean) < 0.0005 * FULL


def test_it_stays_within_the_headroom_left_by_the_steering_force():
    for steering in (32767.0, 32500.0, -32700.0):
        out = drive(texture(1.0, road=True, impact=True, lock=True), rough(3.0, amp=0.02), steering=steering)
        assert max(abs(v) for v in out) <= 32767.0 - abs(steering) + 1e-6
    assert all(v == 0.0 for v in drive(texture(1.0, road=True), rough(1.0, amp=0.02), steering=32767.0))


def test_a_new_car_starts_the_impact_mean_again():
    impact = ft.ImpactPulse()
    for _ in range(int(HZ)):
        impact.step(DT, car(load=(9000.0, 9000.0, 0.0, 0.0)))
    s = car(load=(2000.0, 2000.0, 0.0, 0.0))
    s.car = 'acr/Another'
    impact.step(DT, s)
    assert impact._mean == pytest.approx(4000.0)


def test_the_high_pass_removes_slow_suspension_motion():
    """Body roll at 1.5 Hz (5 cm each way, far more travel than any bump) against a 20 Hz bump of the same
    velocity: the roll is 20+ dB down."""
    def response(hz, amp):
        road = ft.RoadTexture()
        samples = [car(susp=(0.1 + amp * math.sin(2 * math.pi * hz * i * DT), 0.1, 0.1, 0.1))
                   for i in range(int(4 * HZ))]
        values = [road.step(DT, s) for s in samples]
        return max(abs(v) for v in values[int(2 * HZ):])
    slow = response(1.5, 0.05)
    fast = response(20.0, 0.05 * 1.5 / 20.0)            # the same peak velocity
    assert fast > 0.1 * ft.RoadTexture.CAP * FULL
    assert slow < 0.1 * fast
    # Both wheels moving together (heave, a symmetric bump) does not steer
    road = ft.RoadTexture()
    heave = [road.step(DT, car(susp=(0.1 + 0.01 * math.sin(2 * math.pi * 20 * i * DT),) * 2 + (0.1, 0.1)))
             for i in range(int(HZ))]
    assert max(abs(v) for v in heave) == 0.0


def test_a_suspension_reset_or_nan_never_spikes_it():
    road = ft.RoadTexture()
    values = [road.step(DT, car(susp=(0.1, 0.1, 0.1, 0.1))) for _ in range(50)]
    values.append(road.step(DT, car(susp=(30.0, -30.0, 0.1, 0.1))))       # a teleport: 18000 m/s
    values.append(road.step(DT, car(susp=(float('nan'), 0.1, 0.1, 0.1))))
    values.append(road.step(DT, car(susp=None)))
    values += [road.step(DT, car(susp=(0.1, 0.1, 0.1, 0.1))) for _ in range(50)]
    assert all(math.isfinite(v) and abs(v) <= ft.RoadTexture.CAP * FULL for v in values)


def landing(t_air=0.3, t_land=1.0, seconds=2.0, left=6000.0, right=6000.0, normal_step=False, fx=None):
    """Rolling, airborne (no front load) from t_air to t_land, then down hard (loads to left/right)."""
    out = []
    for i in range(int(seconds * HZ)):
        t = i * DT
        if t < t_air:
            load, normal = (3000.0, 3000.0, 2500.0, 2500.0), UP
        elif t < t_land:
            load, normal = (0.0, 0.0, 0.0, 0.0), UP
        else:
            k = min((t - t_land) / 0.02, 1.0)
            load = (k * left, k * right, 2500.0, 2500.0)
            normal = UP
        s = car(load=load, normal=normal)
        if fx is not None:               # (Fx_FL, Fx_FR) from touchdown on; before it the wheels roll free
            s.tyre_fx = fx if t >= t_land else (0.0, 0.0, 0.0, 0.0)
            s.tyre_fx = (s.tyre_fx[0], s.tyre_fx[1], 0.0, 0.0)
        out.append(s)
    return out


def test_a_landing_gives_one_zero_area_pulse():
    impact = ft.ImpactPulse()
    values = [impact.step(DT, s) for s in landing()]
    assert impact.count == 1
    assert max(abs(v) for v in values) <= ft.ImpactPulse.CAP * FULL
    assert max(abs(v) for v in values) > 0.1 * ft.ImpactPulse.CAP * FULL
    # Zero area: the pulse's sum is a small part of its size times its length
    area = sum(values) * DT
    assert abs(area) < 0.01 * max(abs(v) for v in values) * ft.ImpactPulse.LENGTH
    # Starts from nothing (no step)
    start = next(i for i, v in enumerate(values) if v != 0.0)
    assert abs(values[start]) < 0.6 * max(abs(v) for v in values)     # a half-sine, 18 ms long: one 3 ms step in


def pulse(**kw):
    impact = ft.ImpactPulse()
    return [impact.step(DT, s) for s in landing(**kw)], impact


def test_the_pulse_pushes_the_way_the_fx_step_says_with_a_thin_return():
    left, _ = pulse(fx=(-1500.0, 0.0))               # the left front drags: Fx_FL - Fx_FR steps by -1500
    right, _ = pulse(fx=(0.0, -1500.0))
    first = next(v for v in left if abs(v) > 50)
    assert first > 0 and next(v for v in right if abs(v) > 50) < 0     # sign of -SCRUB * step
    peak = max(abs(v) for v in left)
    lead = [v for v in left if v > 0]
    back = [v for v in left if v < 0]
    assert max(lead) == peak                         # the largest lobe is the one with the sign
    assert abs(min(back)) <= 0.25 * peak             # the return is spread thin
    assert len(back) * DT >= 0.08                    # ... over at least 80 ms
    assert abs(sum(left) * DT) < 0.03 * peak * ft.ImpactPulse.LENGTH
    assert peak <= ft.ImpactPulse.CAP * FULL
    assert [v for v in right if v != 0][0] < 0 and -min(right) == pytest.approx(peak)


def test_without_an_fx_step_the_pulse_is_symmetric_and_smaller():
    signed, _ = pulse(fx=(-1500.0, 0.0))
    for fx in (None, (-50.0, 0.0)):                  # no Fx at all, or a step under FX_STEP
        neutral, impact = pulse(fx=fx)
        assert impact.count == 1
        assert max(neutral) == pytest.approx(-min(neutral), rel=0.05)
        assert max(neutral) == pytest.approx(ft.ImpactPulse.NEUTRAL * max(signed), rel=0.05)
        assert abs(sum(neutral) * DT) < 0.03 * max(neutral) * ft.ImpactPulse.LENGTH


def test_ordinary_bumps_do_not_jolt_and_pulses_are_rate_limited():
    impact = ft.ImpactPulse()
    # The front load x4 every 0.1 s, never off the ground, the road flat: no pulse
    for i in range(int(3 * HZ)):
        k = 1.0 + 3.0 * ((i // 33) % 2)
        impact.step(DT, car(load=(3000.0 * k, 3000.0 * k, 2500.0, 2500.0)))
    assert impact.count == 0
    # The same with the contact normals stepping 15+ degrees (a ledge): one pulse per HOLD at most
    impact = ft.ImpactPulse()
    tilted = (0.0, math.cos(0.3), math.sin(0.3))
    for i in range(int(3 * HZ)):
        k = 1.0 + 3.0 * ((i // 33) % 2)
        impact.step(DT, car(load=(3000.0 * k, 3000.0 * k, 2500.0, 2500.0), normal=tilted if k > 1 else UP))
    assert 1 <= impact.count <= int(3.0 / ft.ImpactPulse.HOLD) + 1


def rolling(seconds=2.0, speed=25.0):
    return [car(speed=speed) for _ in range(int(seconds * HZ))]


def locking(seconds=1.0, speed=25.0, brake=0.8, rot_share=0.1):
    rot = (rot_share * speed / 0.32,) * 2 + (speed / 0.32,) * 2
    return [car(speed=speed, brake=brake, rot=rot) for _ in range(int(seconds * HZ))]


def test_the_lock_cue_only_braking_with_the_fronts_locked():
    lock = ft.LockCue()
    [lock.step(DT, s) for s in rolling()]                   # it learns the radius rolling free
    assert lock.radius == pytest.approx(0.32, rel=0.01)
    on = [lock.step(DT, s) for s in locking()]
    assert max(abs(v) for v in on) > 0.5 * ft.LockCue.CAP * FULL
    assert max(abs(v) for v in on) <= ft.LockCue.CAP * FULL
    mean, rms = mean_rms(on[int(0.2 * HZ):])
    assert abs(mean) < 0.05 * rms
    # About 40 Hz: sign changes per second
    flips = sum(1 for a, b in zip(on, on[1:]) if a * b < 0)
    assert 70 <= flips <= 90
    # Locked but off the brake (a jump, the handbrake): nothing
    off = [lock.step(DT, s) for s in locking(brake=0.0)]
    assert max(abs(v) for v in off[int(0.5 * HZ):]) == 0.0
    # Braking but rolling (under the slip threshold): nothing
    rolling_brake = [lock.step(DT, s) for s in locking(rot_share=0.85)]
    assert max(abs(v) for v in rolling_brake[int(0.3 * HZ):]) == 0.0
    # Slow: nothing
    slow = [lock.step(DT, s) for s in locking(speed=3.0)]
    assert max(abs(v) for v in slow[int(0.3 * HZ):]) == 0.0


def test_the_lock_cue_eases_in_and_out():
    lock = ft.LockCue()
    [lock.step(DT, s) for s in rolling()]
    on = [lock.step(DT, s) for s in locking(seconds=0.5)]
    assert abs(on[0]) < 0.2 * ft.LockCue.CAP * FULL
    off = [lock.step(DT, s) for s in rolling(0.5)]
    assert abs(off[0]) <= max(abs(v) for v in on[-10:]) + 1.0
    assert off[-1] == 0.0


def test_the_lock_cue_falls_back_on_the_games_slip_ratio_and_the_wheel_speed():
    lock = ft.LockCue()
    s = car(brake=0.9)
    s.wheel_rot = None
    s.tyre_slip_ratio = (-0.9, -0.85, 0.0, 0.0)
    assert lock.target(s) > 0.5
    s = car(brake=0.9)
    s.wheel_speed = (2.0, 2.0, 20.0, 20.0)
    assert lock.target(s) > 0.5


# -- all of it --

def test_off_is_exactly_zero_and_other_games_get_none():
    tx = texture(0.0, road=True, impact=True, lock=True)
    assert all(v == 0.0 for v in drive(tx, rough(1.0)))
    tx = texture(1.0)                                   # every part off
    assert all(v == 0.0 for v in drive(tx, rough(1.0)))
    tx = texture(1.0, road=True, impact=True, lock=True)
    assert all(v == 0.0 for v in drive(tx, [car(susp=s.susp, game='acc') for s in rough(1.0)]))
    assert ft.TEXTURE_GAMES == ('acr', 'rbr', 'forza-fh')


def chaos(seconds=5.0, seed=7):
    rnd = random.Random(seed)
    nan = float('nan')
    pick = lambda *values: rnd.choice(values)
    out = []
    for _ in range(int(seconds * HZ)):
        s = car(speed=pick(25.0, 25.0, nan, 1e9, -5.0),
                susp=pick((rnd.uniform(-1e6, 1e6), rnd.uniform(-1e6, 1e6), 0.1, 0.1), (nan, 0.1, 0.1, 0.1),
                          None, (0.1 + rnd.gauss(0, 0.01), 0.1 + rnd.gauss(0, 0.01), 0.1, 0.1)),
                load=pick((rnd.uniform(0, 1e9), rnd.uniform(0, 1e9), 0.0, 0.0), (nan, nan, nan, nan), None,
                          (0.0, 0.0, 0.0, 0.0), (20000.0, 20000.0, 0.0, 0.0)),
                normal=pick(UP, (nan, 1.0, 0.0), (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), None),
                brake=pick(0.0, 1.0, nan, None, 1e6),
                rot=pick((0.0, 0.0, 80.0, 80.0), (nan, nan, nan, nan), (1e9, -1e9, 0.0, 0.0), None))
        s.tyre_slip_ratio = pick(None, (nan, 1e9, 0, 0), (-1.0, -1.0, 0.0, 0.0))
        out.append(s)
    return out


@pytest.mark.parametrize('steering', [0.0, 300.0, 4000.0, 32767.0, float('nan'), 1e12])
def test_the_caps_hold_under_any_input(steering):
    tx = texture(1.0, road=True, impact=True, lock=True)
    values = drive(tx, chaos(), steering=steering)
    s = 0.0 if not math.isfinite(steering) else abs(steering)
    limit = min(ft.TOTAL_CAP * FULL, ft.STEER_SHARE * s + ft.FLOOR * FULL)
    assert all(math.isfinite(v) for v in values)
    assert max(abs(v) for v in values) <= limit + 1e-9


def test_the_level_scales_it_and_the_steering_force_bounds_it():
    full = drive(texture(1.0, road=True, impact=True, lock=True), rough(5.0, amp=0.02), steering=32767.0)
    low = drive(texture(0.3, road=True, impact=True, lock=True), rough(5.0, amp=0.02), steering=32767.0)
    assert max(abs(v) for v in full) <= ft.TOTAL_CAP * FULL
    assert max(abs(v) for v in low) <= 0.3 * ft.TOTAL_CAP * FULL + 1e-9
    assert max(abs(v) for v in low) == pytest.approx(0.3 * max(abs(v) for v in full), rel=0.05)
    centred = drive(texture(1.0, road=True), rough(5.0, amp=0.02), steering=0.0)
    assert max(abs(v) for v in centred) <= ft.FLOOR * FULL + 1e-6


def test_it_ramps_in_after_a_start_or_a_gap_and_slews_with_the_level():
    tx = texture(1.0, road=True)
    samples = rough(3.0, amp=0.02)
    out = drive(tx, samples)
    peak = max(abs(v) for v in out[int(HZ):])
    assert max(abs(v) for v in out[:int(0.05 * HZ)]) < 0.3 * peak       # no step at the start
    # A gap (the engine stopped): it starts again from nothing
    again = drive(tx, samples[:int(0.05 * HZ)], t0=10.0)
    assert max(abs(v) for v in again) < 0.3 * peak
    # The level dropped to 0: it slews down over SLEW, not at once
    tx = texture(1.0, road=True)
    drive(tx, samples[:int(2 * HZ)])
    tx.set(0.0)
    down = drive(tx, samples[int(2 * HZ):], t0=2 * HZ * DT)
    assert max(abs(v) for v in down[:int(0.05 * HZ)]) > 0.0
    assert all(v == 0.0 for v in down[int(1.1 * ft.SLEW * HZ):])


def test_everything_together_has_no_dc():
    tx = texture(1.0, road=True, impact=True, lock=True)
    samples = rough(5.0, amp=0.01) + rolling(1.0) + locking(1.0) + landing() + rough(5.0, seed=3, amp=0.01)
    out = drive(tx, samples)
    mean, rms = mean_rms(out)
    assert rms > 0.0
    assert abs(mean) < 0.03 * rms


# -- in the engine --

def acr_rough(t, rnd, **kw):
    s = acr(t, **kw)
    if s.packet_id is not None:
        s.susp = (0.1 + rnd.gauss(0, 0.004), 0.1 + rnd.gauss(0, 0.004), 0.1, 0.1)
        s.tyre_load = (3000.0, 3000.0, 2500.0, 2500.0)
        s.wheel_rot = (20.0 / 0.32,) * 4
        s.vel = (20.0, 0.0, 0.0)
        s.brake = 0.0
    return s


def feed(engine, t0, seconds, rnd, **kw):
    t = t0
    for _ in range(int(round(seconds * HZ))):
        s = acr_rough(t, rnd, **kw)
        if s.game_ffb is not None:
            upload(game_raw(s))
        engine.offer(t, s)
        engine.step(t)
        t += DT
    return t


def _original_play(self, t, sample):
    """FfbEngine._play without the texture (the steering force, its strength and lock stop, the glide and the
    output path as they are now), to check that texture off adds nothing."""
    self._dyn_play(t, sample)
    ours = self.model.force(sample) * self._strength + self._lock_stop_force(t, sample)
    w = self._glide(t)
    if w >= 1.0:
        self._out(t, ours)
    else:
        game = self._game_level(sample)
        self._out(t, game + w * (ours - game))


def run_engine(tmp_path, name, level, play=None, off_at=None, monkeypatch=None):
    engine, path = engine_on(tmp_path / name)
    if play is not None:
        monkeypatch.setattr(engine, '_play', play.__get__(engine))
    engine.set_texture(level, dict.fromkeys(ft.PARTS, True))
    start = len(forces())
    rnd = random.Random(5)
    t = feed(engine, 0.0, 8.0, rnd)
    assert engine.status.active
    if off_at is not None:
        engine.set_enabled(False)
    feed(engine, t, 1.0, rnd)
    return forces()[start:]


def test_texture_off_is_the_steering_force_bit_for_bit(tmp_path, monkeypatch):
    (tmp_path / 'a').mkdir()
    (tmp_path / 'b').mkdir()
    (tmp_path / 'c').mkdir()
    off = run_engine(tmp_path, 'a', 0.0)
    before = run_engine(tmp_path, 'b', 1.0, play=_original_play, monkeypatch=monkeypatch)
    assert off == before and len(off) > 1000
    on = run_engine(tmp_path, 'c', 1.0)
    assert len(on) == len(off) and on != off
    diff = [a - b for a, b in zip(on, off)]
    assert max(abs(d) for d in diff) <= ft.TOTAL_CAP * FULL + 1


def test_the_fade_gain_applies_to_the_texture(tmp_path):
    (tmp_path / 'a').mkdir()
    (tmp_path / 'b').mkdir()
    with_tx = run_engine(tmp_path, 'a', 1.0, off_at=True)
    without = run_engine(tmp_path, 'b', 0.0, off_at=True)
    first_on = next(i for i, v in enumerate(without) if v != 0)
    # Fading in: the texture is scaled by the same gain (tiny at the start, like the steering force)
    early = [abs(a - b) for a, b in zip(with_tx[first_on:first_on + 5], without[first_on:first_on + 5])]
    assert max(early) < 0.05 * ft.TOTAL_CAP * FULL
    # Faded out: the last write is 0 with the texture on too
    assert with_tx[-1] == 0 and without[-1] == 0


def test_the_engine_takes_the_texture_settings_on_its_thread(tmp_path):
    engine, path = engine_on(tmp_path)
    engine.set_texture(0.5, {'road': True, 'impact': False, 'lock': True, 'engine': True})
    assert engine.texture.level == ft.LEVEL                 # not before its step
    engine.step(0.0)
    assert engine.texture.level == 0.5 and engine.texture.parts == {'road': True, 'impact': False, 'lock': True, 'engine': True}
    assert engine.texture.describe() == '50 % (road, lock, engine)'
    # Applied once per request; a request made after it is taken, not lost
    engine.texture.set(0.9)
    engine.step(0.01)
    assert engine.texture.level == 0.9
    engine.set_texture(0.2, {'road': True, 'impact': True, 'lock': True, 'engine': True})
    engine.set_texture(0.4, {'road': True, 'impact': True, 'lock': True, 'engine': True})
    engine.step(0.02)
    assert engine.texture.level == 0.4


def test_profiles_keep_the_texture(tmp_path):
    from oversteer.model import Model
    from tests.fake_wheel import FakeDevice
    device = FakeDevice(ext_force_support=True)
    model = Model(device, None)
    assert model.data['countersteer_texture'] == 30
    assert model.get_countersteer_texture() == (0.3, {'road': True, 'impact': True, 'lock': True, 'engine': True})
    model.data['countersteer_texture'] = 55
    model.data['countersteer_texture_impact'] = False
    profile = str(tmp_path / 'p.ini')
    model.save(profile)
    fresh = Model(device, None)
    fresh.load(profile)
    assert fresh.get_countersteer_texture() == (0.55, {'road': True, 'impact': False, 'lock': True, 'engine': True})
    (tmp_path / 'old.ini').write_text('[DEFAULT]\nrange = 900\ncountersteer_ffb = 1\n')
    fresh.load(str(tmp_path / 'old.ini'))
    assert fresh.data['countersteer_texture'] == 30 and fresh.data['countersteer_texture_lock'] is True
    none = Model(FakeDevice(), None)
    assert none.data['countersteer_texture'] is None


# -- the engine part --

def revving(rpm, speed=20.0, throttle=1.0, cylinders=None, running=None, idle=900.0, top=7500.0):
    s = car(speed=speed)
    s.rpm, s.max_rpm, s.idle_rpm, s.throttle = rpm, top, idle, throttle
    s.running = running
    if cylinders is not None:
        s.cylinders = cylinders
    return s


def hum(sample, seconds=1.0, steering=8000.0):
    tx = texture(1.0, engine=True)
    return drive(tx, [sample] * int(seconds * HZ), steering=steering), tx


def dominant_hz(values):
    n = len(values)
    best = max(range(5, 160), key=lambda f: abs(sum(v * complex(math.cos(2 * math.pi * f * i / HZ),
                                                               -math.sin(2 * math.pi * f * i / HZ))
                                                     for i, v in enumerate(values))))
    return best


def test_engine_frequency_follows_the_firing_rate():
    e = ft.EngineHum()
    assert e.frequency(revving(1800.0)) == pytest.approx(60.0)             # 4 cylinders: rpm / 60 * 2
    assert e.frequency(revving(1800.0, cylinders=8)) == pytest.approx(120.0)
    assert e.frequency(revving(1800.0, cylinders=0)) == pytest.approx(60.0)   # unknown: the default
    assert e.frequency(revving(None)) is None and e.frequency(revving(float('nan'))) is None
    # Above what the wheel renders it folds down by octaves
    assert e.render_frequency(40.0) == 40.0 and e.render_frequency(200.0) == 50.0
    assert e.render_frequency(240.0) == 30.0


def test_engine_is_rev_aware_in_pitch_and_amplitude():
    idle, _ = hum(revving(900.0, throttle=0.0), 1.5)
    mid, _ = hum(revving(1200.0, throttle=0.0), 1.5)
    assert dominant_hz(idle[-333:]) == 30 and dominant_hz(mid[-333:]) == 40
    peak = lambda v: max(abs(x) for x in v[-333:])
    top_open, _ = hum(revving(900.0, throttle=1.0), 1.5)
    top_shut, _ = hum(revving(900.0, throttle=0.0), 1.5)
    high, _ = hum(revving(7000.0, throttle=0.0), 1.5)
    assert peak(top_open) > peak(top_shut) > peak(high) > 0.0              # idle and load are strongest
    assert peak(top_open) <= ft.EngineHum.CAP * FULL + 1


def test_engine_feel_scales_the_hum_and_zero_is_off_within_the_total_cap():
    peak = lambda v: max(abs(x) for x in v[-333:])
    base, _ = hum(revving(900.0, throttle=1.0), 1.5)
    tx = texture(1.0, engine=True)
    tx.engine.feel = 3.0
    loud = drive(tx, [revving(900.0, throttle=1.0)] * int(1.5 * HZ), steering=8000.0)
    assert peak(loud) == pytest.approx(3.0 * peak(base), rel=0.05) and tx.peak <= ft.TOTAL_CAP * FULL + 1
    tx = texture(1.0, engine=True)
    tx.engine.feel = 0.0
    off = drive(tx, [revving(900.0, throttle=1.0)] * int(1.5 * HZ), steering=8000.0)
    assert peak(off) == 0.0


def test_engine_is_zero_mean_and_stays_in_the_texture_headroom():
    values, tx = hum(revving(1500.0), 3.0)
    assert abs(sum(values[-666:]) / 666.0) < 0.02 * max(abs(v) for v in values)
    assert tx.peak <= ft.TOTAL_CAP * FULL + 1
    # Next to a steering force near full scale, the headroom FULL - |steering| still holds
    near, _ = hum(revving(1500.0), 1.0, steering=FULL - 100.0)
    assert max(abs(v) for v in near) <= 100.0 + 1e-6


def test_engine_idle_shake_plays_standing_but_not_when_off():
    still, _ = hum(revving(900.0, speed=0.0), 1.5)
    assert max(abs(v) for v in still[-333:]) > 0.0
    assert hum(revving(900.0, speed=0.0, running=False), 1.5)[0][-1] == 0.0
    assert max(abs(v) for v in hum(revving(None), 1.5)[0]) == 0.0
    assert max(abs(v) for v in hum(revving(0.0), 1.5)[0]) == 0.0
    # The other parts still wait for the car to roll: a landing does not shake a car at rest
    tx = texture(1.0, road=True)
    assert max(abs(v) for v in drive(tx, rough(1.0), steering=8000.0)) > 0.0


def test_engine_respects_the_switch_and_master_level():
    tx = texture(1.0, engine=False)
    assert max(abs(v) for v in drive(tx, [revving(900.0)] * 700)) == 0.0
    tx = texture(0.0, engine=True)
    assert max(abs(v) for v in drive(tx, [revving(900.0)] * 700)) == 0.0
    full, _ = hum(revving(900.0), 2.0)
    tx = texture(0.5, engine=True)
    half = drive(tx, [revving(900.0)] * 666)
    assert max(abs(v) for v in half[-333:]) == pytest.approx(0.5 * max(abs(v) for v in full[-333:]), rel=0.1)


def test_countersteer_settings_in_the_profile(tmp_path):
    from oversteer.model import Model
    from tests.fake_wheel import FakeDevice
    device = FakeDevice(ext_force_support=True)
    model = Model(device, None)
    assert model.data['countersteer_strength'] == 100 and model.get_countersteer_strength() == 100
    assert model.data['countersteer_texture_engine'] is True
    model.data['countersteer_strength'] = 60
    model.data['countersteer_texture_engine'] = False
    profile = str(tmp_path / 'p.ini')
    model.save(profile)
    fresh = Model(device, None)
    fresh.load(profile)
    assert fresh.get_countersteer_strength() == 60 and fresh.get_countersteer_texture()[1]['engine'] is False
    (tmp_path / 'old.ini').write_text('[DEFAULT]\nrange = 900\ncountersteer_ffb = 1\ncountersteer_strength = 3\n')
    fresh.load(str(tmp_path / 'old.ini'))
    assert fresh.data['countersteer_strength'] == 3 and fresh.data['countersteer_texture_engine'] is True
    assert Model(FakeDevice(), None).data['countersteer_strength'] is None


def test_strength_scales_the_rack_force_only(tmp_path):
    engine, path = engine_on(tmp_path)
    assert engine._strength == 1.0
    engine.set_strength(50)
    engine.step(0.0)
    assert engine._strength == 0.5
    engine.set_strength(500)
    engine.step(0.01)
    assert engine._strength == 1.3                      # 130 % at most
    assert engine.car_properties() is None or len(engine.car_properties()) == 3


def test_thermal_is_read_only_and_may_be_absent(tmp_path):
    from oversteer.device import Device
    class D(Device):
        def __init__(self, base):
            self.base = base
        def device_file(self, name):
            return str(self.base / name)
    assert D(tmp_path).get_ffb_thermal() is None
    (tmp_path / 'ffb_thermal').write_text('37 0\n')
    assert D(tmp_path).get_ffb_thermal() == (37, 0, 100)
    (tmp_path / 'ffb_thermal').write_text('37 1 74\n')
    assert D(tmp_path).get_ffb_thermal() == (37, 1, 74)
    (tmp_path / 'ffb_thermal').write_text('junk\n')
    assert D(tmp_path).get_ffb_thermal() is None


def test_engine_feel_is_outside_the_limiter_and_can_be_felt_with_little_steering():
    """Field: nothing to feel at 300 %: under 0.5 % of full scale once the limiter and a small steering force had their say."""
    peak = lambda v: max(abs(x) for x in v[-333:])
    tx = texture(1.0, engine=True)
    tx.engine.feel = 6.0
    loud = drive(tx, [revving(900.0, throttle=1.0)] * int(1.5 * HZ), steering=500.0)
    assert peak(loud) == pytest.approx(6.0 * ft.EngineHum.CAP * FULL, rel=0.1)
