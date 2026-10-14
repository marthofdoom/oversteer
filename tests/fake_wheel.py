"""A wheel that is not there: a fake device, window and controller around the real Model, for the web
page's Controls and FFB tabs (tests/test_web_controls.py, and the page's screenshots). Nothing here touches
sysfs, evdev or a display."""
import queue
import threading
from types import SimpleNamespace

from evdev import ecodes

from oversteer.device import Device
from oversteer.gui import Gui
from oversteer.model import Model

G29 = dict(range=900, max_range=900, sensitivity=50, ff_gain=100, autocenter=0, autocenter_persistent=False,
           app_gain=True, inertia_mode=False, combine_pedals=0, invert_pedals=0, spring_level=30,
           damper_level=30, friction_level=30, rumble_level=50, ffb_leds=0, rev_leds=True, pedal_response=True)


class FakeDevice:
    """Just enough of Device for the model and the web bridge. Every setting is a value in `values`;
    `get_x()` reads it, `set_x(v)` writes it and logs (name, value, thread), `touched` counts every call."""
    PEDAL_NAMES = Device.PEDAL_NAMES
    name = 'Fake Logitech G29'

    def __init__(self, **values):
        self.values = dict(G29, **values)
        self.responses = {}
        self.log = []
        self.touched = []
        self.demos = []

    def _note(self, name):
        self.touched.append(name)

    def get_id(self):
        return 'fake-g29'

    def is_ready(self):
        self._note('is_ready')
        return True

    def get_max_range(self):
        self._note('get_max_range')
        return self.values['max_range']

    def get_max_ff_gain(self):
        self._note('get_max_ff_gain')
        return 150 if self.values.get('app_gain') is not None else 100

    def has_pedal_response(self):
        self._note('has_pedal_response')
        return bool(self.values['pedal_response'])

    def has_rev_leds(self):
        self._note('has_rev_leds')
        return bool(self.values.get('rev_leds'))

    def pedal_axes(self, mask=None):
        self._note('pedal_axes')
        return {ecodes.ABS_Y: (65535, 0, 1), ecodes.ABS_Z: (65535, 0, 2), ecodes.ABS_RZ: (65535, 0, 4)}

    def get_pedal_response(self, bit):
        self._note('get_pedal_response')
        return self.responses.get(bit, (0, 100, 50)) if self.values['pedal_response'] else None

    def set_pedal_response(self, bit, start, end, sensitivity):
        self.log.append(('pedal_response', (bit, start, end, sensitivity), threading.get_ident()))
        self.responses[bit] = (start, end, sensitivity)
        return True

    def play_demo(self, kind, on_error=None):
        self.demos.append((kind, threading.get_ident()))
        return True

    def list_modes(self):
        return None

    def __getattr__(self, name):
        if name.startswith('get_'):
            key = name[4:]
            return lambda *a: (self._note(name), self.values.get(key))[1]
        if name.startswith('set_'):
            key = name[4:]

            def setter(value, *a):
                self.log.append((key, value, threading.get_ident()))
                self.values[key] = value
                return True
            return setter
        raise AttributeError(name)


class FakeUi:
    """The window: records the calls the model and the bridge make; `set_profile` acts like the profile box."""

    def __init__(self):
        self.calls = []
        self.pedal_bits = {'clutch': 1, 'accelerator': 2, 'brakes': 4}
        self.controller = None
        self.active_profile = ''

    def __getattr__(self, name):
        if name.startswith('_') or name == 'controller':
            raise AttributeError(name)
        return lambda *a, **k: self.calls.append((name,) + a)

    def set_profile(self, name):
        self.calls.append(('set_profile', name))
        self.active_profile = name
        self.controller.load_profile(name)


class FakeController:
    """The parts of Gui the bridge uses; the settings logic is the real Model and the real Gui methods."""
    reset_ffb_defaults = Gui.reset_ffb_defaults
    change_rev_leds_shift_unit = Gui.change_rev_leds_shift_unit
    try_effect = Gui.try_effect
    save_profile = Gui.save_profile

    def __init__(self, profile_path, device=None, enabled=True, token='pair-me-1234'):
        self.device = device if device is not None else FakeDevice()
        self.app = SimpleNamespace(profile_path=str(profile_path))
        self.ui = FakeUi()
        self.ui.controller = self
        self.model = Model(self.device, self.ui)
        self.telemetry = None
        self.telemetry_web_control = enabled
        self.web_token = token
        self.loaded = []
        self.revs = 0

    def update_pedals(self, mask=None):
        pass

    def apply_rev_leds(self):
        self.revs += 1

    def update_rev_leds_shift(self):
        pass

    def load_profile(self, name):
        self.loaded.append(name)
        self.model.load('{}/{}.ini'.format(self.app.profile_path, name))
        self.model.flush_device()
        self.model.flush_ui()


class MainLoop:
    """A stand-in for the GTK main loop: `schedule(fn)` runs fn on this one thread, as GLib.idle_add does."""

    def __init__(self):
        self.jobs = queue.Queue()
        self.thread = threading.Thread(target=self._run, name='fake-main', daemon=True)
        self.thread.start()
        self.ident = self.thread.ident

    def _run(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            job()

    def schedule(self, fn):
        self.jobs.put(fn)

    def stop(self):
        self.jobs.put(None)
        self.thread.join(2.0)
