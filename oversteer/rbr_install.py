"""Richard Burns Rally (RallySimFans, RSF) install, found on its own and optional: the shipped tables
(data/telemetry/stages/rbr.json) name every stage RSF lists, and nothing has to be set up.

Where an install is found without being told (the usual Lutris, Bottles, Heroic, Steam and Wine places, and the
game path in Lutris' game files; no setting, no environment variable) it adds what no shipped table can:

- `Cars/Cars.ini` `[Car05]` `RSFCarID` (RSF writes it when a stage loads) with `rsfdata/cache/cars.json` (name) and
  `cars_data.json` (drivetrain, gearbox): the exact car. Without an install the packets give no reliable car (the
  slot is always 5 or 6, and cars with the same mass, balance, idle and redline cannot be told apart), so the car
  key is the shared `rbr/unknown`.
- `rsfdata/cache/stages_data.json` and `Maps/Tracks.ini`: custom stages the shipped table lacks, and corrections;
  built into `<data>/oversteer/rbr/stages.json` (the format of data/telemetry/stages) until the install's files
  change, then merged over the shipped table by stage_tables.load.

Nothing is ever written under the install. A sandbox (the Flatpak) that cannot see it simply finds none.
$OVERSTEER_RBR_DIR points at an install for development; it is not a setting.
"""

import glob
import json
import logging
import os
import re
import threading
import time

from . import diagnostics

ENV = 'OVERSTEER_RBR_DIR'
SLOT = 5                                             # RSF puts the chosen car in this slot (packet car.index)
SURFACE_RSF = {1: 'tarmac', 2: 'gravel', 3: 'snow'}  # stages_data.json surface_id
SURFACE_TRACKS_INI = {0: 'tarmac', 1: 'gravel', 2: 'snow'}   # Maps/Tracks.ini Surface
ROOT_RECHECK = 30.0                                  # s between looks for an install that was not found
UNKNOWN_CAR = 'rbr/unknown'                          # a car the files do not name: shared, so nothing is learnt under it
CAR_RECHECK = 2.0                                    # s between looks at Cars.ini (RSF rewrites it at a stage load)
_FOLDERS = ('Richard Burns Rally', 'RBR', 'Games/Richard Burns Rally', 'Games/RBR', 'RallySimFans',
            'Program Files (x86)/Richard Burns Rally', 'Program Files/Richard Burns Rally')
_PREFIXES = (
    '~/Games/*/drive_c', '~/Games/*/*/drive_c', '~/Games/Heroic/Prefixes/*/*/drive_c', '~/Games/Heroic/*/drive_c',
    '~/.wine/drive_c', '~/.local/share/bottles/bottles/*/drive_c',
    '~/.var/app/com.usebottles.bottles/data/bottles/bottles/*/drive_c',
    '~/.local/share/Steam/steamapps/compatdata/*/pfx/drive_c', '~/.steam/steam/steamapps/compatdata/*/pfx/drive_c',
    '~/.var/app/com.valvesoftware.Steam/data/Steam/steamapps/compatdata/*/pfx/drive_c',
    '~/.local/share/lutris/runners/wine/*/drive_c',
)
CANDIDATES = tuple('{}/{}'.format(prefix, folder) for prefix in _PREFIXES for folder in _FOLDERS)
STEAM_SHORTCUTS = ('~/.local/share/Steam/userdata/*/config/shortcuts.vdf', '~/.steam/steam/userdata/*/config/shortcuts.vdf',
                   '~/.var/app/com.valvesoftware.Steam/data/Steam/userdata/*/config/shortcuts.vdf')
SHORTCUTS_MAX = (20, 1 << 20)       # files read, and bytes of each: the search is bounded
ASYNC = True                        # a search that is not asked for synchronously runs on a thread of its own (tests: False)
LUTRIS_GAMES = ('~/.config/lutris/games', '~/.var/app/net.lutris.Lutris/config/lutris/games')

_root = None
_root_at = None
_searching = False


def data_dir():
    return os.path.join(diagnostics.data_home(), 'oversteer', 'rbr')


