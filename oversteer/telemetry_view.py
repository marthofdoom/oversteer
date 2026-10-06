"""What the Telemetry tab shows, as strings: built here from snapshots and
database rows so it can be tested without a display; gtk_ui only places
them (docs/telemetry-coaching.md, section 12). The tab's app-wide
settings are read from and written to config.ini here too."""

import time
from locale import gettext as _
from xml.sax.saxutils import escape

from .timefmt import clock

DISCIPLINES = {
    'rally-stage': _("Rally stage"), 'hillclimb': _("Hillclimb"), 'circuit': _("Circuit"),
    'rallycross': _("Rallycross"), 'drift': _("Drift"), 'free-roam': _("Free roam"),
    'time-attack': _("Time attack"),
}
SURFACES = {'tarmac': _("tarmac"), 'gravel': _("gravel"), 'snow': _("snow"), 'ice': _("ice"),
            'loose-low': _("snow or wet gravel")}
METHODS = {'h-pattern': _("H-pattern"), 'sequential': _("sequential"), 'paddles': _("paddles"),
           'auto': _("automatic"), 'mixed': _("mixed shifting")}
KINDS = {'top3': _("Top 3"), 'focus': _("Focus"), 'tip': '', 'praise': _("Better"), 'still': '', 'note': '', 'technique': _("Technique")}
CHANGES = {'first': _("first seen"), 'final-drive': _("final drive changed"), 'user': _("set by you")}


def surface_name(value):
    if value is None or value == 'unknown':
        return None
    if value.startswith('mixed:'):
        return _("mixed ({})").format(', '.join(SURFACES.get(s, s) for s in value[6:].split(',')))
    return SURFACES.get(value, value)


def _confidence(conf):
    return ' ({})'.format(conf) if conf else ''


def shift_summary(snapshot):
    """The line above the shift table: the limiter, where the best changes
    up come from (the game's engine data, or how much of the power curve
    is learnt and from what) and the surface they are for."""
    if snapshot is None:
        return _("Nothing learnt yet: drive with the rev lights or \"Learn from game telemetry\" on and Oversteer "
                 "learns each car's gearing and power.")
    limiter = snapshot['limiter']
    surface = surface_name(snapshot.get('surface'))
    if snapshot['power_source'] == 'game data':
        parts = [_("Limiter {} rpm").format(int(limiter) if limiter else '?'),
                 _("best changes up from the game's engine data")]
        parts.append(_("checked for grip on {}").format(surface) if surface else _("on any surface"))
        return '  ·  '.join(parts)
    source = _("engine power from the game") if snapshot['power_source'] == 'game' else \
        _("engine power estimated from acceleration")
    text = _("Limiter {} rpm  ·  {} rev bands of power known ({})  ·  learnt from your recent driving").format(
        int(limiter) if limiter else '?', snapshot['power_bands'], source)
    if snapshot.get('gearing_aside'):
        text += '  ·  ' + _("the game's data set aside: the gearing learnt is {:.0f} % off every gear set it gives "
                            "this car").format(snapshot['gearing_aside'] * 100)
    return text + ('  ·  ' + _("for {}").format(surface) if surface else '')


SHIFT_METHODS = (('h-pattern', _("H-pattern")), ('sequential', _("Sequential")), ('paddles', _("Paddles")))


