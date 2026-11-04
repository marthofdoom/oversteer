"""Countersteer FFB (oversteer/ffb_engine.py, oversteer/wheel_owner.py) against a fake driver directory and
synthetic OVST v5 streams with the game's own force as the driver's game_ffb_raw would show it: the support
classifier's states and transitions, the fit and the strength, the crossfades, every stop path writing
game_ffb_level 100, stall detection, the wheel's owner, and an engine that can never hold up the telemetry
listener. Nothing here touches a real wheel or /sys."""
import logging
import math
import os
import select
import threading
import time

import pytest

from oversteer import ffb_engine as fe, wheel_owner
from oversteer.telemetry import NoLeds, Telemetry
from oversteer.telemetry_formats import OVST4_SIZE, Sample, decode_sample
from tests.sim import ovst_packet

HZ = 333.0
SCALE = 1.0 / 582.0


def acr(t, game_gain=1.0, rate_mz=400.0, speed=20.0, mz_sign=1.0, ff_sign=1.0, mz=True, v5=True, game='acr',
        car='acr/Skoda Fabia RS Rally2', step=None, texture=3.0):
    """A decoded ACR v5 sample at time t: steering weaving 0.3 to each side every 3.3 s, the front tyres' Mz
    following it (centring: positive to the right, as in marth's dump) and finalFF = gain * Mz / 582."""
    right = 0.3 * math.sin(2 * math.pi * 0.3 * t)
    front = mz_sign * rate_mz * right + texture * math.sin(2 * math.pi * 37.0 * t)    # a little texture
    s = Sample(6000.0, 7500.0, gear=3, speed=speed, car=car, game=game)
    s.steer = -right
    if v5:
        s.packet_id = int(t * HZ) if step is None else step
        s.game_ffb = max(-1.0, min(1.0, ff_sign * game_gain * front * SCALE))
        s.tyre_mz = (front / 2, front / 2, 0.1, 0.1) if mz else None
    return s


class FakeDriver(fe.DriverReplace):
    """The real DriverReplace on files in a temporary directory, every write logged; game_ffb_raw as the
    test sets it (FakeDriver.raw), the way the game's uploads would leave it."""
    log = []                 # (path, name, value), shared so tests see every instance's writes
    fail_force = False
    fail_scale = False
    raw = None               # (level, count, id) the game left, None: read the file

    def set_game_level(self, level):
        FakeDriver.log.append((self.path, 'game_ffb_level', level))
        super().set_game_level(level)

    def force(self, value):
        if FakeDriver.fail_force:
            raise OSError(19, 'No such device')
        FakeDriver.log.append((self.path, 'ext_force', value))
        super().force(value)

    def set_const_scale(self, value):
        if FakeDriver.fail_scale:
            raise OSError(19, 'No such device')
        FakeDriver.log.append((self.path, 'game_const_scale', value))
        super().set_const_scale(value)

    def read_raw(self):
        return FakeDriver.raw if FakeDriver.raw is not None else super().read_raw()


def upload(level, effect=0):
    """The game uploads a constant force: game_ffb_raw's level, a new count."""
    count = FakeDriver.raw[1] + 1 if FakeDriver.raw else 1
    FakeDriver.raw = (int(round(level)), count, effect)


def game_raw(s, raw_sign=1.0):
    """The game's own force for the sample in ext_force units: its finalFF at full scale."""
    return raw_sign * 32767 * s.game_ffb


def wheel(tmp_path, name='wheel', level=100, raw=True, scale=True):
    """A fake driver directory; scale=False: an older driver without game_const_scale."""
    d = tmp_path / name
    d.mkdir()
    (d / 'ext_force').write_text('0\n')
    (d / 'game_ffb_level').write_text('%d\n' % level)
    if scale:
        (d / 'game_const_scale').write_text('100\n')
    (d / 'ext_force_cap').write_text('80\n')
    if raw:
        (d / 'game_ffb_raw').write_text('0 0 -1\n')
    return str(d)


def read(path, name):
    with open(path + '/' + name) as f:
        return int(f.readline())         # written at offset 0, never truncated: the first line is the value


@pytest.fixture(autouse=True)
def _clean_log():
    FakeDriver.log = []
    FakeDriver.fail_force = False
    FakeDriver.fail_scale = False
    FakeDriver.raw = None
    yield


def engine_on(tmp_path, clock=time.monotonic, **kw):
    path = wheel(tmp_path, **kw)
    engine = fe.FfbEngine(clock=clock, driver=FakeDriver)
    engine.set_device(path)
    engine.set_enabled(True)
    engine.step(0.0)
    return engine, path


def run(engine, t0, seconds, hz=HZ, raw=True, raw_sign=1.0, **kw):
    """Feed `seconds` of samples at `hz` (the game uploading its force with each), one engine step per
    sample; returns the time after the last."""
    t, n = t0, int(round(seconds * hz))
    for _ in range(n):
        s = acr(t, **kw)
        if raw and s.game_ffb is not None:
            upload(game_raw(s, raw_sign))
        engine.offer(t, s)
        engine.step(t)
        t += 1.0 / hz
    return t


def levels(path=None):
    return [v for p, name, v in FakeDriver.log if name == 'game_ffb_level' and (path is None or p == path)]


def forces():
    return [v for _p, name, v in FakeDriver.log if name == 'ext_force']


def scales(path=None):
    return [v for p, name, v in FakeDriver.log if name == 'game_const_scale' and (path is None or p == path)]


# -- the classifier --

def classify(samples, holding=False, raw=True):
    c = fe.SupportClassifier()
    c.has_game_ffb_raw = raw
    for t, s in samples:
        c.feed(t, s)
    return c, c.verdict(samples[-1][0], holding)


def stream(seconds, hz=HZ, t0=0.0, **kw):
    n = int(round(seconds * hz))
    return [(t0 + i / hz, acr(t0 + i / hz, **kw)) for i in range(n)]


def test_acr_v5_at_333_hz_with_live_mz_is_green_after_a_second():
    _, v = classify(stream(0.5))
    assert (v.state, v.reason) == ('none', 'measuring')
    _, v = classify(stream(1.5))
    assert (v.state, v.reason) == ('replace', 'ok')
    assert v.rate == pytest.approx(333, abs=3)


@pytest.mark.parametrize('game', ['acc', 'ac'])
def test_acc_and_ac_go_green_only_by_passing_live(game):
    assert classify(stream(1.5, game=game))[1].state == 'replace'
    assert classify(stream(1.5, game=game, mz=False))[1].reason == 'no-mz'       # AC: no Mz in the page


def test_a_rate_drop_turns_it_grey_and_holding_tolerates_a_sag():
    assert classify(stream(1.5, hz=200.0))[1].reason == 'low-rate'
    assert classify(stream(1.5, hz=220.0), holding=True)[1].state == 'replace'
    assert classify(stream(1.5, hz=150.0), holding=True)[1].reason == 'low-rate'


def test_a_gap_over_20_ms_blocks_green_and_over_50_ms_ends_a_hold():
    samples = stream(1.5)
    late = [(t + (0.03 if t > 1.0 else 0.0), s) for t, s in samples]           # one 33 ms gap
    assert classify(late)[1].reason == 'gaps'
    assert classify(late, holding=True)[1].state == 'replace'
    later = [(t + (0.06 if t > 1.0 else 0.0), s) for t, s in samples]
    assert classify(later, holding=True)[1].reason == 'gaps'
    # The gap leaves the window after a second: green again
    c, _ = classify(late)
    for t, s in stream(1.1, t0=late[-1][0] + 1 / HZ):
        c.feed(t, s)
    assert c.verdict(t).state == 'replace'


def test_missing_flat_or_old_bridge_mz_is_not_replace():
    assert classify(stream(1.5, mz=False))[1].reason == 'no-mz'
    assert classify(stream(1.5, rate_mz=0.0001, texture=0.0))[1].reason == 'mz-flat'
    # A real v4 packet (the bridge before the force fields), decoded: no packet_id, no Mz
    s = acr(0.0)
    s.accel, s.stage_length = (0.0, 0.0, 0.0), 5000.0
    data = bytearray(ovst_packet(s, 1)[:OVST4_SIZE])
    data[4] = 4
    old = decode_sample(bytes(data))
    assert old.game == 'acr' and old.packet_id is None and old.tyre_mz is None
    old.speed = 20.0
    samples = [(i / HZ, old) for i in range(int(1.5 * HZ))]
    assert classify(samples)[1].reason == 'old-bridge'
    # One old packet in the window is enough
    mixed = stream(1.5)
    mixed[300] = (mixed[300][0], old)
    assert classify(mixed)[1].reason == 'old-bridge'


