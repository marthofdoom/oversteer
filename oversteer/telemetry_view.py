"""What the Telemetry tab shows, as strings: built here from snapshots and
database rows so it can be tested without a display; gtk_ui only places
them (docs/telemetry-coaching.md, section 12). The tab's app-wide
settings are read from and written to config.ini here too."""

import time
from locale import gettext as _
from xml.sax.saxutils import escape

DISCIPLINES = {
    'rally-stage': _("Rally stage"), 'hillclimb': _("Hillclimb"), 'circuit': _("Circuit"),
    'rallycross': _("Rallycross"), 'drift': _("Drift"), 'free-roam': _("Free roam"),
    'time-attack': _("Time attack"),
}
SURFACES = {'tarmac': _("tarmac"), 'gravel': _("gravel"), 'snow': _("snow"), 'ice': _("ice"),
            'loose-low': _("snow or wet gravel")}
METHODS = {'h-pattern': _("H-pattern"), 'sequential': _("sequential"), 'paddles': _("paddles"),
           'auto': _("automatic"), 'mixed': _("mixed shifting")}
KINDS = {'focus': _("Focus"), 'tip': '', 'praise': _("Better"), 'still': '', 'note': ''}
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
    """The line above the shift table: the limiter, how much of the
    power curve is known and where it comes from."""
    if snapshot is None:
        return _("Nothing learnt yet: drive with the rev lights or \"Learn from game telemetry\" on and Oversteer "
                 "learns each car's gearing and power.")
    limiter = snapshot['limiter']
    source = _("engine power from the game") if snapshot['power_source'] == 'game' else \
        _("engine power estimated from acceleration")
    return _("Limiter {} rpm  ·  {} rev bands of power known ({})  ·  learnt from your recent driving").format(
        int(limiter) if limiter else '?', snapshot['power_bands'], source)


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
    headers = ((_("Change"), _("Best upshift"), _("Known to"), _("of limiter"), _("You change up"))
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
                low, high = row.get('best_low'), row.get('best_high')
                if low is not None and high is not None and high - low >= 1.0:
                    band = '{:.0f}–{:.0f}'.format(low, high)
                share = '{:.0f} %'.format(row['best'] / limiter * 100) if limiter else ''
        mine = '{:.0f} rpm ({})'.format(row['average_shift'], row['shifts']) if row['average_shift'] else '—'
        per_method = []
        for method, _name in used:
            average = methods.get(row['gear'], {}).get(method)
            per_method.append('{:.0f} rpm ({})'.format(*average) if average else '—')
        rows.append((change, best, band, share, mine) + tuple(per_method)
                    + ('{:.1f}'.format(row['ratio'] / 3.6), str(row['ratio_samples'])))
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
                'sessions': [], 'last_session': None}
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
            'last_session': session}
