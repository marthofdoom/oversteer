"""Hotkeys: settings that can be changed from a wheel button or a keyboard
shortcut while a game is running.

Each action names the GUI control it moves, so a hotkey does exactly what
moving that control by hand would do (write the driver, update the save
button, grey out with the control). Wheel buttons are saved in the
profile; keyboard keys belong to the desktop (see global_shortcuts).
"""

from locale import gettext as _

from evdev import ecodes


class Action:

    def __init__(self, id, group, label, widget, delta=None, kind='step'):
        self.id = id
        self.group = group
        self.label = label
        self.widget = widget       # GtkUi attribute of the control it moves
        self.delta = delta         # step actions: change per press, in the control's units
        self.kind = kind           # 'step', 'toggle', 'range', 'shift', 'profile'

    def description(self):
        """Shown in the desktop's shortcut settings."""
        return '{}: {}'.format(self.group, self.label)


def _actions():
    rev = _("Rev LEDs")
    ffb = _("Force feedback")
    wheel = _("Wheel")
    profiles = _("Profiles")
    return [
        Action('rev_shift_up', rev, _("Shift point up"), 'rev_leds_shift', +1, 'shift'),
        Action('rev_shift_down', rev, _("Shift point down"), 'rev_leds_shift', -1, 'shift'),
        Action('rev_leds_toggle', rev, _("Rev LEDs on/off"), 'rev_leds', kind='toggle'),
        Action('ffb_toggle', ffb, _("Force feedback on/off"), 'ffb_enabled', kind='toggle'),
        Action('ff_gain_up', ffb, _("Overall strength up"), 'ff_gain', +5),
        Action('ff_gain_down', ffb, _("Overall strength down"), 'ff_gain', -5),
        Action('autocenter_up', ffb, _("Centering spring up"), 'autocenter', +5),
        Action('autocenter_down', ffb, _("Centering spring down"), 'autocenter', -5),
        Action('spring_up', ffb, _("Spring up"), 'ff_spring_level', +5),
        Action('spring_down', ffb, _("Spring down"), 'ff_spring_level', -5),
        Action('damper_up', ffb, _("Damper up"), 'ff_damper_level', +5),
        Action('damper_down', ffb, _("Damper down"), 'ff_damper_level', -5),
        Action('friction_up', ffb, _("Friction up"), 'ff_friction_level', +5),
        Action('friction_down', ffb, _("Friction down"), 'ff_friction_level', -5),
        Action('rumble_up', ffb, _("Rumble up"), 'ff_rumble_level', +5),
        Action('rumble_down', ffb, _("Rumble down"), 'ff_rumble_level', -5),
        Action('app_gain_toggle', ffb, _("Let games adjust the gain on/off"), 'app_gain', kind='toggle'),
        Action('inertia_toggle', ffb, _("Inertia mode on/off"), 'inertia_mode', kind='toggle'),
        Action('autocenter_persistent_toggle', ffb, _("Keep centering spring on/off"), 'autocenter_persistent', kind='toggle'),
        Action('range_up', wheel, _("Rotation range +10°"), 'wheel_range', +10, 'range'),
        Action('range_down', wheel, _("Rotation range −10°"), 'wheel_range', -10, 'range'),
        Action('range_up_90', wheel, _("Rotation range +90°"), 'wheel_range', +90, 'range'),
        Action('range_down_90', wheel, _("Rotation range −90°"), 'wheel_range', -90, 'range'),
        Action('sensitivity_up', wheel, _("Sensitivity up"), 'wheel_sensitivity', +5),
        Action('sensitivity_down', wheel, _("Sensitivity down"), 'wheel_sensitivity', -5),
        Action('profile_next', profiles, _("Next profile"), 'profile_combobox', +1, 'profile'),
        Action('profile_prev', profiles, _("Previous profile"), 'profile_combobox', -1, 'profile'),
    ]


ACTIONS = _actions()
BY_ID = {a.id: a for a in ACTIONS}

SHIFT_STEP = {'percent': 1, 'rpm': 100}      # one press of the shift point hotkey

HAT_NAMES = {
    (ecodes.ABS_HAT0X, -1): _("D-pad left"),
    (ecodes.ABS_HAT0X, 1): _("D-pad right"),
    (ecodes.ABS_HAT0Y, -1): _("D-pad up"),
    (ecodes.ABS_HAT0Y, 1): _("D-pad down"),
}


def key_input(code):
    return 'btn:{}'.format(code)


def hat_input(code, direction):
    return 'hat:{}:{}'.format(code, direction)


def button_number(code):
    """The number the Controls tab shows for a button code, or None."""
    if 288 <= code <= 303:
        return code - 288
    if 304 <= code <= 316:
        return code - 304
    if 704 <= code <= 767:
        return code - 688
    return None


def input_name(wheel_input):
    """A wheel input as the user knows it."""
    try:
        kind, rest = wheel_input.split(':', 1)
        if kind == 'btn':
            code = int(rest)
            number = button_number(code)
            if number is not None:
                return _("Button {}").format(number)
            name = ecodes.BTN.get(code) or ecodes.KEY.get(code)
            if isinstance(name, list):
                name = name[0]
            return name or _("Button code {}").format(code)
        if kind == 'hat':
            code, direction = map(int, rest.split(':'))
            return HAT_NAMES.get((code, direction), wheel_input)
    except (ValueError, TypeError):
        pass
    return wheel_input


def _valid_input(wheel_input):
    try:
        kind, rest = wheel_input.split(':', 1)
        if kind == 'btn':
            return 0 < int(rest) < ecodes.KEY_MAX
        if kind == 'hat':
            code, direction = map(int, rest.split(':'))
            return (code, direction) in HAT_NAMES
    except (ValueError, TypeError):
        pass
    return False


def parse(text):
    """The profile's 'hotkeys' value -> {action id: wheel input}. Unknown
    actions and malformed inputs are dropped, not fatal: a profile from a
    newer version still loads."""
    bindings = {}
    for item in (text or '').split(','):
        action, sep, wheel_input = item.strip().partition('=')
        if sep and action in BY_ID and _valid_input(wheel_input):
            bindings[action] = wheel_input
    return bindings


def serialize(bindings):
    """{action id: wheel input} -> the profile value, in catalog order so
    an unchanged set compares equal."""
    return ','.join('{}={}'.format(a.id, bindings[a.id]) for a in ACTIONS if a.id in bindings)
