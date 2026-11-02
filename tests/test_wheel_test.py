"""The Test tab's logic against a fake wheel: nothing here opens an evdev node or sysfs of a real device."""
import errno
import os
import threading
from types import SimpleNamespace

import pytest
from evdev import ecodes

from oversteer.device import Device
from oversteer.linear_chart import LinearChart
from oversteer.signal import Signal
from oversteer.test import Test, TestAborted


class FakeInput:
    ff_effects_count = 4

    def __init__(self, upload_errno=None):
        self.upload_errno = upload_errno
        self.uploads = 0
        self.erased = []

    def erase_effect(self, effect_id):
        self.erased.append(effect_id)

    def upload_effect(self, effect):
        if self.upload_errno:
            raise OSError(self.upload_errno, os.strerror(self.upload_errno))
        self.uploads += 1
        return self.uploads

    def write(self, *a):
        pass


class FakeWheel:
    def __init__(self, input_device):
        self.input = input_device
        self.settings = {'range': 540, 'gain': 70, 'autocenter': 10}
        self.log = []

    def get_input_device(self):
        return self.input

    def get_range(self):
        return self.settings['range']

    def get_ff_gain(self):
        return self.settings['gain']

    def get_autocenter(self):
        return self.settings['autocenter']

    def set_range(self, v):
        self.settings['range'] = v

    def set_ff_gain(self, v):
        self.settings['gain'] = v

    def set_autocenter(self, v):
        self.settings['autocenter'] = v

    def get_last_axis_value(self, axis):
        return 32768


def run_guarded(test, body):
    done = []
    test.notify = lambda *a: done.append(a)
    test._guarded(body)
    return done


def test_unsupported_ff_ends_cleanly_and_restores_settings():
    wheel = FakeWheel(FakeInput(upload_errno=errno.ENOSYS))
    test = Test(wheel, lambda *a: None)
    done = run_guarded(test, test.test2)
    assert done[0][0] == 'error' and 'force feedback' in done[0][1]
    assert wheel.settings == {'range': 540, 'gain': 70, 'autocenter': 10}


def test_other_oserror_is_reported_not_raised():
    wheel = FakeWheel(FakeInput(upload_errno=errno.EIO))
    test = Test(wheel, lambda *a: None)
    done = run_guarded(test, test.test3)
    assert done[0][0] == 'error'
    assert wheel.settings['range'] == 540


def test_no_input_device_is_an_abort():
    wheel = FakeWheel(None)
    test = Test(wheel, lambda *a: None)
    done = run_guarded(test, test.test1)
    assert done[0][0] == 'error'


def test_test1_times_out_waiting_for_the_user():
    wheel = FakeWheel(FakeInput())
    test = Test(wheel, lambda *a: None)
    test.ACTION_TIMEOUT = -1
    done = run_guarded(test, test.test1)
    assert done[0][0] == 'error' and 'Timed out' in done[0][1]
    assert not test.is_awaiting_action()
    assert wheel.settings == {'range': 540, 'gain': 70, 'autocenter': 10}


def test_a_finished_test_restores_settings_and_notifies_end(monkeypatch):
    monkeypatch.setattr('oversteer.test.time.sleep', lambda s: None)
    wheel = FakeWheel(FakeInput())
    test = Test(wheel, lambda *a: None)
    done = run_guarded(test, test.test3)
    assert done == [()]
    assert wheel.settings == {'range': 540, 'gain': 70, 'autocenter': 10}
    assert not test.is_collecting_data()


def test_restores_the_profile_when_the_device_cannot_read_back():
    # mainline hid-logitech: no gain/autocenter files, the getters answer a hardcoded 100 and 0
    wheel = FakeWheel(FakeInput())
    wheel.settings = {'range': 900, 'gain': 100, 'autocenter': 0}
    wheel.get_ff_gain = lambda: 100
    wheel.get_autocenter = lambda: 0
    test = Test(wheel, lambda *a: None, {'range': 540, 'ff_gain': 60, 'autocenter': 20})
    run_guarded(test, lambda: None)
    assert wheel.settings == {'range': 540, 'gain': 60, 'autocenter': 20}


def test_leftover_effects_are_erased_on_failure():
    wheel = FakeWheel(FakeInput())
    test = Test(wheel, lambda *a: None)

    def body():
        test.create_left_effect()
        raise OSError(errno.EIO, 'boom')
    run_guarded(test, body)
    assert wheel.input.erased[-1] == 1   # after start()'s sweep of every slot


def test_no_samples_means_no_output_data():
    test = Test(FakeWheel(FakeInput()), lambda *a: None)
    test.output_values = [(0, 0.1)]
    assert not test.has_output_data()
    test.output_values.append((0.01, 0.2))
    assert test.has_output_data()


def test_empty_signal_filter_does_not_unpack():
    # Issue 77: zip(*[]) gave "not enough values to unpack (expected 2, got 0)"
    assert Signal([], resample=True).filter(5).get_values() == []
    assert Signal([(0, 0.0)], resample=True).filter(5).get_values() == []


def test_end_test_with_no_data_returns_to_idle():
    from oversteer.gui import Gui
    calls = []
    ui = SimpleNamespace(error_dialog=lambda *a: calls.append(('error',) + a),
                         switch_test_panel=lambda i: calls.append(('panel', i)))
    gui = SimpleNamespace(ui=ui, test_run=1, test=SimpleNamespace(has_output_data=lambda: False))
    gui.abort_test = lambda m='': Gui.abort_test(gui, m)
    Gui.end_test(gui)
    assert gui.test is None and gui.test_run is None
    assert calls[-1] == ('panel', None) and calls[0][0] == 'error'


# Issue 342: only the wheel's own sysfs node is looked at
def test_check_permissions_ignores_unrelated_unreadable_files(tmp_path, monkeypatch):
    wheel = tmp_path / 'wheel'
    wheel.mkdir()
    other = tmp_path / 'mouse'
    other.mkdir()
    (other / 'range').write_text('0')
    (other / 'range').chmod(0)
    (wheel / 'range').write_text('900')
    device = Device(None, {'dev_path': str(wheel)})
    checked = []
    real = os.access
    monkeypatch.setattr(os, 'access', lambda p, m: checked.append(str(p)) or real(p, m))
    assert device.check_permissions()
    assert all(p.startswith(str(wheel)) for p in checked)


def test_check_file_permissions_survives_unnamed_owner(tmp_path, monkeypatch):
    (tmp_path / 'range').write_text('900')
    import pwd, grp

    def nokey(_):
        raise KeyError('no such id')
    monkeypatch.setattr(pwd, 'getpwuid', nokey)
    monkeypatch.setattr(grp, 'getgrgid', nokey)
    assert Device(None, {'dev_path': str(tmp_path)}).check_file_permissions('range') is True
