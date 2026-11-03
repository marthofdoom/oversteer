"""The driver's virtual steering dynamics from the application's side (oversteer/ffb_dynamics.py, ffb_record.py, and the
engine, calibration, capture and model that use them): unit conversions, fake sysfs files for every read and write,
absence on older drivers, the calibration switching the stage off and back, a reset writing k = 0 first, and the
FFB tuning capture's sidecar. Nothing here touches a real wheel or /sys."""
import math
import os
import sys
from types import SimpleNamespace

import pytest

from oversteer import ffb_dynamics as fd
from oversteer import ffb_engine as fe
from oversteer import ffb_carprops as cp
from oversteer import ffb_record, wheel_calibration as wcal, wheel_torque
from oversteer.telemetry_formats import Sample

from . import test_ffb_engine as tfe
from .test_wheel_calibration import g29_like  # noqa: F401  (the fixture)
from .wheel_sim import SimWheel

G29 = {'a_fs': 14925.0, 'damping': 4.7e-4, 'friction': 0.05, 'breakaway': 0.125, 'fit_latency_ms': 3.0}


# -- units --

def test_a_calibration_result_converts_to_the_drivers_units():
    assert fd.wheel_values(G29) == (470, 670, 500, 1250, 5000)        # the driver's own defaults came from this wheel
    assert fd.wheel_text(fd.wheel_values(G29)) == '470 670 500 1250 5000'
    slow = dict(G29, fit_latency_ms=8.4, damping=6e-4, a_fs=10000.0, friction=0.04, breakaway=0.11)
    assert fd.wheel_values(slow) == (600, 1000, 400, 1100, 8400)


def test_the_latency_never_goes_below_the_drivers_default_or_over_its_limit():
    assert fd.wheel_values(dict(G29, fit_latency_ms=1.0))[4] == 5000
    assert fd.wheel_values(dict(G29, fit_latency_ms=None))[4] == 5000
    assert fd.wheel_values(dict(G29, fit_latency_ms=60.0))[4] == 20000


def test_values_are_clamped_into_what_the_driver_accepts():
    v = fd.wheel_values(dict(G29, friction=0.2, breakaway=0.5, damping=0.01, a_fs=100.0))
    assert v[0] == 2000 and v[1] == 5000 and v[2] == 800 and v[3] == 3000
    v = fd.wheel_values(dict(G29, friction=0.0, breakaway=0.0001, damping=1e-6, a_fs=1e9))
    assert v[0] == 100 and v[1] == 100 and v[2] == 0 and v[3] >= v[2]
    assert all(isinstance(x, int) for x in v)


@pytest.mark.parametrize('bad', [None, {}, 'x', dict(G29, a_fs=0), dict(G29, damping=float('nan')),
                                 dict(G29, friction=None), dict(G29, breakaway=True), dict(G29, a_fs=-1)])
def test_a_result_without_the_figures_gives_none(bad):
    assert fd.wheel_values(bad) is None


def test_column_targets_scale_with_s_and_the_peak_and_never_go_under_the_wheels_own():
    d, f = fd.column_targets(0.4, 2.1, (470, 670, 500, 1250, 5000))
    assert d == round(0.17 * math.pi / 180 * 0.4 / 2.1 * 1e6) and f == round(0.75 * 0.4 / 2.1 * 1e4)
    # a light scale: friction held at the wheel's own unless removal is asked for; damping is the car's (the driver
    # renders target - own and limits what it removes)
    d, f = fd.column_targets(0.1, 2.1, (470, 670, 500, 1250, 5000))
    assert d == round(0.17 * math.pi / 180 * 0.1 / 2.1 * 1e6) < 470 and f == 500
    d, f = fd.column_targets(0.1, 2.1, (470, 670, 500, 1250, 5000), removal=True)
    assert f == round(0.75 * 0.1 / 2.1 * 1e4) < 500
    # an unknown wheel counts as the G29's 2.1 Nm; the figures stay in the driver's range
    assert fd.column_targets(0.4, None) == fd.column_targets(0.4, 2.1)
    assert fd.column_targets(50.0, 2.1) == (fd.COLUMN_DAMPING_MAX, fd.COLUMN_FRICTION_MAX)
    # the damping's own scale (1:1 with the torque under the knee): the friction keeps the impedance scale
    d, f = fd.column_targets(0.3, 2.1, (470, 670, 500, 1250, 5000), damping_scale=1.0)
    assert d == round(0.17 * math.pi / 180 / 2.1 * 1e6) > 470 and f == round(0.75 * 0.3 / 2.1 * 1e4)


def test_the_alignment_target_is_in_wheel_position_units_with_abs_xs_sign():
    assert fd.align_target(0.0, 900) == 0
    assert fd.align_target(450.0, 900) == -32767                       # left (ext_force's sign) = decreasing ABS_X
    assert fd.align_target(-450.0, 900) == 32767
    assert fd.align_target(90.0, 900) == -round(32767 * 0.2)
    assert fd.align_target(5000.0, 900) == -32767                      # held to the wheel's range
    assert fd.align_target(90.0, 540) == -round(32767 * 90 / 270)      # the range in force, not the car's lock
    assert fd.align_text(10.0, 0.0123456, 0.75, 900) == '%d 12346 7500' % fd.align_target(10.0, 900)
    assert fd.align_text(0.0, 1.0, 5.0, 900) == '0 60000 8000'         # the driver's own ceilings


# -- the sysfs side --

def wheel_dir(tmp_path, **files):
    d = tmp_path / 'wheel'
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text)
    return str(d)


def test_the_driver_attributes_read_and_write_through_fake_files(tmp_path):
    path = wheel_dir(tmp_path, ffb_align='0 0 0 60000\n', ffb_dyn_damping='0\n', ffb_dyn_friction='0\n',
                     ffb_dyn_wheel='470 670 500 1250 5000\n', ffb_hands='-120 1\n', ffb_dyn_live='520 480 3.5\n',
                     range='900\n')
    dyn = fd.Dynamics(path)
    assert dyn.available() and all(dyn.has(n) for n in dyn.RW + dyn.RO)
    assert dyn.align_k_limit() == pytest.approx(0.06) and dyn.range() == 900
    assert dyn.hands() == (-120.0, True) and dyn.live() == (520.0, 480.0, 3.5)
    dyn.write('ffb_dyn_damping', '700')
    dyn.write('ffb_align', '1 2 3')
    assert open(path + '/ffb_dyn_damping').read() == '700' and open(path + '/ffb_align').read() == '1 2 3'
    assert dyn.align_k_limit() is None                                 # a driver that reports 3 fields only
    dyn.off()
    assert open(path + '/ffb_align').read() == '0 0 0' and open(path + '/ffb_dyn_friction').read() == '0'