def shift_table(snapshot):
    """(headers, rows of cells) for the shift table: per gear the change
    up it is about, the best rpm for it and the range that is known to
    (the bootstrap band: narrow once the power curve around it is well
    measured), its share of the limiter, where you change up, per way of
    changing once that way has been used, then the gearing and the
    samples behind it."""
    limiter = snapshot['limiter']
    methods = snapshot.get('methods') or {}
    used = [(m, name) for m, name in SHIFT_METHODS if any(m in v for v in methods.values())]
    surface = surface_name(snapshot.get('surface'))
    best_header = _("Best upshift ({})").format(surface) if surface else _("Best upshift")
    headers = ((_("Change"), best_header, _("Known to"), _("of limiter"), _("You change up"))
               + tuple(name for _m, name in used) + (_("rpm per km/h"), _("Samples")))
    rows = []
    for row in snapshot['gears']:
        band = ''
        if row['last']:
            change, best, share = _("{} (top)").format(row['gear']), _("top gear"), ''
        else:
            change = '{}→{}'.format(row['gear'], row['gear'] + 1)
            if row['best'] is None:
                best, share = _("learning…"), ''
            else:
                best = '{:.0f} rpm'.format(row['best'])
                shown = row['best']
                lit_low, lit_high = row.get('lights_low'), row.get('lights_high')
                if lit_low is not None and lit_high is not None:
                    # The band the coach judges the change up against: the game's lights, inside the limiter's margin
                    best = '{:.0f}–{:.0f} rpm'.format(lit_low, lit_high) if lit_high - lit_low >= 1.0 \
                        else '{:.0f} rpm'.format(lit_low)
                    best += ' ' + _("(lights)")
                    shown = lit_low
                    engine = row.get('engine_best')
                    if engine and limiter and engine < limiter - 1.0 and abs(engine - lit_low) >= 1.0:
                        best += '  ' + _("engine {:.0f}").format(engine)
                elif row.get('grip_limited'):
                    # Anywhere up to the engine's best gives the same drive
                    if (row.get('engine_best') or 0) > row['best']:
                        best = '{:.0f}–{:.0f} rpm'.format(row['best'], row['engine_best'])
                    best += ' ' + _("(grip)")
                elif row.get('source') == 'game':
                    best += ' ' + _("(game)")
                low, high = row.get('best_low'), row.get('best_high')
                if low is not None and high is not None and high - low >= 1.0:
                    band = '{:.0f}–{:.0f}'.format(low, high)
                share = '{:.0f} %'.format(shown / limiter * 100) if limiter else ''
        mine = '{:.0f} rpm ({})'.format(row['average_shift'], row['shifts']) if row['average_shift'] else '—'
        per_method = []
        for method, _name in used:
            average = methods.get(row['gear'], {}).get(method)
            per_method.append('{:.0f} rpm ({})'.format(*average) if average else '—')
        rows.append((change, best, band, share, mine) + tuple(per_method)
                    + ('{:.1f}'.format(row['ratio'] / 3.6) if row['ratio'] else '—', str(row['ratio_samples'])))
    return headers, rows


def context_line(session, runs, stage):
    """(line, evidence) about the most recent session: discipline and
    surface with their confidence and the stage's prior, the stage, the
    tune change and the shifter; the evidence sentences of its runs."""
    if session is None:
        return _("No session recorded yet."), []
    parts = []
    discipline = session.get('discipline')
    if discipline and discipline != 'unknown':
        parts.append(DISCIPLINES.get(discipline, discipline) + _confidence(session.get('discipline_conf')))
    else:
        parts.append(_("discipline unknown"))
    surface = surface_name(session.get('surface'))
    parts.append(surface + _confidence(session.get('surface_conf')) if surface else _("surface unknown"))
    prior = surface_name(stage.get('surface_prior')) if stage else None
    if prior and prior != surface:
        parts.append(_("usually {} here").format(prior))
    if session.get('wet') == 'wet':
        parts.append(_("wet"))
    where = (stage.get('name') if stage else None) or session.get('stage') or session.get('track')
    if where:
        parts.append(where)
    if session.get('shifter'):
        parts.append(METHODS.get(session['shifter'], session['shifter']))
    evidence = []
    for run in runs:
        lines = (run.get('discipline_evidence') or []) + (run.get('surface_evidence') or []) + \
            (run.get('wet_evidence') or [])
        if lines:
            head = _("Run {}").format(run['n'])
            if run.get('distance'):
                head += ', {:.1f} km'.format(run['distance'] / 1000.0)
            evidence.append(head + ': ' + ' '.join(lines))
    return '  ·  '.join(parts), evidence


def coaching_items(tips, advice=None):
    """The coach's tips as (badge, text, kind), the badge naming the kind
    ('' for a plain tip); `advice` (the live car's own lines) when the
    coach has nothing new to say, as before a run has ended."""
    items = []
    for tip in tips:
        kind = tip['kind'] if isinstance(tip, dict) else tip.kind
        text = tip['text'] if isinstance(tip, dict) else tip.text
        items.append((KINDS.get(kind, ''), text, kind))
    if advice and not any(kind != 'still' for _b, _t, kind in items):
        items = [('', text, 'tip') for text in advice] + items
    return items


def coaching_lines(tips, advice=None):
    """coaching_items() as (line, kind), the badge before the text."""
    return [('{}: {}'.format(badge, text) if badge else text, kind)
            for badge, text, kind in coaching_items(tips, advice)]


