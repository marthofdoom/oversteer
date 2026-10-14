"""The wheel's settings on the web page: the Controls and FFB tabs.

The page reads the settings from `Bridge.state()` (a snapshot built from
the model's own values, with nothing read from the device on the web
thread) and changes them through `Bridge.apply()`, which hands the change
to the GTK main thread (`schedule`, GLib.idle_add in the app) and waits
for the result. There the same model and controller calls run that the
GTK controls make, then the widgets are updated to match, so a change on
the page shows in the window at once and the other way round (the page
polls). Values are checked against the same ranges the GTK controls
have; a setting the model has no value for (the driver lacks it) is
neither shown nor accepted.

Who may change anything is decided by telemetry_web.py (the setting, the
pairing token, same origin, rate limits); this module only does the work.
"""

import glob
import logging
import os
import threading

from .model import Model

PEDALS = Model.PEDAL_RESPONSES
RANGE_PRESETS = (270, 360, 540, 720, 900)
RANGE_MIN = 40                                  # the GTK range scale's lower end (4 x 10)
LEVELS = ('spring_level', 'damper_level', 'friction_level', 'rumble_level')
REV_MODES = ('auto', 'percent', 'rpm')
SHIFT_LIMITS = {'percent': (50, 100), 'rpm': (1000, 25000)}
ACTIONS = ('reset_ffb', 'load_profile', 'save_profile', 'try_effect')
EFFECTS = ('constant', 'spring', 'damper', 'friction', 'rumble')
# Greyed in the GTK window while force feedback is off (GtkUi.set_ffb_enabled)
NEEDS_FFB = ('ff_gain', 'app_gain', 'inertia_mode') + LEVELS