def test_an_older_driver_has_none_of_it(tmp_path):
    dyn = fd.Dynamics(wheel_dir(tmp_path, ext_force='0\n'))
    assert not dyn.available() and not any(dyn.has(n) for n in dyn.RW + dyn.RO)
    assert dyn.hands() is None and dyn.live() is None and dyn.align_k_limit() is None and dyn.range() is None
    dyn.off()                                                          # nothing to set back, no error
    stage = fd.DynStage(wheel_dir(tmp_path, ext_force='0\n'))
    stage.start(0.0)
    stage.column(0.0, 0.4, 2.1)
    stage.align(0.0, 10.0, 0.01, 0.75)
    stage.keep(1.0)
    stage.stop(1.0)
    assert not stage.usable() and not stage.can_align()
    assert fd.Dynamics(None).hands() is None and not fd.Dynamics(None).has('ffb_align')


def test_garbage_in_a_read_only_attribute_reads_as_absent(tmp_path):
    dyn = fd.Dynamics(wheel_dir(tmp_path, ffb_hands='x y\n', ffb_dyn_live='1 2\n', ffb_align='0 0 0 nan\n'))
    assert dyn.hands() is None and dyn.live() is None and dyn.align_k_limit() is None


def test_the_device_reads_hands_and_live_friction(tmp_path):
    from oversteer.device import Device
    path = wheel_dir(tmp_path, ffb_hands='30 0\n', ffb_dyn_live='500 470 0.0\n')
    dev = SimpleNamespace(dev_path=path)
    assert Device.get_ffb_hands(dev) == (30.0, False) and Device.get_ffb_dyn_live(dev) == (500.0, 470.0, 0.0)
    assert Device.get_ffb_hands(SimpleNamespace(dev_path=wheel_dir(tmp_path / 'x', ext_force='0'))) is None


def test_the_stage_writes_the_column_when_it_changes_and_zero_when_off(tmp_path):
    path = wheel_dir(tmp_path, ffb_dyn_damping='0\n', ffb_dyn_friction='0\n', ffb_align='0 0 0 60000\n', range='900\n')
    seen = []
    stage = fd.DynStage(path, lambda t, name, *v: seen.append((name, v)))
    stage.start(0.0)
    assert stage.range == 900 and stage.k_limit == pytest.approx(0.06)
    read = [v for name, v in seen if name == 'ffb_align_read']
    assert read == [pytest.approx((60000.0, 900.0))]
    stage.column(0.0, 0.4, 2.1)
    want = fd.column_targets(0.4, 2.1)
    assert (int(open(path + '/ffb_dyn_damping').read()), int(open(path + '/ffb_dyn_friction').read())) == want
    n = len(seen)
    stage.column(0.1, 0.401, 2.1)                                      # a 0.25 % change: no write
    assert len(seen) == n
    stage.column(0.2, 0.2, 2.1)
    assert int(open(path + '/ffb_dyn_friction').read()) != want[1]
    stage.align(0.3, 20.0, 0.02, 0.75)
    assert open(path + '/ffb_align').read() == fd.align_text(20.0, 0.02, 0.75, 900)
    stage.stop(0.4)
    assert open(path + '/ffb_align').read() == '0 0 0' and open(path + '/ffb_dyn_damping').read() == '0' \
        and open(path + '/ffb_dyn_friction').read() == '0'
    n = len(seen)
    stage.stop(0.5)                                                    # already off: nothing again
    assert len(seen) == n
    stage.column_on = False
    stage.column(1.0, 0.4, 2.1)
    assert open(path + '/ffb_dyn_damping').read() == '0'               # the switch off: 0 written, not the targets


def test_the_wheel_calibration_goes_to_ffb_dyn_wheel_at_the_start(tmp_path):
    path = wheel_dir(tmp_path, ffb_dyn_wheel='470 670 500 1250 5000\n', ffb_dyn_damping='0\n', range='900\n')
    stage = fd.DynStage(path)
    stage.set_wheel_result(dict(G29, fit_latency_ms=7.0, friction=0.04))
    stage.start(0.0)
    assert open(path + '/ffb_dyn_wheel').read() == '470 670 400 1250 7000'
    assert stage.own[2] == 400                                         # the column is judged against it


def test_the_spring_is_kept_alive_and_dropped_with_k_zero(tmp_path):
    path = wheel_dir(tmp_path, ffb_align='0 0 0 60000\n', range='900\n')
    stage = fd.DynStage(path)
    stage.start(0.0)
    stage.align(1.0, 5.0, 0.02, 0.75)
    open(path + '/ffb_align', 'w').write('stale')
    stage.keep(1.005)                                                  # too soon
    assert open(path + '/ffb_align').read() == 'stale'
    stage.keep(1.02)
    assert open(path + '/ffb_align').read() == fd.align_text(5.0, 0.02, 0.75, 900)
    stage.align(1.03, 5.0, 0.0, 0.75)                                  # k = 0 always goes through
    assert open(path + '/ffb_align').read().split()[1] == '0'
    stage.keep(2.0)                                                    # nothing to keep alive
    assert open(path + '/ffb_align').read().split()[1] == '0'


# -- the rack force: the spring in the wheel's frame, a reset --

def rack(peak=2.1):
    r = fe.RackForce()
    r.LEARNS = False
    r.wheel_peak = peak
    r.mapping = 'knee'
    r.reset('acr/x')
    r._align.s[0] = 1e4
    r.ratio_known = r.lock_known = True
    r.ready = True
    return r


def slip_sample(t, steer=0.3, alpha=0.05, pos=(0.0, 0.0, 0.0), forward=(0.0, 0.0, 1.0)):
    s = Sample(6000.0, 7500.0, gear=3, speed=25.0, car='acr/x', game='acr')
    s.tyre_mz = (-150.0, -150.0, 0, 0)
    s.tyre_slip_angle = (alpha, alpha, 0.0, 0.0)
    s.steer = steer
    s.pos, s.forward = pos, forward
    return s


def drive(r, t0, seconds, writes, dt=0.005, **kw):
    t = t0
    while t < t0 + seconds:
        r._now = t
        r.force(slip_sample(t, **kw))
        t += dt
    return t