def resolve(root, rel):
    """`rel` (any case, either slash) under `root`, or None: the game's files are case-insensitive."""
    path = root
    for part in rel.replace('\\', '/').split('/'):
        if not part or part == '.':
            continue
        try:
            names = os.listdir(path)
        except OSError:
            return None
        match = part if part in names else next((n for n in names if n.lower() == part.lower()), None)
        if match is None:
            return None
        path = os.path.join(path, match)
    return path


def _is_root(path):
    return bool(path) and os.path.isdir(path) and (resolve(path, 'RichardBurnsRally.ini') is not None
                                                   or resolve(path, 'rsfdata/cache') is not None)


def find_root(refresh=False):
    """The install folder, or None, searching here if it has not been (or `refresh`). The packet path uses
    current_root() instead, which never searches on its thread."""
    global _root, _root_at
    now = time.monotonic()
    if not refresh and _root_at is not None and (_root is not None or now - _root_at < ROOT_RECHECK):
        return _root
    _root_at = now
    found = None
    candidates = [os.environ.get(ENV)]
    for pattern in CANDIDATES:
        candidates += sorted(glob.glob(os.path.expanduser(pattern)))
    candidates += _lutris_roots() + _steam_roots()
    for path in candidates:
        if _is_root(path):
            found = path
            break
    _root = found
    return _root


def _steam_roots():
    """The folders of Steam non-Steam shortcuts that point at Richard Burns Rally: shortcuts.vdf is binary, its strings
    (Exe, StartDir, quoted) are NUL-ended; a path with the game's name in it is the exe, its folder the install."""
    found, files = [], 0
    for pattern in STEAM_SHORTCUTS:
        for path in sorted(glob.glob(os.path.expanduser(pattern))):
            if files >= SHORTCUTS_MAX[0]:
                return found
            files += 1
            try:
                with open(path, 'rb') as f:
                    data = f.read(SHORTCUTS_MAX[1])
            except OSError:
                continue
            for match in re.finditer(rb'[^\x00\x01\x02"]*richardburnsrally[^\x00"]*', data, re.I):
                text = match.group(0).decode('utf-8', 'replace').strip()
                if text.startswith('/'):
                    found.append(os.path.dirname(text) if os.path.splitext(text)[1] else text)
    return found


def _lutris_roots():
    """The folders of Lutris games whose executable is Richard Burns Rally's (its game files hold `exe: <path>`)."""
    found = []
    for folder in LUTRIS_GAMES:
        for path in sorted(glob.glob(os.path.join(os.path.expanduser(folder), '*.yml')))[:200]:
            try:
                with open(path, encoding='utf-8', errors='replace') as f:
                    text = f.read(20000)
            except OSError:
                continue
            for match in re.finditer(r'^\s*exe:\s*[\'"]?(.+?)[\'"]?\s*$', text, re.M):
                exe = match.group(1)
                if 'richardburnsrally' in exe.lower().replace(' ', ''):
                    found.append(os.path.dirname(exe))
    return found


def current_root(retry=False):
    """The install found so far, without searching on the caller's thread (the packet path asks): the search runs on
    a thread of its own when none has run yet, or, with `retry` (a run starting), when the last one found nothing
    ROOT_RECHECK s ago. Until it has finished the answer is None."""
    global _searching
    now = time.monotonic()
    due = _root_at is None or (retry and _root is None and now - _root_at >= ROOT_RECHECK)
    if due and not _searching:
        if ASYNC:
            _searching = True

            def search():
                global _searching
                try:
                    find_root(refresh=True)
                finally:
                    _searching = False
            threading.Thread(target=search, name='rbr-install', daemon=True).start()
        else:
            find_root(refresh=True)
    return _root


def forget():
    """Look for the install again at the next call (tests; a changed setting)."""
    global _root, _root_at, _cars, _searching
    _root = _root_at = _cars = None
    _searching = False
    _slot_cache.clear()


