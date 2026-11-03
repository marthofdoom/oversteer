"""Axis calibration handed to a running proxy without reinstalling it.

The handbrake's travel changes with its firmware, so it is recalibrated
often; reinstalling the service (pkexec, a restart of the combined device)
each time is not an option. The app writes the learnt travel to
`<CALIBRATION_DIR>/<proxy id>.json` and the daemon, which watches the
file, applies it to the matching mappings:

    {"handbrake1:6": {"low": 5600, "high": 59000, "dead": 0.03}}

The directory is writable by every local user (sticky, like /tmp), so the
daemon only takes what cannot do harm: a regular file it opens without
following links, small, numbers only, a travel of at least MIN_SPAN of the
source's range, clamped into that range. The proxy never moves low or high
by itself."""

import json
import math
import os
import stat
import tempfile

CALIBRATION_DIR = '/run/oversteer/calibration'
MAX_BYTES = 4096
MIN_SPAN = 0.05          # of the source's declared range


def path_for(proxy_id, directory=CALIBRATION_DIR):
    return os.path.join(directory, proxy_id + '.json')


def write(proxy_id, entries, directory=CALIBRATION_DIR):
    """entries: {'<source>:<code>': {'low', 'high', 'dead'}}. Atomic; the
    daemon sees a whole file or the previous one."""
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.cal.', dir=directory)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(entries, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path_for(proxy_id, directory))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read(path):
    """The entries in the file, validated, or None when there is nothing
    usable. Never raises on bad content."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            return None
        with os.fdopen(fd, 'rb', closefd=False) as f:
            data = json.loads(f.read(MAX_BYTES + 1).decode('utf-8'))
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    if not isinstance(data, dict):
        return None
    entries = {}
    for key, value in data.items():
        try:
            low, high = float(value['low']), float(value['high'])
            dead = float(value.get('dead', 0.03))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if not all(math.isfinite(x) for x in (low, high, dead)) or not low < high or not 0 <= dead < 1:
            continue
        entries[str(key)] = {'low': low, 'high': high, 'dead': dead}
    return entries


def fit(entry, src_min, src_max):
    """(low, high, dead) clamped into the source's range, or None when the
    travel is too short to be a real one."""
    span = src_max - src_min
    if span <= 0:
        return None
    low = min(src_max, max(src_min, entry['low']))
    high = min(src_max, max(src_min, entry['high']))
    if high - low < MIN_SPAN * span:
        return None
    return low, high, entry['dead']