def test_the_spring_is_worked_out_in_the_wheels_own_position_frame():
    r = rack()
    r.align_on = True
    seen = []
    r.align_writer = lambda *a: seen.append(a)
    s = slip_sample(0.0)
    car = r.align(s)
    r.force(s)
    assert seen[-1][0] == pytest.approx(car['theta_a_deg'])             # no range known: the car's own frame
    seen.clear()
    r.wheel_range = 900.0
    r._now = 0.005
    r.force(s)
    g = 450.0 / (r.lock * r.ratio * 180.0 / math.pi)
    theta_a, k, tau = seen[-1]
    assert theta_a == pytest.approx(max(-450.0, min(450.0, car['theta_a_deg'] * g)))
    slope = wheel_torque.rim_share_slope(r.rim_torque(s), 2.1, 1.0, r.top_torque(), r.knee)
    assert k == pytest.approx(slope * car['k'] / 2.1 / g)
    # steer 1 at the lock puts the zero-slip angle at the wheel's own steer * range / 2, whatever the car's lock
    s = slip_sample(0.01, steer=0.5, alpha=0.0)
    r._now = 0.01
    r.force(s)
    assert seen[-1][0] == pytest.approx(0.5 * 450.0)
    s = slip_sample(0.02, steer=1.0, alpha=-0.2)                       # held to the range
    r._now = 0.02
    r.force(s)
    assert seen[-1][0] == 450.0


def test_a_measured_wheel_slope_scales_the_target_and_the_stiffness():
    r = rack()
    r.align_on = True
    r.wheel_range = 900.0
    seen = []
    r.align_writer = lambda *a: seen.append(a)
    s = slip_sample(0.0, steer=0.5, alpha=0.0)
    r._now = 0.0
    r.force(s)
    theta_a1, k1, _ = seen[-1]
    r.wheel_slope = 0.6
    r._now = 0.005
    r.force(s)
    theta_a2, k2, _ = seen[-1]
    assert theta_a2 == pytest.approx(0.6 * theta_a1)                    # the wheel turns 0.60 of the expected angle
    assert k2 == pytest.approx(k1 / 0.6)                                # the same torque per (fewer) wheel degrees


def test_a_reset_writes_k_zero_first_and_comes_back_after_the_hold():
    r = rack()
    r.align_on = True
    r.wheel_range = 900.0
    seen = []
    r.align_writer = lambda theta, k, tau: seen.append((r._now, k))
    t = drive(r, 0.0, 1.0, seen)
    assert seen[-1][1] > 0.0
    r._now = t
    r.force(slip_sample(t, pos=(500.0, 0.0, 0.0)))                      # a teleport 500 m away
    assert seen[-1] == (t, 0.0)                                         # k = 0 on the very sample
    held = drive(r, t + 0.005, 0.4, seen, pos=(500.0, 0.0, 0.0))
    assert all(k == 0.0 for at, k in seen if t <= at < held)
    drive(r, held, 0.6, seen, pos=(500.0, 0.0, 0.0))
    assert seen[-1][1] > 0.0                                            # back after the hold


def test_a_heading_flip_is_a_reset_and_driving_is_not():
    r = rack()
    r.align_on = True
    r.align_writer = lambda *a: None
    drive(r, 0.0, 0.5, [], pos=(0.0, 0.0, 0.0))
    assert r._align_hold is None
    t = 0.5
    for i in range(50):                                                 # 25 m/s in a straight line, a hard turn: no reset
        t += 0.005
        r._now = t
        r.force(slip_sample(t, pos=(0.125 * i, 0.0, 0.0), forward=(math.sin(0.1 * i), 0.0, math.cos(0.1 * i))))
    assert r._align_hold is None
    r._now = t + 0.005
    r.force(slip_sample(t + 0.005, pos=(6.0, 0.0, 0.0), forward=(0.0, 0.0, -1.0)))   # spun round 180 degrees at once
    assert r._align_hold is not None


def test_the_split_waits_for_the_ratio_the_lock_and_the_fit():
    r = fe.RackForce()
    r.LEARNS = False
    r.reset('acr/unknown-car')
    assert not r.align_ready()
    r.ready = True
    r._align.s[0] = 1e4
    r.lock_known, r.ratio_known = True, False
    assert not r.align_ready()
    r.lock_known, r.ratio_known = False, True
    assert not r.align_ready()
    r.lock_known = True
    assert r.align_ready()
    r._align.s[0] = 10.0
    assert not r.align_ready()


# -- the engine --

def dyn_wheel(tmp_path, **extra):
    files = dict(ffb_align='0 0 0 60000\n', ffb_dyn_damping='0\n', ffb_dyn_friction='0\n',
                 ffb_dyn_wheel='470 670 500 1250 5000\n', ffb_hands='0 0\n', ffb_dyn_live='500 470 0.0\n',
                 range='900\n')
    files.update(extra)
    path = tfe.wheel(tmp_path)
    for name, text in files.items():
        open(os.path.join(path, name), 'w').write(text)
    return path


class Pos:
    """The physical wheel's ABS_X as the engine reads it: follows the game's steering (steer + = left = decreasing
    ABS_X); `sign` -1 is an inverted axis, `scale` another range than the game maps."""

    def __init__(self, sign=1.0, scale=1.0):
        self.sign, self.scale, self.raw = sign, scale, 32767

    def __call__(self):
        return self.raw

    def follow(self, steer):
        self.raw = int(round(min(max((1.0 - self.sign * self.scale * steer) / 2.0, 0.0), 1.0) * 65535))


POS = Pos()


@pytest.fixture(autouse=True)
def _short_frame_window(monkeypatch):
    monkeypatch.setattr(fd.FrameCheck, 'MIN_SECONDS', 2.0)
    POS.sign, POS.scale, POS.raw = 1.0, 1.0, 32767


def playing_engine(path):
    engine = fe.FfbEngine(clock=lambda: 0.0, driver=tfe.FakeDriver)
    engine.set_position_source(POS)
    engine.set_device(path)
    engine.set_enabled(True)
    engine.step(0.0)
    t = tfe.run(engine, 0.0, 8.0)
    assert engine._phase in ('in', 'on')
    engine._frame.reset()                # (the check is fed from the start; tfe.run's wheel does not follow the steering)
    return engine, t


def feed(engine, t, seconds, **kw):
    end = t + seconds
    while t < end:
        s = tfe.acr(t)
        s.tyre_slip_angle, s.pos, s.forward = (0.04, 0.04, 0, 0), (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)
        for k, v in kw.items():
            setattr(s, k, v)
        POS.follow(s.steer)
        tfe.upload(tfe.game_raw(s))
        engine.offer(t, s)
        engine.step(t)
        t += 1.0 / tfe.HZ
    return t


def test_playing_writes_the_column_and_the_stage_is_zero_again_when_it_stops(tmp_path):
    path = dyn_wheel(tmp_path)
    engine, t = playing_engine(path)
    want = fd.column_targets(engine.model.impedance(), engine.model.wheel_peak, damping_scale=engine.model.damping_scale())
    assert (int(open(path + '/ffb_dyn_damping').read()), int(open(path + '/ffb_dyn_friction').read())) == want
    assert want[1] >= 500
    engine.set_enabled(False)
    engine.step(t)
    feed(engine, t, 1.0)
    engine.stop()
    assert open(path + '/ffb_dyn_damping').read() == '0' and open(path + '/ffb_dyn_friction').read() == '0'
    assert open(path + '/ffb_align').read() == '0 0 0'