def read_ini(path):
    """{section: {key: value}} of an RBR ini: cp1252, CRLF, quoted values, keys as written. A line that is no
    section or `key = value` (the stray `***` lines some of RBR's own files have) is skipped."""
    out, section = {}, None
    with open(path, encoding='cp1252', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line or line[0] in ';#':
                continue
            if line[0] == '[':
                end = line.find(']')
                section = out.setdefault(line[1:end].strip(), {}) if end > 0 else None
            elif section is not None and '=' in line:
                key, _, value = line.partition('=')
                section[key.strip()] = value.split(';', 1)[0].strip().strip('"')
    return out


def _json(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def _int(v, default=None):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _float(v, default=None):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


# -- stages --

def _fingerprint(root, rels):
    prints = [root]
    for rel in rels:
        path = resolve(root, rel)
        try:
            st = os.stat(path) if path else None
        except OSError:
            st = None
        prints.append([rel, st.st_size, st.st_mtime_ns] if st else [rel, None, None])
    return prints


STAGE_FILES = ('rsfdata/cache/stages_data.json', 'Maps/Tracks.ini')


def stage_fingerprint(root):
    """What stage_table() caches against: the install's path and its two stage files' sizes and times."""
    return _fingerprint(root, STAGE_FILES)


def _surface_parts(row):
    """({surface: share 0..1}, the one surface or 'mixed') of a stages_data.json row."""
    shares = {s: (_int(row.get(s), 0) or 0) / 100.0 for s in ('tarmac', 'gravel', 'snow')}
    shares = {s: v for s, v in shares.items() if v > 0}
    if len(shares) >= 2:
        return shares, 'mixed'
    if len(shares) == 1:
        return shares, next(iter(shares))
    return {}, SURFACE_RSF.get(_int(row.get('surface_id')))


def build_stages(root):
    """The stage entries of an install ({id, location, stage, length_m, surface, surface_parts, sources}), in
    id order. stages_data.json first (RSF's list, with the shares of each surface), then Maps/Tracks.ini for the
    ids it lacks (StageName, Length in km, Surface)."""
    out = {}
    path = resolve(root, STAGE_FILES[0])
    if path:
        for row in _json(path):
            if not isinstance(row, dict):
                continue
            sid = _int(row.get('stage_id', row.get('id')))
            if sid is None or not row.get('name'):
                continue
            parts, surface = _surface_parts(row)
            entry = {'id': sid, 'location': row.get('short_country') or None, 'stage': str(row['name']),
                     'length_m': _int(row.get('length')) or None, 'surface': surface,
                     'sources': ['stages_data.json']}
            if parts:
                entry['surface_parts'] = parts
            out[sid] = entry
    path = resolve(root, 'Maps/Tracks.ini')
    if path:
        try:
            sections = read_ini(path)
        except OSError:
            sections = {}
        for section, keys in sections.items():
            sid = _int(section[3:]) if section.lower().startswith('map') else None
            if sid is None or sid in out or not keys.get('StageName'):
                continue
            km = _float(keys.get('Length'))
            out[sid] = {'id': sid, 'location': keys.get('CountryCode') or None, 'stage': keys['StageName'],
                        'length_m': round(km * 1000) if km else None,
                        'surface': SURFACE_TRACKS_INI.get(_int(keys.get('Surface'))),
                        'sources': ['Tracks.ini']}
    return [out[k] for k in sorted(out)]


def stage_table(root=None):
    """The local stage table ({'game': 'rbr', 'stages': [...]}) of the install, or None: read from the cache in
    the user's data folder, built (and the cache written) when it is missing or the install's files changed."""
    root = root or find_root()
    if not root:
        return None
    prints = _fingerprint(root, STAGE_FILES)
    cache = os.path.join(data_dir(), 'stages.json')
    try:
        with open(cache, encoding='utf-8') as f:
            table = json.load(f)
        if table.get('fingerprint') == prints and table.get('stages'):
            return table
    except (OSError, ValueError, AttributeError):
        pass
    stages = build_stages(root)
    if not stages:
        return None
    table = {'game': 'rbr', 'fingerprint': prints, 'stages': stages,
             'notes': "Built by Oversteer from this install's RallySimFans files; not for redistribution."}
    try:
        os.makedirs(data_dir(), exist_ok=True)
        tmp = cache + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(table, f)
        os.replace(tmp, cache)
    except OSError as e:
        logging.warning("rbr: stage table not cached in %s: %s", data_dir(), e)
    logging.info("rbr: stage table of %d stages built from %s", len(stages), root)
    return table


# -- cars --

_cars = None                                         # (fingerprint, {RSF car id: {...}})
_slot_cache = {}                                     # slot -> (checked at, Cars.ini (size, mtime), car dict or None)
DRIVETRAINS = {'fwd': 'fwd', 'rwd': 'rwd', 'awd': 'awd', '4wd': 'awd'}


def _car_list(root):
    """{RSF car id: {'name', 'drivetrain', 'gears'}} from cars.json and cars_data.json, read again when they change."""
    global _cars
    rels = ('rsfdata/cache/cars.json', 'rsfdata/cache/cars_data.json')
    prints = _fingerprint(root, rels)
    if _cars is not None and _cars[0] == prints:
        return _cars[1]
    cars = {}
    try:
        path = resolve(root, rels[0])
        for row in _json(path) if path else []:
            cid = _int(row.get('id')) if isinstance(row, dict) else None
            if cid is not None and row.get('name'):
                cars[cid] = {'name': str(row['name'])}
        path = resolve(root, rels[1])
        for row in _json(path) if path else []:
            cid = _int(row.get('car_id')) if isinstance(row, dict) else None
            if cid in cars:
                # "AWD with torque split", "AWD w/o torque split (50:50)", "FWD", "RWD": the first word
                word = re.match(r'\s*([A-Za-z0-9]+)', str(row.get('drive_train') or ''))
                drive = DRIVETRAINS.get(word.group(1).lower()) if word else None
                if drive:
                    cars[cid]['drivetrain'] = drive
                match = re.match(r'\s*(\d+)\s*gear', str(row.get('transmission') or ''), re.I)
                if match and 1 < int(match.group(1)) < 12:
                    cars[cid]['gears'] = int(match.group(1))
    except (OSError, ValueError) as e:
        logging.warning("rbr: car list not read: %s", e)
    _cars = (prints, cars)
    return cars


def car_in_slot(slot=SLOT, root=None):
    """The car RSF put in `slot` of Cars/Cars.ini, or None when no install is found: {'key': 'rbr/<RSFCarID>' (or
    UNKNOWN_CAR where the slot has no RSFCarID), 'name', 'id', 'drivetrain', 'gears'} (the last two where
    cars_data.json has them). Cars.ini is looked at again at most every CAR_RECHECK s, and read again only when it
    changed: RSF writes it when a stage loads."""
    now = time.monotonic()
    cached = _slot_cache.get(slot)
    if cached is not None and now - cached[0] < CAR_RECHECK:
        return cached[2]
    root = root or current_root()
    path = resolve(root, 'Cars/Cars.ini') if root else None
    try:
        st = os.stat(path) if path else None
    except OSError:
        st = None
    stamp = (root, st.st_size, st.st_mtime_ns) if st else None
    if cached is not None and cached[1] == stamp:
        _slot_cache[slot] = (now, stamp, cached[2])
        return cached[2]
    car = None
    if stamp is not None:
        try:
            keys = read_ini(path).get('Car%02d' % slot, {})
        except OSError:
            keys = {}
        rid = _int(keys.get('RSFCarID'))
        listed = _car_list(root).get(rid) if rid is not None else None
        name = (listed or {}).get('name') or keys.get('CarName') or None
        car = {'key': 'rbr/%d' % rid if rid is not None else UNKNOWN_CAR, 'id': rid, 'name': name,
               'drivetrain': (listed or {}).get('drivetrain'), 'gears': (listed or {}).get('gears')}
    _slot_cache[slot] = (now, stamp, car)
    return car
