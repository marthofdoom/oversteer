"""Shared loading for the extrapolation prototypes (research only, not
shipped code).

Reads Oversteer captures (read-only) through the app's own reader and
decoder, and returns numpy columns per capture. For the OVST v3 bridge
packets it also unpacks the fields the app's decoder does not take
(wheel slip, wheel load, tyre fx/fy, tyre radius, ride height, surface
grip, brake bias, session type), straight from the packet layout in
data/telemetry/oversteer-shm-bridge.c, so research can judge them.

Nothing here writes outside research/extrapolation/out/, and nothing
writes raw samples anywhere: the scripts save derived numbers and plots.
"""

import glob
import math
import os
import struct
import sys

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)

from oversteer.telemetry_capture import read_capture  # noqa: E402
from oversteer import telemetry_formats as tf  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')
CAPTURE_DIRS = (os.path.expanduser('~/.local/share/oversteer/captures'),
                os.path.expanduser('~/.var/app/io.github.berarma.Oversteer/data/oversteer/captures'))
G = tf.G


def capture_paths():
    paths = []
    for folder in CAPTURE_DIRS:
        paths += glob.glob(os.path.join(folder, '*.ovcap.gz'))
    return sorted(paths, key=os.path.basename)


SCALARS = ('rpm', 'max_rpm', 'gear', 'speed', 'throttle', 'brake', 'clutch', 'handbrake', 'steer', 'yaw_rate',
           'lap_distance', 'distance', 'progress', 'stage_length', 'stage_time', 'game_time', 'idle_rpm', 'gears')
VECTORS = {'pos': 3, 'vel': 3, 'accel': 3, 'forward': 3, 'up': 3, 'wheel_speed': 4, 'wheel_rot': 4, 'susp': 4,
           'susp_vel': 4, 'susp_norm': 4, 'slip_ratio': 4, 'slip_angle': 4}
# OVST v3 fields after the 96-byte v2 head, by index into OVST3_FORMAT's values
OVST3_EXTRA = {'x_wheel_slip': (14, 4), 'x_wheel_load': (26, 4), 'x_ride_height': (30, 2), 'x_tyre_radius': (32, 4),
               'x_susp_max': (36, 4), 'x_fx': (40, 4), 'x_fy': (44, 4), 'x_surface_grip': (52, 1),
               'x_brake_bias': (53, 1), 'x_session': (55, 1), 'x_accg_raw': (5, 3), 'x_ang_raw': (11, 3)}


def _nan(n):
    return [math.nan] * n


def load(path, max_seconds=None):
    """dict of numpy columns for one capture: 't' (capture clock), 'game',
    'track' and 'car' (lists of str/None), the Sample fields in SCALARS
    and VECTORS (NaN where absent), and the OVST v3 extras."""
    meta, records = read_capture(path)
    cols = {k: [] for k in ('t', 'game', 'track', 'car')}
    for k in SCALARS:
        cols[k] = []
    for k in VECTORS:
        cols[k] = []
    for k in OVST3_EXTRA:
        cols[k] = []
    for t, _addr, data in records:
        if max_seconds is not None and t > max_seconds:
            break
        s = tf.decode_sample(data)
        if s is None:
            continue
        cols['t'].append(t)
        cols['game'].append(s.game)
        cols['track'].append(s.track)
        cols['car'].append(s.car_name)
        for k in SCALARS:
            v = getattr(s, k)
            cols[k].append(math.nan if v is None else float(v))
        for k, n in VECTORS.items():
            v = getattr(s, k)
            cols[k].append(_nan(n) if v is None else [float(x) for x in v])
        if len(data) >= tf.OVST3_SIZE and data[:4] == tf.OVST_MAGIC and data[4] >= 3:
            v = struct.unpack_from(tf.OVST3_FORMAT, data, tf.OVST2_SIZE)
            for k, (i, n) in OVST3_EXTRA.items():
                cols[k].append([float(x) for x in v[i:i + n]])
        else:
            for k, (i, n) in OVST3_EXTRA.items():
                cols[k].append(_nan(n))
    out = {'meta': meta, 'path': path}
    for k, v in cols.items():
        if k in ('game', 'track', 'car'):
            out[k] = v
        else:
            out[k] = np.asarray(v, dtype=float)
    return out