def signed(delta):
    """±s.s with a real minus sign, '–' for none; a difference that rounds to nothing is ±0.0, never +0.0 or -0.0."""
    if delta is None:
        return '–'
    r = round(delta, 1)
    return '{}{:.1f}'.format('+' if r > 0 else '\u2212' if r < 0 else '\u00b1', abs(r))


def splits_lines(found):
    """coach.splits() as (summary, rows): the summary line ('Afon Bidno · Best 3:17.7 · SoB 3:14.5 (−3.2)')
    and one (name, last, best, delta, tone) per split, tone 'gold' (the last run set the best), 'good'
    (level with or ahead of it), 'bad' or '' (not run). (None, []) without splits."""
    if not found:
        return None, []
    summary = '{}  ·  {} {}  ·  {} {}'.format(found['name'].split(' - ')[0], _("Best"), clock(found['best']),
                                              _("SoB"), clock(found['possible']))
    if found.get('gain') is not None:
        summary += ' (\u2212{:.1f})'.format(found['gain'])
    rows = []
    for r in found['splits']:
        tone = '' if r['last'] is None else 'gold' if r['gold'] else 'good' if r['delta'] <= 0.05 else 'bad'
        name = r['name'][4:] if r['name'].startswith('the ') else r['name']
        rows.append((name + (_(" (finish)") if r['finish'] else ''), clock(r['last']), clock(r['best']),
                     'gold' if tone == 'gold' else signed(r['delta']), tone))
    return summary, rows


def split_tone(last, delta, gold, cum=None, pb=None):
    """LiveSplit's colour rule for a split (LiveSplitStateHelper.GetSplitColor; the web page's splitTone):
    'gold' for a best split, else 'ahead' or 'behind' by the cumulative time against the PB (`cum`, the last run's
    elapsed time at the split's end less the PB's: ahead when it is not over zero) and 'gain' or 'lose' by what
    the split itself did against the PB's (`last` less `pb`): 'ahead-gain', 'ahead-lose', 'behind-gain',
    'behind-lose'; 'none' when not run. Without `cum` and `pb` (older data) `delta`, against the best, stands in
    for both."""
    if last is None or delta is None:
        return 'none'
    if gold:
        return 'gold'
    if cum is None or pb is None:
        cum = seg = delta
    else:
        seg = last - pb
    return ('ahead' if round(cum, 1) <= 0 else 'behind') + '-' + ('gain' if round(seg, 1) <= 0 else 'lose')


def _split_cells(r):
    """(Δ PB text, save text, tone) of a split of coach.splits(): the split against the PB's (a gold one shows
    how far it beat the previous best), what it could still save against the best."""
    tone = split_tone(r['last'], r['delta'], r['gold'], r.get('cum'), r.get('pb'))
    if r['last'] is None:
        return '–', '–', tone
    if r['gold']:
        text = '\u2605' + (' ' + signed(-r['margin']) if r.get('margin') else '')
    else:
        text = signed(r['last'] - r['pb'] if r.get('pb') is not None else r['delta'])
    save = r['delta'] if r['delta'] is not None and round(r['delta'], 1) > 0 else None
    return text, '–' if save is None else '{:.1f}'.format(save), tone


def potential_line(potential):
    """'Potential: you 3:03.1 · grip 2:51.4 · car 2:42.7' (oversteer/potential.py: the sum of best, the lap
    simulation at the driver's own grip, at the car's), the layers it has; None without a grip layer."""
    if not potential or potential.get('grip') is None:
        return None
    parts = [(_("you"), potential.get('user')), (_("grip"), potential['grip']), (_("car"), potential.get('car'))]
    return _("Potential") + ': ' + ' \u00b7 '.join('{} {}'.format(name, clock(t)) for name, t in parts if t is not None)


