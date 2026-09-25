#!/usr/bin/env python3
"""Build a shipped stage table (data/telemetry/stages/) from a game's own
installed data.

    scripts/stage-tables.py wrcg [--game-dir DIR] [--out PATH]

The games' files are only read, never written, and the table keeps only
facts derived from them (codes, names, lengths, elevation, surface), not
the files. Whatever the previous table has that the game does not (the
surface of a shakedown and its sources) is kept.

WRC Generations (Common/, plain text):
- Tuning/RALLIESMETRICS.CFG: per level (LEVELS/<LEVEL>/<LEVEL>.PLM) every
  route code (SS1_RaceTrack, SS1R_RaceTrack its reverse, ES1_RaceTrack a
  long stage, <cc>_Short_01 a shakedown, RaceTrack and VersusRaceTrack in a
  super special's level) with its Length in km (the float the game sends in
  its telemetry, field 3), ElevationMin/Max, SlopeUp/SlopeDown (the total
  climb and descent, m), IsShakedown and LoadId.
- Tuning/FACTORIES/RALLIES.CFG and BONUS_RALLIES.CFG: the 21 rallies, each
  special with its name key (LocalesId.STAGENAME_...), SelectiveLoadId (the
  route whose LoadId is 1 << SelectiveLoadId in the rally's level; a super
  special names its own Level), the length shown in the menus, Surface,
  SurfaceRatio (% of Surface), Surface2 and Reverse; a shakedown only its
  ShortStage code.
- SCRIPTS/LOCALISATION.LUA: LocalesId name -> index; LOCALISATION/
  LOCALISATION_EN.PLOC: a 4-byte header, then the strings in index order,
  UTF-16LE, each ended by a NUL.
"""
import argparse
import json
import os
import re
import sys
import unicodedata

STEAM = '/mnt/gaming/Steam/steamapps/common'
WRCG_DIR = os.path.join(STEAM, 'WRC Generations - The FIA WRC Official Game')
HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = os.path.join(HERE, '..', 'data', 'telemetry', 'stages')

# The table's location names (the keys of earlier tables), per level
WRCG_LOCATION = {
    'MC_MONTECARLO': 'Rallye Monte-Carlo', 'SE2_SWEDEN': 'Rally Sweden', 'HR_CROATIA': 'Croatia Rally',
    'PT2_PORTUGAL': 'Rally de Portugal', 'IT_ITALIASARDEGNA': 'Rally Italia Sardegna',
    'KE_KENYA': 'Safari Rally Kenya', 'EE_ESTONIA': 'Rally Estonia', 'FI2_FINLAND': 'Rally Finland',
    'BE_YPRES': 'Ypres Rally Belgium', 'GR_ACROPOLIS': 'Acropolis Rally of Gods',
    'NZ_NEW_ZEALAND': 'Rally New Zealand', 'ES2_SPAIN': 'Rally de España', 'JP_JAPAN': 'Rally Japan',
    'AR_ARGENTINA': 'Rally Argentina', 'CL_CHILE': 'Rally Chile', 'DE_DEUTSCHLAND': 'Rallye Deutschland',
    'MX_MEXICO': 'Rally México', 'IT_SANREMO': 'Rally Sanremo', 'FR_FRANCECORSE': 'Tour de Corse',
    'TK_TURKEY': 'Rally Turkey', 'GB_WALESGB': 'Wales Rally GB',
}
WRCG_SURFACE = {'TARMAC': 'tarmac', 'GRAVEL': 'gravel', 'SNOW': 'snow', 'ICE': 'ice'}
WRCG_FILES = ('Common/Tuning/RALLIESMETRICS.CFG', 'Common/Tuning/FACTORIES/RALLIES.CFG',
              'Common/Tuning/FACTORIES/BONUS_RALLIES.CFG', 'Common/SCRIPTS/LOCALISATION.LUA',
              'Common/LOCALISATION/LOCALISATION_EN.PLOC')

# -- a reader for the games' Lua tables --

_TOKEN = re.compile(r'\s+|--\[\[.*?\]\](?:--)?|--[^\n]*|(?P<s>"[^"]*")|(?P<n>-?\d+\.?\d*(?:[eE][-+]?\d+)?)'
                    r'|(?P<id>[A-Za-z_][\w.]*)|(?P<p>[{}=,\[\];])', re.S)