def test_mz_is_judged_only_when_moving_and_a_stop_keeps_the_verdict():
    parked = stream(1.5, speed=0.5)
    assert classify(parked)[1].reason == 'drive'
    c, v = classify(stream(1.5))
    assert v.state == 'replace'
    for t, s in stream(2.0, t0=1.5, speed=0.0, rate_mz=0.0001, texture=0.0):       # stopped, Mz flat at a standstill
        c.feed(t, s)
    assert c.verdict(t).state == 'replace'


def test_other_games_are_model_add_on_or_none():
    rbr = stream(1.5, hz=236.0, game='rbr', v5=False)
    assert classify(rbr, raw=False)[1][:2] == ('add-on', 'rbr-no-raw')
    c = fe.SupportClassifier()
    c.has_game_ffb_raw = True                 # the hook for the driver's game_ffb_raw
    for t, s in rbr:
        c.feed(t, s)
    assert c.verdict(t)[:2] == ('model', 'rbr-raw')        # no uploads measured yet
    c.raw_rate = 810.0
    assert c.verdict(t)[:2] == ('replace', 'ok')
    assert classify(stream(1.5, hz=60.0, game='forza-fh', v5=False))[1][:2] == ('add-on', 'fh-addon')
    assert classify(stream(1.5, hz=20.0, game='forza-fh', v5=False))[1][:2] == ('add-on', 'fh-rate')
    assert classify(stream(1.5, hz=60.0, game='dirt', v5=False))[1][:2] == ('add-on', 'no-forces')
    c, _ = classify(stream(1.5))
    assert c.verdict(1.5 + 1.1).state == 'none'      # a second of silence


def test_a_game_change_starts_the_measurement_again():
    c, _ = classify(stream(1.5))
    for t, s in stream(0.5, t0=1.5, game='acc'):
        c.feed(t, s)
    assert c.verdict(t).reason == 'measuring'


def test_a_driver_without_game_ffb_raw_gets_no_replace():
    assert classify(stream(1.5), raw=False)[1][:2] == ('add-on', 'old-driver')


def test_a_few_samples_without_mz_do_not_drop_it():
    samples, at = stream(1.5), 400
    for i in range(at, at + fe.MZ_MISSING_RUN - 1):
        samples[i][1].tyre_mz = None
    c, v = classify(samples[:at + fe.MZ_MISSING_RUN - 1])
    assert v.state == 'replace'
    samples[at + fe.MZ_MISSING_RUN - 1][1].tyre_mz = None
    c, v = classify(samples[:at + fe.MZ_MISSING_RUN])
    assert v.reason == 'no-mz'


def _flat_after_live(live_s, flat_s, mz=0.0, wiggle=0.0):
    """A stream with live Mz for live_s, then Mz that stays at `mz` (+- wiggle, a tiny texture) for flat_s."""
    out = stream(live_s)
    t0 = out[-1][0] + 1 / HZ
    for i in range(int(flat_s * HZ)):
        t = t0 + i / HZ
        s = acr(t)
        v = mz + wiggle * math.sin(2 * math.pi * 37.0 * t)
        s.tyre_mz = (v / 2, v / 2, 0.1, 0.1)
        out.append((t, s))
    return out


def test_small_but_moving_mz_is_not_flat_a_stream_that_stopped_sending_it_is():
    # ACR's front Mz at low lateral force: tiny (well under MZ_FLAT in a second) but never identical
    assert classify(stream(3.0, rate_mz=0.5, texture=0.05))[1].reason != 'mz-flat'
    # never live and not changing at all: flat after a second
    assert classify(stream(2.5, rate_mz=0.0, texture=0.0))[1].reason == 'mz-flat'


def classify_live(samples):
    """classify(), with the verdict asked for every 0.1 s as the engine does (the classifier learns that Mz was live
    from the verdicts)."""
    c = fe.SupportClassifier()
    c.has_game_ffb_raw = True
    nxt = 0.0
    for t, s in samples:
        c.feed(t, s)
        if t >= nxt:
            nxt = t + 0.1
            c.verdict(t)
    return c, c.verdict(samples[-1][0])


def test_a_stream_that_has_shown_mz_needs_ten_seconds_of_unchanging_values_to_be_flat():
    assert classify_live(_flat_after_live(2.0, 5.0))[1].reason == 'ok'            # a quiet straight, identical values
    assert classify_live(_flat_after_live(2.0, 9.0))[1].reason == 'ok'
    assert classify_live(_flat_after_live(2.0, 11.5))[1].reason == 'mz-flat'
    # tiny Mz that still moves (a texture of 0.1 Nm) is not unchanging however long it lasts
    assert classify_live(_flat_after_live(2.0, 15.0, wiggle=0.1))[1].reason != 'mz-flat'
    # and it recovers when it moves again
    samples = _flat_after_live(2.0, 11.5) + stream(1.5, t0=14.0)
    assert classify_live(samples)[1].reason == 'ok'


CAPTURE = os.path.expanduser('~/.var/app/io.github.berarma.Oversteer/data/oversteer/captures/20261102-192230.ovcap.gz')


@pytest.mark.skipif(not os.path.exists(CAPTURE), reason="marth's ACR drive capture is not here")
def test_marths_acr_drive_is_never_called_mz_flat_while_driving():
    from oversteer.telemetry_capture import read_capture
    _meta, records = read_capture(CAPTURE)
    c = fe.SupportClassifier()
    c.has_game_ffb_raw = True
    flat, nxt = 0, 0.0
    for t, _arrival, data in records:
        s = decode_sample(data)
        if s is None:
            continue
        c.note(t, s.packet_id)
        c.feed(t, s)
        if t >= nxt:
            nxt = t + 0.1
            if (s.speed or 0.0) > fe.MIN_SPEED and c.verdict(t).reason == 'mz-flat':
                flat += 1
    assert flat == 0


# -- the lock stop --

def _hold(stop, t, pos, steer=1.0, lock=270.0, seconds=0.15):
    """The force once the wheel has stayed at `pos` for a while (the speed's low-pass has settled)."""
    f = 0.0
    for _ in range(int(seconds / 0.003)):
        f = stop.force(t, steer, pos, lock)
        t += 0.003
    return f / fe.FULL, t


def test_the_lock_stop_pushes_back_past_the_lock_and_not_inside_it():
    stop = fe.LockStop()
    for i, pos in enumerate([0.0, 100.0, -200.0, 269.9, -269.9, 270.0]):
        assert _hold(stop, i, pos)[0] == 0
    forces, t = [], 10.0
    for depth in range(0, 10):
        f, t = _hold(stop, t, 270.0 + depth)
        forces.append(f)
    assert all(a <= b for a, b in zip(forces, forces[1:]))              # smooth onset, growing with the depth
    assert forces[0] == 0 and 0 < forces[1] < 0.04 and forces[-1] >= 0.5
    assert max(forces) <= fe.LockStop.MAX + 1e-9
    # the other side pushes the other way (positive ext_force: toward decreasing ABS_X, back from the high side)
    assert _hold(fe.LockStop(), 0.0, -280.0)[0] < 0 < _hold(fe.LockStop(), 0.0, 280.0)[0]
    assert _hold(fe.LockStop(), 0.0, 400.0)[0] == pytest.approx(fe.LockStop.MAX)


def test_the_lock_stop_waits_for_a_measured_lock():
    stop = fe.LockStop()
    assert _hold(stop, 0.0, 300.0, lock=None)[0] == 0 and not stop.active


def test_a_wheel_pushed_past_the_lock_is_stopped_and_settles_without_bouncing_far_back():
    stop = fe.LockStop()
    pos, vel, t = 260.0, 200.0, 0.0                                     # deg, deg/s: thrown at the lock
    a_fs = 6000.0                                                       # deg/s^2 per full scale (a wheel's inertia)
    push = 0.3 * fe.FULL                                                # the hands' / the game's force outward
    deepest = lowest = 0.0
    for _ in range(int(2.0 / 0.002)):
        f = stop.force(t, 1.0, pos, 270.0)
        vel += a_fs * (push - f) / fe.FULL * 0.002
        pos += vel * 0.002
        t += 0.002
        deepest, lowest = max(deepest, pos - 270.0), min(lowest, pos - 270.0)
    assert 5.0 < deepest < 50.0                                         # resisted: the 44 degrees it went before
    assert lowest > -15.0                                               # no bounce across the range
    assert 0.0 < pos - 270.0 < 12.0 and abs(vel) < 5.0                  # at rest where the spring balances the push