def test_the_column_switch_and_friction_removal_reach_the_driver(tmp_path):
    path = dyn_wheel(tmp_path)
    engine = fe.FfbEngine(clock=lambda: 0.0, driver=tfe.FakeDriver)
    engine.set_device(path)
    engine.set_dynamics(False, False, True)
    engine.set_enabled(True)
    engine.step(0.0)
    tfe.run(engine, 0.0, 8.0)
    assert engine._dyn.removal and not engine._dyn.column_on
    assert open(path + '/ffb_dyn_damping').read() in ('0', '0\n') and open(path + '/ffb_dyn_friction').read().strip() == '0'
    assert open(path + '/ffb_align').read().startswith('0 0 0')
    assert not engine.model.align_on
    engine.stop()


def test_the_spring_plays_only_when_the_split_is_ready_and_the_driver_has_it(tmp_path):
    path = dyn_wheel(tmp_path)
    engine, t = playing_engine(path)
    assert not engine.model.align_on and engine.model.wheel_range == 900.0
    assert engine.model.align_k_limit == pytest.approx(0.06 / max(engine._strength, 0.05))
    m = engine.model
    m.ratio_known = m.lock_known = True
    m._align.s[0] = 1e4
    t = feed(engine, t, 3.5)
    assert m.align_on and engine.align_note is None
    words = open(path + '/ffb_align').read().split()
    assert len(words) == 3 and int(words[1]) > 0 and 0 < int(words[2]) <= 8000
    # the toggle off: the spring is set to 0 and the whole torque goes through ext_force again
    engine.set_dynamics(True, False, False)
    t = feed(engine, t, 0.2)
    assert not m.align_on and open(path + '/ffb_align').read() == '0 0 0'
    engine.stop()


def ready_engine(path):
    engine, t = playing_engine(path)
    m = engine.model
    m.ratio_known = m.lock_known = True
    m._align.s[0] = 1e4
    return engine, m, t


def test_the_frame_check_is_fed_and_the_slope_known_before_the_ratio_gate_opens(tmp_path):
    POS.scale = 0.6
    engine, t = playing_engine(dyn_wheel(tmp_path))
    m = engine.model
    assert not (m.ratio_known and m.lock_known)                         # the spring's gate is shut ...
    t = feed(engine, t, 6.0)
    assert not m.align_on
    assert engine._frame.ok and engine._frame.slope == pytest.approx(0.6, abs=0.02)      # ... the slope is there
    engine.stop()


def test_the_lock_stop_resists_the_wheel_past_the_games_lock_and_is_silent_inside_it(tmp_path):
    POS.scale = 0.6                                                     # the game's lock: 270 of the 450 degrees
    engine, t = playing_engine(dyn_wheel(tmp_path))
    t = feed(engine, t, 6.0)
    assert engine._frame.ok
    def settle(steer, raw_deg, seconds=0.3):
        nonlocal t
        end = t + seconds
        while t < end:
            s = tfe.acr(t)
            s.tyre_slip_angle, s.pos, s.forward = (0.04, 0.04, 0, 0), (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)
            s.steer = steer
            POS.raw = int(round((raw_deg / 900.0 + 0.5) * 65535))
            tfe.upload(tfe.game_raw(s))
            engine.offer(t, s)
            engine.step(t)
            t += 1.0 / tfe.HZ
        return engine._lock_stop_force(t, s), engine._lock_stop.active
    inside, active = settle(0.9, -243.0)                                # inside the lock
    assert inside == 0 and not active
    push, active = settle(1.0, 285.0)                                   # 15 degrees past it on the +ABS_X side
    assert active and 0.4 * fe.FULL <= push <= fe.LockStop.MAX * fe.FULL * engine._strength
    push_other, _ = settle(-1.0, -285.0)
    assert push_other < 0 < push
    engine.stop()


def _store_slope(engine, slope, wheel_range=900.0, sens='unset'):
    sens = engine._dyn.sensitivity if sens == 'unset' else sens
    cp.STORE.set_frame(engine.model.car, wheel_range, sens, slope)


def _push_at(engine, t, raw_deg, steer=1.0, seconds=0.3):
    """Feed samples with the wheel held at `raw_deg`; (time, the lock stop's push, active)."""
    end = t + seconds
    while t < end:
        s = tfe.acr(t)
        s.tyre_slip_angle, s.pos, s.forward = (0.04, 0.04, 0, 0), (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)
        s.steer = steer
        POS.raw = int(round((raw_deg / 900.0 + 0.5) * 65535))
        tfe.upload(tfe.game_raw(s))
        engine.offer(t, s)
        engine.step(t)
        t += 1.0 / tfe.HZ
    return t, engine._lock_stop_force(t, s), engine._lock_stop.active


def test_a_stored_slope_enables_the_lock_stop_at_once_but_not_the_spring(tmp_path):
    path = dyn_wheel(tmp_path)
    engine, t = playing_engine(path)
    assert not engine._frame.ok
    _store_slope(engine, 0.6)
    engine._prov_key = None                                             # (a new session reads the store)
    t, push, active = _push_at(engine, t, 285.0)                        # 15 degrees past the stored lock (270)
    assert not engine._frame.ok and active and push > 0.4 * fe.FULL * 0.5
    _, inside, active = _push_at(engine, t, 200.0, steer=0.8)
    assert inside == 0 and not active
    m = engine.model
    m.ratio_known = m.lock_known = True
    m._align.s[0] = 1e4
    assert not m.align_on and m.wheel_slope == 1.0                      # the spring waits for the live check
    engine.stop()


def test_a_slope_stored_for_another_range_or_sensitivity_is_not_used(tmp_path):
    engine, t = playing_engine(dyn_wheel(tmp_path))
    _store_slope(engine, 0.6, wheel_range=540.0)
    _store_slope(engine, 0.6, sens=80)
    engine._prov_key = None
    _, push, active = _push_at(engine, t, 285.0)
    assert push == 0 and not active and engine._prov is None
    engine.stop()


def test_a_stored_slope_outside_the_sane_range_is_rejected(tmp_path):
    engine, t = playing_engine(dyn_wheel(tmp_path))
    for bad in (0.2, 2.5, -0.6):
        _store_slope(engine, bad)
        assert cp.STORE.get_frame(engine.model.car, 900.0, engine._dyn.sensitivity) is None
    engine.stop()