def lua_tables(text):
    """{name: value} of the top-level assignments of a Lua data file:
    tables as dicts (a list where it has no keys), identifiers such as
    LocalesId.X as strings."""
    tokens, i = [], 0
    while i < len(text):
        m = _TOKEN.match(text, i)
        if not m:
            raise ValueError('unreadable at {}: {!r}'.format(i, text[i:i + 20]))
        i = m.end()
        if m.group('s') is not None:
            tokens.append(('v', m.group('s')[1:-1]))
        elif m.group('n'):
            n = m.group('n')
            tokens.append(('v', float(n) if '.' in n or 'e' in n.lower() else int(n)))
        elif m.group('id'):
            word = m.group('id')
            tokens.append(('v', {'true': True, 'false': False, 'nil': None}[word])
                          if word in ('true', 'false', 'nil') else ('id', word))
        elif m.group('p'):
            tokens.append(('p', m.group('p')))
    pos = [0]

    def peek(k=0):
        return tokens[pos[0] + k] if pos[0] + k < len(tokens) else (None, None)

    def take():
        pos[0] += 1
        return tokens[pos[0] - 1]

    def value():
        kind, v = take()
        return table() if (kind, v) == ('p', '{') else v

    def table():
        keyed, listed = {}, []
        while True:
            kind, v = peek()
            if kind is None or (kind, v) == ('p', '}'):
                take()
                break
            if kind == 'p' and v in ',;':
                take()
            elif (kind, v) == ('p', '['):
                take()
                key = value()
                take()                                  # ]
                take()                                  # =
                keyed[key] = value()
            elif kind == 'id' and peek(1) == ('p', '='):
                take()
                take()
                keyed[v] = value()
            else:
                listed.append(value())
        if listed and not keyed:
            return listed
        if listed:
            keyed['_list'] = listed
        return keyed

    top = {}
    while pos[0] < len(tokens):
        kind, v = take()
        if kind == 'id' and peek() == ('p', '='):
            take()
            top[v] = value()
    return top


def _slug(text):
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


def _read(path):
    with open(path, encoding='latin-1') as f:
        return f.read()


# -- WRC Generations --

def wrcg_strings(game_dir, lang='EN'):
    """{LocalesId name: string} of one language."""
    with open(os.path.join(game_dir, 'Common', 'LOCALISATION', 'LOCALISATION_{}.PLOC'.format(lang)), 'rb') as f:
        data = f.read()[4:]
    strings = []
    i = 0
    while i < len(data):
        j = i
        while j + 1 < len(data) and data[j:j + 2] != b'\0\0':
            j += 2
        strings.append(data[i:j].decode('utf-16-le'))
        i = j + 2
    ids = lua_tables(_read(os.path.join(game_dir, 'Common', 'SCRIPTS', 'LOCALISATION.LUA')))['LocalesId']
    return {name: strings[index] for name, index in ids.items() if index < len(strings)}


def _level(path):
    return path.replace('\\', '/').split('/')[-2].upper()


def _km(value):
    return round(float(value) * 1000.0, 6)


