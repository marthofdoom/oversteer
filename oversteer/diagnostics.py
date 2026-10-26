"""The diagnostics log and the diagnostics bundle.

A rotating log file (5 files of 2 MB) in the app's data folder:
`<data>/oversteer/logs/oversteer.log`, where <data> is $XDG_DATA_HOME or
~/.local/share (in the Flatpak, ~/.var/app/io.github.berarma.Oversteer/data).
INFO by default, DEBUG with --debug or the Settings toggle. Lines read

    2026-10-26 12:00:00,123 INFO [telemetry-listener] drive_log: run 3 start: ...

The bundle (`make_bundle`, Settings -> Diagnostics, `--diagnostics-bundle`) is
a zip of the logs, the last few captures with their sidecars, the versions
and the config (the pairing code blanked); never the database.
"""

import configparser
import glob
import logging
import logging.handlers
import os
import platform
import re
import sys
import threading
import time
import zipfile

APP_ID = 'io.github.berarma.Oversteer'
LOG_NAME = 'oversteer.log'
LOG_BYTES = 2 * 1024 * 1024
LOG_FILES = 5                       # the live file and four rotated ones
LOG_FORMAT = '%(asctime)s %(levelname)s [%(threadName)s] %(module)s: %(message)s'
BUNDLE_CAPTURES = 3
SECRET_KEYS = ('web_token',)
PAIR_QUERY = re.compile(rb'([?&]pair=)[^&\s]+')

_file_handler = None
_stream_handler = None


def data_home(environ=None, home=None):
    """The user's data folder: $XDG_DATA_HOME, else ~/.local/share; inside the
    Flatpak (FLATPAK_ID set) ~/.var/app/<id>/data when the sandbox sets none."""
    environ = os.environ if environ is None else environ
    home = home or os.path.expanduser('~')
    if environ.get('XDG_DATA_HOME'):
        return environ['XDG_DATA_HOME']
    if environ.get('FLATPAK_ID'):
        return os.path.join(home, '.var', 'app', environ['FLATPAK_ID'], 'data')
    return os.path.join(home, '.local', 'share')


def log_dir(environ=None, home=None):
    return os.path.join(data_home(environ, home), 'oversteer', 'logs')


def log_path(environ=None, home=None):
    return os.path.join(log_dir(environ, home), LOG_NAME)


def setup(debug=False, folder=None, stream=sys.stderr, to_file=True):
    """Configure logging once: the rotating file (INFO, DEBUG with `debug`) and
    a stream handler (warnings, everything with `debug`). Returns the file's
    path, or None when no log file could be made (the stream still works)."""
    global _file_handler, _stream_handler
    root = logging.getLogger()
    for handler in (_file_handler, _stream_handler):
        if handler is not None:
            root.removeHandler(handler)
            handler.close()
    _file_handler = _stream_handler = None
    formatter = logging.Formatter(LOG_FORMAT)
    if stream is not None:
        _stream_handler = logging.StreamHandler(stream)
        _stream_handler.setLevel(logging.DEBUG if debug else logging.WARNING)
        _stream_handler.setFormatter(formatter)
        root.addHandler(_stream_handler)
    path = None
    if not to_file:
        return None
    try:
        folder = folder or log_dir()
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, LOG_NAME)
        _file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=LOG_BYTES, backupCount=LOG_FILES - 1, encoding='utf-8')
        _file_handler.setLevel(logging.DEBUG if debug else logging.INFO)
        _file_handler.setFormatter(formatter)
        root.addHandler(_file_handler)
    except OSError as e:
        path = None
        logging.warning("diagnostics log: %s", e)
    _root_level()
    return path


def _root_level():
    """The root passes only what some handler wants, so logging.debug() calls
    stay cheap while the file is at INFO and the stream at WARNING."""
    levels = [h.level for h in (_file_handler, _stream_handler) if h is not None]
    logging.getLogger().setLevel(min(levels) if levels else logging.WARNING)


def set_debug(on):
    """Switch the file's level while running (the Settings toggle)."""
    if _file_handler is not None:
        _file_handler.setLevel(logging.DEBUG if on else logging.INFO)
    _root_level()
    logging.info("diagnostics: debug logging %s", 'on' if on else 'off')


