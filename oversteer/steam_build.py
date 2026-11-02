"""The build of a game Steam has installed, for each session (sessions.game_build): what a PB was set on, so a patch
that changes the cars or the stages shows (telemetry_store.Reader.stage_pb_builds).

Steam keeps one app manifest per installed game, `<library>/steamapps/appmanifest_<appid>.acf` (text KeyValues), whose
`buildid` is the build installed (a number unique across Steam) and `LastUpdated` when it was. The libraries are listed
in `libraryfolders.vdf` under each Steam root (the native, Debian/Ubuntu and Flatpak Steam places): nothing to set up.

A game key names one or more Steam apps (APPIDS): a key the packets share between games (the Forza Horizons, the AC
family's shared memory, DiRT) takes the installed one Steam last started (`LastPlayed`, written at launch). A game not
on Steam (Richard Burns Rally, Live for Speed) has none: its build is unknown (None).

Inside the Flatpak the home folder is not readable: the same files are read through `flatpak-spawn --host` (the
sandbox already talks to org.freedesktop.Flatpak for the proxy service), and where that fails too the build is unknown.
lookup() is called once per session start, on the drive-log thread, and caches for CACHE_S: never on the packet path.
"""

import logging
import os
import re
import subprocess
import threading
import time

# Game keys (Sample.game, cars.game) to Steam app ids, the likeliest first. Ids from the manifests of marth's
# libraries where installed there (ACR, ACC, DiRT Rally 2.0, WRC Generations, Forza Horizon 6, BeamNG.drive)
APPIDS = {
    'acr': (3917090,),                         # Assetto Corsa Rally
    'ac': (244210,),                           # Assetto Corsa
    'acc': (805550,),                          # Assetto Corsa Competizione
    'acpmf': (805550, 244210, 3058630),        # the AC family's shared memory: ACC, AC, Assetto Corsa EVO
    'dirt': (690790, 310560, 421020),          # DiRT Rally 2.0, DiRT Rally, DiRT 4 (one packet length)
    'wrcg': (1953520,),                        # WRC Generations
    'eawrc': (1849250,),                       # EA SPORTS WRC
    'forza-fh': (2483190, 1551360, 1293830),   # Forza Horizon 6, 5, 4 (one packet format)
    'forza-fm': (2440510,),                    # Forza Motorsport (2023)
    'forza': (2440510,),                       # the 'sled' format: Forza Motorsport (2023); FM7 is not on Steam
    'beamng': (284160,),                       # BeamNG.drive
    'lfs': (),                                 # Live for Speed: not on Steam
    'rbr': (),                                 # Richard Burns Rally (RSF): not on Steam
}
STEAM_ROOTS = ('~/.local/share/Steam', '~/.steam/steam', '~/.steam/root', '~/.steam/debian-installation',
               '~/.var/app/com.valvesoftware.Steam/data/Steam',
               '~/.var/app/com.valvesoftware.Steam/.local/share/Steam', '~/snap/steam/common/.local/share/Steam')
CACHE_S = 60.0              # s a game's build is kept before the manifest is read again (one per session start)
MAX_BYTES = 1 << 20         # of any one file read
HOST_TIMEOUT = 3.0          # s for one read through flatpak-spawn --host
HOST_RETRY_S = 600.0        # s before flatpak-spawn is tried again after it failed to run

_cache = {}                 # game -> (monotonic time, manifest dict or None)
_host_failed_at = None
_lock = threading.Lock()

_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|//[^\n]*|([^\s{}"]+)')


def parse_vdf(text):
    """Valve's text KeyValues (libraryfolders.vdf, appmanifest_*.acf) as nested dicts, keys lowercased (Steam writes
    them in either case). A file cut short gives what was read; anything unparsable gives {}."""
    root = {}
    stack = [root]
    key = None
    for match in _TOKEN.finditer(text):
        quoted, brace, bare = match.groups()
        if brace == '{':
            child = {}
            if key is not None:
                stack[-1][key.lower()] = child
                key = None
            stack.append(child)
        elif brace == '}':
            if len(stack) > 1:
                stack.pop()
            key = None
        elif quoted is not None or bare is not None:
            token = quoted if quoted is not None else bare
            if quoted is not None:
                token = token.replace('\\\\', '\\').replace('\\"', '"')
            if key is None:
                key = token
            else:
                stack[-1][key.lower()] = token
                key = None
    return root


def in_flatpak():
    return os.path.exists('/.flatpak-info')


