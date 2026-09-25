"""The shipped stage tables (data/telemetry/stages/<game>.json): the stages
a rally game ships, with their location, length and surface, so a stage is
known from the first run without the driver labelling anything
(docs/telemetry-coaching.md, sections 5.4 and 8.6).

A table file is {"game": ..., "stages": [entry, ...]} plus notes, sources
and, where the data came from someone else's table, its licence. An entry
has `location`, `stage`, `length_m`, `surface` ('gravel', 'tarmac',
'snow', 'ice' or 'mixed'), `surface_parts` (for a mixed one: {surface:
share}, or a list, larger part first, where the shares are not known),
`reverse_of`, `shakedown`, `sources` and `confidence`; DiRT's also
`start_z` (its stages are told apart by the length the game sends and
where they start), Assetto Corsa Rally's `track` and `config` where known.
WRC Generations' come from the game's files (scripts/stage-tables.py):
`code` and `level` (its route), `length_m` (the float it sends),
`alt_codes` (other layouts of the stage, [{code, length_m}]),
`menu_length_m`, `elevation_min_m`, `elevation_max_m`, `climb_m`,
`descent_m`, `kind`, `rally`, `surface_source` and `name_source`.

Each entry gets a stage key: DiRT's is the key a measured run would get
(`dirt:<length>:<start z>`), so a run matches it by tolerance; the others
are `<game>:<location>:<stage>` in lower-case words.
"""

import json
import logging
import os
import re
import sys
import unicodedata

GAMES = ('dirt', 'wrcg', 'acr')
SINGLE = ('tarmac', 'gravel', 'snow', 'ice')
MIXED_SHARE = 0.25               # a second surface below this share leaves the stage one surface (section 8.6)
TRACK_CHARS = 31                 # the bridge sends the shared memory's track name in 32 bytes, NUL included

DATADIR = None                   # the installed data folder (share/oversteer), set by the application

_tables = None


def find_dir(datadir=None):
    """The folder of the tables: installed with the telemetry data, or in
    the source tree."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    for base in (datadir, DATADIR):
        if base:
            candidates.append(os.path.join(base, 'telemetry', 'stages'))
    candidates.append(os.path.join(here, '..', 'data', 'telemetry', 'stages'))
    candidates.append(os.path.join(sys.prefix, 'share', 'oversteer', 'telemetry', 'stages'))
    for path in candidates:
        if os.path.isdir(path):
            return os.path.normpath(path)
    return None


def _slug(text):
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


def bridge_track(name):
    """A shared-memory track name as the bridge sends it: printable ASCII,
    anything else '_', cut to what fits."""
    return ''.join(c if 0x20 <= ord(c) < 0x7f else '_' for c in name)[:TRACK_CHARS].strip()


def stage_key(game, entry):
    if game == 'dirt' and entry.get('length_m') and entry.get('start_z') is not None:
        return 'dirt:{:.0f}:{:.0f}'.format(round(entry['length_m']), round(entry['start_z'], -1))
    return '{}:{}:{}'.format(game, _slug(entry['location']), _slug(entry['stage']))


def surface_of(entry):
    """(surface, prior) of an entry: a single surface is the game's word,
    and so is a mixed one whose other parts are each under 25 % (a gravel
    stage with 2 % tarmac is gravel); otherwise only a prior,
    'mixed:<a>,<b>' with the larger part first."""
    surface = entry.get('surface')
    if surface in SINGLE:
        return surface, None
    parts = entry.get('surface_parts') or {}
    if isinstance(parts, dict):
        shares = sorted(((p, s) for s, p in parts.items() if s in SINGLE), reverse=True)
        if shares and all(p < MIXED_SHARE for p, _ in shares[1:]):
            return shares[0][1], None
        parts = [s for _, s in shares]
    ordered = [s for s in parts if s in SINGLE]
    if len(ordered) >= 2:
        return None, 'mixed:' + ','.join(ordered)
    if len(ordered) == 1:
        return ordered[0], None
    return None, None


def load(directory=None):
    """{game: {key: entry}} from every table in `directory` (found when
    None). A table that does not parse is logged and skipped."""
    directory = directory or find_dir()
    tables = {}
    if not directory:
        return tables
    for name in sorted(os.listdir(directory)):
        if not name.endswith('.json'):
            continue
        try:
            with open(os.path.join(directory, name), encoding='utf-8') as f:
                table = json.load(f)
            game = table['game']
            entries = tables.setdefault(game, {})
            for entry in table['stages']:
                entry = dict(entry)
                entry['key'] = key = stage_key(game, entry)
                if key in entries:
                    logging.warning("stage tables: %s: %s twice, the first kept", name, key)
                    continue
                entries[key] = entry
        except (OSError, ValueError, KeyError, TypeError) as e:
            logging.warning("stage tables: %s not loaded: %s", name, e)
    return tables


def tables():
    """The shipped tables, loaded once."""
    global _tables
    if _tables is None:
        _tables = load()
    return _tables


def set_tables(value):
    """Replace the loaded tables (tests; None loads them again)."""
    global _tables
    _tables = value


def entry(key):
    """The shipped entry of a stage key, or None."""
    if not key:
        return None
    return tables().get(key.split(':', 1)[0], {}).get(key)


def candidates_surface(candidates):
    """The one surface a set of entries shares, or None."""
    found = {surface_of(e)[0] for e in candidates}
    return found.pop() if len(found) == 1 and None not in found else None


def location_surface(game, location):
    """(surface, prior) of a location from its game's table: the one
    surface all its stages share, else a prior naming them."""
    if not location:
        return None, None
    found = []
    for e in tables().get(game, {}).values():
        if e.get('location') != location:
            continue
        surface, prior = surface_of(e)
        for s in [surface] if surface else (prior[len('mixed:'):].split(',') if prior else []):
            if s not in found:
                found.append(s)
    if len(found) == 1:
        return found[0], None
    if len(found) > 1:
        return None, 'mixed:' + ','.join(found)
    return None, None