def splits_row(found):
    """coach.splits() for the splits row of the Coaching and Telemetry views, or None without splits: a dict
    with `stage` (short name), `name`, `pb`, `sob`, `gain`, `last`, `delta` (last less PB, None unless the last
    run finished), `new_pb` (the last run is the PB, and clean: the only time the row says PB), `finish_m` and
    `finish_confidence` (the times end at the stage's flying finish), `tones` (one per split, for the ribbon),
    `bounds` ((d0, d1) per split, for cells as long as the splits), `rows` ((name, last, Δ PB, best, tone, save)
    per split, the stage total and the sum of best at the end), `unit` ('sectors': the splits are the game's own sectors; 'sections': the corner
    sections), `sectors` ((name, last, delta, tone) per game sector beside corner-section splits, the last time marked
    with a leading '≈' where the position is estimated; [] where the sectors are the splits or there are none) and
    `estimated`, `potential` (the three layers, user / grip / car seconds) with `potential_line` (its words) and
    `avail` (the time available against the grip layer per row of `rows`, as text; '' where there is none)."""
    if not found:
        return None
    pb, last = found.get('best'), found.get('last')
    unit = found.get('unit') or 'sections'
    tones, rows, bounds = [], [], []
    for n, r in enumerate(found['splits'], 1):
        text, save, tone = _split_cells(r)
        tones.append(tone)
        bounds.append((r.get('d0'), r.get('d1')))
        name = r['name'][4:] if r['name'].startswith('the ') else r['name']
        rows.append((name if unit == 'sectors' else '{}. {}{}'.format(n, name, _(" (finish)") if r['finish'] else ''),
                     clock(r['last']), text,
                     clock(r['best']), tone, save))
    delta = None if last is None or pb is None else last - pb
    new_pb = bool(found.get('new_pb'))
    rows.append((_("Stage"), clock(last), '–' if delta is None else ('\u2605 ' if new_pb else '') + signed(delta), clock(pb),
                 'none' if delta is None else 'gold' if new_pb else 'ahead-gain' if round(delta, 1) <= 0 else 'behind-lose',
                 '–' if delta is None or round(delta, 1) <= 0 else '{:.1f}'.format(delta)))
    gain = found.get('gain')
    rows.append((_("Sum of best"), '', '' if gain is None else '\u2212{:.1f}'.format(gain), clock(found.get('possible')),
                 'gold', ''))
    pot = found.get('potential')
    avail = []
    for r in found['splits']:
        a = r.get('available')
        avail.append('\u2013' if a is None or round(a, 1) <= 0 else '{:.1f}'.format(a))
    grip = (pot or {}).get('grip')
    avail.append('\u2013' if last is None or grip is None or round(last - grip, 1) <= 0 else '{:.1f}'.format(last - grip))
    avail.append('')
    sectors, estimated = [], False
    for r in [] if unit == 'sectors' else found.get('sectors') or []:      # the sectors are the splits: not shown twice
        low = r.get('confidence') == 'low'
        estimated = estimated or low
        mark = '\u2248' if low else ''
        text, _save, tone = _split_cells(r)
        sectors.append((r['name'], '–' if r['last'] is None else mark + clock(r['last']),
                        text if r['last'] is not None else '–' if r['best'] is None
                        else _("best {}").format(mark + clock(r['best'])), tone))
    return {'stage': found['name'].split(' - ')[0], 'name': found['name'], 'pb': pb, 'sob': found.get('possible'),
            'gain': gain, 'last': last, 'delta': delta, 'new_pb': new_pb, 'finish_m': found.get('finish_m'),
            'finish_confidence': found.get('finish_confidence'), 'tones': tones, 'bounds': bounds, 'rows': rows,
            'unit': unit, 'sectors': sectors, 'estimated': estimated, 'runs': found.get('runs'), 'potential': pot,
            'potential_line': potential_line(pot), 'avail': avail}


def _splits(reader, profile, car_id):
    """coach.splits(), or None when it fails: the splits must never take the
    rest of the view (or the GTK refresh timer) down with them."""
    from . import coach
    try:
        return coach.splits(reader, profile, car_id)
    except Exception:
        import logging
        logging.exception("splits")
        return None


def tuning_lines(tune, notes):
    """The current tune (ratios, what changed, the measured extras or why
    they are missing) and the tuning notes."""
    if tune is None:
        return [_("No setup recorded yet: a tune is kept once a session with two or more gears learnt ends.")]
    ratios = ', '.join('{}: {:.1f}'.format(g, r / 3.6) for g, r in sorted(tune['ratios'].items()))
    change = tune.get('change') or ''
    if change.startswith('gears:'):
        change = _("gears {} changed").format(change[6:].replace(',', ', '))
    else:
        change = CHANGES.get(change, change)
    when = time.strftime('%x', time.localtime(tune['first_seen']))
    lines = [_("Gearing (rpm per km/h) {}  ·  {} {}").format(ratios, change, when)]
    extras = []
    for field, name in (('ride_height_f', _("ride height")), ('brake_bias', _("brake bias")),
                        ('tyre_radius', _("tyre radius"))):
        value = tune.get(field)
        if isinstance(value, (int, float)):
            unit = '{:.0f} %'.format(value * 100) if field == 'brake_bias' else '{:.3f} m'.format(value)
            extras.append('{} {}'.format(name, unit))
        elif value:
            extras.append('{}: {}'.format(name, _(value)))
    if extras:
        lines.append('  ·  '.join(extras))
    for note in notes:
        text = note['text'] if isinstance(note, dict) else note.text
        kind = note['kind'] if isinstance(note, dict) else note.kind
        lines.append((_("Setup") if kind == 'setup' else _("Driving")) + ': ' + text)
    return lines


