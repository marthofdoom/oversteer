"""Enumerate racing equipment and build a combined-device proxy spec from it.

Enumeration goes through udev/sysfs only (no device node is opened), so it
works for devices the hide rules have made root-only.
"""

import os
import pyudev
from evdev import ecodes

from .spec import ProxySpec, code_name
from .. import wheel_ids as wid

KIND_WHEEL = 'wheel'
KIND_SHIFTER = 'shifter'
KIND_PEDALS = 'pedals'
KIND_HANDBRAKE = 'handbrake'
KIND_BUTTONBOX = 'buttonbox'
KIND_GAMEPAD = 'gamepad'
KIND_VIRTUAL = 'virtual'
KIND_OTHER = 'other'

# Kinds that get combined by default
COMBINE_DEFAULT = (KIND_WHEEL, KIND_SHIFTER, KIND_PEDALS, KIND_HANDBRAKE, KIND_BUTTONBOX)

# Where a G29 reports its Driving Force Shifter: gears 1-6, reverse
G29_GEAR_CODES = [300, 301, 302, 303, 704, 705]
G29_REVERSE_CODE = 706

# Shifters whose button count exceeds their gear positions: (gears, index of reverse)
KNOWN_SHIFTERS = {
    '044f:b660': (7, 7),    # Thrustmaster T500 RS Gear Shift (7 + R, 10 buttons reported)
    '044f:b65c': (7, 7),    # Thrustmaster TH8A (7 + R)
    '046d:c26f': (6, 6),    # Logitech Driving Force Shifter (standalone, G923/G PRO)
}

# Axes a combined wheel hands out to extra devices, in order. Only codes
# above ABS_RZ: SDL/Proton number DirectInput axes by evdev code order, so
# an extra axis must sort after the wheel's own X/Y/Z/RZ or it shifts the
# game's pedal bindings (ABS_RX/RY would land between Z and RZ).
SPARE_AXES = [ecodes.ABS_THROTTLE, ecodes.ABS_RUDDER, ecodes.ABS_WHEEL, ecodes.ABS_GAS,
              ecodes.ABS_BRAKE, ecodes.ABS_HAT3X, ecodes.ABS_HAT3Y, ecodes.ABS_PRESSURE,
              ecodes.ABS_DISTANCE, ecodes.ABS_TILT_X, ecodes.ABS_TILT_Y, ecodes.ABS_MISC]

_KNOWN_WHEELS = {v for k, v in vars(wid).items() if k.isupper() and isinstance(v, str)}


class Equipment:

    def __init__(self, udevice):
        self.sys_path = udevice.sys_path
        self.node = udevice.device_node
        parent = udevice.parent            # the inputN device
        self.name = _attr(parent, 'name') or udevice.get('NAME', '').strip('"')
        self.vendor = _hex_attr(parent, 'id/vendor')
        self.product = _hex_attr(parent, 'id/product')
        self.version = _hex_attr(parent, 'id/version')
        self.bustype = _hex_attr(parent, 'id/bustype')
        self.phys = _attr(parent, 'phys') or ''
        self.usb = udevice.find_parent('usb', 'usb_device') is not None
        self.keys = _bits(parent, 'capabilities/key')
        self.abs = _bits(parent, 'capabilities/abs')
        self.ff = bool(_bits(parent, 'capabilities/ff'))
        self.readable = bool(self.node) and os.access(self.node, os.R_OK)
        self.rest_high = self._axes_resting_high()
        self.keyboard = udevice.get('ID_INPUT_KEYBOARD') == '1' or udevice.get('ID_INPUT_KEY') == '1'
        self.kind = self._classify()

    def _axes_resting_high(self):
        """Axes whose current (resting) value is in the upper half, so a
        game sees them as 'pulled' unless inverted. Needs a readable node;
        hidden devices are read by the daemon at attach time instead."""
        high = set()
        if not self.readable:
            return high
        try:
            from evdev import InputDevice
            dev = InputDevice(self.node)
            for code, info in dev.capabilities(absinfo=True).get(ecodes.EV_ABS, []):
                if info.max > info.min and info.value > (info.min + info.max) // 2:
                    high.add(code)
            dev.close()
        except OSError:
            pass
        return high

    @property
    def usb_id(self):
        return '{:04x}:{:04x}'.format(self.vendor or 0, self.product or 0)

    @property
    def buttons(self):
        return [c for c in self.keys if c >= ecodes.BTN_MISC]

    @property
    def axes(self):
        """Real control axes: X..RZ, throttle, rudder, wheel, gas, brake.
        ABS_MISC and friends (keyboard/mouse consumer-control interfaces)
        don't count."""
        return [c for c in self.abs if c < ecodes.ABS_HAT0X]

    @property
    def hats(self):
        return [c for c in self.abs if ecodes.ABS_HAT0X <= c <= ecodes.ABS_HAT3Y]

    def _classify(self):
        if not self.usb and self.bustype != ecodes.BUS_BLUETOOTH:
            return KIND_VIRTUAL
        if self.keyboard:
            return KIND_OTHER            # a keyboard's media/system keys, not a controller
        if self.usb_id in _KNOWN_WHEELS:
            return KIND_WHEEL
        pad_buttons = {ecodes.BTN_SOUTH, ecodes.BTN_EAST, ecodes.BTN_NORTH, ecodes.BTN_WEST}
        if pad_buttons & set(self.keys) or (ecodes.ABS_RX in self.abs and ecodes.ABS_RY in self.abs and len(self.buttons) >= 8):
            return KIND_GAMEPAD
        if self.ff and ecodes.ABS_X in self.abs:
            return KIND_WHEEL
        axes, buttons = self.axes, self.buttons
        if len(axes) == 1 and len(buttons) <= 1:
            return KIND_HANDBRAKE
        if 2 <= len(axes) <= 3 and not buttons:
            return KIND_PEDALS
        if len(buttons) >= 6 and (not axes or len(axes) <= 2):
            return KIND_SHIFTER
        if buttons and not axes:
            return KIND_BUTTONBOX
        return KIND_OTHER

    def describe(self):
        return "{:<10} {} {:<42} axes {:<2} buttons {:<2} {}{}".format(
            self.kind, self.usb_id, self.name[:42], len(self.axes), len(self.buttons),
            'FF ' if self.ff else '', '' if self.readable else '(hidden)')