def test_a_live_slope_that_disagrees_drops_the_stored_one_and_logs_once(tmp_path, caplog):
    POS.scale = 0.6
    engine, t = playing_engine(dyn_wheel(tmp_path))
    _store_slope(engine, 0.9)                                           # the wheel setup changed meanwhile
    engine._prov_key = None
    t, push, _active = _push_at(engine, t, 285.0, seconds=0.1)
    assert push == 0 and engine._prov == 0.9                            # provisional: lock 405, 285 is inside
    engine._frame.reset(); engine._pos_cache = None
    with caplog.at_level('INFO'):
        t = feed(engine, t, 6.0)
        engine._lock_stop_force(t, tfe.acr(t))
    assert engine._frame.ok and engine._prov is None
    assert len([r for r in caplog.records if 'stored value dropped' in r.getMessage()]) == 1
    assert cp.STORE.get_frame(engine.model.car, 900.0, engine._dyn.sensitivity) == pytest.approx(0.6, abs=0.02)
    t, push, active = _push_at(engine, t, 285.0, seconds=0.1)           # the live lock (270) now holds
    assert active and push > 0
    engine.stop()


def test_a_live_slope_of_the_other_sign_drops_the_stored_one(tmp_path):
    engine, t = playing_engine(dyn_wheel(tmp_path))
    _store_slope(engine, 0.6)
    engine._prov_key = None
    engine._lock_slope(engine._dyn)
    assert engine._prov == 0.6
    engine._frame.ok, engine._frame.slope = True, -0.6
    assert engine._lock_slope(engine._dyn) == -0.6 and engine._prov is None
    assert cp.STORE.get_frame(engine.model.car, 900.0, engine._dyn.sensitivity) is None
    engine.stop()


def test_a_live_slope_that_agrees_keeps_it_and_a_passing_check_stores_the_slope(tmp_path):
    POS.scale = 0.6
    engine, t = playing_engine(dyn_wheel(tmp_path))
    t = feed(engine, t, 6.0)
    assert engine._frame.ok
    engine._lock_stop_force(t, tfe.acr(t))
    assert cp.STORE.get_frame(engine.model.car, 900.0, engine._dyn.sensitivity) == pytest.approx(0.6, abs=0.02)
    engine.stop()


def test_an_inverted_axis_keeps_the_spring_off_and_says_why(tmp_path, caplog):
    POS.sign = -1.0
    engine, m, t = ready_engine(dyn_wheel(tmp_path))
    with caplog.at_level('WARNING'):
        t = feed(engine, t, 6.0)
    assert not m.align_on and open(engine._path + '/ffb_align').read().startswith('0 0 0')
    assert "doesn't match the game's steering" in engine.align_note and 'inverted' in engine.align_note
    assert len([r for r in caplog.records if "doesn't match" in r.getMessage()]) == 1       # said once
    engine.stop()


def test_a_wheel_range_the_game_does_not_map_keeps_it_off(tmp_path):
    POS.scale = 0.15                                                    # a sixth of the travel for the same steering
    engine, m, t = ready_engine(dyn_wheel(tmp_path))
    feed(engine, t, 6.0)
    assert not m.align_on and 'scale' in engine.align_note
    engine.stop()


def test_a_game_mapping_less_of_the_wheel_plays_with_the_measured_slope(tmp_path):
    POS.scale = 0.6                                                     # ACR: 540 deg of game lock on the 900 deg wheel
    engine, m, t = ready_engine(dyn_wheel(tmp_path))
    feed(engine, t, 6.0)
    assert m.align_on and engine.align_note is None
    assert m.wheel_slope == pytest.approx(0.6, abs=0.02)
    engine.stop()


def test_a_matching_wheel_turns_it_on_only_after_the_window_and_a_failure_turns_it_off(tmp_path):
    engine, m, t = ready_engine(dyn_wheel(tmp_path))
    t = feed(engine, t, 1.0)
    assert not m.align_on                                               # not enough movement seen yet
    t = feed(engine, t, 4.0)
    assert m.align_on and engine.align_note is None
    POS.sign = -1.0                                                     # the axis is inverted from now on (a proxy)
    t = feed(engine, t, 40.0)
    assert not m.align_on and engine.align_note
    engine.stop()


def test_no_wheel_position_means_no_spring(tmp_path):
    engine, m, t = ready_engine(dyn_wheel(tmp_path))
    engine.set_position_source(None)
    feed(engine, t, 6.0)
    assert not m.align_on and 'unavailable' in engine.align_note
    engine.stop()


def test_a_range_change_or_a_new_source_resets_the_check(tmp_path):
    path = dyn_wheel(tmp_path)
    engine, m, t = ready_engine(path)
    t = feed(engine, t, 5.0)
    assert m.align_on
    open(path + '/range', 'w').write('540\n')                           # the user changes the range
    engine._dyn._poll_at = None                                         # (read again at the next sample)
    t = feed(engine, t, 0.1)
    assert not m.align_on and not engine._frame.ok                      # the window starts again
    t = feed(engine, t, 5.0)
    assert m.align_on
    engine.set_position_source(Pos())
    engine.step(t)
    assert not engine._frame.ok and engine._frame._pairs == []
    engine.stop()


def test_the_games_force_sign_gate_is_required_too(tmp_path):
    engine, m, t = ready_engine(dyn_wheel(tmp_path))
    t = feed(engine, t, 5.0)
    assert m.align_on
    m.ready = False
    assert not m.align_ready()
    engine.stop()


def test_the_frame_check_needs_movement_correlation_slope_and_time():
    def run(sign=1.0, scale=1.0, seconds=25.0, amp=0.3, offset=0.0):
        c = fd.FrameCheck()
        c.MIN_SECONDS = 20.0
        t = 0.0
        while t < seconds:
            steer = amp * math.sin(2 * math.pi * 0.3 * t)
            c.feed(t, steer, -steer * 450.0, sign * scale * -steer * 450.0 + offset)
            t += 0.05
        return c
    assert run().ok and run().failed() is None
    assert not run(seconds=15.0).ok and run(seconds=15.0).failed() is None           # waiting
    assert not run(sign=-1.0).ok and 'inverted' in run(sign=-1.0).failed()
    assert not run(scale=0.2).ok and 'scale' in run(scale=0.2).failed()
    assert not run(scale=2.5).ok
    assert run(scale=0.6).ok and run(scale=0.6).slope == pytest.approx(0.6)       # ACR: 540 deg lock on a 900 deg wheel
    assert run(scale=0.85).ok and run(scale=1.2).ok
    assert not run(sign=-1.0, scale=0.6).ok
    assert not run(amp=0.05).ok and run(amp=0.05).failed() is None                  # steering barely moved
    assert run(offset=20.0).ok                                                        # a centre offset is fine
    c = run()
    c.reset()
    assert not c.ok and c._pairs == []


