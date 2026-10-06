"""Cairo drawing for the Telemetry tab, following the web page's visual system (docs/telemetry-ui-design.md,
section 4): the splits ribbon and the live dash. Plain data in, a cairo context to draw on, so the functions are
tested by rendering to an ImageSurface."""
import math

# The web page's tokens (dark): LiveSplit's colours for the splits, the shift lights, the ink
TONES = {'gold': '#d8af1f', 'ahead-gain': '#29cc54', 'ahead-lose': '#70cc89', 'behind-gain': '#cc7870',
         'behind-lose': '#cc3729', 'none': '#2a313a'}
PANEL, LINE, TEXT, DIM, FAINT, ACCENT = '#11151a', '#232a33', '#eef2f6', '#8c96a3', '#56606c', '#4c9dff'
LED_OFF, LED_G, LED_Y, LED_R, LED_B = '#1f252d', '#2fd27a', '#ffc21a', '#ff4d4d', '#4c9dff'
LIGHTS = 15
FONT = 'monospace'


def rgb(cr, colour, alpha=1.0):
    cr.set_source_rgba(int(colour[1:3], 16) / 255.0, int(colour[3:5], 16) / 255.0, int(colour[5:7], 16) / 255.0, alpha)


def rounded(cr, x, y, w, h, r):
    r = min(r, w / 2.0, h / 2.0)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def text(cr, s, x, y, size, colour, bold=False, align='left', alpha=1.0):
    """`s` at baseline (x, y), left, right or centre aligned."""
    import cairo
    cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(size)
    extents = cr.text_extents(s)
    if align == 'right':
        x -= extents.x_advance
    elif align == 'center':
        x -= extents.x_advance / 2.0
    cr.move_to(x, y)
    rgb(cr, colour, alpha)
    cr.show_text(s)
    cr.new_path()                       # show_text leaves a current point that a later arc would draw a line from


def ribbon(cr, w, h, tones, sector_tones=(), bounds=None, gap=1.5, sector_w=22.0, current=None, pulse=1.0):
    """One cell per split, then (after a wider gap) one fixed-width cell per game sector, each in its tone. The
    split cells are as long as the splits are (`bounds`: (d0, d1) per split, in metres) when all are known, else
    equal."""
    cells = list(tones)
    sectors = list(sector_tones)
    spare = 5.0 if sectors and cells else 0.0
    sectors_w = len(sectors) * sector_w + max(0, len(sectors) - 1) * gap
    avail = w - (sectors_w + spare if sectors else 0)
    x = 0.0
    if cells:
        usable = avail - gap * (len(cells) - 1)
        lengths = [max(1.0, (b[1] - b[0])) if b and None not in b else None for b in (bounds or [])]
        if len(lengths) != len(cells) or None in lengths:
            lengths = [1.0] * len(cells)
        total = sum(lengths)
        for j, (tone, length) in enumerate(zip(cells, lengths)):
            each = usable * length / total
            rounded(cr, x, 0, each, h, 1.5)
            if j == current:                            # a live run: the split the car is in, pulsing
                rgb(cr, TEXT, pulse)
            else:
                rgb(cr, TONES.get(tone, TONES['none']))
            cr.fill()
            x += each + gap
    x = w - sectors_w
    for tone in sectors:
        rounded(cr, x, 0, sector_w, h, 1.5)
        rgb(cr, TONES.get(tone, TONES['none']))
        cr.fill()
        x += sector_w + gap


def lights_on(rpm, target):
    """How many of the 15 lights are lit and whether they flash: the first at 60 % of the change-up point, the
    last at it, all flashing above it (the web page's rule)."""
    if not target:
        return 0, False
    on = max(0, min(LIGHTS, math.ceil(LIGHTS * (rpm - 0.6 * target) / (0.4 * target))))
    return on, rpm >= target


