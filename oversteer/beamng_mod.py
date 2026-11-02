"""Countersteer's BeamNG.drive mod (data/beamng/countersteer_ffb): builds mods/countersteer_ffb.zip from the
shipped files and puts it in BeamNG's user folder, where a zip in mods/ is active without any further step.

The user folders it looks for, each only where BeamNG has made it (`current` exists; nothing else is created):

- native Linux: `<XDG data home or ~/.local/share>/BeamNG/BeamNG.drive/current/mods`;
- Proton: `<Steam library holding the game>/steamapps/compatdata/284160/pfx/drive_c/users/steamuser/AppData/Local/
  BeamNG.drive/current/mods` (the Windows build's %LOCALAPPDATA% folder; not checked against a Proton install).

The mod's version is the `MOD_VERSION` number in its Lua file; an installed zip with a newer one is never
overwritten (a zip that cannot be read is replaced). In the Flatpak the home folder is not visible: everything
(folders, the installed zip's version, the write) then goes through `flatpak-spawn --host` with argv only (test,
cat, mkdir, tee, mv), as steam_build reads files; when that itself fails the zip can be saved by hand instead.
"""

import io
import logging
import os
import pwd
import re
import subprocess
import sys
import zipfile

from . import steam_build

MOD_NAME = 'countersteer_ffb'
ZIP_NAME = MOD_NAME + '.zip'
LUA_PATH = 'lua/vehicle/extensions/auto/countersteer_ffb.lua'
APPID = 284160                                       # BeamNG.drive on Steam
DATADIR = None                                       # the installed data folder (share/oversteer), set by the application
HOST_TIMEOUT = 10.0                                 # s for one flatpak-spawn call
MAX_ZIP = 1 << 20                                    # bytes read of an installed zip
_VERSION = re.compile(rb'^\s*local\s+MOD_VERSION\s*=\s*(\d+)', re.M)
_ZIP_TIME = (2026, 1, 1, 0, 0, 0)                    # fixed, so the same files always make the same zip
_PROTON_USER = ('pfx/drive_c/users/steamuser/AppData/Local/BeamNG.drive/current',
                'pfx/drive_c/users/steamuser/AppData/Local/BeamNG/BeamNG.drive/current')

# install() statuses
INSTALLED, UPDATED, CURRENT, NEWER, FAILED = 'installed', 'updated', 'current', 'newer', 'failed'


class HostError(OSError):
    """flatpak-spawn --host itself failed (not a file the host lacks)."""