def session_rows(history):
    """Per recent session (reader.history): (when, where, facts): the
    date, the stage with the discipline and surface, then the changes up
    with their error and the time on the limiter per km."""
    rows = []
    for h in history:
        when = time.strftime('%x %H:%M', time.localtime(h['started']))
        where = []
        if h.get('stage') or h.get('track'):
            where.append(h.get('stage') or h.get('track'))
        found = [DISCIPLINES.get(h.get('discipline'), '') if h.get('discipline') not in (None, 'unknown') else '',
                 surface_name(h.get('surface')) or '']
        if any(found):
            where.append(' '.join(x for x in found if x))
        facts = []
        if h.get('shifts'):
            error = h.get('error')
            facts.append(_("{} changes up, {:+.0f} rpm from the best").format(h['shifts'], error)
                         if error is not None else _("{} changes up").format(h['shifts']))
        km = (h.get('distance') or 0.0) / 1000.0
        if km >= 0.5:
            facts.append(_("{:.1f} km, {:.1f} s/km on the limiter").format(km, (h.get('limiter_time') or 0.0) / km))
        rows.append((when, '  ·  '.join(where), '  ·  '.join(facts)))
    return rows


def session_lines(history):
    """One line per recent session: session_rows() joined."""
    return ['  ·  '.join(part for part in row if part) for row in session_rows(history)]


def live_status(port, sample=None, learnt=None, elsewhere=None):
    """(state, markup) of the line about the telemetry arriving now:
    'off' without a listener (`port` None), 'waiting' while nothing
    arrives on it, 'live' with the car, gear, revs, speed, where the
    lights end when learnt, and the way through the stage. `elsewhere`:
    the port a game was heard sending to instead of ours."""
    if port is None:
        return 'off', escape(
            _("Not listening. Turn on the rev lights or \"Learn from game telemetry\" in Settings to read the "
              "game's telemetry and learn from it."))
    if sample is None and elsewhere:
        return 'waiting', escape(
            _("Telemetry is arriving on UDP {0}, but Oversteer listens on {1}: set the port to {0} under "
              "Settings and save the profile, or set the game to {1}.").format(elsewhere, port))
    if sample is None:
        return 'waiting', escape(_("Waiting for telemetry on UDP {}.").format(port))
    parts = ['<b>{}</b>'.format(escape(sample.car_name or sample.car or _("unknown car")))]
    if sample.gear is not None:
        parts.append(_("gear {}").format({-1: 'R', 0: 'N'}.get(sample.gear, sample.gear)))
    parts.append('{:.0f} rpm'.format(sample.rpm))
    if sample.speed is not None:
        parts.append('{:.0f} km/h'.format(sample.speed * 3.6))
    if learnt:
        parts.append(_("lights at the learnt {:.0f} rpm").format(learnt))
    distance = getattr(sample, 'lap_distance', None)
    if distance is None:
        distance = getattr(sample, 'distance', None)
    length = getattr(sample, 'stage_length', None)
    if distance is not None and length:
        parts.append(_("{:.1f} of {:.1f} km").format(max(0.0, distance) / 1000.0, length / 1000.0))
    return 'live', '  ·  '.join(parts)


def web_status(running, error, addresses, remote_seen, bind, port):
    """The lines under the web page switch: where to open it, or why it is
    not running, and what having it on means."""
    if error:
        return _("The web page could not start: {}").format(error)
    if not running:
        return _("Off. When on, any phone or computer on your network can open a read-only page with the "
                 "live gear, the shift tables, coaching and your history.")
    lines = [_("Open {}").format(_(" or ").join(addresses))]
    lines.append(_("Anyone on the same network can read your driving history, car and profile names, when you "
                   "drive and the live telemetry; nobody can change anything. It is plain HTTP: anything on the "
                   "way can read it too. On a public or shared network, use \"This computer only\" or leave it "
                   "off."))
    if bind != 'local' and not remote_seen:
        lines.append(_("Not opened from another device yet: if it does not load there, the firewall "
                       "(firewalld, ufw) may need TCP port {} opened.").format(port))
    return '\n'.join(lines)


