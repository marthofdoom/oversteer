"""App-side safety (an audit's findings): the spike gate and slew, release after the driver's watchdog, the
game_ffb_level read-back, ACR write gaps, a new car mid-fade, a reversing car, the car properties file, the
correction held back while a force plays, one demo at a time, the wheel test's limits, the udev lists."""
import json
import math
import os
import re
from types import SimpleNamespace
from unittest import mock

import pytest

from oversteer import ffb_carprops as cp, ffb_engine as fe, wheel_owner
from oversteer.test import Test
from oversteer.telemetry_formats import Sample
from tests.test_ffb_engine import _clean_log, FakeDriver, active, forces, levels, run, HZ, acr, upload, game_raw
from tests.test_wheel_test import FakeInput, FakeWheel, run_guarded


# -- spike gate and slew --

def shaped(values, dt=0.003):
    e = fe.FfbEngine(driver=FakeDriver)
    return [e._shape(i * dt, v) for i, v in enumerate(values)]


def test_a_one_sample_outlier_never_reaches_the_wheel():
    out = shaped([1000, 1000, 30000, 1000, 1000])
    assert max(abs(v) for v in out) < 1100


def test_a_real_jump_passes_at_its_second_sample_within_the_slew():
    out = shaped([0, 0, 20000, 20000, 20000, 20000, 20000])
    assert out[2] == 0                                    # held back one sample
    step = fe.SLEW * 0.003
    assert out[3] == pytest.approx(min(20000, step))      # then on its way at the slew
    assert out[-1] == 20000


def test_the_slew_is_full_scale_in_ten_ms():
    out = shaped([0, 0] + [-fe.FULL] * 8, dt=0.001)
    # two samples to agree, then 1 ms steps of a tenth of full scale
    assert out[3] == pytest.approx(-fe.FULL * 0.1, abs=2)
    assert out[-1] <= -fe.FULL * 0.6


def test_changes_under_the_spike_threshold_pass_unchanged():
    assert shaped([0, 0, 5000, 9000]) == [0, 0, 5000, 9000]


def test_the_gate_state_is_forgotten_when_the_wheel_is_let_go():
    e = fe.FfbEngine(driver=FakeDriver)
    e._shape(0.0, 5000)
    e._let_go()
    assert e._shape(1.0, -20000) == -20000


# -- watchdog --

def test_a_write_gap_over_a_second_while_on_is_a_release_and_a_new_takeover_mutes_again(tmp_path):
    engine, path, t = active(tmp_path)
    engine.step(t + 1.5)
    assert levels() == [0, 100] and engine.last_stop == 'gap'
    assert not engine.status.active
    t = run(engine, t + 1.6, 3.5)                         # REARM later it takes over again, muting the game again
    assert levels() == [0, 100, 0]


def test_game_level_read_back_as_100_while_muted_is_a_release(tmp_path):
    engine, path, t = active(tmp_path)
    with open(path + '/game_ffb_level', 'w') as f:
        f.write('100\n')                                  # the driver's watchdog restored it
    run(engine, t, 0.5)
    assert engine.last_stop == 'level' and not engine.status.active
    assert levels() == [0, 100]                           # our release writes 100 once, no stacking


# -- ACR write gaps --

def test_the_last_value_is_written_again_in_a_gap_easing_down_until_the_stall(tmp_path):
    engine, path, t = active(tmp_path)
    last = t - 1 / HZ
    n, value = len(forces()), forces()[-1]
    engine.step(last + 0.005)
    assert len(forces()) == n
    engine.step(last + 0.012)
    assert len(forces()) == n + 1 and forces()[-1] == value
    engine.step(last + 0.024)
    assert len(forces()) == n + 2 and forces()[-1] == value
    engine.step(last + 0.105)                                # 75 ms into the easing: half way to 50 %
    assert forces()[-1] == pytest.approx(value * 0.75, abs=abs(value) * 0.02 + 2)
    engine.step(last + 0.25)
    assert forces()[-1] == pytest.approx(value * 0.5, abs=abs(value) * 0.02 + 2)
    assert engine.last_stop is None
    engine.step(last + 0.31)
    assert engine.last_stop == 'stall'


# -- a new car mid-fade, a reversing car --

def front_sample(mz=50.0, vel=None, car='acr/a'):
    s = Sample(6000.0, 7500.0, gear=3, speed=20.0, car=car, game='acr')
    s.tyre_mz = (mz / 2, mz / 2, 0, 0)
    s.vel = vel
    return s


def test_a_new_car_keeps_the_playing_model_through_the_fade():
    rack = fe.RackForce()
    rack.reset('acr/a')
    rack.assist = 0.0                                        # an unassisted car: the column torque is played
    rack.ready = True
    rack.hold()
    held = rack.force(front_sample(300.0, car='acr/b'))
    rack.reset('acr/b')
    assert not rack.ready and rack.assist == fe.RackForce.ASSIST
    assert rack.force(front_sample(300.0, car='acr/b')) == pytest.approx(held, rel=1e-6)
    rack.release()
    assert rack.force(front_sample(300.0, car='acr/b')) < held        # the assisted default is lighter


def test_a_reversing_car_is_bounded_not_faded():
    rack = fe.RackForce()
    rack.reset('acr/a')
    rack.assist = 0.0
    rack.wheel_peak = 2.1
    big = 2000.0
    assert rack.force(front_sample(big, vel=(10.0, 0.0, 0.0))) == pytest.approx(fe.FULL * 0.75, rel=0.01)
    back = rack.force(front_sample(big, vel=(-4.0, 0.0, 0.0)))
    assert back == pytest.approx(fe.FULL * min(fe.REVERSE_MAX, 0.75), rel=0.01)
    assert rack.force(front_sample(-big, vel=(-4.0, 0.0, 0.0))) == pytest.approx(-back)
    small = rack.force(front_sample(8.0, vel=(-4.0, 0.0, 0.0)))
    assert small == pytest.approx(rack.force(front_sample(8.0, vel=(4.0, 0.0, 0.0))))     # a modest force is unfaded


