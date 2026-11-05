import gi
import locale as Locale
from locale import gettext as _
import logging
import math
import os
import time
from .gtk_handlers import GtkHandlers
from . import hotkeys
from .ffb_meter import FfbMeter, FfbPoller
from . import qr
from . import wheel_torque
from . import steam_options
from .telemetry import AUTO, DEFAULT_PORT, PORT_PRESETS, PORT_MIN, PORT_MAX, parse_port
from .telemetry_formats import eawrc_structure, eawrc_config_lines
from . import telemetry_plot
from .telemetry_capture import FFB_MAX_BYTES, FFB_MAX_SECONDS
from .telemetry_view import DISCIPLINES, SURFACES, METHODS, clock, signed, coaching_items, live_status, sob_gain, splits_row, shift_summary, shift_table
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib, GObject

class GtkUi:

    def __init__(self, controller, argv):
        self.controller = controller

        self.ffbmeter_timer = False
        self.current_test_canvas = None
        self.current_test_toolbar = None

        Gdk.init(argv)
        style_provider = Gtk.CssProvider()
        style_provider.load_from_path(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'main.css'))
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            style_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.builder = Gtk.Builder()
        self.builder.set_translation_domain('oversteer')
        self.builder.add_from_file(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'main.ui'))

        self._set_builder_objects()

        self._set_markers()
        self._build_pedal_response()

        cell_renderer = Gtk.CellRendererText()
        self.device_combobox.pack_start(cell_renderer, True)
        self.device_combobox.add_attribute(cell_renderer, 'text', 1)
        self.device_combobox.set_id_column(0)

        cell_renderer = Gtk.CellRendererText()
        self.profile_combobox.pack_start(cell_renderer, True)
        self.profile_combobox.add_attribute(cell_renderer, 'text', 0)
        self.profile_combobox.set_id_column(0)

        cell_renderer = Gtk.CellRendererText()
        self.emulation_mode_combobox.pack_start(cell_renderer, True)
        self.emulation_mode_combobox.add_attribute(cell_renderer, 'text', 1)
        self.emulation_mode_combobox.set_id_column(0)

        self.set_range_overlay('never')
        self.disable_save_profile()
        self._build_hotkeys_page()
        self._build_telemetry_page()
        self._build_wheel_launch_options()
        self._build_ffb_meter()
        self._build_countersteer_ffb()

    def reset_view(self):
        self.new_profile_name_entry.hide()
        self.start_app.hide()
        self.switch_test_panel(None)

    def start(self):
        handlers = GtkHandlers(self, self.controller)
        self.builder.connect_signals(handlers)
        entry = self.rev_leds_port.get_child()
        entry.connect('activate', handlers.on_rev_leds_port_commit)
        entry.connect('focus-out-event', handlers.on_rev_leds_port_leave)
        entry.connect('changed', lambda e: self._port_error(None))
        self.window.show_all()
        self.reset_view()
        self._set_range_markers(1080)

    def main(self):
        Gtk.main()

    def quit(self):
        Gtk.main_quit()

    def safe_call(self, callback, *args):
        GLib.idle_add(callback, *args)

    def confirmation_dialog(self, message):
        dialog = Gtk.MessageDialog(self.window, 0,
                Gtk.MessageType.WARNING, Gtk.ButtonsType.OK_CANCEL, message)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def info_dialog(self, message, secondary_text = ''):
        dialog = Gtk.MessageDialog(self.window, 0, Gtk.MessageType.INFO,
                Gtk.ButtonsType.OK, message)
        dialog.format_secondary_text(secondary_text)
        dialog.run()
        dialog.destroy()

    def error_dialog(self, message, secondary_text = ''):
        dialog = Gtk.MessageDialog(self.window, 0, Gtk.MessageType.ERROR,
                Gtk.ButtonsType.OK, message)
        dialog.format_secondary_text(secondary_text)
        dialog.run()
        dialog.destroy()

    def file_chooser(self, title, action, default_name = None, file_type = 'all'):
        if action == 'open':
            action = Gtk.FileChooserAction.OPEN
        elif action == 'save':
            action = Gtk.FileChooserAction.SAVE
        else:
            return None

        dialog = Gtk.FileChooserNative(
            title=title, transient_for=self.window, action=action
        )

        if action == Gtk.FileChooserAction.SAVE:
            if default_name is not None:
                dialog.set_current_name(default_name)

        file_filter = Gtk.FileFilter()

        if file_type == 'csv':
            file_filter.set_name('CSV')
            file_filter.add_pattern('*.csv')
        elif file_type == 'ini':
            file_filter.set_name('INI')
            file_filter.add_pattern('*.ini')
        elif file_type == 'all':
            file_filter.set_name(_('All files'))
            file_filter.add_pattern('*')

        dialog.add_filter(file_filter)
        dialog.set_filter(file_filter)

        response = dialog.run()

        if response == Gtk.ResponseType.ACCEPT:
            filename = dialog.get_filename()
        else:
            filename = None

        dialog.destroy()

        return filename

    def update(self):
        self.window.queue_draw()

    def set_app_version(self, version):
        self.about_window.set_version(version)

    def set_app_icon(self, icon):
        if not os.access(icon, os.R_OK):
            logging.debug("Icon not found: %s", icon)
            return
        self.window.set_icon_from_file(icon)

    def set_languages(self, languages):
        cell_renderer = Gtk.CellRendererText()
        self.languages_combobox.pack_start(cell_renderer, True)
        self.languages_combobox.add_attribute(cell_renderer, 'text', 1)
        self.languages_combobox.set_id_column(0)
        model = self.languages_combobox.get_model()
        model = Gtk.ListStore(str, str)
        for pair in languages:
            model.append(pair)
        self.languages_combobox.set_model(model)

    def set_language(self, language):
        self.languages_combobox.set_active_id(language)

    def set_check_permissions(self, state):
        self.check_permissions.set_state(state)

    def set_device_id(self, device_id):
        self.device_combobox.set_active_id(device_id)

    def set_devices(self, devices):
        model = self.device_combobox.get_model()
        if model is None:
            model = Gtk.ListStore(str, str)
        else:
            self.device_combobox.set_model(None)
            model.clear()
        self.device_combobox.set_model(model)
        if devices:
            for pair in devices:
                model.append(pair)
            if self.device_combobox.get_active() == -1:
                self.device_combobox.set_active(0)
            self.enable_controls()
        else:
            self.disable_controls()

    def select_device(self, device_id):
        self.device_combobox.set_active_id(device_id)

    def disable_controls(self):
        self.profile_combobox.set_sensitive(False)
        self.test_start_button.set_sensitive(False)
        self.test_start_button.set_sensitive(False)

    def enable_controls(self):
        self.profile_combobox.set_sensitive(True)
        self.test_start_button.set_sensitive(True)
        self.test_start_button.set_sensitive(True)

    def update_profiles_combobox(self):
        model = self.profile_combobox.get_model()
        if model is None:
            active_id = ''
            model = Gtk.ListStore(str)
        else:
            active_id = self.profile_combobox.get_active_id()
            model.clear()
        model.append([''])

        profiles = []
        for row in self.profile_listbox.get_children():
            profiles.append(row.get_children()[0].get_text())
        profiles.sort()

        for profile_name in profiles:
            model.append([profile_name])

        self.profile_combobox.set_model(model)
        self.profile_combobox.set_active_id(active_id)

    def profile_listbox_add(self, profile_name):
        label = Gtk.Label(label=profile_name)
        label.set_xalign(0)
        self.profile_listbox.add(label)
        label.show()
        self.profile_listbox.select_row(label.get_parent())

    def set_profiles(self, profiles):
        for widget in self.profile_listbox.get_children():
            widget.destroy()

        for profile_name in profiles:
            self.profile_listbox_add(profile_name)

        self.update_profiles_combobox()

    def set_profile(self, profile):
        self.profile_combobox.set_active_id(profile)

    def set_max_range(self, max_range):
        self.wheel_range_setup.set_upper(max_range / 10)
        self._set_range_markers(max_range)
        # A preset the wheel can't reach would just clamp to its maximum
        for degrees, button in self.range_presets.items():
            button.set_visible(max_range >= degrees)

    def set_modes(self, modes):
        self.change_emulation_mode_button.set_sensitive(False)
        model = self.emulation_mode_combobox.get_model()
        if model is None:
            model = Gtk.ListStore(str, str)
        else:
            self.emulation_mode_combobox.set_model(None)
            model.clear()
        if not modes:
            self.emulation_mode_combobox.set_sensitive(False)
        else:
            for key, values in enumerate(modes):
                model.append(values[:2])
                if values[2]:
                    self.emulation_mode_combobox.set_active(key)
            self.emulation_mode_combobox.set_sensitive(True)
        self.emulation_mode_combobox.set_model(model)

    def set_mode(self, mode):
        model = self.emulation_mode_combobox.get_model()
        if model and len(model) != 0:
            self.emulation_mode_combobox.set_sensitive(True)
            self.change_emulation_mode_button.set_sensitive(True)
            self.emulation_mode_combobox.set_active_id(mode)

    def set_range(self, wrange):
        if wrange is None:
            self.wheel_range.set_sensitive(False)
            self.wheel_range_overlay_always.set_sensitive(False)
            self.wheel_range_overlay_auto.set_sensitive(False)
            return
        self.wheel_range.set_sensitive(True)
        self.wheel_range_overlay_always.set_sensitive(True)
        self.wheel_range_overlay_auto.set_sensitive(True)
        wrange = int(wrange) / 10
        self.wheel_range.set_value(wrange)
        wrange = str(round(wrange * 10))
        self.overlay_wheel_range.set_label(wrange)

    def set_sensitivity(self, sensitivity):
        if sensitivity is None:
            self.wheel_sensitivity.set_sensitive(False)
            return
        self.wheel_sensitivity.set_sensitive(True)
        self.wheel_sensitivity.set_value(int(sensitivity))

    def set_combine_pedals(self, combine_pedals):
        self.set_pedal_response_combined(bool(combine_pedals))
        if combine_pedals is None:
            self.combine_brakes.set_sensitive(False)
            self.combine_clutch.set_sensitive(False)
        else:
            self.combine_brakes.set_sensitive(True)
            self.combine_clutch.set_sensitive(True)
        if combine_pedals == 1:
            self.combine_brakes.set_active(True)
        elif combine_pedals == 2:
            self.combine_clutch.set_active(True)
        else:
            self.combine_none.set_active(True)

    def set_autocenter(self, autocenter):
        if autocenter is None:
            self.autocenter.set_sensitive(False)
        else:
            self.autocenter.set_sensitive(True)
            self.autocenter.set_value(int(autocenter))

    def _set_switch(self, switch, value):
        self._available[switch] = value is not None
        if value is None:
            switch.set_sensitive(False)
            return
        switch.set_sensitive(True)
        switch.set_active(bool(value))

    def set_autocenter_persistent(self, value):
        self._set_switch(self.autocenter_persistent, value)

    def set_app_gain(self, value):
        self._set_switch(self.app_gain, value)

    def set_inertia_mode(self, value):
        self._set_switch(self.inertia_mode, value)

    # invert_pedals bit mask as the driver defines it, by evdev axis
    # Which invert_pedals bit each box drives. The default is the usual
    # layout; set_pedal_bits() replaces it per device, because several
    # wheels report their pedals on other axes than they present them.
    PEDAL_BOXES = ('clutch', 'accelerator', 'brakes')

    def set_pedal_bits(self, bits):
        """{'clutch'|'accelerator'|'brakes': invert_pedals bit or None} for
        the current device."""
        self.pedal_bits = dict(bits)
        for name in self.pedal_response:
            self._update_pedal_response_button(name)

    def _pedal_boxes(self):
        return zip(self.PEDAL_BOXES, (self.invert_clutch, self.invert_accelerator, self.invert_brakes))

    def set_invert_pedals(self, mask):
        # set_active emits 'clicked'; don't write partial masks to the device
        self.updating_invert_pedals = True
        try:
            for name, box in self._pedal_boxes():
                bit = self.pedal_bits.get(name)
                box.set_sensitive(mask is not None and bit is not None)
                box.set_active(bool(mask) and bit is not None and bool(mask & bit))
        finally:
            self.updating_invert_pedals = False

    # Controls tab grid column of each pedal
    PEDAL_COLUMNS = {'clutch': 0, 'brakes': 1, 'accelerator': 2}

    def _build_pedal_response(self):
        """A 'Response...' button under each pedal's Invert box, opening a
        popover with the pedal's deadzone, full-travel point and curve."""
        self.updating_pedal_response = False
        self.showing_pedal_preset = False
        self.pedal_response_shown = {}       # name -> last response set, None if none
        self.pedal_response_combined = False
        self.pedal_response = {}
        self.pedal_preset = {}
        grid = self.invert_clutch.get_parent()
        for name, column in self.PEDAL_COLUMNS.items():
            popover_box = Gtk.Grid(row_spacing=6, column_spacing=10, margin=10)
            model = self.controller.model
            start = self._response_scale(0, model.RESPONSE_START_MAX, 5)
            end = self._response_scale(model.RESPONSE_END_MIN, 100, 5)
            sensitivity = self._response_scale(0, 100, 10)
            sensitivity.add_mark(0, Gtk.PositionType.BOTTOM, _("Soft"))
            sensitivity.add_mark(50, Gtk.PositionType.BOTTOM, _("Linear"))
            sensitivity.add_mark(100, Gtk.PositionType.BOTTOM, _("Sharp"))
            start.set_tooltip_text(_("How far the pedal travels before it starts to register (deadzone at the released end)."))
            end.set_tooltip_text(_("How far the pedal travels before it reads full."))
            sensitivity.set_tooltip_text(_("50 is linear; lower is softer at the start of the travel, higher is sharper."))
            presets = self.controller.model.PEDAL_PRESETS[name]
            preset = Gtk.ComboBoxText()
            preset.append('custom', _("Custom"))
            for pid, label, _response, _note in presets:
                preset.append(pid, _(label))
            preset.set_active_id('custom')
            note = Gtk.Label(label="", halign=Gtk.Align.START, xalign=0, wrap=True, max_width_chars=34)
            note.get_style_context().add_class('dim-label')
            note.set_no_show_all(True)
            reset = Gtk.Button(label=_("Reset"), halign=Gtk.Align.END)
            for row, (label, scale) in enumerate(((_("Starts at"), start), (_("Full at"), end), (_("Sensitivity"), sensitivity))):
                label = Gtk.Label(label=label, halign=Gtk.Align.START, valign=Gtk.Align.START)
                popover_box.attach(label, 0, row, 1, 1)
                popover_box.attach(self._response_row(scale, scale is not sensitivity), 1, row, 1, 1)
            popover_box.attach(Gtk.Label(label=_("Preset"), halign=Gtk.Align.START), 0, 3, 1, 1)
            popover_box.attach(preset, 1, 3, 1, 1)
            popover_box.attach(note, 1, 4, 1, 1)
            popover_box.attach(reset, 1, 5, 1, 1)
            popover_box.show_all()
            popover = Gtk.Popover()
            popover.add(popover_box)
            button = Gtk.MenuButton(label=_("Response…"), halign=Gtk.Align.CENTER, popover=popover, sensitive=False)
            button.set_tooltip_text(_("Reshape this pedal: a deadzone at the start of the travel, where it reaches full, and a curve. The bars show what games see. Saved in the profile. Needs a driver with pedal_response."))
            button.show()
            grid.attach(button, column, 4, 1, 1)
            self.pedal_response[name] = (button, start, end, sensitivity)
            self.pedal_response_shown[name] = None
            self.pedal_preset[name] = (preset, note)
            preset.connect('changed', self._on_pedal_preset_changed, name)
            for scale in (start, end, sensitivity):
                scale.connect('value-changed', self._on_pedal_response_changed, name)
            reset.connect('clicked', self._on_pedal_response_reset, name)

    @staticmethod
    def _response_scale(low, high, step):
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, low, high, 1)
        scale.set_digits(0)
        scale.set_size_request(200, -1)
        scale.set_hexpand(True)
        scale.set_draw_value(False)
        scale.get_adjustment().set_page_increment(step)
        # Only a drag moves a setting: not the mouse wheel (it scrolls the page) and not the keys (field: Steering strength found
        # at 10 % on a drive, nobody having touched it, in a multiple of this page step, with no web write: cause not proven)
        scale.set_can_focus(False)
        scale.connect('scroll-event', lambda w, _e: (GObject.signal_stop_emission_by_name(w, 'scroll-event'), False)[1])
        return scale

    @staticmethod
    def _response_row(scale, percent=True):
        """The scale with its value, in a label of a fixed width so the three
        tracks come out the same length. `percent` adds a '%' (the sensitivity
        is not a percentage); the label is kept the same width either way. Without
        it the label sits at the top, level with the track, as the sensitivity
        track has marks under it."""
        value = Gtk.Label(label="", width_chars=5, xalign=1)
        if not percent:
            value.set_valign(Gtk.Align.START)
            value.set_margin_top(4)
        def show(scale):
            value.set_text("{:.0f} %".format(scale.get_value()) if percent else "{:.0f}".format(scale.get_value()))
        scale.connect('value-changed', show)
        show(scale)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.pack_start(scale, True, True, 0)
        row.pack_start(value, False, False, 0)
        return row

    def get_pedal_response(self, name):
        _button, start, end, sensitivity = self.pedal_response[name]
        return int(start.get_value()), int(end.get_value()), int(sensitivity.get_value())

    def set_pedal_response(self, name, response):
        """Show a pedal's (start, end, sensitivity); None greys the button
        out (the driver has no pedal response)."""
        _button, start, end, sensitivity = self.pedal_response[name]
        self.pedal_response_shown[name] = response
        self._update_pedal_response_button(name)
        if response is None:
            return
        self.updating_pedal_response = True
        try:
            for scale, value in zip((start, end, sensitivity), response):
                scale.set_value(value)
            self._show_pedal_preset(name)
        finally:
            self.updating_pedal_response = False

    def set_pedal_response_combined(self, combined):
        """The driver ignores the responses while the pedals are combined."""
        self.pedal_response_combined = bool(combined)
        for name in self.pedal_response:
            self._update_pedal_response_button(name)

    def _update_pedal_response_button(self, name):
        """The Response button needs a value, a driver that has the
        attributes, the pedal on this device and the pedals not combined."""
        device = self.controller.model.get_device()
        usable = (self.pedal_response_shown[name] is not None
                  and device is not None and device.has_pedal_response()
                  and self.pedal_bits.get(name) is not None
                  and not self.pedal_response_combined)
        self.pedal_response[name][0].set_sensitive(bool(usable))

    def _show_pedal_preset(self, name):
        """Preset box and note follow the three controls."""
        model = self.controller.model
        current = model.data.get(name + '_response') or self.get_pedal_response(name)
        combo, note = self.pedal_preset[name]
        match = [(pid, text) for pid, _label, response, text in self.controller.model.PEDAL_PRESETS[name]
                 if response == current]
        self.showing_pedal_preset = True
        try:
            combo.set_active_id(match[0][0] if match else 'custom')
        finally:
            self.showing_pedal_preset = False
        text = match[0][1] if match else None
        note.set_text(_(text) if text else "")
        note.set_visible(bool(text))

    def _on_pedal_preset_changed(self, combo, name):
        if self.updating_pedal_response or self.showing_pedal_preset or combo.get_active_id() in (None, 'custom'):
            return
        response = self.controller.model.set_pedal_preset(name, combo.get_active_id())
        self.set_pedal_response(name, response)

    def _on_pedal_response_changed(self, widget, name):
        if self.updating_pedal_response:
            return
        model = self.controller.model
        _button, *scales = self.pedal_response[name]
        shown = self.get_pedal_response(name)
        # Only the moved scale comes from the UI: the others may show a value
        # clamped for display that the driver holds beyond the scale's range.
        response = shown
        stored = model.data.get(name + '_response')
        if stored is not None:
            mixed = tuple(s if scale is widget else st for scale, s, st in zip(scales, shown, stored))
            if model.valid_response(mixed) is not None:
                response = mixed
        model.set_pedal_response(name, *response)
        self._show_pedal_preset(name)   # after the model took it

    def _on_pedal_response_reset(self, widget, name):
        self.set_pedal_response(name, self.controller.model.DEFAULT_RESPONSE)
        self.controller.model.set_pedal_response(name, *self.controller.model.DEFAULT_RESPONSE)

    def get_invert_pedals(self):
        mask = 0
        for name, box in self._pedal_boxes():
            bit = self.pedal_bits.get(name)
            if bit is not None and box.get_active():
                mask |= bit
        return mask

    def set_ffb_enabled(self, value):
        self._set_switch(self.ffb_enabled, value)
        if value is None:
            return
        # Grey the strength controls while off; when on, only re-enable the
        # ones the device actually has (their own setters record that).
        enabled = bool(value)
        for widget in (self.ff_gain, self.ff_spring_level, self.ff_damper_level, self.ff_friction_level, self.ff_rumble_level, self.app_gain, self.inertia_mode):
            widget.set_sensitive(enabled and self._available.get(widget, False))
        for widget in self.try_buttons:
            widget.set_sensitive(enabled)

    def set_ff_gain(self, ff_gain):
        self._available[self.ff_gain] = ff_gain is not None
        if ff_gain is None:
            self.ff_gain.set_sensitive(False)
        else:
            self.ff_gain.set_sensitive(True)
            self.ff_gain.set_value(int(ff_gain))

    def set_spring_level(self, level):
        self._available[self.ff_spring_level] = level is not None
        if level is None:
            self.ff_spring_level.set_sensitive(False)
        else:
            self.ff_spring_level.set_sensitive(True)
            self.ff_spring_level.set_value(int(level))

    def set_damper_level(self, level):
        self._available[self.ff_damper_level] = level is not None
        if level is None:
            self.ff_damper_level.set_sensitive(False)
        else:
            self.ff_damper_level.set_sensitive(True)
            self.ff_damper_level.set_value(int(level))

    def set_friction_level(self, level):
        self._available[self.ff_friction_level] = level is not None
        if level is None:
            self.ff_friction_level.set_sensitive(False)
        else:
            self.ff_friction_level.set_sensitive(True)
            self.ff_friction_level.set_value(int(level))

    def set_rumble_level(self, level):
        self._available[self.ff_rumble_level] = level is not None
        if level is None:
            self.ff_rumble_level.set_sensitive(False)
        else:
            self.ff_rumble_level.set_sensitive(True)
            self.ff_rumble_level.set_value(int(level))

    def set_driver_status(self, text):
        self.driver_status.set_markup(text)

    def set_equipment(self, rows):
        """rows: (include, kind, name, usb_id, status, sys_path)"""
        self.equipment_store.clear()
        for row in rows:
            self.equipment_store.append(list(row))

    def get_included_equipment(self):
        return [row[5] for row in self.equipment_store if row[0]]

    def get_equipment_includes(self):
        """{sys_path: include} as currently shown."""
        return {row[5]: bool(row[0]) for row in self.equipment_store}

    def toggle_equipment(self, path):
        self.equipment_store[path][0] = not self.equipment_store[path][0]

    def set_combine(self, enabled, status='', generic=None):
        self.updating_combine = True
        try:
            self.combine_switch.set_sensitive(enabled is not None)
            self.combine_switch.set_active(bool(enabled))
            if generic is not None:
                self.combine_generic.set_active(bool(generic))
        finally:
            self.updating_combine = False
        self.combine_status.set_text(status)
        self.combine_status.set_tooltip_text(status)

    def set_combine_start_visible(self, visible):
        self.combine_start.set_visible(visible)

    def set_combine_busy(self, busy, text=None):
        self.combine_switch.set_sensitive(not busy)
        self.combine_generic.set_sensitive(not busy)
        self.combine_start.set_sensitive(not busy)
        self.equipment_view.set_sensitive(not busy)
        if busy:
            self.combine_status.set_text(text or _("Installing…"))

    def get_combine_generic(self):
        return self.combine_generic.get_active()

    def set_rev_leds(self, enabled, port, shift=None, unit=None):
        self._set_switch(self.rev_leds, enabled)
        for w in (self.rev_leds_port, self.rev_leds_shift, self.rev_leds_shift_unit, self.rev_leds_test):
            w.set_sensitive(enabled is not None)
        if enabled is not None:
            # Pre-shift-point profiles carry no unit/value: show the defaults
            unit = unit or 'percent'
            if shift is None:
                shift = 7000 if unit == 'rpm' else 97
        # Auto: each gear's learnt point, no single figure to show or edit
        auto = unit == 'auto' or (unit is None and self.rev_leds_shift_unit.get_active_id() == 'auto')
        self.rev_leds_shift.set_no_show_all(True)
        self.rev_leds_shift.set_visible(not auto)
        self.rev_leds_shift.set_sensitive(enabled is not None and not auto)
        self.updating_rev_leds = True
        try:
            if port is not None:                    # None: leave the port box as it is
                self._show_rev_leds_port(None if port == AUTO else int(port))
            if unit is not None:
                # The spin button's range follows the unit
                self.rev_leds_shift.set_adjustment(self.rev_leds_shift_rpm_adjustment if unit == 'rpm'
                                                   else self.rev_leds_shift_percent_adjustment)
                self.rev_leds_shift_unit.set_active_id(unit)
            if shift is not None and not auto:
                self.rev_leds_shift.set_value(int(shift))
        finally:
            self.updating_rev_leds = False

    @staticmethod
    def port_preset_text(port, label):
        return "{}: {}".format(port, _(label))

    @staticmethod
    def port_auto_text():
        return _("Auto: listen on all of these")

    def _is_auto_text(self, text):
        text = (text or '').strip().lower()
        return text == self.port_auto_text().lower() or text == 'auto'

    def _show_rev_leds_port(self, port):
        """Show `port`: its preset row if it has one, else the bare number;
        None: the Auto row (the first; the presets follow it)."""
        self._rev_leds_port_valid = port
        if port is None:
            self.rev_leds_port.set_active(0)
            self._port_error(None)
            return
        for index, (preset, label) in enumerate(PORT_PRESETS):
            if preset == port:
                self.rev_leds_port.set_active(index + 1)
                self._port_error(None)
                return
        self.rev_leds_port.set_active(-1)
        self.rev_leds_port.get_child().set_text(str(port))
        # Set after the text: a change of the text clears the error state
        self._port_error(_("Ports below 1024 need root.") if port < PORT_MIN else None)

    def _port_error(self, message):
        """The port entry's error state (icon, style class, tooltip), or none."""
        entry = self.rev_leds_port.get_child()
        context = entry.get_style_context()
        if message:
            context.add_class('error')
            entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, 'dialog-error-symbolic')
            entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, message)
        else:
            context.remove_class('error')
            entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, None)

    def rev_leds_port_selected(self):
        """The port of the preset row picked in the box, AUTO for the Auto
        row, or None while typing."""
        index = self.rev_leds_port.get_active()
        if index < 0:
            return None
        self._port_error(None)
        if index == 0:
            self._rev_leds_port_valid = None
            return AUTO
        self._rev_leds_port_valid = PORT_PRESETS[index - 1][0]
        return self._rev_leds_port_valid

    def rev_leds_port_typed(self, leave=False):
        """The port typed in the box (on Enter, or focus-out with `leave`) if
        it is valid and new, else None. An invalid entry gets the error state
        and the last valid port stays; on Enter the text stays to be fixed,
        on leaving it goes back to the port in use. AUTO when the text
        is the Auto row's (or "auto") and Auto was not in use."""
        text = self.rev_leds_port.get_child().get_text()
        valid = getattr(self, '_rev_leds_port_valid', None)
        if self._is_auto_text(text):
            self._with_port_update(self._show_rev_leds_port, None)
            return AUTO if valid is not None else None
        port = parse_port(text)
        if port is None:
            if leave:
                self._with_port_update(self._show_rev_leds_port, valid)
            else:
                self._port_error(_("Enter a port from {low} to {high}.").format(low=PORT_MIN, high=PORT_MAX))
            return None
        self._with_port_update(self._show_rev_leds_port, port)    # a preset's number selects its row
        return port if port != valid else None

    def _with_port_update(self, func, *args):
        self.updating_rev_leds = True
        try:
            func(*args)
        finally:
            self.updating_rev_leds = False

    def set_launch_options(self, text):
        self.launch_options.set_text(text)
        self.wheel_launch_guidance.set_text(steam_options.guidance(text))

    def _build_wheel_launch_options(self):
        """A row at the foot of the Devices tab: the Steam launch options
        for wheels and the combined device under Proton."""
        entry = Gtk.Entry(editable=False, text=steam_options.WHEEL_OPTIONS)
        entry.set_width_chars(len(steam_options.WHEEL_OPTIONS) + 1)
        button = Gtk.Button(label=_("Copy"))
        button.connect('clicked', lambda w: self._copy_text(entry.get_text()))
        self.wheel_launch_guidance = self._status_label()
        self.wheel_launch_guidance.set_text(steam_options.guidance(''))
        listbox = self._list()
        listbox.add(self._setting_row(
            _("Steam launch options for wheels under Proton"), tooltip=steam_options.WHEEL_TOOLTIP,
            controls=(entry, button), status=self.wheel_launch_guidance))
        page = self.builder.get_object('devices_page')
        page.pack_start(listbox, False, False, 0)
        listbox.show_all()

    def copy_launch_options(self):
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(self.launch_options.get_text(), -1)

    def set_rev_leds_status(self, text):
        self.rev_leds_status.set_text(text)

    def set_ffb_leds(self, value):
        if value is None:
            self.ffbmeter_leds.set_sensitive(False)
        else:
            self.ffbmeter_leds.set_sensitive(True)
            self.ffbmeter_leds.set_active(bool(value))

    def set_ffb_overlay(self, state):
        if state is None:
            self.set_ffbmeter_overlay_visibility(False)
            self.ffbmeter_overlay.set_sensitive(False)
        else:
            self.set_ffbmeter_overlay_visibility(True)
            self.ffbmeter_overlay.set_sensitive(True)
            self.ffbmeter_overlay.set_active(state)
            self.update_overlay()

    def set_range_overlay(self, sid):
        self.wheel_range_overlay_never.set_active(False)
        self.wheel_range_overlay_always.set_active(False)
        self.wheel_range_overlay_auto.set_active(False)
        if sid == 'always':
            self.wheel_range_overlay_always.set_active(True)
        elif sid == 'auto':
            self.wheel_range_overlay_auto.set_active(True)
        else:
            self.wheel_range_overlay_never.set_active(True)

    def set_use_buttons(self, state):
        if state is None:
            self.wheel_buttons.set_sensitive(False)
        else:
            self.wheel_buttons.set_sensitive(True)
            self.wheel_buttons.set_state(state)

    def set_center_wheel(self, state):
        self.center_wheel.set_state(state)

    def set_new_profile_name(self, name):
        self.new_profile_name.set_text(name)

    def set_steering_input(self, value):
        if value < 32768:
            self.steering_left_input.set_value(self._round_input((32768 - value) / 32768, 3))
            self.steering_right_input.set_value(0)
        else:
            self.steering_left_input.set_value(0)
            self.steering_right_input.set_value(self._round_input((value - 32768) / 32768, 3))

    # The pedal bars take a position, 0 released to 1 fully pressed; which
    # end of the axis that is depends on the device and on invert_pedals,
    # so the controller works it out (Device.pedal_axes).
    def set_clutch_input(self, fraction):
        self.clutch_input.set_value(self._round_input(fraction, 2))

    def set_accelerator_input(self, fraction):
        self.accelerator_input.set_value(self._round_input(fraction, 2))

    def set_brakes_input(self, fraction):
        self.brakes_input.set_value(self._round_input(fraction, 2))

    def set_handbrake_input(self, fraction):
        """`fraction` is 0 (released) to 1 (fully pulled)."""
        self.handbrake_input.set_value(self._round_input(fraction, 2))

    def set_handbrake_visible(self, visible):
        """The handbrake column only shows on devices that have one: a
        combined device carrying a handbrake, or a wheel with the axis."""
        self.handbrake_input.set_visible(visible)
        self.handbrake_label.set_visible(visible)
        self.invert_handbrake.set_visible(visible)
        self.calibrate_handbrake.set_visible(visible)
        if not visible:
            self.handbrake_input.set_value(0)

    def set_handbrake_invert(self, state):
        """Tick state of the handbrake's Invert box; None when nothing can
        change it (the handbrake is read directly, not through a proxy)."""
        self.invert_handbrake.set_sensitive(state is not None)
        self.updating_invert_pedals = True
        try:
            self.invert_handbrake.set_active(bool(state))
        finally:
            self.updating_invert_pedals = False

    def handbrake_progress_dialog(self, message, secondary_text=''):
        """A modeless notice for the seconds the handbrake is sampled;
        destroy() it when done."""
        dialog = Gtk.MessageDialog(self.window, 0, Gtk.MessageType.INFO,
                Gtk.ButtonsType.NONE, message)
        dialog.format_secondary_text(secondary_text)
        dialog.show()
        return dialog

    def get_handbrake_invert(self):
        return self.invert_handbrake.get_active()

    def set_hatx_input(self, value):
        if value < 0:
            self.hat_left_input.set_value(-value)
            self.hat_right_input.set_value(0)
        else:
            self.hat_left_input.set_value(0)
            self.hat_right_input.set_value(value)

    def set_haty_input(self, value):
        if value < 0:
            self.hat_up_input.set_value(-value)
            self.hat_down_input.set_value(0)
        else:
            self.hat_up_input.set_value(0)
            self.hat_down_input.set_value(value)

    def set_btn_input(self, index, value, wait = None):
        if wait is not None:
            GLib.timeout_add(wait, lambda index=index, value=value: self.set_btn_input(index, value))
        else:
            self.btn_input[index].set_value(value)
        return False

    def set_ffbmeter_overlay_visibility(self, state):
        self.ffbmeter_overlay.set_sensitive(state)

    def set_define_buttons_text(self, text):
        self.start_define_buttons.set_label(text)

    def reset_define_buttons_text(self):
        self.start_define_buttons.set_label(self.define_buttons_text)

    def get_wheel_range_overlay(self):
        wheel_range_overlay = None
        if self.wheel_range_overlay_never.get_active():
            wheel_range_overlay = 'never'
        elif self.wheel_range_overlay_always.get_active():
            wheel_range_overlay = 'always'
        elif self.wheel_range_overlay_auto.get_active():
            wheel_range_overlay = 'auto'
        return wheel_range_overlay

    def update_overlay(self, auto = False):
        ffbmeter_overlay = self.ffbmeter_overlay.get_active()
        wheel_range_overlay = self.get_wheel_range_overlay()
        if ffbmeter_overlay or wheel_range_overlay == 'always' or (wheel_range_overlay == 'auto' and auto):
            if not self.overlay_window.props.visible:
                self.overlay_window.show()
            if not self.ffbmeter_timer and self.overlay_window.props.visible and ffbmeter_overlay:
                self.ffbmeter_timer = True
                self.controller.ffb_consumer_start('overlay')
                GLib.timeout_add(250, self._update_ffbmeter_overlay)
            if ffbmeter_overlay:
                self._ffbmeter_overlay.show()
            else:
                self._ffbmeter_overlay.hide()
            if wheel_range_overlay == 'always' or (wheel_range_overlay == 'auto' and auto):
                self._wheel_range_overlay.show()
            else:
                self._wheel_range_overlay.hide()
        else:
            self.overlay_window.hide()

    def enable_save_profile(self):
        if self.profile_combobox.get_active_id() != '':
            self.save_profile_button.set_sensitive(True)

    def disable_save_profile(self):
        self.save_profile_button.set_sensitive(False)

    def enable_start_app(self):
        self.start_app.show()

    def disable_start_app(self):
        self.start_app.hide()

    def set_profile_note(self, text):
        self.profile_note.set_text(text or '')

    def set_apply_on_plug(self, state):
        self.profile_apply_on_plug.set_active(bool(state))

    def set_start_app_manually(self, state):
        self.start_app_manually.set_state(state)

    def on_test_ready(self):
        if self.device_combobox.get_active_id() is not None:
            self.test_start_button.set_sensitive(True)
        self.test_open_chart_button.set_sensitive(True)
        self.test_export_csv_button.set_sensitive(True)
        self.test_container_stack.set_visible_child(self.test_panel_results)

    def switch_test_panel(self, test_id):
        self.test_panel_warning.set_visible(False)
        self.test_panel_buttons.set_visible(False)
        self.test_start_button.set_sensitive(False)
        self.test_open_chart_button.set_sensitive(False)
        self.test_export_csv_button.set_sensitive(False)
        self.test_chart_window.hide()
        if test_id is None:
            self.test_container_stack.set_visible_child(self.test_panel_empty)
            self.test_start_button.set_sensitive(True)
        elif test_id == 0:
            self.test_container_stack.set_visible_child(self.test_panel_start1)
            self.test_panel_buttons.set_visible(True)
            self.test_panel_warning.set_visible(True)
            self.test_panel_running1_ready.set_visible(True)
            self.test_panel_running1_go.set_visible(False)
        elif test_id == 1:
            self.test_container_stack.set_visible_child(self.test_panel_start2)
            self.test_panel_buttons.set_visible(True)
            self.test_panel_warning.set_visible(True)
        elif test_id == 2:
            self.test_container_stack.set_visible_child(self.test_panel_start3)
            self.test_panel_buttons.set_visible(True)
            self.test_panel_warning.set_visible(True)

    def show_test_running(self, test_id, data = None):
        self.test_panel_warning.set_visible(False)
        self.test_panel_buttons.set_visible(False)
        if test_id == 0:
            self.test_panel_running1_ready.set_visible(True)
            self.test_panel_running1_go.set_visible(False)
            if data is not None:
                if data == 1:
                    self.test_panel_running1_ready.set_visible(False)
                    self.test_panel_running1_go.set_visible(True)
            self.test_container_stack.set_visible_child(self.test_panel_running1)
        elif test_id == 1:
            self.test_container_stack.set_visible_child(self.test_panel_running)
        elif test_id == 2:
            self.test_container_stack.set_visible_child(self.test_panel_running)

    FFB_METER_MS = 80           # ~12 Hz, only while the bar is on screen

    def _build_ffb_meter(self):
        """A live level bar with a peak-hold marker and a clip counter, as a
        row under the FFBmeter row. Polled only while it is mapped."""
        anchor = self._row_of(self.ffbmeter_leds)
        if anchor is None or anchor.get_parent() is None:
            return
        self.ffb_meter = FfbMeter()
        bar = Gtk.DrawingArea()
        bar.set_size_request(220, 18)
        bar.connect('draw', self._draw_ffb_meter)
        bar.connect('map', self._start_ffb_meter)
        self.ffb_meter_bar = bar
        self.ffb_poller = FfbPoller(self.ffb_meter, self.controller, bar.get_mapped)
        self.ffb_clip_label = Gtk.Label(xalign=1)
        self._show_ffb_clips()
        reset = Gtk.Button(label=_("Reset"))
        reset.connect('clicked', self._reset_ffb_clips)
        row = self._setting_row(_("FFB level"), None, _("Live force feedback level as a share of the wheel's full torque, with the highest recent value held. Clipped is how long the game asked for more than 100%: the force was cut off there, so the detail in it was lost. Lower the in-game FFB strength or the gain if it keeps counting."),
                (bar, self.ffb_clip_label, reset))
        anchor.get_parent().insert(row, anchor.get_index() + 1)
        row.show_all()

    @staticmethod
    def _calibrate_tooltip(supported):
        if not supported:
            return _("Needs a newer new-lg4ff driver (with ext_force, game_ffb_level and ffb_lut) and permission "
                     "to write its settings.")
        return _("Measures your wheel, hands off, in a few minutes: where its motor starts to push (dead "
                 "zone), how its force grows with the command, its friction, inertia and latency. The result "
                 "corrects the force in every game so it grows evenly from the lightest to the strongest effect. "
                 "The wheel turns by itself while it runs.")

    def _build_wheel_calibration(self, listbox):
        """Rows of the Countersteer FFB tab's "Wheel correction" list: the calibration button and its result,
        the correction switch, dither, soft clip and friction compensation (wheel_calibration.py)."""
        self.calibrate_button = Gtk.Button(label=_("Calibrate wheel"))
        self.calibrate_button.connect('clicked', lambda w: self.controller.start_calibration())
        self.calibration_status = self._status_label()
        self.calibration_status.set_text(_("Not calibrated"))
        self.updating_calibration = False
        rows = [self._setting_row(_("Wheel calibration"), controls=(self.calibrate_button,),
                                  status=self.calibration_status)]
        row, self.wheel_correction_switch = self._switch_row(
            _("Use wheel correction"),
            _("Applies the measured correction (force curve and dead zone) in the driver, for every game. Off: "
              "the wheel's own response."),
            lambda state: None if self.updating_calibration else self.controller.set_use_wheel_correction(state))
        rows.append(row)
        row, self.wheel_dither_switch = self._switch_row(
            _("Smooth small forces (dither)"),
            _("The wheel takes its force in 256 steps. Dither alternates between neighbouring steps so their "
              "average is the exact force, which keeps light forces smooth. Turned on by the calibration when "
              "the correction makes the steps coarse at the bottom; it sends a new command to the wheel most "
              "ticks."),
            lambda state: None if self.updating_calibration else self.controller.set_wheel_dither(state))
        rows.append(row)
        self.wheel_soft_clip = Gtk.SpinButton.new_with_range(0, 100, 5)
        self.wheel_soft_clip.set_value(0)
        self.wheel_soft_clip.connect('value-changed', lambda w: None if self.updating_calibration else
                                     self.controller.set_wheel_soft_clip(w.get_value_as_int()))
        rows.append(self._setting_row(
            _("Soft clip from (%)"), tooltip=_("Forces past this share of full scale are rounded off towards the "
                                               "maximum instead of being cut flat, so a request past 100 % is "
                                               "still felt as stronger. 0: off (hard limit)."),
            controls=(self.wheel_soft_clip,)))
        self.wheel_friction_comp = Gtk.SpinButton.new_with_range(0, 50, 10)
        self.wheel_friction_comp.set_value(0)
        self.wheel_friction_comp.connect('value-changed', lambda w: None if self.updating_calibration else
                                         self.controller.set_wheel_friction_comp(w.get_value_as_int()))
        rows.append(self._setting_row(
            _("Friction compensation (%)"),
            tooltip=_("Lifts the smallest forces by this share of the wheel's measured friction, so light "
                      "effects get through the gears and belt instead of being eaten by them. At most 50 % (a warm "
                      "wheel has less friction than measured) and never more than 80 % of the force that starts "
                      "the wheel moving. High values make the centre feel notchy (the force jumps as it changes "
                      "side). 0: off."),
            controls=(self.wheel_friction_comp,)))
        for row in rows:
            listbox.add(row)
        self.calibration_dialog = None
        self.set_wheel_calibration(False, None, False)

    def set_wheel_calibration(self, supported, entry, running):
        if not hasattr(self, 'calibrate_button'):
            return
        from .wheel_calibration import summary, _clamped_int
        self.updating_calibration = True
        try:
            self.calibrate_button.set_sensitive(bool(supported) and not running)
            self.calibrate_button.set_tooltip_text(self._calibrate_tooltip(supported))
            result = (entry or {}).get('result') or {}
            if running:
                self.calibration_status.set_text(_("Calibrating..."))
            elif not supported:
                self.calibration_status.set_text(_("Not available with this driver"))
            elif result:
                date = str(result.get('date', ''))[:10]
                self.calibration_status.set_text(_("Calibrated {}: {}").format(date, summary(result)))
            else:
                self.calibration_status.set_text(_("Not calibrated"))
            have = bool(supported and result)
            for widget in (self.wheel_correction_switch, self.wheel_dither_switch, self.wheel_soft_clip,
                           self.wheel_friction_comp):
                widget.set_sensitive(have and not running)
            self.wheel_correction_switch.set_active(bool(have and entry.get('use')))
            self.wheel_dither_switch.set_active(bool(have and entry.get('dither')))
            self.wheel_soft_clip.set_value(_clamped_int(entry.get('soft_clip'), 0, 100) if have else 0)
            self.wheel_friction_comp.set_value(_clamped_int(entry.get('friction_comp'), 0, 50) if have else 0)
        finally:
            self.updating_calibration = False

    def show_calibration_dialog(self, on_cancel):
        """A modal window while the calibration runs: what it is doing, how far, and Cancel. Closing it
        cancels too."""
        dialog = Gtk.Dialog(title=_("Calibrating the wheel"), transient_for=self.window, modal=True)
        dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        box = dialog.get_content_area()
        box.set_spacing(10)
        box.set_border_width(14)
        warning = Gtk.Label(xalign=0)
        warning.set_markup('<b>{}</b>'.format(GLib.markup_escape_text(_("Hands off: the wheel turns by itself."))))
        box.pack_start(warning, False, False, 0)
        self.calibration_label = Gtk.Label(label=_("Starting"), xalign=0)
        box.pack_start(self.calibration_label, False, False, 0)
        self.calibration_bar = Gtk.ProgressBar()
        self.calibration_bar.set_size_request(360, -1)
        box.pack_start(self.calibration_bar, False, False, 0)

        def cancel(*args):
            self.calibration_label.set_text(_("Stopping..."))
            on_cancel()
            return True             # the window stays until the wheel has been put back

        dialog.connect('response', cancel)
        dialog.connect('delete-event', cancel)
        dialog.show_all()
        self.calibration_dialog = dialog

    def set_calibration_progress(self, fraction, text):
        if getattr(self, 'calibration_dialog', None) is not None:
            self.calibration_bar.set_fraction(max(0.0, min(1.0, fraction)))
            self.calibration_label.set_text(text)
        return False

    def close_calibration_dialog(self):
        dialog, self.calibration_dialog = getattr(self, 'calibration_dialog', None), None
        if dialog is not None:
            dialog.destroy()

    COUNTERSTEER_MS = 250       # the light and its reason, while the row is on screen
    LIGHT_COLOURS = {'green': (0.2, 0.75, 0.3), 'amber': (0.95, 0.65, 0.1), 'grey': (0.55, 0.55, 0.55)}
    # Translated where shown
    COUNTERSTEER_TOOLTIP = ("Countersteer plays its own steering force, from the tyre forces in the game's "
                              "telemetry, in place of the game's force feedback, where it can do better: the light "
                              "is green when the game sends what that needs, fast enough (measured live). Amber: "
                              "the game keeps its own force and Countersteer adds to it (Forza Horizon: lighter as "
                              "the front tyres run out of grip), or a force model is possible but not built yet; "
                              "grey: the game keeps its own force. It only takes over while the light is green and "
                              "gives the game its force back at once otherwise. Saved with the profile.")
    COUNTERSTEER_NO_DRIVER = ("Needs the new-lg4ff driver with replace mode (ext_force): update the driver")

    def _build_countersteer_ffb(self):
        """The "Countersteer FFB" row under the Force feedback switch: a
        status light, the reason under the title, the switch; the rest is on the Countersteer FFB tab."""
        anchor = self._row_of(self.ffb_enabled)
        if anchor is None or anchor.get_parent() is None:
            return
        self.countersteer_light = Gtk.DrawingArea()
        self.countersteer_light.set_size_request(14, 14)
        self.countersteer_light.connect('draw', self._draw_countersteer_light)
        self.countersteer_light.connect('map', self._start_countersteer_poll)
        self._countersteer_shown = ('off', '')
        self.countersteer_status = self._status_label()
        def switched(state):
            self._show_countersteer_texture(state)
            self.controller.model.set_countersteer_ffb(state)
        row, self.countersteer_switch = self._switch_row(
            _("Countersteer FFB"), _(self.COUNTERSTEER_TOOLTIP), switched,
            status=self.countersteer_status, extra=(self.countersteer_light,))
        self.countersteer_switch.set_sensitive(False)
        anchor.get_parent().insert(row, anchor.get_index() + 1)
        row.show_all()
        self._countersteer_polling = False
        self.sensitivity_note = self._status_label()
        self.sensitivity_row = self._setting_row(_("Steering sensitivity"), None, None, (), self.sensitivity_note)
        self.sensitivity_row.show_all()
        self.sensitivity_row.set_no_show_all(True)
        self.sensitivity_row.set_visible(False)         # shown while the sensitivity warning applies
        anchor.get_parent().insert(self.sensitivity_row, row.get_index() + 1)
        pointer = self._setting_row(_("More Countersteer settings"),
                                    _("Steering, texture, game-specific settings and the wheel correction are on the "
                                      "Countersteer FFB tab."))
        anchor.get_parent().insert(pointer, row.get_index() + 2)
        pointer.show_all()
        self._build_countersteer_page()

    COUNTERSTEER_STRENGTH_TOOLTIP = ("How strong Countersteer's own steering force is. 100 % = the car's real steering "
                                     "torque played 1:1 in Nm up to the \"1:1 up to\" share of your wheel's peak "
                                     "torque and compressed above it, a quarter of the wheel's range kept free for "
                                     "road texture and impacts. 0 to 130 %. It does not depend on the global gain or "
                                     "on the game's own FFB setting. Lower it if the wheel feels heavy or runs warm. "
                                     "100 % by default. Saved with the profile.")
    COUNTERSTEER_KNEE_TOOLTIP = ("The share of your wheel's peak torque up to which the car's steering torque is played "
                                 "1:1 in Nm. Above it the torque is compressed so the car's top torque lands at three "
                                 "quarters of the wheel's range. Lower: a stronger squeeze of the heavy moments, more "
                                 "headroom; higher: more of the car's real weight, less room at the top. 30 to 60 %, "
                                 "50 % by default. Saved with the profile.")
    COUNTERSTEER_RBR_CUT_TOOLTIP = ("RBR's own force is used below this frequency; faster vibration comes from the Texture "
                                    "settings instead.")
    COUNTERSTEER_ENGINE_FEEL_TOOLTIP = ("How strongly the engine's revs come through the wheel. Over 100 % is stronger than today's level; "
                                        "turn it down if the gears get noisy.")
    COUNTERSTEER_CASTER_TOOLTIP = ("How much of the mechanical caster's force stays in a slide. 100 % is physically correct, 0 is off.")
    COUNTERSTEER_DAMPING_TOOLTIP = ("Resistance to turning the wheel quickly, added to what the wheel already has. Lower "
                                    "feels freer on fast corrections; higher settles the wheel better when you let go "
                                    "after a slide. 0 % is the wheel as it is; 100 % adds a real column's damping on top.")
    COUNTERSTEER_RIM_TOOLTIP = ("Hands feel force, not torque: the force is played 1:1 at your rim for the car's wheel size. Set your rim if you fitted a different one.")
    COUNTERSTEER_PULL_TOOLTIP = ("The drive's left/right split (the front wheels pulling unevenly under power or braking) "
                                 "acts on the steering through the scrub radius; the game's own force has it. On: "
                                 "Countersteer plays that pull, unfiltered, at the level below. Off: the steering "
                                 "force without it. On by default. Saved with the profile.")
    COUNTERSTEER_PULL_LEVEL_TOOLTIP = ("How much of the physical scrub radius the drive pull is played at, 0 to 100 %. "
                                       "100 % is the physically correct value and the default. Saved with the profile.")
    COUNTERSTEER_KICK_TOOLTIP = ("A short tug at the moment a kerb, a lifted inside wheel or a dirt edge hits one front "
                                 "wheel, in the direction the new drag pulls, easing back to nothing and never past "
                                 "it. On by default. Saved with the profile.")
    COUNTERSTEER_KICK_LEVEL_TOOLTIP = ("How strong the kickback's tug is, 0 to 100 % of its amplitude. 100 % is the "
                                       "physically correct value and the default. Saved with the profile.")
    COUNTERSTEER_GEAR_GUARD_TOOLTIP = ("Keeps the gears loaded on one side so small force reversals don't rattle the backlash. "
                                       "Fast detail stays; a real change of direction passes the gears slowly and then catches up.")
    COUNTERSTEER_GEAR_GUARD_LEVEL_TOOLTIP = ("How gently a real change of direction crosses the gears. Stronger is quieter but "
                                             "later: the figure beside it is the estimated delay of a big force reversal from "
                                             "the game to the wheel, red over 50 ms (a warning only), and the mark is where it "
                                             "reaches 50 ms. 61 % is the default; 0 is off.")
    COUNTERSTEER_GEAR_FLIP_TOOLTIP = ("How far the force must reverse before the gears are moved to the other side. Higher is "
                                      "quieter, but small direction changes arrive later.")
    COUNTERSTEER_GAME_TOOLTIP = ("Settings that only matter in one game. Each is named for its game and does nothing "
                                 "in the others.")
    COUNTERSTEER_TEXTURE_TOOLTIP = ("Small vibrations Countersteer adds on top of its steering force while it plays "
                                    "it: road texture from the front suspension, a jolt on landings and hits, a "
                                    "buzz while the front wheels lock under braking, and the engine's own "
                                    "vibration, which follows the revs (its pitch is the firing rate, strongest "
                                    "near idle and on the throttle, and it is there at a standstill, as in a real "
                                    "car). Kept very low on purpose: at 100 % they still stay under 8 % of the "
                                    "wheel's full force, the engine alone near 1 %. 30 % by default, every part "
                                    "on. Assetto Corsa Rally only for now. Saved with the profile.")
    COUNTERSTEER_TEXTURE_PARTS = (('road', "Road"), ('impact', "Landings"), ('lock', "Front lock"), ('engine', "Engine"))
    COUNTERSTEER_LIGHTNESS_TOOLTIP = ("Forza Horizon only. Forza keeps its own force feedback; Countersteer makes "
                                      "it lighter as the front tyres run out of grip (from the front slip in the "
                                      "game's telemetry), so understeer is felt in the wheel. 0 % leaves the game's "
                                      "force as it is; 100 % plays the whole drop, down to 40 % of the game's "
                                      "force at most, what a real steering keeps (its caster part). Never stronger than "
                                      "the game. 100 % by default. Saved with the "
                                      "profile.")

    COUNTERSTEER_STATUS_TOOLTIP = ("Read only. The light and its reason are measured live from what the game sends. "
                                   "The calibration line is what the wheel calibration measured. The learnt lines "
                                   "are what Countersteer works out for the car you are driving (the steering "
                                   "ratio from its yaw rate, the caster trail and scrub radius from its tyre forces, "
                                   "the column feel's scale for your wheel, and whether the "
                                   "car has power steering from its force against speed). Motor heat is the "
                                   "driver's own estimate; hands and live friction show once the driver measures them.")
    COUNTERSTEER_COLUMN_TOOLTIP = ("A real steering column has damping and friction that follow the car. On: "
                                   "Countersteer sends the car's own column damping and friction to the driver so "
                                   "the wheel carries its weight and settles like the car's. Off: only the "
                                   "steering force. On by default: it is what makes the feel one piece.")
    COUNTERSTEER_ALIGN_TOOLTIP = ("The self-aligning pull of the front tyres, played as a spring inside the driver "
                                  "instead of a force sent from here, so the wheel self-steers with about 4 ms "
                                  "response rather than the telemetry's 15 to 30 ms. Off: the telemetry plays it "
                                  "alone. On by default; turn it off if the centre feels nervous.")
    COUNTERSTEER_FRICTION_REMOVAL_TOOLTIP = ("Takes the wheel's own friction out of the feel: lighter than a real "
                                             "column, and the lightest forces get through. Off by default because "
                                             "a real car's column has that friction; turn it on if the wheel feels "
                                             "sticky.")

    def _page_list(self, page, title, subtitle=None):
        listbox = self._list()
        self._section(page, title, listbox, subtitle)
        return listbox

    def _build_countersteer_page(self):
        """The Countersteer FFB tab: status, steering, texture, game-specific settings and the wheel correction,
        in framed lists like the other tabs, after the Force Feedback tab."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        page.set_border_width(12)
        self._dependent_rows = []

        status = self._page_list(page, _("Status"))
        self.countersteer_page_light = Gtk.DrawingArea()
        self.countersteer_page_light.set_size_request(14, 14)
        self.countersteer_page_light.connect('draw', self._draw_countersteer_light)
        self.countersteer_page_light.connect('map', self._start_countersteer_poll)
        self.countersteer_page_status = self._status_label()
        status.add(self._setting_row(_("Countersteer FFB"), None, _(self.COUNTERSTEER_TOOLTIP),
                                     (self.countersteer_page_light,), self.countersteer_page_status))
        self.countersteer_lines = {}
        for key, title in (('calibration', _("Wheel calibration")), ('car', _("Learnt for this car")),
                           ('wheel', _("Wheel")), ('sensitivity', _("Steering sensitivity"))):
            self.countersteer_lines[key] = self._status_label()
            status.add(self._setting_row(title, None, _(self.COUNTERSTEER_STATUS_TOOLTIP), (),
                                         self.countersteer_lines[key]))

        steering = self._page_list(page, _("Steering"))
        def switched(state):
            self._show_countersteer_texture(state)
            self.controller.model.set_countersteer_ffb(state)
        row, self.countersteer_page_switch = self._switch_row(
            _("Countersteer FFB"), _(self.COUNTERSTEER_TOOLTIP), switched)
        self.countersteer_page_switch.set_sensitive(False)
        steering.add(row)
        strong = self._response_scale(0, 130, 10)
        strong.set_size_request(160, -1)
        strong.connect('value-changed',
                       lambda w: self.controller.model.set_countersteer_strength(int(w.get_value())))
        self.countersteer_strength = strong
        self._dependent_rows.append(self._setting_row(
            _("Steering strength"), None, _(self.COUNTERSTEER_STRENGTH_TOOLTIP), (self._response_row(strong),)))
        steering.add(self._dependent_rows[-1])
        knee = self._response_scale(30, 60, 5)
        knee.set_size_request(160, -1)
        knee.connect('value-changed', lambda w: self.controller.model.set_countersteer_knee(int(w.get_value())))
        self.countersteer_knee = knee
        self._dependent_rows.append(self._setting_row(
            _("1:1 up to"), None, _(self.COUNTERSTEER_KNEE_TOOLTIP), (self._response_row(knee),)))
        steering.add(self._dependent_rows[-1])
        cut = self._response_scale(15, 120, 5)
        cut.set_size_request(160, -1)
        cut.connect('value-changed', lambda w: self.controller.model.set_countersteer_rbr_cut(int(w.get_value())))
        self.countersteer_rbr_cut = cut
        cut.connect('value-changed', lambda w: self._show_countersteer_delay())
        self.countersteer_cut_delay = self._delay_label()
        self._dependent_rows.append(self._setting_row(
            _("RBR force cut"), None, _(self.COUNTERSTEER_RBR_CUT_TOOLTIP),
            (self._response_row(cut, percent=False), self.countersteer_cut_delay)))
        steering.add(self._dependent_rows[-1])
        caster = self._response_scale(0, 100, 5)
        caster.set_size_request(160, -1)
        caster.connect('value-changed', lambda w: self.controller.model.set_countersteer_caster_floor(int(w.get_value())))
        self.countersteer_caster_floor = caster
        self._dependent_rows.append(self._setting_row(
            _("Caster in slides"), None, _(self.COUNTERSTEER_CASTER_TOOLTIP), (self._response_row(caster),)))
        steering.add(self._dependent_rows[-1])
        feel = self._response_scale(0, 600, 10)
        feel.set_size_request(160, -1)
        feel.connect('value-changed', lambda w: self.controller.model.set_countersteer_engine_feel(int(w.get_value())))
        self.countersteer_engine_feel = feel
        self._dependent_rows.append(self._setting_row(
            _("Engine feel"), None, _(self.COUNTERSTEER_ENGINE_FEEL_TOOLTIP), (self._response_row(feel),)))
        steering.add(self._dependent_rows[-1])
        rim = Gtk.SpinButton.new_with_range(*wheel_torque.RIM_RANGE, 1)
        rim.set_value(wheel_torque.DEFAULT_RIM_MM)
        rim.connect('value-changed', lambda w: None if self.updating_countersteer_rim else
                    self.controller.model.set_countersteer_rim_mm(w.get_value_as_int()))
        self.countersteer_rim_mm = rim
        self._dependent_rows.append(self._setting_row(
            _("Your wheel's rim diameter (mm)"), None, _(self.COUNTERSTEER_RIM_TOOLTIP), (rim,)))
        steering.add(self._dependent_rows[-1])
        car = Gtk.SpinButton.new_with_range(*wheel_torque.CAR_WHEEL_RANGE, 1)
        car.set_value(wheel_torque.CAR_WHEEL_MM)
        car.connect('value-changed', lambda w: self.controller.model.set_countersteer_car_wheel_mm(w.get_value_as_int()))
        self.countersteer_car_wheel_mm = car
        self._dependent_rows.append(self._setting_row(
            _("Car's steering wheel diameter (mm)"), None, _(self.COUNTERSTEER_RIM_TOOLTIP), (car,)))
        steering.add(self._dependent_rows[-1])
        damping = self._response_scale(0, 100, 5)
        damping.set_size_request(160, -1)
        damping.connect('value-changed', lambda w: self.controller.model.set_countersteer_column_added(int(w.get_value())))
        self.countersteer_column_added = damping
        self._dependent_rows.append(self._setting_row(
            _("Column damping"), None, _(self.COUNTERSTEER_DAMPING_TOOLTIP), (self._response_row(damping),)))
        steering.add(self._dependent_rows[-1])
        row, self.countersteer_drive_pull = self._switch_row(
            _("Drive pull"), _(self.COUNTERSTEER_PULL_TOOLTIP), lambda state: self.controller.model.set_countersteer_drive_pull(state))
        self._dependent_rows.append(row)
        steering.add(row)
        pull = self._response_scale(0, 100, 5)
        pull.set_size_request(160, -1)
        pull.connect('value-changed', lambda w: self.controller.model.set_countersteer_drive_pull_level(int(w.get_value())))
        self.countersteer_drive_pull_level = pull
        self._dependent_rows.append(self._setting_row(
            _("Drive pull level"), None, _(self.COUNTERSTEER_PULL_LEVEL_TOOLTIP), (self._response_row(pull),)))
        steering.add(self._dependent_rows[-1])
        row, self.countersteer_kick = self._switch_row(
            _("Kickback"), _(self.COUNTERSTEER_KICK_TOOLTIP), lambda state: self.controller.model.set_countersteer_kick(state))
        self._dependent_rows.append(row)
        steering.add(row)
        kick = self._response_scale(0, 100, 5)
        kick.set_size_request(160, -1)
        kick.connect('value-changed', lambda w: self.controller.model.set_countersteer_kick_level(int(w.get_value())))
        self.countersteer_kick_level = kick
        self._dependent_rows.append(self._setting_row(
            _("Kickback level"), None, _(self.COUNTERSTEER_KICK_LEVEL_TOOLTIP), (self._response_row(kick),)))
        steering.add(self._dependent_rows[-1])
        row, self.countersteer_gear_guard = self._switch_row(
            _("Gear guard"), _(self.COUNTERSTEER_GEAR_GUARD_TOOLTIP), lambda state: self.controller.model.set_countersteer_gear_guard(state))
        self._dependent_rows.append(row)
        steering.add(row)
        guard = self._response_scale(0, 100, 1)
        guard.set_size_request(160, -1)
        guard.connect('value-changed', lambda w: self.controller.model.set_countersteer_gear_guard_level(int(w.get_value())))
        self.countersteer_gear_guard_level = guard
        guard.connect('value-changed', lambda w: self._show_countersteer_delay())
        self.countersteer_gear_delay = self._delay_label()
        self._gear_mark = None
        self._dependent_rows.append(self._setting_row(
            _("Gear guard level"), None, _(self.COUNTERSTEER_GEAR_GUARD_LEVEL_TOOLTIP),
            (self._response_row(guard), self.countersteer_gear_delay)))
        steering.add(self._dependent_rows[-1])
        flip = self._response_scale(3, 30, 1)
        flip.set_size_request(160, -1)
        flip.connect('value-changed', lambda w: self.controller.model.set_countersteer_gear_flip(int(w.get_value())))
        self.countersteer_gear_flip = flip
        flip.connect('value-changed', lambda w: self._show_countersteer_delay())
        self.countersteer_flip_delay = self._delay_label()
        self._dependent_rows.append(self._setting_row(
            _("Gear flip point"), None, _(self.COUNTERSTEER_GEAR_FLIP_TOOLTIP),
            (self._response_row(flip), self.countersteer_flip_delay)))
        steering.add(self._dependent_rows[-1])
        self.countersteer_toggles = {}
        for key, title, tooltip in (
                ('column', _("Real column feel"), self.COUNTERSTEER_COLUMN_TOOLTIP),
                ('align', _("Local aligning spring"), self.COUNTERSTEER_ALIGN_TOOLTIP),
                ('friction_removal', _("Friction removal"), self.COUNTERSTEER_FRICTION_REMOVAL_TOOLTIP)):
            row, switch = self._switch_row(
                title, _(tooltip), lambda state, key=key: self.controller.model.set_countersteer_dynamics('countersteer_' + key, state))
            self.countersteer_toggles[key] = switch
            self._dependent_rows.append(row)
            steering.add(row)

        texture = self._page_list(page, _("Texture"))
        scale = self._response_scale(0, 100, 10)
        scale.set_size_request(160, -1)
        scale.connect('value-changed', lambda w: self.controller.model.set_countersteer_texture(int(w.get_value())))
        self.countersteer_texture = scale
        self._dependent_rows.append(self._setting_row(_("Texture"), None, _(self.COUNTERSTEER_TEXTURE_TOOLTIP),
                                                      (self._response_row(scale),)))
        texture.add(self._dependent_rows[-1])
        boxes = []
        self.countersteer_texture_parts = {}
        for part, label in self.COUNTERSTEER_TEXTURE_PARTS:
            box = Gtk.CheckButton(label=_(label))
            box.connect('toggled', lambda w, part=part:
                        self.controller.model.set_countersteer_texture_part(part, w.get_active()))
            self.countersteer_texture_parts[part] = box
            boxes.append(box)
        self._dependent_rows.append(self._setting_row(_("Texture parts"), None, _(self.COUNTERSTEER_TEXTURE_TOOLTIP),
                                                      tuple(boxes)))
        texture.add(self._dependent_rows[-1])

        game = self._page_list(page, _("Game-specific"), _("Each setting below works in its own game only"))
        light = self._response_scale(0, 100, 10)
        light.set_size_request(160, -1)
        light.connect('value-changed',
                      lambda w: self.controller.model.set_countersteer_lightness(int(w.get_value())))
        self.countersteer_lightness = light

        self._build_wheel_calibration(self._page_list(page, _("Wheel correction")))

        self.main_notebook.insert_page(self._scrolled(page), Gtk.Label(label=_("Countersteer FFB")),
                                       self.COUNTERSTEER_TAB_POSITION)
        self._countersteer_page = page
        page.show_all()
        self._show_countersteer_texture(False)

    def _show_countersteer_texture(self, on):
        """The settings that act through Countersteer are greyed out while it is off."""
        for row in getattr(self, '_dependent_rows', ()):
            row.set_sensitive(bool(on))

    def set_countersteer_toggles(self, column, align, friction_removal):
        """Real column feel, local aligning spring and friction removal (None where the driver has no replace mode)."""
        if not hasattr(self, 'countersteer_toggles'):
            return
        for key, value in (('column', column), ('align', align), ('friction_removal', friction_removal)):
            switch = self.countersteer_toggles[key]
            if value is not None and switch.get_active() != bool(value):
                switch.set_active(bool(value))

    def set_countersteer_strength(self, value):
        """The overall steering strength, 0..130 (None where the driver has no replace mode)."""
        if not hasattr(self, 'countersteer_strength') or value is None:
            return
        if int(self.countersteer_strength.get_value()) != int(value):
            self.countersteer_strength.set_value(int(value))

    def set_countersteer_knee(self, value):
        """The "1:1 up to" share, 30..60 (None where the driver has no replace mode)."""
        if not hasattr(self, 'countersteer_knee') or value is None:
            return
        if int(self.countersteer_knee.get_value()) != int(value):
            self.countersteer_knee.set_value(int(value))

    def set_countersteer_rbr_cut(self, value):
        """The "RBR force cut" corner, 15..120 Hz (None where the driver has no replace mode)."""
        if not hasattr(self, 'countersteer_rbr_cut') or value is None:
            return
        if int(self.countersteer_rbr_cut.get_value()) != int(value):
            self.countersteer_rbr_cut.set_value(int(value))

    def set_countersteer_caster_floor(self, value):
        """The "Caster in slides" share, 0..100 (None where the driver has no replace mode)."""
        if not hasattr(self, 'countersteer_caster_floor') or value is None:
            return
        if int(self.countersteer_caster_floor.get_value()) != int(value):
            self.countersteer_caster_floor.set_value(int(value))

    def set_countersteer_engine_feel(self, value):
        """The "Engine feel" share, 0..600 (None where the driver has no replace mode)."""
        if not hasattr(self, 'countersteer_engine_feel') or value is None:
            return
        if int(self.countersteer_engine_feel.get_value()) != int(value):
            self.countersteer_engine_feel.set_value(int(value))

    updating_countersteer_rim = False

    def set_countersteer_rim_mm(self, value):
        """The wheel's rim diameter shown, mm (the table's for the wheel when the profile has none; None where the driver
        has no replace mode). Shown without saving it."""
        if not hasattr(self, 'countersteer_rim_mm') or value is None:
            return
        if int(self.countersteer_rim_mm.get_value()) != int(value):
            self.updating_countersteer_rim = True
            try:
                self.countersteer_rim_mm.set_value(int(value))
            finally:
                self.updating_countersteer_rim = False

    def set_countersteer_car_wheel_mm(self, value):
        """The car's steering wheel diameter, mm (None where the driver has no replace mode)."""
        if not hasattr(self, 'countersteer_car_wheel_mm') or value is None:
            return
        if int(self.countersteer_car_wheel_mm.get_value()) != int(value):
            self.countersteer_car_wheel_mm.set_value(int(value))

    def set_countersteer_column_added(self, value):
        """The column damping share, 0..100 (None where the driver has no replace mode)."""
        if not hasattr(self, 'countersteer_column_added') or value is None:
            return
        if int(self.countersteer_column_added.get_value()) != int(value):
            self.countersteer_column_added.set_value(int(value))

    def set_countersteer_drive_pull(self, value, level):
        """The drive pull switch and its level, 0..100 (None where the driver has no replace mode)."""
        if not hasattr(self, 'countersteer_drive_pull') or value is None:
            return
        if self.countersteer_drive_pull.get_active() != bool(value):
            self.countersteer_drive_pull.set_active(bool(value))
        if level is not None and int(self.countersteer_drive_pull_level.get_value()) != int(level):
            self.countersteer_drive_pull_level.set_value(int(level))

    def set_countersteer_kick(self, value, level):
        """The kickback switch and its level, 0..100 (None where the driver has no replace mode)."""
        if not hasattr(self, 'countersteer_kick') or value is None:
            return
        if self.countersteer_kick.get_active() != bool(value):
            self.countersteer_kick.set_active(bool(value))
        if level is not None and int(self.countersteer_kick_level.get_value()) != int(level):
            self.countersteer_kick_level.set_value(int(level))

    def set_countersteer_gear_guard(self, value, level):
        """The gear guard switch and its level, 0..100 (None where the driver has no replace mode)."""
        if not hasattr(self, 'countersteer_gear_guard') or value is None:
            return
        if self.countersteer_gear_guard.get_active() != bool(value):
            self.countersteer_gear_guard.set_active(bool(value))
        if level is not None and int(self.countersteer_gear_guard_level.get_value()) != int(level):
            self.countersteer_gear_guard_level.set_value(int(level))

    def set_countersteer_gear_flip(self, value):
        """The gear flip point, % of full scale (None where the driver has no replace mode)."""
        if not hasattr(self, 'countersteer_gear_flip') or value is None:
            return
        if int(self.countersteer_gear_flip.get_value()) != int(value):
            self.countersteer_gear_flip.set_value(int(value))

    def set_countersteer_texture(self, level, parts):
        """The texture's level (0..100, None where the driver has no replace mode) and parts."""
        if not hasattr(self, 'countersteer_texture'):
            return
        if level is not None and int(self.countersteer_texture.get_value()) != int(level):
            self.countersteer_texture.set_value(int(level))
        for part, box in self.countersteer_texture_parts.items():
            value = parts.get(part)
            if value is not None and box.get_active() != bool(value):
                box.set_active(bool(value))

    def set_countersteer_linearity(self, value):
        """No control any more: RBR plays through the shared chain; the profile key stays for the model."""
        return

    def set_countersteer_lightness(self, value):
        """Forza Horizon's understeer lightness, 0..100 (None where the driver has no replace mode)."""
        if not hasattr(self, 'countersteer_lightness') or value is None:
            return
        if int(self.countersteer_lightness.get_value()) != int(value):
            self.countersteer_lightness.set_value(int(value))

    def set_countersteer_ffb(self, value):
        """The switch: None where the driver has no replace mode (insensitive, the tooltip says why)."""
        if not hasattr(self, 'countersteer_switch'):
            return
        for switch in (self.countersteer_switch, self.countersteer_page_switch):
            switch.set_sensitive(value is not None)
            switch.set_tooltip_text(_(self.COUNTERSTEER_NO_DRIVER) if value is None else None)
            if value is not None and switch.get_active() != bool(value):
                switch.set_active(bool(value))
        self._show_countersteer_texture(bool(value))

    @staticmethod
    def _delay_label():
        """The "about N ms total" beside a slider that moves the estimated delay; the width keeps the track from jumping."""
        return Gtk.Label(label="", width_chars=17, xalign=0)

    def _show_countersteer_delay(self):
        """The estimated delay beside its sliders, red over the soft limit, and the gear guard level's "50 ms" mark. A warning
        only: nothing here touches a value."""
        if not hasattr(self, 'countersteer_gear_delay') or self.controller is None or getattr(self.controller, 'model', None) is None:
            return
        delay = self.controller.countersteer_delay()
        for label, shown in ((self.countersteer_gear_delay, delay['gear']), (self.countersteer_flip_delay, delay['gear']),
                             (self.countersteer_cut_delay, delay['rbr_cut'])):
            if label.get_text() != shown['text']:
                label.set_text(shown['text'])
            context = label.get_style_context()
            (context.add_class if shown['over'] else context.remove_class)('error')
        mark = delay['gear']['mark']
        mark = None if mark is None else round(mark, 1)
        if mark != self._gear_mark:
            self._gear_mark = mark
            scale = self.countersteer_gear_guard_level
            scale.clear_marks()
            if mark is not None:
                scale.add_mark(mark, Gtk.PositionType.BOTTOM, _("50 ms"))

    def _start_countersteer_poll(self, *args):
        if not self._countersteer_polling:
            self._countersteer_polling = True
            self._poll_countersteer()
            GLib.timeout_add(self.COUNTERSTEER_MS, self._poll_countersteer)

    def _poll_countersteer(self):
        if not (self.countersteer_light.get_mapped() or self.countersteer_page_light.get_mapped()):
            self._countersteer_polling = False
            return False
        if self.countersteer_page_light.get_mapped():
            for key, text in self.controller.countersteer_details().items():
                if self.countersteer_lines[key].get_text() != text:
                    self.countersteer_lines[key].set_text(text)
        self._show_countersteer_delay()
        warning = self.controller.sensitivity_warning()
        if self.sensitivity_note.get_text() != warning:
            self.sensitivity_note.set_text(warning)
            self.sensitivity_row.set_visible(bool(warning))
        shown = self.controller.countersteer_view()
        if shown != self._countersteer_shown:
            self._countersteer_shown = shown
            self.countersteer_status.set_text(shown[1])
            self.countersteer_page_status.set_text(shown[1])
            self.countersteer_light.queue_draw()
            self.countersteer_page_light.queue_draw()
        return True

    def _draw_countersteer_light(self, area, cr):
        size = min(area.get_allocated_width(), area.get_allocated_height())
        colour = self.LIGHT_COLOURS.get(self._countersteer_shown[0])
        cr.arc(size / 2, size / 2, size / 2 - 1, 0, 2 * math.pi)
        if colour is None:          # off: an empty ring
            fg = area.get_style_context().get_color(Gtk.StateFlags.NORMAL)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.35)
            cr.set_line_width(1.5)
            cr.stroke()
        else:
            cr.set_source_rgb(*colour)
            cr.fill()

    def reset_ffb_meter(self):
        if hasattr(self, 'ffb_meter'):
            self.ffb_meter.reset()
            self._show_ffb_clips()
            self.ffb_meter_bar.queue_draw()

    def _show_ffb_clips(self):
        meter = self.ffb_meter
        if meter.driver:
            seconds = meter.clipped * self.controller.read_ffb_tick_ms() / 1000
            self.ffb_clip_label.set_text(_("Clipped: %.1f s") % seconds)
        elif meter.clipped:
            self.ffb_clip_label.set_text(_("Clipped: about %d polls") % meter.clipped)
        else:
            self.ffb_clip_label.set_text(_("Clipped: none"))

    def _start_ffb_meter(self, *args):
        if self.ffb_poller.arm():
            GLib.timeout_add(self.FFB_METER_MS, self._poll_ffb_meter)

    def _poll_ffb_meter(self):
        if not self.ffb_poller.tick(time.monotonic()):
            return False
        self.ffb_meter_bar.queue_draw()
        self._show_ffb_clips()
        return True

    def _reset_ffb_clips(self, *args):
        self.ffb_meter.clear_clips()
        self._show_ffb_clips()

    def _draw_ffb_meter(self, area, cr):
        width = area.get_allocated_width()
        height = area.get_allocated_height()
        context = area.get_style_context()
        fg = context.get_color(Gtk.StateFlags.NORMAL)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.15)
        cr.rectangle(0, 0, width, height)
        cr.fill()
        fraction = self.ffb_meter.fraction
        if self.ffb_meter.over:
            cr.set_source_rgb(0.85, 0.2, 0.2)
        elif fraction >= 0.9:
            cr.set_source_rgb(0.9, 0.7, 0.1)
        else:
            cr.set_source_rgb(0.25, 0.65, 0.35)
        cr.rectangle(0, 0, width * fraction, height)
        cr.fill()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.9)
        cr.rectangle(min(width * self.ffb_meter.peak, width - 2), 0, 2, height)
        cr.fill()

    def _update_ffbmeter_overlay(self):
        if not self.overlay_window.props.visible or not self.ffbmeter_overlay.props.visible:
            self.ffbmeter_timer = False
            return False
        level = self.controller.read_ffbmeter('overlay')
        if level is None:
            return True
        if level < 2458: # < 7.5%
            led_states = 0
        elif level < 8192: # < 25%
            led_states = 1
        elif level < 16384: # < 50%
            led_states = 3
        elif level < 24576: # < 75%
            led_states = 7
        elif level < 29491: # < 90%
            led_states = 15
        elif level <= 32768: # <= 100%
            led_states = 31
        elif level < 36045: # < 110%
            led_states = 30
        elif level < 40960: # < 125%
            led_states = 28
        elif level < 49152: # < 150%
            led_states = 24
        else:
            led_states = 16
        self.overlay_led_0.set_value(led_states & 1)
        self.overlay_led_1.set_value((led_states >> 1) & 1)
        self.overlay_led_2.set_value((led_states >> 2) & 1)
        self.overlay_led_3.set_value((led_states >> 3) & 1)
        self.overlay_led_4.set_value((led_states >> 4) & 1)
        return True

    def _round_input(self, value, decimals = 0):
        multiplier = 10 ** decimals
        return math.floor(value * multiplier) / multiplier

    def show_test_chart(self, canvas, toolbar):
        if self.current_test_canvas is not None:
            self.test_chart_frame.remove(self.current_test_canvas)
        if self.current_test_toolbar is not None:
            self.test_chart_container.remove(self.current_test_toolbar)
        self.current_test_canvas = canvas
        self.current_test_toolbar = toolbar
        self.test_chart_container.pack_start(toolbar, False, False, 0)
        self.test_chart_frame.add(canvas)
        self.test_chart_window.show_all()
        self.test_chart_window.show()
        self.test_open_chart_button.set_sensitive(False)

    def _screen_changed(self, widget, old_screen, userdata=None):
        screen = self.overlay_window.get_screen()
        visual = screen.get_rgba_visual()
        self.overlay_window.set_visual(visual)

    HOTKEYS_TAB_POSITION = 3        # after Tools (Telemetry then goes in front of it)
    TELEMETRY_TAB_POSITION = 3
    COUNTERSTEER_TAB_POSITION = 2   # after Force Feedback

    @staticmethod
    def _row_of(widget):
        while widget is not None and not isinstance(widget, Gtk.ListBoxRow):
            widget = widget.get_parent()
        return widget

    @staticmethod
    def _detach(widget):
        """Take a widget built by main.ui out of its box, to place it
        elsewhere; its signals stay connected."""
        parent = widget.get_parent()
        if parent is not None:
            parent.remove(widget)
        return widget

    @staticmethod
    def _heading(text, subtitle=None):
        """A section heading above a framed list, as a box: the title in
        bold, then a dim line under it when there is one."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(2)
        title = Gtk.Label(xalign=0)
        title.set_markup('<b>{}</b>'.format(GLib.markup_escape_text(text)))
        box.pack_start(title, False, False, 0)
        if subtitle:
            line = Gtk.Label(label=subtitle, xalign=0)
            line.set_line_wrap(True)
            line.get_style_context().add_class('dim-label')
            box.pack_start(line, False, False, 0)
        return box

    @staticmethod
    def _list():
        """A framed list like the other tabs' settings."""
        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.set_activate_on_single_click(False)
        listbox.get_style_context().add_class('list')
        return listbox

    def _section(self, parent, title, listbox, subtitle=None):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.pack_start(self._heading(title, subtitle), False, False, 0)
        box.pack_start(listbox, False, False, 0)
        parent.pack_start(box, False, False, 0)
        return box

    SETTING_ROW_HEIGHT = 70             # as the rows of the other tabs

    def _setting_row(self, title, subtitle=None, tooltip=None, controls=(), status=None):
        """A settings row as in the other tabs: the title (and a dim
        explanation or status under it) on the left, the controls on the
        right. `status` is a label to show under the title instead of a
        fixed subtitle. Returns the row."""
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        row.set_size_request(-1, self.SETTING_ROW_HEIGHT)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        if tooltip:
            box.set_tooltip_text(tooltip)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        label = Gtk.Label(label=title, xalign=0)
        label.set_line_wrap(True)
        text.pack_start(label, False, False, 0)
        if subtitle and status is None:
            status = self._status_label()
            status.set_text(subtitle)
        if status is not None:
            text.pack_start(status, False, False, 0)
        box.pack_start(text, True, True, 0)
        end = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, valign=Gtk.Align.CENTER)
        for control in controls:
            control.set_valign(Gtk.Align.CENTER)
            end.pack_start(control, False, False, 0)
        box.pack_end(end, False, False, 0)
        row.add(box)
        return row

    def _switch_row(self, text, tooltip, handler, subtitle=None, status=None, extra=()):
        switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        switch.connect('state-set', lambda w, state: handler(state) or False)
        return self._setting_row(text, subtitle, tooltip, tuple(extra) + (switch,), status), switch

    def _build_telemetry_page(self):
        """The Telemetry tab (docs/telemetry-coaching.md, section 12; the
        sub-tabs of docs/telemetry-ui-design.md), in three views:
        "Coaching" (the car, the telemetry arriving now, its learnt shift
        points, coaching, sessions and setup), "Telemetry" (the live dash)
        and "Settings" (receiving telemetry, the rev lights, moved from
        Tools, the web page and recording), in framed lists like the other
        tabs. The splits row sits above Coaching and Telemetry."""
        self._telemetry_history = None
        self._telemetry_advice_lines = []
        self._telemetry_rows = None
        self._telemetry_coaching_shown = None
        self._splits_shown = None
        self._dash = None
        self._dash_last = None
        self._dash_last_at = 0.0

        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        stack.add_titled(self._telemetry_overview(), 'coaching', _("Coaching"))
        stack.add_titled(self._telemetry_pane(), 'telemetry', _("Telemetry"))
        stack.add_titled(self._telemetry_settings_view(), 'settings', _("Settings"))
        self.telemetry_stack = stack
        switcher = Gtk.StackSwitcher(stack=stack, halign=Gtk.Align.CENTER)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        page.set_margin_top(12)
        switcher.set_margin_bottom(4)
        page.pack_start(switcher, False, False, 0)
        page.pack_start(self._build_splits_row(), False, False, 0)
        page.pack_start(stack, True, True, 0)
        # The splits are about the stage, not about the settings
        stack.connect('notify::visible-child-name', lambda s, p: self._show_splits_row())

        self.main_notebook.insert_page(page, Gtk.Label(label=_("Telemetry")), self.TELEMETRY_TAB_POSITION)
        self.main_notebook.connect('switch-page', lambda notebook, child, index:
                                   self.controller.telemetry_tab_selected() if child is page else None)
        self._telemetry_page = page
        # What it shows until the controller has read anything
        self.set_telemetry_history({'context': (_("No session recorded yet."), []), 'tips': [], 'tuning': [],
                                    'sessions': [], 'last_session': None})
        self.set_telemetry_view(live_status(None), None)

    @staticmethod
    def _scrolled(child):
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.add(child)
        return scrolled

    def _telemetry_overview(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        page.set_border_width(12)
        page.get_style_context().add_class('telemetry-page')

        # The car shown, and what is arriving now
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        car = Gtk.Label(label=_("Car"))
        car.set_margin_end(4)
        bar.pack_start(car, False, False, 0)
        self.telemetry_car = Gtk.ComboBoxText()
        self.telemetry_car.set_tooltip_text(_("Cars learnt in this profile. The car being driven is shown "
                                              "unless you pick another."))
        self.telemetry_car_handler = self.telemetry_car.connect(
            'changed', lambda w: self.controller.select_telemetry_car(w.get_active_id()))
        bar.pack_start(self.telemetry_car, True, True, 0)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        buttons.get_style_context().add_class('linked')
        buttons.set_sensitive(False)                # until there is a car
        self.telemetry_car_buttons = buttons
        rename = Gtk.Button(label=_("Rename…"))
        rename.set_tooltip_text(_("Give this car the name you know it by"))
        rename.connect('clicked', lambda w: self._rename_car())
        buttons.pack_start(rename, False, False, 0)
        forget = Gtk.Button(label=_("Forget…"))
        forget.set_tooltip_text(_("Throw away what has been learnt about this car and start again"))
        forget.connect('clicked', lambda w: self._forget_car())
        buttons.pack_start(forget, False, False, 0)
        bar.pack_start(buttons, False, False, 0)
        top.pack_start(bar, False, False, 0)

        live = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        live.set_margin_start(2)
        self.telemetry_live_dot = Gtk.Label(label='●')
        self.telemetry_live_dot.get_style_context().add_class('telemetry-dot')
        live.pack_start(self.telemetry_live_dot, False, False, 0)
        self.telemetry_live = Gtk.Label(xalign=0)
        self.telemetry_live.set_line_wrap(True)
        self.telemetry_live.set_selectable(True)
        live.pack_start(self.telemetry_live, True, True, 0)
        top.pack_start(live, False, False, 0)
        page.pack_start(top, False, False, 0)

        # The learnt shift points
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        heading = self._heading(_("Shift points"))
        self.telemetry_summary = Gtk.Label(xalign=0)
        self.telemetry_summary.set_line_wrap(True)
        self.telemetry_summary.get_style_context().add_class('dim-label')
        heading.pack_start(self.telemetry_summary, False, False, 0)
        box.pack_start(heading, False, False, 0)
        frame = Gtk.Frame()
        frame.get_style_context().add_class('telemetry-table')
        self.telemetry_gears = Gtk.Grid(column_spacing=20, row_spacing=0)
        self.telemetry_gears.set_border_width(12)
        table = Gtk.ScrolledWindow()
        table.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        table.set_propagate_natural_height(True)
        table.add(self.telemetry_gears)
        frame.add(table)
        self.telemetry_gears_frame = frame
        frame.set_no_show_all(True)
        box.pack_start(frame, False, False, 0)
        page.pack_start(box, False, False, 0)

        # Coaching: one row per tip, its kind as a tag
        self.telemetry_coaching = self._list()
        self._section(page, _("Coaching"), self.telemetry_coaching)

        # The last session, why Oversteer thinks it was that, and the ones before
        sessions = self._list()
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        grid = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        grid.set_margin_top(10)
        grid.set_margin_bottom(10)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        self.telemetry_context = Gtk.Label(xalign=0)
        self.telemetry_context.set_line_wrap(True)
        self.telemetry_context.set_selectable(True)
        column.pack_start(self.telemetry_context, False, False, 0)
        self.telemetry_evidence = self._status_label()
        expander = Gtk.Expander(label=_("Why Oversteer thinks so"))
        expander.get_style_context().add_class('dim-label')
        expander.add(self.telemetry_evidence)
        expander.set_no_show_all(True)
        self.telemetry_evidence_expander = expander
        column.pack_start(expander, False, False, 0)
        grid.pack_start(column, True, True, 0)
        label = Gtk.Button(label=_("Label…"), valign=Gtk.Align.CENTER)
        label.set_tooltip_text(_("Say what the last session was (discipline, surface, wet, shifter): your labels "
                                 "are kept for Oversteer to learn to tell surfaces apart from"))
        label.connect('clicked', lambda w: self._label_session())
        self.telemetry_label_button = label
        grid.pack_end(label, False, False, 0)
        row.add(grid)
        sessions.add(row)
        self.telemetry_sessions = sessions
        self._section(page, _("Sessions"), sessions)

        # The setup
        self.telemetry_tuning = self._list()
        self._section(page, _("Setup"), self.telemetry_tuning)
        return self._scrolled(page)

    def _telemetry_pane(self):
        """The Telemetry view: Live (the dash) and Run (a run against the PB: gtk_run_view) under a switcher."""
        from .gtk_run_view import RunView
        inner = Gtk.Stack()
        inner.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        inner.add_titled(self._telemetry_live_view(), 'live', _("Live"))
        self.telemetry_run = RunView()
        inner.add_titled(self.telemetry_run, 'run', _("Run"))
        self.telemetry_inner_stack = inner
        switcher = Gtk.StackSwitcher(stack=inner, halign=Gtk.Align.CENTER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.pack_start(switcher, False, False, 0)
        box.pack_start(inner, True, True, 0)
        inner.connect('notify::visible-child-name', lambda s, p: self._show_run_view())
        box.connect('map', lambda w: self._show_run_view())
        return box

    def _show_run_view(self):
        if self.telemetry_inner_stack.get_visible_child_name() == 'run':
            self.telemetry_run.refresh()

    def set_run_source(self, reader, profile, car_id):
        """Where the Run view reads runs from (a callable giving a read-only Reader), set by the controller
        with the history it reads."""
        self.telemetry_run.set_source(reader, profile, car_id)
        self._show_run_view()

    def show_run(self, run_id, distance=None, vs='pb'):
        """Open Telemetry > Run on a run, zoomed to `distance` m (a debrief call's place)."""
        self.telemetry_stack.set_visible_child_name('telemetry')
        self.telemetry_inner_stack.set_visible_child_name('run')
        self.telemetry_run.open_run(run_id, vs, True, distance)

    def _telemetry_live_view(self):
        """The Live view: the dash and its delta block on the left; the pedal and steering strips, g-g and the
        stage minimap on the right (gtk_live_view), on the web page's visual system. Its data is read in process
        from the live run's buffer by a timer that runs while the view is shown."""
        from .gtk_live_view import LiveView
        self.telemetry_live_view = LiveView(
            read=lambda since: self.controller.shift_learner.live_run.read(since),
            found=lambda: self._splits_shown, on_debrief=lambda: self.telemetry_stack.set_visible_child_name('coaching'),
            on_ribbon=lambda: self.splits_ribbon.queue_draw(), calls=self._debrief_calls,
            on_open_run=lambda run_id: self.show_run(run_id))
        self.telemetry_dash = self.telemetry_live_view.dash_area
        self.telemetry_dash.connect('draw', self._draw_dash)
        return self._scrolled(self.telemetry_live_view)

    def _debrief_calls(self):
        """'3 calls' for the finished card, from the debrief's own count."""
        history = self._telemetry_history or {}
        tips = [t for t in history.get('tips') or [] if (t.get('kind') if isinstance(t, dict) else getattr(t, 'kind', None)) != 'still']
        if not tips:
            return ''
        return _("1 call") if len(tips) == 1 else _("{} calls").format(len(tips))

    DASH_PAUSE = 30.0                   # s a pause keeps the last values (dimmed) before the dash empties

    def _draw_dash(self, area, cr):
        telemetry_plot.dash(cr, area.get_allocated_width(), area.get_allocated_height(), self._dash, flash=True)
        return False

    def _set_dash(self, live):
        """`live`: web.live_dict() of the sample arriving now, or None. A pause shows the last values dimmed."""
        import time
        now = time.monotonic()
        data = None
        if live and live.get('rpm') is not None:
            def num(x):
                return x if isinstance(x, (int, float)) and math.isfinite(x) else None
            km = ''
            distance, length = num(live.get('distance')), num(live.get('stage_length'))
            if distance is not None and length:
                km = _("{:.1f} / {:.1f} km").format(max(0.0, distance) / 1000.0, length / 1000.0)
            elif distance is not None:
                km = _("{:.1f} km").format(max(0.0, distance) / 1000.0)
            share = num(live.get('progress'))
            if share is None and distance is not None and length:
                share = distance / length
            speed = num(live.get('speed'))
            data = {'gear': live.get('gear'), 'speed': None if speed is None else speed * 3.6, 'rpm': num(live['rpm']) or 0.0,
                    'shift_rpm': num(live.get('shift_rpm')), 'learnt': live.get('learnt'), 'limiter': num(live.get('limiter')),
                    'progress': share, 'km': km, 'title': _("Stage") if km else _("Driving")}
            self._dash_last, self._dash_last_at = data, now
        elif self._dash_last is not None and now - self._dash_last_at < self.DASH_PAUSE:
            data = dict(self._dash_last, dim=True, note=_("paused {:.0f} s").format(now - self._dash_last_at))
        else:
            self._dash_last = None
            data = {'gear': None, 'speed': None, 'rpm': 0.0, 'dim': True, 'title': _("No telemetry arriving")}
        if data != self._dash:
            self._dash = data
            self.telemetry_dash.queue_draw()

    def _telemetry_settings_view(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        page.set_border_width(12)
        page.get_style_context().add_class('telemetry-page')

        # The rev light widgets are built by main.ui (their handlers are
        # there); their row is taken out of Tools and the widgets placed
        # in rows of their own here
        tooltips = {}
        for widget in (self.rev_leds, self.launch_options):
            row = self._row_of(widget)
            if row is None:
                continue
            tooltips[widget] = row.get_child().get_tooltip_text()
            for child in row.get_child().get_children():
                if isinstance(child, Gtk.Label) and child.get_tooltip_text():
                    tooltips[child.get_text()] = child.get_tooltip_text()
            row.get_parent().remove(row)
        for widget in (self.rev_leds, self.rev_leds_status, self.rev_leds_port, self.rev_leds_shift,
                       self.rev_leds_shift_unit, self.rev_leds_test, self.launch_options, self.launch_options_copy):
            self._detach(widget)
        self.rev_leds_status.set_line_wrap(True)
        self.rev_leds_status.set_xalign(0)
        self.rev_leds_status.set_halign(Gtk.Align.FILL)
        self.rev_leds_status.set_max_width_chars(80)
        self.rev_leds_status.set_selectable(True)

        receive = self._list()
        receive.add(self._setting_row(
            _("UDP port"), _("Where games send their telemetry. Saved with the profile."),
            _("Auto (the default) listens on every port in the list and takes the first one that brings a "
              "game's telemetry; another port is used only once that one has been quiet for a few seconds. "
              "It only looks at 5300 (where older setups send) now and then, as Forza Horizon 6 may need "
              "that port: move such a game to 5310. A port another app holds is tried again every 30 s; "
              "SimHub, which needs 20777, wants one port picked here instead of Auto. Turn on the game's UDP telemetry and point it at this "
              "computer at its usual port (Forza: Settings > HUD and gameplay > Data Out; BeamNG: OutGauge; "
              "Live for Speed: OutGauge Port in cfg.txt; DiRT Rally 2.0: hardware_settings_config.xml, "
              "extradata 3; Richard Burns Rally (RSF): its launcher's Telemetry page sends to 6776; add "
              "127.0.0.1:5310 or leave it, Auto listens on both). Forza has no fixed port: set Data Out to the Oversteer default and keep "
              "away from 5200 to 5300, which the game uses itself. Pick one port to listen there alone, or "
              "type any port from 1024 to 65535."), (self.rev_leds_port,)))
        row, self.telemetry_learn = self._switch_row(
            _("Learn from game telemetry"),
            _("Listen to the game's telemetry and learn from it even with the rev lights off or on a wheel "
              "without them. Uses the UDP port above. An app setting, for every profile."),
            lambda state: self.controller.set_telemetry_learn(state),
            subtitle=_("Even with the rev lights off or no wheel. For every profile."))
        receive.add(row)
        self.launch_options.set_width_chars(28)
        self.launch_options.set_hexpand(False)
        receive.add(self._setting_row(
            _("Shared-memory games (Assetto Corsa family)"), _("Put this in the game's Steam launch options."),
            tooltips.get(self.launch_options), (self.launch_options, self.launch_options_copy)))
        receive.add(self._eawrc_setup())
        self.install_beamng = install_beamng = Gtk.Button(label=_("Install the Countersteer mod for BeamNG"))
        install_beamng.connect('clicked', lambda w: self.controller.install_beamng_mod())
        receive.add(self._setting_row(
            _("BeamNG.drive force feedback"),
            _("Adds a small mod to BeamNG's mods folder that sends the steering rack force and each tyre's load "
              "and slip on port 5320. Groundwork for now: the receiver arrives with Countersteer FFB, and the "
              "telemetry above keeps using OutGauge. Safe to press again: it never replaces a newer version."),
            controls=(install_beamng,)))
        self.telemetry_forward = Gtk.Entry(width_chars=34, placeholder_text="192.168.1.20:20777, localhost:9996")
        self.telemetry_forward.connect('activate', self._forward_commit)
        self.telemetry_forward.connect('focus-out-event', lambda w, e: self._forward_commit(w) and False)
        self.telemetry_forward.connect('changed', lambda w: self._forward_error(None))
        receive.add(self._setting_row(
            _("Forward to"), _("Also send every telemetry packet, unchanged, to up to 3 other places, as "
                               "host:port separated by commas (SimHub on another PC, a dashboard, motion software). "
                               "Empty: off. An app setting, for every profile."),
            _("Oversteer holds the game's UDP port, so other tools cannot listen on it. Type where they listen, "
              "for example 192.168.1.20:20777 or mypc:20777. Host names are looked up once, in the background, when you press "
              "Enter. IPv4 only. This computer's own Oversteer ports are refused: that would loop."),
            (self.telemetry_forward,)))
        self._section(page, _("Receiving telemetry"), receive)

        leds = self._list()
        leds.add(self._setting_row(_("Rev lights from game telemetry"), tooltip=tooltips.get(self.rev_leds),
                                   controls=(self.rev_leds_test, self.rev_leds), status=self.rev_leds_status))
        leds.add(self._setting_row(
            _("Shift at"), _("Auto: each gear's best upshift once learnt, 95 % of the limiter until then. "
                             "Or a fixed point, as % of the limiter or in RPM."),
            tooltips.get(_("Shift at")), (self.rev_leds_shift, self.rev_leds_shift_unit)))
        row, self.rev_leds_launch = self._switch_row(
            _("Learn the limiter at each launch"),
            _("A rally stage starts with the clutch in, handbrake up and throttle floored, which holds "
              "the engine on its limiter. Held for a second standing, that RPM becomes the car's maximum for "
              "the % shift point, whatever the game reports. Learnt again at every start."),
            lambda state: self.controller.model.set_rev_leds_launch(state),
            subtitle=_("Clutch in, handbrake up, throttle floored for a second at the start."))
        leds.add(row)
        row, self.rev_leds_learnt = self._switch_row(
            _("Shift lights at the learnt best upshift"),
            _("Once Oversteer has learnt the car's power curve and gearing, the lights complete at the "
              "rpm where the next gear starts pulling harder, gear by gear. Until then, and for gears "
              "it doesn't know yet, the shift point above is used."),
            lambda state: self.controller.model.set_rev_leds_learnt(state),
            subtitle=_("Gear by gear, once known; the shift point above until then."))
        # Now the Auto choice of Shift at; kept (not shown) for its setter
        self._section(page, _("Rev lights"), leds, _("Saved with the profile."))

        web = self._list()
        self.telemetry_web_status = self._status_label()
        row, self.telemetry_web = self._switch_row(
            _("Web page for a phone or another computer"),
            _("A read-only page with the live gear, the shift tables, coaching and your history, on the TCP "
              "port below. Off by default."),
            lambda state: self.controller.set_telemetry_web(on=state), status=self.telemetry_web_status)
        web.add(row)
        self.telemetry_web_port = Gtk.SpinButton.new_with_range(1024, 65535, 1)
        self.telemetry_web_port.connect('value-changed', lambda w: self.controller.set_telemetry_web(
            port=w.get_value_as_int()))
        self.telemetry_web_bind = Gtk.ComboBoxText()
        self.telemetry_web_bind.append('lan', _("Every network"))
        self.telemetry_web_bind.append('local', _("This computer only"))
        self.telemetry_web_bind.connect('changed', lambda w: w.get_active_id() and self.controller.set_telemetry_web(
            bind=w.get_active_id()))
        web.add(self._setting_row(_("TCP port and who can open it"),
                                  controls=(self.telemetry_web_port, self.telemetry_web_bind)))
        row, self.telemetry_web_control = self._switch_row(
            _("Allow changes from the web page"),
            _("Lets a browser paired with the code below change the wheel's settings (range, pedals, force "
              "feedback, rev lights, profiles) from the Controls and FFB tabs of the web page. Anyone who "
              "has the code can do it, and the page is plain HTTP: pair only on a network you trust."),
            lambda state: self.controller.set_telemetry_web_control(state),
            subtitle=_("Off by default. A phone must be paired with the code below first."))
        web.add(row)
        self.web_pair_link = None
        self.web_pair_text = self._status_label()
        self.web_pair_qr = Gtk.DrawingArea()
        self.web_pair_qr.set_size_request(150, 150)
        self.web_pair_qr.connect('draw', self._draw_pair_qr)
        self.web_pair_copy = Gtk.Button(label=_("Copy link"))
        self.web_pair_copy.connect('clicked', lambda w: self.copy_web_pairing())
        self.web_pair_new = Gtk.Button(label=_("New code"))
        self.web_pair_new.set_tooltip_text(_("Unpairs every browser paired so far."))
        self.web_pair_new.connect('clicked', lambda w: self.controller.new_web_token())
        self.web_pair_row = self._setting_row(
            _("Pair a phone or another computer"), status=self.web_pair_text,
            controls=(self.web_pair_qr, self.web_pair_copy, self.web_pair_new))
        self.web_pair_row.set_no_show_all(True)
        web.add(self.web_pair_row)
        self._section(page, _("Web page"), web, _("For every profile."))

        record = self._list()
        self.telemetry_capture_status = self._status_label()
        row, self.telemetry_capture = self._switch_row(
            _("Record raw telemetry"),
            _("Keep everything the game sends, as it arrived, in capture files: Oversteer can learn from them "
              "again when it improves, and one attached to a bug report shows what happened. Labelled "
              "sessions are kept for it to learn to tell surfaces apart from. Off by default."),
            lambda state: self.controller.set_telemetry_capture(on=state), status=self.telemetry_capture_status)
        record.add(row)
        row, self.telemetry_ffb_tuning = self._switch_row(
            _("Record for FFB tuning"),
            _("Save everything at the full rate the game sends it (several times the usual size), for tuning the "
              "force feedback. It stops by itself after {} minutes or {} MB, then recording goes back to the "
              "usual rate; those captures are never deleted for space. Works only while recording is on, and "
              "is remembered between starts.").format(round(FFB_MAX_SECONDS / 60), round(FFB_MAX_BYTES / 1e6)),
            lambda state: self._ffb_syncing or self.controller.set_ffb_tuning(state))
        self._ffb_syncing = False
        self.telemetry_ffb_tuning.set_sensitive(False)
        record.add(row)
        self.telemetry_capture_cap = Gtk.SpinButton.new_with_range(1, 100, 1)
        self.telemetry_capture_cap.connect('value-changed', lambda w: self.controller.set_telemetry_capture(
            cap=w.get_value_as_int()))
        record.add(self._setting_row(_("Keep at most"), _("GB of captures: the oldest go first."),
                                     controls=(self.telemetry_capture_cap,)))
        self._section(page, _("Recording"), record, _("For every profile."))

        diag = self._list()
        self.diagnostics_status = self._status_label()
        row, self.diagnostics_debug = self._switch_row(
            _("Detailed log"),
            _("Write debug lines to the log too (for chasing a problem; the log grows faster). Off: information, "
              "warnings and errors."),
            lambda state: self.controller.set_diagnostics_debug(state))
        diag.add(row)
        open_logs = Gtk.Button(label=_("Open log folder"))
        open_logs.connect('clicked', lambda w: self.controller.open_log_folder())
        bundle = Gtk.Button(label=_("Save a diagnostics bundle…"))
        bundle.connect('clicked', lambda w: self.controller.save_diagnostics_bundle())
        diag.add(self._setting_row(
            _("Logs and bundle"), status=self.diagnostics_status, controls=(open_logs, bundle),
            tooltip=_("The bundle is a zip of the logs, the latest captures with the wheel's inputs, the versions "
                      "and the settings (no database, no pairing code): attach it to a bug report.")))
        self._section(page, _("Diagnostics"), diag, _("For every profile."))
        return self._scrolled(page)

    @staticmethod
    def _status_label():
        """A dim, wrapping, selectable line of text under a setting."""
        label = Gtk.Label(xalign=0)
        label.set_line_wrap(True)
        label.set_selectable(True)
        label.set_max_width_chars(80)
        label.get_style_context().add_class('dim-label')
        return label

    def _forward_error(self, message):
        entry = self.telemetry_forward
        context = entry.get_style_context()
        if message:
            context.add_class('error')
            entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, 'dialog-error-symbolic')
            entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, message)
        else:
            context.remove_class('error')
            entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, None)

    def show_forward_error(self, message):
        self._forward_error(message)

    def _forward_commit(self, entry):
        """Enter or leaving the box: apply the text, or show why it is refused (a host name that
        cannot be found or a loop is reported a moment later, by show_forward_error)."""
        error = self.controller.set_telemetry_forward(entry.get_text())
        if error:
            self._forward_error(error)
        return False

    def telemetry_tab_visible(self):
        page = self.main_notebook.get_nth_page(self.main_notebook.get_current_page())
        return page is self._telemetry_page and self.window.is_active()

    def set_telemetry_preferences(self, prefs):
        """The app-wide telemetry settings (telemetry_view.PREFERENCES),
        shown without writing them back."""
        # The handlers see the values the controller already has: no write
        self.telemetry_learn.set_active(prefs['telemetry_learn'])
        self.telemetry_forward.set_text(prefs['telemetry_forward'])
        self.telemetry_web.set_active(prefs['telemetry_web_on'])
        self.telemetry_web_port.set_value(prefs['telemetry_web_port'])
        self.telemetry_web_bind.set_active_id(prefs['telemetry_web_bind'])
        self.telemetry_web_control.set_active(prefs['telemetry_web_control'])
        self.telemetry_capture.set_active(prefs['telemetry_capture_on'])
        self.telemetry_capture_cap.set_value(prefs['telemetry_capture_cap'])
        self.diagnostics_debug.set_active(prefs['diagnostics_debug'])

    def set_telemetry_web_status(self, text):
        self.telemetry_web_status.set_text(text)

    def set_web_pairing(self, link, token, others=()):
        """The link (with ?pair=) a browser opens to be paired, or None when
        changes from the web page are off or the page is not served. The
        code is shown too, for typing into a page that was opened without it."""
        self.web_pair_link = link
        if link is None:
            self.web_pair_row.hide()
            self.web_pair_row.set_no_show_all(True)     # the window's show_all must not bring it back
            return
        text = _("Scan the code with the phone's camera or open\n{}\nPairing code: {}").format(link, token)
        if others:
            text += "\n" + _("Other addresses of this computer:") + "\n" + "\n".join(others)
        self.web_pair_text.set_text(text)
        try:
            self._web_pair_modules = qr.encode(link)
        except ValueError:
            self._web_pair_modules = None
        # show_all() skips a widget that is itself no_show_all: lift that first
        self.web_pair_row.set_no_show_all(False)
        self.web_pair_row.show_all()
        self.web_pair_qr.set_visible(self._web_pair_modules is not None)
        self.web_pair_qr.queue_draw()

    def copy_web_pairing(self):
        if self.web_pair_link:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(self.web_pair_link, -1)

    def _draw_pair_qr(self, area, cr):
        modules = getattr(self, '_web_pair_modules', None)
        if not modules:
            return False
        count = len(modules)
        width, height = area.get_allocated_width(), area.get_allocated_height()
        unit = min(width, height) // (count + 8)             # four modules of quiet zone round it
        side = unit * (count + 8)
        left, top = (width - side) / 2, (height - side) / 2
        cr.set_source_rgb(1, 1, 1)
        cr.rectangle(left, top, side, side)
        cr.fill()
        cr.set_source_rgb(0, 0, 0)
        for y, row in enumerate(modules):
            for x, dark in enumerate(row):
                if dark:
                    cr.rectangle(left + (x + 4) * unit, top + (y + 4) * unit, unit, unit)
        cr.fill()
        return False

    def set_diagnostics_status(self, text):
        self.diagnostics_status.set_text(text)

    def open_folder(self, path):
        try:
            Gtk.show_uri_on_window(self.window, GLib.filename_to_uri(path, None), Gdk.CURRENT_TIME)
        except GLib.Error as e:
            logging.warning("open %s: %s", path, e.message)

    def set_telemetry_capture_status(self, text):
        self.telemetry_capture_status.set_text(text)

    def set_ffb_tuning_switch(self, active, sensitive):
        """The "Record for FFB tuning" switch: shown on or off, usable while recording is on."""
        self.telemetry_ffb_tuning.set_sensitive(sensitive)
        if self.telemetry_ffb_tuning.get_active() != active:
            self._ffb_syncing = True          # not the user's doing: the controller is not told
            try:
                self.telemetry_ffb_tuning.set_active(active)
            finally:
                self._ffb_syncing = False

    @staticmethod
    def _clear_rows(listbox, keep=0):
        for row in listbox.get_children()[keep:]:
            row.destroy()

    @staticmethod
    def _text_row(text, dim=False, markup=False):
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        label = Gtk.Label(xalign=0)
        if markup:
            label.set_markup(text)
        else:
            label.set_text(text)
        label.set_line_wrap(True)
        label.set_selectable(True)
        label.set_margin_top(10)
        label.set_margin_bottom(10)
        if dim:
            label.get_style_context().add_class('dim-label')
        row.add(label)
        return row

    def set_telemetry_history(self, history):
        """The sections read from the database (telemetry_view.gather())."""
        self._telemetry_history = history
        context, evidence = history['context']
        self.telemetry_context.set_text(context)
        self.telemetry_label_button.set_sensitive(history.get('last_session') is not None)
        self.telemetry_evidence.set_text('\n'.join(evidence))
        if evidence:
            self.telemetry_evidence.show()
            self.telemetry_evidence_expander.show()
        else:
            self.telemetry_evidence_expander.hide()

        self._clear_rows(self.telemetry_tuning)
        for line in history['tuning'] or [_("No setup recorded yet.")]:
            self.telemetry_tuning.add(self._text_row(line))
        self.telemetry_tuning.show_all()

        self._clear_rows(self.telemetry_sessions, keep=1)
        rows = history['sessions']
        if not rows and history.get('last_session') is not None:
            self.telemetry_sessions.add(self._text_row(_("No sessions yet."), dim=True))
        for when, where, facts in rows:
            row = Gtk.ListBoxRow(activatable=False, selectable=False)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
            box.set_margin_top(8)
            box.set_margin_bottom(8)
            date = Gtk.Label(xalign=0, yalign=0)
            date.set_markup('<span font_features="tnum">{}</span>'.format(GLib.markup_escape_text(when)))
            date.set_width_chars(16)
            box.pack_start(date, False, False, 0)
            place = Gtk.Label(label=where or '—', xalign=0, yalign=0)
            place.set_line_wrap(True)
            place.set_width_chars(20)
            place.set_max_width_chars(34)
            box.pack_start(place, False, False, 0)
            detail = Gtk.Label(label=facts, xalign=1, yalign=0)
            detail.set_line_wrap(True)
            detail.set_justify(Gtk.Justification.RIGHT)
            detail.get_style_context().add_class('dim-label')
            box.pack_end(detail, True, True, 0)
            row.add(box)
            self.telemetry_sessions.add(row)
        self.telemetry_sessions.show_all()
        self._show_coaching()

    KIND_TAGS = {'top3': 'telemetry-tip', 'focus': 'telemetry-focus', 'praise': 'telemetry-praise', 'tip': 'telemetry-tip',
                'technique': 'telemetry-technique'}

    def _show_coaching(self):
        """The coach's tips, one row each with its kind as a tag, the
        car's own advice until the coach has any, and the quiet "still"
        lines behind an expander at the end."""
        tips = self._telemetry_history['tips'] if self._telemetry_history else []
        items = coaching_items(tips, self._telemetry_advice_lines)
        self._show_splits()
        if items == self._telemetry_coaching_shown:
            return                          # an open expander stays open
        self._telemetry_coaching_shown = items
        listbox = self.telemetry_coaching
        self._clear_rows(listbox)
        shown = [item for item in items if item[2] != 'still']
        quiet = [item for item in items if item[2] == 'still']
        if not shown:
            listbox.add(self._text_row(_("Drive some full-throttle pulls through the gears: the advice appears "
                                         "as Oversteer learns the car and how you drive it."), dim=True))
        for badge, text, kind in shown:
            row = Gtk.ListBoxRow(activatable=False, selectable=False)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            box.set_margin_top(10)
            box.set_margin_bottom(10)
            tag = Gtk.Label(label=badge or _("Tip"), valign=Gtk.Align.START)
            tag.get_style_context().add_class('telemetry-tag')
            tag.get_style_context().add_class(self.KIND_TAGS.get(kind, 'telemetry-tip'))
            tag.set_width_chars(7)
            box.pack_start(tag, False, False, 0)
            label = Gtk.Label(label=text, xalign=0)
            label.set_line_wrap(True)
            label.set_selectable(True)
            box.pack_start(label, True, True, 0)
            row.add(box)
            listbox.add(row)
        if quiet:
            row = Gtk.ListBoxRow(activatable=False, selectable=False)
            expander = Gtk.Expander(label=_("Also noted ({})").format(len(quiet)))
            expander.set_margin_top(8)
            expander.set_margin_bottom(8)
            text = self._status_label()
            text.set_text('\n'.join('•  ' + text for _badge, text, _kind in quiet))
            text.set_margin_top(4)
            expander.add(text)
            row.add(expander)
            listbox.add(row)
        listbox.show_all()

    SPLIT_TONES = {'gold': 'telemetry-split-gold', 'ahead-gain': 'telemetry-split-ahead',
                   'ahead-lose': 'telemetry-split-ahead', 'behind-gain': 'telemetry-split-behind',
                   'behind-lose': 'telemetry-split-lose', 'none': 'dim-label'}

    def _build_splits_row(self):
        """The splits row above Coaching and Telemetry (docs/telemetry-ui-design.md, 4.4): the stage, PB, sum of
        best and the last run on one line, a ribbon of the splits in LiveSplit's colours (the game's sectors as
        wider cells at the end), and a click opens the table (split, last, difference, best; the sectors under it)."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_bottom(8)
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.splits_label = Gtk.Label(xalign=0)
        self.splits_label.set_ellipsize(3)          # Pango.EllipsizeMode.END
        inner.pack_start(self.splits_label, False, False, 0)
        self.splits_ribbon = Gtk.DrawingArea()
        self.splits_ribbon.set_size_request(-1, 10)
        self.splits_ribbon.connect('draw', self._draw_ribbon)
        inner.pack_start(self.splits_ribbon, False, False, 0)
        self.splits_potential = Gtk.Label(xalign=0)           # "Potential: you 3:03.1 · grip 2:51.4 · car 2:42.7"
        self.splits_potential.set_ellipsize(3)
        self.splits_potential.get_style_context().add_class('dim-label')
        self.splits_potential.set_no_show_all(True)
        inner.pack_start(self.splits_potential, False, False, 0)
        button.add(inner)
        button.set_tooltip_text(_("Show or hide the splits"))
        box.pack_start(button, False, False, 0)
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_reveal_child(True)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_min_content_height(40)
        scrolled.set_max_content_height(220)
        scrolled.set_propagate_natural_height(True)
        self.splits_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.splits_body.set_margin_top(6)
        scrolled.add(self.splits_body)
        revealer.add(scrolled)
        box.pack_start(revealer, False, False, 0)
        button.connect('clicked', lambda w: revealer.set_reveal_child(not revealer.get_reveal_child()))
        self.splits_revealer = revealer
        self.splits_box = box
        self._splits_tones = ([], [], None)
        box.show_all()
        box.set_no_show_all(True)               # shown by _show_splits_row() when there are splits
        return box

    def _draw_ribbon(self, area, cr):
        tones, sectors, bounds = self._splits_tones
        current, pulse = None, 1.0
        live = getattr(self, 'telemetry_live_view', None)
        state = live.ribbon_state() if live is not None else None
        if state is not None and len(state[0]) == len(tones):
            tones, current = state                      # a run is on: the splits completed in their tones, the current one lit
            pulse = 0.675 + 0.325 * math.cos(2 * math.pi * time.monotonic())
        telemetry_plot.ribbon(cr, area.get_allocated_width(), area.get_allocated_height(), tones, sectors, bounds,
                              current=current, pulse=pulse)
        return False

    def _show_splits_row(self):
        visible = bool(self._splits_shown) and self.telemetry_stack.get_visible_child_name() != 'settings'
        self.splits_box.set_visible(visible)

    def _show_splits(self):
        found = splits_row(self._telemetry_history.get('splits') if self._telemetry_history else None)
        if found == self._splits_shown:
            return                          # an open table stays as it is
        self._splits_shown = found
        for child in self.splits_body.get_children():
            child.destroy()
        if found is None:
            self._splits_tones = ([], [], None)
            self._show_splits_row()
            return
        self._splits_tones = (found['tones'], [tone for _n, _t, _d, tone in found['sectors']], found['bounds'])
        summary = '<b>{}</b>   {} <tt><b>{}</b></tt>   {} <tt><b>{}</b>{}</tt>'.format(
            GLib.markup_escape_text(found['stage']), _("Best"), clock(found['pb']), _("SoB"), clock(found['sob']),
            sob_gain(found.get('gain')))
        if found['last'] is not None and found['delta'] is not None:
            # PB for the quickest finished run, clean or with offs (coach.splits new_pb)
            tone = 'gold' if found['new_pb'] else 'ahead-gain' if round(found['delta'], 1) <= 0 else 'behind-lose'
            summary += '   {} <tt>{}</tt> <span foreground="{}"><tt><b>{}</b></tt></span>'.format(
                _("Last"), clock(found['last']), telemetry_plot.TONES[tone],
                _("PB") if found['new_pb'] else signed(found['delta']))
        self.splits_label.set_markup(summary)
        self.splits_potential.set_visible(bool(found.get('potential_line')))
        self.splits_potential.set_text(found.get('potential_line') or '')
        self.splits_potential.set_tooltip_text(_("What the layers mean: you, the sum of your best sectors (corner sections on a stage without the game's sector lines); grip, a lap simulation at the grip you reach 2 % of the time; car, at the best grip any run of the car reached, with its engine."))
        self.splits_label.set_tooltip_text(
            (_("Times run to the stage's finish line.") if found.get('finish_real')
             else _("Times run to the estimated flying finish of the stage.")) if found.get('finish_m') else None)
        grid = Gtk.Grid(column_spacing=16, row_spacing=4)
        has_avail = bool(found.get('potential_line'))                 # the stage's potential is known: a time available column
        for col, head in enumerate((_("Sector") if found['unit'] == 'sectors' else _("Section"), _("Last"), _("\u0394 PB"), _("Best"), _("Save")) + ((_("Avail"),) if has_avail else ())):
            label = Gtk.Label(xalign=0 if col == 0 else 1)
            label.set_markup('<b>{}</b>'.format(GLib.markup_escape_text(head)))
            label.get_style_context().add_class('telemetry-header')
            grid.attach(label, col, 0, 1, 1)
        for n, row in enumerate(found['rows'], 1):
            tone = row[4]
            for col, value in enumerate((row[0], row[1], row[2], row[3], row[5]) + ((found['avail'][n - 1],) if has_avail else ())):
                label = Gtk.Label(xalign=0 if col == 0 else 1)
                markup = GLib.markup_escape_text(value)
                if col:
                    markup = '<span font_features="tnum"><tt>{}</tt></span>'.format(markup)
                if col == 2 and tone in telemetry_plot.TONES and tone != 'none' and value != '\u2013':
                    markup = '<span foreground="{}">{}</span>'.format(telemetry_plot.TONES[tone], markup)
                elif n > len(found['rows']) - 2:
                    markup = '<b>{}</b>'.format(markup)
                label.set_markup(markup)
                grid.attach(label, col, n, 1, 1)
        self.splits_body.pack_start(grid, False, False, 0)
        if found['sectors']:
            sectors = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
            for name, last, delta, tone in found['sectors']:
                label = Gtk.Label(xalign=0)
                colour = telemetry_plot.TONES[tone] if tone != 'none' else telemetry_plot.DIM
                label.set_markup('<b>{}</b>  <tt>{}</tt>  <span foreground="{}"><tt>{}</tt></span>'.format(
                    GLib.markup_escape_text(name), GLib.markup_escape_text(last), colour, GLib.markup_escape_text(delta)))
                sectors.pack_start(label, False, False, 0)
            self.splits_body.pack_start(sectors, False, False, 0)
            if found['estimated']:
                note = Gtk.Label(label=_("\u2248 Sector positions estimated from the game's sector lengths."), xalign=0)
                note.get_style_context().add_class('dim-label')
                self.splits_body.pack_start(note, False, False, 0)
        text = _("Gold \u2605: your last run set that split's best, by the margin shown. Green: ahead of your PB, red: "
                 "behind it; the darker shade where the split gained time, the lighter where it lost time. Save: what "
                 "the split could still gain against its best.")
        if found['unit'] == 'sections':
            text = _("Without the game's sector lines for this stage the splits are corner sections.") + ' ' + text
        if found.get('finish_m') is None and found['unit'] == 'sections':
            text += ' ' + _("The finish split includes the slow-down to the stop.")
        note = Gtk.Label(label=text, xalign=0)
        note.set_line_wrap(True)
        note.set_max_width_chars(100)               # wraps in a window as wide as the page instead of running off its edge
        note.get_style_context().add_class('dim-label')
        self.splits_body.pack_start(note, False, False, 0)
        self.splits_body.show_all()
        self._show_splits_row()

    LABEL_CHOICES = (
        ('discipline', _("Discipline"), ('rally-stage', 'hillclimb', 'circuit', 'rallycross', 'drift', 'free-roam',
                                         'time-attack')),
        ('surface', _("Surface"), ('tarmac', 'gravel', 'snow', 'ice')),
        ('wet', _("Wet"), ('dry', 'wet')),
        ('shifter', _("Shifter"), ('h-pattern', 'sequential', 'paddles', 'auto')),
    )

    def _label_session(self):
        """The label dialog: what the last session was, preset from what
        Oversteer detected; saved for every run of it."""
        history = self._telemetry_history
        session = history['last_session'] if history else None
        if session is None:
            self.info_dialog(_("No session to label yet"), _("Drive a stage or some laps first."))
            return
        names = dict(DISCIPLINES, **SURFACES, **METHODS)
        names.update({'dry': _("dry"), 'wet': _("wet")})
        dialog = Gtk.Dialog(title=_("Label last session"), transient_for=self.window, modal=True)
        dialog.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Save"), Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        combos = {}
        for row, (field, title, choices) in enumerate(self.LABEL_CHOICES):
            grid.attach(Gtk.Label(label=title, xalign=0), 0, row, 1, 1)
            combo = Gtk.ComboBoxText()
            combo.append('', _("Don't know"))
            for value in choices:
                combo.append(value, names.get(value, value))
            detected = session.get(field)
            combo.set_active_id(detected if detected in choices else '')
            grid.attach(combo, 1, row, 1, 1)
            combos[field] = combo
        grid.attach(Gtk.Label(label=_("Note"), xalign=0), 0, len(self.LABEL_CHOICES), 1, 1)
        note = Gtk.Entry(activates_default=True)
        grid.attach(note, 1, len(self.LABEL_CHOICES), 1, 1)
        area = dialog.get_content_area()
        area.set_border_width(12)
        area.set_spacing(8)
        area.add(Gtk.Label(label=_("What was the last session? Your labels are kept for Oversteer to learn "
                                   "to tell surfaces apart from."), xalign=0, wrap=True, max_width_chars=50))
        area.add(grid)
        dialog.show_all()
        response = dialog.run()
        label = {field: combo.get_active_id() or None for field, combo in combos.items()}
        label['note'] = note.get_text().strip() or None
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            self.controller.label_last_session(label)

    EAWRC_FOLDER = ('…/steamapps/compatdata/1849250/pfx/drive_c/users/steamuser/Documents/'
                    'My Games/WRC/telemetry/')

    def _eawrc_setup(self):
        """EA SPORTS WRC sends telemetry once its config says what and
        where. Oversteer can't reach the game's Proton prefix from its
        sandbox, so it hands over the text and says where it goes: a row
        of the "Receiving telemetry" list, folded until opened."""
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        expander = Gtk.Expander(label=_("EA SPORTS WRC: turn on its telemetry"))
        expander.set_margin_top(14)
        expander.set_margin_bottom(14)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(8)
        text = Gtk.Label(xalign=0)
        text.set_line_wrap(True)
        text.set_max_width_chars(90)
        text.set_selectable(True)
        text.set_markup(GLib.markup_escape_text(
            _("Start the game once so it creates its telemetry folder, then quit it. In that folder, inside "
              "the game's Proton prefix:\n{folder}\n• save the structure as udp/oversteer.json;\n"
              "• in config.json, add the config lines to the \"packets\" list under \"udp\" (they send "
              "to this computer on the port above; check that the game's own entries call the switch "
              "\"bEnabled\" too).").format(folder=self.EAWRC_FOLDER)))
        box.pack_start(text, False, False, 0)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        structure = Gtk.Button(label=_("Copy structure JSON"))
        structure.connect('clicked', lambda w: self._copy_text(eawrc_structure()))
        buttons.pack_start(structure, False, False, 0)
        config = Gtk.Button(label=_("Copy config lines"))
        config.connect('clicked', lambda w: self._copy_text(
            eawrc_config_lines(getattr(self, '_rev_leds_port_valid', None) or DEFAULT_PORT)))
        buttons.pack_start(config, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        expander.add(box)
        row.add(expander)
        return row

    def set_beamng_install_busy(self, busy):
        self.install_beamng.set_sensitive(not busy)

    def _copy_text(self, text):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)

    def set_rev_leds_options(self, launch, learnt):
        for switch, value in ((self.rev_leds_launch, launch), (self.rev_leds_learnt, learnt)):
            switch.set_sensitive(value is not None)
            if value is not None and switch.get_active() != bool(value):
                switch.set_active(bool(value))

    def set_telemetry_cars(self, cars, active):
        """[(key, name)] and the key to show."""
        self.telemetry_car.handler_block(self.telemetry_car_handler)
        try:
            self.telemetry_car.remove_all()
            for key, name in cars:
                self.telemetry_car.append(key, name)
            if active is not None:
                self.telemetry_car.set_active_id(active)
            self.telemetry_car_buttons.set_sensitive(bool(cars))
        finally:
            self.telemetry_car.handler_unblock(self.telemetry_car_handler)

    def _rename_car(self):
        key = self.telemetry_car.get_active_id()
        if key is None:
            return
        dialog = Gtk.Dialog(title=_("Rename car"), transient_for=self.window, modal=True)
        dialog.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Rename"), Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        entry = Gtk.Entry(text=self.telemetry_car.get_active_text() or '', activates_default=True)
        dialog.get_content_area().set_border_width(12)
        dialog.get_content_area().add(entry)
        dialog.show_all()
        response = dialog.run()
        name = entry.get_text().strip()
        dialog.destroy()
        if response == Gtk.ResponseType.OK and name:
            self.controller.rename_telemetry_car(key, name)

    def _forget_car(self):
        key = self.telemetry_car.get_active_id()
        if key is not None and self.confirmation_dialog(
                _("Relearn this car from scratch? Its sessions and your labels are kept.")):
            self.controller.forget_telemetry_car(key)

    LIVE_STATES = ('off', 'waiting', 'live')

    def set_telemetry_view(self, live, snapshot, dash=None):
        """`live`: (state, markup) of the telemetry arriving now
        (telemetry_view.live_status()). `snapshot`: the shown car's
        learner snapshot, or None. `dash`: telemetry_web.live_dict() for
        the dash of the Telemetry view."""
        state, markup = live
        self._set_dash(dash)
        self.telemetry_live.set_markup(markup)
        context = self.telemetry_live_dot.get_style_context()
        for name in self.LIVE_STATES:
            if name == state:
                context.add_class('telemetry-' + name)
            else:
                context.remove_class('telemetry-' + name)
        advice = list(snapshot['advice']) if snapshot is not None else []
        if advice != self._telemetry_advice_lines:
            self._telemetry_advice_lines = advice
            self._show_coaching()
        # Rebuilt only when what it shows changed, not every second
        shown = (shift_summary(snapshot), shift_table(snapshot) if snapshot is not None else None)
        if shown == self._telemetry_rows:
            return
        self._telemetry_rows = shown
        for child in self.telemetry_gears.get_children():
            child.destroy()
        self.telemetry_summary.set_text(shown[0])
        if snapshot is None:
            self.telemetry_gears_frame.hide()
            return
        headers, cells = shown[1]
        numeric = set(range(1, len(headers)))
        for column, text in enumerate(headers):
            label = Gtk.Label(label=text, xalign=1 if column in numeric else 0)
            label.get_style_context().add_class('dim-label')
            label.get_style_context().add_class('telemetry-header')
            self.telemetry_gears.attach(label, column, 0, 1, 1)
        for index, row in enumerate(cells, start=1):
            for column, text in enumerate(row):
                label = Gtk.Label(xalign=1 if column in numeric else 0)
                markup = '<span font_features="tnum">{}</span>'.format(GLib.markup_escape_text(text))
                if column == 1:
                    markup = '<b>{}</b>'.format(markup)
                label.set_markup(markup)
                context = label.get_style_context()
                context.add_class('telemetry-cell')
                if column == 2 or column >= len(headers) - 2:
                    context.add_class('dim-label')
                self.telemetry_gears.attach(label, column, index, 1, 1)
        self.telemetry_gears_frame.get_child().show_all()
        self.telemetry_gears_frame.show()

    def _build_hotkeys_page(self):
        """The Hotkeys tab: one row per action, with its wheel button (saved
        in the profile) and its keyboard key (the desktop's)."""
        self.hotkey_rows = {}
        self.hotkey_bindings = {}
        self.hotkey_capturing = None
        self.keyboard_triggers = None

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_border_width(12)

        intro = Gtk.Label(xalign=0)
        intro.set_line_wrap(True)
        intro.set_max_width_chars(80)
        intro.set_markup('<small>' + GLib.markup_escape_text(
            _("Change settings while you drive. Wheel buttons are saved with the profile; "
              "the game still sees them, so pick buttons it doesn't use. The profile buttons and "
              "keyboard keys work in every profile; keys are assigned in your desktop's shortcut "
              "settings.")) + '</small>')
        page.pack_start(intro, False, False, 0)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.keyboard_shortcuts_button = Gtk.Button(label=_("Set keyboard keys…"))
        self.keyboard_shortcuts_button.set_tooltip_text(
            _("Open the desktop's shortcut settings, where Oversteer's actions are listed"))
        self.keyboard_shortcuts_button.connect('clicked', lambda w: self.controller.configure_keyboard_hotkeys())
        bar.pack_start(self.keyboard_shortcuts_button, False, False, 0)
        self.hotkeys_status = Gtk.Label(xalign=0)
        self.hotkeys_status.get_style_context().add_class('dim-label')
        self.hotkeys_status.set_ellipsize(3)        # Pango.EllipsizeMode.END
        bar.pack_start(self.hotkeys_status, True, True, 0)
        page.pack_start(bar, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=4)
        grid.set_margin_top(4)
        row = 0
        for text, column in ((_("Action"), 0), (_("Wheel button"), 1), (_("Keyboard"), 3)):
            header = Gtk.Label(xalign=0)
            header.set_markup('<b>{}</b>'.format(GLib.markup_escape_text(text)))
            grid.attach(header, column, row, 1, 1)
        row += 1
        group = None
        for action in hotkeys.ACTIONS:
            if action.group != group:
                group = action.group
                heading = Gtk.Label(xalign=0)
                heading.set_markup('<small><b>{}</b></small>'.format(GLib.markup_escape_text(group)))
                heading.set_margin_top(6)
                grid.attach(heading, 0, row, 4, 1)
                row += 1
            label = Gtk.Label(label=action.label, xalign=0)
            label.set_margin_start(12)
            label.set_hexpand(True)
            grid.attach(label, 0, row, 1, 1)
            button = Gtk.Button()
            button.set_size_request(170, -1)
            button.set_tooltip_text(_("Click, then press a button on the wheel"))
            button.connect('clicked', lambda w, a=action.id: self.controller.start_hotkey_capture(a))
            grid.attach(button, 1, row, 1, 1)
            clear = Gtk.Button.new_from_icon_name('edit-clear-symbolic', Gtk.IconSize.BUTTON)
            clear.set_relief(Gtk.ReliefStyle.NONE)
            clear.set_tooltip_text(_("Remove the wheel button"))
            clear.set_no_show_all(True)         # only shown next to a set button
            clear.connect('clicked', lambda w, a=action.id: self.controller.clear_hotkey(a))
            grid.attach(clear, 2, row, 1, 1)
            keys = Gtk.Label(xalign=0)
            keys.set_size_request(140, -1)
            keys.get_style_context().add_class('dim-label')
            grid.attach(keys, 3, row, 1, 1)
            self.hotkey_rows[action.id] = (button, clear, keys)
            row += 1

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.add(grid)
        page.pack_start(scrolled, True, True, 0)

        self.main_notebook.insert_page(page, Gtk.Label(label=_("Hotkeys")), self.HOTKEYS_TAB_POSITION)
        # A capture left armed would take the next press in a game
        self.main_notebook.connect('switch-page', lambda *args: self.controller.cancel_hotkey_capture())
        self._refresh_hotkey_rows()

    def _refresh_hotkey_rows(self):
        for action_id, (button, clear, keys) in self.hotkey_rows.items():
            if action_id == self.hotkey_capturing:
                button.set_label(_("Press a wheel button…"))
            else:
                wheel_input = self.hotkey_bindings.get(action_id)
                button.set_label(hotkeys.input_name(wheel_input) if wheel_input else _("Not set"))
            clear.set_visible(action_id in self.hotkey_bindings)
            if self.keyboard_triggers is None:
                keys.set_text('')
            else:
                keys.set_text(self.keyboard_triggers.get(action_id) or '—')
        self.keyboard_shortcuts_button.set_sensitive(self.keyboard_triggers is not None)

    def set_hotkeys(self, text):
        """The profile's wheel bindings; the app-wide ones come from the
        controller."""
        self.hotkey_bindings = {a: i for a, i in hotkeys.parse(text).items() if a not in hotkeys.GLOBAL_ACTIONS}
        self.hotkey_bindings.update(self.controller.global_hotkeys)
        self._refresh_hotkey_rows()

    def set_hotkey_capture(self, action_id):
        self.hotkey_capturing = action_id
        self._refresh_hotkey_rows()

    def set_keyboard_triggers(self, triggers):
        """{action id: key description} from the desktop; None when the
        desktop has no shortcut portal."""
        self.keyboard_triggers = triggers
        self._refresh_hotkey_rows()

    def set_hotkeys_status(self, text):
        self.hotkeys_status.set_text(text)
        self.hotkeys_status.set_tooltip_text(text)

    def cycle_profile(self, delta):
        """Select the next/previous saved profile (wrapping); the name, or
        None when there are none."""
        names = [row[0] for row in self.profile_combobox.get_model() or [] if row[0]]
        if not names:
            return None
        current = self.profile_combobox.get_active_id()
        index = names.index(current) + delta if current in names else (0 if delta > 0 else -1)
        name = names[index % len(names)]
        self.profile_combobox.set_active_id(name)
        return name

    def _set_builder_objects(self):
        self._available = {}
        self.updating_invert_pedals = False
        self.pedal_bits = {'clutch': 1, 'accelerator': 2, 'brakes': 4}
        self.updating_combine = False
        self.updating_rev_leds = False
        self.window = self.builder.get_object('main_window')
        self.about_window = self.builder.get_object('about_window')
        self.preferences_window = self.builder.get_object('preferences_window')
        self.overlay_window = self.builder.get_object('overlay_window')
        self.overlay_window.set_keep_above(True)
        self.overlay_window.connect("screen-changed", self._screen_changed)
        self._screen_changed(self.overlay_window, None)

        self.languages_combobox = self.builder.get_object('languages')
        self.check_permissions = self.builder.get_object('check_permissions')

        self.device_combobox = self.builder.get_object('device')
        self.driver_status = self.builder.get_object('driver_status')
        self.profile_combobox = self.builder.get_object('profile')
        self.main_notebook = self.builder.get_object('main_notebook')
        self.new_profile_name_entry = self.builder.get_object('new_profile_name')
        self.save_profile_button = self.builder.get_object('save_profile')
        self.profile_note = self.builder.get_object('profile_note')
        self.profile_apply_on_plug = self.builder.get_object('profile_apply_on_plug')
        self.new_profile_name = self.builder.get_object('new_profile_name')
        self.emulation_mode_combobox = self.builder.get_object('emulation_mode')
        self.change_emulation_mode_button = self.builder.get_object('change_emulation_mode')
        self.wheel_range = self.builder.get_object('wheel_range')
        self.try_buttons = [self.builder.get_object('try_' + kind) for kind in ('constant', 'spring', 'damper', 'friction', 'rumble')]
        self.equipment_store = self.builder.get_object('equipment_store')
        self.equipment_view = self.builder.get_object('equipment_view')
        self.combine_switch = self.builder.get_object('combine_switch')
        self.combine_status = self.builder.get_object('combine_status')
        self.combine_generic = self.builder.get_object('combine_generic')
        self.combine_start = self.builder.get_object('combine_start')
        self.wheel_range_setup = self.builder.get_object('wheel_range_setup')
        self.range_presets = {d: self.builder.get_object('range_preset_' + str(d))
                              for d in (270, 360, 540, 720, 900)}
        self.wheel_sensitivity = self.builder.get_object('wheel_sensitivity')
        self.combine_none = self.builder.get_object('combine_none')
        self.combine_brakes = self.builder.get_object('combine_brakes')
        self.combine_clutch = self.builder.get_object('combine_clutch')
        self.autocenter = self.builder.get_object('autocenter')
        self.autocenter_persistent = self.builder.get_object('autocenter_persistent')
        self.app_gain = self.builder.get_object('app_gain')
        self.inertia_mode = self.builder.get_object('inertia_mode')
        self.ff_gain = self.builder.get_object('ff_gain')
        self.ffb_enabled = self.builder.get_object('ffb_enabled')
        self.invert_clutch = self.builder.get_object('invert_clutch')
        self.invert_accelerator = self.builder.get_object('invert_accelerator')
        self.invert_brakes = self.builder.get_object('invert_brakes')
        self.ff_spring_level = self.builder.get_object('ff_spring_level')
        self.ff_damper_level = self.builder.get_object('ff_damper_level')
        self.ff_friction_level = self.builder.get_object('ff_friction_level')
        self.ff_rumble_level = self.builder.get_object('ff_rumble_level')
        self.ffbmeter_leds = self.builder.get_object('ffbmeter_leds')
        self.rev_leds = self.builder.get_object('rev_leds')
        self.rev_leds_port = self.builder.get_object('rev_leds_port')
        self.rev_leds_port.append_text(self.port_auto_text())
        for port, label in PORT_PRESETS:
            self.rev_leds_port.append_text(self.port_preset_text(port, label))
        self.rev_leds_port.get_child().set_width_chars(16)
        self.rev_leds_port.connect('scroll-event', lambda w, e: True)    # scrolling the page must not switch ports
        self.updating_rev_leds = True
        try:
            self._show_rev_leds_port(None)                       # profiles without a port: Auto
        finally:
            self.updating_rev_leds = False
        self.rev_leds_shift = self.builder.get_object('rev_leds_shift')
        self.rev_leds_shift_unit = self.builder.get_object('rev_leds_shift_unit')
        self.rev_leds_shift_percent_adjustment = self.builder.get_object('rev_leds_shift_adjustment')
        self.rev_leds_shift_rpm_adjustment = self.builder.get_object('rev_leds_shift_rpm_adjustment')
        self.rev_leds_test = self.builder.get_object('rev_leds_test')
        self.rev_leds_status = self.builder.get_object('rev_leds_status')
        self.launch_options = self.builder.get_object('launch_options')
        self.launch_options_copy = self.builder.get_object('launch_options_copy')
        self.ffbmeter_overlay = self.builder.get_object('ffbmeter_overlay')
        self.wheel_range_overlay_never = self.builder.get_object('wheel_range_overlay_never')
        self.wheel_range_overlay_always = self.builder.get_object('wheel_range_overlay_always')
        self.wheel_range_overlay_auto = self.builder.get_object('wheel_range_overlay_auto')
        self._ffbmeter_overlay = self.builder.get_object('_ffbmeter_overlay')
        self._wheel_range_overlay = self.builder.get_object('_wheel_range_overlay')
        self.overlay_wheel_range = self.builder.get_object('overlay_wheel_range')
        self.overlay_led_0 = self.builder.get_object('overlay_led_0')
        self.overlay_led_1 = self.builder.get_object('overlay_led_1')
        self.overlay_led_2 = self.builder.get_object('overlay_led_2')
        self.overlay_led_3 = self.builder.get_object('overlay_led_3')
        self.overlay_led_4 = self.builder.get_object('overlay_led_4')
        self.wheel_buttons = self.builder.get_object('wheel_buttons')
        self.center_wheel = self.builder.get_object('center_wheel')
        self.start_define_buttons = self.builder.get_object('start_define_buttons')
        self.define_buttons_text = self.start_define_buttons.get_label()
        self.start_app = self.builder.get_object('start_app')
        self.start_app_manually = self.builder.get_object('start_app_manually')

        self.steering_left_input = self.builder.get_object('steering_left_input')
        self.steering_right_input = self.builder.get_object('steering_right_input')
        self.clutch_input = self.builder.get_object('clutch_input')
        self.accelerator_input = self.builder.get_object('accelerator_input')
        self.brakes_input = self.builder.get_object('brakes_input')
        self.handbrake_input = self.builder.get_object('handbrake_input')
        self.handbrake_label = self.builder.get_object('handbrake_label')
        self.invert_handbrake = self.builder.get_object('invert_handbrake')
        self.calibrate_handbrake = self.builder.get_object('calibrate_handbrake')
        self.hat_up_input = self.builder.get_object('hat_up_input')
        self.hat_down_input = self.builder.get_object('hat_down_input')
        self.hat_left_input = self.builder.get_object('hat_left_input')
        self.hat_right_input = self.builder.get_object('hat_right_input')
        self.btn_input = [None] * 30
        for i in range(30):
            self.btn_input[i] = self.builder.get_object('btn' + str(i) + '_input')

        self.profile_listbox = self.builder.get_object('profile_listbox')

        def sort_profiles(row1, row2):
            text1 = row1.get_children()[0].get_text().lower()
            text2 = row2.get_children()[0].get_text().lower()
            if text1 < text2:
                return -1
            if text1 > text2:
                return 1
            return 0

        self.profile_listbox.set_sort_func(sort_profiles)

        self.test_container = self.builder.get_object('test_container')
        self.test_container_stack = self.builder.get_object('test_container_stack')
        self.test_chart_window = self.builder.get_object('test_chart_window')
        self.test_chart_container = self.builder.get_object('test_chart_container')
        self.test_chart_frame = self.builder.get_object('test_chart_frame')
        self.test_start_button = self.builder.get_object('test_start_button')
        self.test_open_chart_button = self.builder.get_object('test_open_chart_button')
        self.test_export_csv_button = self.builder.get_object('test_export_csv_button')
        self.test_import_csv_button = self.builder.get_object('test_import_csv_button')
        self.test_open_chart_button.set_sensitive(False)
        self.test_export_csv_button.set_sensitive(False)
        self.test_max_velocity = self.builder.get_object('test_max_velocity')
        self.test_latency = self.builder.get_object('test_latency')
        self.test_max_accel = self.builder.get_object('test_max_accel')
        self.test_max_decel = self.builder.get_object('test_max_decel')
        self.test_time_to_max_accel = self.builder.get_object('test_time_to_max_accel')
        self.test_time_to_max_decel = self.builder.get_object('test_time_to_max_decel')
        self.test_mean_accel = self.builder.get_object('test_mean_accel')
        self.test_mean_decel = self.builder.get_object('test_mean_decel')
        self.test_residual_decel = self.builder.get_object('test_residual_decel')
        self.test_estimated_snr = self.builder.get_object('test_estimated_snr')
        self.test_minimum_level = self.builder.get_object('test_minimum_level')
        self.test_panel_empty = self.builder.get_object('test_panel_empty')
        self.test_panel_start1 = self.builder.get_object('test_panel_start1')
        self.test_panel_start2 = self.builder.get_object('test_panel_start2')
        self.test_panel_start3 = self.builder.get_object('test_panel_start3')
        self.test_panel_running = self.builder.get_object('test_panel_running')
        self.test_panel_running1 = self.builder.get_object('test_panel_running1')
        self.test_panel_running1_ready = self.builder.get_object('test_panel_running1_ready')
        self.test_panel_running1_go = self.builder.get_object('test_panel_running1_go')
        self.test_panel_results = self.builder.get_object('test_panel_results')
        self.test_panel_warning = self.builder.get_object('test_panel_warning')
        self.test_panel_buttons = self.builder.get_object('test_panel_buttons')
        self.test_panel_back = self.builder.get_object('test_panel_back')
        self.test_panel_run = self.builder.get_object('test_panel_run')

    def _set_markers(self):
        self.autocenter.add_mark(20, Gtk.PositionType.BOTTOM, '20')
        self.autocenter.add_mark(40, Gtk.PositionType.BOTTOM, '40')
        self.autocenter.add_mark(60, Gtk.PositionType.BOTTOM, '60')
        self.autocenter.add_mark(80, Gtk.PositionType.BOTTOM, '80')
        self.autocenter.add_mark(100, Gtk.PositionType.BOTTOM, '100')
        self.ff_gain.add_mark(20, Gtk.PositionType.BOTTOM, '20')
        self.ff_gain.add_mark(40, Gtk.PositionType.BOTTOM, '40')
        self.ff_gain.add_mark(60, Gtk.PositionType.BOTTOM, '60')
        self.ff_gain.add_mark(80, Gtk.PositionType.BOTTOM, '80')
        self.ff_gain.add_mark(100, Gtk.PositionType.BOTTOM, '100')
        self.ff_gain.add_mark(125, Gtk.PositionType.BOTTOM, '125')
        self.ff_gain.add_mark(150, Gtk.PositionType.BOTTOM, '150')
        self.ff_spring_level.add_mark(20, Gtk.PositionType.BOTTOM, '20')
        self.ff_spring_level.add_mark(40, Gtk.PositionType.BOTTOM, '40')
        self.ff_spring_level.add_mark(60, Gtk.PositionType.BOTTOM, '60')
        self.ff_spring_level.add_mark(80, Gtk.PositionType.BOTTOM, '80')
        self.ff_spring_level.add_mark(100, Gtk.PositionType.BOTTOM, '100')
        self.ff_damper_level.add_mark(20, Gtk.PositionType.BOTTOM, '20')
        self.ff_damper_level.add_mark(40, Gtk.PositionType.BOTTOM, '40')
        self.ff_damper_level.add_mark(60, Gtk.PositionType.BOTTOM, '60')
        self.ff_damper_level.add_mark(80, Gtk.PositionType.BOTTOM, '80')
        self.ff_damper_level.add_mark(100, Gtk.PositionType.BOTTOM, '100')
        self.ff_friction_level.add_mark(20, Gtk.PositionType.BOTTOM, '20')
        self.ff_friction_level.add_mark(40, Gtk.PositionType.BOTTOM, '40')
        self.ff_friction_level.add_mark(60, Gtk.PositionType.BOTTOM, '60')
        self.ff_friction_level.add_mark(80, Gtk.PositionType.BOTTOM, '80')
        self.ff_friction_level.add_mark(100, Gtk.PositionType.BOTTOM, '100')
        for v in (20, 40, 60, 80, 100):
            self.ff_rumble_level.add_mark(v, Gtk.PositionType.BOTTOM, str(v))

    RANGE_MARKS = (180, 270, 360, 450, 540, 720, 900, 1080)

    def _set_range_markers(self, max_range):
        # Ticks without labels: the preset buttons underneath carry the
        # numbers, and printing them twice just crowds the row.
        self.wheel_range.clear_marks()
        for degrees in self.RANGE_MARKS:
            if max_range >= degrees:
                self.wheel_range.add_mark(degrees / 10, Gtk.PositionType.BOTTOM, None)
