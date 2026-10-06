"""The Run view of the GTK Telemetry tab (docs/telemetry-ui-design.md 7.3, 7.8): a run against the PB or the
previous run, the strips on one distance axis with one shared cursor, the stage map by the time gained or lost per
split, where the time went with the coach's advice, g-g. It follows the web page's Run view, on the same data
(run_analysis) drawn with cairo (telemetry_plot). The cursor follows the pointer over the strips and the map;
a row of the table (selected, or under the pointer) shows the coach's advice for that split."""
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import GLib, Gtk, Gdk
from locale import gettext as _

from . import run_analysis, telemetry_plot as plot
from .telemetry_view import clock, signed

SCALE = 1.3                             # the strips are taller than on a phone, as on the desktop page
KINDS = {'top3': _("TOP 3"), 'focus': _("FOCUS"), 'tip': _("CALL"), 'praise': _("GOOD"), 'technique': _("TECHNIQUE"), 'note': _("NOTE"),
         'still': _("NOTE")}


def advice_markup(tips, empty):
    """Pango markup of a split's advice in the debrief's style: cost, place, kind, the coach's sentence and its
    evidence; `empty` where there is none."""
    if not tips:
        return '<span foreground="{}">{}</span>'.format(plot.DIM, GLib.markup_escape_text(empty))
    out = []
    for tip in tips:
        cost, kind, km, text = run_analysis.advice_line(tip)
        head = ['<b><span foreground="{}">{}</span></b>'.format(plot.FASTER if kind == 'praise' else plot.SLOWER, cost)
                ] if cost else []
        if km:
            head.append(km)
        head.append('<b>{}</b>'.format(GLib.markup_escape_text(KINDS.get(kind, _("CALL")))))
        lines = [' · '.join(head), GLib.markup_escape_text(text)]
        lines += ['<span foreground="{}">{}</span>'.format(plot.DIM, GLib.markup_escape_text(e)) for e in tip.get('evidence') or []]
        out.append('\n'.join(lines))
    return '\n\n'.join(out)


