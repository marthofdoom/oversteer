import configparser
import csv
from datetime import datetime
from evdev import ecodes
import glob
import locale as Locale
from locale import gettext as _
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from threading import Thread
import time
from xdg.BaseDirectory import save_config_path
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import GLib
from .gtk_ui import GtkUi
from . import hotkeys
from .model import Model
from .test import Test
from .combined_chart import CombinedChart
from .linear_chart import LinearChart
from .performance_chart import PerformanceChart

class Gui:

    button_labels = [
        _("Press toggle button/s"),
        _("Press button to set 270°"),
        _("Press button to set 360°"),
        _("Press button to set 540°"),
        _("Press button to set 900°"),
        _("Press button for +10°"),
        _("Press button for -10°"),
        _("Press button for +90°"),
        _("Press button for -90°"),
    ]

    languages = [
        ('', _('System default')),
        ('en_US', _('English')),
        ('gl_ES', _('Galician')),
        ('ru_RU', _('Russian')),
        ('es_ES', _('Spanish')),
        ('ca_ES', _('Valencian')),
        ('fi_FI', _('Finnish')),
        ('tr_TR', _('Turkish')),
        ('de_DE', _('German')),
        ('pl_PL', _('Polish')),
        ('hu_HU', _('Hungarian')),
    ]

    def __init__(self, application, model, argv):
        self.app = application
        self.locale = ''
        self.check_permissions = True
        self.model = model
        self.device_manager = self.app.device_manager
        self.device = None
        self.grab_input = False
        self.test = None
        self.linear_chart = None
        self.performance_chart = None
        self.combined_chart = None
        self.button_setup_step = False
        self.equipment = []
        self.telemetry = None
        self.telemetry_generation = 0
        # Pedals as pressed fractions for the rev lights' launch mode; the
        # input thread writes, the telemetry thread reads (plain floats).
        # shift_press: (monotonic time, 'gear'/'sequential'/'paddle') of the
        # last press that could change gear, for how each change was made.
        self.launch_inputs = {'clutch': None, 'throttle': None, 'handbrake': None, 'shift_press': None}
        self.shift_buttons = {}           # evdev key code -> 'gear', 'sequential' or 'paddle'
        self.telemetry_status = lambda: None
        from .shift_learner import ShiftLearner
        self.shift_learner = ShiftLearner()
        self.telemetry_car_selected = None      # a saved car picked in the Telemetry tab; None = the live one
        self.telemetry_car_live = None
        self.telemetry_tab_shown = 0            # counts the times the tab was opened: history is re-read then
        self._methods_cache = None
        self.handbrake_axis = None
        self.handbrake_invert = None
        self.pedal_axes = {}
        self.pedals_defaulted = set()     # device ids whose pedals we already straightened
        self.combine_busy = False
        self.combine_timer = None
        self.button_config = [-1] * 9
        self.button_config[0] = [-1]
        self.pressed_button_count = 0
        self.hotkey_capture = None
        self.keyboard_hotkeys = None
        self.keyboard_needs_bind = False
        self.keyboard_session_failed = False
        self.global_hotkeys = {}          # hotkeys.GLOBAL_ACTIONS bindings, from the preferences

        signal.signal(signal.SIGINT, self.sig_int_handler)

        self.config_path = save_config_path('oversteer')

        self.load_preferences()

        if not os.path.isdir(self.app.profile_path):
            os.makedirs(self.app.profile_path, 0o700)

        self.ui = GtkUi(self, argv)
        self.ui.set_app_version(self.app.version)
        self.ui.set_app_icon(os.path.join(self.app.icondir, 'io.github.berarma.Oversteer.svg'))
        self.ui.set_languages(self.languages)

        self.ui.set_language(self.locale)
        self.ui.set_check_permissions(self.check_permissions)

        self.models = {}

        self.ui.start()

        self.model.set_ui(self.ui)

        self.start_keyboard_hotkeys()

        self.populate_window()

        if self.app.args.profile is not None:
            self.ui.set_profile(self.app.args.profile)

        start_manually = self.app.args.start_manually
        if start_manually is None:
            start_manually = self.model.get_start_app_manually()

        if not model.device:
            self.ui.info_dialog("No device available.")
            start_manually = True

        if self.app.args.command:
            if start_manually:
                self.ui.enable_start_app()
            else:
                self.start_app()

        Thread(target=self.input_thread, daemon = True).start()
        GLib.timeout_add(1000, self.refresh_telemetry_view)

        self.ui.main()

    def start_app(self):
        self.ui.disable_start_app()
        Thread(target=self.run_command).start() 

    def sig_int_handler(self, signal, frame):
        sys.exit(0)

    def install_udev_files(self):
        while True:
            affirmative = self.ui.confirmation_dialog(_("You don't have the " +
                "required permissions to change your wheel settings.") + "\n\n" + _("You can " +
                "fix it yourself by copying the files in {} to the {} directory " +
                "and rebooting.").format(self.app.udev_path, self.app.target_dir) + "\n\n" +
                _("Do you want us to make this change for you?"))
            if affirmative:
                copy_cmd = 'cp -f ' + self.app.udev_path + '* ' + self.app.target_dir + ' && '
                return_code = subprocess.call([
                    'pkexec',
                    '/bin/sh',
                    '-c',
                    copy_cmd +
                    'udevadm control --reload-rules && udevadm trigger',
                ])
                if return_code == 0:
                    self.ui.info_dialog(_("Permissions rules installed."),
                            _("In some cases, a system restart might be needed."))
                    break
                answer = self.ui.confirmation_dialog(_("Error installing " +
                    "permissions rules. Please, try again and make sure you " +
                    "use the right password for the administrator user."))
                if not answer:
                    break
            else:
                break

    # --- Devices tab: equipment list and the combined virtual wheel ---

    COMBINED_ID = 'combined-wheel'

    def _combined_spec_path(self):
        from .proxy.manager import user_dir
        return os.path.join(user_dir(), self.COMBINED_ID + '.json')

    def _load_combined_spec(self):
        from .proxy.spec import ProxySpec, SpecError
        path = self._combined_spec_path()
        if not os.path.exists(path):
            return None
        try:
            return ProxySpec.load(path)
        except SpecError as e:
            logging.warning("combined spec: %s", e)
            return None

    def refresh_equipment(self):
        from .proxy.equipment import list_equipment, COMBINE_DEFAULT, KIND_WHEEL
        from .proxy.manager import ProxyManager
        spec = self._load_combined_spec()
        wanted = None
        if spec is not None:
            wanted = {(src.vendor, src.product) for src in spec.sources.values()}
            import glob
            from .proxy.spec import ProxySpec, SpecError
            for extra in glob.glob(os.path.join(os.path.dirname(self._combined_spec_path()), self.COMBINED_ID + '-*.json')):
                try:
                    wanted |= {(src.vendor, src.product) for src in ProxySpec.load(extra).sources.values()}
                except SpecError:
                    pass
        status = ProxyManager.read_status() or {}
        running = next((p for p in status.get('proxies', []) if p['id'] == self.COMBINED_ID), None)
        attached = set()
        for p in status.get('proxies', []):
            if p['id'] == self.COMBINED_ID or p['id'].startswith(self.COMBINED_ID + '-'):
                attached |= {node for node in p.get('sources', {}).values() if node}
        rows = []
        # Keep the user's ticks when the same devices are still there; a
        # periodic refresh must not undo what they were about to combine.
        previous = self.ui.get_equipment_includes()
        self.equipment = list_equipment()
        wheel_seen = False
        for eq in self.equipment:
            if eq.sys_path in previous:
                include = previous[eq.sys_path]
            elif wanted is not None:
                include = (eq.vendor, eq.product) in wanted
            else:
                include = eq.kind in COMBINE_DEFAULT and (eq.kind != KIND_WHEEL or not wheel_seen)
            if eq.kind == KIND_WHEEL and include:
                wheel_seen = True
            if eq.node in attached:
                state = _("in combined device")
            elif not eq.readable:
                state = _("hidden from games")
            else:
                state = _("visible to games")
            rows.append((include, eq.kind, eq.name, eq.usb_id, state, eq.sys_path))
        self.ui.set_equipment(rows)
        from .proxy.manager import system_dir_readable
        installed = None
        try:
            from .proxy.spec import ProxySpec
            installed = ProxySpec.load(os.path.join(system_dir_readable(), self.COMBINED_ID + '.json'))
        except Exception:
            pass
        enabled = bool(installed is not None and installed.enabled)
        if running:
            text = _("Combined device {}: {}").format(running.get('devnode') or '', running.get('state'))
        elif enabled:
            text = _("Installed, but oversteer-proxy.service is not running — the combined device does not exist. "
                     "Start it here, or check why it stopped: journalctl -u oversteer-proxy")
        else:
            text = _("Off")
        self.ui.set_combine_start_visible(enabled and not running)
        if installed is not None:
            spec = installed
        from .proxy.equipment import GENERIC_IDENTITY
        generic = spec is not None and spec.identity.vendor == int(GENERIC_IDENTITY['vendor'], 16)
        self.ui.set_combine(enabled, text, generic=generic)

    def refresh_combine_status(self):
        """Periodic refresh so the Devices tab reflects the service without
        a manual Refresh."""
        if self.combine_busy:
            return True
        try:
            self.refresh_equipment()
            self.update_handbrake()
        except Exception as e:
            logging.debug("combine status: %s", e)
        return True

    def start_proxy_service(self):
        """Start oversteer-proxy.service through pkexec, off the GTK thread."""
        from .proxy import install
        if self.combine_busy:
            return
        self.combine_busy = True
        self.ui.set_combine_busy(True, _("Starting…"))

        def work():
            try:
                code = install.start_service()
            except Exception:
                logging.exception("proxy start")
                code = -1
            self.ui.safe_call(done, code)

        def done(code):
            self.combine_busy = False
            self.ui.set_combine_busy(False)
            if code not in (0, 126, 127):     # 126/127: pkexec cancelled or missing
                self.ui.error_dialog(_("Could not start oversteer-proxy.service."),
                        _("Check its log: journalctl -u oversteer-proxy"))
            GLib.timeout_add(1000, self.refresh_combine_status_once)

        Thread(target=work, daemon=True).start()

    def refresh_combine_status_once(self):
        self.refresh_combine_status()
        return False

    def equipment_changed(self):
        spec = self._load_combined_spec()
        if spec is not None and spec.enabled:
            self.set_combine(True)

    def set_combine(self, state):
        """Build the combined-device spec from the ticked equipment and
        install (or disable) it through pkexec. The privileged step runs
        off the GTK thread; the user's spec is only saved once it succeeded."""
        from .proxy.equipment import build_combined_spec, KIND_WHEEL
        from .proxy import install
        from .proxy.manager import user_dir
        from .proxy.spec import ProxySpec, SpecError
        import glob
        import tempfile
        if self.combine_busy:
            return
        included = set(self.ui.get_included_equipment())
        selected = [eq for eq in self.equipment if eq.sys_path in included]
        path = self._combined_spec_path()
        if state:
            wheel = next((eq for eq in selected if eq.kind == KIND_WHEEL), None)
            if wheel is None:
                self.ui.error_dialog(_("Tick a wheel to build the combined device around."))
                self.refresh_equipment()
                return
            others = [eq for eq in selected if eq is not wheel]
            try:
                identity = 'generic' if self.ui.get_combine_generic() else 'wheel'
                specs = [build_combined_spec(wheel, others, spec_id=self.COMBINED_ID, identity=identity)]
            except Exception as e:
                self.ui.error_dialog(_("Could not build the combined device."), str(e))
                self.refresh_equipment()
                return
        else:
            specs = []
            for old in [path] + glob.glob(os.path.join(user_dir(), self.COMBINED_ID + '-*.json')):
                if os.path.exists(old):
                    try:
                        spec = ProxySpec.load(old)
                        spec.enabled = False
                        specs.append(spec)
                    except SpecError:
                        pass
        # Candidate directory: what the installer sees; copied to the user
        # config only when the install went through. It lives under the
        # config dir, not /tmp: inside Flatpak /tmp is private to the sandbox
        # and the installer runs on the host.
        os.makedirs(user_dir(), 0o700, exist_ok=True)
        candidate = tempfile.mkdtemp(prefix='.candidate-', dir=user_dir())
        for spec in specs:
            spec.save(os.path.join(candidate, spec.id + '.json'))
        self.combine_busy = True
        self.ui.set_combine_busy(True)

        def work():
            try:
                code = install.install(spec_dirs=[candidate])
            except Exception as e:
                logging.exception("proxy install")
                code = -1
            self.ui.safe_call(self._combine_done, code, candidate, specs)

        Thread(target=work, daemon=True).start()

    def _combine_done(self, code, candidate, specs):
        from .proxy.manager import user_dir
        import glob
        import shutil
        self.combine_busy = False
        self.ui.set_combine_busy(False)
        if code == 0:
            os.makedirs(user_dir(), 0o700, exist_ok=True)
            for old in [self._combined_spec_path()] + glob.glob(os.path.join(user_dir(), self.COMBINED_ID + '-*.json')):
                if os.path.exists(old):
                    os.remove(old)
            for spec in specs:
                spec.save(os.path.join(user_dir(), spec.id + '.json'))
        elif code == 4:
            self.ui.error_dialog(_("The proxy service needs python3-evdev and python3-pyudev installed for the system Python."))
        elif code not in (126, 127):     # 126/127: the user cancelled pkexec, or it is missing
            self.ui.error_dialog(_("Installing the combined device failed."),
                    _("The administrator password is needed to hide the real devices from games and run the proxy service."))
        shutil.rmtree(candidate, ignore_errors=True)
        self.refresh_equipment()
        self.update_handbrake()

    def populate_devices(self):
        logging.debug("populate_devices")
        if self.device_manager.is_changed():
            device_list = []
            for device in self.device_manager.get_devices():
                if device.is_ready():
                    device_list.append((device.get_id(), device.name))
            self.ui.set_devices(device_list)

    def populate_profiles(self):
        profiles = []
        for profile_file in glob.iglob(os.path.join(self.app.profile_path, "*.ini")):
            profile_name = os.path.splitext(os.path.basename(profile_file))[0]
            profiles.append(profile_name)
        self.ui.set_profiles(profiles)

    def populate_window(self):
        try:
            self.refresh_equipment()
        except Exception as e:
            logging.warning("equipment: %s", e)
        if self.combine_timer is None:
            self.combine_timer = GLib.timeout_add_seconds(5, self.refresh_combine_status)
        self.populate_devices()
        self.populate_profiles()

    def change_device(self, device_id):
        self.cancel_hotkey_capture()
        if self.telemetry is not None:
            self.telemetry.stop()
            self.telemetry = None
        self.device = self.device_manager.get_device(device_id)

        if self.device is None or not self.device.is_ready():
            self.handbrake_axis = None
            self.pedal_axes = {}
            self.ui.set_handbrake_visible(False)
            return

        if not self.device.check_permissions() and self.check_permissions:
            if self.app.udev_path:
                self.install_udev_files()
            else:
                self.ui.info_dialog(_("You don't have the required permissions to change your wheel settings."))

        if not self.models:
            self.model.set_device(self.device)
            self.models[self.device.get_id()] = self.model
        if self.device.get_id() in self.models:
            self.model = self.models[self.device.get_id()]
        else:
            self.model = Model(self.device, self.ui)
            self.models[self.device.get_id()] = self.model

        self.ui.set_max_range(self.device.get_max_range())
        self.ui.set_modes(self.model.get_mode_list())
        self.update_handbrake()
        self.update_driver_status()
        self.ui.set_launch_options(self.launch_options())
        self.update_learner_directory()
        self.apply_rev_leds()

        if self.model.get_profile():
            self.ui.set_profile(self.model.get_profile())
        else:
            # No profile for this wheel: leave its pedals reading the way
            # games expect (0 released). Once per device per session, so a
            # deliberate change isn't undone by the next rescan; a profile
            # that says otherwise always wins, which is how someone keeps
            # the raw direction.
            device_id = self.device.get_id()
            if device_id not in self.pedals_defaulted and not getattr(self.app, 'invert_pedals_from_cli', False):
                self.pedals_defaulted.add(device_id)
                suggested = self.device.suggested_invert_pedals()
                if suggested is not None and suggested != self.model.get_invert_pedals():
                    logging.debug("pedals rest at the far end; inverting (mask %s)", suggested)
                    self.model.set_invert_pedals(suggested)
            self.model.flush_device()
            self.model.flush_ui()
        self.update_pedals()

    def launch_options(self):
        """The Steam launch options that run a game with the shared-memory
        telemetry bridge: the host path of oversteer-run."""
        from .proxy.manager import in_flatpak
        candidates = []
        if in_flatpak():
            # app-path is the versioned deployment; 'current/active' is the
            # stable alias of the same files, so the string survives updates.
            app_files = None
            try:
                with open('/.flatpak-info') as f:
                    for line in f:
                        if line.startswith('app-path='):
                            app_files = line.split('=', 1)[1].strip()
            except OSError:
                pass
            if app_files:
                app_files = re.sub(r'/[^/]+/[^/]+/[0-9a-f]{16,}/files$', '/current/active/files', app_files)
                candidates.append(os.path.join(app_files, 'bin', 'oversteer-run'))
        else:
            candidates.append(os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), 'oversteer-run'))
            source_root = os.environ.get('MESON_SOURCE_ROOT')
            if source_root:
                candidates.append(os.path.join(source_root, 'data', 'telemetry', 'oversteer-run'))
            candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'telemetry', 'oversteer-run'))
            candidates.append(shutil.which('oversteer-run') or '')
        for path in candidates:
            if path and (in_flatpak() or os.path.exists(path)):
                path = os.path.normpath(path)
                return '"{}" %command%'.format(path) if ' ' in path else '{} %command%'.format(path)
        return _("oversteer-run was not found: install Oversteer, or run it with scripts/run-dev.sh")

    def apply_rev_leds(self):
        """Start or stop the telemetry listener to match the model."""
        from .telemetry import Telemetry, DEFAULT_PORT, LEGACY_PORT
        if self.telemetry is not None:
            self.telemetry.stop()
            self.telemetry = None
        self.telemetry_generation += 1
        if self.device is None or not self.model.get_rev_leds():
            self.ui.set_rev_leds_status('')
            return
        leds = self.device.rev_leds()
        if not leds.available():
            self.ui.set_rev_leds_status(_("no LEDs"))
            return
        self.model.set_ffb_leds(False)        # the meter and the rev lights can't share the LEDs
        self.ui.set_ffb_leds(False)
        self.telemetry_generation += 1
        generation = self.telemetry_generation

        shown = {'source': None, 'limiter': 0.0}

        def show():
            telemetry = self.telemetry
            if telemetry is None or generation != self.telemetry_generation:
                return
            if shown['source']:
                text = _("telemetry from {}").format(shown['source'])
            elif telemetry.elsewhere:
                text = _("telemetry arrives on UDP {0}, not {1}: set the port here to {0}, or the game to {1}").format(
                    telemetry.other_port, telemetry.port)
            elif telemetry.elsewhere is False and telemetry.port == DEFAULT_PORT:
                text = _("waiting for telemetry on UDP {} (games set up for 5300 need the new port)").format(
                    telemetry.port)
            else:
                text = _("waiting for telemetry on UDP {}").format(telemetry.port)
            if shown['source'] and self.model.get_rev_leds_launch() and self.model.get_rev_leds_shift_unit() != 'rpm':
                text += '  ·  ' + (_("limiter {} rpm (from the launch)").format(int(round(shown['limiter'])))
                                   if shown['limiter'] else _("launch to learn the limiter"))
            self.ui.set_rev_leds_status(text)

        def status(source):
            shown['source'] = source
            if not source:
                shown['limiter'] = 0.0          # the listener forgets it when telemetry stops
            self.ui.safe_call(show)

        def limiter(rpm):
            shown['limiter'] = rpm
            self.ui.safe_call(show)
        self.telemetry = Telemetry(leds, self.model.get_rev_leds_port() or DEFAULT_PORT, on_status=status,
                                   inputs=lambda: self.launch_inputs, on_limiter=limiter,
                                   learner=self.shift_learner, use_learnt=self.model.get_rev_leds_learnt(),
                                   **self._shift_kwargs())
        self.telemetry_status = show
        if self.telemetry.start():
            self.ui.set_rev_leds_status(_("waiting for telemetry on UDP {}").format(self.telemetry.port))
        elif self.telemetry.port == LEGACY_PORT:
            # Forza Horizon 6 binds its own socket in 5200-5300
            self.ui.set_rev_leds_status(_("port {} in use (Forza Horizon 6 may hold it): use {}").format(
                LEGACY_PORT, DEFAULT_PORT))
            self.telemetry = None
        else:
            self.ui.set_rev_leds_status(_("port {} in use").format(self.telemetry.port))
            self.telemetry = None

    PEDAL_BOX = {ecodes.ABS_Y: 'clutch', ecodes.ABS_Z: 'accelerator', ecodes.ABS_RZ: 'brakes'}

    def update_pedals(self, mask=None):
        """Re-read which end of each pedal axis means 'released'. `mask` is
        the invert_pedals value about to be written, so events arriving
        while the driver catches up are read the new way."""
        self.pedal_axes = self.device.pedal_axes(mask) if self.device is not None else {}
        # Which bit each box flips depends on the wheel: the pedal shown as
        # the clutch is not always the axis the driver calls the clutch.
        self.ui.set_pedal_bits({name: (self.pedal_axes[code][2] if code in self.pedal_axes else None)
                                for code, name in self.PEDAL_BOX.items()})
        # The driver re-emits the axes when the setting changes and the
        # value takes a moment to come back through a proxy; redraw once it
        # has, so the bars are right without touching anything.
        GLib.timeout_add(250, self.redraw_inputs)

    def redraw_inputs(self):
        """Draw the pedal and handbrake bars from where the axes are now."""
        if self.device is None:
            return False
        setters = {ecodes.ABS_Z: self.ui.set_accelerator_input,
                   ecodes.ABS_RZ: self.ui.set_brakes_input,
                   ecodes.ABS_Y: self.ui.set_clutch_input}
        for code, value in self.device.pedal_values().items():
            axis = self.pedal_axes.get(code)
            if axis is not None and code in setters:
                setters[code](self._axis_fraction(axis, value))
        if self.handbrake_axis is not None:
            code, low, high = self.handbrake_axis
            value = self.device.axis_value(code)
            if value is not None and high > low:
                self.ui.set_handbrake_input(min(1.0, max(0.0, (value - low) / (high - low))))
        return False

    LAUNCH_PEDAL = {ecodes.ABS_Y: 'clutch', ecodes.ABS_Z: 'throttle'}

    @staticmethod
    def _pressed_fraction(axis, value):
        """How far a pedal is pressed, 0 released to 1 floored, whichever
        way its axis runs."""
        released, pressed = axis[0], axis[1]
        if pressed == released:
            return 0.0
        return min(1.0, max(0.0, (value - released) / (pressed - released)))

    @staticmethod
    def _axis_fraction(axis, value):
        """Where a reading sits in its axis, 0 at the bottom to 1 at the
        top. The bars show the axis as the game receives it, not the
        pedal's position: that is what makes a pedal reading backwards
        visible instead of hiding it behind a flipped display."""
        low, high = min(axis[0], axis[1]), max(axis[0], axis[1])
        span = high - low
        if span == 0:
            return 0.0
        return min(1.0, max(0.0, (value - low) / span))

    def _proxied_handbrake(self):
        """(proxy id, source key, from code, inverted) for the handbrake a
        proxy folds into this wheel, or None when there is no proxy: only
        then can Oversteer change the direction games see.

        The spec says what to do with the axis; 'auto' is resolved by the
        daemon at attach and reported in its status, so an older daemon
        just means the box starts unticked until it is set explicitly."""
        if self.device is None:
            return None
        try:
            from .proxy.manager import ProxyManager
            status = ProxyManager.read_status() or {}
        except Exception:
            return None
        proxy = next((p for p in status.get('proxies', [])
                      if self.device.dev_name in p.get('sources', {}).values()), None)
        if proxy is None or not any(k.startswith('handbrake') for k in proxy.get('sources', {})):
            return None
        spec = self._load_combined_spec()
        if spec is None:
            return None
        for mapping in spec.mappings:
            if not str(mapping.source).startswith('handbrake') or mapping.to_type != ecodes.EV_ABS:
                continue
            if mapping.invert in (True, False):
                inverted = bool(mapping.invert)
            else:
                reported = (proxy.get('inverted_axes') or {}).get(
                    '{}:{}'.format(mapping.source, mapping.from_code))
                inverted = bool(reported)
            return (proxy['id'], mapping.source, mapping.from_code, inverted)
        return None

    def update_handbrake(self):
        """Show the handbrake column when the selected device has one.
        Re-checked as proxies come and go: the axis lives on the virtual
        device a proxy presents, which appears, changes and disappears
        while Oversteer runs."""
        self.update_shift_buttons()
        axis = self.device.handbrake_axis() if self.device is not None else None
        proxied = self._proxied_handbrake()
        state = proxied[3] if proxied is not None else None
        if axis == self.handbrake_axis and state == self.handbrake_invert:
            return
        self.handbrake_axis = axis
        if axis is None:
            self.launch_inputs['handbrake'] = None     # none fitted: clutch and throttle make the launch
        self.handbrake_invert = state
        self.ui.set_handbrake_visible(axis is not None)
        self.ui.set_handbrake_invert(state)

    def update_shift_buttons(self):
        """Which key codes of the wheel Oversteer reads are shifter gears,
        the sequential plate or paddles: from the combined device's spec,
        re-read as proxies come and go."""
        from .proxy.equipment import shift_button_kinds
        from . import wheel_ids as wid
        spec = self._load_combined_spec()
        wheel = spec.sources.get('wheel') if spec is not None else None
        if wheel is not None:
            logitech = wheel.vendor == int(wid.VENDOR_LOGITECH, 16)
        else:
            logitech = self.device is not None and self.device.vendor_id == wid.VENDOR_LOGITECH
        self.shift_buttons = shift_button_kinds(spec, logitech)

    def set_handbrake_invert(self, state):
        """Override the direction the proxy gives the handbrake. The spec
        lives in /etc, so this reinstalls the combined device through
        pkexec, off the GTK thread like the Devices tab does."""
        from .proxy import install
        from .proxy.manager import user_dir
        import tempfile
        proxied = self._proxied_handbrake()
        spec = self._load_combined_spec()
        if proxied is None or spec is None or self.combine_busy:
            self.ui.set_handbrake_invert(self.handbrake_invert)
            return
        changed = False
        for mapping in spec.mappings:
            if mapping.source == proxied[1] and mapping.from_code == proxied[2]:
                mapping.invert = bool(state)
                changed = True
        if not changed:
            self.ui.set_handbrake_invert(self.handbrake_invert)
            return
        os.makedirs(user_dir(), 0o700, exist_ok=True)
        candidate = tempfile.mkdtemp(prefix='.candidate-', dir=user_dir())
        spec.save(os.path.join(candidate, spec.id + '.json'))
        self.combine_busy = True
        self.ui.set_combine_busy(True, _("Applying…"))

        def work():
            try:
                code = install.install(spec_dirs=[candidate])
            except Exception:
                logging.exception("handbrake invert")
                code = -1
            self.ui.safe_call(self._combine_done, code, candidate, [spec])

        Thread(target=work, daemon=True).start()

    def update_learner_directory(self):
        """Cars are learnt per Oversteer profile: a rally profile and a
        circuit one each keep their own."""
        profile = self.model.get_profile()
        name = os.path.splitext(os.path.basename(profile))[0] if profile else '_no_profile'
        if self.shift_learner.db is None:
            from xdg.BaseDirectory import save_data_path
            try:
                self.shift_learner.open(os.path.join(save_data_path('oversteer'), 'telemetry.db'))
            except Exception as e:
                logging.warning("telemetry database: %s", e)
        if name != self.shift_learner.profile:
            self.shift_learner.set_profile(name)
            self.ui.safe_call(self.refresh_telemetry_cars)

    def on_quit(self):
        self.shift_learner.save()

    # -- Telemetry tab --

    def refresh_telemetry_cars(self):
        cars = dict(self.shift_learner.known_cars())
        snapshot = self.shift_learner.snapshot()
        if snapshot is not None:
            cars[snapshot['key']] = snapshot['name']
        active = self.telemetry_car_selected or (snapshot['key'] if snapshot else None)
        if active is None and cars:
            active = sorted(cars.items(), key=lambda kv: kv[1])[0][0]
        self.ui.set_telemetry_cars(sorted(cars.items(), key=lambda kv: kv[1].lower()), active)
        return False

    def select_telemetry_car(self, key):
        live = self.shift_learner.car.key if self.shift_learner.car else None
        self.telemetry_car_selected = None if key == live else key
        self.refresh_telemetry_view()

    def rename_telemetry_car(self, key, name):
        self.shift_learner.rename(key, name)
        self.refresh_telemetry_cars()

    def forget_telemetry_car(self, key):
        self.shift_learner.forget(key)
        self._methods_cache = None
        if self.telemetry_car_selected == key:
            self.telemetry_car_selected = None
        self.refresh_telemetry_cars()
        self.refresh_telemetry_view()

    def refresh_telemetry_view(self):
        """Once a second: the live line, and the shown car's learning."""
        telemetry = self.telemetry
        sample = telemetry.live if telemetry is not None else None
        if telemetry is None:
            live = _("Turn on the rev lights to read game telemetry (and learn from it).")
        elif sample is None:
            live = _("Waiting for telemetry on UDP {}.").format(telemetry.port)
        else:
            parts = [GLib.markup_escape_text(sample.car_name or sample.car or _("unknown car"))]
            if sample.gear is not None:
                parts.append(_("gear {}").format({-1: 'R', 0: 'N'}.get(sample.gear, sample.gear)))
            parts.append('{:.0f} rpm'.format(sample.rpm))
            if sample.speed is not None:
                parts.append('{:.0f} km/h'.format(sample.speed * 3.6))
            if telemetry.using_learnt:
                parts.append(_("lights at the learnt {:.0f} rpm").format(telemetry.using_learnt))
            live = '<b>{}</b>'.format('  ·  '.join(parts))
        key = self.shift_learner.car.key if self.shift_learner.car else None
        if key != self.telemetry_car_live:
            self.telemetry_car_live = key
            self.refresh_telemetry_cars()
        if self.telemetry_car_selected is not None:
            snapshot = self.shift_learner.load_snapshot(self.telemetry_car_selected)
        else:
            snapshot = self.shift_learner.snapshot()
        if snapshot is not None:
            snapshot = dict(snapshot, methods=self._method_shifts(snapshot['key']))
        self.ui.set_telemetry_view(live, snapshot)
        return True

    def _method_shifts(self, key):
        """The car's changes up per way of changing, from the database:
        queried again only when the car or the session changes or the tab
        is shown, never just because a second went by."""
        stamp = (key, self.shift_learner.profile, self.shift_learner.sessions_ended, self.telemetry_tab_shown)
        if self._methods_cache is None or self._methods_cache[0] != stamp:
            self._methods_cache = (stamp, self.shift_learner.method_shifts(key))
        return self._methods_cache[1]

    def telemetry_tab_selected(self):
        self.telemetry_tab_shown += 1
        if getattr(self, 'ui', None) is not None:          # not while the window is being built
            self.refresh_telemetry_view()

    def _shift_kwargs(self):
        shift = self.model.get_rev_leds_shift()
        unit = self.model.get_rev_leds_shift_unit()
        if unit == 'rpm':
            return {'shift_rpm': shift or 7000}
        return {'shift': (shift or 95) / 100.0, 'launch': self.model.get_rev_leds_launch()}

    def update_rev_leds_shift(self):
        """Push a changed shift point to the running listener without
        restarting it (a restart would blink the LEDs and forget the
        learnt max RPM)."""
        if self.telemetry is None:
            return
        self.telemetry.set_shift(**self._shift_kwargs())
        self.telemetry.use_learnt = self.model.get_rev_leds_learnt()
        self.telemetry_status()

    def change_rev_leds_shift_unit(self, unit):
        """Switch the shift point between % of max RPM and an RPM figure,
        converting the value when the game has told us the max RPM."""
        value = None
        max_rpm = self.telemetry.reference_max() if self.telemetry is not None else 0.0
        current = self.model.get_rev_leds_shift()
        if max_rpm and current:
            if unit == 'rpm' and self.model.get_rev_leds_shift_unit() != 'rpm':
                value = int(round(current / 100.0 * max_rpm / 50.0) * 50)
            elif unit != 'rpm' and self.model.get_rev_leds_shift_unit() == 'rpm':
                value = int(round(current * 100.0 / max_rpm))
        self.model.set_rev_leds_shift_unit(unit, value)

    def test_rev_leds(self):
        if self.device is None:
            return
        leds = self.device.rev_leds()
        if not leds.available():
            self.ui.info_dialog(_("This wheel has no rev LEDs, or they are not accessible."))
            return
        was_meter = self.model.get_ffb_leds()
        if was_meter:
            self.device.set_ffb_leds(0)

        def run():
            leds.test()
            if was_meter:
                self.device.set_ffb_leds(1)
        Thread(target=run, daemon=True).start()

    def try_effect(self, kind):
        if self.device is None:
            return
        if not self.model.get_ffb_enabled():
            self.ui.info_dialog(_("Force feedback is switched off for this device."))
            return

        def failed(error):
            self.ui.safe_call(self.ui.info_dialog, _("Could not play the effect."), str(error))
        if not self.device.play_demo(kind, on_error=failed):
            self.ui.info_dialog(_("This device has no force feedback."))

    def reset_ffb_defaults(self):
        m = self.model
        for setter, value in ((m.set_ffb_enabled, True), (m.set_ff_gain, 100), (m.set_app_gain, True),
                              (m.set_spring_level, 30), (m.set_damper_level, 30), (m.set_friction_level, 30),
                              (m.set_rumble_level, 50), (m.set_inertia_mode, False),
                              (m.set_autocenter, 0), (m.set_autocenter_persistent, False)):
            try:
                setter(value)
            except Exception as e:
                logging.debug("reset: %s", e)
        m.flush_ui()

    def update_driver_status(self):
        if self.device is None:
            self.ui.set_driver_status('')
            return
        name, version, new_lg4ff = self.device.driver_info()
        if name is None:
            text = ''
        elif new_lg4ff:
            text = _("Driver: new-lg4ff {}").format(version or '')
        elif name == 'logitech':
            text = _("Driver: in-kernel hid-logitech — install the new-lg4ff fork for sensitivity, rumble, friction and the other Logitech features")
        else:
            text = _("Driver: {} {}").format(name, version or '')
        proxied = self.device._proxied_node()
        if proxied:
            text += _("  ·  combined device active ({})").format(proxied)
        self.ui.set_driver_status('<small>{}</small>'.format(GLib.markup_escape_text(text)))

    def load_profile(self, profile_name):
        if profile_name is None or profile_name == '':
            return

        profile_file = os.path.join(self.app.profile_path, profile_name + '.ini')
        if not os.path.exists(profile_file):
            self.ui.info_dialog(_("Error opening profile"), _("The selected profile can't be loaded."))
            return

        self.model.load(profile_file)
        self.model.flush_device()
        self.model.flush_ui()
        self.update_pedals()
        self.update_learner_directory()
        self.apply_rev_leds()

    def save_profile(self, profile_name, check_exists = False):
        if self.device is None:
            return

        if profile_name is None or profile_name == '':
            return

        profile_file = os.path.join(self.app.profile_path, profile_name + '.ini')
        if check_exists:
            if os.path.exists(profile_file):
                if not self.ui.confirmation_dialog(_("This profile already exists. Are you sure?")):
                    raise Exception()
        self.model.save(profile_file)

    def rename_profile(self, current_name, new_name):
        current_file = os.path.join(self.app.profile_path, current_name + '.ini')
        new_file = os.path.join(self.app.profile_path, new_name + '.ini')
        os.rename(current_file, new_file)

    def delete_profile(self, profile_name):
        if profile_name != '' and profile_name is not None:
            profile_file = os.path.join(self.app.profile_path, profile_name + '.ini')
            if self.ui.confirmation_dialog(_("This profile will be deleted, are you sure?")):
                os.remove(profile_file)
            else:
                raise Exception()

    def import_profile(self, path):
        if not path.endswith('.ini'):
            raise Exception(_('Invalid extension.'))
        profile_name = os.path.splitext(os.path.basename(path))[0]
        profile_file = os.path.join(self.app.profile_path, profile_name + '.ini')
        if os.path.exists(profile_file):
            raise Exception(_('A profile with that name already exists.'))
        shutil.copyfile(path, profile_file)
        return profile_name

    def export_profile(self, profile_name, path):
        profile_file = os.path.join(self.app.profile_path, profile_name + '.ini')
        if os.path.exists(path):
            if not self.ui.confirmation_dialog(_('File already exists, overwrite?')):
                raise Exception()
        shutil.copyfile(profile_file, path)

    def load_preferences(self):
        config = configparser.ConfigParser()
        config_file = os.path.join(self.config_path, 'config.ini')
        config.read(config_file)
        self.check_permissions = True
        if 'DEFAULT' in config:
            if 'locale' in config['DEFAULT'] and config['DEFAULT']['locale'] != '':
                self.locale = config['DEFAULT']['locale']
                Locale.setlocale(Locale.LC_ALL, (self.locale, 'UTF-8'))
            if 'check_permissions' in config['DEFAULT']:
                self.check_permissions = config['DEFAULT']['check_permissions'] == '1'
            if 'hotkeys' in config['DEFAULT']:
                self.global_hotkeys = {a: i for a, i in hotkeys.parse(config['DEFAULT']['hotkeys']).items()
                                       if a in hotkeys.GLOBAL_ACTIONS}
            if 'button_config' in config['DEFAULT'] and config['DEFAULT']['button_config'] != '':
                if 'button_toggle' not in config['DEFAULT']:
                    self.button_config = list(map(int, config['DEFAULT']['button_config'].split(',')))
                    self.button_config[0] = [self.button_config[0]]
                    self.save_preferences()
                else:
                    self.button_config[0] = list(map(int, config['DEFAULT']['button_toggle'].split(',')))
                    self.button_config[1:] = list(map(int, config['DEFAULT']['button_config'].split(',')))

    def set_locale(self, locale):
        if locale is None:
            locale = ''
        if locale != '':
            try:
                Locale.setlocale(Locale.LC_ALL, (locale, 'UTF-8'))
                self.locale = locale
            except Locale.Error:
                self.ui.info_dialog(_("Failed to change language"),
                _("Make sure locale '{}.UTF8' is generated on your system.").format(str(locale)))
                self.ui.set_language(self.locale)
        self.save_preferences()

    def set_check_permissions(self, check_permissions):
        self.check_permissions = check_permissions
        self.save_preferences()

    def save_preferences(self):
        config = configparser.ConfigParser()
        config['DEFAULT'] = {
            'locale': self.locale,
            'check_permissions': '1' if self.check_permissions else '0',
            'button_toggle': ','.join(map(str, self.button_config[0])),
            'button_config': ','.join(map(str, self.button_config[1:])),
            'hotkeys': hotkeys.serialize(self.global_hotkeys),
        }
        config_file = os.path.join(self.config_path, 'config.ini')
        with open(config_file, 'w') as file:
            config.write(file)

    def stop_button_setup(self):
        self.button_setup_step = False
        self.pressed_button_count = 0
        self.ui.safe_call(self.ui.reset_define_buttons_text)

    def start_stop_button_setup(self):
        if self.button_setup_step is not False:
            self.stop_button_setup()
        else:
            self.button_setup_step = 0
            self.button_config = [-1] * 9
            self.pressed_button_count = 0
            self.ui.set_define_buttons_text(self.button_labels[self.button_setup_step])

    def on_close_preferences(self):
        self.stop_button_setup()

    def on_button_press(self, button, value):
        if self.button_setup_step is not False:
            if self.button_setup_step == 0:
                if button < 100:
                    if value == 1:
                        self.pressed_button_count += 1
                        if self.button_config[0] == -1:
                            self.button_config[0] = []
                        self.button_config[0].append(button)
                    else:
                        self.pressed_button_count -= 1
                        if self.pressed_button_count == 0:
                            self.button_setup_step += 1
                            self.ui.safe_call(self.ui.set_define_buttons_text, self.button_labels[self.button_setup_step])
                return
            if value == 1:
                self.button_config[self.button_setup_step] = button
                self.button_setup_step += 1
                if self.button_setup_step >= len(self.button_config):
                    self.stop_button_setup()
                    self.save_preferences()
                else:
                    self.ui.safe_call(self.ui.set_define_buttons_text, self.button_labels[self.button_setup_step])
            return

        if self.model.get_use_buttons():
            if self.grab_input and self.pressed_button_count == 0 and value == 1:
                if button == self.button_config[1]:
                    self.ui.safe_call(self.ui.set_range, 270)
                if button == self.button_config[2]:
                    self.ui.safe_call(self.ui.set_range, 360)
                if button == self.button_config[3]:
                    self.ui.safe_call(self.ui.set_range, 540)
                if button == self.button_config[4]:
                    self.ui.safe_call(self.ui.set_range, 900)
                if button == self.button_config[5]:
                    self.ui.safe_call(self.add_range, 10)
                if button == self.button_config[6]:
                    self.ui.safe_call(self.add_range, -10)
                if button == self.button_config[7]:
                    self.ui.safe_call(self.add_range, 90)
                if button == self.button_config[8]:
                    self.ui.safe_call(self.add_range, -90)
            if button in self.button_config[0]:
                if value == 1:
                    self.pressed_button_count += 1
                    if self.pressed_button_count == len(self.button_config[0]):
                        device = self.device.get_input_device()
                        if self.grab_input:
                            device.ungrab()
                            self.grab_input = False
                            self.ui.safe_call(self.ui.update_overlay, False)
                        else:
                            device.grab()
                            self.grab_input = True
                            self.ui.safe_call(self.ui.update_overlay, True)
                else:
                    self.pressed_button_count -= 1

    # -- hotkeys --

    APP_ID = 'io.github.berarma.Oversteer'

    def start_keyboard_hotkeys(self, bind=False):
        """Open a portal session and pick up the keys the desktop has for
        our actions. Plasma keeps them per app and delivers them to any
        session of ours, so there a startup only lists them: it opens its
        shortcut settings on every declaration, which belongs to a click on
        "Set keyboard keys…". GNOME keeps them per session, so there every
        start declares them (silently, once the desktop knows them all)."""
        from .global_shortcuts import GlobalShortcuts
        if self.keyboard_hotkeys is not None:
            self.keyboard_hotkeys.close()
        self.keyboard_session_failed = False
        self.keyboard_hotkeys = GlobalShortcuts(self.APP_ID, self.on_keyboard_hotkey,
                                                self.on_keyboard_hotkeys_bound, self.on_keyboard_hotkeys_failed)
        if not self.keyboard_hotkeys.start():
            self.keyboard_hotkeys = None
            self.ui.set_keyboard_triggers(None)
            return
        if bind:
            self.bind_keyboard_hotkeys()
        else:
            self.keyboard_hotkeys.list(self.on_keyboard_hotkeys_listed)

    @staticmethod
    def _desktop_keeps_shortcuts():
        return 'KDE' in os.environ.get('XDG_CURRENT_DESKTOP', '').upper()

    def on_keyboard_hotkeys_listed(self, triggers):
        # Never declared, or declared by a version with fewer actions
        missing = any(a.id not in triggers for a in hotkeys.ACTIONS)
        if missing and not self._desktop_keeps_shortcuts():
            self.bind_keyboard_hotkeys()
        else:
            self.keyboard_needs_bind = missing

    def bind_keyboard_hotkeys(self):
        """Declare every action: all of them, the desktop forgets any left out."""
        self.keyboard_needs_bind = False
        self.keyboard_hotkeys.bind([(a.id, a.description()) for a in hotkeys.ACTIONS])

    def on_keyboard_hotkeys_bound(self, triggers):
        self.ui.set_keyboard_triggers(triggers)

    def on_keyboard_hotkeys_failed(self, method):
        if method is None:
            self.keyboard_hotkeys = None
            self.ui.set_keyboard_triggers(None)
            self.ui.set_hotkeys_status(_("Keyboard keys need the desktop's shortcut portal, which isn't running"))
            return
        # Declined (a dialog dismissed) or failed: the button retries on a
        # new session
        self.keyboard_session_failed = True
        self.ui.set_keyboard_triggers({})
        self.ui.set_hotkeys_status(_("Keyboard keys aren't set up with the desktop; \"Set keyboard keys…\" tries again"))

    def configure_keyboard_hotkeys(self):
        """Show where the keys are assigned: declare them first if the
        desktop doesn't have them all, then the portal's own editor when
        it has one, otherwise the desktop's shortcut settings."""
        if self.keyboard_hotkeys is None:
            self.open_shortcut_settings()
        elif self.keyboard_session_failed:
            self.start_keyboard_hotkeys(bind=True)
        elif self.keyboard_needs_bind or self.keyboard_hotkeys.session is None:
            self.bind_keyboard_hotkeys()
        else:
            self.keyboard_hotkeys.configure(self.open_shortcut_settings)

    def open_shortcut_settings(self):
        from .proxy.manager import in_flatpak
        desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').upper()
        if 'KDE' in desktop:
            cmd = ['systemsettings', 'kcm_keys']
        elif 'GNOME' in desktop:
            cmd = ['gnome-control-center', 'keyboard']
        else:
            cmd = None
        if cmd is not None:
            if in_flatpak():
                cmd = ['flatpak-spawn', '--host'] + cmd
            try:
                proc = subprocess.Popen(cmd)
                Thread(target=proc.wait, daemon=True).start()
                return
            except OSError as e:
                logging.debug("shortcut settings: %s", e)
        self.ui.info_dialog(_("Keyboard keys"),
                            _("Assign keys to Oversteer's actions in your desktop's keyboard shortcut settings."))

    def on_keyboard_hotkey(self, action_id):
        if not self._hotkeys_suppressed():
            self.run_hotkey(action_id)

    def _hotkeys_suppressed(self):
        """Presses that belong to something else: the Preferences button
        setup, or a test that is waiting for a press or measuring. Read on
        the input thread, before on_button_press can change it."""
        if self.button_setup_step is not False:
            return True
        test = self.test
        return bool(test and (test.is_awaiting_action() or test.is_collecting_data()))

    def hotkey_bindings(self):
        """All bindings: the profile's, plus the app-wide ones."""
        bindings = {a: i for a, i in hotkeys.parse(self.model.get_hotkeys()).items()
                    if a not in hotkeys.GLOBAL_ACTIONS}
        bindings.update(self.global_hotkeys)
        return bindings

    def _store_hotkeys(self, bindings):
        global_hotkeys = {a: i for a, i in bindings.items() if a in hotkeys.GLOBAL_ACTIONS}
        if global_hotkeys != self.global_hotkeys:
            self.global_hotkeys = global_hotkeys
            self.save_preferences()
        self.model.set_hotkeys(hotkeys.serialize({a: i for a, i in bindings.items()
                                                  if a not in hotkeys.GLOBAL_ACTIONS}))
        self.ui.set_hotkeys(self.model.get_hotkeys())

    def start_hotkey_capture(self, action_id):
        self.hotkey_capture = None if self.hotkey_capture == action_id else action_id
        self.ui.set_hotkey_capture(self.hotkey_capture)

    def cancel_hotkey_capture(self):
        if self.hotkey_capture is not None:
            self.hotkey_capture = None
            self.ui.set_hotkey_capture(None)

    def clear_hotkey(self, action_id):
        bindings = self.hotkey_bindings()
        if bindings.pop(action_id, None) is not None:
            self._store_hotkeys(bindings)

    def on_wheel_hotkey(self, wheel_input, suppressed=False):
        """A wheel button went down (main thread)."""
        if self.hotkey_capture is not None:
            action_id, self.hotkey_capture = self.hotkey_capture, None
            bindings = self.hotkey_bindings()
            # One action per button: a button pressing two things is a trap
            taken = [a for a, i in bindings.items() if i == wheel_input and a != action_id]
            for other in taken:
                del bindings[other]
            bindings[action_id] = wheel_input
            self._store_hotkeys(bindings)
            self.ui.set_hotkey_capture(None)
            text = _("{} set to {}").format(hotkeys.BY_ID[action_id].label, hotkeys.input_name(wheel_input))
            if taken:
                text += ' ' + _("(taken from {})").format(', '.join(hotkeys.BY_ID[a].label for a in taken))
            if self._use_buttons_input(wheel_input):
                text += ' ' + _("— also one of the Preferences wheel buttons")
            if action_id not in hotkeys.GLOBAL_ACTIONS and not self.ui.profile_combobox.get_active_id():
                text += ' ' + _("— save a profile to keep it")
            self.ui.set_hotkeys_status(text)
            return
        if suppressed:
            return
        for action_id, bound in self.hotkey_bindings().items():
            if bound == wheel_input:
                self.run_hotkey(action_id)
                return

    def _use_buttons_input(self, wheel_input):
        """True when the upstream Preferences button setup uses this input."""
        if not self.model.get_use_buttons():
            return False
        numbers = set(self.button_config[0]) | set(self.button_config[1:])
        kind, _sep, rest = wheel_input.partition(':')
        if kind == 'btn':
            number = hotkeys.button_number(int(rest))
            return number is not None and number in numbers
        code, direction = map(int, rest.split(':'))
        hat = {(ecodes.ABS_HAT0X, -1): 100, (ecodes.ABS_HAT0X, 1): 101,
               (ecodes.ABS_HAT0Y, -1): 102, (ecodes.ABS_HAT0Y, 1): 103}.get((code, direction))
        return hat in numbers

    def run_hotkey(self, action_id):
        """Move the action's control as if by hand (main thread); the
        control's own handler writes the model and the driver."""
        action = hotkeys.BY_ID.get(action_id)
        if action is None or self.device is None:
            return
        widget = getattr(self.ui, action.widget)
        if not widget.get_sensitive():
            self.ui.set_hotkeys_status(_("{}: not available now").format(action.label))
            return
        if action.kind == 'toggle':
            widget.set_active(not widget.get_active())
            state = widget.get_active()
            self.hotkey_feedback(state=state)
            self.ui.set_hotkeys_status('{}: {}'.format(action.label, _("on") if state else _("off")))
            return
        if action.kind == 'profile':
            name = self.ui.cycle_profile(action.delta)
            self.ui.set_hotkeys_status('{}: {}'.format(action.label, name or _("no saved profiles")))
            return
        if action.kind == 'range':
            self.add_range(action.delta)
            value = self.model.get_range()
            self.hotkey_feedback(fraction=(value - 40) / max(1, self.device.get_max_range() - 40))
            self.ui.set_hotkeys_status('{}: {}°'.format(action.label, value))
            return
        adjustment = widget.get_adjustment()
        delta = action.delta
        if action.kind == 'shift':
            unit = self.model.get_rev_leds_shift_unit()
            delta *= hotkeys.SHIFT_STEP[unit]
        widget.set_value(widget.get_value() + delta)       # the adjustment clamps it
        value = widget.get_value()
        low, high = adjustment.get_lower(), adjustment.get_upper() - adjustment.get_page_size()
        fraction = (value - low) / (high - low) if high > low else 1.0
        if action.kind == 'shift':
            max_rpm = self.telemetry.reference_max() if self.telemetry is not None else 0.0
            if unit == 'rpm':
                if max_rpm:
                    fraction = value / max_rpm
                shown = '{} rpm'.format(int(value))
            else:
                shown = '{} %'.format(int(value)) + (' ({} rpm)'.format(int(round(value / 100.0 * max_rpm))) if max_rpm else '')
        else:
            shown = str(int(value))
        self.hotkey_feedback(fraction=fraction)
        self.ui.set_hotkeys_status('{}: {}'.format(action.label, shown))

    def hotkey_feedback(self, fraction=None, state=None):
        """Show the new level on the rev LEDs for a moment, over the rev
        lights if they are running. Not while the driver's FFB meter has
        the LEDs."""
        if self.device is None or (self.telemetry is None and self.model.get_ffb_leds()):
            return
        leds = self.device.rev_leds()
        if not leds.available():
            return
        if state is not None:
            leds.show_state(state)
        else:
            leds.show_level(fraction)

    def add_range(self, delta):
        max_range = self.device.get_max_range()
        wrange = self.model.get_range()
        wrange = wrange + delta
        if wrange < 40:
            wrange = 40
        if wrange > max_range:
            wrange = max_range
        self.ui.set_range(wrange)

    def read_ffbmeter(self):
        level = self.device.get_peak_ffb_level()
        if level is None:
            return level
        level = int(level)
        if level > 0:
            self.device.set_peak_ffb_level(0)
        return level

    def process_events(self, events):
        for event in events:
            if event.type == ecodes.EV_ABS:
                if event.code == ecodes.ABS_X:
                    self.last_wheel_axis_value = event.value
                    if self.test and self.test.is_collecting_data():
                        self.test.append_data(event.timestamp(), event.value)
                    else:
                        self.ui.safe_call(self.ui.set_steering_input, event.value)
                elif event.code in (ecodes.ABS_Z, ecodes.ABS_RZ, ecodes.ABS_Y):
                    axis = self.pedal_axes.get(event.code)
                    if axis is not None:
                        setter = {ecodes.ABS_Z: self.ui.set_accelerator_input,
                                  ecodes.ABS_RZ: self.ui.set_brakes_input,
                                  ecodes.ABS_Y: self.ui.set_clutch_input}[event.code]
                        self.ui.safe_call(setter, self._axis_fraction(axis, event.value))
                        name = self.LAUNCH_PEDAL.get(event.code)
                        if name is not None:
                            self.launch_inputs[name] = self._pressed_fraction(axis, event.value)
                elif self.handbrake_axis is not None and event.code == self.handbrake_axis[0]:
                    _, low, high = self.handbrake_axis
                    if high > low:
                        # As the game receives it: pulled is high once the
                        # Invert box is right, which the game needs too
                        pulled = min(1.0, max(0.0, (event.value - low) / (high - low)))
                        self.launch_inputs['handbrake'] = pulled
                        self.ui.safe_call(self.ui.set_handbrake_input, pulled)
                elif event.code == ecodes.ABS_HAT0X:
                    self.ui.safe_call(self.ui.set_hatx_input, event.value)
                    if event.value:
                        self.ui.safe_call(self.on_wheel_hotkey, hotkeys.hat_input(event.code, event.value),
                                          self._hotkeys_suppressed())
                    if event.value == -1:
                        self.on_button_press(100, 1)
                    elif event.value == 1:
                        self.on_button_press(101, 1)
                elif event.code == ecodes.ABS_HAT0Y:
                    self.ui.safe_call(self.ui.set_haty_input, event.value)
                    if event.value:
                        self.ui.safe_call(self.on_wheel_hotkey, hotkeys.hat_input(event.code, event.value),
                                          self._hotkeys_suppressed())
                    if event.value == -1:
                        self.on_button_press(102, 1)
                    elif event.value == 1:
                        self.on_button_press(103, 1)
            if event.type == ecodes.EV_KEY:
                if event.value == 1:
                    self.ui.safe_call(self.on_wheel_hotkey, hotkeys.key_input(event.code), self._hotkeys_suppressed())
                    kind = self.shift_buttons.get(event.code)
                    if kind is not None:
                        self.launch_inputs['shift_press'] = (time.monotonic(), kind)
                if event.value:
                    delay = 0
                    if self.test and self.test.is_awaiting_action():
                        self.test.trigger_action()
                else:
                    delay = 100

                button = None

                if event.code >= 288 and event.code <= 303:
                    button = event.code - 288
                if event.code >= 304 and event.code <= 316:
                    button = event.code - 304
                if event.code >= 704 and event.code <= 715:
                    button = event.code - 688

                if button is not None:
                    self.ui.safe_call(self.ui.set_btn_input, button, event.value, delay)
                    self.on_button_press(button, event.value)

    def input_thread(self):
        while 1:
            if self.device is not None and self.device.is_ready():
                try:
                    events = self.device.read_events(0.5)
                    if events is not None:
                        self.process_events(events)
                except OSError as e:
                    logging.debug(e)
                    time.sleep(1)
                except Exception:
                    # Reading input must not stop because one event upset
                    # something: the whole window goes still if it does.
                    logging.exception("input")
                    time.sleep(1)
            else:
                time.sleep(1)
            self.ui.safe_call(self.populate_devices)

    def run_command(self):
        proc = subprocess.Popen(self.app.args.command, shell=True)
        returncode = proc.wait()
        if returncode != 0:
            self.ui.safe_call(self.ui.error_dialog, _('Command error'),
                _("The supplied command failed:\n{}").format(self.app.args.command[0]))
        else:
            self.ui.safe_call(self.ui.quit)

    def start_test(self):
        def test_callback(name = 'end'):
            if name == 'end':
                self.ui.safe_call(self.end_test)
            elif name == 'running':
                self.ui.safe_call(self.ui.show_test_running, self.test_run, 1)
        self.test = Test(self.device, test_callback)
        self.test_run = 0
        self.ui.switch_test_panel(self.test_run)

    def end_test(self):
        if self.test_run == 0:
            self.minimum_level = self.test.get_minimum_level()
        elif self.test_run == 1:
            self.linear_chart = LinearChart(self.test.get_input_values(), self.test.get_output_values(),
                    self.device.get_max_range())
            self.linear_chart.set_minimum_level(self.minimum_level)
        elif self.test_run == 2:
            self.performance_chart = PerformanceChart(self.test.get_input_values(), self.test.get_output_values(),
                    self.device.get_max_range())
            if self.performance_chart.get_latency() is None:
                self.ui.error_dialog(_('Steering wheel not responding.'), _('No wheel movement could be registered.'))
                self.ui.switch_test_panel(None)
                return
            self.combined_chart = CombinedChart(self.linear_chart, self.performance_chart)
            self.test = None
            self.test_run = None
            self.show_test_results()
            return
        self.next_test()

    def run_test(self):
        self.ui.show_test_running(self.test_run)
        self.test.run(self.test_run)

    def prev_test(self):
        self.test_run -= 1
        if self.test_run == -1:
            self.test_run = None
        self.ui.switch_test_panel(self.test_run)
        if self.test_run is None and self.combined_chart is not None:
            self.show_test_results()

    def next_test(self):
        self.test_run += 1
        self.ui.switch_test_panel(self.test_run)
        if self.test_run > 2:
            return

    def show_test_results(self):
        self.ui.test_latency.set_text(format(1000 * self.performance_chart.get_latency(), '.0f'))
        self.ui.test_max_velocity.set_text(format(self.performance_chart.get_max_velocity(), '.0f'))
        self.ui.test_max_accel.set_text(format(self.performance_chart.get_max_accel(), '.0f'))
        self.ui.test_max_decel.set_text(format(self.performance_chart.get_max_decel(), '.0f'))
        self.ui.test_time_to_max_accel.set_text(format(1000 * self.performance_chart.get_time_to_max_accel(), '.0f'))
        self.ui.test_time_to_max_decel.set_text(format(1000 * self.performance_chart.get_time_to_max_decel(), '.0f'))
        self.ui.test_mean_accel.set_text(format(self.performance_chart.get_mean_accel(), '.0f'))
        self.ui.test_mean_decel.set_text(format(self.performance_chart.get_mean_decel(), '.0f'))
        self.ui.test_residual_decel.set_text(format(self.performance_chart.get_residual_decel(), '.0f'))
        self.ui.test_estimated_snr.set_text(format(self.performance_chart.get_estimated_snr(), '.0f'))
        self.ui.test_minimum_level.set_text(format(self.linear_chart.get_minimum_level_percent(), '.1f'))
        self.ui.on_test_ready()

    def import_test_values(self):
        filename = self.ui.file_chooser(_('CSV file to import'), 'open', file_type='csv')
        if filename is None:
            return

        with open(filename) as csv_file:
            lin_input_values = []
            lin_output_values = []
            perf_input_values = []
            perf_output_values = []
            data_block = 0
            csv_reader = csv.reader(csv_file, delimiter=',')
            for row in csv_reader:
                if row[0].startswith('#'):
                    continue
                if row[0] == 'minimum_level':
                    self.minimum_level = row[1]
                elif row[0] == 'linear_data':
                    data_block = 0
                elif row[0] == 'performance_data':
                    data_block = 1
                elif data_block == 0:
                    lin_input_values.append((float(row[0]), float(row[1])))
                    lin_output_values.append((float(row[2]), float(row[3])))
                elif data_block == 1:
                    perf_input_values.append((float(row[0]), float(row[1])))
                    perf_output_values.append((float(row[2]), float(row[3])))

        self.linear_chart = LinearChart(lin_input_values, lin_output_values, self.device.get_max_range())
        self.linear_chart.set_minimum_level(self.minimum_level)
        self.performance_chart = PerformanceChart(perf_input_values, perf_output_values, self.device.get_max_range())
        self.combined_chart = CombinedChart(self.linear_chart, self.performance_chart)

        self.show_test_results()

        self.ui.info_dialog(_("Test data imported."),
            _("New test data imported from CSV file."))

    def export_test_values(self):
        if self.combined_chart is None:
            return

        default_filename = 'report-' + datetime.now().strftime('%Y%m%d%H%M%S') + '.csv'
        filename = self.ui.file_chooser(_('CSV file to export'), 'save', default_filename, 'csv')
        if filename is None:
            return

        with open(filename, mode='w') as csv_file:
            csv_writer = csv.writer(csv_file, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
            csv_writer.writerow(['minimum_level', self.minimum_level])
            csv_writer.writerow(['linear_data'])
            for v1, v2 in zip(self.linear_chart.get_input_values(), self.linear_chart.get_output_values()):
                csv_writer.writerow([format(v1[0], '.5f'), format(v1[1], '.5f'), format(v2[0], '.5f'), format(v2[1], '.5f')])
            csv_writer.writerow(['performance_data'])
            for v1, v2 in zip(self.performance_chart.get_input_values(), self.performance_chart.get_pos_values()):
                csv_writer.writerow([format(v1[0], '.5f'), format(v1[1], '.5f'), format(v2[0], '.5f'), format(v2[1], '.5f')])

        self.ui.info_dialog(_("Test data exported."),
            _("Current test data has been exported to a CSV file."))

    def open_test_chart(self):
        if self.combined_chart is None:
            return

        canvas = self.combined_chart.get_canvas()
        toolbar = self.combined_chart.get_navigation_toolbar(canvas)
        self.ui.show_test_chart(canvas, toolbar)