def install_excepthooks():
    """Uncaught exceptions to the log, with their tracebacks: the main thread,
    other threads, unraisable ones in destructors. (PyGObject reports an
    exception in a GLib callback, an idle or a signal handler, through
    sys.excepthook too.) The previous hooks still run."""
    previous = sys.excepthook
    previous_thread = threading.excepthook
    previous_unraisable = sys.unraisablehook

    def hook(kind, value, trace):
        if issubclass(kind, KeyboardInterrupt):
            previous(kind, value, trace)
            return
        logging.getLogger().error("uncaught exception", exc_info=(kind, value, trace))
        previous(kind, value, trace)

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else '?'
        logging.getLogger().error("uncaught exception in thread %s", name,
                                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        previous_thread(args)

    def unraisable(args):
        logging.getLogger().error("unraisable exception: %s", args.err_msg or '',
                                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        previous_unraisable(args)

    sys.excepthook = hook
    threading.excepthook = thread_hook
    sys.unraisablehook = unraisable


def read_debug_preference(config_file):
    """The Settings toggle as config.ini holds it (read before the GUI exists)."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(config_file)
        return parser['DEFAULT'].get('diagnostics_debug') == '1'
    except (configparser.Error, OSError):
        return False


# -- the bundle --

def log_files(folder=None):
    return sorted(glob.glob(os.path.join(glob.escape(folder or log_dir()), LOG_NAME + '*')))


def _driver_versions():
    out = {}
    for pattern in ('hid_logitech*', '*lg4ff*', 'hid_thrustmaster*', 'hid_fanatec*', 'hid_pidff*', 'hid_universal_pidff*'):
        for path in glob.glob('/sys/module/' + pattern):
            values = []
            for name in ('version', 'srcversion'):
                try:
                    with open(os.path.join(path, name)) as f:
                        values.append(f.read().strip())
                except OSError:
                    pass
            out[os.path.basename(path)] = ' '.join(values) or 'loaded'
    return out


def _games_of(captures):
    from .telemetry_capture import read_capture
    from .telemetry_formats import decode_sample
    games = {}
    for path in captures:
        try:
            _meta, records = read_capture(path)
            for _t, _addr, data in records:
                sample = decode_sample(data)
                if sample is not None:
                    games[os.path.basename(path)] = sample.game or 'telemetry'
                    break
        except (ValueError, OSError):
            continue
    return games


def versions_text(version, captures=()):
    lines = ['Oversteer: {}'.format(version), 'Python: {}'.format(sys.version.split()[0]),
             'Platform: {}'.format(platform.platform()), 'Kernel: {}'.format(platform.release()),
             'Flatpak: {}'.format(os.environ.get('FLATPAK_ID') or 'no')]
    try:
        import evdev
        lines.append('python-evdev: {}'.format(evdev.__version__))
    except Exception:
        pass
    lines.append('Wheel drivers:')
    drivers = _driver_versions()
    lines += ['  {}: {}'.format(k, v) for k, v in sorted(drivers.items())] or ['  none found']
    lines.append('Games in the captures:')
    games = _games_of(captures)
    lines += ['  {}: {}'.format(k, v) for k, v in sorted(games.items())] or ['  none']
    return '\n'.join(lines) + '\n'


def redacted_config(config_file):
    """config.ini's text without the pairing code. '' if there is none."""
    try:
        with open(config_file, encoding='utf-8') as f:
            lines = f.read().splitlines()
    except OSError:
        return ''
    out = []
    for line in lines:
        key = line.split('=', 1)[0].strip()
        out.append(key + ' = (removed)' if key in SECRET_KEYS and '=' in line else line)
    return '\n'.join(out) + '\n'


def config_secrets(config_file):
    """The secret values config.ini holds (the pairing code)."""
    try:
        with open(config_file, encoding='utf-8') as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    values = (line.split('=', 1)[1].strip() for line in lines if '=' in line and line.split('=', 1)[0].strip() in SECRET_KEYS)
    return [v for v in values if v]


def redacted_log(data, secrets=()):
    """A log's bytes without the pairing code or any ?pair= value."""
    for secret in secrets:
        data = data.replace(secret.encode('utf-8'), b'(removed)')
    return PAIR_QUERY.sub(rb'\1(removed)', data)


def bundle_members(capture_folder, count=BUNDLE_CAPTURES, logs=None):
    """[(path, name in the zip)]: the logs and the newest `count` captures
    with their sidecars (.json labels, .wheel.gz wheel inputs)."""
    from .telemetry_capture import list_captures, sidecars
    members = [(p, 'logs/' + os.path.basename(p)) for p in (log_files() if logs is None else logs)]
    for path, _size, _labelled in list_captures(capture_folder)[-count:] if count else []:
        for p in [path] + sidecars(path):
            members.append((p, 'captures/' + os.path.basename(p)))
    return members


def bundle_size(capture_folder, count=BUNDLE_CAPTURES, logs=None):
    """Bytes the bundle's files add up to (a zip of it is a little smaller)."""
    total = 0
    for path, _name in bundle_members(capture_folder, count, logs):
        try:
            total += os.path.getsize(path)
        except OSError:
            pass
    return total


def make_bundle(path, version, capture_folder, config_file=None, count=BUNDLE_CAPTURES, logs=None):
    """Write the diagnostics zip to `path`. Returns the number of files in it."""
    members = bundle_members(capture_folder, count, logs)
    captures = [p for p, name in members if name.endswith('.ovcap.gz')]
    secrets = config_secrets(config_file) if config_file else []
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('versions.txt', versions_text(version, captures))
        if config_file:
            z.writestr('config.ini', redacted_config(config_file))
        for source, name in members:
            try:
                if name.startswith('logs/'):
                    with open(source, 'rb') as f:
                        z.writestr(name, redacted_log(f.read(), secrets))
                else:
                    z.write(source, name)
            except OSError as e:
                z.writestr(name + '.error', str(e))
    logging.info("diagnostics: bundle written to %s (%d files)", path, len(members) + 2)
    return len(members) + 2


def human_size(n):
    for unit in ('bytes', 'KB', 'MB'):
        if n < 1024 or unit == 'MB':
            return '{} {}'.format(n, unit) if unit == 'bytes' else '{:.1f} {}'.format(n, unit)
        n /= 1024.0


def default_bundle_name():
    return 'oversteer-diagnostics-{}.zip'.format(time.strftime('%Y%m%d-%H%M%S'))