def take(cap, mask):
    """The rows of a loaded capture where mask is True."""
    out = {}
    idx = np.flatnonzero(mask)
    for k, v in cap.items():
        if isinstance(v, np.ndarray):
            out[k] = v[idx]
        elif isinstance(v, list):
            out[k] = [v[i] for i in idx]
        else:
            out[k] = v
    return out


def runs(cap, min_length=400.0):
    """Split a capture into runs along the stage: contiguous stretches
    where the distance along the stage moves forward with the car
    driving. A run ends at a backward jump of the stage distance (a
    restart), a change of track, or a gap of more than 2 s. Returns a list
    of row-index arrays, each covering at least `min_length` m."""
    d = cap['lap_distance']
    t = cap['t']
    tracks = cap['track']
    n = len(t)
    out, start = [], None
    for i in range(n):
        ok = math.isfinite(d[i]) and d[i] > 0
        cut = False
        if start is not None:
            if not ok or t[i] - t[i - 1] > 2.0 or d[i] < d[i - 1] - 30.0 or tracks[i] != tracks[i - 1]:
                cut = True
        if cut:
            out.append(np.arange(start, i))
            start = None
        if start is None and ok:
            start = i
    if start is not None:
        out.append(np.arange(start, n))
    kept = []
    for idx in out:
        dd = d[idx]
        if len(idx) > 60 and np.nanmax(dd) - np.nanmin(dd) >= min_length:
            # cut the standing start (before the car moves) and a parked tail
            mv = np.flatnonzero(cap['speed'][idx] > 1.0)
            if len(mv) < 30:
                continue
            idx = idx[mv[0]:mv[-1] + 1]
            kept.append(idx)
    return kept


def smooth(x, n):
    """Centred moving average over n samples, NaN-tolerant at the ends."""
    if n <= 1:
        return x.copy()
    k = np.ones(n) / n
    pad = n // 2
    xp = np.pad(x, (pad, n - 1 - pad), mode='edge')
    return np.convolve(xp, k, mode='valid')


def unwrap_resample(s, x, grid):
    """x(s) resampled on a grid of s (s must be increasing; duplicates
    dropped)."""
    order = np.argsort(s, kind='stable')
    s2, x2 = s[order], x[order]
    keep = np.concatenate(([True], np.diff(s2) > 1e-6))
    return np.interp(grid, s2[keep], x2[keep], left=np.nan, right=np.nan)


def savefig(fig, name):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    fig.savefig(path, dpi=80, bbox_inches='tight')
    return path


CACHE = os.environ.get('EXTRAP_CACHE', os.path.expanduser('~/.cache/oversteer-extrapolation'))


def load_cached(path):
    """load(), cached as .npz outside the repository (the cache holds
    decoded samples, so it never goes into research/ or a commit)."""
    import pickle
    os.makedirs(CACHE, exist_ok=True)
    key = os.path.join(CACHE, os.path.basename(path) + '.%d.pkl' % int(os.path.getmtime(path)))
    if os.path.exists(key):
        with open(key, 'rb') as f:
            return pickle.load(f)
    cap = load(path)
    with open(key, 'wb') as f:
        pickle.dump(cap, f, protocol=4)
    return cap


def all_runs(game=None, min_length=400.0):
    """[(capture name, cap, idx, track, car)] for every run of every
    capture (optionally one game)."""
    found = []
    for p in capture_paths():
        cap = load_cached(p)
        games = [g for g in cap['game'] if g]
        if not games:
            continue
        g = max(set(games), key=games.count)
        if game is not None and g != game:
            continue
        for idx in runs(cap, min_length):
            tr = [x for x in (cap['track'][i] for i in idx) if x]
            ca = [x for x in (cap['car'][i] for i in idx) if x]
            found.append((os.path.basename(p), cap, idx, max(set(tr), key=tr.count) if tr else None,
                          max(set(ca), key=ca.count) if ca else None, g))
    return found