def dash(cr, w, h, d, flash=False):
    """The live dash on its own dark panel: the 15 shift lights, the gear, the speed and rpm, the change-up
    point, the way through the stage. `d`: gear, speed (km/h), rpm, shift_rpm, learnt, limiter, progress (0..1
    or None), km (text), note (a line shown instead of the stage when there is no data), dim (last values of a
    pause, faded) or None for no telemetry at all."""
    rgb(cr, PANEL)
    rounded(cr, 0.5, 0.5, w - 1, h - 1, 10)
    cr.fill_preserve()
    rgb(cr, LINE)
    cr.set_line_width(1)
    cr.stroke()
    pad = 14.0
    alpha = 1.0
    if d is None or d.get('dim'):
        alpha = 0.38
    lights_h = 16.0
    cell = (w - 2 * pad - 3 * (LIGHTS - 1)) / LIGHTS
    on, over = lights_on(d['rpm'], d.get('shift_rpm') or d.get('limiter')) if d else (0, False)
    for i in range(LIGHTS):
        colour = LED_OFF
        if over:
            colour = LED_B if flash else LED_OFF
        elif i < on:
            colour = LED_G if i < LIGHTS * 0.5 else LED_Y if i < LIGHTS * 0.8 else LED_R
        rounded(cr, pad + i * (cell + 3), pad, cell, lights_h, 3)
        rgb(cr, colour, alpha if colour != LED_OFF else 1.0)
        cr.fill()
    top = pad + lights_h + 8
    stage_h = 44.0
    body_h = h - top - stage_h - pad
    gear = '\u2013'
    if d and d.get('gear') is not None:
        gear = 'R' if d['gear'] < 0 else 'N' if d['gear'] == 0 else str(d['gear'])
    hot = bool(d and d.get('shift_rpm') and d['rpm'] >= d['shift_rpm'])
    gear_size = max(40.0, min(body_h * 1.15, w * 0.34))
    text(cr, gear, pad + (w * 0.42 - pad) / 2.0, top + body_h * 0.92, gear_size, LED_R if hot else TEXT, True, 'center',
         alpha if d else 0.38)
    right = w - pad
    speed = '\u2013' if not d or d.get('speed') is None else '{:.0f}'.format(d['speed'])
    rpm = '\u2013' if not d or d.get('rpm') is None else '{:.0f}'.format(d['rpm'])
    big = max(24.0, min(body_h * 0.42, 60.0))
    text(cr, 'km/h', right, top + big * 0.95, 12, DIM, False, 'right')
    text(cr, speed, right - 46, top + big * 0.95, big, TEXT, True, 'right', alpha)
    mid = max(18.0, big * 0.55)
    text(cr, 'rpm', right, top + big + mid * 1.15, 12, DIM, False, 'right')
    text(cr, rpm, right - 34, top + big + mid * 1.15, mid, TEXT, False, 'right', alpha)
    if d and d.get('shift_rpm'):
        text(cr, 'change up at {:.0f} {}'.format(d['shift_rpm'], 'learnt' if d.get('learnt') else 'profile'),
             right, top + big + mid * 1.15 + 20, 12, DIM, False, 'right', alpha)
    # the stage: a line of words and the progress bar
    y = h - pad - stage_h + 8
    rgb(cr, LINE)
    cr.rectangle(pad, y - 10, w - 2 * pad, 1)
    cr.fill()
    line = (d or {}).get('note') or (d or {}).get('km') or ''
    text(cr, (d or {}).get('title') or '', pad, y + 12, 13, TEXT if d else DIM, True)
    text(cr, line, right, y + 12, 12, DIM, False, 'right')
    bar_y = y + 22
    rgb(cr, LED_OFF)
    rounded(cr, pad, bar_y, w - 2 * pad, 6, 3)
    cr.fill()
    share = (d or {}).get('progress')
    if share:
        rgb(cr, ACCENT, alpha)
        rounded(cr, pad, bar_y, max(6.0, (w - 2 * pad) * max(0.0, min(1.0, share))), 6, 3)
        cr.fill()


# -- the Run view (docs/telemetry-ui-design.md 7.3, 7.8): strips on one distance axis, the stage map, g-g --
# `data` is run_analysis.analysis(): `this` and `cmp` channels on a grid `step` m apart, `sections`, `sectors`,
# `x` and `z`, `length`. Colours are the web page's dark tokens.

SPEED, THROTTLE, BRAKE, STEER, RPM, GEAR = '#4cc3ff', '#2fd27a', '#ff4d4d', '#b58cff', '#ffb020', '#e8edf2'
REF, SLOWER, FASTER, NEUTRAL, GRID, BG = '#8c96a3', '#ff5a4e', '#2fd27a', '#3a424d', '#1a2028', '#0a0c0f'
STRIPS = (('delta', 'Δ s', 56), ('v', 'km/h', 84), ('pedals', 'PEDAL', 56), ('steer', 'STEER', 44),
          ('gear', 'GEAR', 40), ('rpm', 'RPM', 56))
LANE, LANE2, GAP, GUT = 16, 12, 6, 34


def _strips(data, scale):
    return [(k, label, int(round(h * scale))) for k, label, h in STRIPS if k != 'delta' or data.get('cmp')]


def strips_height(data, scale=1.0):
    """The height the strip stack needs."""
    return LANE + (LANE2 if data.get('sectors') else 0) + sum(h + GAP for _k, _l, h in _strips(data, scale))


