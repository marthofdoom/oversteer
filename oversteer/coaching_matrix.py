"""The coaching derivations (data/telemetry/coaching/derivations.json): how
every coaching aspect Oversteer needs is derived from the telemetry, and
what each game gives towards it, with Assetto Corsa Rally as the
reference game (docs/coaching-derivations.md).

Three layers:

- channels: every canonical quantity, in Sample's units and conventions
  (oversteer/telemetry_formats.py), plus derived basics (slip angle,
  curvature, longitudinal g...) and the rig inputs Oversteer reads from
  the wheel itself. A channel may list `derive_from`: alternatives, each
  a list of channels and `static:<name>` game data it is computed from.
- games: per game, each channel's status ('confirmed' on a real capture,
  'decoded' in code from published docs, 'derivable', 'absent') and the
  static data the game's files give ('shipped', 'in_game_files',
  'planned', 'absent'). A channel a game does not list is resolved from
  its `derive_from`, else absent. The rig channels hold for every game.
- aspects: every coaching aspect, its derivations in order of preference
  (each with the channels, static data and other aspects it requires,
  and optional ones; `sent_only` when the channels must come from the
  game itself), gates, outputs, validation and implementation.

Read-only data: nothing here changes what the coach does; it answers what
can be coached for a game and why not.
"""

import json
import logging
import os
import sys

SCHEMA_VERSION = 1
AVAILABLE = ('confirmed', 'decoded', 'derivable')
STATIC_AVAILABLE = ('shipped',)

DATADIR = None                   # the installed data folder (share/oversteer); stage_tables.DATADIR also serves

_data = None


def find_path(datadir=None):
    """The derivations file: installed with the telemetry data, or in the
    source tree; None when not found."""
    here = os.path.dirname(os.path.abspath(__file__))
    bases = [datadir, DATADIR]
    try:
        from . import stage_tables              # the application sets its DATADIR at start
        bases.append(stage_tables.DATADIR)
    except ImportError:                         # pragma: no cover
        pass
    candidates = [os.path.join(b, 'telemetry', 'coaching', 'derivations.json') for b in bases if b]
    candidates.append(os.path.join(here, '..', 'data', 'telemetry', 'coaching', 'derivations.json'))
    candidates.append(os.path.join(sys.prefix, 'share', 'oversteer', 'telemetry', 'coaching', 'derivations.json'))
    for path in candidates:
        if os.path.isfile(path):
            return os.path.normpath(path)
    return None


def load(path=None, reload=False):
    """The whole database as a dict, loaded once (an empty one, logged,
    when the file is missing, unreadable or of another schema version).
    A `path` given reads that file and leaves the loaded one alone."""
    global _data
    if _data is not None and path is None and not reload:
        return _data
    given = path is not None
    path = path or find_path()
    data = None
    if path:
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            if data.get('schema_version') != SCHEMA_VERSION:
                logging.warning("coaching matrix: %s is schema %s, not %s", path, data.get('schema_version'),
                                SCHEMA_VERSION)
                data = None
        except (OSError, ValueError, AttributeError) as e:
            logging.warning("coaching matrix: %s not loaded: %s", path, e)
            data = None
    if data is None:
        data = {'schema_version': SCHEMA_VERSION, 'channels': {}, 'rig': {'channels': {}}, 'games': {},
                'aspects': []}
    if not given:
        _data = data
    return data


def channels():
    """{channel id: entry}."""
    return load()['channels']


def games():
    """The game ids, reference game first."""
    data = load()
    ids = list(data['games'])
    ref = data.get('reference_game')
    return sorted(ids, key=lambda g: (g != ref, ids.index(g)))


def aspects():
    """Every aspect entry, in the file's order."""
    return list(load()['aspects'])


def aspect(aspect_id):
    """One aspect entry, or None."""
    return next((a for a in load()['aspects'] if a['id'] == aspect_id), None)


def static_data(game):
    """{name: entry} of the game's static data, {} for an unknown game."""
    entry = load()['games'].get(game)
    return dict(entry.get('static') or {}) if entry else {}


def _static_ok(game, name):
    return (static_data(game).get(name) or {}).get('status') in STATIC_AVAILABLE


def _token_ok(game, token, resolved, stack):
    if token.startswith('static:'):
        return _static_ok(game, token[len('static:'):])
    return _channel(game, token, resolved, stack)['status'] in AVAILABLE


