"""The Live view of the GTK Telemetry tab (docs/telemetry-ui-design.md 7.2, 7.8): the dash with its delta block,
the pedal and steering strips of the last 12 s, g-g with its trail and the stage minimap, the finished card. It
follows the web page's Live view on the same data: ShiftLearner.live_run.read(since) from a GLib timeout of the
main thread at 10 Hz while the view is shown (the timer stops when it is hidden), drawn with cairo
(telemetry_plot.live_*) from telemetry_view.LiveTrack. Nothing is pushed to GTK from the listener."""
import logging
import math
import time

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import GLib, Gtk
from locale import gettext as _

from . import telemetry_plot as plot
from .telemetry_view import LiveTrack, clock, live_delta_view, live_done_view, live_phase, live_ribbon, signed2

INTERVAL = 100                       # ms between two reads while shown


class LiveView(Gtk.Paned):
    """`read(since)` gives the live run (live_buffer.LiveBuffer.read); `found()` the splits row's data
    (telemetry_view.splits_row) or None; `on_debrief()` opens the Coaching view; `on_ribbon()` redraws the splits
    ribbon (it follows the run); `calls()` the debrief's count line, or ''; `on_open_run(run id)` opens the Run view."""

    def __init__(self, read=None, found=None, on_debrief=None, on_ribbon=None, calls=None, on_open_run=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self._read = read
        self._found = found or (lambda: None)
        self._on_debrief = on_debrief
        self._on_ribbon = on_ribbon
        self._on_open_run = on_open_run
        self._calls = calls or (lambda: '')
        self.track = LiveTrack()
        self._timer = None
        self._delta = None
        self._ribbon = None
        self._failed_at = None
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.set_border_width(12)
        self.dash_area = Gtk.DrawingArea()
        self.dash_area.set_size_request(320, 240)
        left.pack_start(self.dash_area, False, False, 0)
        self.delta_area = Gtk.DrawingArea()
        self.delta_area.set_size_request(320, 88)
        self.delta_area.connect('draw', lambda a, cr: self._draw(plot.live_delta, a, cr, self._delta))
        left.pack_start(self.delta_area, False, False, 0)
        self.done = self._done_card()
        left.pack_start(self.done, False, False, 0)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.set_border_width(12)
        self.rolling_area = Gtk.DrawingArea()
        self.rolling_area.set_size_request(320, int(plot.live_rolling_height()))
        self.rolling_area.connect('draw', self._draw_rolling)
        right.pack_start(self.rolling_area, False, False, 0)
        duo = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True)
        self.gg_area = Gtk.DrawingArea()
        self.gg_area.set_size_request(160, 210)
        self.gg_area.connect('draw', self._draw_gg)
        self.map_area = Gtk.DrawingArea()
        self.map_area.set_size_request(160, 210)
        self.map_area.connect('draw', self._draw_map)
        duo.pack_start(self.gg_area, True, True, 0)
        duo.pack_start(self.map_area, True, True, 0)
        right.pack_start(duo, False, False, 0)
        self.right = right
        self.pack1(left, True, False)
        self.pack2(right, True, False)
        self.connect('map', self._on_map)
        self.connect('unmap', self._on_unmap)
        self.show_all()
        self.done.hide()
        self.delta_area.hide()
        self.done.set_no_show_all(True)
        self.delta_area.set_no_show_all(True)

    def _done_card(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.get_style_context().add_class('card')
        box.set_border_width(12)
        self.done_head = Gtk.Label(xalign=0)
        self.done_time = Gtk.Label(xalign=0)
        self.done_line = Gtk.Label(xalign=0)
        self.done_line.set_line_wrap(True)
        self.done_line.get_style_context().add_class('dim-label')
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.open_run = Gtk.Button(label=_("Open the run"))
        self.open_run.connect('clicked', self._open_run)
        debrief = Gtk.Button(label=_("Debrief"))
        debrief.connect('clicked', lambda b: self._on_debrief and self._on_debrief())
        back = Gtk.Button(label=_("Back to live"))
        back.connect('clicked', self._back)
        buttons.pack_start(self.open_run, False, False, 0)
        buttons.pack_start(debrief, False, False, 0)
        buttons.pack_start(back, False, False, 0)
        for child in (self.done_head, self.done_time, self.done_line, buttons):
            box.pack_start(child, False, False, 0)
        return box

    # -- the timer: while the view is shown --

    def _on_map(self, _widget):
        self.track.resync()                      # the whole buffer: what is already held is not added twice
        self._tick()
        if self._timer is None:
            self._timer = GLib.timeout_add(INTERVAL, self._tick)

    def _on_unmap(self, _widget):
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None

    @property
    def running(self):
        return self._timer is not None

    def set_source(self, read):
        self._read = read

    def _tick(self):
        if self._read is None:
            return True
        try:
            body = self._read(self.track.since)
            self.track.ingest(body)
            self.refresh(body)
        except Exception:
            now = time.monotonic()
            if self._failed_at is None or now - self._failed_at > 60:         # logged once a minute: never a flood
                self._failed_at = now
                logging.exception("live view")
        return True

    def refresh(self, body=None):
        body = body or self.track.body
        if body is None:
            return
        phase = live_phase(body, self.track)
        found = self._found()
        bounds = found['bounds'] if found else None
        self._delta = live_delta_view(body, bounds)
        finished = phase == 'finished'
        self.dash_area.set_visible(not finished)
        self.delta_area.set_visible(self._delta is not None and not finished)
        self.done.set_visible(finished)
        if finished:
            card = live_done_view(body, found, self._calls())
            self.done_head.set_markup('<b>{}</b>   <span foreground="{}">{}</span>'.format(
                GLib.markup_escape_text(_("Run finished")), plot.DIM, GLib.markup_escape_text(card['stage'])))
            colour = plot.LV_SLOWER if (card['delta_value'] or 0) > 0.005 else plot.LV_FASTER \
                if (card['delta_value'] or 0) < -0.005 else plot.TEXT
            self.done_time.set_markup('<span size="xx-large"><tt><b>{}</b></tt></span>   <span foreground="{}" size="x-large">'
                                      '<tt><b>{}</b></tt></span>'.format(card['time'], colour, card['delta']))
            self.done_line.set_text(card['line'])
            self.open_run.set_sensitive(bool(body.get('run') and body['run'].get('id') is not None))
        ribbon = live_ribbon(body, self.track, bounds) if phase != 'idle' else None
        if ribbon != self._ribbon:
            self._ribbon = ribbon
        if self._on_ribbon is not None:
            self._on_ribbon()
        self.right.set_opacity(0.45 if phase == 'stale' else 1.0)
        handbrake = any(r['hb'] is not None and r['hb'] > 0.02 for r in self.track.rows if r['t'] >= self.track.last_t - plot.LV_WINDOW)
        height = int(plot.live_rolling_height(handbrake))
        if self.rolling_area.get_size_request()[1] != height:
            self.rolling_area.set_size_request(320, height)
        for area in (self.dash_area, self.delta_area, self.rolling_area, self.gg_area, self.map_area):
            area.queue_draw()

    def _open_run(self, _button):
        body = self.track.body
        run_id = body['run']['id'] if body and body.get('run') else None
        if run_id is not None and self._on_open_run is not None:
            self._on_open_run(run_id)

    def _back(self, _button):
        body = self.track.body
        if body and body.get('run'):
            self.track.dismissed = body['run']['n']
            self.refresh(body)

    def ribbon_state(self):
        """(tones per ribbon cell, the current cell) while a run is on, else None: the splits ribbon follows the
        run by distance."""
        return self._ribbon

    # -- drawing --

    @staticmethod
    def _draw(fn, area, cr, data):
        fn(cr, area.get_allocated_width(), area.get_allocated_height(), data)
        return False

    def _draw_rolling(self, area, cr):
        plot.live_rolling(cr, area.get_allocated_width(), area.get_allocated_height(), self.track.rows, self.track.last_t)
        return False

    def _draw_gg(self, area, cr):
        rows = self.track.rows
        last = rows[-1] if rows else None
        total = math.hypot(last['alat'], last['along']) if last and last['alat'] is not None and last['along'] is not None else 0.0
        plot.live_gg(cr, area.get_allocated_width(), area.get_allocated_height(), rows, total)
        return False

    def _draw_map(self, area, cr):
        body = self.track.body or {}
        ref = body.get('ref')
        stage = body.get('stage') or {}
        length = (ref or {}).get('course') or stage.get('length')      # the reference's metres: the run's distance is in them
        ticks = [s['d1'] for s in ref['splits']] if ref else ()
        distance = body.get('distance')
        label = ''
        if distance is not None:
            label = '{:.1f}{} km'.format(max(0.0, distance) / 1000.0, ' / {:.1f}'.format(length / 1000.0) if length else '')
        plot.live_map(cr, area.get_allocated_width(), area.get_allocated_height(), self.track.path, distance, length, ticks, label)
        return False