def section_at(data, d):
    """The index of the section `d` falls in (the last one past the end), None without sections."""
    sections = data.get('sections') or []
    for s in sections:
        if s['d0'] <= d < s['d1']:
            return s['i']
    if not sections:
        return None
    return sections[0]['i'] if d < sections[0]['d0'] else sections[-1]['i']


def _line(cr, pts, colour, width=1.5, alpha=1.0):
    pen = False
    cr.set_line_width(width)
    rgb(cr, colour, alpha)
    for p in pts:
        if p is None:
            pen = False
        elif pen:
            cr.line_to(*p)
        else:
            cr.move_to(*p)
            pen = True
    cr.stroke()


def _rect(cr, x, y, w, h, colour, alpha=1.0):
    rgb(cr, colour, alpha)
    cr.rectangle(x, y, w, h)
    cr.fill()


def strips(cr, w, h, data, view, cursor, scale=1.0):
    """The strip stack on one distance axis from `view` (a, z) m: a lane of split numbers (and the game's sectors),
    then the delta against the comparison, speed, pedals, steer, gear and rpm, this run in the channel's colour
    and the comparison grey, the cursor through all of them. Returns (gutter px, plot width px): the x of
    distance d is gutter + (d - a) / (z - a) * width."""
    import bisect
    rgb(cr, PANEL)
    cr.paint()
    this, cmp_ = data['this'], data.get('cmp')
    step, n = data['step'], len(this['t'])
    a, z = view
    width = w - GUT - 6.0

    def X(d):
        return GUT + (d - a) / (z - a) * width

    def index(d):
        return max(0, min(n - 1, int(round(d / step))))
    i0, i1 = max(0, index(a) - 1), min(n - 1, index(z) + 1)
    stride = max(1, int((i1 - i0) / (width * 1.5)))
    rows = _strips(data, scale)
    lane2 = LANE2 if data.get('sectors') else 0
    top0 = LANE + lane2
    total = top0 + sum(hh + GAP for _k, _l, hh in rows)
    current = section_at(data, cursor)
    delta = [None if cmp_ is None or this['t'][i] is None or cmp_['t'][i] is None else this['t'][i] - cmp_['t'][i]
             for i in range(n)]

    def pts(arr, f):
        return [None if arr[i] is None else (X(min(data['length'], i * step)), f(arr[i])) for i in range(i0, i1 + 1, stride)]
    for s in data['sections']:
        if s['d1'] < a or s['d0'] > z:
            continue
        x0, x1 = max(GUT, X(s['d0'])), min(GUT + width, X(s['d1']))
        if s['i'] % 2:
            _rect(cr, x0, top0, x1 - x0, total - top0, GRID, 0.55)
        if x1 - x0 > 14:
            text(cr, str(s['i'] + 1), x0 + 2, 11, 9.5, TEXT if s['i'] == current else FAINT, True)
    for s in data.get('sectors') or []:
        if s['d1'] < a or s['d0'] > z:
            continue
        x = max(GUT, X(s['d0']))
        _rect(cr, x, LANE, 1, lane2 - 1, ACCENT, 0.7)
        text(cr, s['name'], x + 3, LANE + 9, 9.5, ACCENT, True)
    y = top0
    for key, label, hh in rows:
        top, bot = y, y + hh
        _rect(cr, GUT, bot, width, 1, LINE)
        text(cr, label, 4, top + 11, 9.5, DIM, True)
        if key == 'delta':
            mx = 0.2
            for i in range(i0, i1 + 1, stride):
                if delta[i] is not None:
                    mx = max(mx, abs(delta[i]))
            mx = math.ceil(mx * 5) / 5.0

            def Y(v, top=top, hh=hh, mx=mx):
                return top + hh / 2.0 - max(-mx, min(mx, v)) / mx * (hh / 2.0 - 3)
            _rect(cr, GUT, Y(0), width, 1, DIM)
            p = [q for q in pts(delta, Y) if q is not None]
            if len(p) > 1:
                for sign, colour in ((1, SLOWER), (-1, FASTER)):
                    cr.save()
                    cr.rectangle(GUT, top if sign > 0 else Y(0), width, (Y(0) - top) if sign > 0 else (bot - Y(0)))
                    cr.clip()
                    cr.move_to(p[0][0], Y(0))
                    for q in p:
                        cr.line_to(*q)
                    cr.line_to(p[-1][0], Y(0))
                    rgb(cr, colour, 0.22)
                    cr.fill()
                    _line(cr, p, colour, 1.75)
                    cr.restore()
            text(cr, '+{:.1f}'.format(mx), 4, top + 24, 9.5, FAINT)
            text(cr, '−{:.1f}'.format(mx), 4, bot - 3, 9.5, FAINT)
        elif key == 'v':
            top_v = 100.0
            for src in (this, cmp_):
                if src:
                    top_v = max([top_v] + [src['speed'][i] for i in range(i0, i1 + 1, stride) if src['speed'][i] is not None])
            top_v = math.ceil(top_v / 25.0) * 25

            def Y(v, bot=bot, hh=hh, top_v=top_v):
                return bot - 2 - v / top_v * (hh - 4)
            for v in range(50, int(top_v), 50):
                _rect(cr, GUT, Y(v), width, 1, GRID)
            text(cr, '100', 6, Y(100) + 3, 9.5, FAINT)
            if cmp_:
                _line(cr, pts(cmp_['speed'], Y), REF, 1.25, 0.9)
            _line(cr, pts(this['speed'], Y), SPEED, 1.75)
        elif key == 'pedals':
            def Y(v, bot=bot, hh=hh):
                return bot - 2 - max(0.0, min(1.0, v)) * (hh - 4)
            if cmp_:
                _line(cr, pts(cmp_['throttle'], Y), REF, 1, 0.8)
                _line(cr, pts(cmp_['brake'], Y), REF, 1, 0.8)
            _line(cr, pts(this['throttle'], Y), THROTTLE, 1.75)
            _line(cr, pts(this['brake'], Y), BRAKE, 1.75)
        elif key == 'steer':
            def Y(v, top=top, hh=hh):
                return top + hh / 2.0 - max(-1.0, min(1.0, v)) * (hh / 2.0 - 3)
            _rect(cr, GUT, Y(0), width, 1, GRID)
            text(cr, 'L', 24, top + 10, 9.5, FAINT)
            text(cr, 'R', 24, bot - 3, 9.5, FAINT)
            if cmp_:
                _line(cr, pts(cmp_['steer'], Y), REF, 1, 0.8)
            _line(cr, pts(this['steer'], Y), STEER, 1.75)
        elif key == 'gear':
            gmax = max([5] + [g for g in this['gear'] if g is not None])

            def Y(v, bot=bot, hh=hh, gmax=gmax):
                return bot - 4 - (max(1, v) - 1) / float(gmax - 1) * (hh - 8)

            def steps(arr, Y=Y, X=X):
                out, prev = [], None
                for i in range(i0, i1 + 1, stride):
                    v = arr[i]
                    if v is None or v < 1:
                        out.append(None)
                        prev = None
                        continue
                    yy, x = Y(v), X(min(data['length'], i * step))
                    if prev is not None and yy != prev:
                        out.append((x, prev))
                    out.append((x, yy))
                    prev = yy
                return out
            if cmp_:
                _line(cr, steps(cmp_['gear']), REF, 1, 0.8)
            _line(cr, steps(this['gear']), GEAR, 1.75)
        elif key == 'rpm':
            seen = max([0] + [v for v in this['rpm'] if v is not None])
            limit = data.get('limiter') or math.ceil(seen / 500.0) * 500
            top_rpm = max(limit, seen)

            def Y(v, bot=bot, hh=hh, top_rpm=top_rpm):
                return bot - 2 - max(0.0, v - 2000.0) / (top_rpm - 2000.0) * (hh - 4)
            _rect(cr, GUT, Y(limit), width, Y(0.93 * limit) - Y(limit), RPM, 0.1)
            text(cr, '{:.1f}k'.format(limit / 1000.0), GUT + width - 4, Y(limit) + 9, 9.5, FAINT, False, 'right')
            if cmp_:
                _line(cr, pts(cmp_['rpm'], Y), REF, 1, 0.8)
            _line(cr, pts(this['rpm'], Y), RPM, 1.75)
        y = bot + GAP
    if a <= cursor <= z:
        _rect(cr, int(X(cursor)), LANE, 1, total - LANE, TEXT)
    return float(GUT), width