class RunView(Gtk.Box):
    """The widget. `set_source(reader, profile, car_id)` says where the runs are (reader: a callable giving a
    read-only Reader, or None); the view reads when it is shown and the car or the runs changed."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.set_border_width(12)
        self._source = None
        self._loaded = None              # (car, profile) the run list was read for
        self._runs = []
        self._vs = 'pb'
        self._run_id = None
        self._data = None
        self._head = None
        self._advice = {}
        self._cursor = 0.0
        self._view = (0.0, 1.0)
        self._zoom = -1
        self._sel = -1
        self._gutter, self._plot_w = float(plot.GUT), 100.0
        self._map_points = None
        self._quiet = False

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self._title = Gtk.Label(xalign=0)
        header.pack_start(self._title, True, True, 0)
        self._combo = Gtk.ComboBoxText()
        self._combo.connect('changed', self._on_run_changed)
        header.pack_start(self._combo, False, False, 0)
        chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        chips.get_style_context().add_class('linked')
        self._pb = Gtk.RadioButton.new_with_label_from_widget(None, _("vs PB"))
        self._prev = Gtk.RadioButton.new_with_label_from_widget(self._pb, _("vs previous"))
        for button, vs in ((self._pb, 'pb'), (self._prev, 'prev')):
            button.set_mode(False)
            button.connect('toggled', self._on_compare, vs)
            chips.pack_start(button, False, False, 0)
        header.pack_start(chips, False, False, 0)
        self.pack_start(header, False, False, 0)
        self._sub = Gtk.Label(xalign=0)
        self._sub.get_style_context().add_class('dim-label')
        self.pack_start(self._sub, False, False, 0)

        self._empty = Gtk.Label(xalign=0, label=_("No run to show yet: the analysis appears once a stage has been driven."))
        self._empty.get_style_context().add_class('dim-label')
        self.pack_start(self._empty, False, False, 0)

        self._body = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._body.set_wide_handle(True)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._readout = Gtk.Label(xalign=0)
        self._readout.set_use_markup(True)
        left.pack_start(self._readout, False, False, 0)
        self._readout_advice = Gtk.Label(xalign=0, yalign=0)
        self._readout_advice.set_line_wrap(True)
        self._readout_advice.set_max_width_chars(80)
        self._readout_advice.set_lines(4)
        self._readout_advice.set_ellipsize(3)
        left.pack_start(self._readout_advice, False, False, 0)
        self._strips = Gtk.DrawingArea()
        self._strips.set_can_focus(True)
        self._strips.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON_PRESS_MASK
                                | Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.KEY_PRESS_MASK)
        self._strips.connect('draw', self._draw_strips)
        self._strips.connect('motion-notify-event', self._on_strips_motion)
        self._strips.connect('button-press-event', self._on_strips_press)
        self._strips.connect('scroll-event', self._on_strips_scroll)
        self._strips.connect('key-press-event', self._on_key)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(self._strips)
        left.pack_start(scrolled, True, True, 0)
        legend = Gtk.Label(xalign=0)
        legend.set_use_markup(True)
        self._legend = legend
        left.pack_start(legend, False, False, 0)
        zoom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for label, handler in (("◀", lambda w: self._step(-1)), (_("All"), lambda w: self._zoom_to(-1)),
                               ("▶", lambda w: self._step(1))):
            button = Gtk.Button(label=label)
            button.connect('clicked', handler)
            zoom.pack_start(button, False, False, 0)
        self._zoom_name = Gtk.Label(xalign=0.5)
        self._zoom_name.get_style_context().add_class('dim-label')
        self._zoom_name.set_ellipsize(3)
        zoom.pack_start(self._zoom_name, True, True, 0)
        left.pack_start(zoom, False, False, 0)
        self._body.pack1(left, True, False)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.set_margin_start(8)
        right.pack_start(self._heading(_("Stage map · time per split")), False, False, 0)
        self._map = Gtk.DrawingArea()
        self._map.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON_PRESS_MASK)
        self._map.connect('draw', self._draw_map)
        self._map.connect('button-press-event', self._on_map)
        self._map.connect('motion-notify-event', self._on_map)
        right.pack_start(self._map, False, False, 0)
        self._map_note = Gtk.Label(xalign=0)
        self._map_note.get_style_context().add_class('dim-label')
        self._map_note.set_line_wrap(True)
        right.pack_start(self._map_note, False, False, 0)
        right.pack_start(self._heading(_("Where the time went"), _("select a row for the coach's advice")), False, False, 0)
        # i, name, loss text, loss, min, exit, brake, colour, apex, time available (against the grip potential)
        self._store = Gtk.ListStore(int, str, str, float, str, str, str, str, float, str)
        self._table = Gtk.TreeView(model=self._store)
        self._table.set_headers_clickable(True)
        for n, (title, col) in enumerate(((_("Split"), 1), (_("Loss"), 2), (_("Avail"), 9), (_("Min"), 4), (_("Exit"), 5), (_("Brake"), 6))):
            renderer = Gtk.CellRendererText()
            if n == 0:
                renderer.set_property('ellipsize', 3)
                renderer.set_property('width-chars', 24)
            if n:
                renderer.set_property('xalign', 1.0)
            column = Gtk.TreeViewColumn(title, renderer, text=col)
            if n == 1:
                column.add_attribute(renderer, 'foreground', 7)
            column.set_sort_column_id({0: 8, 1: 3}.get(n, col))
            if n == 0:
                column.set_expand(True)
            self._table.append_column(column)
        self._store.set_sort_column_id(3, Gtk.SortType.DESCENDING)
        self._table.get_selection().connect('changed', self._on_row)
        self._table.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self._table.connect('motion-notify-event', self._on_row_hover)
        self._table.connect('leave-notify-event', lambda w, e: self._pop.popdown())
        table_scroll = Gtk.ScrolledWindow()
        table_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        table_scroll.set_min_content_height(180)
        table_scroll.set_max_content_height(260)
        table_scroll.set_propagate_natural_height(True)
        table_scroll.add(self._table)
        right.pack_start(table_scroll, False, False, 0)
        self._pop = Gtk.Popover.new(self._table)
        self._pop.set_position(Gtk.PositionType.LEFT)
        self._pop.set_modal(False)
        self._pop_label = Gtk.Label(xalign=0, yalign=0)
        self._pop_label.set_use_markup(True)
        self._pop_label.set_line_wrap(True)
        self._pop_label.set_max_width_chars(60)
        self._pop_label.set_margin_start(10)
        self._pop_label.set_margin_end(10)
        self._pop_label.set_margin_top(8)
        self._pop_label.set_margin_bottom(8)
        self._pop.add(self._pop_label)
        self._pop_label.show()
        self._row_advice = Gtk.Label(xalign=0, yalign=0)
        self._row_advice.set_use_markup(True)
        self._row_advice.set_line_wrap(True)
        self._row_advice.set_max_width_chars(60)
        right.pack_start(self._row_advice, False, False, 0)
        self._sectors = Gtk.Label(xalign=0)
        self._sectors.set_use_markup(True)
        right.pack_start(self._sectors, False, False, 0)
        right.pack_start(self._heading(_("g-g · whole run")), False, False, 0)
        self._gg = Gtk.DrawingArea()
        self._gg.set_size_request(-1, 240)
        self._gg.connect('draw', self._draw_gg)
        right.pack_start(self._gg, False, False, 0)
        side = Gtk.ScrolledWindow()
        side.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        side.add(right)
        self._body.pack2(side, False, False)
        self.pack_start(self._body, True, True, 0)
        self._body.set_position(600)
        self.show_all()
        self._show_state(None)

    @staticmethod
    def _heading(text, hint=None):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        label = Gtk.Label(xalign=0)
        label.set_markup('<b>{}</b>'.format(GLib.markup_escape_text(text.upper())))
        label.get_style_context().add_class('telemetry-header')
        box.pack_start(label, False, False, 0)
        if hint:
            dim = Gtk.Label(label=hint, xalign=1)
            dim.get_style_context().add_class('dim-label')
            box.pack_end(dim, False, False, 0)
        return box

    # -- loading --

    def set_source(self, reader, profile, car_id):
        """`reader`: a callable returning a read-only telemetry_store.Reader (or None)."""
        self._source = (reader, profile, car_id)

    def refresh(self):
        """Read the car's runs if the car or profile changed since they were read (called when the view is
        shown). Keeps the run being looked at unless a newer finished one arrived and none was picked."""
        if self._source is None:
            return
        reader_fn, profile, car_id = self._source
        key = (car_id, profile)
        if car_id is None:
            self._show_state(_("No car yet: the run analysis appears once a stage has been driven."))
            return
        reader = reader_fn() if callable(reader_fn) else reader_fn
        if reader is None:
            return
        if key != self._loaded:
            self._loaded = key
            self._run_id = None
        try:
            self._runs = run_analysis.recent_runs(reader, car_id, 20)
        except Exception:
            import logging
            logging.exception("run list")
            self._runs = []
        self._quiet = True
        self._combo.remove_all()
        for r in self._runs:
            self._combo.append(str(r['id']), '{} · {} · {}{}{}'.format(
                _("Run {}").format(r['n']), clock(r['time']) if r['time'] is not None else _("not finished"),
                _date(r['started']), ' · PB' if r['pb'] else '',
                '' if r['run_class'] == 'clean' else ' · ' + str(r['run_class'])))
        self._quiet = False
        if not self._runs:
            self._show_state(_("No run to show yet: the analysis appears once a stage has been driven."))
            return
        ids = [r['id'] for r in self._runs if r['trace']]
        newest = next((r['id'] for r in self._runs if r['trace'] and r['time'] is not None), ids[0] if ids else None)
        pick = self._run_id if self._run_id in ids else newest
        if pick is None:
            self._show_state(_("No run has a trace yet."))
            return
        self.open_run(pick, self._vs, pick != self._run_id)

    def open_run(self, run_id, vs='pb', zoom=True, distance=None):
        """Show a run against `vs` ('pb' or 'prev'), opened on the biggest loss (or at `distance` m)."""
        if self._source is None:
            return
        reader_fn, profile, car_id = self._source
        reader = reader_fn() if callable(reader_fn) else reader_fn
        try:
            data = run_analysis.analysis(reader, run_id, vs)
            tips = run_analysis.car_tips(reader, profile, car_id)
            head = run_analysis.head(reader, run_id, tips)
        except Exception:
            import logging
            logging.exception("run analysis")
            data = head = None
        if data is None or head is None:
            self._show_state(_("This run can't be read: it has no trace."))
            return
        self._run_id, self._vs, self._data, self._head = run_id, vs, data, head
        self._advice = run_analysis.advice_by_section(head['advice'], data['sections'])
        self._quiet = True
        self._combo.set_active_id(str(run_id))
        self._pb.set_active(vs == 'pb')
        self._prev.set_active(vs == 'prev')
        self._pb.set_sensitive(head['compare'][0]['run'] is not None)
        self._prev.set_sensitive(head['compare'][1]['run'] is not None)
        self._quiet = False
        self._show_state(None)
        self._sel = -1
        if zoom:
            self._view, self._cursor = (0.0, data['length']), 0.0
            if distance is not None:
                self._cursor = distance
                self._zoom_to(run_analysis_section(data, distance), False)
            else:
                k = run_analysis.biggest_loss(data['sections'])
                self._zoom_to(-1 if k is None else k, False)
        self._fill()

    def _show_state(self, message):
        self._empty.set_text(message or '')
        self._empty.set_visible(bool(message))
        self._body.set_visible(message is None and self._data is not None)
        if message:
            self._title.set_text('')
            self._sub.set_text('')

    def _fill(self):
        data, head = self._data, self._head
        stage = (head['stage_name'] or '').split(' - ')[0]
        ref = data['ref']
        delta = None if head['time'] is None or data['ref_time'] is None else head['time'] - data['ref_time']
        self._title.set_markup('<b>{}</b>   <span font_features="tnum" size="x-large"><b>{}</b></span>   {}'.format(
            GLib.markup_escape_text(stage), clock(head['time']) if head['time'] is not None else _("Not finished"),
            '' if delta is None else '<span foreground="{}"><b>{}</b></span>'.format(
                plot.SLOWER if round(delta, 1) > 0 else plot.FASTER, signed(delta))))
        self._sub.set_text(' · '.join(x for x in (
            _("Run {}").format(head['n']), head['run_class'] or '', _date(head['started']),
            _("vs {} {}").format(ref['label'], clock(ref['time'])) if ref else _("No run to compare with yet")) if x))
        self._legend.set_markup('<span foreground="{}">—</span> {}   <span foreground="{}">—</span> {}'.format(
            plot.SPEED, _("Run {}").format(head['n']), plot.REF,
            GLib.markup_escape_text((ref['label'] + (' · PB' if data['vs'] == 'pb' else ' · ' + _("previous"))) if ref
                                    else _("no comparison"))))
        self._store.clear()
        top = max([0.1] + [abs(s['loss']) for s in data['sections'] if s['loss'] is not None and not s['off']])
        for s in data['sections']:
            if s['loss'] is None:
                continue
            note = _(" (launch)") if s['first'] else _(" (finish)") if s['last'] else ''
            num = lambda v: '–' if v is None or s['off'] or abs(v) < 1 else signed(v).replace('.0', '')
            self._store.append([s['i'], '{}. {}{}  {:.2f} km'.format(s['i'] + 1, s['name'].replace('the ', '', 1), note,
                                                                  s['apex'] / 1000.0),
                                _("off") if s['off'] else signed(s['loss']), -1e9 if s['off'] else s['loss'],
                                num(s.get('dmin')), num(s.get('dexit')), num(s.get('dbrake')),
                                plot.FAINT if s['off'] else plot.SLOWER if s['loss'] > 0.05 else plot.FASTER
                                if s['loss'] < -0.05 else plot.DIM, s['apex'],
                                '–' if s.get('avail') is None or s['off'] else '{:.1f}'.format(s['avail'])])
        self._sectors.set_markup('   '.join('<b>{}</b> <tt>{}</tt> <span foreground="{}"><tt>{}</tt></span>'.format(
            s['name'], '–' if s['time'] is None else ('≈' if s.get('confidence') == 'low' else '') + '{:.1f}'.format(s['time']),
            plot.DIM if s['delta'] is None else plot.SLOWER if round(s['delta'], 1) > 0 else plot.FASTER,
            '' if s['delta'] is None else signed(s['delta'])) for s in data.get('sectors') or []))
        self._row_advice.set_text('')
        has_pos = plot.map_points(data, 100, 100) is not None
        self._map.set_size_request(-1, 44 if not has_pos else 280)
        self._map_note.set_text('' if has_pos else _("No position for this run: the map needs runs after the "
                                                     "2026-09-27 bridge fix. The splits are drawn along the distance."))
        self._map_note.set_visible(not has_pos)
        self._strips.set_size_request(-1, plot.strips_height(data, SCALE))
        self._redraw()

    # -- drawing --

    def _redraw(self):
        if self._data is None:
            return
        self._readout_update()
        for area in (self._strips, self._map, self._gg):
            area.queue_draw()

    def _draw_strips(self, area, cr):
        if self._data is None:
            return False
        w = area.get_allocated_width()
        self._gutter, self._plot_w = plot.strips(cr, w, area.get_allocated_height(), self._data, self._view,
                                                 self._cursor, SCALE)
        return False

    def _draw_map(self, area, cr):
        if self._data is not None:
            self._map_points = plot.stage_map(cr, area.get_allocated_width(), area.get_allocated_height(), self._data,
                                              self._cursor)
        return False

    def _draw_gg(self, area, cr):
        if self._data is not None:
            plot.gg(cr, area.get_allocated_width(), area.get_allocated_height(), self._data)
        return False

    def _readout_update(self):
        data = self._data
        d = max(0.0, min(data['length'], self._cursor))
        i = max(0, min(len(data['this']['t']) - 1, int(round(d / data['step']))))
        k = plot.section_at(data, d)
        section = next((s for s in data['sections'] if s['i'] == k), None)
        this, cmp_ = data['this'], data.get('cmp')

        def pair(name, label, fn, colour):
            a = this[name][i]
            b = cmp_[name][i] if cmp_ else None
            text = '<span foreground="{}">{}</span> <tt>{}</tt>'.format(colour, label, '\u2013' if a is None else fn(a))
            if cmp_:
                text += ' <span foreground="{}"><tt>{}</tt></span>'.format(plot.REF, '\u2013' if b is None else fn(b))
            return text
        dt = None if not cmp_ or this['t'][i] is None or cmp_['t'][i] is None else this['t'][i] - cmp_['t'][i]
        line1 = '<b><tt>{:.2f} km</tt></b> · {}{}'.format(
            d / 1000.0, GLib.markup_escape_text(_("split {} · {}").format(k + 1, section['short']) if section else ''),
            '' if dt is None else '   <span foreground="{}"><tt>Δ {} s</tt></span>'.format(
                plot.SLOWER if dt > 0.005 else plot.FASTER, '{}{:.2f}'.format('+' if dt > 0 else '−', abs(dt))))
        line2 = '   '.join((pair('speed', 'km/h', lambda v: '{:.0f}'.format(v), plot.SPEED),
                            pair('throttle', 'thr', lambda v: '{:.0f}'.format(v * 100), plot.THROTTLE),
                            pair('brake', 'brk', lambda v: '{:.0f}'.format(v * 100), plot.BRAKE),
                            pair('gear', 'gear', lambda v: 'N' if v == 0 else 'R' if v < 0 else '{}'.format(v), plot.GEAR),
                            pair('rpm', 'rpm', lambda v: '{:.1f}k'.format(v / 1000.0), plot.RPM),
                            pair('steer', 'steer', lambda v: '{:.0f}'.format(v * 100), plot.STEER)))
        self._readout.set_markup(line1 + '\n' + line2)
        tips = self._advice.get(k) or []
        self._readout_advice.set_markup(advice_markup(tips[:1], '') if tips else '')
        self._readout_advice.set_visible(bool(tips))

    # -- the strips: cursor, zoom, keys --

    def _distance_at(self, x):
        a, z = self._view
        return a + (x - self._gutter) / self._plot_w * (z - a)

    def _set_cursor(self, d):
        self._cursor = max(self._view[0], min(self._view[1], d))
        self._redraw()

    def _on_strips_motion(self, area, event):
        if self._data is not None:
            self._set_cursor(self._distance_at(event.x))
        return False

    def _on_strips_press(self, area, event):
        area.grab_focus()
        if self._data is None:
            return False
        self._set_cursor(self._distance_at(event.x))
        if event.type == Gdk.EventType._2BUTTON_PRESS:
            k = plot.section_at(self._data, self._cursor)
            self._zoom_to(-1 if self._zoom == k else k)
        return True

    def _scale_view(self, factor, around):
        a, z = self._view
        length = self._data['length']
        span = max(100.0, min(length, (z - a) * factor))
        lo = max(0.0, min(length - span, around - (around - a) * span / (z - a)))
        self._view = (lo, lo + span)
        if span >= length:
            self._zoom = -1
            self._zoom_name.set_text(_("Whole stage"))
        self._set_cursor(self._cursor)

    def _on_strips_scroll(self, area, event):
        if self._data is None:
            return False
        self._scale_view(1.25 if event.direction == Gdk.ScrollDirection.DOWN else 0.8, self._distance_at(event.x))
        return True

    def _on_key(self, area, event):
        if self._data is None:
            return False
        key, shift = event.keyval, bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        if key == Gdk.KEY_Left:
            self._set_cursor(self._cursor - (100 if shift else 10))
        elif key == Gdk.KEY_Right:
            self._set_cursor(self._cursor + (100 if shift else 10))
        elif key in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self._scale_view(0.8, self._cursor)
        elif key in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self._scale_view(1.25, self._cursor)
        elif key == Gdk.KEY_0:
            self._zoom_to(-1)
        elif key == Gdk.KEY_bracketleft:
            self._step(-1)
        elif key == Gdk.KEY_bracketright:
            self._step(1)
        else:
            return False
        return True

    def _zoom_to(self, k, redraw=True):
        data = self._data
        sections = data['sections']
        if k is None or k < 0 or k >= len(sections):
            self._zoom, self._view = -1, (0.0, data['length'])
            self._zoom_name.set_text(_("Whole stage"))
        else:
            s = sections[k]
            self._zoom, self._view = k, (max(0.0, s['d0'] - 60), min(data['length'], s['d1'] + 60))
            if not s['d0'] <= self._cursor <= s['d1']:
                self._cursor = max(self._view[0], min(self._view[1], s['apex']))
            self._zoom_name.set_text(_("Split {} · {} · {:.2f} km").format(
                k + 1, s['name'].replace('the ', '', 1), s['apex'] / 1000.0))
        if redraw:
            self._redraw()

    def _step(self, by):
        if self._data is None:
            return
        k = plot.section_at(self._data, self._cursor) if self._zoom < 0 else self._zoom
        self._zoom_to(max(0, min(len(self._data['sections']) - 1, k + by)))

    def _on_map(self, area, event):
        if self._data is None:
            return False
        if event.type == Gdk.EventType.MOTION_NOTIFY and event.state & Gdk.ModifierType.BUTTON1_MASK:
            return False
        data = self._data
        pts = self._map_points
        if pts is None:
            d = (event.x - 4) / max(1, area.get_allocated_width() - 8) * data['length']
        else:
            best, near = None, 1e12
            for i, p in enumerate(pts):
                if p is None:
                    continue
                dist = (p[0] - event.x) ** 2 + (p[1] - event.y) ** 2
                if dist < near:
                    best, near = i, dist
            if best is None:
                return False
            d = min(data['length'], best * data['step'])
        self._cursor = max(0.0, min(data['length'], d))
        if not self._view[0] <= self._cursor <= self._view[1]:
            self._zoom_to(plot.section_at(data, self._cursor), False)
        self._redraw()
        return True

    # -- the table --

    def _on_run_changed(self, combo):
        if self._quiet:
            return
        run_id = combo.get_active_id()
        if run_id is not None:
            self.open_run(int(run_id), self._vs)

    def _on_compare(self, button, vs):
        if self._quiet or not button.get_active() or self._run_id is None or vs == self._vs:
            return
        self.open_run(self._run_id, vs, False)

    def _on_row(self, selection):
        model, it = selection.get_selected()
        if it is None or self._data is None:
            self._sel = -1
            self._row_advice.set_text('')
            return
        self._sel = model[it][0]
        self._zoom_to(self._sel)
        empty = _("The coach has nothing to say about this split.") if self._head['advice'] else \
            _("No advice from the coach for this run: it advises on the car's latest run.")
        self._row_advice.set_markup(advice_markup(self._advice.get(self._sel), empty))

    def _on_row_hover(self, table, event):
        if self._data is None:
            return False
        found = table.get_path_at_pos(int(event.x), int(event.y))
        if not found:
            self._pop.popdown()
            return False
        path = found[0]
        rect = table.get_cell_area(path, table.get_column(0))
        k = self._store[path][0]
        empty = _("The coach has nothing to say about this split.") if self._head['advice'] else \
            _("No advice from the coach for this run: it advises on the car's latest run.")
        self._pop_label.set_markup(advice_markup(self._advice.get(k), empty))
        self._pop.set_pointing_to(rect)
        self._pop.popup()
        return False


def run_analysis_section(data, d):
    return plot.section_at(data, d)


def _date(started):
    import time
    return time.strftime('%-d %b %H:%M', time.localtime(started)) if started else ''