# -- the force: fit against the game's raw force, strength --

def fitted(seconds=8.0, raw_sign=1.0, raw=True, **kw):
    model = fe.RackForce()
    for t, s in stream(seconds, **kw):
        model.feed(t, s, game_raw(s, raw_sign) if raw else None)
    return model


def test_it_starts_at_finalff_scale_and_fits_the_games_force_but_plays_its_own_level():
    assert fe.RackForce().scale == pytest.approx(32767 / 582)
    model = fitted(game_gain=0.5)
    assert model.ready and model.refused is None and model.fit > 0.95
    assert model.scale == pytest.approx(0.5 * 32767 / 582, rel=0.05)         # the fit (the game's units per Nm)
    assert model.cross == pytest.approx(1.0, abs=0.05)       # finalFF agrees
    s = acr(1.0, game_gain=0.5)
    model.mapping = 'classic'                                  # (the profile INI's countersteer_mapping)
    classic = fe.FULL * fe.RackForce.CLASSIC_SCALE * model.rim_torque(s) / fe.wheel_torque.DEFAULT_PEAK_NM
    assert model.force(s) == pytest.approx(max(-0.75 * fe.FULL, min(0.75 * fe.FULL, classic)))
    model.mapping = 'knee'
    # the magnitude is the rim-torque model's, not the game's: its FFB setting does not move it
    assert model.force(s) == pytest.approx(fe.FULL * fe.wheel_torque.rim_share(model.rim_torque(s) * model.rho(), None, 1.0, model.top_torque() * model.rho(), model.knee))
    twice = fitted(game_gain=2.0)
    twice.mapping = 'knee'
    assert twice.force(s) == pytest.approx(model.force(s), rel=1e-6)
    strong = fitted(game_gain=2.0)
    assert strong.scale == pytest.approx(2.0 * 32767 / 582, rel=0.1)           # clipped raw left out


def test_the_cross_check_shows_a_game_force_that_is_not_finalff():
    model = fe.RackForce()
    for t, s in stream(8.0):
        model.feed(t, s, 0.5 * game_raw(s))                 # the wheel gets half of what finalFF says
    assert model.ready and model.cross == pytest.approx(0.5, abs=0.05)


def test_it_waits_for_the_games_force_and_for_corners():
    model = fitted(raw=False)
    assert not model.ready and model.waiting == 'raw'
    assert not fitted(seconds=2.0).ready                     # under MIN_TIME
    model = fitted(rate_mz=10.0)                              # straight-line Mz: no corners yet
    assert not model.ready and model.waiting == 'corners'


def test_samples_at_the_games_lock_are_left_out_of_the_fit_and_the_keep_check():
    # ACR's lock-stop force is in the wheel's force but not in telemetry: at |steer| >= 0.97 raw is garbage
    model = fitted()
    assert model.ready
    for t, s in stream(3.0, t0=20.0):
        s.steer = 1.0
        model.feed(t, s, -fe.FULL / 2)                       # nonsense raw, the lock-stop force
    assert model.ready and model.fit > 0.95                   # not dropped by the keep check, fit unmoved
    assert model.moving_time == pytest.approx(fitted().moving_time)