def wrcg_table(game_dir, previous=None):
    common = os.path.join(game_dir, 'Common')
    metrics = {_level(path): routes for path, routes in
               lua_tables(_read(os.path.join(common, 'Tuning', 'RALLIESMETRICS.CFG')))['RalliesMetrics'].items()}
    rallies = {}
    for name in ('RALLIES.CFG', 'BONUS_RALLIES.CFG'):
        rallies.update(lua_tables(_read(os.path.join(common, 'Tuning', 'FACTORIES', name)))['Rallies'])
    text = wrcg_strings(game_dir)
    old = {}
    for entry in (previous or {}).get('stages', []):
        old[(entry['location'], _slug(entry['stage']))] = entry
        if entry.get('shakedown'):
            old[(entry['location'], 'shakedown')] = entry

    def loc(key):
        return text.get(key.split('.', 1)[1]) if key else None

    stages, used = [], set()
    for rally_id in sorted(rallies):
        rally = rallies[rally_id]
        level = _level(rally['Level'])
        location = WRCG_LOCATION[level]
        routes = metrics[level]
        rally_entries = []
        for special in rally['Specials']:
            entry = {'location': location, 'rally': loc(rally['Name'])}
            alt = []
            if special.get('ShortStage'):
                code = special['ShortStage']
                country = code.split('_')[0].rstrip('0123456789')
                entry['stage'] = text.get('STAGENAME_{}_SHK_01'.format(country)) or '{} shakedown'.format(location)
                name_source = 'LOCALISATION: STAGENAME_{}_SHK_01 (the key by the convention of the others)'.format(
                    country)
                kind = 'shakedown'
                route_level = level
            else:
                entry['stage'] = loc(special['Name'])
                name_source = 'LOCALISATION: ' + special['Name'].split('.', 1)[1]
                kind = {'WrcStageTypes.ES': 'long', 'WrcStageTypes.SSS': 'super special'}.get(
                    special.get('StageType'), 'special')
                if special.get('Level'):
                    route_level = _level(special['Level'])
                    codes = [c for c in metrics[route_level] if not c.startswith('Versus')]
                    alt = [c for c in metrics[route_level] if c.startswith('Versus')]
                    if len(codes) != 1:
                        raise ValueError('{}: {} routes'.format(route_level, codes))
                    code = codes[0]
                else:
                    route_level = level
                    load_id = str(1 << special['SelectiveLoadId'])
                    codes = [c for c, r in routes.items() if str(r.get('LoadId')) == load_id
                             and not r.get('IsShakedown')]
                    main = [c for c in codes if re.search(r'(SS|ES)\d*R?_RaceTrack$', c)]
                    if len(main) != 1:
                        raise ValueError('{} {}: {}'.format(level, entry['stage'], codes))
                    code = main[0]
                    alt = [c for c in codes if c != code]
                    if bool(re.search(r'R_RaceTrack$', code)) != bool(special.get('Reverse')):
                        raise ValueError('{} {}: {} against Reverse = {}'.format(level, entry['stage'], code,
                                                                                 special.get('Reverse')))
            route = metrics[route_level][code]
            used.add((route_level, code))
            used.update((route_level, c) for c in alt)
            entry.update(code=code, level=route_level, length_m=_km(route['Length']),
                         menu_length_m=_km(special['Length']) if special.get('Length') else None,
                         elevation_min_m=route.get('ElevationMin'), elevation_max_m=route.get('ElevationMax'),
                         climb_m=route.get('SlopeUp'), descent_m=-route['SlopeDown'] if 'SlopeDown' in route else None)
            if alt:
                entry['alt_codes'] = [{'code': c, 'length_m': _km(metrics[route_level][c]['Length'])} for c in alt]
            entry['kind'] = kind
            entry['reverse_of'] = None
            if special.get('Reverse'):
                forward = [e for e in rally_entries if e.get('code', '').replace('_RaceTrack', 'R_RaceTrack') == code]
                entry['reverse_of'] = forward[0]['stage'] if forward else re.sub(r' reverse$', '', entry['stage'])
            entry['shakedown'] = kind == 'shakedown'
            sources = ['game: ' + f for f in WRCG_FILES]
            note = []
            surface = special.get('Surface')
            previous_entry = old.get((location, 'shakedown' if kind == 'shakedown' else _slug(entry['stage'])))
            if surface:
                first = WRCG_SURFACE[surface.split('.')[-1]]
                second = special.get('Surface2')
                second = WRCG_SURFACE[second.split('.')[-1]] if second else None
                ratio = special.get('SurfaceRatio', 100)
                if ratio >= 100 or second in (None, first):
                    entry['surface'], entry['surface_parts'] = first, None
                else:
                    entry['surface'] = 'mixed'
                    entry['surface_parts'] = {first: round(ratio / 100.0, 2), second: round(1 - ratio / 100.0, 2)}
                surface_source = 'game'
                if previous_entry and (previous_entry.get('surface'), previous_entry.get('surface_parts')) != (
                        entry['surface'], entry['surface_parts']):
                    note.append('surface: the game says {}, the earlier table (web) {}'.format(
                        _surface_words(entry), _surface_words(previous_entry)))
            elif previous_entry and previous_entry.get('surface'):
                entry['surface'] = previous_entry['surface']
                entry['surface_parts'] = previous_entry.get('surface_parts')
                surface_source = 'earlier table'
                sources += [s for s in previous_entry.get('sources', []) if not s.startswith('game')]
                note.append('surface not in the game files (a shakedown has none): the earlier table\'s, from '
                            'its sources')
            else:
                entry['surface'], entry['surface_parts'] = _surface_of_rally(rally_entries)
                surface_source = 'inferred'
                note.append('surface not in the game files: that of the rally\'s stages')
            entry['surface_source'] = surface_source
            entry['name_source'] = name_source
            entry['sources'] = sources
            entry['confidence'] = 'high' if surface_source == 'game' else 'medium'
            if previous_entry and previous_entry.get('length_m') and abs(
                    previous_entry['length_m'] - entry['length_m']) >= 5.0:
                note.append('the earlier table (web) had {:.0f} m'.format(previous_entry['length_m']))
            entry['note'] = '; '.join(note) or None
            rally_entries.append(entry)
        stages.extend(rally_entries)

    unused = sorted('{} {} ({:.1f} m)'.format(level, code, _km(route['Length']))
                    for level, routes in metrics.items() for code, route in routes.items()
                    if (level, code) not in used)
    return {
        'game': 'wrcg',
        'title': 'WRC Generations stages',
        'keys': 'WRC Generations sends the length of the stage (Codemasters layout, field 3, in km: the Length '
                'of its route in RALLIESMETRICS.CFG, to the float), which is matched to length_m (or an '
                'alt_codes length) within 0.5 m at the start of a run. Where it sends none, a run to the '
                'finish is matched by its distance within 1 % of length_m, and by where earlier runs of a '
                'stage started when two stages are that close.',
        'notes': 'Built by scripts/stage-tables.py from the installed game (WRC Generations, Steam, vanilla '
                 'files): {} entries, {} rallies. code and level are the route in RALLIESMETRICS.CFG '
                 '(LEVELS/<level>/<level>.PLM); length_m is its Length in km times 1000, the value the game '
                 'sends; menu_length_m the rounded figure the rally menus show (RALLIES.CFG), which is not what '
                 'is driven; elevation_min_m/elevation_max_m, climb_m and descent_m (total climb and descent) '
                 'are the route\'s. Names are the English localisation of each special\'s name key; a '
                 'shakedown has no name key in the rally data, so its name is the string of '
                 'STAGENAME_<country>_SHK_01. Surfaces are the specials\' Surface, SurfaceRatio (share of '
                 'Surface) and Surface2; the game gives none for shakedowns, which keep the earlier table\'s '
                 '(WRC Generations fandom wiki, TechBriefly). A super special\'s level also has a '
                 'VersusRaceTrack (its head-to-head layout), kept as alt_codes. Routes in no rally: {}.'.format(
                     len(stages), len(rallies), ', '.join(unused)),
        'stages': stages,
    }


def _surface_words(entry):
    parts = entry.get('surface_parts')
    if isinstance(parts, dict):
        return ' '.join('{} {:.0f} %'.format(s, p * 100) for s, p in parts.items())
    return entry.get('surface')


def _surface_of_rally(entries):
    found = {e['surface'] for e in entries if e.get('surface') in WRCG_SURFACE.values()}
    return (found.pop(), None) if len(found) == 1 else ('mixed', sorted(found))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('game', choices=['wrcg'])
    parser.add_argument('--game-dir', help='the game\'s install folder')
    parser.add_argument('--out', help='where to write the table (default: data/telemetry/stages/<game>.json)')
    args = parser.parse_args()
    out = args.out or os.path.join(STAGES, args.game + '.json')
    previous = None
    if os.path.exists(out):
        with open(out, encoding='utf-8') as f:
            previous = json.load(f)
    table = wrcg_table(args.game_dir or WRCG_DIR, previous)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(table, f, ensure_ascii=False, indent=1)
        f.write('\n')
    print('{}: {} entries'.format(out, len(table['stages'])), file=sys.stderr)


if __name__ == '__main__':
    main()