def loss_tone(loss):
    """The colour of a split's time against the comparison (the map's diverging scale, grey under 0.05 s)."""
    if loss is None or abs(loss) < 0.05:
        return NEUTRAL
    return SLOWER if loss > 0 else FASTER


def _map_alpha(loss):
    if loss is None or abs(loss) < 0.05:
        return 1.0
    return 0.35 + 0.65 * min(1.0, abs(loss) / 0.6)


def map_points(data, w, h, pad=30.0):
    """[(x, y) or None] per grid point: the road fitted to w by h, or None without a position."""
    xs, zs = data.get('x'), data.get('z')
    if not xs or not any(v is not None for v in xs):
        return None
    ok = [i for i, v in enumerate(xs) if v is not None]
    x0, x1 = min(xs[i] for i in ok), max(xs[i] for i in ok)
    z0, z1 = min(zs[i] for i in ok), max(zs[i] for i in ok)
    sc = min((w - 2 * pad) / max(1.0, x1 - x0), (h - 2 * pad) / max(1.0, z1 - z0))
    ox, oy = (w - (x1 - x0) * sc) / 2.0, (h - (z1 - z0) * sc) / 2.0
    return [None if xs[i] is None else (ox + (xs[i] - x0) * sc, h - (oy + (zs[i] - z0) * sc)) for i in range(len(xs))]


