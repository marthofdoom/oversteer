"""Enumerate racing equipment and build a combined-device proxy spec from it.

Enumeration goes through udev/sysfs only (no device node is opened), so it
works for devices the hide rules have made root-only.
"""

import ctypes
import os
import pyudev
import re
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

# Games identify a wheel by its exact control layout, not just VID/PID
# (Forza Horizon 6 stops recognising a G29 with one axis more), so in strict
# mode the combined wheel keeps the wheel's own axes and buttons: shifter
# gears go on the wheel's gear buttons, and axis devices become separate
# virtual devices with an identity games already know.
G29_PS_BUTTON = 0x2c8            # button 25, the least useful one to lose to an extra gear

# A generic identity nothing recognises. Forza Horizon keeps force feedback
# in a custom wheel profile only for devices it has no built-in profile
# for (this is how EmuWheel/vJoy works on Windows); a recognised wheel
# switched to a custom profile loses FFB. 0x1209 is the pid.codes vendor id.
GENERIC_IDENTITY = {'name': 'Oversteer Combined Wheel', 'vendor': '1209', 'product': '0ec5',
                    'version': '0100', 'bustype': 'usb'}
HANDBRAKE_IDENTITY = {'name': 'Fanatec ClubSport Handbrake', 'vendor': '0eb7', 'product': '00e5',
                      'version': '0001', 'bustype': 'usb', 'phys': 'usb-fanatec-virt/input0'}

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
        if self.usb_id in _KNOWN_WHEELS:
            return KIND_WHEEL
        if self.keyboard:
            return KIND_OTHER            # a keyboard's media/system keys, not a controller
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
    bits_per_word = ctypes.sizeof(ctypes.c_ulong) * 8     # the kernel's BITS_PER_LONG, as udev's input_id assumes
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


def build_combined_specs(wheel, others, spec_id='combined-wheel'):
    """Strict layout: a caps-identical virtual copy of `wheel` carrying the
    button-only devices, plus one virtual device per axis device (a
    handbrake becomes a Fanatec ClubSport Handbrake). Returns the specs in
    the order they must be created so the wheel enumerates first."""
    folded = [d for d in others if d.kind in (KIND_SHIFTER, KIND_BUTTONBOX)]
    separate = [d for d in others if d.kind not in (KIND_SHIFTER, KIND_BUTTONBOX)]
    specs = [build_combined_spec(wheel, folded, spec_id=spec_id, strict=True)]
    for index, dev in enumerate(separate):
        if dev.kind == KIND_HANDBRAKE and dev.axes:
            specs.append(ProxySpec.from_dict({
                'id': '{}-handbrake{}'.format(spec_id, index + 1),
                'name': '{} as {}'.format(dev.name, HANDBRAKE_IDENTITY['name']),
                'description': 'Generated by Oversteer: handbrake presented as a device games recognise.',
                'enabled': True,
                'identity': dict(HANDBRAKE_IDENTITY),
                'sources': {'hb': {'match': {'vendor': '{:04x}'.format(dev.vendor), 'product': '{:04x}'.format(dev.product)},
                                   'grab': True, 'hide': True}},
                'capabilities': {'keys': ['BTN_TRIGGER'], 'abs': {'ABS_Z': {'min': 0, 'max': 65535, 'fuzz': 16, 'flat': 4096}}},
                'mappings': [{'source': 'hb', 'from': code_name(ecodes.EV_ABS, dev.axes[0]), 'to': 'ABS_Z', 'invert': 'auto'}],
            }))
        # pedals / other axis devices: passed through untouched for now
    return specs


