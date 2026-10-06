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


def ribbon(cr, w, h, tones, sector_tones=(), bounds=None, gap=1.5, sector_w=22.0):
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
        for tone, length in zip(cells, lengths):
            each = usable * length / total
            rounded(cr, x, 0, each, h, 1.5)
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