def capture_status(on, listening, folder, summary, cap_gb, error=None, dropped=0):
    """The line under "Record raw telemetry": where the captures go and
    how much they take, or why nothing is being recorded. `summary` is
    telemetry_capture.capture_summary() of the folder."""
    count, size, labelled = summary
    if not count:
        kept = _("no captures yet")
    else:
        kept = (_("1 capture") if count == 1 else _("{} captures").format(count)) + \
            _(", {:.0f} MB of {} GB").format(size / 1e6, cap_gb)
    if labelled:
        kept += _(", {} labelled").format(labelled)
    if error:
        return _("Recording stopped: {}. Turn it off and on again to retry.").format(error)
    if not on:
        return _("Off. When on, everything the game sends is kept as it arrived (some 20–40 MB an hour), to "
                 "replay when Oversteer learns more or to attach to a bug report. Folder: {} ({}).").format(
            folder, kept)
    lines = [_("Recording to {} ({}). The oldest go first when the folder is full; the ones of a session you "
               "labelled are kept.").format(folder, kept)]
    if not listening:
        lines.append(_("Nothing is listening: turn on the rev lights or \"Learn from game telemetry\"."))
    if dropped:
        lines.append(_("{} packets were lost: the disk was too slow.").format(dropped))
    return '\n'.join(lines)


# The app-wide telemetry settings: (the controller's attribute, the
# config.ini key, its default). Off unless asked for.
PREFERENCES = (
    ('telemetry_learn', 'telemetry_learn', False),
    ('telemetry_web_on', 'telemetry_web', False),
    ('telemetry_web_port', 'telemetry_web_port', 5301),
    ('telemetry_web_bind', 'telemetry_web_bind', 'lan'),
    ('telemetry_capture_on', 'telemetry_capture', False),
    ('telemetry_capture_cap', 'telemetry_capture_cap', 1),       # GB
)
LIMITS = {'telemetry_web_port': (1024, 65535), 'telemetry_capture_cap': (1, 100)}
CHOICES = {'telemetry_web_bind': ('lan', 'local')}


def read_preferences(section):
    """{attribute: value} from config.ini's DEFAULT section (a mapping of
    strings): a missing or unreadable value keeps its default, a number
    out of range is brought into it."""
    values = {}
    for name, key, default in PREFERENCES:
        text = section.get(key)
        value = default
        if text is not None:
            if isinstance(default, bool):
                value = text == '1'
            elif isinstance(default, int):
                low, high = LIMITS[name]
                try:
                    value = max(low, min(high, int(text)))
                except ValueError:
                    pass
            elif text in CHOICES[name]:
                value = text
        values[name] = value
    return values


def write_preferences(values):
    """{config key: text} for config.ini from {attribute: value}."""
    out = {}
    for name, key, default in PREFERENCES:
        value = values[name]
        out[key] = ('1' if value else '0') if isinstance(default, bool) else str(value)
    return out


def gather(reader, profile, key, show_all=False):
    """Everything the tab shows about a car's history, from a reader: a
    history query, run on events (car change, session end, the tab
    shown), never on the 1 s timer. `tips` are the coach's Tip objects,
    for the caller to mark as seen once they are on screen."""
    from . import coach, tuning
    car = reader.car(profile, key) if key else None
    if car is None:
        return {'car_id': None, 'context': (_("No session recorded yet."), []), 'tips': [], 'tuning': [],
                'sessions': [], 'last_session': None, 'splits': None}
    sessions = reader.sessions(car['id'], 1)
    session = sessions[0] if sessions else None
    runs = reader.runs(session['id']) if session else []
    stage = reader.stage(session['stage'] or next((r['stage'] for r in runs if r['stage']), None) or '') \
        if session else None
    tips = coach.Coach(reader).tips(profile, car['id'], show_all=show_all)
    surface = session['surface'] if session else None
    notes = tuning.advice(reader, profile, car['id'], surface)
    return {'car_id': car['id'], 'context': context_line(session, runs, stage), 'tips': tips,
            'tuning': tuning_lines(tuning.tune_summary(reader, car['id'], car['game']), notes),
            'sessions': session_rows(reader.history(profile, car['key'], 10)),
            'last_session': session, 'splits': _splits(reader, profile, car['id'])}