def start_label(pts, first, w, h, width=34.0):
    """(x, y, align) for the START label: the spot beside the marker (right or left of it, above or below) that is
    furthest from the road, kept on the map; the old below-right spot wins a tie."""
    road = [p for p in pts[::2] if p]
    best = None
    for dx, align in ((6, 'left'), (-6, 'right')):
        for dy in (14, -8, 26, -20):
            x0 = first[0] + dx - (width if align == 'right' else 0)
            y1 = first[1] + dy
            if x0 < 0 or x0 + width > w or y1 - 9 < 0 or y1 + 3 > h:
                continue
            gap = min((max(x0 - p[0], 0, p[0] - x0 - width) ** 2 + max(y1 - 9 - p[1], 0, p[1] - y1 - 3) ** 2
                       for p in road), default=1e9)
            if best is None or gap > best[0] + 1e-6:
                best = (gap, first[0] + dx, y1, align)
    if best is None:
        return first[0] + 6, first[1] + 12, 'left'
    return best[1], best[2], best[3]


def stage_map_size(data, w):
    return int(min(340, w * 0.8)) if map_points(data, 100, 100) is not None else 44


def stage_map(cr, w, h, data, cursor):
    """The stage coloured per split by the time gained or lost against the comparison; the road from its
    position, or a bar along the distance for a run that has none. Returns the points (or None)."""
    step, n = data['step'], len(data['this']['t'])
    pts = map_points(data, w, h)
    if pts is None:
        def X(d):
            return 4 + d / data['length'] * (w - 8)
        for s in data['sections']:
            loss = None if s['off'] else s['loss']
            _rect(cr, X(s['d0']), 4, max(1.0, X(s['d1']) - X(s['d0']) - 1), 24, loss_tone(loss), _map_alpha(loss))
            if loss is not None and abs(loss) >= 0.25 and X(s['d1']) - X(s['d0']) > 20:
                text(cr, str(s['i'] + 1), X(s['d0']) + 3, 19, 10, TEXT, True)
        for s in data.get('sectors') or []:               # the sector lines, labelled
            _rect(cr, X(s['d0']), 2, 1, 28, ACCENT, 0.8)
            text(cr, s['name'], X(s['d0']) + 3, 38, 9.5, ACCENT, True)
        x = X(cursor)
        rgb(cr, TEXT)
        cr.move_to(x, 30)
        cr.line_to(x - 5, 40)
        cr.line_to(x + 5, 40)
        cr.close_path()
        cr.fill()
        return None
    each = max(1, int(round(4 / step)))

    def road(i0, i1):
        return [pts[min(i, n - 1)] for i in range(i0, i1 + 1, each)]
    _line(cr, road(0, n - 1), BG, 9)
    for s in data['sections']:
        loss = None if s['off'] else s['loss']
        i0, i1 = int(round(s['d0'] / step)), int(round(s['d1'] / step))
        _line(cr, road(max(0, i0), min(n - 1, i1)), loss_tone(loss), 5, _map_alpha(loss))
        if loss is not None and abs(loss) >= 0.25:
            p = pts[max(0, min(n - 1, int(round(s['apex'] / step))))]
            if p:
                label = '{} {}'.format(s['i'] + 1, '{}{:.1f}'.format('+' if loss > 0 else '−', abs(loss)))
                text(cr, label, p[0] - 7 if p[0] > w - 70 else p[0] + 7, p[1] - 6, 10.5, TEXT, True,
                     'right' if p[0] > w - 70 else 'left')
    for s in (data.get('sectors') or [])[1:]:             # the game's sector lines (S2 on: S1 starts at START)
        p = pts[max(0, min(n - 1, int(round(s['d0'] / step))))]
        if p:
            _rect(cr, p[0] - 3, p[1] - 3, 6, 6, ACCENT)
            edge = p[0] > w - 30
            text(cr, s['name'], p[0] - 6 if edge else p[0] + 6, p[1] - 6, 10, ACCENT, True, 'right' if edge else 'left')
    first = next((p for p in pts if p), None)
    last = next((p for p in reversed(pts) if p), None)
    if first:
        _rect(cr, first[0] - 3, first[1] - 3, 6, 6, FASTER)
        sx, sy, align = start_label(pts, first, w, h)
        text(cr, 'START', sx, sy, 10, FASTER, True, align)
    if last:
        _rect(cr, last[0] - 3, last[1] - 3, 6, 6, TEXT)
        text(cr, 'FINISH', last[0] - 44 if last[0] > w - 60 else last[0] + 6, last[1] + 12, 10, TEXT, True)
    c = pts[max(0, min(n - 1, int(round(cursor / step))))]
    if c:
        rgb(cr, TEXT)
        cr.arc(c[0], c[1], 5.5, 0, 2 * math.pi)
        cr.fill_preserve()
        rgb(cr, PANEL)
        cr.set_line_width(2)
        cr.stroke()
    return pts