def _channel(game, name, resolved, stack):
    """The status entry of one channel for one known game, memoised in
    `resolved`; `stack` stops derivations going round in circles."""
    if name in resolved:
        return resolved[name]
    outer = stack
    data = load()
    listed = (data['games'][game].get('channels') or {}).get(name)
    rig = (data.get('rig') or {}).get('channels', {}).get(name)
    if listed is not None and listed.get('status') in ('confirmed', 'decoded'):
        found = dict(listed)
    elif rig is not None:
        found = dict(rig, source='rig')
    else:
        found = None
        stack = stack | {name}
        alternatives = []
        if listed is not None and listed.get('status') == 'derivable' and listed.get('via') is not None:
            alternatives.append(listed['via'])
        alternatives += (data['channels'].get(name) or {}).get('derive_from') or []
        if listed is None or listed.get('status') != 'absent':
            for via in alternatives:
                if any(t in stack for t in via if not t.startswith('static:')):
                    continue
                if all(_token_ok(game, t, resolved, stack) for t in via):
                    found = dict(listed or {}, status='derivable', via=list(via))
                    break
        if found is None:
            found = dict(listed or {}, status='absent')
            if listed is not None and listed.get('status') == 'derivable':
                found['note'] = (found.get('note', '') + ' (its inputs are not available)').strip()
    if not outer:
        resolved[name] = found               # a result found under a cycle break may be pessimistic: not kept
    return found


def game_channels(game):
    """{channel: {'status', and 'via', 'evidence', 'note', 'source' where
    known}} for every catalogued channel; {} for an unknown game."""
    data = load()
    if game not in data['games']:
        return {}
    resolved = {}
    return {name: _channel(game, name, resolved, frozenset()) for name in data['channels']}


def _missing(game, need, table, done, sent_only=False):
    """What of `need` ({'channels', 'static', 'aspects'}) the game lacks,
    as sentences. `sent_only`: channels must come from the game (confirmed
    or decoded), not be derived."""
    out = []
    ok = ('confirmed', 'decoded') if sent_only else AVAILABLE
    for name in need.get('channels') or []:
        if table.get(name, {}).get('status') not in ok:
            note = table.get(name, {}).get('note')
            out.append('channel {}{}'.format(name, ' ({})'.format(note) if note else ''))
    for name in need.get('static') or []:
        if not _static_ok(game, name):
            status = (static_data(game).get(name) or {}).get('status', 'absent')
            out.append('static {} ({})'.format(name, status))
    for name in need.get('aspects') or []:
        if _usable(game, name, table, done) is None:
            out.append('aspect {}'.format(name))
    return out


def _usable(game, aspect_id, table, done):
    """(derivation, missing optional) of the first derivation the game
    can do, or None. `done` memoises and breaks cycles."""
    if aspect_id in done:
        return done[aspect_id]
    done[aspect_id] = None                       # in progress: a cycle reads as unavailable
    entry = aspect(aspect_id)
    if entry is None:
        return None
    for derivation in entry['derivations']:
        if not _missing(game, derivation.get('requires') or {}, table, done, derivation.get('sent_only')):
            optional = derivation.get('optional') or {}
            missing = [c for c in optional.get('channels') or [] if table.get(c, {}).get('status') not in AVAILABLE]
            missing += ['static:' + s for s in optional.get('static') or [] if not _static_ok(game, s)]
            done[aspect_id] = (derivation, missing)
            break
    return done[aspect_id]


def available_aspects(game):
    """[(aspect, derivation used, missing optional channels)] for every
    aspect the game can give by at least one derivation (implemented or
    planned: the derivation's 'status' says which); [] for an unknown
    game."""
    table = game_channels(game)
    if not table:
        return []
    done = {}
    out = []
    for entry in load()['aspects']:
        found = _usable(game, entry['id'], table, done)
        if found is not None:
            out.append((entry, found[0], found[1]))
    return out


def why_unavailable(game, aspect_id):
    """Why the game cannot give the aspect: [] when it can, else one line
    per derivation naming what it lacks; None for an unknown game or
    aspect."""
    table = game_channels(game)
    entry = aspect(aspect_id)
    if not table or entry is None:
        return None
    done = {}
    if _usable(game, aspect_id, table, done) is not None:
        return []
    return ['{}: needs {}'.format(d['id'], ', '.join(_missing(game, d.get('requires') or {}, table, done,
                                                            d.get('sent_only'))))
            for d in entry['derivations']]


def metric_aspects():
    """{metric name the coach writes: aspect id}."""
    out = {}
    for entry in load()['aspects']:
        for name in (entry.get('implementation') or {}).get('metrics') or []:
            out[name] = entry['id']
    return out


# -- the readable form (docs/coaching-derivations.md, between its markers) --

MARK_START = '<!-- generated by oversteer/coaching_matrix.py: do not edit by hand -->'
MARK_END = '<!-- end of generated -->'
SYMBOL = {'confirmed': 'C', 'decoded': 'D', 'derivable': 'd', 'absent': '-'}


def _cell(text):
    return str(text).replace('|', '\\|').replace('\n', ' ')