# -- the live run (docs/telemetry-ui-design.md, "Phase 3 live API"): the rows of the run the tab has been shown,
# and the strings of the delta block, the ribbon and the finished card; the web page's liveIngest/liveShow rules --

GRAVITY = 9.80665
LIVE_KEEP = 30.0                     # s of rows kept for the strips
LIVE_BUS = ('t', 'distance', 'throttle', 'brake', 'clutch', 'handbrake', 'steer', 'a_long', 'a_lat', 'x', 'z')


def signed2(delta):
    """±s.ss with a real minus sign ('–' for none): the live delta's two decimals; a difference that rounds to
    nothing is ±0.00, never +0.00 or -0.00."""
    if delta is None:
        return '–'
    r = round(delta, 2)
    return '{}{:.2f}'.format('+' if r > 0 else '−' if r < 0 else '±', abs(r))


class LiveTrack:
    """What the tab has seen of the live run: the rows of the last 30 s (dicts: t, d, thr, brk, clu, hb, steer,
    along and alat in g, x, z), every position of the run, the tone of each split as it was completed (by the
    reference's grid index, or its sector's), and the last body. Rows are told apart by their clock, so a read that repeats rows
    (back on the tab) adds nothing, and rows missed while the tab was hidden leave a gap in the strips. The main
    thread only."""

    def __init__(self):
        self.since = 0
        self.n = None
        self.epoch = None                    # the Oversteer process the held rows came from
        self.rows = []
        self.path = []
        self.last_t = -1.0
        self.tones = {}
        self.body = None
        self.dismissed = None                # the run number whose finished card was put away
        self.unit = 'sections'               # what the splits row's cells are (live_unit): the tones are by their index

    def set_unit(self, unit):
        if unit != self.unit:
            self.unit, self.tones = unit, {}

    def clear(self):
        self.rows, self.path, self.last_t, self.tones = [], [], -1.0, {}

    def resync(self):
        """Read the whole buffer next time (back on the tab); what is already held is not added twice."""
        self.since = 0

    def ingest(self, body):
        epoch = body.get('epoch')
        if body.get('reset') or (epoch and self.epoch and epoch != self.epoch) or (body['run']['n'] if body.get('run') else None) != self.n:
            self.clear()
        self.epoch = epoch
        self.n = body['run']['n'] if body.get('run') else None
        self.since = body['seq']
        self.body = body
        index = {c: i for i, c in enumerate(body['channels'])}
        pick = {name: index.get(name) for name in LIVE_BUS}
        for r in body['samples']:
            def v(name):
                i = pick[name]
                return None if i is None else r[i]
            t = v('t')
            if t is None or t <= self.last_t:
                continue
            self.last_t = t
            along, alat = v('a_long'), v('a_lat')
            row = {'t': t, 'd': v('distance'), 'thr': v('throttle'), 'brk': v('brake'), 'clu': v('clutch'),
                   'hb': v('handbrake'), 'steer': v('steer'),
                   'along': None if along is None else along / GRAVITY, 'alat': None if alat is None else alat / GRAVITY,
                   'x': v('x'), 'z': v('z')}
            self.rows.append(row)
            if row['x'] is not None and row['z'] is not None:
                self.path.append((row['x'], row['z']))
        keep = 0
        while keep < len(self.rows) and self.rows[keep]['t'] < self.last_t - LIVE_KEEP:
            keep += 1
        del self.rows[:keep]
        if len(self.path) > 30000:
            del self.path[:5000]
        # A split's tone as it is completed: ahead or behind the PB by the clock at its end, gaining or losing by the
        # split's own time (LiveSplit's rule; gold needs the best splits, which the live run does not have)
        body = live_unit(body, self.unit)
        split = body.get('split')
        if body.get('ref') and split and split.get('prev') and body.get('delta') is not None \
                and split['prev']['index'] not in self.tones:
            cum = body['delta'] - (split['delta'] if split.get('delta') is not None else 0.0)
            seg = split['prev']['delta']
            self.tones[split['prev']['index']] = ('ahead' if round(cum, 1) <= 0 else 'behind') + '-' + \
                ('gain' if round(seg, 1) <= 0 else 'lose')


def live_unit(body, unit):
    """`body` (live_buffer's read) with the splits the splits row has: where `unit` is 'sectors' the live run's `sector`
    view is its `split` and the reference's sectors its `splits`, else `body` itself."""
    ref = body.get('ref')
    if unit != 'sectors' or not ref or not ref.get('sectors'):
        return body
    return dict(body, split=body.get('sector'), ref=dict(ref, splits=ref['sectors']))