class LocalFs:
    host = False

    @staticmethod
    def isdir(path):
        return os.path.isdir(path)

    @staticmethod
    def read(path):
        """The file's bytes, or None if there is none."""
        try:
            with open(path, 'rb') as f:
                return f.read(MAX_ZIP)
        except OSError:
            return None

    @staticmethod
    def write(path, data):
        tmp = path + '.tmp'
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class HostFs:
    """The host's files through `flatpak-spawn --host`, one program per call and no shell."""
    host = True

    @staticmethod
    def _run(argv, data=None):
        try:
            return subprocess.run(['flatpak-spawn', '--host'] + argv, input=data, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=HOST_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            raise HostError("flatpak-spawn failed: {}".format(e))

    @classmethod
    def _ok(cls, argv, data=None):
        """True/False for a test's answer (exit 0/1); anything else is flatpak-spawn or the program failing."""
        done = cls._run(argv, data)
        if done.returncode not in (0, 1):
            raise HostError("flatpak-spawn --host {}: {}".format(argv[0], done.stderr.decode('utf-8', 'replace')[:200].strip()
                                                                or 'exit {}'.format(done.returncode)))
        return done.returncode == 0

    @classmethod
    def isdir(cls, path):
        return cls._ok(['test', '-d', path])

    @classmethod
    def read(cls, path):
        if not cls._ok(['test', '-f', path]):
            return None
        done = cls._run(['head', '-c', str(MAX_ZIP), '--', path])
        if done.returncode != 0:
            raise HostError("flatpak-spawn --host head: {}".format(done.stderr.decode('utf-8', 'replace')[:200].strip()))
        return done.stdout

    @classmethod
    def write(cls, path, data):
        tmp = path + '.tmp'
        for argv, stdin in ((['mkdir', '-p', '--', os.path.dirname(path)], None), (['tee', '--', tmp], data),
                            (['mv', '-f', '--', tmp, path], None)):
            done = cls._run(argv, stdin)
            if done.returncode != 0:
                if argv[0] != 'mkdir':
                    cls._run(['rm', '-f', '--', tmp])
                raise OSError("{}: {}".format(argv[0], done.stderr.decode('utf-8', 'replace')[:200].strip()
                                              or 'exit {}'.format(done.returncode)))


def find_source(datadir=None):
    """The folder of the mod's files: installed with the data, or in the source tree."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(base, 'beamng', MOD_NAME) for base in (datadir, DATADIR) if base]
    candidates.append(os.path.join(here, '..', 'data', 'beamng', MOD_NAME))
    candidates.append(os.path.join(sys.prefix, 'share', 'oversteer', 'beamng', MOD_NAME))
    for path in candidates:
        if os.path.isfile(os.path.join(path, LUA_PATH)):
            return os.path.normpath(path)
    return None


def lua_version(lua_bytes):
    """The MOD_VERSION in the mod's Lua file, or None."""
    match = _VERSION.search(lua_bytes or b'')
    return int(match.group(1)) if match else None


def source_version(source):
    try:
        with open(os.path.join(source, LUA_PATH), 'rb') as f:
            return lua_version(f.read())
    except OSError:
        return None


def build_zip(source):
    """The mod's zip as bytes: every file under `source`, paths with forward slashes, fixed times."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for folder, dirs, files in os.walk(source):
            dirs.sort()
            for name in sorted(files):
                path = os.path.join(folder, name)
                rel = os.path.relpath(path, source).replace(os.sep, '/')
                info = zipfile.ZipInfo(rel, _ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                with open(path, 'rb') as f:
                    z.writestr(info, f.read())
    return out.getvalue()


def zip_version(path_or_bytes):
    """The MOD_VERSION of an installed (or built) zip: a number, 0 for a valid zip without one, None if it
    is not a readable zip."""
    try:
        with zipfile.ZipFile(io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else path_or_bytes) as z:
            try:
                data = z.read(LUA_PATH)
            except KeyError:
                return 0
        return lua_version(data) or 0
    except (OSError, zipfile.BadZipFile, EOFError):
        return None


def real_home():
    """The user's home folder; inside the Flatpak the passwd entry, not the sandbox's own HOME."""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        return os.path.expanduser('~')


def user_folders(home=None, environ=None, libraries=None, fs=LocalFs):
    """[(label, mods folder)] of the BeamNG user folders that exist: native first, then Proton. `libraries`:
    the Steam libraries (paths) to look in for the Proton prefix; None asks steam_build."""
    home = home or real_home()
    environ = os.environ if environ is None else environ
    found, seen = [], set()

    def add(label, user_folder):
        if fs.isdir(user_folder):
            mods = os.path.join(user_folder, 'mods')
            key = os.path.realpath(mods) if not fs.host else mods
            if key not in seen:
                seen.add(key)
                found.append((label, mods))
    data_homes = [environ.get('XDG_DATA_HOME') or '' if not fs.host else '', os.path.join(home, '.local', 'share')]
    for base in data_homes:
        if base:
            add('native', os.path.join(base, 'BeamNG', 'BeamNG.drive', 'current'))
    if libraries is None:
        roots = tuple(os.path.join(home, r[2:]) if r.startswith('~/') else r for r in steam_build.STEAM_ROOTS)
        try:
            libraries = [path for path, _apps in steam_build.libraries(roots)]
        except Exception:
            logging.debug("beamng mod: no Steam libraries", exc_info=True)
            libraries = list(roots)
    for lib in libraries:
        for rel in _PROTON_USER:
            add('Proton', os.path.join(lib, 'steamapps', 'compatdata', str(APPID), rel))
    return found


def install_one(mods_dir, zip_bytes, version, fs=LocalFs):
    """Write the zip into `mods_dir` unless the one there is as new or newer. Returns (status, detail)."""
    dest = os.path.join(mods_dir, ZIP_NAME)
    status = INSTALLED
    try:
        old = fs.read(dest)
        if old is not None:
            have = zip_version(old)
            if have is not None and have > version:
                return NEWER, have
            if have == version:
                return CURRENT, have
            status = UPDATED
        fs.write(dest, zip_bytes)
    except OSError as e:
        return FAILED, str(e)
    return status, version


def install(datadir=None, home=None, environ=None, libraries=None, host=None):
    """Build the zip and install it in every BeamNG user folder found (through the host inside the Flatpak).
    Returns a dict: `results` [(label, mods folder, status, detail)], `version`, and `error` (a sentence, or
    None) for the cases with nothing to install: the source missing, flatpak-spawn failing, or no BeamNG folder."""
    source = find_source(datadir)
    version = source_version(source) if source else None
    if version is None:
        return {'results': [], 'version': None, 'error': 'The mod files are missing from this installation.'}
    fs = HostFs if (steam_build.in_flatpak() if host is None else host) else LocalFs
    try:
        folders = user_folders(home, environ, libraries, fs)
    except HostError as e:
        return {'results': [], 'version': version, 'error': "Can't reach the computer outside the Flatpak ({}). "
                "Save the zip and copy it to BeamNG's mods folder yourself.".format(e)}
    if not folders:
        return {'results': [], 'version': version, 'error': "BeamNG.drive's user folder was not found. Start the game "
                "once so it creates it, or save the zip and copy it to the mods folder yourself."}
    data = build_zip(source)
    results = [(label, mods) + install_one(mods, data, version, fs) for label, mods in folders]
    return {'results': results, 'version': version, 'error': None}


def summary(report):
    """One sentence for the install's outcome, to show the user."""
    if report['error']:
        return report['error']
    parts = []
    for label, mods, status, detail in report['results']:
        if status in (INSTALLED, UPDATED):
            parts.append("{} ({}): {} version {} in {}".format(label, 'new' if status == INSTALLED else 'updated',
                                                                 ZIP_NAME, report['version'], mods))
        elif status == CURRENT:
            parts.append("{}: already up to date (version {}) in {}".format(label, detail, mods))
        elif status == NEWER:
            parts.append("{}: kept the newer version {} already in {}".format(label, detail, mods))
        else:
            parts.append("{}: could not write to {}: {}".format(label, mods, detail))
    return '\n'.join(parts)