class Rejected(Exception):
    """A change that can't be made: the HTTP status and what to tell the page."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def call_on_main(fn, schedule, timeout):
    """Run `fn()` on the main thread through `schedule` (GLib.idle_add) and
    return what it returns, or raise what it raised. After `timeout`
    seconds, a TimeoutError, and `fn` will not run if it has not started."""
    done = threading.Event()
    box = {}
    gate = threading.Lock()
    state = {'abandoned': False}

    def run():
        with gate:
            if state['abandoned']:
                return False
            state['started'] = True
        try:
            box['value'] = fn()
        except BaseException as e:             # handed to the waiting thread
            box['error'] = e
        finally:
            done.set()
        return False                           # idle_add: once

    schedule(run)
    if not done.wait(timeout):
        with gate:
            if not state.get('started'):
                state['abandoned'] = True
                raise TimeoutError("the window did not answer")
        done.wait(timeout)                     # it has started: let it finish
        if 'value' not in box and 'error' not in box:
            raise TimeoutError("the window did not finish")
    if 'error' in box:
        raise box['error']
    return box['value']


def _int(value, name, low, high, step=1):
    if isinstance(value, bool) or not isinstance(value, int):
        raise Rejected(400, "{} must be a whole number".format(name))
    if not low <= value <= high or (value - low) % step:
        raise Rejected(400, "{} must be {} to {}".format(name, low, high))
    return value


def _bool(value, name):
    if not isinstance(value, bool):
        raise Rejected(400, "{} must be true or false".format(name))
    return value


class Bridge:
    """What the web server needs from the app. `controller` is the GUI
    controller (oversteer/gui.py); `schedule(fn)` runs fn on the main
    thread."""

    def __init__(self, controller, schedule, timeout=5.0):
        self.controller = controller
        self.schedule = schedule
        self.timeout = timeout
        self.caps = {}

    # -- what the server asks --

    @property
    def enabled(self):
        return bool(getattr(self.controller, 'telemetry_web_control', False))

    @property
    def token(self):
        return getattr(self.controller, 'web_token', '') or ''

    def apply(self, payload):
        """(status, body) of a change, made on the main thread."""
        try:
            return call_on_main(lambda: self._apply(payload), self.schedule, self.timeout)
        except TimeoutError:
            return 504, {'error': "The Oversteer window did not answer"}

    # -- main thread --

    def refresh(self):
        """Read what only the device can say. Main thread; the app calls it
        every few seconds while the page is served, and after a change."""
        c = self.controller
        device = getattr(c, 'device', None)
        try:
            if device is None or not device.is_ready():
                self.caps = {}
                return
            ui = getattr(c, 'ui', None)
            self.caps = {
                'device': {'name': getattr(device, 'name', None) or '', 'id': device.get_id()},
                'max_range': device.get_max_range(),
                'max_gain': device.get_max_ff_gain(),
                'pedal_response': bool(device.has_pedal_response()),
                'bits': dict(getattr(ui, 'pedal_bits', None) or {}),
            }
        except Exception as e:
            logging.debug("web controls: can't read the device: %s", e)
            self.caps = {}

    def _apply(self, payload):
        try:
            if not self.caps:
                self.refresh()
            if not self.caps or self.controller.model is None:
                raise Rejected(409, "No wheel is connected")
            kinds = [k for k in ('set', 'do') if k in payload]
            if len(kinds) != 1 or set(payload) - {'set', 'do', 'arg'}:
                raise Rejected(400, "Send either set or do")
            if kinds[0] == 'set':
                self._set(payload['set'])
            else:
                self._do(payload['do'], payload.get('arg'))
            self.refresh()
            return 200, {'ok': True, 'state': self.state()}
        except Rejected as e:
            return e.status, {'error': e.message}
        except OSError as e:
            logging.warning("web controls: the wheel refused: %s", e)
            self.refresh()
            return 500, {'error': "The wheel refused the change: {}".format(e.strerror or e)}
        except Exception:
            logging.exception("web controls")
            return 500, {'error': "The change failed"}

    def _set(self, changes):
        if not isinstance(changes, dict) or not changes:
            raise Rejected(400, "set must hold at least one setting")
        model = self.controller.model
        # rev_leds_shift is checked against the mode it will be written under, so after rev_leds_mode
        order = sorted(changes, key=lambda k: k == 'rev_leds_shift')
        plan = []
        for key in order:
            if key == 'rev_leds_shift' and 'rev_leds_mode' in changes:
                plan.append((key, self._check_shift(changes[key], changes['rev_leds_mode'])))
            else:
                plan.append((key, self._check(key, changes[key])))
        try:
            for key, value in plan:
                self._write(model, key, value)
        finally:
            self._sync_ui(model, [key for key, _value in plan])

    def _available(self, model, key):
        """Raise unless the setting exists on this device (and is not greyed out)."""
        data = model.data
        if key in ('rev_leds_mode', 'rev_leds_shift', 'rev_leds_launch'):
            have = data.get('rev_leds') is not None
        elif key.startswith('invert_'):
            have = data.get('invert_pedals') is not None and self.caps['bits'].get(key[7:]) is not None
        elif key.endswith('_response'):
            have = data.get(key) is not None and self.caps['pedal_response'] \
                and self.caps['bits'].get(key[:-9]) is not None
        else:
            have = data.get(key) is not None
        if not have:
            raise Rejected(409, "{} is not available on this wheel".format(key))
        if key in NEEDS_FFB and data.get('ffb_enabled') is False:
            raise Rejected(409, "Force feedback is off")
        if key.endswith('_response') and data.get('combine_pedals'):
            raise Rejected(409, "The pedals are combined: their responses are ignored")
        if key == 'rev_leds_shift' and self._mode(data) == 'auto':
            raise Rejected(409, "The shift point is automatic")

    @staticmethod
    def _mode(data):
        """The Shift-at choice the GTK window shows."""
        if data.get('rev_leds') is not None and data.get('rev_leds_learnt') is not False:
            return 'auto'
        return data.get('rev_leds_shift_unit') or 'percent'

    def _check_shift(self, value, mode):
        """A shift point sent with a new mode: valid in that mode (never 'auto', which has no single point)."""
        model = self.controller.model
        if model.data.get('rev_leds') is None:
            raise Rejected(409, "rev_leds_shift is not available on this wheel")
        if mode not in SHIFT_LIMITS:
            raise Rejected(409, "The shift point is automatic")
        return _int(value, 'rev_leds_shift', *SHIFT_LIMITS[mode])

    def _check(self, key, value):
        """The value, validated like the GTK control it stands for."""
        model = self.controller.model
        data = model.data
        if key in ('range', 'sensitivity', 'autocenter', 'ff_gain', 'rev_leds_shift') or key in LEVELS:
            self._available(model, key)
            if key == 'range':
                value = _int(value, key, RANGE_MIN, max(RANGE_MIN, int(self.caps['max_range'])), 10)
            elif key == 'ff_gain':
                value = _int(value, key, 0, int(self.caps['max_gain']))
            elif key == 'rev_leds_shift':
                value = _int(value, key, *SHIFT_LIMITS[self._mode(data)])
            else:
                value = _int(value, key, 0, 100)
            return value
        if key == 'combine_pedals':
            self._available(model, key)
            return _int(value, key, 0, 2)
        if key in ('ffb_enabled', 'autocenter_persistent', 'app_gain', 'inertia_mode', 'ffb_leds', 'rev_leds',
                   'rev_leds_launch') or key.startswith('invert_'):
            if key.startswith('invert_') and key[7:] not in PEDALS:
                raise Rejected(400, "unknown setting {}".format(key))
            self._available(model, key)
            return _bool(value, key)
        if key == 'rev_leds_mode':
            self._available(model, key)
            if value not in REV_MODES:
                raise Rejected(400, "rev_leds_mode must be one of {}".format(', '.join(REV_MODES)))
            return value
        if key.endswith('_response') and key[:-9] in PEDALS:
            self._available(model, key)
            if not isinstance(value, (list, tuple)) or len(value) != 3:
                raise Rejected(400, "{} must be [start, end, sensitivity]".format(key))
            start = _int(value[0], key + " start", 0, model.RESPONSE_START_MAX)
            end = _int(value[1], key + " end", model.RESPONSE_END_MIN, 100)
            sensitivity = _int(value[2], key + " sensitivity", 0, 100)
            if model.valid_response((start, end, sensitivity)) is None:
                raise Rejected(400, "{} is not a valid response".format(key))
            return (start, end, sensitivity)
        raise Rejected(400, "unknown setting {}".format(key))

    def _write(self, model, key, value):
        """The call the GTK control makes."""
        controller = self.controller
        if key.startswith('invert_'):
            name = key[7:]
            bit = self.caps['bits'][name]
            mask = model.get_invert_pedals() or 0
            model.set_invert_pedals(mask | bit if value else mask & ~bit)
        elif key.endswith('_response'):
            model.set_pedal_response(key[:-9], *value)
        elif key == 'rev_leds_mode':
            controller.change_rev_leds_shift_unit(value)
        elif key == 'ffb_leds':
            ui = controller.ui
            if value and model.get_rev_leds():
                ui.set_rev_leds(False, None)           # the meter and the rev lights can't share the LEDs
                model.set_rev_leds(False)
            model.set_ffb_leds(value)
        else:
            getattr(model, 'set_' + key)(value)

    def _sync_ui(self, model, keys):
        """Show the new values in the window, as flush_ui() does for a profile."""
        ui = getattr(self.controller, 'ui', None)
        if ui is None:
            return
        data = model.data
        shown = set()
        for key in keys:
            if key.startswith('invert_'):
                group = 'invert'
            elif key.endswith('_response'):
                group = key
            elif key.startswith('rev_leds'):
                group = 'rev_leds' if key != 'rev_leds_launch' else 'rev_options'
            else:
                group = key
            if group in shown:
                continue
            shown.add(group)
            try:
                if group == 'invert':
                    ui.set_invert_pedals(data['invert_pedals'])
                elif group.endswith('_response'):
                    ui.set_pedal_response(group[:-9], data[group])
                elif group == 'rev_leds':
                    ui.set_rev_leds(data['rev_leds'], None, data['rev_leds_shift'], self._mode(data))
                elif group == 'rev_options':
                    ui.set_rev_leds_options(data['rev_leds_launch'], data['rev_leds_learnt'])
                elif group == 'ffb_enabled':
                    pass                               # the model has already told the window
                else:
                    getattr(ui, 'set_' + group)(data[group])
            except Exception as e:
                logging.debug("web controls: window not updated for %s: %s", group, e)

    def _do(self, action, arg):
        controller = self.controller
        if action not in ACTIONS:
            raise Rejected(400, "unknown action {}".format(action))
        model = controller.model
        if action == 'reset_ffb':
            if model.data.get('ff_gain') is None:
                raise Rejected(409, "This wheel has no force feedback settings")
            controller.reset_ffb_defaults()
        elif action == 'try_effect':
            if arg not in EFFECTS:
                raise Rejected(400, "unknown effect")
            if not model.get_ffb_enabled():
                raise Rejected(409, "Force feedback is off")
            # The device directly: the window's try_effect answers a failure with a dialog on the desktop
            if not controller.device.play_demo(arg, on_error=lambda e: logging.warning("web effect: %s", e)):
                raise Rejected(409, "This device has no force feedback")
        elif action == 'load_profile':
            if not isinstance(arg, str) or arg not in self.profiles():
                raise Rejected(400, "unknown profile")
            if self.current_profile() != arg:
                controller.ui.set_profile(arg)         # the profile box loads it, as choosing it does
        elif action == 'save_profile':
            name = self.current_profile()
            if not name or arg != name:
                raise Rejected(409, "Choose the profile to save first")
            controller.save_profile(name)

    # -- any thread --

    def profiles(self):
        path = getattr(getattr(self.controller, 'app', None), 'profile_path', None)
        if not path:
            return []
        return sorted(os.path.splitext(os.path.basename(f))[0] for f in glob.glob(os.path.join(path, '*.ini')))

    def current_profile(self):
        model = self.controller.model
        profile = getattr(model, 'profile', None) if model is not None else None
        return os.path.splitext(os.path.basename(profile))[0] if profile else None

    def state(self):
        """The page's view of the settings: JSON-ready, from the model's values."""
        c = self.controller
        model = c.model
        caps = self.caps
        out = {'writable': self.enabled, 'device': None, 'settings': {}, 'pedals': {}, 'profile': None}
        if model is None or not caps:
            return out
        data = dict(model.data)
        out['device'] = caps.get('device')
        reference = model.reference_values
        current = self.current_profile()
        out['profile'] = {'current': current, 'list': self.profiles(),
                          'dirty': bool(current and reference is not None
                                        and any(data.get(k) != v for k, v in reference.items()))}
        settings = out['settings']
        ffb_on = data.get('ffb_enabled') is not False

        def number(key, low, high, step=1, enabled=True, unit=None):
            if data.get(key) is not None:
                settings[key] = {'value': int(data[key]), 'min': low, 'max': high, 'step': step, 'enabled': enabled}
                if unit:
                    settings[key]['unit'] = unit

        def flag(key, enabled=True):
            if data.get(key) is not None:
                settings[key] = {'value': bool(data[key]), 'enabled': enabled}

        top = max(RANGE_MIN, int(caps.get('max_range') or RANGE_MIN))
        number('range', RANGE_MIN, top, 10)
        if 'range' in settings:
            settings['range']['presets'] = [d for d in RANGE_PRESETS if d <= top]
        number('sensitivity', 0, 100)
        if data.get('combine_pedals') is not None:
            settings['combine_pedals'] = {'value': int(data['combine_pedals']), 'options': [0, 1, 2], 'enabled': True}
        flag('ffb_enabled')
        number('ff_gain', 0, int(caps.get('max_gain') or 100), enabled=ffb_on)
        number('autocenter', 0, 100)
        flag('autocenter_persistent')
        flag('app_gain', ffb_on)
        flag('inertia_mode', ffb_on)
        for key in LEVELS:
            number(key, 0, 100, enabled=ffb_on)
        flag('ffb_leds')
        flag('rev_leds')
        if data.get('rev_leds') is not None:
            mode = self._mode(data)
            settings['rev_leds_mode'] = {'value': mode, 'options': list(REV_MODES), 'enabled': True}
            unit = data.get('rev_leds_shift_unit') or 'percent'
            low, high = SHIFT_LIMITS[unit]
            number('rev_leds_shift', low, high, 50 if unit == 'rpm' else 1, mode != 'auto', unit)
            settings['rev_leds_launch'] = {'value': model.get_rev_leds_launch(), 'enabled': True}
        combined = bool(data.get('combine_pedals'))
        mask = data.get('invert_pedals')
        for name in PEDALS:
            bit = caps['bits'].get(name)
            entry = {}
            if mask is not None and bit:
                entry['invert'] = bool(mask & bit)
            response = data.get(name + '_response')
            if response is not None and bit and caps.get('pedal_response'):
                entry['response'] = {'value': list(response), 'enabled': not combined,
                                     'start_max': model.RESPONSE_START_MAX, 'end_min': model.RESPONSE_END_MIN,
                                     'presets': [{'id': pid, 'label': label, 'response': list(preset), 'note': note}
                                                 for pid, label, preset, note in model.PEDAL_PRESETS[name]]}
            if entry:
                out['pedals'][name] = entry
        return out