def live_phase(body, track):
    """'idle', 'live', 'stale', 'finished' (shown as the finished card) or 'put-away' (finished, the card put away)."""
    state = body['state']
    if state == 'finished':
        return 'put-away' if track.dismissed == body['run']['n'] else 'finished'
    return state


def live_cursor(body, bounds):
    """The index of the splits-row cell the car is in, by distance (the row's `bounds`, not list position: the
    grid has a launch section the row leaves out), or None between cells or past the finish."""
    d = body.get('distance')
    if d is None or body['state'] == 'finished':
        return None
    found = None
    for j, b in enumerate(bounds or ()):
        if b and None not in b and b[0] <= d < b[1]:
            found = j
    return found


SPLIT_MATCH = 15.0               # m: a row's split is the reference's whose start is this close (the sectors' lines
                                 # are placed from where the reference began, a few metres from the row's 0)


def live_ribbon(body, track, bounds):
    """(tones per cell, current cell) for the splits ribbon while a run is on, or None: completed splits in the tone
    they were completed in, the rest unlit."""
    ref = body.get('ref')
    if not bounds or body['state'] == 'idle':
        return None
    tones = []
    for b in bounds:
        g = -1
        if ref and b and b[0] is not None:
            near = min(range(len(ref['splits'])), key=lambda i: abs(ref['splits'][i]['d0'] - b[0]), default=None)
            if near is not None and abs(ref['splits'][near]['d0'] - b[0]) < SPLIT_MATCH:     # S1 starts a couple of m on
                g = near
        tones.append(track.tones.get(g, 'none'))
    return tones, live_cursor(body, bounds)


def ref_name(ref):
    """What the live reference is called: 'PB' only when it is the stage's PB, else 'Run n'."""
    if ref.get('pb') is False:
        return _("Run {}").format(ref['n']) if ref.get('n') is not None else _("best run")
    return _("PB")


def live_delta_view(body, bounds=None, unit='sections'):
    """The dict telemetry_plot.live_delta draws, or None while there is nothing to say: the delta only with a
    reference that is ready and a delta; 'no PB yet' as a note; 'vs PB · split n ±x.xx · finish ≈' ('S2 ±0.42' where the splits are `unit` 'sectors')."""
    if body['state'] not in ('live', 'stale'):
        return None
    if body.get('ref_status') == 'none':
        return {'note': _("No PB yet on this stage: finish a clean run"), 'dim': True}
    ref = body.get('ref')
    if body.get('ref_status') != 'ready' or body.get('delta') is None or not ref:
        return None
    split = body.get('split')
    cur = live_cursor(body, bounds) if bounds else None
    label = '–'
    if split and split.get('delta') is not None:
        if bounds:
            label = '{}{} {}'.format('S' if unit == 'sectors' else '', cur + 1, signed2(split['delta'])) if cur is not None else '–'
        else:
            label = signed2(split['delta'])
    return {'text': signed2(body['delta']), 'delta': body['delta'], 'pb': clock(ref['time']), 'ref_name': ref_name(ref), 'split': label,
            'finish': clock(body['predicted']), 'dim': body['state'] == 'stale'}


def live_done_view(body, found=None, calls=None):
    """The finished card: `time`, `delta` (text, or '' without a PB), `delta_value`, `stage`, and `line` ('PB 3:06.0
    · SoB 3:04.0 · 3 gold splits · 3 calls in the debrief', from the splits row `found` and the debrief's count)."""
    final = body.get('final') or {}
    ref = body.get('ref')
    parts = [ref_name(ref) + ' ' + clock(ref['time'])] if ref else [_("No PB to compare with yet")]
    if found:
        if found.get('sob') is not None:
            parts.append(_("SoB") + ' ' + clock(found['sob']))
        golds = sum(1 for t in found.get('tones') or () if t == 'gold')
        if golds:
            parts.append((_("{} gold split") if golds == 1 else _("{} gold splits")).format(golds))
    if calls:
        parts.append(_("{} in the debrief").format(calls))
    delta = final.get('delta')
    return {'time': clock(final.get('time')), 'delta': '' if delta is None else signed2(delta), 'delta_value': delta,
            'stage': (body.get('stage') or {}).get('name') or '', 'line': ' · '.join(parts)}