def test_the_games_minimum_force_floor_is_learnt_and_carries_no_information():
    floor = 1965
    model = fe.RackForce()
    n = 0
    for t, s in stream(20.0):
        raw = game_raw(s)
        if n % 4 == 0:                                       # a quarter of the uploads sit at the floor (+-1965 / 1967)
            raw = math.copysign(floor + (n // 4) % 3, raw)
        n += 1
        model.feed(t, s, raw, new=True)
    assert model.floor is not None and abs(model.floor - floor) <= 3
    before = model.moving_time
    s = acr(30.0)
    model.feed(30.0, s, float(floor), new=True)
    assert model.moving_time == before                       # at the floor: no information for the fit
    model.feed(30.01, acr(30.01), 5000.0, new=True)
    assert model.moving_time > before
    # a game with no such floor learns none
    assert fitted(seconds=20.0).floor is None


def test_a_game_force_against_mz_is_refused():
    model = fitted(raw_sign=-1.0)
    assert not model.ready and model.refused == 'reversed'


def test_a_force_pulling_into_the_turn_is_fine_when_the_game_agrees():
    # A long drift: Mz (and the game's force) toward the countersteer, against the steering angle
    model = fitted(mz_sign=-1.0)
    assert model.ready and model.refused is None
    assert model.centring is None or model.centring < 0       # the hint says so, and it gates nothing


def test_a_game_force_that_stops_following_drops_it_and_it_learns_again():
    model = fitted()
    assert model.ready
    for t, s in stream(1.0, t0=8.0):
        model.feed(t, s, -game_raw(s))
    assert model.ready                                       # not on the first moments of it
    for t, s in stream(1.5, t0=9.0):
        model.feed(t, s, -game_raw(s))
    assert not model.ready and model.waiting == 'mismatch' and model.refused is None


def test_no_fresh_game_force_while_ready_withdraws_it_and_the_fit_is_judged_again_at_the_next_upload():
    model = fitted()
    for t, s in stream(1.9, t0=8.0, rate_mz=700.0):
        model.feed(t, s, None)
    assert model.ready                                       # 2 s is the window now (the game uploads on change)
    for t, s in stream(1.0, t0=9.9, rate_mz=700.0):
        model.feed(t, s, None)
    assert not model.ready and model.waiting == 'raw'
    assert model.fit is not None and model._long.spread() > 0   # frozen, not forgotten
    for t, s in stream(0.5, t0=11.0):
        model.feed(t, s, game_raw(s))
    assert model.ready                                       # back at the next uploads, no relearning


def test_a_move_the_games_force_is_not_due_an_upload_for_is_not_stale():
    model = fitted()
    assert model._stale_dmz() >= 0.1 * fe.FULL / model.scale
    t0 = 8.0
    s = acr(t0)
    model.feed(t0, s, game_raw(s))
    small = model._stale_dmz() * 0.5
    for t, s in stream(3.0, t0=t0 + 0.01, rate_mz=small / 0.3):
        model.feed(t, s, None)
    assert model.ready


def test_a_new_car_fits_again():
    model = fitted(raw_sign=-1.0)
    for t, s in stream(8.0, car='acr/Hyundai i20 N Rally2'):
        model.feed(t, s, game_raw(s))
    assert model.ready and model.refused is None


# -- the engine: crossfades, toggle semantics, every stop path --

def test_on_and_green_fades_ours_in_with_the_games_ramp(tmp_path):
    engine, path = engine_on(tmp_path)
    run(engine, 0.0, 2.0)
    assert levels() == [] and forces() == []           # green, but the fit is not ready: the game plays
    t = run(engine, 2.0, 6.0)
    assert levels() == [0]
    assert engine.status.active and engine.status.state == 'replace'
    log = [e for e in FakeDriver.log if e[1] == 'ext_force']
    first = [abs(v) for v in forces()[:int(0.2 * HZ)]]
    # The first writes are a fraction of the force, growing over the 200 ms the driver ramps the game out
    assert first[0] < 200 and max(first[-10:]) > 1000
    s = acr(t)
    upload(game_raw(s))
    engine.offer(t, s)
    engine.step(t)
    assert forces()[-1] == read(path, 'ext_force') == pytest.approx(engine.model.force(s), rel=0.05, abs=60)
    assert read(path, 'game_ffb_level') == 0
    assert len(log) > 300


def test_off_never_writes(tmp_path):
    engine, path = engine_on(tmp_path)
    engine.set_enabled(False)
    run(engine, 0.0, 8.0)
    assert FakeDriver.log == []
    assert engine.status.state == 'replace' and not engine.status.active


def active(tmp_path, **kw):
    engine, path = engine_on(tmp_path, **kw)
    t = run(engine, 0.0, 8.0)
    assert engine.status.active and levels() == [0]
    return engine, path, t


def test_switching_off_crossfades_back_to_the_game(tmp_path):
    engine, path, t = active(tmp_path)
    engine.set_enabled(False)
    n = len(forces())
    run(engine, t, 0.5)
    assert levels() == [0, 100] and read(path, 'game_ffb_level') == 100      # at once: the game ramps in
    tail = forces()[n:]
    assert int(0.15 * HZ) <= len(tail) <= int(0.25 * HZ) + 2               # ours fades over 200 ms
    assert tail[-1] == 0 and abs(tail[0]) > 10 * abs(tail[-2])
    assert engine.last_stop == 'off' and not engine.status.active


@pytest.mark.parametrize('drop, why', [
    (dict(hz=150.0), 'low-rate'),
    (dict(mz=False), 'no-mz'),
    (dict(v5=False), 'old-bridge'),
])
def test_the_state_dropping_restores_the_game(tmp_path, drop, why):
    engine, path, t = active(tmp_path)
    run(engine, t, 4.0, **drop)           # (a rate dip must also last SOFT_HOLD and glide down before the crossfade)
    assert levels() == [0, 100] and read(path, 'game_ffb_level') == 100
    assert engine.last_stop == 'state' and engine.status.reason == why
    assert forces()[-1] == 0                     # faded out to nothing


def test_the_games_force_turning_against_ours_hands_it_back(tmp_path):
    engine, path, t = active(tmp_path)
    run(engine, t, 2.0, raw_sign=-1.0)
    assert levels() == [0, 100] and engine.last_stop == 'state'
    assert engine.status.waiting in ('mismatch', 'corners')


def test_a_real_stall_over_300_ms_restores_the_game_and_it_waits_before_taking_over_again(tmp_path):
    engine, path, t = active(tmp_path)
    last = t - 1 / HZ
    engine.step(last + 0.2)
    assert levels() == [0]                      # 200 ms: still ours (held, easing down; the driver's watchdog behind)
    engine.step(last + 0.31)
    assert levels() == [0, 100] and engine.last_stop == 'stall'
    # Back and clean after a second, but not ours again until REARM after the release
    t = run(engine, last + 0.35, 2.0)
    assert levels() == [0, 100]
    run(engine, t, 1.5)
    assert levels() == [0, 100, 0]


def test_a_device_change_restores_the_old_wheel(tmp_path):
    engine, path, t = active(tmp_path)
    other = wheel(tmp_path, 'other')
    engine.set_device(other)
    run(engine, t, 0.5)
    assert levels(path) == [0, 100] and read(path, 'game_ffb_level') == 100
    assert not engine._muted
    engine.set_device(None)
    engine.step(t + 1.0)
    assert levels(other) == []                   # the new wheel waits REARM like any other release


def test_quitting_restores_the_game(tmp_path):
    engine, path, t = active(tmp_path)
    engine.stop()
    assert levels() == [0, 100] and read(path, 'game_ffb_level') == 100


def test_a_failed_write_restores_the_game_and_latches(tmp_path):
    engine, path, t = active(tmp_path)
    FakeDriver.fail_force = True
    run(engine, t, 0.1)
    assert levels() == [0, 100] and engine.last_stop == 'write'
    assert not engine.status.available
    FakeDriver.fail_force = False
    run(engine, t + 0.1, 4.0)
    assert levels() == [0, 100]                 # not again until the device is set again
    assert fe.status_text(engine.status)[1].startswith('The wheel stopped')


def test_an_error_in_the_loop_latches_without_a_mute_loop(tmp_path):
    clock = [0.0]
    engine, path = engine_on(tmp_path, clock=lambda: clock[0])
    t = run(engine, 0.0, 8.0)
    assert engine.status.active

    def broken(*a):
        raise RuntimeError('boom')
    engine.model.feed = broken
    clock[0] = t
    engine.start()
    try:
        for i in range(200):
            engine.offer(t + i / HZ, acr(t + i / HZ))
            clock[0] = t + i / HZ
            time.sleep(0.001)
    finally:
        engine.stop()
    assert levels() == [0, 100] and engine.last_stop == 'error'
    assert not engine.status.available


def test_a_stuck_thread_still_gets_the_game_its_force_back(tmp_path):
    clock = [0.0]
    engine, path = engine_on(tmp_path, clock=lambda: clock[0])
    t = run(engine, 0.0, 8.0)
    gate = threading.Event()
    engine.step = lambda now=None: gate.wait(5.0)
    engine.start()
    try:
        engine.offer(t, acr(t))
        time.sleep(0.05)
        engine.stop(timeout=0.05)
        assert levels() == [0, 100] and read(path, 'game_ffb_level') == 100
        assert engine._driver._fd is not None                # left open: the thread may still write
        assert wheel_owner.current(path) is None
    finally:
        gate.set()
    engine._driver.close()


def test_a_game_left_muted_is_restored_when_the_wheel_is_taken(tmp_path):
    engine = fe.FfbEngine(driver=FakeDriver)
    path = wheel(tmp_path, level=0)
    engine.set_device(path)
    engine.step(0.0)
    assert levels() == [100] and read(path, 'game_ffb_level') == 100


def test_not_replace_does_nothing_but_says_why(tmp_path):
    engine, path = engine_on(tmp_path)
    t = 0.0
    for i in range(int(8 * 236)):
        engine.offer(t, acr(t, game='rbr', v5=False))
        engine.step(t)
        t += 1 / 236.0
    assert FakeDriver.log == []
    assert engine.status[:2] == ('model', 'rbr-raw')
    light, text = fe.status_text(engine.status)
    assert light == 'amber' and 'force feedback' in text


def test_a_driver_without_game_ffb_raw_is_never_muted(tmp_path):
    engine, path = engine_on(tmp_path, raw=False)
    run(engine, 0.0, 8.0, raw=False)
    assert FakeDriver.log == [] and engine.status.reason == 'old-driver'
    assert 'update the driver' in fe.status_text(engine.status)[1]


def test_a_driver_without_replace_mode_is_never_written(tmp_path):
    d = tmp_path / 'old'
    d.mkdir()
    engine = fe.FfbEngine(driver=FakeDriver)
    engine.set_device(str(d))
    engine.set_enabled(True)
    run(engine, 0.0, 8.0)
    assert FakeDriver.log == [] and not engine.status.available
    assert fe.status_text(engine.status)[0] == 'off'


def test_status_text_per_state(tmp_path):
    engine, path = engine_on(tmp_path)
    assert fe.status_text(engine.status, listening=True)[1] == 'No game telemetry'
    t = run(engine, 0.0, 2.0)
    light, text = fe.status_text(engine.status)
    assert light == 'green' and 'drive a few corners' in text
    run(engine, t, 6.0)
    light, text = fe.status_text(engine.status)
    assert light == 'green' and "Countersteer's force" in text
    assert fe.status_text(fe.IDLE_STATUS)[1] == 'No wheel'


# -- the wheel's owner --

def test_wheel_owner_rules():
    w = '/fake/wheel-a'
    asked = []
    assert wheel_owner.acquire(w, 'engine', on_inhibit=lambda: asked.append(1) or wheel_owner.release(w, 'engine'))
    assert wheel_owner.current(w) == ('engine', False)
    assert not wheel_owner.acquire(w, 'other')                       # shared: refused while held
    assert wheel_owner.acquire(w, 'engine')                          # the holder again: fine
    wheel_owner.acquire(w, 'engine', on_inhibit=lambda: asked.append(1) or wheel_owner.release(w, 'engine'))
    assert wheel_owner.acquire(w, 'test', exclusive=True) and asked == [1]
    assert wheel_owner.current(w) == ('test', True)
    assert not wheel_owner.acquire(w, 'engine')
    assert not wheel_owner.acquire(w, 'calibration', exclusive=True)  # exclusive against exclusive
    wheel_owner.release(w, 'engine')                                 # not the holder: nothing
    assert wheel_owner.current(w) == ('test', True)
    wheel_owner.release(w, 'test')
    assert wheel_owner.current(w) is None
    # A holder that does not let go in time, or cannot be asked
    assert wheel_owner.acquire(w, 'deaf')
    assert not wheel_owner.acquire(w, 'test', exclusive=True)
    wheel_owner.release(w, 'deaf')
    assert wheel_owner.acquire(w, 'slow', on_inhibit=lambda: None)
    start = time.monotonic()
    assert not wheel_owner.acquire(w, 'test', exclusive=True, timeout=0.05)
    assert time.monotonic() - start < 0.5
    wheel_owner.release(w, 'slow')


def test_the_engine_never_takes_a_held_wheel(tmp_path):
    engine, path = engine_on(tmp_path)
    assert wheel_owner.acquire(path, 'wheel-test', exclusive=True)
    try:
        run(engine, 0.0, 8.0)
        assert FakeDriver.log == [] and engine.status.held_by == 'wheel-test'
        assert 'in use' in fe.status_text(engine.status)[1]
    finally:
        wheel_owner.release(path, 'wheel-test')
    t = run(engine, 8.0, 0.1)
    assert levels() == [0]                          # free again: it takes over


def test_an_exclusive_owner_gets_the_wheel_within_half_a_second(tmp_path):
    clock = [0.0]
    engine, path = engine_on(tmp_path, clock=lambda: clock[0])
    t = run(engine, 0.0, 8.0)
    assert wheel_owner.current(path) == (fe.OWNER, False)
    clock[0] = t
    engine.start()
    try:
        start = time.monotonic()
        assert wheel_owner.acquire(path, 'wheel-test', exclusive=True)
        assert time.monotonic() - start < 0.5
        assert levels() == [0, 100] and read(path, 'game_ffb_level') == 100
        assert engine.last_stop == 'inhibited'
        assert forces()[-1] == 0                     # the last value faded out over 100 ms first
    finally:
        engine.stop()
        wheel_owner.release(path, 'wheel-test')


def test_the_wheel_test_takes_the_wheel_and_gives_it_back(monkeypatch):
    from unittest import mock
    from oversteer import gui
    me = mock.MagicMock()
    me.device.dev_path = '/fake/wheel-test'
    me.TEST_OWNER = gui.Gui.TEST_OWNER
    me._release_test_wheel = lambda: gui.Gui._release_test_wheel(me)
    me._start_test = lambda: gui.Gui._start_test(me)
    monkeypatch.setattr(gui, 'Test', mock.MagicMock())
    gui.Gui.start_test(me)
    assert wheel_owner.current('/fake/wheel-test') == ('wheel-test', True)
    gui.Gui.abort_test(me, 'gone')
    assert wheel_owner.current('/fake/wheel-test') is None
    assert wheel_owner.acquire('/fake/wheel-test', 'calibration', exclusive=True)
    gui.Gui.start_test(me)                          # someone else has it: the test does not start
    me.ui.info_dialog.assert_called()
    assert wheel_owner.current('/fake/wheel-test') == ('calibration', True)
    wheel_owner.release('/fake/wheel-test', 'calibration')


# -- the driver files --

def test_the_driver_files_are_kept_open_and_used_at_offset_0(tmp_path, monkeypatch):
    path = wheel(tmp_path)
    opened = []
    real_open = fe.os.open
    monkeypatch.setattr(fe.os, 'open', lambda p, *a: (opened.append(p), real_open(p, *a))[1])
    driver = fe.DriverReplace(path)
    assert driver.available() and driver.has_game_ffb_raw()
    for v in (-12000, 5, 32767, -3):
        driver.force(v)
        assert read(path, 'ext_force') == v
    assert driver.read_raw() == (0, 0, -1)
    (tmp_path / 'wheel' / 'game_ffb_raw').write_text('-8192 4711 3\n')
    assert driver.read_raw() == (-8192, 4711, 3)
    assert opened == [path + '/ext_force', path + '/game_ffb_raw']
    driver.set_game_level(0)
    assert read(path, 'game_ffb_level') == 0 and driver.game_level() == 0
    driver.close()


def test_raw_from_two_effects_or_gone_quiet_is_not_used(tmp_path):
    engine, path = engine_on(tmp_path)
    FakeDriver.raw = (1000, 5, 0)
    assert engine._raw(0.0) == (1000, True)
    assert engine._raw(0.05) == (1000, False)        # same count, 50 ms: still the game's now, held
    assert engine._raw(0.2) == (None, False)         # quiet for 200 ms
    FakeDriver.raw = (900, 6, 1)                     # another effect id: two constant effects
    assert engine._raw(0.3) == (None, False)
    FakeDriver.raw = (900, 7, 1)
    assert engine._raw(1.4) == (900, True)           # a second on the one id: usable again


# -- the listener is never held up --

# -- game hitches: held through, not handed back --

def hitch(engine, t, gap, burst_ms=20.0, polls=0.005):
    """ACR's physics hitch at `t`: no packet for `gap` s (the engine polls meanwhile), then one packet and a burst of
    them with the step counter catching up (what the game missed arrives within burst_ms). Returns the time after."""
    last = t - 1 / HZ
    x = last + polls
    while x < t + gap:
        engine.step(x)
        x += polls
    t += gap
    step0 = int(last * HZ)
    n = int(round((gap + burst_ms / 1000.0) * HZ))
    for k in range(1, n + 1):
        at = t + burst_ms / 1000.0 * k / n if k > 1 else t         # the first one ends the gap, the rest come in a burst
        s = acr(at, step=step0 + k)
        upload(game_raw(s))
        engine.offer(at, s)
        engine.step(at)
    return t + burst_ms / 1000.0


def test_a_hitch_with_a_catch_up_burst_is_held_through_not_handed_back(tmp_path):
    engine, path, t = active(tmp_path)
    for gap in (0.06, 0.13, 0.25):
        t = hitch(engine, t, gap)
        t = run(engine, t, 2.0)
        assert levels() == [0] and engine.status.active, gap
    assert engine.last_stop is None and not engine._stalls


def test_a_hitch_is_held_at_the_last_value_easing_down_then_comes_back_smoothly(tmp_path):
    engine, path, t = active(tmp_path)
    value = forces()[-1]
    n = len(forces())
    t = hitch(engine, t, 0.2)
    gap = forces()[n:]
    assert len(gap) > 15                                         # rewritten through the gap (the driver's hold is 30 ms)
    assert gap[0] == value and abs(min(abs(v) for v in gap[:-20])) <= abs(value) * 0.6 + 5
    assert all(abs(a) >= abs(b) - 2 for a, b in zip(gap[:15], gap[1:16])) or abs(value) < 100
    t = run(engine, t, 1.0)
    assert levels() == [0]


def test_gaps_without_a_burst_are_losses_and_hand_back_only_after_the_hysteresis(tmp_path):
    engine, path, t = active(tmp_path)

    def loss(t):
        last = t - 1 / HZ
        x = last
        while x < last + 0.13:
            x += 0.005
            engine.step(x)
        return run(engine, last + 0.13, 0.3)             # the counter ran on through the gap: nothing to catch up
    t = loss(t)
    assert levels() == [0] and engine.status.active      # one loss: soft, and it has to last
    for _ in range(8):
        t = loss(t)
    assert engine.last_stop == 'state' and levels() == [0, 100]


def test_a_real_400_ms_stall_still_releases_and_a_window_of_gaps_reads_in_the_log(tmp_path, caplog):
    import logging
    engine, path, t = active(tmp_path)
    with caplog.at_level(logging.INFO):
        engine.step(t + 0.4)
    assert engine.last_stop == 'stall' and levels() == [0, 100]


def test_the_classifier_tells_a_hitch_from_a_loss_by_the_step_counter():
    def stream_with_gap(burst):
        c = fe.SupportClassifier()
        c.has_game_ffb_raw = True
        t, pid = 0.0, 0
        def packet(at, step):
            c.note(at, step)
            c.feed(at, acr(at, step=step))
        while t < 1.0:
            packet(t, pid)
            t, pid = t + 1 / HZ, pid + 1
        if burst:                                        # 130 ms silent, the counter stalled; then 43 steps in 20 ms
            t += 0.13
            packet(t, pid + 1)
            pid += 1
            for _ in range(20):
                t, pid = t + 0.001, pid + 2
                packet(t, pid)
        else:                                            # 130 ms silent, the counter ran on
            t, pid = t + 0.13, pid + int(0.13 * HZ)
        while t < 1.6:
            t, pid = t + 1 / HZ, pid + 1
            packet(t, pid)
        return c, t
    c, t = stream_with_gap(True)
    v = c.verdict(t, holding=True)
    assert v.state == 'replace' and v.gap_ms >= 125.0 and c._plain_gap == 0.0
    assert c.verdict(t).reason == 'gaps'                 # to go green again the window must be free of gaps over 20 ms
    c, t = stream_with_gap(False)
    assert c.verdict(t, holding=True).reason == 'gaps' and c._plain_gap >= 0.12


def test_a_soft_verdict_must_last_before_ours_is_handed_back_and_a_hard_fault_does_not_wait(tmp_path):
    engine, path, t = active(tmp_path)
    t = run(engine, t, 0.45, hz=60.0)                    # a rate dip that clears within the hysteresis
    t = run(engine, t, 3.0)
    assert levels() == [0] and engine.status.active and engine._soft_since is None
    t = run(engine, t, 5.0, hz=100.0)                    # one that lasts
    assert levels() == [0, 100] and engine.last_stop == 'state'
    FakeDriver.log.clear()
    engine2, path2, t2 = active(tmp_path, name='wheel2')
    run(engine2, t2, 0.6, mz=False)                      # no Mz: at once (the crossfade is 200 ms)
    assert engine2.last_stop == 'state' and levels(path2) == [0, 100]


def test_the_release_line_says_what_the_classifier_saw(tmp_path, caplog):
    import logging
    engine, path, t = active(tmp_path)
    with caplog.at_level(logging.INFO):
        run(engine, t, 6.0, hz=100.0)
    line = [r.getMessage() for r in caplog.records if 'handing the force back' in r.getMessage()]
    assert len(line) == 1 and 'state: rate ' in line[0] and 'engine skipped' in line[0] and 'behind' in line[0]


def test_three_stalls_within_30_s_back_off_to_the_games_force_for_30_s_and_say_so_once(tmp_path, caplog):
    import logging
    engine, path, t = active(tmp_path)
    with caplog.at_level(logging.INFO):
        for i in range(3):
            last = t - 1 / HZ
            engine.step(last + 0.31)
            assert engine.last_stop == 'stall'
            t = run(engine, last + 0.35, 4.0)
            if i < 2:
                assert levels().count(0) == i + 2 and engine.status.active      # taken over again after REARM
        assert levels().count(0) == 3 and not engine.status.active           # the third: it stays away
        t = run(engine, t, 20.0)
        assert levels().count(0) == 3
        run(engine, t, 10.0)
    assert levels().count(0) == 4 and engine.status.active
    said = [r for r in caplog.records if 'keeps stalling; staying on its force for 30 s' in r.getMessage()]
    assert len(said) == 1


def test_a_take_over_starts_at_the_games_level_and_glides_to_ours_over_2_5_s(tmp_path):
    engine, path = engine_on(tmp_path)
    t = 0.0
    start = None
    seen = []
    while t < 14.0:
        s = acr(t, game_gain=0.5)                        # the game plays half of what the model would (the fit's scale
        upload(game_raw(s))                              # is the game's own, the model's level is ours)
        engine.offer(t, s)
        engine.step(t)
        if engine.status.active and start is None:
            start = t
        if start is not None:
            ours = engine.model.force(s) * engine._strength
            game = engine._game_level(s)
            seen.append((t - start, forces()[-1], game, ours, engine._w))
        t += 1 / HZ
    assert start is not None
    # right after the driver's ramp (g = 1) the output is the game's level plus the glide's share of the difference
    for at, out, game, ours, w in seen:
        if 0.25 < at < 0.3:
            assert w == pytest.approx(at / fe.GLIDE_IN, abs=0.01)
            assert out == pytest.approx(game + w * (ours - game), abs=300)
        if at > fe.GLIDE_IN + 0.1:
            assert w == 1.0 and abs(out - ours) <= 300


def test_a_planned_handback_glides_down_to_the_games_level_before_the_crossfade(tmp_path):
    engine, path, t = active(tmp_path)
    t = run(engine, t, 3.0)
    assert engine._w == 1.0
    began = None
    trace = []
    while t < 30.0 and len(levels()) < 2:
        t = run(engine, t, 0.01, hz=100.0)               # a lasting rate dip
        if engine._handback and began is None:
            began = t
        if began is not None:
            trace.append((t - began, engine._w, len(levels())))
    assert began is not None and len(levels()) == 2
    assert all(n == 1 for at, w, n in trace if at < fe.GLIDE_OUT - 0.05)         # the game stays muted while it glides
    mid = [w for at, w, n in trace if 0.2 < at < 0.3]
    assert mid and all(0.3 < w < 0.7 for w in mid)                                # on its way down
    assert engine.last_stop == 'state'


def test_the_listener_never_waits_for_the_engine(tmp_path):
    """The real thread, made to take 5 ms a sample (15 times slower than the stream): offer() stays O(1),
    nothing queues, the engine skips to the newest sample and still counts every arrival for the rate."""
    engine = fe.FfbEngine(driver=FakeDriver)
    engine.set_device(wheel(tmp_path))
    engine.set_enabled(True)
    feed = engine.classifier.feed
    seen = []

    def slow(t, sample, arrivals=1):
        seen.append(arrivals)
        time.sleep(0.005)
        feed(t, sample, arrivals)
    engine.classifier.feed = slow
    engine.start()
    try:
        samples = [acr(i / HZ) for i in range(20000)]
        worst = 0.0
        start = time.perf_counter()
        for sample in samples:
            a = time.perf_counter()
            engine.offer(time.monotonic(), sample)
            worst = max(worst, time.perf_counter() - a)
        spent = time.perf_counter() - start
        deadline = time.monotonic() + 2.0
        while engine._seen != engine._seq and time.monotonic() < deadline:
            time.sleep(0.005)
    finally:
        engine.stop()
    assert worst < 0.005                         # never the engine's 5 ms
    assert spent < 2.0
    assert engine.skipped > 10000 and len(seen) < 10000
    assert sum(seen) == 20000                     # every arrival counted, though most were never looked at


def test_the_listener_hands_every_decoded_sample_over():
    telemetry = Telemetry(NoLeds(), 5310)
    engine = fe.FfbEngine()
    telemetry.ffb = engine
    for i in range(5):
        s = acr(i / HZ)
        s.accel, s.stage_length = (0.0, 0.0, 0.0), 5000.0
        telemetry.handle(100.0 + i / HZ, ovst_packet(s, 1000 + i), ('127.0.0.1', 1))
    seq, t, sample = engine._slot
    assert seq == 5 and t == pytest.approx(100.0 + 4 / HZ)
    assert sample.packet_id == 1004 and sample.tyre_mz is not None


def test_the_thread_stops_writing_on_a_stall(tmp_path):
    """The real thread on a clock the test holds: samples stop, 30 ms on it is still ours, past 300 ms the
    game's force is back within a few polls."""
    clock = [0.0]
    engine, path = engine_on(tmp_path, clock=lambda: clock[0])
    last = run(engine, 0.0, 8.0) - 1 / HZ
    assert engine.status.active
    clock[0] = last + 0.03
    engine.start()
    try:
        time.sleep(0.1)                          # ten polls
        assert engine.status.active and levels() == [0]
        clock[0] = last + 0.32
        deadline = time.monotonic() + 1.0
        while engine.last_stop != 'stall' and time.monotonic() < deadline:
            time.sleep(0.001)
        assert engine.last_stop == 'stall'
        assert time.monotonic() < deadline - 0.8  # within 200 ms of the stall
        assert levels() == [0, 100] and read(path, 'game_ffb_level') == 100
    finally:
        engine.stop()


def test_profiles_keep_the_switch(tmp_path):
    from oversteer.model import Model
    from tests.fake_wheel import FakeDevice
    device = FakeDevice(ext_force_support=True)
    model = Model(device, None)
    assert model.data['countersteer_ffb'] is False
    model.data['countersteer_ffb'] = True
    profile = str(tmp_path / 'p.ini')
    model.save(profile)
    fresh = Model(device, None)
    fresh.load(profile)
    assert fresh.get_countersteer_ffb() is True
    (tmp_path / 'old.ini').write_text('[DEFAULT]\nrange = 900\n')
    fresh.load(str(tmp_path / 'old.ini'))
    assert fresh.data['countersteer_ffb'] is False
    none = Model(FakeDevice(), None)
    assert none.data['countersteer_ffb'] is None


# -- re-review: stale raw on a straight, fades, gains, demos --

def test_a_straight_with_no_new_upload_keeps_ours_playing(tmp_path):
    """A game that uploads only on change: moving, Mz steady (a few Nm of texture), no new count for 2 s."""
    engine, path, t = active(tmp_path)
    t = run(engine, t, 0.3, rate_mz=0.0, texture=3.0)          # onto the straight: the game's force follows
    n = len(forces())
    t = run(engine, t, 2.0, raw=False, rate_mz=0.0, texture=3.0)
    assert levels() == [0] and engine.status.active and engine.model.ready
    assert len(forces()) - n == int(round(2.0 * HZ))
    # And when it uploads again, the fit carries on from where it was
    run(engine, t, 1.0)
    assert levels() == [0] and engine.status.active


def test_a_corner_with_no_new_upload_hands_back(tmp_path):
    engine, path, t = active(tmp_path)
    run(engine, t, 3.0, raw=False, rate_mz=1000.0)
    assert levels() == [0, 100] and engine.last_stop == 'state' and engine.status.waiting == 'raw'


def test_the_upload_rate_is_in_the_status(tmp_path):
    engine, path, t = active(tmp_path)
    assert engine.status.raw_rate == pytest.approx(333, abs=5)
    assert '333 a second' in fe.status_text(engine.status)[1] or 'a second' in fe.status_text(engine.status)[1]


def test_switching_off_during_the_fade_in_fades_out_from_there(tmp_path):
    engine, path = engine_on(tmp_path)
    t = run(engine, 0.0, 3.0)
    while not levels():
        t = run(engine, t, 1 / HZ)
    t = run(engine, t, 0.05)                         # a quarter of the way in
    engine.set_enabled(False)
    n = len(forces())
    run(engine, t, 0.5)
    tail = forces()[n:]
    assert tail[-1] == 0 and len(tail) <= int(0.07 * HZ)    # down at the full rate: about 50 ms, not 200


def test_a_wheel_wanted_elsewhere_fades_the_last_value_quickly(tmp_path):
    engine, path, t = active(tmp_path)
    last = forces()[-1]
    engine.stop()                                    # quit: the same quick fade
    tail = forces()[-10:]
    assert tail[-1] == 0 and abs(tail[0]) == pytest.approx(abs(last) * 0.9, abs=2)
    assert levels() == [0, 100]


def test_app_gain_and_the_cap_are_said(tmp_path, monkeypatch):
    monkeypatch.setattr(fe.FfbEngine, '_range_wanted', lambda self: False)      # (full range would raise the 5 % cap to 100 %)
    engine, path = engine_on(tmp_path)
    with open(path + '/app_gain', 'w') as f:
        f.write('1\n')
    with open(path + '/gain', 'w') as f:
        f.write('65535\n')
    with open(path + '/ext_force_cap', 'w') as f:
        f.write('5\n')                               # 5 %: ours clips most of the time
    run(engine, 0.0, 10.0, game_gain=2.0)
    status = engine.status
    assert status.active and status.app_gain and status.clipping
    text = fe.status_text(status)[1]
    assert 'ext_force_cap' in text and 'gain setting' in text


def test_after_stop_it_never_takes_over(tmp_path):
    engine, path = engine_on(tmp_path)
    engine.stop()
    run(engine, 0.0, 8.0)
    assert levels() == [] and not engine.status.active


def test_demos_never_cut_countersteer_off(tmp_path):
    engine, path, t = active(tmp_path)
    assert not wheel_owner.hold_for_demo(path, 0.05)
    assert 'Countersteer FFB is playing' in wheel_owner.demo_refusal(path)
    assert engine.status.active                     # not inhibited
    engine.stop()
    assert wheel_owner.hold_for_demo(path, 0.05)
    assert wheel_owner.current(path) == ('demo', True)
    assert not wheel_owner.hold_for_demo(path, 0.05)    # one at a time
    assert 'still playing' in wheel_owner.demo_refusal(path)
    deadline = time.monotonic() + 1.0
    while wheel_owner.current(path) is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert wheel_owner.current(path) is None        # let go after the demo's length


def test_try_effect_refuses_while_countersteer_plays(tmp_path):
    from unittest import mock
    from oversteer import gui
    engine, path, t = active(tmp_path)
    me = mock.MagicMock()
    me.device.dev_path = path
    me.model.get_ffb_enabled.return_value = True
    gui.Gui.try_effect(me, 'constant')
    me.device.play_demo.assert_not_called()
    assert 'Countersteer FFB is playing' in me.ui.info_dialog.call_args[0][0]
    engine.stop()


def test_a_test_that_fails_to_start_gives_the_wheel_back(monkeypatch):
    from unittest import mock
    from oversteer import gui
    me = mock.MagicMock()
    me.device.dev_path = '/fake/wheel-raise'
    me.TEST_OWNER = gui.Gui.TEST_OWNER
    me._release_test_wheel = lambda: gui.Gui._release_test_wheel(me)
    me._start_test = mock.MagicMock(side_effect=OSError('no input device'))
    with pytest.raises(OSError):
        gui.Gui.start_test(me)
    assert wheel_owner.current('/fake/wheel-raise') is None


def test_a_stop_published_as_0_is_no_game_force_for_the_fit(tmp_path):
    """The driver publishes level 0 with a count step when the game's constant effect stops: while the car is
    moving that is no game force, not a 0 to fit against front Mz."""
    engine, path = engine_on(tmp_path)
    t = run(engine, 0.0, 6.0)
    model = engine.model
    assert model.ready and engine.status.active
    before, moving = list(model._long.s), model.moving_time
    upload(0)                                                # the game stops its effect, still driving
    t = run(engine, t, 0.08, raw=False)                      # within RAW_FRESH: the 0 is fresh
    assert model._long.s == before and model.moving_time == moving
    assert engine._raw(t, stops=True) == (None, False) and engine._raw(t)[0] == 0
    # As with no upload at all: once front Mz moves on with none, the fit is dropped and waits for the game
    run(engine, t, 2.6, raw=False)
    assert not model.ready and model.waiting == 'raw'


# -- the caster term: the force is Mz + the Fy arm, as a real steering and ACR's own force --

def tyre_sample(mz, fy, speed=20.0):
    """Front tyres' Mz and Fy each, in the game's signs (Fy left-positive, Mz opposite to Fy in grip)."""
    s = Sample(6000.0, 7500.0, gear=3, speed=speed, car='acr/Skoda Fabia RS Rally2', game='acr')
    s.packet_id = 1
    s.tyre_mz = (mz / 2, mz / 2, 0.0, 0.0)
    s.tyre_fy = (fy / 2, fy / 2, 0.0, 0.0)
    return s


def test_front_torque_adds_the_caster_arm_and_falls_back_to_mz():
    s = tyre_sample(-100.0, 3000.0)                          # grip: Mz = -tp * Fy
    assert fe.front_torque(s) == pytest.approx(-100.0 - fe.CASTER_TRAIL * 3000.0)
    s.tyre_fy = None
    assert fe.front_torque(s) == pytest.approx(-100.0)
    s.tyre_mz = None
    assert fe.front_torque(s) is None


def test_in_a_drift_the_force_pulls_toward_the_direction_of_travel_when_mz_has_gone():
    model = fe.RackForce()
    cornering = tyre_sample(-120.0, 3500.0)                  # same sign as Mz alone, a little bigger
    mz_only = model.force(tyre_sample(-120.0, 0.0))
    assert model.force(cornering) * mz_only > 0
    assert 1.0 < model.force(cornering) / mz_only
    # a left slide (car turning left, Fy left-positive, past the peak: Mz ~ 0): the wheel is pulled the way
    # the grip-cornering torque would pull, the self-steer into countersteer, not left slack
    drift = tyre_sample(0.0, 4500.0)
    assert model.force(drift) < -1.0 and model.force(drift) * model.force(cornering) > 0
    right = tyre_sample(0.0, -4500.0)
    assert model.force(right) == pytest.approx(-model.force(drift))


def test_straight_line_force_stays_zero_without_side_force():
    model = fe.RackForce()
    assert model.force(tyre_sample(0.0, 0.0)) == 0


def test_driven_front_axle_couple_adds_the_scrub_term():
    s = tyre_sample(0.0, 0.0)
    s.tyre_fx = (800.0, 200.0, 0.0, 0.0)                     # left front pulls harder: torque steer
    assert fe.front_torque(s) == pytest.approx(-fe.SCRUB * 600.0)
    s.tyre_fx = (500.0, 500.0, 0.0, 0.0)                     # an even split: no couple
    assert fe.front_torque(s) == pytest.approx(0.0)


def test_late_passes_are_logged_once_per_window(caplog):
    engine = fe.FfbEngine(driver=FakeDriver)
    with caplog.at_level('WARNING'):
        engine._late(100.0, "pass took 120 ms")
        engine._late(101.0, "pass took 90 ms")
        engine._late(111.0, "pass took 60 ms")
    lines = [r.getMessage() for r in caplog.records if 'engine late' in r.getMessage()]
    assert len(lines) == 2 and '(1 in' in lines[0] and '(2 in' in lines[1]


def test_a_late_ext_force_write_with_samples_waiting_is_a_warning(tmp_path, caplog):
    engine, path = engine_on(tmp_path)
    t = run(engine, 0.0, 1.0)
    engine._phase = 'on'
    engine._write_mono = time.monotonic() - 0.2
    engine._late_at = -fe.LATE_LOG_EVERY
    engine._written_at = t - 0.05
    engine._arrived_at = t - 0.01           # a sample came in since the last write
    with caplog.at_level('INFO'):
        engine._out(t, 1000.0)
    assert any(r.levelname == 'WARNING' and 'engine late: ext_force written' in r.getMessage() for r in caplog.records)
    assert not any('telemetry paused' in r.getMessage() for r in caplog.records)


def test_keep_alive_rewrites_in_a_telemetry_gap_count_as_writes_for_the_late_write_check(tmp_path, caplog):
    # capture 20261103-152713: ext_force was rewritten every ~30 ms through a 130 ms telemetry gap, and the sample that
    # ended the gap was reported as a write 130 ms after the last one
    engine, path = engine_on(tmp_path)
    t = run(engine, 0.0, 1.0)
    engine._phase = 'on'
    engine._late_at = -fe.LATE_LOG_EVERY
    engine._write_mono = time.monotonic() - 0.13       # the last sample's write, 130 ms ago
    engine._written_at = t - 0.13
    engine._arrived_at = t - 0.13
    for i in range(4):                                 # the rewrites through the gap
        engine._rewrite(t - 0.1 + i * 0.03)
    assert time.monotonic() - engine._write_mono < 0.05
    engine._arrived_at = t + 0.001                     # the sample that ends the gap
    with caplog.at_level('INFO'):
        engine._out(t + 0.001, 1000.0)
    assert not any('engine late' in r.getMessage() or 'telemetry paused' in r.getMessage() for r in caplog.records)


def test_a_late_ext_force_write_with_no_sample_since_is_an_info_pause(tmp_path, caplog):
    engine, path = engine_on(tmp_path)
    t = run(engine, 0.0, 1.0)
    engine._phase = 'on'
    engine._write_mono = time.monotonic() - 0.2
    engine._late_at = -fe.LATE_LOG_EVERY
    engine._written_at = t - 0.05
    engine._arrived_at = t - 0.1           # nothing arrived since the last write
    with caplog.at_level('INFO'):
        engine._out(t, 1000.0)
    lines = [r for r in caplog.records if 'telemetry paused' in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelname == 'INFO' and 'the game sent nothing' in lines[0].getMessage()
    assert not any(r.levelname == 'WARNING' and 'engine late' in r.getMessage() for r in caplog.records)


def test_a_slow_pass_is_noted_and_arms_the_stack_dump(caplog):
    engine = fe.FfbEngine(driver=FakeDriver)
    with caplog.at_level('WARNING'):
        engine._pass_done(time.monotonic() - 0.2)
    assert any('pass took' in r.getMessage() for r in caplog.records)


def test_the_steering_path_clamps_the_implied_pneumatic_trail():
    s = tyre_sample(949.0, -8627.0)                          # a landing spike: tp = 0.11 m, the wrong way round for grip
    lo, hi = sorted((-fe.TP_MAX * -8627.0, -fe.TP_MIN * -8627.0))
    assert fe.front_torque(s, 0.0, 0.0, clamp=True) == pytest.approx(hi)
    assert fe.front_torque(s, 0.0, 0.0) == pytest.approx(949.0)       # the fit and the classifier see it as it is
    s = tyre_sample(-8627.0 * -0.11 * -1, 8627.0)            # the same the other way (tp 0.11 m, Fy positive)
    assert fe.front_torque(s, 0.0, 0.0, clamp=True) == pytest.approx(-fe.TP_MAX * 8627.0)
    s = tyre_sample(-100.0, 3000.0)                          # a normal sample (tp 33 mm) is untouched
    assert fe.front_torque(s, clamp=True) == pytest.approx(fe.front_torque(s))
    s = tyre_sample(30.0, 3000.0)                            # a small opposite sign (tp -10 mm) is within TP_MIN
    assert fe.front_torque(s, 0.0, 0.0, clamp=True) == pytest.approx(30.0)


def test_rim_torque_uses_the_clamped_mz():
    model = fe.RackForce()
    spike = tyre_sample(949.0, -8627.0)
    plain = tyre_sample(fe.TP_MAX * 8627.0, -8627.0)         # exactly at the limit
    assert model.rim_torque(spike) == pytest.approx(model.rim_torque(plain))


# -- the event-driven game_ffb_raw intake --

class NotifyDriver(FakeDriver):
    """A pipe standing in for sysfs_notify: a byte written is the driver's upload wake-up (POLLIN, as a pipe
    reports it); read_raw drains it, the way the read at offset 0 re-arms."""

    def __init__(self, path):
        super().__init__(path)
        self.r, self.w = os.pipe()
        self.NOTIFY_EVENTS = select.POLLIN

    def notify_fd(self):
        return self.r, self.NOTIFY_EVENTS

    def read_raw(self):
        try:
            os.read(self.r, 64)
        except BlockingIOError:
            pass
        return super().read_raw()


class SilentDriver(FakeDriver):
    """A driver or fake with no way to be notified."""
    notify_fd = None


def notified_engine(tmp_path, driver=NotifyDriver):
    path = wheel(tmp_path)
    engine = fe.FfbEngine(driver=driver)
    engine.set_device(path)
    engine.set_enabled(True)
    engine.step(0.0)
    engine._wake.clear()
    engine.classifier.has_game_ffb_raw = True
    engine._fast = lambda: True
    if driver is NotifyDriver:
        os.set_blocking(engine._driver.r, False)
    return engine


def test_a_game_ffb_raw_upload_wakes_the_wait_and_its_read_lag_is_logged(tmp_path, caplog):
    engine = notified_engine(tmp_path)
    driver = engine._driver
    began = time.monotonic()
    threading.Timer(0.05, os.write, (driver.w, b'x')).start()
    engine._wait(2.0)
    assert 0.03 < time.monotonic() - began < 1.0           # woken by the upload, not the 2 s timeout
    assert engine._raw_wake_at is not None
    upload(1000)
    engine._raw(time.monotonic())
    assert engine._raw_wake_at is None and len(engine._raw_lags) == 1 and engine._raw_reads == 1
    engine._raw(time.monotonic())                          # a read without a wake: counted, no lag
    assert len(engine._raw_lags) == 1 and engine._raw_reads == 2
    engine._phase = 'on'
    with caplog.at_level(logging.INFO):
        engine._release('test', 0.0)
    assert any('wake to read: median' in r.message and '1 of 2 reads' in r.message for r in caplog.records)
    assert engine._raw_lags == [] and engine._raw_reads == 0


def test_the_other_wake_still_ends_the_wait_beside_game_ffb_raw(tmp_path):
    engine = notified_engine(tmp_path)
    began = time.monotonic()
    threading.Timer(0.05, engine._wake.set).start()
    assert engine._wait(2.0)
    assert time.monotonic() - began < 1.0 and engine._raw_wake_at is None
    engine._wake.clear()
    assert not engine._wait(0.02)                          # neither: the pass period ends it
    engine._wake.set()
    assert engine._wait(2.0)                               # already set: at once
    engine._wake.close()


def test_without_notification_the_wait_falls_back_to_its_timeout(tmp_path):
    engine = notified_engine(tmp_path, driver=SilentDriver)
    assert engine.raw_wait() is None
    began = time.monotonic()
    assert not engine._wait(0.05)
    assert time.monotonic() - began >= 0.04
    threading.Timer(0.02, engine._wake.set).start()
    assert engine._wait(2.0)
    engine._wake.close()


def test_no_fd_is_watched_when_the_force_is_not_read_each_pass(tmp_path):
    engine = notified_engine(tmp_path)
    assert engine.raw_wait() is not None
    engine._fast = lambda: False
    assert engine.raw_wait() is None


def test_the_real_driver_detects_notification_and_gives_up_when_stuck(tmp_path):
    path = wheel(tmp_path)
    driver = fe.DriverReplace(path)
    fd, events = driver.notify_fd()                        # a plain file is never signalled: treated as armed
    assert events == select.POLLPRI | select.POLLERR and driver.read_raw() == (0, 0, -1)
    stuck = fe.DriverReplace(path)
    stuck._ready = lambda timeout: True                    # signalled again right after the re-arming read
    assert stuck.notify_fd() is None
    quiet = fe.DriverReplace(path)
    assert quiet.notify_fd() is not None
    quiet._ready = lambda timeout: True
    quiet.read_raw()
    assert quiet.notify_fd() is None                       # and a later stuck read switches it off
    for d in (driver, stuck, quiet):
        d.close()
