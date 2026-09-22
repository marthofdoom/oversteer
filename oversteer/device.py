from evdev import ecodes, InputDevice, InputEvent, ff
import threading
import grp
import logging
import os
import pwd
import re
import select
import time
from . import wheel_ids as wid

logging.basicConfig(level=logging.DEBUG)

class Device:

    last_axis_value = {
        ecodes.ABS_X: 0,
        ecodes.ABS_Y: 0,
        ecodes.ABS_Z: 0,
        ecodes.ABS_RZ: 0,
    }

    def __init__(self, device_manager, data):
        self.device_manager = device_manager
        self.input_device = None
        self._device_lock = threading.Lock()
        self.id = None
        self.vendor_id = None
        self.product_id = None
        self.usb_id = None
        self.dev_path = None
        self.dev_name = None
        self.name = None
        self.ready = True
        self.max_range = None

        self.set(data)

    def set(self, data):
        for key, value in data.items():
            setattr(self, key, value)

    def close(self):
        if self.input_device is not None:
            self.input_device.close()
            self.input_device = None

    def disable(self):
        self.dev_name = None
        self.ready = False
        self.close()

    def enable(self):
        self.ready = True

    def is_ready(self):
        return self.ready

    def get_id(self):
        return self.id

    def device_file(self, filename):
        return os.path.join(self.dev_path, filename)

    def checked_device_file(self, filename):
        path = self.device_file(filename)
        if not os.access(path, os.F_OK | os.R_OK | os.W_OK):
            return False
        return path

    def check_file_permissions(self, filename):
        if filename is None:
            return True
        path = self.device_file(filename)
        if not os.access(path, os.F_OK):
            return True
        status = os.stat(path)
        logging.debug("check_file_permissions mode: %s user: %s group: %s file: %s", oct(status.st_mode & 0o777),
                pwd.getpwuid(status.st_uid)[0], grp.getgrgid(status.st_gid)[0], path)
        if os.access(path, os.R_OK | os.W_OK):
            return True
        return False

    def get_max_range(self):
        return self.max_range

    def list_modes(self):
        path = self.checked_device_file("alternate_modes")
        if not path:
            return None
        with open(path, "r") as file:
            data = file.read()
        lines = data.splitlines()
        reg = re.compile("([^:]+): (.*)")
        alternate_modes = []
        for line in lines:
            matches = reg.match(line)
            mode_id = matches.group(1)
            if mode_id == "native":
                continue
            name = matches.group(2)
            if name.endswith("*"):
                name = name[:-2]
                selected = True
            else:
                selected = False
            alternate_modes.append([mode_id, name, selected])
        return alternate_modes

    def get_mode(self):
        path = self.checked_device_file("alternate_modes")
        if not path:
            return None
        with open(path, "r") as file:
            data = file.read()

        mode_id = None
        lines = data.splitlines()
        reg = re.compile("([^:]+): (.*)")
        for line in lines:
            matches = reg.match(line)
            mode_id = matches.group(1)
            if mode_id == "native":
                continue
            name = matches.group(2)
            if name.endswith("*"):
                return mode_id
        return mode_id

    def set_mode(self, emulation_mode):
        path = self.checked_device_file("alternate_modes")
        if not path:
            return False
        old_mode = self.get_mode()
        if old_mode == emulation_mode:
            return True
        self.disable()
        logging.debug("Setting mode: %s", str(emulation_mode))
        with open(path, "w") as file:
            file.write(emulation_mode)
        # Wait for device ready
        for i in range(10):
            if self.is_ready():
                return True
            time.sleep(1)
        return False

    def get_range(self):
        path = self.checked_device_file("range")
        if not path:
            return None
        with open(path, "r") as file:
            data = file.read()
        wrange = data.strip()
        return int(wrange)

    def set_range(self, wrange):
        path = self.checked_device_file("range")
        if not path:
            return False
        wrange = str(wrange)
        logging.debug("Setting range: %s", wrange)
        with open(path, "w") as file:
            file.write(wrange)
        return True

    def get_sensitivity(self):
        path = self.checked_device_file("sensitivity")
        if not path:
            return None
        with open(path, "r") as file:
            data = file.read()
        return int(data.strip())

    def set_sensitivity(self, sensitivity):
        path = self.checked_device_file("sensitivity")
        if not path:
            return False
        sensitivity = str(int(sensitivity))
        logging.debug("Setting sensitivity: %s", sensitivity)
        with open(path, "w") as file:
            file.write(sensitivity)
        return True

    def _get_flag(self, filename):
        path = self.checked_device_file(filename)
        if not path:
            return None
        with open(path, "r") as file:
            return int(file.read().strip()) != 0

    def _set_flag(self, filename, value):
        path = self.checked_device_file(filename)
        if not path:
            return False
        logging.debug("Setting %s: %s", filename, value)
        with open(path, "w") as file:
            file.write("1" if value else "0")
        return True

    def get_autocenter_persistent(self):
        return self._get_flag("autocenter_persistent")

    def set_autocenter_persistent(self, value):
        return self._set_flag("autocenter_persistent", value)

    def get_inertia_mode(self):
        return self._get_flag("inertia_mode")

    def set_inertia_mode(self, value):
        return self._set_flag("inertia_mode", value)

    def get_app_gain(self):
        return self._get_flag("app_gain")

    def set_app_gain(self, value):
        return self._set_flag("app_gain", value)

    def get_invert_pedals(self):
        path = self.checked_device_file("invert_pedals")
        if not path:
            return None
        with open(path, "r") as file:
            return int(file.read().strip())

    def set_invert_pedals(self, mask):
        path = self.checked_device_file("invert_pedals")
        if not path:
            return False
        logging.debug("Setting invert_pedals: %s", mask)
        with open(path, "w") as file:
            file.write(str(int(mask)))
        return True

    def get_combine_pedals(self):
        path = self.checked_device_file("combine_pedals")
        if not path:
            return None
        with open(path, "r") as file:
            data = file.read()
        combine_pedals = data.strip()
        return int(combine_pedals)

    def set_combine_pedals(self, combine_pedals):
        path = self.checked_device_file("combine_pedals")
        if not path:
            return False
        combine_pedals = str(combine_pedals)
        logging.debug("Setting combined pedals: %s", combine_pedals)
        with open(path, "w") as file:
            file.write(combine_pedals)
        return True

    def get_autocenter(self):
        path = self.checked_device_file("autocenter")
        if not path:
            capabilities = self.get_capabilities()
            if ecodes.EV_FF in capabilities and ecodes.FF_AUTOCENTER in capabilities[ecodes.EV_FF]:
                return 0
            else:
                return None
        with open(path, "r") as file:
            data = file.read()
        autocenter = data.strip()
        return int(round((int(autocenter) * 100) / 65535))

    def set_autocenter(self, autocenter):
        if autocenter > 100:
            autocenter = 100
        autocenter = str(int(autocenter / 100.0 * 65535))
        logging.debug("Setting autocenter strength: %s", autocenter)
        path = self.checked_device_file("autocenter")
        if path:
            with open(path, "w") as file:
                file.write(autocenter)
        else:
            input_device = self.get_input_device()
            input_device.write(ecodes.EV_FF, ecodes.FF_AUTOCENTER, int(autocenter))
        return True

    def get_ff_gain(self):
        path = self.checked_device_file("gain")
        if not path:
            capabilities = self.get_capabilities()
            if ecodes.EV_FF in capabilities and ecodes.FF_GAIN in capabilities[ecodes.EV_FF]:
                return 100
            else:
                return None
        with open(path, "r") as file:
            data = file.read()
        gain = int(data.strip())
        return int(round((int(gain) * 100) / 65535))

    def get_max_ff_gain(self):
        if self.checked_device_file("gain") and self.checked_device_file("app_gain"):
            return 150
        return 100

    def set_ff_gain(self, gain):
        path = self.checked_device_file("gain")
        # Only new-lg4ff >= 0.6 (which also has app_gain) accepts up to 150 %
        gain = min(int(gain), self.get_max_ff_gain())
        gain = str(int(gain / 100.0 * 65535))
        logging.debug("Setting FF gain: %s", gain)
        if path:
            with open(path, "w") as file:
                file.write(gain)
        else:
            input_device = self.get_input_device()
            input_device.write(ecodes.EV_FF, ecodes.FF_GAIN, int(gain))

    def get_spring_level(self):
        path = self.checked_device_file("spring_level")
        if not path:
            return None
        with open(path, "r") as file:
            data = file.read()
        spring_level = data.strip()
        return int(spring_level)

    def set_spring_level(self, level):
        path = self.checked_device_file("spring_level")
        if not path:
            return False
        level = str(level)
        logging.debug("Setting spring level: %s", level)
        with open(path, "w") as file:
            file.write(level)
        return True

    def get_damper_level(self):
        path = self.checked_device_file("damper_level")
        if not path:
            return None
        with open(path, "r") as file:
            data = file.read()
        damper_level = data.strip()
        return int(damper_level)

    def set_damper_level(self, level):
        path = self.checked_device_file("damper_level")
        if not path:
            return False
        level = str(level)
        logging.debug("Setting damper level: %s", level)
        with open(path, "w") as file:
            file.write(level)
        return True

    def get_friction_level(self):
        path = self.checked_device_file("friction_level")
        if not path:
            return None
        with open(path, "r") as file:
            data = file.read()
        friction_level = data.strip()
        return int(friction_level)

    def set_friction_level(self, level):
        path = self.checked_device_file("friction_level")
        if not path:
            return False
        level = str(level)
        logging.debug("Setting friction level: %s", level)
        with open(path, "w") as file:
            file.write(level)
        return True

    def get_rumble_level(self):
        path = self.checked_device_file("rumble_level")
        if not path:
            return None
        with open(path, "r") as file:
            return int(file.read().strip())

    def set_rumble_level(self, level):
        path = self.checked_device_file("rumble_level")
        if not path:
            return False
        logging.debug("Setting rumble level: %s", level)
        with open(path, "w") as file:
            file.write(str(int(level)))
        return True

    def get_ffb_leds(self):
        path = self.checked_device_file("ffb_leds")
        if not path:
            return None
        with open(path, "r") as file:
            data = file.read()
        ffb_leds = data.strip()
        return int(ffb_leds)

    def set_ffb_leds(self, ffb_leds):
        path = self.checked_device_file("ffb_leds")
        if not path:
            return False
        ffb_leds = str(ffb_leds)
        logging.debug("Setting FF leds: %s", ffb_leds)
        with open(path, "w") as file:
            file.write(ffb_leds)
        return True

    def get_peak_ffb_level(self):
        path = self.checked_device_file("peak_ffb_level")
        if not path:
            return None
        with open(path, "r") as file:
            data = file.read()
        peak_ffb_level = data.strip()
        return int(peak_ffb_level)

    def set_peak_ffb_level(self, peak_ffb_level):
        path = self.checked_device_file("peak_ffb_level")
        if not path:
            return False
        peak_ffb_level = str(peak_ffb_level)
        logging.debug("Setting peak FF level: %s", peak_ffb_level)
        with open(path, "w") as file:
            file.write(peak_ffb_level)
        return True

    def center_wheel(self):
        self.set_autocenter(100)
        time.sleep(1)
        self.set_autocenter(0)

    def check_permissions(self):
        logging.debug("check_permissions: %s", self.dev_path)
        if not os.access(self.dev_path, os.F_OK | os.R_OK | os.X_OK):
            return False
        if not self.check_file_permissions('alternate_modes'):
            return False
        if not self.check_file_permissions('range'):
            return False
        if not self.check_file_permissions('combine_pedals'):
            return False
        if not self.check_file_permissions('gain'):
            return False
        if not self.check_file_permissions('autocenter'):
            return False
        if not self.check_file_permissions('spring_level'):
            return False
        if not self.check_file_permissions('damper_level'):
            return False
        if not self.check_file_permissions('friction_level'):
            return False
        if not self.check_file_permissions('ffb_leds'):
            return False
        if not self.check_file_permissions('peak_ffb_level'):
            return False
        for name in ('sensitivity', 'invert_pedals', 'app_gain', 'autocenter_persistent', 'inertia_mode'):
            if not self.check_file_permissions(name):
                return False
        return True

    def has_rev_leds(self):
        from .telemetry import RevLeds
        return RevLeds(self.dev_path).available() if self.dev_path else False

    def rev_leds(self):
        from .telemetry import RevLeds
        return RevLeds(self.dev_path)

    def driver_info(self):
        """(driver name, version, has new-lg4ff features) for the status line."""
        name = None
        try:
            name = os.path.basename(os.readlink(os.path.join(self.dev_path, 'driver')))
        except OSError:
            pass
        # Both drivers register as "logitech"; the module link on the driver
        # tells which one is bound, and its version file which build it is.
        version = None
        try:
            module = os.path.basename(os.readlink(os.path.join(self.dev_path, 'driver', 'module')))
            with open(os.path.join('/sys/module', module, 'version')) as f:
                version = f.read().strip()
        except OSError:
            pass
        new_lg4ff = os.path.exists(self.device_file('sensitivity'))
        return name, version, new_lg4ff

    def play_demo(self, kind, level=100, seconds=2.0, on_error=None):
        """Play one effect type on the wheel for a moment so the user can feel
        it: 'constant' (a steady push), 'spring', 'damper', 'friction',
        'inertia', 'rumble'. Runs in a thread; returns False if the device
        has no force feedback. `on_error(exception)` is called from the
        thread if the effect could not be uploaded or played."""
        dev = self.get_input_device()
        if dev is None or ecodes.EV_FF not in dev.capabilities():
            return False
        strength = max(0, min(100, int(level)))
        ms = int(seconds * 1000)
        if kind == 'constant':
            effect = ff.Effect(ecodes.FF_CONSTANT, -1, 0x4000, ff.Trigger(0, 0), ff.Replay(ms, 0),
                               ff.EffectType(ff_constant_effect=ff.Constant(level=int(0x7fff * strength / 100 * 0.35))))
        elif kind == 'rumble':
            effect = ff.Effect(ecodes.FF_RUMBLE, -1, 0, ff.Trigger(0, 0), ff.Replay(ms, 0),
                               ff.EffectType(ff_rumble_effect=ff.Rumble(strong_magnitude=0xffff, weak_magnitude=0x8000)))
        else:
            types = {'spring': ecodes.FF_SPRING, 'damper': ecodes.FF_DAMPER, 'friction': ecodes.FF_FRICTION, 'inertia': ecodes.FF_INERTIA}
            if kind not in types:
                return False
            cond = ff.Condition(right_saturation=0xffff, left_saturation=0xffff, right_coeff=0x7fff, left_coeff=0x7fff,
                                deadband=0, center=0)
            effect = ff.Effect(types[kind], -1, 0x4000, ff.Trigger(0, 0), ff.Replay(ms, 0),
                               ff.EffectType(ff_condition_effect=(cond, cond)))

        def run():
            try:
                effect_id = dev.upload_effect(effect)
                dev.write(ecodes.EV_FF, effect_id, 1)
                time.sleep(seconds + 0.2)
                try:
                    dev.erase_effect(effect_id)
                except OSError:
                    # The device was re-opened (or went away) while the
                    # effect played; the kernel frees the slot with the fd.
                    pass
            except OSError as e:
                logging.warning("demo effect %s: %s", kind, e)
                if on_error is not None:
                    on_error(e)
        threading.Thread(target=run, daemon=True).start()
        return True

    def get_last_axis_value(self, axis):
        return self.last_axis_value[axis]

    def _proxied_node(self):
        """The virtual device standing in for this wheel while a proxy
        holds it (the real node is root-only then), or None."""
        try:
            from .proxy.manager import ProxyManager
            status = ProxyManager.read_status() or {}
        except Exception:
            return None
        for proxy in status.get('proxies', []):
            if self.dev_name in proxy.get('sources', {}).values() and proxy.get('devnode'):
                return proxy['devnode']
        return None

    def _input_node(self):
        """The node to read this wheel from: its own, or the virtual device
        of the proxy holding it."""
        if os.access(self.dev_name, os.R_OK):
            return self.dev_name
        node = self._proxied_node()
        return node if node and os.access(node, os.R_OK) else None

    def _input_device_stale(self):
        """True when the open device is gone or is no longer the node we
        should be reading. A proxy that restarts destroys its virtual
        device and creates a new one under the same name, which leaves us
        holding a deleted node that never reports anything again."""
        dev = self.input_device
        if dev is None or dev.fd == -1:
            return True
        node = self._input_node()
        if node is None or node != dev.path:
            return node is not None
        try:
            return os.stat(node).st_ino != os.fstat(dev.fd).st_ino
        except OSError:
            return True

    def get_input_device(self):
        with self._device_lock:
            return self._get_input_device_locked()

    def _get_input_device_locked(self):
        if self._input_device_stale():
            if self.input_device is not None:
                try:
                    self.input_device.close()
                except OSError:
                    pass
                self.input_device = None
            node = self._input_node()
            if node is not None:
                if node != self.dev_name:
                    logging.debug("reading %s through its proxy %s", self.dev_name, node)
                self.input_device = InputDevice(node)
        return self.input_device

    # invert_pedals bits, by the *raw* axis the driver sees
    PEDAL_BITS = {ecodes.ABS_Y: 1, ecodes.ABS_Z: 2, ecodes.ABS_RZ: 4}
    PEDALS = (ecodes.ABS_Y, ecodes.ABS_Z, ecodes.ABS_RZ)   # as Oversteer sees them: clutch, accelerator, brakes

    def _axis_now(self, device, code, fallback):
        """An axis's current value. capabilities(absinfo=True) answers from
        the snapshot taken when the device was opened, so a reading that
        has to be current asks the kernel again."""
        try:
            return device.absinfo(code).value
        except (AttributeError, OSError):
            return fallback

    def _normalized_axis(self, code, value):
        """An axis reading as normalize_event will deliver it (code and
        value): several wheels report their pedals on other axes, or the
        other way round."""
        event = self.normalize_event(InputEvent(0, 0, ecodes.EV_ABS, code, value))
        return event.code, event.value

    def pedal_axes(self, mask=None):
        """{code: (released, pressed, bit)} for the pedals this device has,
        keyed and valued the way events arrive (after normalize_event).
        `released`/`pressed` are the readings at the ends of the travel, so
        a reading becomes a pedal position; `bit` is the invert_pedals bit
        that flips that pedal, or None when the driver can't."""
        try:
            device = self.get_input_device()
            if device is None:
                return {}
            axes = dict(device.capabilities(absinfo=True).get(ecodes.EV_ABS, []))
        except OSError as e:
            logging.debug("pedal axes: %s", e)
            return {}
        if mask is None:
            mask = self.get_invert_pedals() or 0
        pedals = {}
        for code, info in axes.items():
            if info.max <= info.min:
                continue
            bit = self.PEDAL_BITS.get(code)
            # Logitech pedals rest at the top of the axis unless the driver
            # is inverting them; normalisation may flip that again.
            rest_raw, pressed_raw = (info.min, info.max) if bit and (mask & bit) else (info.max, info.min)
            norm_code, released = self._normalized_axis(code, rest_raw)
            if norm_code not in self.PEDALS or norm_code in pedals:
                continue
            _, pressed = self._normalized_axis(code, pressed_raw)
            if released != pressed:
                pedals[norm_code] = (released, pressed, bit)
        return pedals

    def pedal_values(self):
        """{code: value} of the pedals right now, as events deliver them."""
        try:
            device = self.get_input_device()
            if device is None:
                return {}
            axes = dict(device.capabilities(absinfo=True).get(ecodes.EV_ABS, []))
        except OSError:
            return {}
        values = {}
        for code, info in axes.items():
            norm_code, value = self._normalized_axis(code, self._axis_now(device, code, info.value))
            if norm_code in self.PEDALS and norm_code not in values:
                values[norm_code] = value
        return values

    def axis_value(self, code):
        """The current reading of one axis, or None."""
        try:
            device = self.get_input_device()
            if device is None:
                return None
            return device.absinfo(code).value
        except (AttributeError, OSError):
            return None

    def suggested_invert_pedals(self):
        """The invert_pedals mask that leaves every pedal reading 0 when
        released, which is what games expect. Only offered on a pristine
        setting (mask 0, as the driver comes up): anything else is somebody
        else's decision. None when the driver can't invert."""
        mask = self.get_invert_pedals()
        if mask is None or mask != 0:
            return None
        try:
            device = self.get_input_device()
            if device is None:
                return None
            axes = dict(device.capabilities(absinfo=True).get(ecodes.EV_ABS, []))
        except OSError:
            return None
        # Every pedal the driver can invert: these wheels all report a
        # released pedal at the top of its travel, and reading the current
        # position instead would get it wrong for a pedal held down now.
        for code, bit in self.PEDAL_BITS.items():
            info = axes.get(code)
            if info is not None and info.max > info.min:
                mask |= bit
        return mask

    def _proxy_handbrake_axis(self):
        """The axis a proxy maps a handbrake onto, from its spec: which
        spare axis that is depends on what else was folded in."""
        try:
            from .proxy.manager import (ProxyManager, load_specs, BUILTIN_DIR,
                                        system_dir_readable, user_dir)
            status = ProxyManager.read_status() or {}
            proxy_id = next((p['id'] for p in status.get('proxies', [])
                             if self.dev_name in p.get('sources', {}).values()), None)
            if proxy_id is None:
                return None
            specs, _ = load_specs([BUILTIN_DIR, system_dir_readable(), user_dir()])
            spec = specs.get(proxy_id)
            if spec is None:
                return None
            for mapping in spec.mappings:
                if str(mapping.source).startswith('handbrake') and mapping.to_type == ecodes.EV_ABS:
                    return mapping.to_code
        except Exception as e:
            logging.debug("proxy handbrake axis: %s", e)
        return None

    def _axis_is_its_own(self, code):
        """False when normalize_event turns this axis into one of the
        wheel's own controls: the T150, TMX and T248 report their clutch
        on ABS_THROTTLE, which is not a handbrake."""
        try:
            return self.normalize_event(InputEvent(0, 0, ecodes.EV_ABS, code, 0)).code == code
        except Exception:
            return True

    def handbrake_axis(self):
        """(code, min, max, inverted) of this device's handbrake axis, or
        None. A proxy presenting this wheel says which axis it put the
        handbrake on; otherwise the axes a handbrake reports natively are
        probed, skipping any the wheel uses for something else."""
        try:
            device = self.get_input_device()
            if device is None:
                return None
            axes = dict(device.capabilities(absinfo=True).get(ecodes.EV_ABS, []))
        except OSError as e:
            logging.debug("handbrake axis: %s", e)
            return None
        proxied = self._proxy_handbrake_axis()
        if proxied is not None:
            candidates = [proxied]
        else:
            candidates = [c for c in (ecodes.ABS_THROTTLE, ecodes.ABS_RUDDER) if self._axis_is_its_own(c)]
        for code in candidates:
            info = axes.get(code)
            if info is not None and info.max > info.min:
                # A handbrake resting at the top of its travel reads
                # backwards; the proxy levels this out with 'invert': auto,
                # a natively read one does not.
                value = self._axis_now(device, code, info.value)
                inverted = value >= info.min + (info.max - info.min) * 0.75
                return (code, info.min, info.max, inverted)
        return None

    def get_capabilities(self):
        return self.get_input_device().capabilities()

    def read_events(self, timeout):
        input_device = self.get_input_device()
        if input_device is None or input_device.fd == -1:
            # Nothing to read from (hidden wheel, proxy restarting): wait
            # the timeout out instead of spinning.
            time.sleep(timeout)
        else:
            try:
                r, _, _ = select.select({input_device.fd: input_device}, [], [], timeout)
                if input_device.fd in r:
                    for event in input_device.read():
                        event = self.normalize_event(event)
                        if event.type == ecodes.EV_ABS:
                            self.last_axis_value[ecodes.ABS_X] = event.value
                        yield event
            except OSError as e:
                # The device went away (unplugged, or a proxy restarted):
                # drop it so the next read re-opens whatever is there now.
                logging.debug("input device %s: %s", input_device.path, e)
                with self._device_lock:
                    if self.input_device is input_device:
                        try:
                            input_device.close()
                        except OSError:
                            pass
                        self.input_device = None

    def normalize_event(self, event):
        #
        # Oversteer expects axes as follows:
        #
        # - Steering wheel direction: ABS_X [0, 65535]
        # - Throttle: ABS_Z [0, 255]
        # - Brakes: ABS_RZ [0, 255]
        # - Clutch: ABS_Y [0, 255]
        # - Hat X: ABS_HAT0X [-1, 1]
        # - Hat Y: ABS_HAT0Y [-1, 1]
        #

        if event.type == ecodes.EV_KEY:
            if self.usb_id in [wid.LG_WFF]:
                if event.code in [ecodes.BTN_GEAR_DOWN, ecodes.BTN_GEAR_UP]:
                    event.code = event.code - ecodes.BTN_GEAR_DOWN + ecodes.BTN_TRIGGER

        if event.type != ecodes.EV_ABS:
            return event

        if self.usb_id in [wid.LG_WFF]:
            if event.code == ecodes.ABS_WHEEL:
                event.code = ecodes.ABS_X
                event.value = (event.value + 2048) * 16
            elif event.code == ecodes.ABS_GAS:
                event.code = ecodes.ABS_Z
            elif event.code == ecodes.ABS_BRAKE:
                event.code = ecodes.ABS_RZ
        if event.code == ecodes.ABS_X:
            if self.usb_id in [wid.LG_WFG, wid.LG_WFFG]:
                event.value = event.value * 64
            elif self.usb_id in [wid.LG_SFW, wid.LG_MOMO, wid.LG_MOMO2, wid.LG_DF, wid.LG_DFP, wid.LG_DFGT, wid.LG_G25, wid.LG_G27]:
                event.value = event.value * 4
            elif self.vendor_id == wid.VENDOR_CAMMUS:
                event.value = event.value + 32768
            elif self.usb_id in [wid.TM_T80H]:
                event.value = event.value * 257
        elif self.usb_id in [wid.LG_WFG, wid.LG_WFFG, wid.LG_SFW, wid.LG_MOMO, wid.LG_MOMO2, wid.LG_DF, wid.LG_DFP,
                wid.LG_DFGT, wid.LG_G920]:
            if event.code == ecodes.ABS_Y:
                event.code = ecodes.ABS_Z
            elif event.code == ecodes.ABS_Z:
                event.code = ecodes.ABS_RZ
            elif event.code == ecodes.ABS_RZ:
                event.code = ecodes.ABS_Y
        elif self.usb_id in [wid.TM_T248, wid.TM_T150, wid.TM_TMX]:
            if event.code == ecodes.ABS_RZ:
                event.code = ecodes.ABS_Z
            elif event.code == ecodes.ABS_Y:
                event.code = ecodes.ABS_RZ
            elif event.code == ecodes.ABS_THROTTLE:
                event.code = ecodes.ABS_Y
        elif self.usb_id in [wid.TM_T80H]:
            if event.code == ecodes.ABS_Y:
                event.code = ecodes.ABS_Z
            elif event.code == ecodes.ABS_Z:
                event.code = ecodes.ABS_RZ
        elif self.vendor_id == wid.VENDOR_FANATEC and event.code in [ecodes.ABS_Y, ecodes.ABS_Z, ecodes.ABS_RZ]:
            event.value = int(event.value + 32768 / 257)
        elif self.usb_id in [wid.LG_GPRO_PS, wid.LG_GPRO_XBOX]:
            if event.code in [ecodes.ABS_RX, ecodes.ABS_RY, ecodes.ABS_RZ]:
                event.value = int(255 - event.value / 257)
                if event.code == ecodes.ABS_RX:
                    event.code = ecodes.ABS_Z
                elif event.code == ecodes.ABS_RY:
                    event.code = ecodes.ABS_RZ
                elif event.code == ecodes.ABS_RZ:
                    event.code = ecodes.ABS_Y
        elif self.usb_id == wid.LG_G923X:
            if event.code == ecodes.ABS_Y:
                event.code = ecodes.ABS_Z
            elif event.code == ecodes.ABS_RZ:
                event.code = ecodes.ABS_Y
            elif event.code == ecodes.ABS_Z:
                event.code = ecodes.ABS_RZ

        return event