def gg(cr, w, h, data):
    """g-g for the whole run: lateral against longitudinal acceleration, this run over the comparison, rings at
    0.5 g and 1 g (a hit, over 2.5 g, is not grip and is left out)."""
    cx, cy = w / 2.0, h / 2.0
    radius = min(w, h) / 2.0 - 6
    sc = radius / 1.4
    rgb(cr, GRID)
    cr.set_line_width(1)
    for r in (0.5, 1.0):
        cr.arc(cx, cy, r * sc, 0, 2 * math.pi)
        cr.stroke()
    cr.move_to(cx - radius, cy)
    cr.line_to(cx + radius, cy)
    cr.move_to(cx, cy - radius)
    cr.line_to(cx, cy + radius)
    cr.stroke()
    text(cr, '1g', cx + sc + 2, cy - 3, 10, FAINT)
    text(cr, 'BRK', cx - 10, cy + radius - 1, 10, FAINT)
    text(cr, 'ACC', cx - 10, cy - radius + 9, 10, FAINT)
    for src, colour, alpha in ((data.get('cmp'), REF, 0.22), (data['this'], SPEED, 0.3)):
        if not src:
            continue
        rgb(cr, colour, alpha)
        for lat, lon in zip(src['a_lat'], src['a_long']):
            if lat is None or lon is None or abs(lat) > 2.5 or abs(lon) > 2.5:
                continue
            cr.rectangle(cx - lat * sc - 1, cy - lon * sc - 1, 2.2, 2.2)
        cr.fill()


# -- the live view (docs/telemetry-ui-design.md, 7.2 and 7.8): the delta block, the rolling strips, g-g with its
# trail and the stage minimap, on plain data from telemetry_view.LiveTrack --

LV_SLOWER, LV_FASTER, LV_GRID, LV_BG = '#ff5a4e', '#2fd27a', '#1a2028', '#0a0c0f'
LV_THR, LV_BRK, LV_STEER, LV_SPEED, LV_CLUTCH, LV_HB = '#2fd27a', '#ff4d4d', '#b58cff', '#4cc3ff', '#5fc4c4', '#ff8a3d'
LV_WINDOW, LV_GAP = 12.0, 0.5
LV_STRIPS_H = (64.0, 44.0, 24.0)             # pedals, steering, handbrake


def _live_panel(cr, w, h):
    rgb(cr, PANEL)
    rounded(cr, 0.5, 0.5, w - 1, h - 1, 10)
    cr.fill_preserve()
    rgb(cr, LINE)
    cr.set_line_width(1)
    cr.stroke()


def _live_path(cr, pts, colour, width, alpha=1.0):
    """A polyline through the points, lifting the pen at None."""
    pen = False
    cr.set_line_width(width)
    cr.set_line_join(1)                                     # cairo.LINE_JOIN_ROUND
    rgb(cr, colour, alpha)
    for p in pts:
        if p is None:
            pen = False
        elif pen:
            cr.line_to(*p)
        else:
            cr.move_to(*p)
            pen = True
    cr.stroke()


def live_delta(cr, w, h, d):
    """The delta block on its own panel: the big ±0.00, the ±2 s bar, and 'vs PB · split n ±x.xx' with the predicted
    finish. `d`: text (the ±0.00), delta (s), pb, split, finish (strings), or note (a line shown instead: no PB yet);
    dim (the last values of a pause); None draws the empty panel."""
    _live_panel(cr, w, h)
    if not d:
        return
    pad = 14.0
    alpha = 0.38 if d.get('dim') else 1.0
    if d.get('note'):
        text(cr, d['note'], pad, h / 2.0 + 5, 13, DIM, False, 'left', alpha)
        return
    delta = d.get('delta') or 0.0
    colour = LV_SLOWER if delta > 0.005 else LV_FASTER if delta < -0.005 else TEXT
    text(cr, d['text'], pad, pad + 34, 40, colour, True, 'left', alpha)
    bx, by, bh = pad + 150.0, pad + 12.0, 14.0
    bw = w - pad - bx
    if bw > 20:
        rgb(cr, LV_GRID)
        rounded(cr, bx, by, bw, bh, 3)
        cr.fill()
        f = max(-1.0, min(1.0, delta / 2.0))
        rgb(cr, LV_SLOWER if f > 0 else LV_FASTER, alpha)
        cr.rectangle(bx + bw / 2.0 + (0 if f > 0 else f * bw / 2.0), by, abs(f) * bw / 2.0, bh)
        cr.fill()
        rgb(cr, DIM)
        cr.rectangle(bx + bw / 2.0 - 0.75, by - 2, 1.5, bh + 4)
        cr.fill()
    y = pad + 34 + 22
    text(cr, 'vs {} {} · split {}'.format(d.get('ref_name', 'PB'), d.get('pb', '–'), d.get('split', '–')), pad, y, 12, DIM, False, 'left', alpha)
    text(cr, 'finish ≈ {}'.format(d.get('finish', '–')), w - pad, y, 12, DIM, False, 'right', alpha)