# -- the car properties file --

def test_a_damaged_car_file_cannot_put_nan_or_huge_values_into_the_force(tmp_path):
    path = tmp_path / 'carprops.json'
    path.write_text('{"a": {"trail": NaN, "scrub": 0.01}, "b": {"trail": 9.0, "scrub": -3}, '
                    '"c": {"trail": 0.02, "scrub": 0.01}, "d": {"trail": Infinity, "scrub": 0.0}}')
    store = cp.Store()
    store.use(str(path))
    assert store.get('a') is None and store.get('d') is None
    assert store.get('b') == (cp.TRAIL_RANGE[1], cp.SCRUB_RANGE[0])
    assert store.get('c') == (0.02, 0.01)


# -- the correction waits for any holder --

def gui_stub(held):
    from oversteer import gui
    me = mock.MagicMock()
    me.calibration = None
    me._wheel_supported.return_value = True
    me._wheel_entry.return_value = {'use': True}
    me._wheel_held.return_value = held
    return gui, me


def test_the_correction_is_not_written_under_a_playing_force():
    gui, me = gui_stub(('countersteer-ffb', False))
    with mock.patch.object(gui.wcal, 'apply_correction') as apply:
        gui.Gui.apply_wheel_correction(me)
        apply.assert_not_called()
        me._defer_wheel_writes.assert_called_once()


def test_the_correction_is_written_once_the_wheel_is_free():
    gui, me = gui_stub(None)
    with mock.patch.object(gui.wcal, 'apply_correction') as apply:
        gui.Gui.apply_wheel_correction(me)
        apply.assert_called_once()


def test_a_device_change_waits_for_a_playing_force():
    gui, me = gui_stub(('countersteer-ffb', False))
    me.device = mock.MagicMock()
    with mock.patch.object(gui.wcal, 'wheel_key', return_value='new'):
        gui.Gui.check_active_device(me)
    me.apply_wheel_correction.assert_not_called()


# -- demos --

def test_a_demo_is_refused_while_another_plays():
    wheel = '/dev/test-demo-wheel'
    assert wheel_owner.hold_for_demo(wheel, 0.05)
    try:
        assert not wheel_owner.hold_for_demo(wheel, 0.05)
        assert wheel_owner.demo_refusal(wheel)
    finally:
        wheel_owner.release(wheel, wheel_owner.DEMO)


# -- the wheel test --

class LockedWheel(FakeWheel):
    axis = 32768

    def get_last_axis_value(self, axis):
        return self.axis


class RecordingInput(FakeInput):
    def __init__(self):
        super().__init__()
        self.levels = []

    def upload_effect(self, effect):
        self.levels.append(effect.u.ff_constant_effect.level)
        return super().upload_effect(effect)


def test_the_wheel_test_never_pushes_past_the_cap(monkeypatch):
    monkeypatch.setattr('oversteer.test.time.sleep', lambda s: None)
    wheel = LockedWheel(RecordingInput())
    test = Test(wheel, lambda *a: None)
    run_guarded(test, test.test2)
    run_guarded(test, test.test3)
    assert max(wheel.input.levels) <= int(0x7fff * Test.MAX_FORCE)


def test_the_wheel_test_does_not_push_into_the_lock(monkeypatch):
    monkeypatch.setattr('oversteer.test.time.sleep', lambda s: None)
    wheel = LockedWheel(RecordingInput())
    wheel.axis = 32768 + 31500                            # at the right lock
    test = Test(wheel, lambda *a: None)
    writes = []
    wheel.input.write = lambda *a: writes.append(a)
    run_guarded(test, test.test3)
    on = [a for a in writes if a[2] == 1]
    assert len(on) == 2                                   # the seed and the left push; both right pushes skipped


def test_the_square_wave_ramp_times_out(monkeypatch):
    ticks = iter(range(10000))
    holder = {}

    def sleep(s):
        if holder['test'].awaiting_action and not holder.get('pressed'):
            holder['pressed'] = True
            holder['test'].action_triggered = True
    clock = SimpleNamespace(sleep=sleep, monotonic=lambda: float(next(ticks)), time=lambda: 0.0)
    monkeypatch.setattr('oversteer.test.time', clock)
    wheel = LockedWheel(RecordingInput())
    test = Test(wheel, lambda *a: None)
    test.ACTION_TIMEOUT = 5
    holder['test'] = test
    orig = test.test1

    def body():
        orig()
    done = run_guarded(test, body)
    assert done[-1][0] == 'error' and 'Timed out' in done[-1][1]


# -- udev --

def test_the_driver_attributes_are_in_every_udev_list_that_has_ext_force():
    path = os.path.join(os.path.dirname(__file__), '..', 'data', 'udev', '99-logitech-wheel-perms.rules')
    lines = [l for l in open(path) if ' ext_force ' in l]
    assert lines
    for l in lines:
        assert ' ffb_output_cap ' in l and ' ffb_thermal_limit ' in l
        for name in ('ffb_dyn_wheel', 'ffb_dyn_damping', 'ffb_dyn_friction', 'ffb_align'):
            assert f' {name} ' in l
        assert 'ffb_thermal ' not in l.replace('ffb_thermal_limit', '')    # read-only: no chmod