def test_a_car_reset_in_the_engine_writes_k_zero(tmp_path):
    path = dyn_wheel(tmp_path)
    engine, t = playing_engine(path)
    m = engine.model
    m.ratio_known = m.lock_known = True
    m._align.s[0] = 1e4
    t = feed(engine, t, 3.5)
    assert int(open(path + '/ffb_align').read().split()[1]) > 0
    t = feed(engine, t, 0.01, pos=(900.0, 0.0, 40.0))
    assert open(path + '/ffb_align').read().split()[1] == '0'
    engine.stop()


def test_a_driver_without_the_stage_changes_nothing(tmp_path):
    path = tfe.wheel(tmp_path)
    engine, t = playing_engine(path)
    assert engine._dyn is not None and not engine._dyn.usable() and not engine.model.align_on
    assert sorted(os.listdir(path)) == sorted(['ext_force', 'game_ffb_level', 'game_const_scale', 'ext_force_cap',
                                               'game_ffb_raw'])
    engine.stop()


def test_a_driver_with_only_some_attributes_is_used_as_far_as_it_goes(tmp_path):
    path = tfe.wheel(tmp_path)
    open(path + '/ffb_dyn_damping', 'w').write('0\n')
    engine, t = playing_engine(path)
    assert int(open(path + '/ffb_dyn_damping').read()) > 0
    assert not os.path.exists(path + '/ffb_align') and not engine.model.align_on
    engine.stop()


def test_the_calibration_result_is_written_to_the_driver_when_playing_starts(tmp_path):
    path = dyn_wheel(tmp_path)
    engine = fe.FfbEngine(clock=lambda: 0.0, driver=tfe.FakeDriver)
    engine.set_device(path)
    engine.set_wheel_result(dict(G29, fit_latency_ms=9.0))
    engine.set_enabled(True)
    engine.step(0.0)
    tfe.run(engine, 0.0, 8.0)
    assert open(path + '/ffb_dyn_wheel').read() == '470 670 500 1250 9000'
    engine.stop()


# -- the calibration: the stage off during a run, put back after --

def test_a_calibration_writes_the_stage_off_and_puts_it_back():
    sim = SimWheel()
    sim.attrs.update(ffb_dyn_damping='600', ffb_dyn_friction='1400', ffb_align='12 3000 4000 60000')
    cal = wcal.Calibration(sim)
    cal._prepare()
    assert sim.attrs['ffb_dyn_damping'] == '0' and sim.attrs['ffb_dyn_friction'] == '0' \
        and sim.attrs['ffb_align'] == '0 0 0'
    first = [n for _t, n, _v in sim.writes if n in wcal.DYN_ATTRS]
    assert sorted(first) == sorted(wcal.DYN_ATTRS)
    assert cal.saved['ffb_align'] == '12 3000 4000'                    # k_limit, a read-only field, is not written back
    cal._restore()
    assert sim.attrs['ffb_dyn_damping'] == '600' and sim.attrs['ffb_dyn_friction'] == '1400' \
        and sim.attrs['ffb_align'] == '12 3000 4000'
    assert sim.last('ext_force') == '0' and sim.last('game_ffb_level') == '100'


def test_a_calibration_on_an_older_driver_touches_none_of_it():
    sim = SimWheel()
    cal = wcal.Calibration(sim)
    cal._prepare()
    cal._restore()
    assert not any(n in wcal.DYN_ATTRS for _t, n, _v in sim.writes) and not any(n in sim.attrs for n in wcal.DYN_ATTRS)


def test_the_stored_result_is_applied_to_ffb_dyn_wheel_and_live_friction_to_the_compensation():
    written = {}
    entry = wcal.new_entry(dict(G29, lut=list(range(0, 33 * 1000, 1000))[:33]))
    assert wcal.apply_dyn_wheel(lambda n, v: written.update({n: v}), entry) == '470 670 500 1250 5000'
    assert written == {'ffb_dyn_wheel': '470 670 500 1250 5000'}
    assert wcal.apply_dyn_wheel(lambda n, v: written.update({n: v}), {'result': {}}) is None
    with pytest.raises(OSError):
        wcal.apply_dyn_wheel(lambda n, v: (_ for _ in ()).throw(OSError('no such attribute')), entry)


def test_live_friction_replaces_the_measured_one_in_the_compensation(g29_like):
    g29_result = g29_like[2]
    plain = wcal.compensated_lut(g29_result, 40)
    live = wcal.compensated_lut(g29_result, 40, 0.5 * g29_result['friction'])
    assert live != plain
    assert wcal.compensated_lut(g29_result, 40, g29_result['friction']) == plain
    assert wcal.correction_values({'use': True, 'result': g29_result, 'version': wcal.ENTRY_VERSION,
                                   'friction_comp': 40}, 0.5 * g29_result['friction'])['ffb_lut'] == ' '.join(map(str, live))


# -- the GUI's info line --

def test_the_info_line_shows_hands_and_the_live_friction(tmp_path):
    from oversteer.gui import Gui
    from oversteer.device import Device
    path = wheel_dir(tmp_path, ffb_hands='80 1\n', ffb_dyn_live='520 470 4.0\n')
    noted = []
    dev = SimpleNamespace(dev_path=path, get_ffb_thermal=lambda: (12, 0, 100),
                          get_ffb_hands=lambda: Device.get_ffb_hands(dev),
                          get_ffb_dyn_live=lambda: Device.get_ffb_dyn_live(dev))
    gui = SimpleNamespace(device=dev, ffb_engine=SimpleNamespace(car_properties=lambda: None),
                          _note_live_friction=noted.append)
    car, wheel = Gui.countersteer_info(gui)
    assert 'hands on' in wheel and '5.2 %' in wheel and '12 %' in wheel
    assert noted == [pytest.approx(0.052)]
    open(path + '/ffb_hands', 'w').write('0 0\n')
    open(path + '/ffb_dyn_live', 'w').write('500 470 0.0\n')
    car, wheel = Gui.countersteer_info(gui)
    assert 'hands off' in wheel and len(noted) == 1                    # no samples yet: the figure is not used
    for name in ('ffb_hands', 'ffb_dyn_live'):
        os.remove(path + '/' + name)
    assert Gui.countersteer_info(gui)[1] == 'Wheel motor heat: 12 %'
    gui.ffb_engine.align_note = "wheel position doesn't match the game's steering (x): the alignment spring is off"
    assert "doesn't match the game's steering" in Gui.countersteer_info(gui)[1]


# -- the profile keys --