def _attr(device, name):
    if device is None:
        return None
    try:
        return device.attributes.asstring(name)
    except (KeyError, OSError, UnicodeDecodeError):
        return None


def _hex_attr(device, name):
    value = _attr(device, name)
    if value is None:
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _bits(device, name):
    """Decode a sysfs capabilities bitmap ("7 0 0 ... ff") into codes."""
    value = _attr(device, name)
    if value is None:
        return []
    words = value.split()
    codes = []
    bits_per_word = 64 if os.uname().machine.endswith('64') else 32
    for i, word in enumerate(reversed(words)):
        value = int(word, 16)
        base = i * bits_per_word
        bit = 0
        while value:
            if value & 1:
                codes.append(base + bit)
            value >>= 1
            bit += 1
    return sorted(codes)


_NOT_EQUIPMENT = ('ID_INPUT_MOUSE', 'ID_INPUT_POINTINGSTICK', 'ID_INPUT_TOUCHPAD', 'ID_INPUT_TOUCHSCREEN',
                  'ID_INPUT_TABLET', 'ID_INPUT_TABLET_PAD', 'ID_INPUT_ACCELEROMETER', 'ID_INPUT_SWITCH')


def list_equipment(include_virtual=False):
    """Every input device that looks like a controller, one per event node.

    udev's ID_INPUT_JOYSTICK is not required: a single-axis handbrake with no
    buttons gets no class at all from udev (which is also why games ignore
    it), so anything with absolute axes or joystick buttons is considered."""
    context = pyudev.Context()
    found = []
    for udevice in context.list_devices(subsystem='input'):
        node = udevice.device_node
        if not node or 'event' not in os.path.basename(node):
            continue
        if any(udevice.get(prop) == '1' for prop in _NOT_EQUIPMENT):
            continue
        eq = Equipment(udevice)
        if not eq.axes and not eq.buttons:
            continue
        if eq.keyboard and not eq.axes:
            continue                     # keyboards' media/system-control interfaces
        if eq.kind == KIND_VIRTUAL and not include_virtual:
            continue
        found.append(eq)
    found.sort(key=lambda e: (e.kind != KIND_WHEEL, e.kind, e.name))
    return found