def live_rolling_height(handbrake=False):
    return 10 + 16 + LV_STRIPS_H[0] + (16 + LV_STRIPS_H[2] if handbrake else 0) + 8 + 16 + LV_STRIPS_H[1] + 10


def _live_series(rows, now, fn, x_of):
    """The last LV_WINDOW s of `rows` through fn(row) -> y or None, as points with None where a row is missing for
    over LV_GAP s (a pause, a page away): the line breaks instead of crossing the gap."""
    pts, last = [], None
    for r in rows:
        if r['t'] < now - LV_WINDOW - LV_GAP:
            continue
        y = fn(r)
        if y is None or (last is not None and r['t'] - last > LV_GAP):
            if pts and pts[-1] is not None:
                pts.append(None)
        if y is not None:
            pts.append((x_of(r['t']), y))
            last = r['t']
    return pts


def live_rolling(cr, w, h, rows, now):
    """Pedals (throttle and brake, a thin clutch line while it is used) and steering over the last 12 s, the
    newest at the right; a handbrake strip only while the rig reads it. `rows`: dicts with t, thr, brk, clu, hb,
    steer (None where unknown); `now`: the clock of the newest row."""
    _live_panel(cr, w, h)
    pad = 10.0
    inner = w - 2 * pad
    window = [r for r in rows if r['t'] >= now - LV_WINDOW]
    clutch = any(r.get('clu') is not None and r['clu'] > 0.05 for r in window)
    handbrake = any(r.get('hb') is not None and r['hb'] > 0.02 for r in window)

    def x_of(t):
        return pad + inner - (now - t) / LV_WINDOW * inner

    def label(y, left, right=''):
        text(cr, left, pad, y + 11, 10.5, DIM, True)
        if right:
            text(cr, right, w - pad, y + 11, 10.5, DIM, True, 'right')

    def grid(y, sh, lines):
        rgb(cr, LV_GRID)
        cr.set_line_width(1)
        for f in lines:
            cr.move_to(pad, y + f * sh)
            cr.line_to(pad + inner, y + f * sh)
        cr.stroke()

    def filled(y, sh, key, colour):
        pts = _live_series(rows, now, lambda r: None if r.get(key) is None
                           else y + sh - 1 - max(0.0, min(1.0, r[key])) * (sh - 2), x_of)
        run = []
        for p in pts + [None]:
            if p is None:
                if len(run) > 1:
                    rgb(cr, colour, 0.18)
                    cr.move_to(run[0][0], y + sh)
                    for q in run:
                        cr.line_to(*q)
                    cr.line_to(run[-1][0], y + sh)
                    cr.fill()
                run = []
            else:
                run.append(p)
        _live_path(cr, pts, colour, 1.75)

    y = pad
    label(y, 'PEDALS · LAST 12 S', 'THR · BRK' + (' · CLT' if clutch else ''))
    y += 16
    sh = LV_STRIPS_H[0]
    grid(y, sh, (0.01, 0.5, 0.99))
    filled(y, sh, 'thr', LV_THR)
    filled(y, sh, 'brk', LV_BRK)
    if clutch:
        pts = _live_series(rows, now, lambda r: None if r.get('clu') is None
                           else y + sh - 1 - max(0.0, min(1.0, r['clu'])) * (sh - 2), x_of)
        _live_path(cr, pts, LV_CLUTCH, 1.0)
    y += sh
    if handbrake:
        y += 8
        label(y, 'HANDBRAKE')
        y += 16
        sh = LV_STRIPS_H[2]
        grid(y, sh, (0.98,))
        filled(y, sh, 'hb', LV_HB)
        y += sh
    y += 8
    label(y, 'STEERING', 'L ▲ · R ▼')
    y += 16
    sh = LV_STRIPS_H[1]
    grid(y, sh, (0.5,))
    pts = _live_series(rows, now, lambda r: None if r.get('steer') is None
                       else y + sh / 2.0 - max(-1.0, min(1.0, r['steer'])) * (sh / 2.0 - 2), x_of)
    _live_path(cr, pts, LV_STEER, 1.75)