def build_combined_spec(wheel, others, spec_id='combined-wheel', name=None, strict=False, identity='wheel'):
    """A ProxySpec presenting `wheel` (passthrough + force feedback) with the
    other devices' controls folded in. With strict=True nothing is added to
    the wheel's own layout: overflow goes to the wheel's spare buttons
    (one gear onto the PS button) or is dropped."""
    if wheel.kind != KIND_WHEEL:
        raise ValueError("the combined device must be built around a wheel")
    # identity 'wheel' (default): the combined device keeps the wheel's real
    # identity so games apply their built-in profile. 'generic': present as
    # "Oversteer Combined Wheel" — needed for Forza Horizon 6 under Proton,
    # where a recognised wheel switched to a custom profile loses force
    # feedback but a generic device keeps it.
    def match(dev):
        # vendor/product plus the exact name: a base with several interfaces
        # (keyboard + joystick) must resolve to the controller node
        return {'vendor': '{:04x}'.format(dev.vendor), 'product': '{:04x}'.format(dev.product),
                'name': '^' + re.escape(dev.name) + '$'}

    notes = []
    sources = {'wheel': {'match': match(wheel), 'grab': True, 'hide': True, 'required': True}}
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

    strict_spares = [G29_PS_BUTTON] if strict and G29_PS_BUTTON in used_keys else []

    def next_free_key():
        nonlocal next_key
        if strict:
            return strict_spares.pop(0) if strict_spares else None
        while next_key in used_keys or next_key in extra_keys:
            next_key += 1
        if next_key > ecodes.KEY_MAX:
            return None
        code = next_key
        next_key += 1
        return code

    for index, dev in enumerate(others):
        key = '{}{}'.format(dev.kind, index + 1)
        sources[key] = {'match': match(dev), 'grab': True, 'hide': True, 'required': False}
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
                    if target is None:
                        notes.append("{}: no button left for gear {}".format(dev.name, i + 1))
                        continue
                    if target not in used_keys:
                        extra_keys.append(target)
                mappings.append({'source': key, 'from': code, 'to': target})
            if reverse is not None:
                mappings.append({'source': key, 'from': reverse, 'to': reverse_code})
        else:
            for code in buttons:
                target = next_free_key()
                if target is None:
                    notes.append("{}: no button left for {}".format(dev.name, code_name(ecodes.EV_KEY, code)))
                    continue
                if target not in used_keys:
                    extra_keys.append(target)
                mappings.append({'source': key, 'from': code, 'to': target})
        for code in ([] if strict or dev.kind in (KIND_SHIFTER, KIND_BUTTONBOX) else dev.axes):
            if not spare_axes:
                notes.append("{}: no axis left for {}".format(dev.name, code_name(ecodes.EV_ABS, code)))
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
        'description': "Generated by Oversteer from the plugged-in equipment. Edit and save to keep changes."
                       + ("".join("\nNote: " + n for n in notes)),
        'enabled': True,
        'identity': dict(GENERIC_IDENTITY) if identity == 'generic' else
                    {'name': wheel.name, 'vendor': '{:04x}'.format(wheel.vendor), 'product': '{:04x}'.format(wheel.product),
                     'version': '{:04x}'.format(wheel.version or 0), 'bustype': 'usb'},
        'passthrough': 'wheel',
        'ff': {'source': 'wheel'} if wheel.ff else None,
        'sources': sources,
        'capabilities': {'keys': [code_name(ecodes.EV_KEY, k) for k in extra_keys],
                         'abs': {code_name(ecodes.EV_ABS, a): info for a, info in extra_abs.items()}},
        'mappings': [dict(m, **_from_fields(m['from'], ecodes.EV_KEY if isinstance(m['from'], int) and m['from'] >= ecodes.BTN_MISC else ecodes.EV_ABS))
                     for m in mappings],
    }
    if data['ff'] is None:
        del data['ff']
    data['mappings'] = [dict(m, to=(m['to'] if isinstance(m['to'], str) else str(m['to']))) for m in data['mappings']]
    return ProxySpec.from_dict(data)


def _from_fields(code, etype):
    """'from' (and 'from_type' for codes without a name) for a mapping."""
    if isinstance(code, str):
        return {'from': code}
    name = code_name(etype, code)
    if name.isdigit():
        return {'from': str(code), 'from_type': 'ABS' if etype == ecodes.EV_ABS else 'KEY'}
    return {'from': name}
