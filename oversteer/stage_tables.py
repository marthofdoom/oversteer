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
where they start), Assetto Corsa Rally's `track` and `config` where known
and, from the game, `elevation_start_m`, `sectors_km` and
`pacenote_first_m`/`pacenote_last_m` (along the road spline; the last note is the
stop control), `finish_m` (the flying finish, from marth's runs where several agree:
scripts/acr-finish.py, with `finish_runs`, `finish_spread_m`, `finish_confidence`,
`finish_source`), `start_m` (the start line along the spline, where measured) and `discipline`
where it is not a rally stage ('circuit' for Livigno).
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


# The finish lines learnt from the game's own clock (Store.learn_finishes): {stage key: {'finish_m',
# 'finish_runs', 'finish_spread_m'}}, set by the store that has them. A learnt line beats the shipped one, which
# beats the last pace note (the stop control): entry() and acr_stage() hand out the entry with it applied.
_learnt = {}


def set_learnt(found):
    """Replace the learnt finish lines (the store's; {} forgets them)."""
    global _learnt
    _learnt = dict(found or {})


def _learnt_entry(key, found):
    """`found` (a shipped entry) with the stage's learnt finish line applied, or as it is without one."""
    learnt = _learnt.get(key)
    if not learnt or found is None:
        return found
    merged = dict(found)
    runs = learnt.get('finish_runs') or 1
    merged.update(finish_m=learnt['finish_m'], finish_runs=runs, finish_spread_m=learnt.get('finish_spread_m'),
                  finish_confidence='high' if runs >= 2 else 'medium',
                  finish_source="the game's own stage clock stopping, over {} of marth's runs".format(runs))
    return merged


def set_tables(value):
    """Replace the loaded tables (tests; None loads them again)."""
    global _tables
    _tables = value


# Assetto Corsa Rally: where along the road spline a run starts, against
# the stage's first pace note (marth's captures: 25-50 m before it)
START_BEFORE_NOTE = (-30.0, 150.0)


def acr_stage(track, start=None, length=None):
    """The Assetto Corsa Rally entry for a track name as the bridge sends
    it, or None. Two stages of one name (the bridge cuts names and replaces
    accents) are told apart by where the run started: just before a
    stage's first pace note. Without that, by the spline length the game
    sent, against each one's published length and last pace note."""
    if not track:
        return None
    found = [_learnt_entry(k, e) for k, e in tables().get('acr', {}).items()
             if e.get('track') and bridge_track(e['track']) == track]
    if len(found) > 1 and start is not None:
        low, high = START_BEFORE_NOTE
        near = [e for e in found if e.get('pacenote_first_m') is not None
                and low <= e['pacenote_first_m'] - start <= high]
        if len(near) == 1:
            return near[0]
    if len(found) > 1 and length:
        found.sort(key=lambda e: min(abs(v - length) for v in (e.get('length_m') or 0.0,
                                                                 e.get('pacenote_last_m') or 0.0)))
        return found[0]
    return found[0] if len(found) == 1 else None


# The game's official sectors (`sectors_km`) along the road spline. The bridge
# sends no sector index or time (the graphics page's currentSectorIndex is not
# read), so they are placed by length. Against marth's captures of Wales Afon Bidno:
# the line is at 238.2 m on every run, the flying finish at 5287 m, so 5049 m
# are driven against sectors that add up to 5100 (the published 4800 is not the
# road): the sectors are taken as the road from the start line to the finish,
# scaled to it. 24 of 46 rows' sectors do not add up to their length: those are left out.
ROAD_BEFORE_NOTE = 25.0          # m the start line is before the first pace note, as far as the road length goes (start_line)
START_BEFORE_FIRST_NOTE = 35.0   # m the start line is before the first pace note (25 on Afon Bidno and Elatia, 38-47 elsewhere)
SECTOR_SCALE = (0.9, 1.1)        # the sectors' sum against the road from the start line to the finish
SECTOR_SUM_TOLERANCE = 0.03      # the sectors' sum against the stage's length where there is no finish line


def sector_bounds(entry):
    """The official sectors of a stage entry along the road spline, or None where they cannot be
    placed: {'start_m' (the start line), 'bounds': [(start_m, end_m)] per sector, 'confidence', 'source'}.
    With `finish_m` the sectors are scaled to the road from the start line (`start_m` where measured,
    else the first pace note less START_BEFORE_FIRST_NOTE) to the finish ('medium' with a measured
    start line and a medium finish, else 'low'); without it they are laid end to end from the start line
    where they add up to the stage's length ('low')."""
    sectors = (entry or {}).get('sectors_km')
    if not sectors or len(sectors) < 2 or not all(isinstance(v, (int, float)) and v > 0 for v in sectors):
        return None
    total = sum(sectors) * 1000.0
    measured = entry.get('start_m')
    start = measured
    if start is None:
        first = entry.get('pacenote_first_m')
        if first is None:
            return None
        start = max(0.0, first - START_BEFORE_FIRST_NOTE)
    finish = entry.get('finish_m')
    if finish is not None and finish > start:
        scale = (finish - start) / total
        if not SECTOR_SCALE[0] <= scale <= SECTOR_SCALE[1]:
            return None
        source = 'scaled to the finish line'
        confidence = 'medium' if measured is not None and entry.get('finish_confidence') in ('medium', 'high') else 'low'
    else:
        length = entry.get('length_m')
        if not length or abs(total - length) > SECTOR_SUM_TOLERANCE * length:
            return None
        last = entry.get('pacenote_last_m')
        if last is not None and start + total > last:
            return None
        scale, source, confidence = 1.0, 'laid end to end from the start line', 'low'
    bounds, at = [], start
    for v in sectors:
        bounds.append((at, at + v * 1000.0 * scale))
        at = bounds[-1][1]
    return {'start_m': start, 'bounds': bounds, 'confidence': confidence, 'source': source}


START_LINE_PAST = 30.0           # m: an ACR run that began further along the road than this past the stage's start line did not start the stage


def start_line(entry):
    """Where an ACR stage's start line is along the road spline: measured (`start_m`), else the first pace
    note less ROAD_BEFORE_NOTE, else None."""
    if not entry:
        return None
    if entry.get('start_m') is not None:
        return entry['start_m']
    if entry.get('pacenote_first_m') is not None:
        return entry['pacenote_first_m'] - ROAD_BEFORE_NOTE
    return None


def road_length(entry):
    """The road a run drives on an ACR stage, from the start line to the finish (the flying finish, else the
    last pace note), or None where either end is not known. The `length` the game sends is the length of its
    spline, which is longer than the road."""
    if not entry:
        return None
    start = start_line(entry)
    end = entry.get('finish_m') or entry.get('pacenote_last_m')
    return end - start if start is not None and end is not None and end > start else None


def entry(key):
    """The shipped entry of a stage key, or None."""
    if not key:
        return None
    return _learnt_entry(key, tables().get(key.split(':', 1)[0], {}).get(key))


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