def live_gg(cr, w, h, rows, total=None):
    """g-g on its own panel: lateral against longitudinal acceleration (g), rings at 0.5 g and 1 g, the last 3 s as a
    fading trail and the car as a dot. `rows`: dicts with alat and along in g (None where unknown)."""
    _live_panel(cr, w, h)
    text(cr, 'g-g', 12, 20, 10.5, DIM, True)
    if total is not None:
        text(cr, '{:.1f} g'.format(total), w - 12, 20, 10.5, DIM, True, 'right')
    cx, cy = w / 2.0, h / 2.0 + 8
    radius = min(w, h - 16) / 2.0 - 6
    if radius < 10:
        return
    sc = radius / 1.4
    rgb(cr, LV_GRID)
    cr.set_line_width(1)
    for r in (0.5, 1.0):
        cr.new_sub_path()
        cr.arc(cx, cy, r * sc, 0, 2 * math.pi)
        cr.stroke()
    cr.move_to(cx - radius, cy)
    cr.line_to(cx + radius, cy)
    cr.move_to(cx, cy - radius)
    cr.line_to(cx, cy + radius)
    cr.stroke()
    text(cr, '1g', cx + sc + 2, cy - 3, 10, FAINT)
    text(cr, 'BRK', cx - 10, cy + radius - 1, 10, FAINT)
    text(cr, 'ACC', cx - 10, cy - radius + 9, 10, FAINT)
    trail = [r for r in rows if r.get('alat') is not None and r.get('along') is not None][-30:]
    for i, r in enumerate(trail):
        rgb(cr, LV_SPEED, (i + 1) / len(trail) * 0.6)
        cr.rectangle(cx - r['alat'] * sc - 1.5, cy - r['along'] * sc - 1.5, 3, 3)
        cr.fill()
    if trail:
        rgb(cr, TEXT)
        cr.new_sub_path()
        cr.arc(cx - trail[-1]['alat'] * sc, cy - trail[-1]['along'] * sc, 5, 0, 2 * math.pi)
        cr.fill()


def live_map(cr, w, h, path, distance=None, length=None, ticks=(), label=''):
    """The stage on its own panel: the way driven from the samples' positions (x, z) with the car as a dot; without
    positions a straight bar by distance with the splits' ends (`ticks`, metres) marked. `length`: metres the bar
    spans."""
    _live_panel(cr, w, h)
    text(cr, 'STAGE', 12, 20, 10.5, DIM, True)
    if label:
        text(cr, label, w - 12, 20, 10.5, DIM, True, 'right')
    top, pad = 28.0, 10.0
    if path and len(path) >= 2:
        xs, zs = [p[0] for p in path], [p[1] for p in path]
        x0, x1, z0, z1 = min(xs), max(xs), min(zs), max(zs)
        aw, ah = w - 2 * pad, h - top - pad
        sc = min(aw / max(1.0, x1 - x0), ah / max(1.0, z1 - z0))
        ox, oy = pad + (aw - (x1 - x0) * sc) / 2.0, top + (ah - (z1 - z0) * sc) / 2.0
        step = max(1, len(path) // 400)
        pts = [(ox + (x - x0) * sc, oy + (z1 - z) * sc) for x, z in path[::step]]
        pts.append((ox + (path[-1][0] - x0) * sc, oy + (z1 - path[-1][1]) * sc))
        _live_path(cr, pts, LINE, 4)
        _live_path(cr, pts, DIM, 2)
        rgb(cr, TEXT)
        cr.new_sub_path()
        cr.arc(pts[-1][0], pts[-1][1], 5, 0, 2 * math.pi)
        cr.fill_preserve()
        rgb(cr, LV_BG)
        cr.set_line_width(2)
        cr.stroke()
        return
    y = top + (h - top - pad) / 2.0
    _live_path(cr, [(pad, y), (w - pad, y)], LINE, 4)
    if length and distance is not None:
        span = w - 2 * pad
        x = pad + max(0.0, min(1.0, distance / length)) * span
        _live_path(cr, [(pad, y), (x, y)], DIM, 4)
        for t in ticks:
            tx = pad + max(0.0, min(1.0, t / length)) * span
            _live_path(cr, [(tx, y - 6), (tx, y + 6)], FAINT, 1)
        rgb(cr, TEXT)
        cr.new_sub_path()
        cr.arc(x, y, 5, 0, 2 * math.pi)
        cr.fill_preserve()
        rgb(cr, LV_BG)
        cr.set_line_width(2)
        cr.stroke()