def markdown():
    """The generated part of docs/coaching-derivations.md."""
    data = load()
    ids = games()
    lines = [MARK_START, '', '### Channels', '',
             '| Channel | Kind | Unit | Convention | Rate | Derived from |', '|---|---|---|---|---|---|']
    for name, c in data['channels'].items():
        derive = ' or '.join('(' + ', '.join(v) + ')' if v else '(always)' for v in c.get('derive_from') or [])
        lines.append('| `{}` | {} | {} | {} | {} | {} |'.format(
            name, c['kind'], _cell(c['unit']), _cell(c.get('convention') or c.get('values') or ''), c['rate'],
            _cell(derive)))
    lines += ['', '### Games', '', "A 'packet' rate is the game's own packet rate; the drive log keeps a 10 Hz "
              'trace of it and the corner and coaching figures come from that.', '',
              '| Game | Name | Format | Packets/s |', '|---|---|---|---|']
    for g in ids:
        entry = data['games'][g]
        lines.append('| {} | {} | {} | {} |'.format(g, _cell(entry['name']), _cell(entry['format']),
                                                   entry.get('rate_hz') or 'set by the user'))
    lines += ['', '### Games: channel coverage', '',
              'C confirmed on a capture, D decoded (unverified), d derivable, - absent.', '',
              '| Channel | ' + ' | '.join(ids) + ' |', '|---|' + '---|' * len(ids)]
    tables = {g: game_channels(g) for g in ids}
    for name in data['channels']:
        lines.append('| `{}` | '.format(name) + ' | '.join(SYMBOL[tables[g][name]['status']] for g in ids) + ' |')
    lines += ['', '### Games: static data', '', '| Game | Data | Status | Provides | Note |', '|---|---|---|---|---|']
    for g in ids:
        for name, s in static_data(g).items():
            lines.append('| {} | {} | {} | {} | {} |'.format(
                g, name, s['status'] + (' ({})'.format(s['clickup']) if s.get('clickup') else ''),
                _cell(', '.join(s.get('provides') or [])), _cell(s.get('note') or s.get('path') or '')))
    lines += ['', '### Aspects', '',
              '| Aspect | Outputs | Derivations (preferred first) | Gates | Validation | Implementation |',
              '|---|---|---|---|---|---|']
    for a in data['aspects']:
        derivations = '; '.join('{} [{}]: {}'.format(d['id'], d['status'], ', '.join(
            (d['requires'].get('channels') or []) + ['static:' + s for s in d['requires'].get('static') or []]
            + ['aspect:' + s for s in d['requires'].get('aspects') or []]) or '-') for d in a['derivations'])
        gates = ', '.join('{}: {}'.format(k, v) for k, v in a['gates'].items() if v != 'any') or '-'
        im = a['implementation']
        where = ', '.join(im.get('metrics') or []) or im.get('module') or ''
        lines.append('| `{}` {} | {} | {} | {} | {} | {}{}{} |'.format(
            a['id'], _cell(a['name']), _cell(', '.join('{} ({})'.format(o['name'], o['unit']) for o in a['outputs'])),
            _cell(derivations), _cell(gates), a['validation']['status'], im['status'],
            ': ' + _cell(where) if where else '', ' ({})'.format(im['clickup']) if im.get('clickup') else ''))
    ref = data.get('reference_game')
    if ref in data['games']:
        usable = {a['id']: (d, missing) for a, d, missing in available_aspects(ref)}
        lines += ['', '### {} coverage'.format(data['games'][ref]['name']), '',
                  '| Aspect | Coverage | Derivation | Missing optional | Note |', '|---|---|---|---|---|']
        for a in data['aspects']:
            d, missing = usable.get(a['id'], (None, []))
            why = '' if d else '; '.join(why_unavailable(ref, a['id']) or [])
            lines.append('| `{}` | {} | {} | {} | {} |'.format(
                a['id'], a['acr']['coverage'], '{} [{}]'.format(d['id'], d['status']) if d else '-',
                _cell(', '.join(missing)), _cell(a['acr'].get('note') or why)))
        lines += ['', '### {} gaps'.format(data['games'][ref]['name']), '']
        lines += ['- ' + gap for gap in data['games'][ref].get('gaps') or []]
    lines += ['', MARK_END]
    return '\n'.join(lines) + '\n'


def main(argv=None):
    """`python3 -m oversteer.coaching_matrix [--doc PATH]`: print the
    generated tables, or rewrite them between the markers of PATH."""
    argv = sys.argv[1:] if argv is None else argv
    text = markdown()
    if argv[:1] == ['--doc'] and len(argv) == 2:
        with open(argv[1], encoding='utf-8') as f:
            doc = f.read()
        head, rest = doc.split(MARK_START, 1)
        tail = rest.split(MARK_END, 1)[1]
        with open(argv[1], 'w', encoding='utf-8') as f:
            f.write(head + text.rstrip('\n') + tail)
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