def _read_host(path):
    """The file at `path` on the host, through flatpak-spawn (from inside the Flatpak), or None."""
    global _host_failed_at
    if _host_failed_at is not None and time.monotonic() - _host_failed_at < HOST_RETRY_S:
        return None
    try:
        done = subprocess.run(['flatpak-spawn', '--host', 'head', '-c', str(MAX_BYTES), '--', path],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=HOST_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as e:
        _host_failed_at = time.monotonic()
        logging.debug("steam build: flatpak-spawn failed: %s", e)
        return None
    if done.returncode != 0:
        # head's own complaint (its name first, in any language) is a file the host lacks; anything else is
        # flatpak-spawn failing to run it (the portal or the bus refused): not tried again for a while
        if not done.stderr.lstrip().startswith(b'head:'):
            _host_failed_at = time.monotonic()
            logging.debug("steam build: flatpak-spawn failed: %s", done.stderr[:200])
        return None
    return done.stdout.decode('utf-8', 'replace')


def read_text(path, host=None):
    """A small text file, or None: directly, else (inside the Flatpak, or `host`) through the host."""
    try:
        with open(path, 'rb') as f:
            return f.read(MAX_BYTES).decode('utf-8', 'replace')
    except OSError:
        pass
    if host is None:
        host = in_flatpak()
    return _read_host(path) if host else None


def libraries(roots=STEAM_ROOTS, read=read_text):
    """[(library path, set of the app ids its entry in libraryfolders.vdf lists, or None where none is listed)]:
    every library each Steam root's libraryfolders.vdf names, and the root itself, once each."""
    found, index, texts = [], {}, set()

    def add(path, apps):
        key = os.path.realpath(path)
        if key in index:
            i = index[key]
            if found[i][1] is None and apps is not None:
                found[i] = (found[i][0], apps)
            return
        index[key] = len(found)
        found.append((path, apps))
    for root in roots:
        root = os.path.expanduser(root)
        text = read(os.path.join(root, 'steamapps', 'libraryfolders.vdf'))
        if text is None or text in texts:
            continue                # none, or a root seen under another name (a symlink the sandbox cannot resolve)
        texts.add(text)
        for name, entry in (parse_vdf(text).get('libraryfolders') or {}).items():
            if not name.isdigit():
                continue
            if isinstance(entry, str):                    # the old format: "1" "/path"
                add(entry, None)
            elif isinstance(entry, dict) and entry.get('path'):
                apps = entry.get('apps')
                add(entry['path'], {int(a) for a in apps if a.isdigit()} if isinstance(apps, dict) else None)
        add(root, None)
    return found


def manifest(appid, libs, read=read_text):
    """{'appid', 'build', 'updated', 'played', 'path'} of the app's manifest in the first library that holds it, or
    None. A library whose list of apps is known and lacks the app is not looked in."""
    for path, apps in libs:
        if apps is not None and appid not in apps:
            continue
        acf = os.path.join(path, 'steamapps', 'appmanifest_{}.acf'.format(appid))
        text = read(acf)
        if text is None:
            continue
        state = parse_vdf(text).get('appstate') or {}
        build = state.get('buildid')
        if not build or not str(build).isdigit() or build == '0':
            continue

        def number(name):
            value = state.get(name)
            return int(value) if value and str(value).isdigit() and int(value) > 0 else None
        return {'appid': appid, 'build': str(build), 'updated': number('lastupdated'), 'played': number('lastplayed'),
                'path': acf}
    return None


def find(game, roots=STEAM_ROOTS, read=read_text):
    """The manifest (as manifest() gives it) of the Steam app the game key `game` is, or None: of several installed
    apps the key could be, the one last started."""
    appids = APPIDS.get(game or '', ())
    if not appids:
        return None
    libs = libraries(roots, read)
    if not libs:
        return None
    found = [m for m in (manifest(a, libs, read) for a in appids) if m is not None]
    if not found:
        return None
    return max(found, key=lambda m: (m['played'] or 0, -appids.index(m['appid'])))


def lookup(game, now=None):
    """The Steam build (text) of the game key `game` installed now, or None where it is not on Steam or not found;
    cached for CACHE_S."""
    if not APPIDS.get(game or ''):
        return None
    now = time.monotonic() if now is None else now
    with _lock:
        hit = _cache.get(game)
        if hit is not None and now - hit[0] < CACHE_S:
            return hit[1]['build'] if hit[1] else None
        try:
            found = find(game)
        except Exception:                       # never let a strange file stop a session from being written
            logging.exception("steam build: %s", game)
            found = None
        _cache[game] = (now, found)
        if found:
            logging.debug("steam build: %s is app %d build %s", game, found['appid'], found['build'])
        return found['build'] if found else None


def clear_cache():
    global _host_failed_at
    with _lock:
        _cache.clear()
        _host_failed_at = None