def build_combined_spec(wheel, others, spec_id='combined-wheel', name=None):
    """A ProxySpec presenting `wheel` (passthrough + force feedback) with the
    other devices' controls folded in as extra buttons and axes."""
    if wheel.kind != KIND_WHEEL:
        raise ValueError("the combined device must be built around a wheel")
    sources = {'wheel': {'match': {'vendor': '{:04x}'.format(wheel.vendor), 'product': '{:04x}'.format(wheel.product)},
                         'grab': True, 'hide': True, 'required': True}}
    mappings = []
    extra_keys = []
    extra_abs = {}
    used_keys = set(wheel.keys)
    used_abs = set(wheel.abs)
    spare_axes = [a for a in SPARE_AXES if a not in used_abs]
    next_key = max([ecodes.BTN_TRIGGER_HAPPY] + [k for k in wheel.keys if k >= ecodes.BTN_TRIGGER_HAPPY]) + 1
    gear_codes = list(G29_GEAR_CODES) if wheel.usb_id in (wid.LG_G29, wid.LG_G27, wid.LG_G923P, wid.LG_G923X, wid.LG_G920, wid.LG_G25) else []
    reverse_code = G29_REVERSE_CODE if gear_codes else None
    parts = []

    def next_free_key():
        nonlocal next_key
        while next_key in used_keys or next_key in extra_keys:
            next_key += 1
        code = next_key
        next_key += 1
        return code

    for index, dev in enumerate(others):
        key = '{}{}'.format(dev.kind, index + 1)
        sources[key] = {'match': {'vendor': '{:04x}'.format(dev.vendor), 'product': '{:04x}'.format(dev.product)},
                        'grab': True, 'hide': True, 'required': False}
        parts.append(dev.name)
        buttons = dev.buttons
        if dev.kind == KIND_SHIFTER and gear_codes:
            # Button order is gear order on the shifters we know. Known
            # shifters say how many positions they have and which button is
            # reverse; otherwise 7 buttons means 6 + R and 8 means 7 + R.
            n_gears, r_index = KNOWN_SHIFTERS.get(dev.usb_id, (None, None))
            if n_gears is None:
                if len(buttons) in (7, 8):
                    n_gears, r_index = len(buttons) - 1, len(buttons) - 1
                else:
                    n_gears, r_index = len(buttons), None
            gears = buttons[:n_gears]
            reverse = buttons[r_index] if r_index is not None and r_index < len(buttons) else None
            for i, code in enumerate(gears):
                if i < len(gear_codes):
                    target = gear_codes[i]
                else:
                    target = next_free_key()
                    extra_keys.append(target)
                mappings.append({'source': key, 'from': code, 'to': target})
            if reverse is not None:
                mappings.append({'source': key, 'from': reverse, 'to': reverse_code})
        else:
            for code in buttons:
                target = next_free_key()
                extra_keys.append(target)
                mappings.append({'source': key, 'from': code, 'to': target})
        for code in ([] if dev.kind in (KIND_SHIFTER, KIND_BUTTONBOX) else dev.axes):
            if not spare_axes:
                break
            target = spare_axes.pop(0)
            extra_abs[target] = {'min': 0, 'max': 65535, 'fuzz': 16, 'flat': 4096}
            # Rest at the low end: games treat an axis parked at max as engaged.
            # The daemon decides at attach time (the node may be hidden from us).
            mappings.append({'source': key, 'from': code, 'to': code_name(ecodes.EV_ABS, target),
                             'invert': 'auto'})

    data = {
        'id': spec_id,
        'name': name or "{} + {}".format(wheel.name, ", ".join(parts)) if parts else wheel.name,
        'description': "Generated by Oversteer from the plugged-in equipment. Edit and save to keep changes.",
        'enabled': True,
        'identity': {'name': wheel.name, 'vendor': '{:04x}'.format(wheel.vendor), 'product': '{:04x}'.format(wheel.product),
                     'version': '{:04x}'.format(wheel.version or 0), 'bustype': 'usb'},
        'passthrough': 'wheel',
        'ff': {'source': 'wheel'} if wheel.ff else None,
        'sources': sources,
        'capabilities': {'keys': [code_name(ecodes.EV_KEY, k) for k in extra_keys],
                         'abs': {code_name(ecodes.EV_ABS, a): info for a, info in extra_abs.items()}},
        'mappings': [dict(m, **{'from': _as_name(m['from'], ecodes.EV_KEY if isinstance(m['from'], int) and m['from'] >= ecodes.BTN_MISC else ecodes.EV_ABS)})
                     for m in mappings],
    }
    if data['ff'] is None:
        del data['ff']
    data['mappings'] = [dict(m, to=(m['to'] if isinstance(m['to'], str) else str(m['to']))) for m in data['mappings']]
    return ProxySpec.from_dict(data)


def _as_name(code, etype):
    if isinstance(code, str):
        return code
    name = code_name(etype, code)
    return name if not name.isdigit() else str(code)