def test_the_profile_keys_default_on_on_off_and_round_trip(tmp_path):
    from oversteer.model import Model
    assert Model.DYNAMICS_KEYS == {'countersteer_column': True, 'countersteer_align': True,
                                   'countersteer_friction_removal': False}
    defaults = Model._texture_defaults(True)
    assert defaults['countersteer_column'] is True and defaults['countersteer_align'] is True \
        and defaults['countersteer_friction_removal'] is False
    assert Model._texture_defaults(False)['countersteer_column'] is None
    model = SimpleNamespace(data={'countersteer_column': None, 'countersteer_align': False,
                                  'countersteer_friction_removal': True}, DYNAMICS_KEYS=Model.DYNAMICS_KEYS)
    assert Model.get_countersteer_dynamics(model) == (True, False, True)
    for key in Model.DYNAMICS_KEYS:
        assert Model.types[key] == 'boolean'


# -- the FFB tuning record --

def test_the_ffb_record_round_trips_every_channel(tmp_path):
    path = str(tmp_path / 'x.ffb.gz')
    sampler = ffb_record.FfbSampler(path, origin=100.0, header={'capture': 'x'})
    sampler.start()
    assert ffb_record.active()
    ffb_record.log(100.5, 'ext_force', 1234)
    ffb_record.log(100.6, 'game_ffb_raw', -300, 77, 2)
    ffb_record.log(100.7, 'ffb_align', '-3000 12000 7500')
    ffb_record.log(100.8, 'ffb_dyn_wheel', '470 670 500 1250 5000')
    ffb_record.log(100.9, 'ffb_hands', -55.0, 1)
    ffb_record.log(101.0, 'ffb_thermal', 14, 0, 100)
    ffb_record.log(101.1, 'nonsense', 1)
    sampler.stop()
    assert not ffb_record.active()
    ffb_record.log(102.0, 'ext_force', 9)                              # nothing recording: nothing happens
    meta, records = ffb_record.load(path)
    records = list(records)
    assert meta['capture'] == 'x' and set(meta['channels']) >= {'ext_force', 'ffb_align', 'ffb_hands', 'ffb_thermal'}
    assert [(round(t, 3), ch) for t, ch, _v in records] == [(0.5, 'ext_force'), (0.6, 'game_ffb_raw'), (0.7, 'ffb_align'),
                                                            (0.8, 'ffb_dyn_wheel'), (0.9, 'ffb_hands'),
                                                            (1.0, 'ffb_thermal')]
    assert records[0][2][0] == 1234 and records[1][2][:3] == (-300, 77, 2) and records[2][2][:3] == (-3000, 12000, 7500)
    assert records[3][2] == (470, 670, 500, 1250, 5000) and records[4][2][:2] == (-55.0, 1)


def test_the_ffb_record_loader_refuses_other_files_and_reads_a_cut_tail(tmp_path):
    import gzip
    bad = tmp_path / 'bad.ffb.gz'
    with gzip.open(bad, 'wb') as f:
        f.write(b'nope\n')
    with pytest.raises(ValueError):
        ffb_record.load(str(bad))
    path = str(tmp_path / 'cut.ffb.gz')
    sampler = ffb_record.FfbSampler(path, 0.0)
    sampler.add(1.0, 'ext_force', (5,))
    sampler.pump()
    sampler.stop()
    with open(path, 'ab') as f:
        f.write(b'\x00\x01')
    assert len(list(ffb_record.load(path)[1])) in (0, 1)               # a damaged tail ends the records quietly


def test_the_engine_logs_what_an_offline_replay_needs(tmp_path):
    path = dyn_wheel(tmp_path, ffb_hands='-40 1\n', ffb_dyn_live='500 470 2.0\n', ffb_thermal='10 0 100\n')
    sampler = ffb_record.FfbSampler(str(tmp_path / 'r.ffb.gz'), 0.0)
    sampler.start()
    try:
        engine, t = playing_engine(path)
        m = engine.model
        m.ratio_known = m.lock_known = True
        m._align.s[0] = 1e4
        feed(engine, t, 3.5)
        engine.stop()
    finally:
        sampler.stop()
    channels = {ch for _t, ch, _v in ffb_record.load(str(tmp_path / 'r.ffb.gz'))[1]}
    assert channels >= {'game_ffb_raw', 'ext_force', 'ffb_align', 'ffb_dyn_damping', 'ffb_dyn_friction', 'ffb_hands',
                        'ffb_dyn_live', 'ffb_thermal', 'ffb_align_read', 'off'}


def test_an_ffb_tuning_capture_has_the_engine_sidecar(tmp_path):
    from oversteer import telemetry_capture as tc
    from . import test_capture as tcap
    rec = tcap._ffb_recorder(tmp_path)
    rec.set_ffb_tuning(True)
    packets = tcap._ovst5_stream(seconds=0.5)
    for t, d in packets:
        rec.packet(100.0 + t, ('127.0.0.1', 1), d, 5300)
        if t > 0.2:
            ffb_record.log(100.0 + t, 'ext_force', 100)
    path = rec.path
    rec.close()
    side = path + tc.FFB_SUFFIX
    assert os.path.exists(side) and side in tc.sidecars(path)
    meta, records = ffb_record.load(side)
    assert meta['capture'] == os.path.basename(path) and len(list(records)) > 10
    assert not ffb_record.active()
    # the sidecar counts with the capture and goes with it
    assert tc._wheel_size(path) > 0
    # a thinned capture has none
    thin = tcap._ffb_recorder(tmp_path / 'thin')
    for t, d in packets[:50]:
        thin.packet(100.0 + t, ('127.0.0.1', 1), d, 5300)
    thin_path = thin.path
    thin.close()
    assert not os.path.exists(thin_path + tc.FFB_SUFFIX)


def test_pruning_removes_the_engine_sidecar_with_its_capture(tmp_path):
    from oversteer import telemetry_capture as tc
    old = tmp_path / ('old' + tc.SUFFIX)
    old.write_bytes(b'x' * 100)
    (tmp_path / ('old' + tc.SUFFIX + tc.FFB_SUFFIX)).write_bytes(b'y' * 100)
    assert tc.prune(str(tmp_path), 10) == [str(old)]
    assert not any(n.startswith('old') for n in os.listdir(tmp_path))


# -- the wheel position source: the wheel's own node, or the proxy's virtual device --

class _Node:
    """A fake evdev node: absinfo of ABS_X over [lo, hi], value set by the test."""

    def __init__(self, lo, hi):
        self.lo, self.hi, self.value = lo, hi, lo

    def absinfo(self, code):
        return SimpleNamespace(min=self.lo, max=self.hi, value=self.value)


