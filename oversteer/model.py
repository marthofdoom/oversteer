import configparser
import logging

from . import ffb_carprops
from . import wheel_torque

STRENGTH_MIN = 0                     # Countersteer's overall steering strength, %
STRENGTH_MAX = 130
MAPPINGS = ('knee', 'classic')       # steering mapping (profile INI only): 1:1 up to a knee and compressed above (default), or the uniform impedance scale
GEAR_FLIP_RANGE = (3, 30)            # "Gear flip point", % of full scale (ffb_gear.GEAR_FLIP_RANGE)
# Keys of older profiles, dropped without a word at the next save. countersteer_column_damping meant a total with the wheel's
# own damping included (0 was off, 5-15 took damping out); countersteer_column_added, on top of the wheel's own, replaces it
# with no conversion: a profile with only the old key gets the new default.
RETIRED_KEYS = ('countersteer_column_damping',)
RBR_CUT_MIN, RBR_CUT_MAX = 15, 120   # "RBR force cut": the low-pass corner on RBR's force, Hz; the top is off
KNEE_MIN, KNEE_MAX = 30, 60          # "1:1 up to": the share of the wheel's peak torque played 1:1 in Nm, %


def N_(text):
    """Marks a string for translation; it is translated where shown."""
    return text


def pick_plug_profile(flagged, recent):
    """Which profile a newly plugged wheel gets: of the profile names that have
    "apply when the wheel is plugged in" set, the most recently selected one
    (`recent` lists names, most recent first); one not in `recent` ranks after
    all that are, alphabetically among themselves. None when none is flagged."""
    rank = {name: i for i, name in enumerate(recent)}
    names = sorted(flagged, key=lambda name: (rank.get(name, len(rank)), name))
    return names[0] if names else None


def read_apply_on_plug(profile_file):
    """Whether the profile file has the plug flag set (False if unreadable)."""
    config = configparser.ConfigParser()
    try:
        config.read(profile_file)
        return bool(int(config['DEFAULT'].get('apply_on_plug', '0')))
    except (configparser.Error, ValueError, OSError):
        return False


