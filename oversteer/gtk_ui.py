import gi
import locale as Locale
from locale import gettext as _
import logging
import math
import os
from .gtk_handlers import GtkHandlers
from . import hotkeys
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

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

    def reset_view(self):
        self.new_profile_name_entry.hide()
        self.start_app.hide()
        self.switch_test_panel(None)

    def start(self):
        handlers = GtkHandlers(self, self.controller)
        self.builder.connect_signals(handlers)
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
        self.updating_rev_leds = True
        try:
            if port is not None:
                self.rev_leds_port.set_value(int(port))
            if unit is not None:
                # The spin button's range follows the unit
                self.rev_leds_shift.set_adjustment(self.rev_leds_shift_rpm_adjustment if unit == 'rpm'
                                                   else self.rev_leds_shift_percent_adjustment)
                self.rev_leds_shift_unit.set_active_id(unit)
            if shift is not None:
                self.rev_leds_shift.set_value(int(shift))
        finally:
            self.updating_rev_leds = False

    def set_launch_options(self, text):
        self.launch_options.set_text(text)

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

    def _update_ffbmeter_overlay(self):
        if not self.overlay_window.props.visible or not self.ffbmeter_overlay.props.visible:
            self.ffbmeter_timer = False
            return False
        level = self.controller.read_ffbmeter()
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

    @staticmethod
    def _row_of(widget):
        while widget is not None and not isinstance(widget, Gtk.ListBoxRow):
            widget = widget.get_parent()
        return widget

    def _switch_row(self, text, tooltip, handler):
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        row.set_size_request(-1, 56)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        box.set_valign(Gtk.Align.CENTER)
        box.set_tooltip_text(tooltip)
        label = Gtk.Label(label=text, xalign=0)
        label.set_line_wrap(True)
        box.pack_start(label, True, True, 0)
        switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        switch.connect('state-set', lambda w, state: handler(state) or False)
        box.pack_end(switch, False, False, 0)
        row.add(box)
        return row, switch

    def _build_telemetry_page(self):
        """The Telemetry tab: the rev lights (moved from Tools), what is
        being learnt about the car being driven, and coaching."""
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_border_width(12)

        settings = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        settings.get_style_context().add_class('frame')
        for widget in (self.rev_leds, self.launch_options):
            row = self._row_of(widget)
            if row is not None:
                row.get_parent().remove(row)
                settings.add(row)
        row, self.rev_leds_launch = self._switch_row(
            _("Learn the limiter at each launch"),
            _("A rally stage starts with the clutch in, handbrake up and throttle floored, which holds "
              "the engine on its limiter. Held for a second, that RPM becomes the car's maximum for "
              "the % shift point, whatever the game reports. Learnt again at every start."),
            lambda state: self.controller.model.set_rev_leds_launch(state))
        settings.insert(row, 1)
        row, self.rev_leds_learnt = self._switch_row(
            _("Shift lights at the learnt best upshift for each gear"),
            _("Once Oversteer has learnt the car's power curve and gearing, the lights complete at the "
              "rpm where the next gear starts pulling harder, gear by gear. Until then, and for gears "
              "it doesn't know yet, the shift point above is used."),
            lambda state: self.controller.model.set_rev_leds_learnt(state))
        settings.insert(row, 2)
        page.pack_start(settings, False, False, 0)

        self.telemetry_live = Gtk.Label(xalign=0)
        self.telemetry_live.set_selectable(True)
        page.pack_start(self.telemetry_live, False, False, 0)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.pack_start(Gtk.Label(label=_("Car")), False, False, 0)
        self.telemetry_car = Gtk.ComboBoxText()
        self.telemetry_car.set_tooltip_text(_("Cars learnt in this profile. The car being driven is shown "
                                              "unless you pick another."))
        self.telemetry_car_handler = self.telemetry_car.connect(
            'changed', lambda w: self.controller.select_telemetry_car(w.get_active_id()))
        bar.pack_start(self.telemetry_car, True, True, 0)
        rename = Gtk.Button(label=_("Rename…"))
        rename.connect('clicked', lambda w: self._rename_car())
        bar.pack_start(rename, False, False, 0)
        forget = Gtk.Button(label=_("Forget"))
        forget.set_tooltip_text(_("Throw away what has been learnt about this car and start again"))
        forget.connect('clicked', lambda w: self._forget_car())
        bar.pack_start(forget, False, False, 0)
        page.pack_start(bar, False, False, 0)

        self.telemetry_summary = Gtk.Label(xalign=0)
        self.telemetry_summary.get_style_context().add_class('dim-label')
        self.telemetry_summary.set_line_wrap(True)
        page.pack_start(self.telemetry_summary, False, False, 0)

        self.telemetry_gears = Gtk.Grid(column_spacing=24, row_spacing=4)
        page.pack_start(self.telemetry_gears, False, False, 0)

        heading = Gtk.Label(xalign=0)
        heading.set_markup('<b>{}</b>'.format(GLib.markup_escape_text(_("Coaching"))))
        page.pack_start(heading, False, False, 0)
        self.telemetry_advice = Gtk.Label(xalign=0, yalign=0)
        self.telemetry_advice.set_line_wrap(True)
        self.telemetry_advice.set_selectable(True)
        page.pack_start(self.telemetry_advice, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(page)
        self.main_notebook.insert_page(scrolled, Gtk.Label(label=_("Telemetry")), self.TELEMETRY_TAB_POSITION)
        self._telemetry_rows = None

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
        if key is not None and self.confirmation_dialog(_("Forget everything learnt about this car?")):
            self.controller.forget_telemetry_car(key)

    def set_telemetry_view(self, live, snapshot):
        """`live`: the line about the telemetry arriving now. `snapshot`:
        the shown car's learner snapshot, or None."""
        self.telemetry_live.set_markup(live)
        rows = None
        if snapshot is not None:
            rows = (snapshot['limiter'], tuple(tuple(sorted(r.items())) for r in snapshot['gears']),
                    tuple(snapshot['advice']), snapshot['power_bands'])
        if rows == self._telemetry_rows:
            return
        self._telemetry_rows = rows
        for child in self.telemetry_gears.get_children():
            child.destroy()
        if snapshot is None:
            self.telemetry_summary.set_text(_("Nothing learnt yet: drive with the rev lights on and "
                                              "Oversteer learns each car's gearing and power."))
            self.telemetry_advice.set_text('')
            return
        limiter = snapshot['limiter']
        source = _("engine power from the game") if snapshot['power_source'] == 'game' else \
            _("engine power estimated from acceleration")
        self.telemetry_summary.set_text(_("Limiter {} rpm  ·  {} rev bands of power known ({})").format(
            int(limiter) if limiter else '?', snapshot['power_bands'], source))
        headers = (_("Gear"), _("rpm per km/h"), _("Best upshift"), _("of limiter"), _("You change up"), _("Samples"))
        for column, text in enumerate(headers):
            label = Gtk.Label(xalign=0)
            label.set_markup('<b>{}</b>'.format(GLib.markup_escape_text(text)))
            self.telemetry_gears.attach(label, column, 0, 1, 1)
        for index, row in enumerate(snapshot['gears'], start=1):
            if row['last']:
                best, share = _("top gear"), ''
            elif row['best'] is None:
                best, share = _("learning…"), ''
            else:
                best = '{:.0f} rpm'.format(row['best'])
                share = '{:.0f} %'.format(row['best'] / limiter * 100) if limiter else ''
            mine = '{:.0f} rpm ({})'.format(row['average_shift'], row['shifts']) if row['average_shift'] else '—'
            cells = (str(row['gear']), '{:.1f}'.format(row['ratio'] / 3.6), best, share, mine,
                     str(row['ratio_samples']))
            for column, text in enumerate(cells):
                self.telemetry_gears.attach(Gtk.Label(label=text, xalign=0), column, index, 1, 1)
        self.telemetry_gears.show_all()
        advice = snapshot['advice']
        self.telemetry_advice.set_text('\n'.join('•  ' + tip for tip in advice) if advice else
                                       _("Drive some full-throttle pulls through the gears: the advice "
                                         "appears as Oversteer learns the car and how you drive it."))

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
        self.rev_leds_shift = self.builder.get_object('rev_leds_shift')
        self.rev_leds_shift_unit = self.builder.get_object('rev_leds_shift_unit')
        self.rev_leds_shift_percent_adjustment = self.builder.get_object('rev_leds_shift_adjustment')
        self.rev_leds_shift_rpm_adjustment = self.builder.get_object('rev_leds_shift_rpm_adjustment')
        self.rev_leds_test = self.builder.get_object('rev_leds_test')
        self.rev_leds_status = self.builder.get_object('rev_leds_status')
        self.launch_options = self.builder.get_object('launch_options')
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