def _proxied(tmp_path, lo=-32768, hi=32767):
    tmp_path.mkdir(parents=True, exist_ok=True)
    node_file = tmp_path / 'event-virtual'
    node_file.write_text('')
    node = _Node(lo, hi)
    device = SimpleNamespace(dev_name=str(tmp_path / 'event-own-root-only'), _input_node=lambda: str(node_file),
                             _normalized_axis=lambda code, value: (code, value))
    return device, node, fd.position_source(device, opener=lambda path: node)


def test_the_source_reads_the_proxys_virtual_node_by_its_own_range(tmp_path):
    device, node, read = _proxied(tmp_path)
    node.value = node.lo
    assert read() == 0
    node.value = node.hi
    assert read() == 65535
    node.value = 0
    assert abs(read() - 32768) <= 1
    assert fd.position_source(None) is None
    device._input_node = lambda: None
    assert read() is None                                                # nothing readable: no spring


def test_the_wheels_own_node_goes_through_normalize_and_a_node_change_reopens(tmp_path):
    own = tmp_path / 'event-own'
    own.write_text('')
    node = _Node(0, 65535)
    device = SimpleNamespace(dev_name=str(own), _input_node=lambda: str(own),
                             _normalized_axis=lambda code, value: (code, 65535 - value))
    opened = []
    read = fd.position_source(device, opener=lambda path: opened.append(path) or node)
    node.value = 1000
    assert read() == 64535 and opened == [str(own)]
    read()
    assert len(opened) == 1                                              # kept open
    own.unlink()
    own.write_text('')                                                   # a new node under the same name
    read()
    assert len(opened) == 2


def _drive_with_node(tmp_path, invert):
    device, node, read = _proxied(tmp_path)
    path = dyn_wheel(tmp_path)
    engine = fe.FfbEngine(clock=lambda: 0.0, driver=tfe.FakeDriver)
    engine.set_position_source(read)
    engine.set_device(path)
    engine.set_enabled(True)
    engine.step(0.0)
    t = tfe.run(engine, 0.0, 8.0)
    engine._frame.reset()                # (fed from the start, while the node did not follow the steering)
    m = engine.model
    m.ratio_known = m.lock_known = True
    m._align.s[0] = 1e4
    end = t + 5.0
    while t < end:
        s = tfe.acr(t)
        s.tyre_slip_angle, s.pos, s.forward = (0.04, 0.04, 0, 0), (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)
        # the virtual node: steer + = left = decreasing ABS_X; a proxy that inverts it reads the other way
        frac = (1.0 + (1.0 if invert else -1.0) * s.steer) / 2.0
        node.value = int(round(node.lo + frac * (node.hi - node.lo)))
        tfe.upload(tfe.game_raw(s))
        engine.offer(t, s)
        engine.step(t)
        t += 1.0 / tfe.HZ
    on = m.align_on
    engine.stop()
    return on


def test_a_passthrough_proxy_turns_the_spring_on_and_an_inverting_one_keeps_it_off(tmp_path):
    assert _drive_with_node(tmp_path / 'a', invert=False) is True
    assert _drive_with_node(tmp_path / 'b', invert=True) is False


# -- the driver's steering sensitivity curve --

def test_the_sensitivity_curve_mirrors_the_drivers_and_inverts():
    assert fd.sensitivity_curve(0.3, 50) == 0.3
    assert fd.sensitivity_curve(1.0, 75) == pytest.approx(1.0) and fd.sensitivity_curve(-1.0, 25) == pytest.approx(-1.0)
    assert fd.sensitivity_curve(0.25, 75) == pytest.approx(0.5 * 0.25 + 0.5 * 0.5)       # 0.5 x + 0.5 sqrt(x)
    assert fd.sensitivity_curve(0.5, 25) == pytest.approx(0.5 * 0.5 + 0.5 * 0.25)        # 0.5 x + 0.5 x^2
    assert fd.sensitivity_curve(-0.25, 75) == pytest.approx(-0.375)
    for s in (0, 25, 49, 50, 51, 75, 100):
        for y in (-1.0, -0.4, -0.01, 0.0, 0.02, 0.3, 0.9, 1.0):
            assert fd.sensitivity_curve(fd.sensitivity_inverse(y, s), s) == pytest.approx(y, abs=1e-6)
    assert fd.sensitivity_inverse(0.3, None) is None and fd.sensitivity_inverse(0.3, 140) is None
    assert fd.sensitivity_gain(75, 1.0, 900.0) == pytest.approx(11.1, abs=0.2)           # the field finding
    assert fd.sensitivity_gain(50) == 1.0 and fd.sensitivity_gain(25, 1.0, 900.0) < 1.0


def test_the_sensitivity_warning_only_off_50():
    assert fd.sensitivity_warning(50, 900) is None and fd.sensitivity_warning(None, 900) is None
    text = fd.sensitivity_warning(75, 900)
    assert "×11 at 1°" in text and "game's steering lock/ratio" in text and "shake" in text


def test_the_frame_check_keeps_a_bent_position_off_but_a_linear_one_on():
    def run(sens, linear=True, sweep=0.5):
        c = fd.FrameCheck()
        c.MIN_SECONDS = 20.0
        c.linear = linear
        t = 0.0
        while t < 25.0:
            steer = sweep * math.sin(2 * math.pi * 0.3 * t)
            evdev = -steer                                       # the game's steering is linear in the evdev position
            raw = fd.sensitivity_inverse(evdev, sens)            # the spring's frame
            c.feed(t, steer, -steer * 450.0, raw * 450.0)
            t += 0.05
        return c
    assert run(50).ok                                            # linear: the same as before
    bent = run(75)
    assert not bent.ok and 'not linear' in bent.failed()
    assert run(75, linear=False).slope is not None               # (without the test the fit alone would pass or not)
    assert run(51).ok                                            # a slight bend is within 20 %
    assert not run(75, sweep=0.02).ok                            # too small a sweep to judge: waiting or failing, never on


def test_a_bent_sensitivity_keeps_the_alignment_spring_off_and_50_plays_it(tmp_path):
    for sens, plays in ((50, True), (75, False)):
        (tmp_path / str(sens)).mkdir()
        path = dyn_wheel(tmp_path / str(sens), sensitivity='%d\n' % sens)
        engine, t = playing_engine(path)
        m = engine.model
        m.ratio_known = m.lock_known = True
        m._align.s[0] = 1e4
        t = feed(engine, t, 6.0)
        assert engine._dyn.sensitivity == sens
        assert m.align_on is plays
        if not plays:
            assert 'linear' in (engine.align_note or ''), (engine._frame.reason, len(engine._frame._pairs), engine._frame.linear)
        open(path + '/sensitivity', 'w').write('50\n')           # the sensitivity changes: the check starts again
        t = feed(engine, t, 6.0)
        assert engine._frame_sens == 50 and m.align_on
        engine.stop()