class Model:

    profile = None
    device = None

    defaults = {
        'mode': None,
        'range': None,
        'sensitivity': None,
        'ff_gain': None,
        'autocenter': None,
        'autocenter_persistent': None,
        'app_gain': None,
        'inertia_mode': None,
        'combine_pedals': None,
        'invert_pedals': None,
        'clutch_response': None,
        'accelerator_response': None,
        'brakes_response': None,
        'ffb_enabled': None,
        'spring_level': None,
        'damper_level': None,
        'friction_level': None,
        'rumble_level': None,
        'ffb_leds': None,
        'rev_leds': None,
        'rev_leds_port': None,
        'rev_leds_port_auto': None,
        'rev_leds_shift': None,
        'rev_leds_shift_unit': None,
        'rev_leds_launch': None,
        'rev_leds_learnt': None,
        'ffb_overlay': None,
        'range_overlay': None,
        'use_buttons': None,
        'hotkeys': None,
        'center_wheel': None,
        'start_app_manually': None,
        'profile_note': None,
        'apply_on_plug': None,
        'countersteer_ffb': None,
        'countersteer_strength': None,
        'countersteer_knee': None,
        'countersteer_rbr_cut': None,
        'countersteer_caster_floor': None,
        'countersteer_engine_feel': None,
        'countersteer_mapping': None,
        'countersteer_forza_model': None,
        'countersteer_rbr_spring': None,
        'countersteer_rbr_source': None,
        'countersteer_drive_pull': None,
        'countersteer_drive_pull_level': None,
        'countersteer_kick': None,
        'countersteer_kick_level': None,
        'countersteer_gear_guard': None,
        'countersteer_gear_guard_level': None,
        'countersteer_gear_flip': None,
        'countersteer_wheel_drive': None,
        'countersteer_column_added': None,
        'countersteer_rim_mm': None,
        'countersteer_car_wheel_mm': None,
        'countersteer_power_class': None,
        'rbr_auto_range': None,
        'countersteer_texture': None,
        'countersteer_texture_road': None,
        'countersteer_texture_impact': None,
        'countersteer_texture_lock': None,
        'countersteer_texture_engine': None,
        'countersteer_linearity': None,
        'countersteer_lightness': None,
        'countersteer_column': None,
        'countersteer_align': None,
        'countersteer_friction_removal': None,
    }

    types = {
        'mode': 'string',
        'range': 'integer',
        'sensitivity': 'integer',
        'ff_gain': 'integer',
        'autocenter': 'integer',
        'autocenter_persistent': 'boolean',
        'app_gain': 'boolean',
        'inertia_mode': 'boolean',
        'combine_pedals': 'integer',
        'invert_pedals': 'integer',
        'clutch_response': 'tuple',
        'accelerator_response': 'tuple',
        'brakes_response': 'tuple',
        'ffb_enabled': 'boolean',
        'spring_level': 'integer',
        'damper_level': 'integer',
        'friction_level': 'integer',
        'rumble_level': 'integer',
        'ffb_leds': 'integer',
        'rev_leds': 'boolean',
        'rev_leds_port': 'integer',
        'rev_leds_port_auto': 'boolean',
        'rev_leds_shift': 'integer',
        'rev_leds_shift_unit': 'string',
        'rev_leds_launch': 'boolean',
        'rev_leds_learnt': 'boolean',
        'ffb_overlay': 'boolean',
        'range_overlay': 'string',
        'use_buttons': 'boolean',
        'hotkeys': 'string',
        'center_wheel': 'boolean',
        'start_app_manually': 'boolean',
        'profile_note': 'string',
        'apply_on_plug': 'boolean',
        'countersteer_ffb': 'boolean',
        'countersteer_strength': 'integer',
        'countersteer_knee': 'integer',
        'countersteer_rbr_cut': 'integer',
        'countersteer_caster_floor': 'integer',
        'countersteer_engine_feel': 'integer',
        'countersteer_mapping': 'string',
        'countersteer_forza_model': 'boolean',
        'countersteer_rbr_spring': 'boolean',
        'countersteer_rbr_source': 'string',
        'countersteer_drive_pull': 'boolean',
        'countersteer_drive_pull_level': 'integer',
        'countersteer_kick': 'boolean',
        'countersteer_kick_level': 'integer',
        'countersteer_gear_guard': 'boolean',
        'countersteer_gear_guard_level': 'integer',
        'countersteer_gear_flip': 'integer',
        'countersteer_wheel_drive': 'string',
        'countersteer_column_added': 'integer',
        'countersteer_rim_mm': 'integer',
        'countersteer_car_wheel_mm': 'integer',
        'countersteer_power_class': 'string',
        'rbr_auto_range': 'boolean',
        'countersteer_texture': 'integer',
        'countersteer_texture_road': 'boolean',
        'countersteer_texture_impact': 'boolean',
        'countersteer_texture_lock': 'boolean',
        'countersteer_texture_engine': 'boolean',
        'countersteer_linearity': 'integer',
        'countersteer_lightness': 'integer',
        'countersteer_column': 'boolean',
        'countersteer_align': 'boolean',
        'countersteer_friction_removal': 'boolean',
    }

    # pedal response: (start %, end %, sensitivity), in pedal-travel terms
    # from the released end, whichever way invert_pedals has the axis. The
    # popover offers start up to 45 and end from 55; the model keeps to the
    # same ranges so what it holds is always what the UI can show.
    DEFAULT_RESPONSE = (0, 100, 50)
    RESPONSE_START_MAX = 45
    RESPONSE_END_MIN = 55
    PEDAL_RESPONSES = ('clutch', 'accelerator', 'brakes')
    # Presets the Response popover offers: (id, label, response, note)
    LINEAR_PRESET = ('linear', N_('Linear'), (0, 100, 50), None)
    PEDAL_PRESETS = {
        'clutch': (LINEAR_PRESET,),
        'accelerator': (LINEAR_PRESET,),
        'brakes': (LINEAR_PRESET,
                   ('spring_brake', N_('Spring brake (recommended)'), (3, 85, 40),
                    N_("Stock spring-and-rubber brakes: full braking where the rubber starts to bite, "
                       "a softer start for trail braking. Set the game's own brake curve to linear."))),
    }

    def __init__(self, device = None, ui = None):
        self.ui = ui
        self.reference_values = None
        self.data = self.defaults.copy()
        self.pedal_response_written = set()   # pedals whose response the driver has
        self.pedal_axis_warned = set()        # pedals already warned about as missing
        if device != None:
            self.set_device(device)

    def get_device(self):
        return self.device

    def set_device(self, device):
        self.device = device
        pending_data = self.data.copy()
        self.update_from_device_settings()
        for k, v in pending_data.items():
            if v != None:
                self.data[k] = v

    def set_ui(self, ui):
        self.ui = ui

    def update_save_profile_button(self):
        self.ui.disable_save_profile()
        if self.ui is None or self.reference_values is None:
            return
        for (key, value) in self.reference_values.items():
            if self.data[key] != value:
                self.ui.enable_save_profile()

    def read_device_settings(self):
        return {
            'mode': self.device.get_mode(),
            'range': self.device.get_range(),
            'sensitivity': self.device.get_sensitivity(),
            'ff_gain': self.device.get_ff_gain(),
            'autocenter': self.device.get_autocenter(),
            'autocenter_persistent': self.device.get_autocenter_persistent(),
            'app_gain': self.device.get_app_gain(),
            'inertia_mode': self.device.get_inertia_mode(),
            'combine_pedals': self.device.get_combine_pedals(),
            'invert_pedals': self.device.get_invert_pedals(),
            'clutch_response': self.read_pedal_response('clutch'),
            'accelerator_response': self.read_pedal_response('accelerator'),
            'brakes_response': self.read_pedal_response('brakes'),
            'ffb_enabled': True if self.device.get_ff_gain() is not None else None,
            'spring_level': self.device.get_spring_level(),
            'damper_level': self.device.get_damper_level(),
            'friction_level': self.device.get_friction_level(),
            'rumble_level': self.device.get_rumble_level(),
            'ffb_leds': self.device.get_ffb_leds(),
            'rev_leds': False if self.device.has_rev_leds() else None,
            'rev_leds_port': None,                     # no fixed port: Auto (rev_leds_port_auto)
            'rev_leds_port_auto': True if self.device.has_rev_leds() else None,
            'rev_leds_shift': 95 if self.device.has_rev_leds() else None,
            'rev_leds_shift_unit': 'percent' if self.device.has_rev_leds() else None,
            'rev_leds_launch': True if self.device.has_rev_leds() else None,
            'rev_leds_learnt': True if self.device.has_rev_leds() else None,
            'ffb_overlay': False if self.device.get_peak_ffb_level() is not None else None,
            'range_overlay': 'never' if self.device.get_peak_ffb_level() is not None else None,
            'use_buttons': False if self.device.get_range() is not None else None,
            'hotkeys': self.data.get('hotkeys') or '',
            'center_wheel': False,
            'start_app_manually': False,
            'profile_note': '',
            'apply_on_plug': False,
            # Countersteer FFB, a feel preference kept per profile; None where the driver has no replace mode
            'countersteer_ffb': False if self.device.get_ext_force_support() else None,
            # Its texture (ffb_texture): the master level in %, and each part on or off
            **self._texture_defaults(self.device.get_ext_force_support(), getattr(self.device, 'usb_id', None)),
        }

    @staticmethod
    def wheel_drive(usb_id, override=None):
        """The wheel's drive type: the profile's countersteer_wheel_drive when valid, else the table's (wheel_torque.DRIVE)."""
        value = str(override or '').strip().lower()
        return value if value in wheel_torque.DRIVE_TYPES else wheel_torque.drive_type(usb_id)

    TEXTURE_PARTS = ('road', 'impact', 'lock', 'engine')
    DYNAMICS_KEYS = {'countersteer_column': True, 'countersteer_align': True, 'countersteer_friction_removal': False}

    @classmethod
    def _texture_defaults(cls, supported, usb_id=None, drive_override=None):
        from .ffb_addon import STRENGTH
        from .ffb_rbr import LINEARITY
        from .ffb_engine import OVERALL_STRENGTH as OVERALL, OVERALL_KNEE, OVERALL_MAPPING, OVERALL_PULL, OVERALL_PULL_LEVEL, \
            OVERALL_COLUMN_DAMPING, OVERALL_FORZA_MODEL, OVERALL_RBR_SPRING, OVERALL_RBR_SOURCE, OVERALL_RBR_CUT, OVERALL_CASTER_FLOOR, OVERALL_ENGINE_FEEL
        from .ffb_kick import OVERALL_KICK, OVERALL_KICK_LEVEL
        from .ffb_texture import LEVEL
        from .ffb_gear import OVERALL_GEAR_FLIP, OVERALL_GEAR_GUARD_LEVEL
        drive = cls.wheel_drive(usb_id, drive_override)
        # The overall Countersteer force in % of the car's real steering torque, and where it stops being played 1:1
        data = {'countersteer_strength': int(round(OVERALL * 100)) if supported else None,
                'countersteer_knee': int(round(OVERALL_KNEE * 100)) if supported else None,
                'countersteer_rbr_cut': OVERALL_RBR_CUT if supported else None,
                'countersteer_caster_floor': OVERALL_CASTER_FLOOR if supported else None,
                'countersteer_engine_feel': OVERALL_ENGINE_FEEL if supported else None,
                'countersteer_mapping': OVERALL_MAPPING if supported else None,
                'countersteer_forza_model': OVERALL_FORZA_MODEL if supported else None,
                'countersteer_rbr_spring': OVERALL_RBR_SPRING if supported else None,
                'countersteer_rbr_source': OVERALL_RBR_SOURCE if supported else None,
                'countersteer_drive_pull': OVERALL_PULL if supported else None,
                'countersteer_drive_pull_level': OVERALL_PULL_LEVEL if supported else None,
                'countersteer_kick': OVERALL_KICK if supported else None,
                'countersteer_kick_level': OVERALL_KICK_LEVEL if supported else None,
                'countersteer_gear_guard': (drive == 'gear') if supported else None,     # on for a gear drive, off for the rest
                'countersteer_gear_guard_level': OVERALL_GEAR_GUARD_LEVEL if supported else None,
                'countersteer_gear_flip': OVERALL_GEAR_FLIP if supported else None,
                'countersteer_wheel_drive': '' if supported else None,       # '': from the wheel table
                'countersteer_column_added': OVERALL_COLUMN_DAMPING if supported else None,
                'countersteer_rim_mm': 0 if supported else None,       # 0: from the wheel table
                'countersteer_car_wheel_mm': int(wheel_torque.CAR_WHEEL_MM) if supported else None,
                'countersteer_power_class': ffb_carprops.DEFAULT_POWER_CLASS if supported else None,
                'countersteer_texture': int(round(LEVEL * 100)) if supported else None}
        for part in cls.TEXTURE_PARTS:
            data['countersteer_texture_' + part] = True if supported else None
        # Richard Burns Rally: how much of NGP's force curve Countersteer undoes (ffb_rbr)
        data['countersteer_linearity'] = LINEARITY if supported else None
        # Forza Horizon: how much of the add-on's lightening is played (ffb_addon)
        data['countersteer_lightness'] = int(round(STRENGTH * 100)) if supported else None
        # The driver's virtual steering dynamics (ffb_dynamics): the car's column feel and the local aligning spring
        # on, friction removal (a column lighter than the wheel's own) off
        for key, on in cls.DYNAMICS_KEYS.items():
            data[key] = on if supported else None
        return data

    def pedal_bit(self, name):
        """The invert_pedals bit (also the driver's pedal_response_* key) of
        the named pedal on this device; None when it has no such pedal."""
        mask = self.device.get_invert_pedals() or 0
        for code, (_released, _pressed, bit) in self.device.pedal_axes(mask).items():
            if bit and self.device.PEDAL_NAMES.get(code) == name:
                return bit
        return None

    def read_pedal_response(self, name):
        """What the driver holds for the pedal, so reading the device never
        resets its curves; None when it has no pedal response."""
        if not self.device.has_pedal_response():
            return None
        bit = self.pedal_bit(name)
        if bit is None:
            return None
        try:
            return self.valid_response(self.device.get_pedal_response(bit))
        except OSError as e:
            logging.warning("Can't read the %s response: %s", name, e)
            return None

    @classmethod
    def valid_response(cls, value):
        """A (start, end, sensitivity) of three ints with 0 <= start < end <=
        100 and 0 <= sensitivity <= 100; None when it isn't one. Values past
        the ranges the UI offers are kept (the UI clamps them for display),
        so what the driver holds is never rewritten unless the user changes it."""
        try:
            start, end, sensitivity = value
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in (start, end, sensitivity)):
                return None
        except (TypeError, ValueError):
            return None
        if not 0 <= start < end <= 100 or not 0 <= sensitivity <= 100:
            return None
        return (start, end, sensitivity)

    def refresh_pedal_responses(self):
        """Read the pedal responses again where the model has none, for after
        the udev rules made the attributes readable and writable or the input
        device opened, and write the ones a profile set that never got out."""
        for name in self.PEDAL_RESPONSES:
            key = name + '_response'
            if self.data[key] is None:
                self.data[key] = self.read_pedal_response(name)
                if self.data[key] is not None:
                    # what the driver holds: nothing to write
                    self.pedal_response_written.add(name)
                    if self.ui is not None:
                        self.ui.set_pedal_response(name, self.data[key])
            elif name not in self.pedal_response_written:
                self.apply_pedal_response(name)

    def update_from_device_settings(self):
        self.data.update(self.read_device_settings())
        for name in self.PEDAL_RESPONSES:
            if self.data[name + '_response'] is not None:
                # what the driver holds: nothing to write back
                self.pedal_response_written.add(name)

    def get_profile(self):
        return self.profile

    def load(self, profile_file, force=False):
        logging.debug('Load')

        if profile_file == self.profile and not force:
            return

        config = configparser.ConfigParser()
        config.read(profile_file)
        data = self.defaults.copy()
        for (key, value) in config['DEFAULT'].items():
            if key in RETIRED_KEYS:
                continue
            if key not in self.types:
                logging.warning("Unknown profile setting name: %s", key)
                continue
            if value is None:
                continue
            if self.types[key] == 'string':
                data[key] = value
            elif self.types[key] == 'integer':
                data[key] = int(value)
            elif self.types[key] == 'boolean':
                data[key] = bool(int(value))
            elif self.types[key] == 'tuple':
                try:
                    data[key] = tuple(map(int, value.split(',')))
                except ValueError:
                    logging.warning("Profile %s: ignoring invalid %s %s", profile_file, key, value)

        # Profiles from before the force feedback switch existed
        if data['ffb_enabled'] is None and data['ff_gain'] is not None:
            data['ffb_enabled'] = True
        # A profile saved before the driver could invert pedals says
        # nothing about it; keep what the device is doing rather than
        # greying the boxes out.
        if data['invert_pedals'] is None:
            data['invert_pedals'] = self.device.get_invert_pedals()
        # Likewise for the pedal response: an older profile leaves it as is
        for name in self.PEDAL_RESPONSES:
            key = name + '_response'
            valid = None
            if data[key] is not None:
                valid = self.valid_response(data[key])
                if valid is None:
                    logging.warning("Profile %s: ignoring invalid %s %s", profile_file, key, data[key])
                data[key] = valid
            if valid is not None:
                self.pedal_response_written.discard(name)
            if data[key] is None:
                data[key] = self.data.get(key)
        if data['rev_leds'] is not None:
            if data['rev_leds_shift_unit'] == 'launch':
                # a development build had the launch limiter as a unit
                data['rev_leds_shift_unit'], data['rev_leds_launch'] = 'percent', True
            if data['rev_leds_shift_unit'] not in ('percent', 'rpm'):
                data['rev_leds_shift_unit'] = 'percent'
            # Profiles from before the shift point: keep the old fixed 97 %
            # (a new profile starts at 95)
            if data['rev_leds_shift'] is None and data['rev_leds_shift_unit'] == 'percent':
                data['rev_leds_shift'] = 97
            # Profiles from before these existed: both on, as for a new one
            if data['rev_leds_launch'] is None:
                data['rev_leds_launch'] = True
            if data['rev_leds_learnt'] is None:
                data['rev_leds_learnt'] = True
            data['rev_leds_shift'] = self._clamp_shift(data['rev_leds_shift_unit'], data['rev_leds_shift'])

        # A profile from before Auto: no port, or the 5310 the app wrote as
        # its default without being asked (one of Auto's ports), listens on
        # all of them. Any other port stays fixed, 5300 too: Auto only
        # probes 5300 (Forza Horizon 6's range), and a game set to it would
        # lose telemetry. The port itself is kept: an older Oversteer
        # reading the profile listens there as before.
        if data['rev_leds_port_auto'] is None:
            data['rev_leds_port_auto'] = self._port_auto(data)

        # A profile saved before hotkeys existed keeps the ones in use, so
        # switching to it (perhaps by a hotkey) doesn't strand the others.
        if data['hotkeys'] is None:
            data['hotkeys'] = self.data.get('hotkeys') or ''

        # A profile from before the note and the plug flag has neither
        if data['profile_note'] is None:
            data['profile_note'] = ''
        if data['apply_on_plug'] is None:
            data['apply_on_plug'] = False
        # A profile from before Countersteer FFB: off (kept None where the driver has no replace mode)
        if data['countersteer_ffb'] is None:
            data['countersteer_ffb'] = None if self.data.get('countersteer_ffb') is None else False
        # ... or from before its texture: the defaults
        texture = self._texture_defaults(self.data.get('countersteer_ffb') is not None, getattr(self.device, 'usb_id', None),
                                         data['countersteer_wheel_drive'])
        for key, value in texture.items():
            if data[key] is None:
                data[key] = value
        if data['countersteer_texture'] is not None:
            data['countersteer_texture'] = min(max(data['countersteer_texture'], 0), 100)
        for key in ('countersteer_linearity', 'countersteer_lightness'):
            if data[key] is not None:
                data[key] = min(max(data[key], 0), 100)
        if data['countersteer_strength'] is not None:
            data['countersteer_strength'] = min(max(data['countersteer_strength'], STRENGTH_MIN), STRENGTH_MAX)
        if data['countersteer_knee'] is not None:
            data['countersteer_knee'] = min(max(data['countersteer_knee'], KNEE_MIN), KNEE_MAX)
        if data['countersteer_rbr_cut'] is not None:
            data['countersteer_rbr_cut'] = min(max(data['countersteer_rbr_cut'], RBR_CUT_MIN), RBR_CUT_MAX)
        if data['countersteer_caster_floor'] is not None:
            data['countersteer_caster_floor'] = min(max(data['countersteer_caster_floor'], 0), 100)
        if data['countersteer_engine_feel'] is not None:
            data['countersteer_engine_feel'] = min(max(data['countersteer_engine_feel'], 0), 300)
        if data['countersteer_drive_pull_level'] is not None:
            data['countersteer_drive_pull_level'] = min(max(data['countersteer_drive_pull_level'], 0), 100)
        if data['countersteer_column_added'] is not None:
            data['countersteer_column_added'] = min(max(data['countersteer_column_added'], 0), 100)
        if data['countersteer_rim_mm']:
            data['countersteer_rim_mm'] = int(min(max(data['countersteer_rim_mm'], wheel_torque.RIM_RANGE[0]),
                                                  wheel_torque.RIM_RANGE[1]))
        if data['countersteer_car_wheel_mm'] is not None:
            data['countersteer_car_wheel_mm'] = int(min(max(data['countersteer_car_wheel_mm'], wheel_torque.CAR_WHEEL_RANGE[0]),
                                                        wheel_torque.CAR_WHEEL_RANGE[1]))
        if data['countersteer_kick_level'] is not None:
            data['countersteer_kick_level'] = min(max(data['countersteer_kick_level'], 0), 100)
        if data['countersteer_gear_guard_level'] is not None:
            data['countersteer_gear_guard_level'] = min(max(data['countersteer_gear_guard_level'], 0), 100)
        if data['countersteer_gear_flip'] is not None:
            data['countersteer_gear_flip'] = min(max(data['countersteer_gear_flip'], GEAR_FLIP_RANGE[0]), GEAR_FLIP_RANGE[1])
        if data['countersteer_mapping'] is not None and data['countersteer_mapping'] not in MAPPINGS:
            data['countersteer_mapping'] = MAPPINGS[0]

        self.data = data
        self.save_reference_values()
        self.profile = profile_file

        logging.debug("\n".join('{0} = {1}'.format(k, v) for k, v in self.data.items()))

    def save(self, profile_file):
        data = {}
        for key, value in self.data.items():
            if value is None:
                continue
            if key == 'profile_note':
                # One line; a % is doubled because the file is read with
                # interpolation (an older Oversteer too) and a lone one fails.
                data[key] = ' '.join(str(value).split()).replace('%', '%%')
            elif self.types[key] == 'string':
                data[key] = value
            elif self.types[key] == 'integer':
                data[key] = int(value)
            elif self.types[key] == 'boolean':
                data[key] = int(bool(value))
            elif self.types[key] == 'tuple':
                data[key] = ','.join(map(str, value))

        config = configparser.ConfigParser()
        config['DEFAULT'] = data
        with open(profile_file, 'w') as configfile:
            config.write(configfile)

        self.save_reference_values()

    def save_reference_values(self):
        data = self.data.copy()
        self.reference_values = data
        if self.ui is not None:
            self.ui.disable_save_profile()

    def set_if_changed(self, key, value):
        if self.data[key] != value:
            self.data[key] = value
            if self.ui is not None:
                self.update_save_profile_button()
            if self.device:
                return True
        return False

    def get_mode_list(self):
        return self.device.list_modes()

    def set_mode(self, value):
        if self.set_if_changed('mode', value):
            self.device.set_mode(value)

    def get_mode(self):
        return self.data['mode']

    def set_range(self, value):
        value = int(value)
        if self.set_if_changed('range', value):
            self.device.set_range(value)

    def get_range(self):
        return self.data['range']

    def set_sensitivity(self, value):
        value = int(value)
        if self.set_if_changed('sensitivity', value):
            self.device.set_sensitivity(value)

    def get_sensitivity(self):
        return self.data['sensitivity']

    def set_ff_gain(self, value):
        value = int(value)
        if self.set_if_changed('ff_gain', value) and self.data['ffb_enabled'] is not False:
            self.device.set_ff_gain(value)

    def set_ffb_enabled(self, value):
        value = bool(value)
        if self.data['ff_gain'] is None:
            # Device without a gain control: nothing to switch
            return
        if self.set_if_changed('ffb_enabled', value):
            self.device.set_ff_gain(self.data['ff_gain'] if value else 0)
            if self.ui is not None:
                self.ui.set_ffb_enabled(value)

    def get_ffb_enabled(self):
        return self.data['ffb_enabled']

    def set_invert_pedals(self, mask):
        mask = int(mask)
        if self.set_if_changed('invert_pedals', mask):
            # Read events the new way before the driver starts sending them:
            # it re-emits the axes as soon as the setting is written.
            if self.ui is not None:
                self.ui.controller.update_pedals(mask)
            self.device.set_invert_pedals(mask)

    def get_invert_pedals(self):
        return self.data['invert_pedals']

    def apply_pedal_response(self, name):
        """Write a pedal's response to the driver. It is in pedal-travel
        terms, from the released end: the driver mirrors it itself when
        invert_pedals flips the axis. It ignores the settings while the
        pedals are combined, so nothing is written then."""
        response = self.data[name + '_response']
        if response is None or not self.device or self.data['combine_pedals']:
            return
        bit = self.pedal_bit(name)
        if bit is None:
            # once per pedal: update_pedals gets here on every refresh
            level = logging.debug if name in self.pedal_axis_warned else logging.warning
            self.pedal_axis_warned.add(name)
            level("No %s axis on the input device (yet); pedal response not written", name)
            return
        try:
            if self.device.set_pedal_response(bit, *response):
                self.pedal_response_written.add(name)
        except OSError as e:
            logging.warning("Can't set the %s response: %s", name, e)

    def apply_pedal_responses(self):
        for name in self.PEDAL_RESPONSES:
            self.apply_pedal_response(name)

    def set_pedal_response(self, name, start, end, sensitivity):
        start, end, sensitivity = int(start), int(end), int(sensitivity)
        if self.valid_response((start, end, sensitivity)) != (start, end, sensitivity):
            raise ValueError("pedal response out of range: {} {} {}".format(start, end, sensitivity))
        key = name + '_response'
        if self.data[key] is None:
            return
        if self.set_if_changed(key, (start, end, sensitivity)):
            self.pedal_response_written.discard(name)
            self.apply_pedal_response(name)

    def set_pedal_preset(self, name, preset_id):
        for pid, _label, response, _note in self.PEDAL_PRESETS[name]:
            if pid == preset_id:
                self.set_pedal_response(name, *response)
                return response
        raise ValueError("no preset {} for {}".format(preset_id, name))

    def set_clutch_response(self, start, end, sensitivity):
        self.set_pedal_response('clutch', start, end, sensitivity)

    def set_accelerator_response(self, start, end, sensitivity):
        self.set_pedal_response('accelerator', start, end, sensitivity)

    def set_brakes_response(self, start, end, sensitivity):
        self.set_pedal_response('brakes', start, end, sensitivity)

    def get_clutch_response(self):
        return self.data['clutch_response']

    def get_accelerator_response(self):
        return self.data['accelerator_response']

    def get_brakes_response(self):
        return self.data['brakes_response']

    def get_ff_gain(self):
        return self.data['ff_gain']

    def set_autocenter(self, value):
        value = int(value)
        if self.set_if_changed('autocenter', value):
            self.device.set_autocenter(value)

    def get_autocenter(self):
        return self.data['autocenter']

    def set_autocenter_persistent(self, value):
        value = bool(value)
        if self.set_if_changed('autocenter_persistent', value):
            self.device.set_autocenter_persistent(value)

    def get_autocenter_persistent(self):
        return self.data['autocenter_persistent']

    def set_app_gain(self, value):
        value = bool(value)
        if self.set_if_changed('app_gain', value):
            self.device.set_app_gain(value)

    def get_app_gain(self):
        return self.data['app_gain']

    def set_inertia_mode(self, value):
        value = bool(value)
        if self.set_if_changed('inertia_mode', value):
            self.device.set_inertia_mode(value)

    def get_inertia_mode(self):
        return self.data['inertia_mode']

    def set_combine_pedals(self, value):
        value = int(value)
        if self.set_if_changed('combine_pedals', value):
            self.device.set_combine_pedals(value)
            if self.ui is not None:
                self.ui.set_pedal_response_combined(bool(value))
            if not value:
                # The driver ignored the responses while combined
                self.apply_pedal_responses()

    def get_combine_pedals(self):
        return self.data['combine_pedals']

    def set_spring_level(self, value):
        value = int(value)
        if self.set_if_changed('spring_level', value):
            self.device.set_spring_level(value)

    def get_spring_level(self):
        return self.data['spring_level']

    def set_damper_level(self, value):
        value = int(value)
        if self.set_if_changed('damper_level', value):
            self.device.set_damper_level(value)

    def get_damper_level(self):
        return self.data['damper_level']

    def set_friction_level(self, value):
        value = int(value)
        if self.set_if_changed('friction_level', value):
            self.device.set_friction_level(value)

    def get_friction_level(self):
        return self.data['friction_level']

    def set_rumble_level(self, value):
        value = int(value)
        if self.set_if_changed('rumble_level', value):
            self.device.set_rumble_level(value)

    def get_rumble_level(self):
        return self.data['rumble_level']

    def set_ffb_leds(self, value):
        value = bool(value)
        if self.set_if_changed('ffb_leds', value):
            self.device.set_ffb_leds(1 if value else 0)

    def get_ffb_leds(self):
        return self.data['ffb_leds']

    def set_countersteer_ffb(self, value):
        value = bool(value)
        if self.set_if_changed('countersteer_ffb', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_ffb()

    def get_countersteer_ffb(self):
        return bool(self.data['countersteer_ffb'])

    def set_countersteer_strength(self, value):
        """Countersteer's own steering force, STRENGTH_MIN..STRENGTH_MAX % of the car's real steering torque."""
        value = min(max(int(value), STRENGTH_MIN), STRENGTH_MAX)
        if self.set_if_changed('countersteer_strength', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_strength()

    def get_countersteer_strength(self):
        from .ffb_engine import OVERALL_STRENGTH as OVERALL
        value = self.data.get('countersteer_strength')
        return int(round(OVERALL * 100)) if value is None else value

    def set_countersteer_knee(self, value):
        """The share of the wheel's peak torque the steering force is played 1:1 in Nm up to, KNEE_MIN..KNEE_MAX %."""
        value = min(max(int(value), KNEE_MIN), KNEE_MAX)
        if self.set_if_changed('countersteer_knee', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_knee()

    def get_countersteer_knee(self):
        from .ffb_engine import OVERALL_KNEE
        value = self.data.get('countersteer_knee')
        return int(round(OVERALL_KNEE * 100)) if value is None else value

    def set_countersteer_rbr_cut(self, value):
        """RBR's force cut: the low-pass corner on its force in Hz, RBR_CUT_MIN..RBR_CUT_MAX (the top: off)."""
        value = min(max(int(value), RBR_CUT_MIN), RBR_CUT_MAX)
        if self.set_if_changed('countersteer_rbr_cut', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_rbr_cut()

    def get_countersteer_rbr_cut(self):
        from .ffb_engine import OVERALL_RBR_CUT
        value = self.data.get('countersteer_rbr_cut')
        return OVERALL_RBR_CUT if value is None else value

    def set_countersteer_caster_floor(self, value):
        """RBR's "Caster in slides": the share of the mechanical caster's force kept in a slide, 0..100 % (0: off)."""
        value = min(max(int(value), 0), 100)
        if self.set_if_changed('countersteer_caster_floor', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_caster_floor()

    def get_countersteer_caster_floor(self):
        from .ffb_engine import OVERALL_CASTER_FLOOR
        value = self.data.get('countersteer_caster_floor')
        return OVERALL_CASTER_FLOOR if value is None else value

    def set_countersteer_engine_feel(self, value):
        """The engine hum's strength in % of its base level, 0..300 (0: off)."""
        value = min(max(int(value), 0), 300)
        if self.set_if_changed('countersteer_engine_feel', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_engine_feel()

    def get_countersteer_engine_feel(self):
        from .ffb_engine import OVERALL_ENGINE_FEEL
        value = self.data.get('countersteer_engine_feel')
        return OVERALL_ENGINE_FEEL if value is None else value

    def set_countersteer_mapping(self, value):
        """The steering mapping (profile INI only, no control): 'knee' (1:1 up to the knee, compressed above, the default) or
        'classic' (the uniform impedance scale)."""
        value = value if value in MAPPINGS else MAPPINGS[0]
        if self.set_if_changed('countersteer_mapping', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_mapping()

    def get_countersteer_mapping(self):
        from .ffb_engine import OVERALL_MAPPING
        value = self.data.get('countersteer_mapping')
        return value if value in MAPPINGS else OVERALL_MAPPING

    def set_countersteer_forza_model(self, value):
        """Forza Horizon (profile INI only, no control): True plays Countersteer's own force through the ACR chain, False keeps the
        game's force lightened by the add-on."""
        if self.set_if_changed('countersteer_forza_model', bool(value)) and self.ui is not None:
            self.ui.controller.apply_countersteer_forza_model()

    def get_countersteer_forza_model(self):
        from .ffb_engine import OVERALL_FORZA_MODEL
        value = self.data.get('countersteer_forza_model')
        return OVERALL_FORZA_MODEL if value is None else bool(value)

    def set_countersteer_rbr_spring(self, value):
        """Richard Burns Rally's aligning spring (profile INI only, no control): True plays it through the driver as ACR's, False
        leaves RBR with the column's damping and friction."""
        if self.set_if_changed('countersteer_rbr_spring', bool(value)) and self.ui is not None:
            self.ui.controller.apply_countersteer_rbr_spring()

    def get_countersteer_rbr_spring(self):
        from .ffb_engine import OVERALL_RBR_SPRING
        value = self.data.get('countersteer_rbr_spring')
        return OVERALL_RBR_SPRING if value is None else bool(value)

    def set_countersteer_rbr_source(self, value):
        """Richard Burns Rally's force (profile INI only, no control): 'derived' plays the kingpin torque of the car's physics, 'ngp'
        the game's own force recovered and played through the chain."""
        from .ffb_engine import RBR_SOURCES, OVERALL_RBR_SOURCE
        value = str(value).strip().lower()
        value = value if value in RBR_SOURCES else OVERALL_RBR_SOURCE
        if self.set_if_changed('countersteer_rbr_source', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_rbr_source()

    def get_countersteer_rbr_source(self):
        from .ffb_engine import RBR_SOURCES, OVERALL_RBR_SOURCE
        value = str(self.data.get('countersteer_rbr_source') or '').strip().lower()
        return value if value in RBR_SOURCES else OVERALL_RBR_SOURCE

    def set_countersteer_column_added(self, value):
        """The share of a real steering column's damping the knee mapping plays, 0..100 %."""
        value = min(max(int(value), 0), 100)
        if self.set_if_changed('countersteer_column_added', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_column_added()

    def get_countersteer_column_added(self):
        from .ffb_engine import OVERALL_COLUMN_DAMPING
        value = self.data.get('countersteer_column_added')
        return OVERALL_COLUMN_DAMPING if value is None else value

    def set_countersteer_rim_mm(self, value):
        """The wheel's rim diameter in mm, 200..400; 0: from the wheel table (wheel_torque.RIM_MM)."""
        value = int(value)
        value = 0 if value <= 0 else min(max(value, wheel_torque.RIM_RANGE[0]), wheel_torque.RIM_RANGE[1])
        if self.set_if_changed('countersteer_rim_mm', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_rim_mm()

    def get_countersteer_rim_mm_setting(self):
        """The saved rim diameter, 0 when none (from the table)."""
        return int(self.data.get('countersteer_rim_mm') or 0)

    def get_countersteer_rim_mm(self):
        """The rim diameter in effect: the saved one, else the table's for the wheel, else the G29's."""
        saved = self.get_countersteer_rim_mm_setting()
        if saved:
            return saved
        device = getattr(self, 'device', None)
        table = wheel_torque.rim_mm(getattr(device, 'usb_id', None))
        return int(round(table or wheel_torque.DEFAULT_RIM_MM))

    def set_countersteer_power_class(self, value):
        """The assist class, rally / street / off (profile INI only, no control): see ffb_carprops.POWER_CLASSES."""
        value = str(value).strip().lower()
        value = value if value in ffb_carprops.POWER_CLASSES else ffb_carprops.DEFAULT_POWER_CLASS
        if self.set_if_changed('countersteer_power_class', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_power_class()

    def get_countersteer_power_class(self):
        value = str(self.data.get('countersteer_power_class') or '').strip().lower()
        return value if value in ffb_carprops.POWER_CLASSES else ffb_carprops.DEFAULT_POWER_CLASS

    def get_rbr_auto_range(self):
        """Richard Burns Rally's car sets the wheel's range to its real steering lock (profile INI only, default on)."""
        value = self.data.get('rbr_auto_range')
        return True if value is None else bool(value)

    def set_countersteer_car_wheel_mm(self, value):
        """The car's steering wheel diameter in mm, 280..420: the hand-force scale is the
        wheel's rim over it."""
        value = int(min(max(int(value), wheel_torque.CAR_WHEEL_RANGE[0]), wheel_torque.CAR_WHEEL_RANGE[1]))
        if self.set_if_changed('countersteer_car_wheel_mm', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_car_wheel_mm()

    def get_countersteer_car_wheel_mm(self):
        value = self.data.get('countersteer_car_wheel_mm')
        return int(wheel_torque.CAR_WHEEL_MM) if value is None else value

    def set_countersteer_drive_pull(self, value):
        """Drive pull (the scrub couple of the drive's left/right split in the steering force) on or off."""
        if self.set_if_changed('countersteer_drive_pull', bool(value)) and self.ui is not None:
            self.ui.controller.apply_countersteer_drive_pull()

    def get_countersteer_drive_pull(self):
        from .ffb_engine import OVERALL_PULL
        value = self.data.get('countersteer_drive_pull')
        return OVERALL_PULL if value is None else bool(value)

    def set_countersteer_drive_pull_level(self, value):
        """Drive pull's level, 0..100 % of the physical scrub radius."""
        value = min(max(int(value), 0), 100)
        if self.set_if_changed('countersteer_drive_pull_level', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_drive_pull()

    def get_countersteer_drive_pull_level(self):
        from .ffb_engine import OVERALL_PULL_LEVEL
        value = self.data.get('countersteer_drive_pull_level')
        return OVERALL_PULL_LEVEL if value is None else value

    def set_countersteer_kick(self, value):
        """Kickback (the onset pulse of the scrub couple, ffb_kick) on or off."""
        if self.set_if_changed('countersteer_kick', bool(value)) and self.ui is not None:
            self.ui.controller.apply_countersteer_kick()

    def get_countersteer_kick(self):
        from .ffb_kick import OVERALL_KICK
        value = self.data.get('countersteer_kick')
        return OVERALL_KICK if value is None else bool(value)

    def set_countersteer_kick_level(self, value):
        """Kickback's level, 0..100 % of its amplitude."""
        value = min(max(int(value), 0), 100)
        if self.set_if_changed('countersteer_kick_level', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_kick()

    def get_countersteer_kick_level(self):
        from .ffb_kick import OVERALL_KICK_LEVEL
        value = self.data.get('countersteer_kick_level')
        return OVERALL_KICK_LEVEL if value is None else value

    def set_countersteer_gear_guard(self, value):
        """Gear guard (ffb_gear) on or off."""
        if self.set_if_changed('countersteer_gear_guard', bool(value)) and self.ui is not None:
            self.ui.controller.apply_countersteer_gear_guard()

    def get_countersteer_gear_guard(self):
        value = self.data.get('countersteer_gear_guard')
        if value is None:
            return self.get_countersteer_wheel_drive() == 'gear'
        return bool(value)

    def set_countersteer_gear_guard_level(self, value):
        """Gear guard's level, 0..100 % of its preload."""
        value = min(max(int(value), 0), 100)
        if self.set_if_changed('countersteer_gear_guard_level', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_gear_guard()

    def get_countersteer_gear_guard_level(self):
        from .ffb_gear import OVERALL_GEAR_GUARD_LEVEL
        value = self.data.get('countersteer_gear_guard_level')
        return OVERALL_GEAR_GUARD_LEVEL if value is None else value

    def set_countersteer_gear_flip(self, value):
        """The gear guard's flip point, % of full scale."""
        value = min(max(int(value), GEAR_FLIP_RANGE[0]), GEAR_FLIP_RANGE[1])
        if self.set_if_changed('countersteer_gear_flip', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_gear_guard()

    def get_countersteer_gear_flip(self):
        from .ffb_gear import OVERALL_GEAR_FLIP
        value = self.data.get('countersteer_gear_flip')
        return OVERALL_GEAR_FLIP if value is None else value

    def get_countersteer_wheel_drive(self):
        """'gear', 'belt' or 'direct': the profile's countersteer_wheel_drive (INI only), else the wheel table's."""
        return self.wheel_drive(getattr(getattr(self, 'device', None), 'usb_id', None), self.data.get('countersteer_wheel_drive'))

    def set_countersteer_texture(self, value):
        """The texture's master level, 0..100 %."""
        value = min(max(int(value), 0), 100)
        if self.set_if_changed('countersteer_texture', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_texture()

    def set_countersteer_texture_part(self, part, value):
        if self.set_if_changed('countersteer_texture_' + part, bool(value)) and self.ui is not None:
            self.ui.controller.apply_countersteer_texture()

    def set_countersteer_linearity(self, value):
        """Richard Burns Rally: how much of the game's force curve Countersteer undoes, 0..100 %."""
        value = min(max(int(value), 0), 100)
        if self.set_if_changed('countersteer_linearity', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_linearity()

    def get_countersteer_linearity(self):
        from .ffb_rbr import LINEARITY
        value = self.data.get('countersteer_linearity')
        return LINEARITY if value is None else value

    def set_countersteer_lightness(self, value):
        """Forza Horizon: "Understeer lightness", how much of the add-on's lightening is played, 0..100 %."""
        value = min(max(int(value), 0), 100)
        if self.set_if_changed('countersteer_lightness', value) and self.ui is not None:
            self.ui.controller.apply_countersteer_lightness()

    def get_countersteer_lightness(self):
        from .ffb_addon import STRENGTH
        value = self.data.get('countersteer_lightness')
        return int(round(STRENGTH * 100)) if value is None else value

    def set_countersteer_dynamics(self, key, value):
        """One of the driver's virtual steering dynamics switches (DYNAMICS_KEYS): column feel, local alignment
        spring, friction removal."""
        if key not in self.DYNAMICS_KEYS:
            raise KeyError(key)
        if self.set_if_changed(key, bool(value)) and self.ui is not None:
            self.ui.controller.apply_countersteer_dynamics()

    def get_countersteer_dynamics(self):
        """(column feel, local aligning spring, friction removal) for the engine."""
        return tuple(bool(self.data.get(key)) if self.data.get(key) is not None else default
                     for key, default in self.DYNAMICS_KEYS.items())

    def get_countersteer_texture(self):
        """(level 0..1, {part: on}) for the engine."""
        from .ffb_texture import LEVEL
        level = self.data['countersteer_texture']
        parts = {part: self.data['countersteer_texture_' + part] is not False for part in self.TEXTURE_PARTS}
        return (LEVEL if level is None else level / 100.0), parts

    def set_rev_leds(self, value):
        value = bool(value)
        if self.set_if_changed('rev_leds', value) and self.ui is not None:
            self.ui.controller.apply_rev_leds()

    def get_rev_leds(self):
        return self.data['rev_leds']

    @staticmethod
    def _valid_port(value):
        """The port as an int, or None (meaning the default) if it is not a
        possible UDP port; below 1024 is kept (it needs root) and flagged
        by the UI."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
        return value if 1 <= value <= 65535 else None

    # No port, or the 5310 the app saved as its default since 0.14 (5300 before it stays fixed:
    # Auto only probes 5300).
    # An older Oversteer that saves a profile drops rev_leds_port_auto (unknown to it), so a
    # fixed 5310 saved by it reads as Auto again here: accepted, Auto listens on 5310 too.
    LEGACY_DEFAULT_PORTS = (None, 5310)

    def set_rev_leds_port(self, value):
        """Listen on one port, or on Auto's (`value` telemetry.AUTO). Auto
        is kept apart from the port (rev_leds_port_auto), so a profile
        that picks 5310 itself keeps it; the port stays as it was when
        Auto is picked, for an older Oversteer reading the profile."""
        if value == 'auto':                        # telemetry.AUTO
            changed = self.set_if_changed('rev_leds_port_auto', True)
        else:
            value = self._valid_port(value)
            changed = self.set_if_changed('rev_leds_port', value)
            changed = self.set_if_changed('rev_leds_port_auto', False) or changed
        if changed and self.ui is not None and self.data['rev_leds']:
            self.ui.controller.apply_rev_leds()

    def get_rev_leds_port(self):
        """The fixed port, or None (the default port). See get_rev_leds_port_auto()."""
        return self._valid_port(self.data['rev_leds_port'])

    def get_rev_leds_port_auto(self):
        """Whether the listener takes Auto's ports (a profile with nothing saved does)."""
        return self._port_auto(self.data)

    @classmethod
    def _port_auto(cls, data):
        auto = data.get('rev_leds_port_auto')
        if auto is None:
            return cls._valid_port(data.get('rev_leds_port')) in cls.LEGACY_DEFAULT_PORTS
        return bool(auto)

    @staticmethod
    def _clamp_shift(unit, value):
        """The shift point within its unit's range; None -> the default."""
        if unit == 'rpm':
            return max(1000, min(25000, int(value))) if value is not None else 7000
        return max(50, min(100, int(value))) if value is not None else 95

    def set_rev_leds_shift(self, value):
        value = self._clamp_shift(self.get_rev_leds_shift_unit(), value)
        if self.set_if_changed('rev_leds_shift', value) and self.ui is not None and self.data['rev_leds']:
            self.ui.controller.update_rev_leds_shift()

    def get_rev_leds_shift(self):
        return self.data['rev_leds_shift']

    def set_rev_leds_shift_unit(self, unit, value=None):
        """Change the unit of the shift point: 'percent' of the max RPM
        (the limiter learnt at a launch when there is one) or 'rpm'.
        `value` is the converted shift point in the new unit (the
        controller supplies it when it can), otherwise the unit's default."""
        if unit == 'auto':
            # Each gear's learnt best upshift; the stored point stays as the
            # fallback for gears not learnt yet
            if self.set_if_changed('rev_leds_learnt', True) and self.ui is not None:
                self.ui.set_rev_leds(self.data['rev_leds'], None, self.data['rev_leds_shift'], 'auto')
                self.ui.controller.update_rev_leds_shift()
            return
        unit = 'rpm' if unit == 'rpm' else 'percent'
        if self.data['rev_leds_learnt'] is not False:
            # A fixed point chosen: the learnt ones are no longer used
            self.set_if_changed('rev_leds_learnt', False)
            if self.data['rev_leds_shift_unit'] == unit:
                if self.ui is not None:
                    self.ui.set_rev_leds(self.data['rev_leds'], None, self.data['rev_leds_shift'], unit)
                    self.ui.controller.update_rev_leds_shift()
                return
        if self.data['rev_leds_shift_unit'] == unit:
            return
        # Value first, so the save button reflects both keys
        self.data['rev_leds_shift'] = self._clamp_shift(unit, value)
        self.set_if_changed('rev_leds_shift_unit', unit)
        if self.ui is not None:
            self.ui.set_rev_leds(self.data['rev_leds'], None, self.data['rev_leds_shift'], unit)
            if self.data['rev_leds']:
                self.ui.controller.update_rev_leds_shift()

    def set_rev_leds_launch(self, value):
        """Learn the limiter at each launch."""
        if self.set_if_changed('rev_leds_launch', bool(value)) and self.ui is not None:
            self.ui.controller.update_rev_leds_shift()

    def get_rev_leds_launch(self):
        return self.data['rev_leds_launch'] is not False

    def set_rev_leds_learnt(self, value):
        """Put the rev lights' shift point where the learner found each
        gear's best upshift, once it knows."""
        if self.set_if_changed('rev_leds_learnt', bool(value)) and self.ui is not None:
            self.ui.controller.update_rev_leds_shift()

    def get_rev_leds_learnt(self):
        return self.data['rev_leds_learnt'] is not False

    def get_rev_leds_shift_unit(self):
        return self.data['rev_leds_shift_unit'] or 'percent'

    def set_ffb_overlay(self, value):
        self.set_if_changed('ffb_overlay', bool(value))

    def get_ffb_overlay(self):
        return self.data['ffb_overlay']

    def set_range_overlay(self, value):
        self.set_if_changed('range_overlay', value)

    def get_range_overlay(self):
        return self.data['range_overlay']

    def set_use_buttons(self, value):
        self.set_if_changed('use_buttons', bool(value))

    def get_use_buttons(self):
        return self.data['use_buttons']

    def set_hotkeys(self, value):
        """The wheel-button bindings, as hotkeys.serialize() writes them."""
        self.set_if_changed('hotkeys', value or '')

    def get_hotkeys(self):
        return self.data['hotkeys'] or ''

    def set_center_wheel(self, value):
        value = bool(value)
        if self.set_if_changed('center_wheel', value) and value:
            self.device.center_wheel()

    def set_profile_note(self, value):
        self.set_if_changed('profile_note', ' '.join(str(value or '').split()))

    def get_profile_note(self):
        return self.data['profile_note']

    def set_apply_on_plug(self, value):
        self.set_if_changed('apply_on_plug', bool(value))

    def get_apply_on_plug(self):
        return self.data['apply_on_plug']

    def set_start_app_manually(self, value):
        value = bool(value)
        self.set_if_changed('start_app_manually', value)

    def get_start_app_manually(self):
        return self.data['start_app_manually']

    def flush_device(self):
        logging.debug("flush_device")
        if self.data['mode'] is not None:
            self.device.set_mode(self.data['mode'])
        if self.data['range'] is not None:
            self.device.set_range(self.data['range'])
        if self.data['sensitivity'] is not None:
            self.device.set_sensitivity(self.data['sensitivity'])
        if self.data['combine_pedals'] is not None:
            self.device.set_combine_pedals(self.data['combine_pedals'])
        if self.data['center_wheel']:
            self.device.center_wheel()
        if self.data['autocenter'] is not None:
            self.device.set_autocenter(self.data['autocenter'])
        if self.data['autocenter_persistent'] is not None:
            self.device.set_autocenter_persistent(self.data['autocenter_persistent'])
        if self.data['app_gain'] is not None:
            self.device.set_app_gain(self.data['app_gain'])
        if self.data['inertia_mode'] is not None:
            self.device.set_inertia_mode(self.data['inertia_mode'])
        if self.data['ffb_enabled'] is False and self.data['ff_gain'] is not None:
            self.device.set_ff_gain(0)
        elif self.data['ff_gain'] is not None:
            self.device.set_ff_gain(self.data['ff_gain'])
        if self.data['invert_pedals'] is not None:
            self.device.set_invert_pedals(self.data['invert_pedals'])
        self.apply_pedal_responses()
        if self.data['spring_level'] is not None:
            self.device.set_spring_level(self.data['spring_level'])
        if self.data['damper_level'] is not None:
            self.device.set_damper_level(self.data['damper_level'])
        if self.data['friction_level'] is not None:
            self.device.set_friction_level(self.data['friction_level'])
        if self.data['rumble_level'] is not None:
            self.device.set_rumble_level(self.data['rumble_level'])
        if self.data['ffb_leds'] is not None:
            self.device.set_ffb_leds(self.data['ffb_leds'])

    def flush_ui(self, data = None):
        logging.debug("flush_ui")
        if data is None:
            data = self.data
        self.ui.set_mode(data['mode'])
        self.ui.set_range(data['range'])
        self.ui.set_sensitivity(data['sensitivity'])
        self.ui.set_ff_gain(data['ff_gain'])
        self.ui.set_autocenter(data['autocenter'])
        self.ui.set_autocenter_persistent(data['autocenter_persistent'])
        self.ui.set_app_gain(data['app_gain'])
        self.ui.set_inertia_mode(data['inertia_mode'])
        self.ui.set_combine_pedals(data['combine_pedals'])
        self.ui.set_invert_pedals(data['invert_pedals'])
        for name in self.PEDAL_RESPONSES:
            self.ui.set_pedal_response(name, data[name + '_response'])
        self.ui.set_spring_level(data['spring_level'])
        self.ui.set_damper_level(data['damper_level'])
        self.ui.set_friction_level(data['friction_level'])
        self.ui.set_rumble_level(data['rumble_level'])
        self.ui.set_ffb_leds(data['ffb_leds'])
        self.ui.set_rev_leds(data['rev_leds'], 'auto' if self._port_auto(data)
                             else (self._valid_port(data['rev_leds_port']) or 5310), data['rev_leds_shift'],
                             'auto' if data['rev_leds'] is not None and data['rev_leds_learnt'] is not False
                             else data['rev_leds_shift_unit'])
        self.ui.set_rev_leds_options(data['rev_leds_launch'], data['rev_leds_learnt'])
        self.ui.set_ffb_overlay(data['ffb_overlay'])
        self.ui.set_range_overlay(data['range_overlay'])
        self.ui.set_use_buttons(data['use_buttons'])
        self.ui.set_hotkeys(data['hotkeys'])
        self.ui.set_center_wheel(data['center_wheel'])
        self.ui.set_start_app_manually(data['start_app_manually'])
        self.ui.set_profile_note(data['profile_note'])
        self.ui.set_apply_on_plug(data['apply_on_plug'])
        self.ui.set_countersteer_ffb(data['countersteer_ffb'])
        self.ui.set_countersteer_strength(data['countersteer_strength'])
        self.ui.set_countersteer_knee(data['countersteer_knee'])
        self.ui.set_countersteer_rbr_cut(data['countersteer_rbr_cut'])
        self.ui.set_countersteer_caster_floor(data['countersteer_caster_floor'])
        self.ui.set_countersteer_engine_feel(data['countersteer_engine_feel'])
        self.ui.set_countersteer_column_added(data['countersteer_column_added'])
        self.ui.set_countersteer_rim_mm(None if data['countersteer_rim_mm'] is None else self.get_countersteer_rim_mm())
        self.ui.set_countersteer_car_wheel_mm(data['countersteer_car_wheel_mm'])
        self.ui.set_countersteer_drive_pull(data['countersteer_drive_pull'], data['countersteer_drive_pull_level'])
        self.ui.set_countersteer_kick(data['countersteer_kick'], data['countersteer_kick_level'])
        self.ui.set_countersteer_gear_guard(data['countersteer_gear_guard'], data['countersteer_gear_guard_level'])
        self.ui.set_countersteer_gear_flip(data['countersteer_gear_flip'])
        self.ui.set_countersteer_texture(data['countersteer_texture'],
                                         {part: data['countersteer_texture_' + part] for part in self.TEXTURE_PARTS})
        self.ui.set_countersteer_linearity(data['countersteer_linearity'])
        self.ui.set_countersteer_lightness(data['countersteer_lightness'])
        self.ui.set_countersteer_toggles(data['countersteer_column'], data['countersteer_align'],
                                         data['countersteer_friction_removal'])
        # Last: it greys out the strength controls when force feedback is off
        self.ui.set_ffb_enabled(data['ffb_enabled'])
        self.update_save_profile_button()

