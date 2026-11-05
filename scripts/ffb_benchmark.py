"""FFB benchmark: the force a reference game's drives call for, per condition, to tune any game's derived force against
(scripts/ffb-benchmark.py is the CLI).

THE TARGET IS "WHAT ACR WOULD DO": the owner's stated reference. Physics may overrule it, and a gap is a question, not a
verdict.

build: every usable reference-game (ACR) drive is replayed from its telemetry through TODAY's RackForce (oversteer/ffb_engine.py,
as the app uses it for ACR) at one fixed mapping (MAPPING below). The game's own FFB force is not the target. Each moving
sample gets a cell (normalised lateral load x body slip x speed x pedal); a cell pools the drives of a car class with a
weight cap per drive, keeps the spread within the cell and across cars, and has a basis: measured, interpolated or
extrapolated (a physics-shaped fit of the effective trail against slip, never presented as measured), or no entry at all
when it has too little data. Rates (band energy, time near zero under load, falls per minute, share of time opposing
Fy_f) are kept per single condition, where a cell is too narrow for them.

compare: the same quantities from a drive of any game, scored against the pool of its class. Numbers are R (recorded,
read from the capture) or E (estimated: needs an assumption, named where it is used).

Conventions as scripts/ffb_parity.py: force positive to the left as the engine writes it; Newton's front lateral force
Fy_f (ffb_forza.front_lateral) is positive left, so a restoring force has the sign opposite Fy_f. Every quantity here is
the RESTORING part (positive: toward centre; negative: against Fy_f).
"""
import contextlib
import hashlib
import json
import logging
import math
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import ffb_parity as fp  # noqa: E402

np, sg, MISSING = fp.np, fp.sg, fp.MISSING

VERSION = 1
CACHE = os.path.join(os.path.expanduser('~'), '.cache', 'ffb-benchmark')
BENCHMARK = os.path.join(CACHE, 'benchmark.json')
WATCHED = ('oversteer/ffb_engine.py', 'oversteer/ffb_carprops.py', 'oversteer/wheel_torque.py')
FULL = fp.FULL
G = 9.81
PEAK_NM = 3.5                                    # the fixed mapping's wheel peak (the G29 class)
MOVE = 5.0                                       # m/s: a moving sample
FY_MIN = 500.0                                   # N: under this |Fy_f| a ratio to it means nothing
MIN_MOVING_S = 60.0                              # s of moving, valid data a reference drive needs
CAP_S = 60.0                                     # s: the most one drive weighs in one cell
MIN_CELL_S, MIN_CELL_DRIVES = 3.0, 2             # a measured cell: this much data from this many drives
MIN_SPARSE_S, MIN_SPARSE_N = 0.5, 20             # under this a cell has nothing (stays empty); between: a fit may fill it
MIN_FIT_CELLS, MIN_FIT_SLIPS = 4, 3              # measured cells and distinct slip bands a fit needs
MIN_COMPARE_S = 0.5                              # s of the drive in a cell before it is scored
MIN_COMPARE_N = 30
MIN_RATE_S = fp.MIN_S                            # s of a condition a rate needs
Z_CLIP = 6.0

LAT_EDGES = (0.0, 0.1, 0.25, 0.4, 0.55, 0.7, 0.9, math.inf)       # |Fy_f| / (m (b/L) g)
SLIP_EDGES = (0.0, 0.05, 0.1, 0.2, 0.4, 0.7, math.inf)           # body slip beta, rad
SPEED_EDGES = (0.0, 15.0, 25.0, 35.0, math.inf)                  # m/s
PEDALS = ('on', 'off', 'brake')
BANDS = ((0.5, 3.0), (3.0, 15.0), (15.0, 30.0), (30.0, 60.0))
QUANTITIES = ('torque_nm', 'share', 'trail_m', 'nm_per_kn')
SCORED = ('torque_nm', 'share', 'trail_m')
UNITS = {'torque_nm': 'Nm', 'share': 'FS', 'trail_m': 'm', 'nm_per_kn': 'Nm/kN'}
SPREAD_FLOOR = {'torque_nm': 0.05, 'share': 0.02, 'trail_m': 0.003, 'nm_per_kn': 0.3}
RATE_FLOOR = {'band': 0.01, 'zero_load': 0.02, 'oppose': 0.02, 'sudden_per_min': 5.0, 'sustained_per_min': 3.0}
BASIS_WEIGHT = {'measured': 1.0, 'interpolated': 0.5, 'extrapolated': 0.25}
NOZERO_LOAD = (0.02, 0.3)                        # |share| under 0.02 FS while lat load >= 0.3: "near zero under load"
SPREAD_K = 2.563                                 # p90 - p10 of a normal, in sigma

MAPPING = {'wheel_peak_nm': PEAK_NM, 'note': "one mapping for every reference drive: RackForce at its class defaults but the "
           "peak (knee, rim 279 mm against the car's 350 mm, power class, strength 100 %, Drive pull and Kickback on, no aligning "
           "spring); the assist is our model's (the game-shaped assist needs the game's raw force, which most drives lack)"}


# ---------------------------------------------------------------------------------------------------- bins

def lat_label(i):
    lo, hi = LAT_EDGES[i], LAT_EDGES[i + 1]
    return "lat%g-%g" % (lo, hi) if hi < 9 else "lat>%g" % lo


def slip_label(i):
    lo, hi = SLIP_EDGES[i], SLIP_EDGES[i + 1]
    return "slip%g-%g" % (lo, hi) if hi < 9 else "slip>%g" % lo


def speed_label(i):
    lo, hi = SPEED_EDGES[i], SPEED_EDGES[i + 1]
    return "v%g-%g" % (lo, hi) if hi < 900 else "v>%g" % lo


N_LAT, N_SLIP, N_SPEED, N_PEDAL = len(LAT_EDGES) - 1, len(SLIP_EDGES) - 1, len(SPEED_EDGES) - 1, len(PEDALS)
N_CELLS = N_LAT * N_SLIP * N_SPEED * N_PEDAL


def _digit(x, edges):
    return np.clip(np.digitize(x, edges[1:-1]), 0, len(edges) - 2)


def pedal_index(throttle, brake):
    """0 throttle on, 1 off, 2 braking (brake wins)."""
    t, b = np.nan_to_num(np.asarray(throttle, float)), np.nan_to_num(np.asarray(brake, float))
    return np.where(b > 0.1, 2, np.where(t > 0.1, 0, 1))


def cell_ids(lat, beta, speed, throttle, brake):
    """Integer cell id per sample: ((lat * N_SLIP + slip) * N_SPEED + speed) * N_PEDAL + pedal."""
    a = _digit(np.asarray(lat, float), LAT_EDGES)
    b = _digit(np.asarray(beta, float), SLIP_EDGES)
    c = _digit(np.asarray(speed, float), SPEED_EDGES)
    d = pedal_index(throttle, brake)
    return ((a * N_SLIP + b) * N_SPEED + c) * N_PEDAL + d


def cell_parts(cid):
    cid = int(cid)
    pedal = cid % N_PEDAL
    cid //= N_PEDAL
    speed = cid % N_SPEED
    cid //= N_SPEED
    return cid // N_SLIP, cid % N_SLIP, speed, pedal


def cell_key(cid):
    lat, slip, speed, pedal = cell_parts(cid)
    return "%s|%s|%s|%s" % (lat_label(lat), slip_label(slip), speed_label(speed), PEDALS[pedal])


def condition_masks(lat, beta, speed, throttle, brake):
    """{condition name: bool mask}: each bin of each dimension alone (a cell is too narrow for a rate)."""
    a, b = _digit(np.asarray(lat, float), LAT_EDGES), _digit(np.asarray(beta, float), SLIP_EDGES)
    c, d = _digit(np.asarray(speed, float), SPEED_EDGES), pedal_index(throttle, brake)
    out = {'all': np.ones(len(a), bool)}
    for i in range(N_LAT):
        out[lat_label(i)] = a == i
    for i in range(N_SLIP):
        out[slip_label(i)] = b == i
    for i in range(N_SPEED):
        out[speed_label(i)] = c == i
    for i, name in enumerate(PEDALS):
        out['pedal-' + name] = d == i
    return out


# ---------------------------------------------------------------------------------------------------- statistics

def wquant(v, w, qs):
    """Weighted quantiles of `v` (weights `w`) at `qs` (0..1)."""
    v, w = np.asarray(v, float), np.asarray(w, float)
    o = np.argsort(v, kind='stable')
    v, w = v[o], w[o]
    c = np.cumsum(w)
    if c[-1] <= 0:
        return [float(np.median(v))] * len(qs)
    return [float(x) for x in np.interp(np.asarray(qs), (c - 0.5 * w) / c[-1], v)]


def sigma(p10, p90):
    return (p90 - p10) / SPREAD_K


def _round(x):
    return None if x is None else float('%.5g' % x)


# ---------------------------------------------------------------------------------------------------- classes and provenance

def car_class(key):
    """The pooling class of a car key by its table name: rally2 (Rally2, R5), rally4, groupb, wrc, kitcar, historic, other."""
    from oversteer import car_data
    e = car_data.entry(key) if key else None
    name = ((e or {}).get('screen_name') or (e or {}).get('name') or (key or '').split('/', 1)[-1]).lower().replace('_', ' ')
    if 'rally2' in name or ' r5' in name or name.endswith('r5'):
        return 'rally2'
    if 'rally4' in name:
        return 'rally4'
    if any(s in name for s in ('quattro', '037', 'group b', ' s4', 'rs200', 'delta s4', '205 t16', 'metro 6r4')):
        return 'groupb'
    if 'wrc' in name or 'impreza' in name:
        return 'wrc'
    if 'kit' in name or 'maxi' in name:
        return 'kitcar'
    if key and key.startswith('acr/'):
        return 'historic'
    return 'other'


def configure_store():
    """Point the car store at the Flatpak's file when there is one (read only; the writes are patched out in `quiet`)."""
    from oversteer import ffb_carprops
    flat = os.path.join(os.path.expanduser('~'), '.var', 'app', 'io.github.berarma.Oversteer', 'data', 'oversteer',
                        ffb_carprops.FILE)
    if os.path.isfile(flat):
        ffb_carprops.STORE.use(flat)


@contextlib.contextmanager
def quiet():
    """The replay may not touch the real car store or log: the store's writers are no-ops while it runs."""
    from oversteer import ffb_carprops
    S = ffb_carprops.Store
    names = ('_write', 'put', 'set_rack', 'set_lock', 'set_frame', 'set_mass')
    saved = {n: getattr(S, n) for n in names if hasattr(S, n)}
    for n in saved:
        setattr(S, n, lambda *a, **k: None)
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        for n, f in saved.items():
            setattr(S, n, f)
        logging.disable(logging.NOTSET)


def provenance(car, entry, header=None):
    """{'ratio': (value, source), 'mass': (kg, source), 'trail': (m, source)}: where each number the replay used came from.
    A header's own ratio (the drive's recording) first, then the car's learnt values in the store, then the car table, then
    the default (assumed)."""
    from oversteer import ffb_carprops
    out = {}
    learnt = ((header or {}).get('settings') or {}).get('learnt') or {}
    rack = ffb_carprops.STORE.get_rack(car) if car else None
    table = ffb_carprops.table_ratio(entry)
    if isinstance(learnt.get('ratio'), (int, float)) and learnt.get('car') == car and car:
        out['ratio'] = (float(learnt['ratio']), 'recorded')
    elif rack:
        out['ratio'] = (rack[0], 'learnt')
    elif table:
        out['ratio'] = (table, 'tabled')
    else:
        out['ratio'] = (ffb_carprops.STEER_RATIO, 'assumed')
    if entry and entry.get('mass_kg'):
        out['mass'] = (float(entry['mass_kg']), 'tabled')
    elif car and ffb_carprops.STORE.get_mass(car):
        out['mass'] = (ffb_carprops.STORE.get_mass(car), 'learnt')
    else:
        out['mass'] = (1390.0, 'assumed')
    kept = ffb_carprops.STORE.get(car) if car else None
    out['trail'] = (kept[0], 'learnt') if kept else (ffb_carprops.default_trail(0.027), 'assumed')
    return out


def code_hashes():
    """{file: git blob hash of the working copy} of the files whose change makes a benchmark stale."""
    out = {}
    for rel in WATCHED:
        path = os.path.join(ROOT, rel)
        try:
            out[rel] = subprocess.run(['git', 'hash-object', path], capture_output=True, text=True, check=True,
                                      cwd=ROOT).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            with open(path, 'rb') as f:
                out[rel] = hashlib.sha1(f.read()).hexdigest()
    return out


def git_head():
    try:
        return subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True, check=True,
                              cwd=ROOT).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


# ---------------------------------------------------------------------------------------------------- reading a drive

class Extract:
    """What a drive contributes: per-sample arrays (`rows`) for the cells and per-condition rates (`rates`)."""
    ok = False
    why = ''


def _levels(path):
    """[(t, level)] of the sidecar's game_ffb_raw, and (ext_force (t, value) array or None, header, playing writes)."""
    from oversteer import ffb_record
    raw, ext, header, align, wheel_range = [], [], {}, [], None
    side = path + ffb_record.SUFFIX
    if os.path.exists(side):
        try:
            header, frames = ffb_record.load(side)
            for t, name, v in list(frames):
                if name == 'game_ffb_raw':
                    raw.append((t, v[0]))
                elif name == 'ext_force':
                    ext.append((t, v[0]))
                elif name == 'ffb_align':
                    align.append((t, v[0], v[1], v[2]))
                elif name == 'ffb_align_read':
                    wheel_range = v[1]
        except (OSError, ValueError):
            header, raw, ext = {}, [], []
    played = np.array(ext, float) if len(ext) >= 50 else None
    if played is not None and align and wheel_range:
        played = _with_spring(path, played, np.array(align, float), wheel_range)
    return raw, played, header


def _with_spring(path, played, align, wheel_range):
    """ext_force is the residual: the played force is it plus the driver's aligning spring (target, k uFS/deg, wheel position)."""
    from oversteer import telemetry_capture as tc
    side = path + tc.WHEEL_SUFFIX
    if not os.path.exists(side):
        return played
    _meta, records = tc.read_wheel(side)
    pos = np.array([(t, v) for t, kind, code, v in records if kind == 3 and code == 0], float)
    if len(pos) < 50:
        return played
    t, half = played[:, 0], wheel_range / 2.0
    theta = -(np.interp(t, pos[:, 0], pos[:, 1]) - 32767.5) / 32767.5 * half
    target = -np.interp(t, align[:, 0], align[:, 1]) / 32767.0 * half
    k = np.interp(t, align[:, 0], align[:, 2]) * 1e-6
    live = np.abs(t - np.interp(t, align[:, 0], align[:, 0])) < 0.2
    out = played.copy()
    out[:, 1] += FULL * np.clip(k * (target - theta), -0.8, 0.8) * live
    return out


def _walk(path):
    """(meta, generator of (t, Sample)): a capture's decodable samples in order."""
    from oversteer import telemetry_capture as tc, telemetry_formats as tf
    meta, recs = tc.read_capture(path)

    def gen():
        for t, _a, data in recs:
            try:
                s = tf.decode_sample(data)
            except Exception:
                continue
            if s is not None:
                yield t, s
    return meta, gen()


def _f(v):
    return float(v) if v is not None and math.isfinite(v) else math.nan


def read_drive(path, replay=None):
    """Decode a capture into arrays. `replay`: 'acr' (RackForce over the telemetry's tyre forces: torque and share from
    today's chain), 'rbr' (RbrRack over the sidecar's raw levels: the recovered column torque), or None (no chain run).
    Returns an Extract-like object `d` with .ok / .why and, when ok, the arrays t speed thr brk steer yaw ay vx vy torque
    share (nan where not replayed), the car, game, drivetrain, entry and the provenance."""
    from oversteer import car_data
    d = Extract()
    d.path = path
    d.name = os.path.basename(path)[:-len('.ovcap.gz')] if path.endswith('.ovcap.gz') else os.path.basename(path)
    raw, ext, header = _levels(path)
    d.header, d.ext = header, ext
    meta, gen = _walk(path)
    first = None
    rows = []
    rack = tex = None
    mz_n = seen = 0
    last_car = None
    ri = 0
    car = game = drivetrain = None
    with quiet():
        for t, s in gen:
            last_car = s.car or last_car
            car = car or s.car
            game = game or getattr(s, 'game', None)
            drivetrain = getattr(s, 'drivetrain', None) or drivetrain
            torque = share = math.nan
            seen += 1
            if replay == 'acr' and seen == 300 and mz_n == 0:
                break                            # an older recording: no front Mz in the packets, nothing to replay
            if replay == 'acr':
                if s.car != car or s.tyre_mz is None or s.tyre_fy is None:
                    continue
                if rack is None:
                    rack = _acr_rack(car)
                rack.feed(t, s, None, False)
                f = rack.force(s)
                share = f / FULL
                mz = rack._front(s, rack.trail, rack.pull_scrub())       # the kingpin torque the chain starts from
                torque = mz / rack.ratio if mz is not None else math.nan       # the column torque, before the assist
                mz_n += 1
            elif replay == 'rbr':
                ev = []
                while ri < len(raw) and raw[ri][0] <= t:
                    ev.append(raw[ri])
                    ri += 1
                if rack is None:
                    rack = _rbr_rack(header)
                strength = (((header or {}).get('settings') or {}).get('learnt') or {}).get('rbr_strength')
                s_val = (car_data.entry(s.car) or {}).get('ffb_sensitivity')
                for rt, lv in ev:
                    rack.set_level(rt, lv, s_val, strength if isinstance(strength, (int, float)) else 1.0)
                rack.set_car(s.car)
                rack.feed(t, s, ev[-1][1] if ev else None, bool(ev))
                if rack.column is not None:
                    torque = rack.column + rack.floor_lift             # the column torque (with the caster floor's lift)
            acc, vel = s.accel, s.vel
            rows.append((t, _f(s.speed), _f(s.throttle), _f(getattr(s, 'brake', None)), _f(s.steer), _f(s.yaw_rate),
                         _f(acc[1]) if acc is not None and len(acc) > 1 else math.nan,
                         _f(vel[0]) if vel is not None and len(vel) > 0 else math.nan,
                         _f(vel[1]) if vel is not None and len(vel) > 1 else math.nan, torque, share))
    if len(rows) < 100:
        d.car = car
        d.why = ("no sample carries front Mz and Fy (an older recording: today's RackForce cannot run)" if replay == 'acr'
                 else "fewer than 100 decodable packets")
        return d
    a = np.array(rows, float)
    a = a[np.isfinite(a[:, 0])]
    a = a[np.argsort(a[:, 0], kind='stable')]
    (d.t, d.speed, d.thr, d.brk, d.steer, d.yaw, d.ay, d.vx, d.vy, d.torque, d.share) = (a[:, i] for i in range(11))
    d.car, d.game = (last_car if replay != 'acr' else car), game
    d.entry = car_data.entry(car) if car else None
    d.drivetrain = drivetrain or (d.entry or {}).get('drivetrain') or 'awd'
    d.prov = provenance(car, d.entry, header if replay is None or replay == 'rbr' else None)
    if replay == 'rbr' and rack is not None:
        d.prov['ratio'] = (rack.ratio, 'tabled' if getattr(rack, 'ratio_known', False) else 'assumed')
    d.replay = replay
    d.ok = True
    return d


def _acr_rack(car):
    from oversteer import ffb_carprops, ffb_engine as fe

    class Rack(fe.RackForce):
        LEARNS = False                           # fixed trail and ratio: the replay does not depend on the drive order
    r = Rack()
    r.wheel_peak = PEAK_NM
    r.reset(car)
    kept = ffb_carprops.STORE.get(car) if car else None
    if kept:
        r.trail, r.scrub = kept
    return r


def _rbr_rack(header):
    from oversteer import ffb_engine as fe
    r = fe.RbrRack()
    r.wheel_peak = PEAK_NM
    r.reset(None)
    r.ready = True
    prof = ((header or {}).get('settings') or {}).get('profile') or {}
    if isinstance(prof.get('rbr_cut_hz'), (int, float)):
        r.cut_hz = float(prof['rbr_cut_hz'])
    if isinstance(prof.get('caster_floor'), (int, float)):
        r.floor_level = float(prof['caster_floor']) / 100.0
    return r


# ---------------------------------------------------------------------------------------------------- per-drive quantities

def derive(d, view='replay'):
    """Add Newton's Fy_f, the bins and the quantities to a read drive; view: 'replay' (torque and share from the chain run
    in read_drive), 'played' (the recorded ext_force as the share, torque from it: share x peak / rho, E) or 'column'
    (recorded share, torque from RbrRack's recovered column). Sets d.valid and the per-sample arrays."""
    from oversteer import ffb_carprops, ffb_forza, wheel_torque
    n = len(d.t)
    dt = np.diff(d.t)
    d.fs = 1.0 / float(np.median(dt[dt > 0])) if (dt > 0).any() else 0.0
    d.dt = np.minimum(np.maximum(np.diff(d.t, prepend=d.t[0]), 0.0), 0.05)
    mass, mass_src = d.prov['mass']
    bl = ffb_forza.front_share(d.drivetrain)
    yaw = fp.lowpass(np.nan_to_num(d.yaw), d.fs, 20.0)
    rdot = np.gradient(yaw, d.t) if n > 2 else np.zeros(n)
    ay20 = fp.lowpass(np.nan_to_num(d.ay), d.fs, 20.0)
    d.fyf = np.array([ffb_forza.front_lateral(mass, bl, x, y) for x, y in zip(ay20, rdot)])
    d.lat = np.abs(d.fyf) / (mass * bl * G)
    d.beta = np.abs(np.arctan2(d.vy, np.abs(d.vx)))
    d.playing = np.ones(n, bool)
    d.native = None
    if view in ('played', 'column'):
        ef = d.ext
        if ef is None:
            raise ValueError("no recorded force (ext_force) in the sidecar: the 'played' view needs it")
        j = np.clip(np.searchsorted(ef[:, 0], d.t), 1, len(ef) - 1)
        near = np.minimum(np.abs(ef[j, 0] - d.t), np.abs(ef[j - 1, 0] - d.t))
        d.playing = near < fp.PLAY_GAP
        d.share = np.interp(d.t, ef[:, 0], ef[:, 1]) / FULL
        d.native = (ef[:, 0], ef[:, 1] / FULL)
        prof = ((d.header or {}).get('settings') or {}).get('profile') or {}
        peak = prof.get('wheel_peak') if isinstance(prof.get('wheel_peak'), (int, float)) else PEAK_NM
        d.peak_source = 'recorded' if isinstance(prof.get('wheel_peak'), (int, float)) else 'assumed'
        if view == 'played':
            rho = wheel_torque.DEFAULT_RIM_MM / wheel_torque.CAR_WHEEL_MM
            drv = d.share * peak / rho                       # the driver's torque, taken as played 1:1 (below the knee)
            col = np.full(n, math.nan)
            for i in np.flatnonzero(np.isfinite(drv) & (d.speed > MOVE)):
                try:
                    col[i] = ffb_carprops.column_from_driver(float(drv[i]), float(d.speed[i]), 1.0)
                except (ValueError, OverflowError, ZeroDivisionError):
                    pass
            d.torque = col                                   # (E) the column torque that driver torque balances, rally assist class
    d.view = view
    d.valid = (d.playing & (d.speed > MOVE) & np.isfinite(d.ay) & np.isfinite(d.beta) & np.isfinite(d.share)
               & np.isfinite(d.torque))
    d.moving_s = float(d.dt[d.valid].sum())
    d.cid = cell_ids(d.lat, d.beta, d.speed, d.thr, d.brk)
    ratio = d.prov['ratio'][0]
    sgn = -np.sign(d.fyf)
    d.q_ok = d.valid & (np.abs(d.fyf) >= FY_MIN)
    fy = np.maximum(np.abs(d.fyf), 1.0)
    d.q = {'torque_nm': d.torque * sgn, 'share': d.share * sgn}
    d.q['trail_m'] = d.q['torque_nm'] * ratio / fy
    d.q['nm_per_kn'] = d.q['torque_nm'] / (fy / 1000.0)
    return d


def sample_rows(d):
    """The arrays a pool needs from a derived drive (valid, |Fy_f| large enough samples only)."""
    m = d.q_ok
    return {'cell': d.cid[m], 'dt': d.dt[m], 'beta': d.beta[m], 'fy': np.abs(d.fyf[m]),
            **{k: d.q[k][m] for k in QUANTITIES}}


def _series(d):
    """(grid t, share on a uniform grid, index of the telemetry sample under each grid point, fs): the native force writes
    when the sidecar has them (zero-order hold), else the replayed share at the telemetry's rate."""
    if d.native is not None:
        tn, xn = d.native
        dts = np.diff(tn)
        fs = min(500.0, 1.0 / float(np.median(dts[dts > 0]))) if (dts > 0).any() else d.fs
        grid = np.arange(max(tn[0], d.t[0]), min(tn[-1], d.t[-1]), 1.0 / fs)
        x = xn[np.clip(np.searchsorted(tn, grid, side='right') - 1, 0, len(xn) - 1)]
    else:
        fs = d.fs
        grid = np.arange(d.t[0], d.t[-1], 1.0 / fs)
        x = np.interp(grid, d.t, np.nan_to_num(d.share))
    idx = np.clip(np.searchsorted(d.t, grid), 0, len(d.t) - 1)
    return grid, x, idx, fs


def drive_rates(d):
    """{condition: {metric: [value or None, seconds]}} for a derived drive: band energy (Nm at the wheel), time near zero
    under load, sudden and sustained falls per minute, share of time opposing Fy_f. A band above the series' Nyquist is None."""
    grid, x, idx, fs = _series(d)
    ok = d.valid[idx]
    sgn = -np.sign(d.fyf[idx])
    fyabs = np.abs(d.fyf[idx])
    lat = d.lat[idx]
    conds = condition_masks(lat, d.beta[idx], d.speed[idx], d.thr[idx], d.brk[idx])
    peak = PEAK_NM
    xlow = fp.lowpass(x, fs, 8.0)
    oppose_ok = ok & (fyabs > FY_MIN)
    opposing = xlow * sgn < 0
    near_zero = np.abs(x) < NOZERO_LOAD[0]
    loaded = ok & (lat >= NOZERO_LOAD[1])
    falls = fp.find_falls(x, ok, fs) if ok.sum() > int(fs * 2) else []
    fall_at = np.array([f[0] for f in falls], int)
    fall_sus = np.array([f[2] for f in falls], bool)
    dtg = 1.0 / fs
    out = {}
    for name, cm in conds.items():
        m = ok & cm
        secs = float(m.sum() * dtg)
        r = {}
        if secs < MIN_RATE_S:
            out[name] = {'seconds': secs}
            continue
        for (lo, hi) in BANDS:
            key = 'band.%g_%g' % (lo, hi)
            if hi > fs / 2.0 * 1.05:
                r[key] = [None, 0.0]
                continue
            v, s = fp.band_rms(x * peak, m, fs, ((lo, min(hi, fs / 2.0 * 0.999)),))
            r[key] = [v[0] if s >= MIN_RATE_S else None, s]
        lm = loaded & cm
        r['zero_load'] = [float(np.mean(near_zero[lm])) if lm.sum() * dtg >= MIN_RATE_S else None, float(lm.sum() * dtg)]
        om = oppose_ok & cm
        r['oppose'] = [float(np.mean(opposing[om])) if om.sum() * dtg >= MIN_RATE_S else None, float(om.sum() * dtg)]
        if len(fall_at):
            inside = cm[fall_at]
            r['sudden_per_min'] = [float(inside.sum() / (secs / 60.0)), secs]
            r['sustained_per_min'] = [float((inside & fall_sus).sum() / (secs / 60.0)), secs]
        else:
            r['sudden_per_min'] = r['sustained_per_min'] = [0.0 if ok.sum() > int(fs * 2) else None, secs]
        r['seconds'] = secs
        out[name] = r
    return out


def summarise_cells(d):
    """{cell key: {seconds, n, per quantity [p10, p50, p90]}} of a derived drive (for scoring and ranges)."""
    rows = sample_rows(d)
    out = {}
    if not len(rows['cell']):
        return out
    order = np.argsort(rows['cell'], kind='stable')
    cells = rows['cell'][order]
    bounds = np.flatnonzero(np.diff(cells)) + 1
    for sl in np.split(np.arange(len(cells)), bounds):
        idx = order[sl]
        seconds = float(rows['dt'][idx].sum())
        rec = {'seconds': seconds, 'n': int(len(idx))}
        for qn in QUANTITIES:
            rec[qn] = [float(x) for x in np.percentile(rows[qn][idx], [10, 50, 90])]
        out[cell_key(cells[sl[0]])] = rec
    return out


# ---------------------------------------------------------------------------------------------------- pooling

def pool_cells(drives, cap_s=CAP_S):
    """The pooled cells of a list of (rows, car, drive name): {cell id: record}, where a record holds seconds, n, n_drives,
    n_cars, the pooled p10/p50/p90 of each quantity (drives weighted so none weighs more than `cap_s` seconds in the cell), the
    spread (the within-cell sigma and, with two cars or more, the sigma of the cars' medians combined), the range of the
    drives' own medians, and the cell's median |Fy_f|, slip and ratio. No basis yet."""
    if not drives:
        return {}
    cars = sorted({d[1] for d in drives})
    cat = {k: [] for k in ('cell', 'dt', 'beta', 'fy', 'drive', 'car') + QUANTITIES}
    ratio = []
    for i, (rows, car, name, rat) in enumerate(drives):
        m = len(rows['cell'])
        for k in ('cell', 'dt', 'beta', 'fy') + QUANTITIES:
            cat[k].append(rows[k])
        cat['drive'].append(np.full(m, i))
        cat['car'].append(np.full(m, cars.index(car)))
        ratio.append(np.full(m, rat))
    A = {k: np.concatenate(v) for k, v in cat.items()}
    A['ratio'] = np.concatenate(ratio)
    nd = len(drives)
    key = A['cell'].astype(np.int64) * nd + A['drive']
    _, inv = np.unique(key, return_inverse=True)
    T = np.bincount(inv, weights=A['dt'])
    A['w'] = A['dt'] * np.minimum(1.0, cap_s / np.maximum(T[inv], 1e-9))
    order = np.argsort(A['cell'], kind='stable')
    cells = A['cell'][order]
    bounds = np.flatnonzero(np.diff(cells)) + 1
    out = {}
    for sl in np.split(np.arange(len(cells)), bounds):
        idx = order[sl]
        seconds = float(A['dt'][idx].sum())
        if seconds < MIN_SPARSE_S or len(idx) < MIN_SPARSE_N:
            continue
        w = A['w'][idx]
        dr, cr = A['drive'][idx], A['car'][idx]
        rec = {'seconds': seconds, 'n': int(len(idx)), 'n_drives': int(len(np.unique(dr))), 'n_cars': int(len(np.unique(cr))),
               'beta_med': wquant(A['beta'][idx], w, [0.5])[0], 'fy_med': wquant(A['fy'][idx], w, [0.5])[0],
               'ratio_med': wquant(A['ratio'][idx], w, [0.5])[0], 'spread': {}, 'spread_cars': {}, 'range': {}}
        for qn in QUANTITIES:
            v = A[qn][idx]
            p10, p50, p90 = wquant(v, w, [0.1, 0.5, 0.9])
            rec[qn] = {'p10': p10, 'p50': p50, 'p90': p90}
            within = sigma(p10, p90)
            medc = [wquant(v[cr == c], w[cr == c], [0.5])[0] for c in np.unique(cr)]
            between = float(np.std(medc)) if len(medc) >= 2 else None
            rec['spread_cars'][qn] = between
            rec['spread'][qn] = max(math.hypot(within, between or 0.0), SPREAD_FLOOR[qn])
            medd = [wquant(v[dr == k], w[dr == k], [0.5])[0] for k in np.unique(dr)]
            rec['range'][qn] = [min(medd), max(medd)]
        rec['basis'] = 'measured' if seconds >= MIN_CELL_S and rec['n_drives'] >= MIN_CELL_DRIVES else None
        out[int(cells[sl[0]])] = rec
    return out


def fit_trail(cells):
    """The physics-shaped fit of the effective trail against slip over the measured cells: t(beta) = c + p / (1 + (beta /
    b0)^2), the pneumatic part collapsing as the tyre saturates (b0 by a small grid search, c and p by weighted least
    squares). Returns {'c','p','b0','resid','beta_range','lat_range','n'} or None with too few cells or slip bands."""
    pts = [(c['beta_med'], c['trail_m']['p50'], min(c['seconds'], CAP_S), cell_parts(i)[1], cell_parts(i)[0])
           for i, c in cells.items() if c.get('basis') == 'measured']
    if len(pts) < MIN_FIT_CELLS or len({p[3] for p in pts}) < MIN_FIT_SLIPS:
        return None
    b, y, w = (np.array([p[k] for p in pts], float) for k in (0, 1, 2))
    best = None
    for b0 in (0.05, 0.1, 0.15, 0.2, 0.3, 0.5):
        X = np.stack([np.ones_like(b), 1.0 / (1.0 + (b / b0) ** 2)], 1)
        sw = np.sqrt(w)
        coef, *_ = np.linalg.lstsq(X * sw[:, None], y * sw, rcond=None)
        res = float(np.sqrt(np.sum(w * (X @ coef - y) ** 2) / np.sum(w)))
        if best is None or res < best['resid']:
            best = {'c': float(coef[0]), 'p': float(coef[1]), 'b0': b0, 'resid': res}
    best['beta_range'] = [float(b.min()), float(b.max())]
    lat = [p[4] for p in pts]
    best['lat_range'] = [int(min(lat)), int(max(lat))]
    best['n'] = len(pts)
    return best


def apply_fit(cells, fit):
    """Give each unfilled cell with some data (but under the measured threshold) a fitted target of effective trail, torque and
    Nm per kN: interpolated inside the measured slip and lateral range, else extrapolated (twice the residual as spread). The
    share cannot be fitted (it needs the mapping) and stays None. A cell with no fit stays empty."""
    if fit is None:
        return
    for i, c in cells.items():
        if c.get('basis'):
            continue
        lat_i = cell_parts(i)[0]
        b = c['beta_med']
        inside = fit['beta_range'][0] <= b <= fit['beta_range'][1] and fit['lat_range'][0] <= lat_i <= fit['lat_range'][1]
        t = fit['c'] + fit['p'] / (1.0 + (b / fit['b0']) ** 2)
        sp = max(fit['resid'], SPREAD_FLOOR['trail_m']) * (1.0 if inside else 2.0)
        torque = t * c['fy_med'] / c['ratio_med']
        nmkn = t * 1000.0 / c['ratio_med']
        c['basis'] = 'interpolated' if inside else 'extrapolated'
        c['trail_m'] = {'p10': t - SPREAD_K / 2 * sp, 'p50': t, 'p90': t + SPREAD_K / 2 * sp}
        c['torque_nm'] = {'p10': None, 'p50': torque, 'p90': None}
        c['nm_per_kn'] = {'p10': None, 'p50': nmkn, 'p90': None}
        c['share'] = {'p10': None, 'p50': None, 'p90': None}
        c['spread'] = {'trail_m': sp, 'torque_nm': sp * c['fy_med'] / c['ratio_med'], 'nm_per_kn': sp * 1000.0 / c['ratio_med'],
                       'share': None}
        c['spread_cars'] = {q: None for q in QUANTITIES}
        c['range'] = {q: None for q in QUANTITIES}


def pool_rates(per_drive, cars, cap_s=CAP_S):
    """{condition: {metric: record}} from [{condition: {metric: [value, seconds]}}] (one per drive) with the drives' car keys:
    the median of the drives' values (weight: seconds, capped), the sigma across drives combined with the sigma across cars,
    n_drives, n_cars, seconds. A metric with fewer than two drives is left out."""
    out = {}
    conds = {c for p in per_drive for c in p}
    for cond in sorted(conds):
        metrics = {m for p in per_drive for m in p.get(cond, {}) if m != 'seconds'}
        rec = {}
        for m in sorted(metrics):
            vals = [(p[cond][m][0], p[cond][m][1], cars[i]) for i, p in enumerate(per_drive)
                    if cond in p and m in p[cond] and p[cond][m][0] is not None]
            if len(vals) < 2:
                continue
            v = np.array([x[0] for x in vals])
            w = np.minimum(np.array([x[1] for x in vals]), cap_s)
            p10, p50, p90 = wquant(v, w, [0.1, 0.5, 0.9])
            carlist = sorted({x[2] for x in vals})
            medc = [wquant(v[[x[2] == c for x in vals]], w[[x[2] == c for x in vals]], [0.5])[0] for c in carlist]
            between = float(np.std(medc)) if len(medc) >= 2 else None
            floor = RATE_FLOOR['band'] if m.startswith('band') else RATE_FLOOR[m]
            rec[m] = {'p10': p10, 'p50': p50, 'p90': p90, 'spread': max(math.hypot(sigma(p10, p90), between or 0.0), floor),
                      'spread_cars': between, 'n_drives': len(vals), 'n_cars': len(carlist),
                      'seconds': float(sum(x[1] for x in vals))}
        if rec:
            out[cond] = rec
    return out


# ---------------------------------------------------------------------------------------------------- build

def build(paths, hold_out=(), log=print, replay_fn=None):
    """The benchmark dict from reference captures. Excluded drives are listed with the reason."""
    configure_store()
    drives, used, skipped = [], [], []
    for path in paths:
        name = os.path.basename(path)[:-len('.ovcap.gz')]
        if name in hold_out:
            skipped.append({'drive': name, 'why': "held out of this benchmark (a sanity check)"})
            continue
        log("reading %s" % name)
        t0 = time.time()
        d = read_drive(path, 'acr') if replay_fn is None else replay_fn(path)
        if not d.ok:
            skipped.append({'drive': name, 'car': getattr(d, 'car', None), 'why': d.why})
            continue
        derive(d, 'replay')
        if d.moving_s < MIN_MOVING_S:
            skipped.append({'drive': name, 'car': d.car, 'why': "only %.0f s of moving data with tyre forces (needs %.0f)"
                            % (d.moving_s, MIN_MOVING_S)})
            continue
        rates = drive_rates(d)
        drives.append((sample_rows(d), d.car, name, d.prov['ratio'][0]))
        used.append({'drive': name, 'car': d.car, 'class': car_class(d.car), 'moving_s': round(d.moving_s, 1), 'fs_hz': round(d.fs, 1),
                     'ratio': list(d.prov['ratio']), 'mass_kg': list(d.prov['mass']), 'trail_m': list(d.prov['trail']),
                     'rates': rates})
        log("  %s: %s, %.0f s moving, %.1f s of work" % (d.car, car_class(d.car), d.moving_s, time.time() - t0))
    return assemble(drives, used, skipped)


def assemble(drives, used, skipped):
    """Pools (one per class with at least one drive, plus 'all') from the drives read; the benchmark dict."""
    classes = sorted({u['class'] for u in used})
    pools = {}
    for cl in classes + ['all']:
        sel = [i for i, u in enumerate(used) if cl == 'all' or u['class'] == cl]
        cells = pool_cells([drives[i] for i in sel])
        fit = fit_trail({i: c for i, c in cells.items()})
        apply_fit(cells, fit)
        rates = pool_rates([used[i]['rates'] for i in sel], [used[i]['car'] for i in sel])
        count = {'measured': 0, 'interpolated': 0, 'extrapolated': 0}
        for c in cells.values():
            if c['basis']:
                count[c['basis']] += 1
        pools[cl] = {'n_drives': len(sel), 'n_cars': len({used[i]['car'] for i in sel}),
                     'cars': sorted({used[i]['car'] for i in sel}), 'fit': fit,
                     'counts': {**count, 'empty': N_CELLS - sum(count.values()),
                                'sparse_empty': sum(1 for c in cells.values() if not c['basis'])},
                     'cells': {cell_key(i): _clean(c) for i, c in cells.items() if c['basis']},
                     'conditions': rates}
    return {'version': VERSION, 'built': time.strftime('%Y-%m-%d %H:%M:%S'), 'git_head': git_head(), 'hashes': code_hashes(),
            'mapping': MAPPING, 'target': "what ACR would do (the owner's reference); physics may overrule it",
            'surface': "not available: the ACR telemetry gives no surface (surface_grip is empty in every reference drive), "
                       "so no cell is split by surface",
            'bins': {'lat': [lat_label(i) for i in range(N_LAT)], 'slip': [slip_label(i) for i in range(N_SLIP)],
                     'speed': [speed_label(i) for i in range(N_SPEED)], 'pedal': list(PEDALS), 'n_cells': N_CELLS},
            'drives': [{k: v for k, v in u.items() if k != 'rates'} for u in used],
            'skipped': skipped, 'pools': pools}


def _clean(c):
    def walk(x):
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, float):
            return _round(x)
        return x
    return walk(c)


# ---------------------------------------------------------------------------------------------------- compare

def stale(bench):
    """[(file, benchmark hash, today's hash)] of the watched files that changed since the benchmark was built."""
    now = code_hashes()
    return [(f, bench.get('hashes', {}).get(f), now.get(f)) for f in WATCHED if bench.get('hashes', {}).get(f) != now.get(f)]


def pick_pool(bench, cls, forced=None):
    """(pool name, pool, why): the drive's class pool when it holds 10 measured cells, else the 'all' pool."""
    pools = bench['pools']
    if forced:
        if forced not in pools:
            raise ValueError("no pool %r (have %s)" % (forced, ", ".join(pools)))
        return forced, pools[forced], "forced"
    if cls in pools and pools[cls]['counts']['measured'] >= 10:
        return cls, pools[cls], "the drive's class"
    return 'all', pools['all'], ("the %s pool has %d measured cells (needs 10): all classes pooled" % (cls, pools[cls]['counts']['measured'])
                                 if cls in pools else "no %s drive in the benchmark: all classes pooled" % cls)


def z_of(value, target, spread):
    return max(-Z_CLIP, min(Z_CLIP, (value - target) / spread))


def score_cells(cells, pool, quantities=SCORED):
    """Score a drive's cell summary ({key: {seconds, n, q: [p10, p50, p90]}}) against a pool. Returns a list of per-cell rows
    {key, seconds, basis, z{q}, value{q}, target{q}, spread{q}, z (mean |z|), out_of_range [q], weight} for the cells the drive has
    at least MIN_COMPARE_S of, and the keys with no target."""
    rows, untargeted = [], []
    for key, c in cells.items():
        if c['seconds'] < MIN_COMPARE_S or c['n'] < MIN_COMPARE_N:
            continue
        t = pool['cells'].get(key)
        if t is None:
            untargeted.append((key, c['seconds']))
            continue
        row = {'key': key, 'seconds': c['seconds'], 'basis': t['basis'], 'z': {}, 'value': {}, 'target': {}, 'spread': {},
               'out_of_range': []}
        for q in quantities:
            tv, sp = (t.get(q) or {}).get('p50'), (t.get('spread') or {}).get(q)
            if tv is None or sp is None:
                continue
            v = c[q][1]
            row['z'][q], row['value'][q], row['target'][q], row['spread'][q] = z_of(v, tv, sp), v, tv, sp
            rng = (t.get('range') or {}).get(q)
            if rng and not rng[0] - 1e-12 <= v <= rng[1] + 1e-12:
                row['out_of_range'].append(q)
        if not row['z']:
            continue
        row['mean_abs_z'] = float(np.mean([abs(z) for z in row['z'].values()]))
        row['like'] = float(np.mean([math.exp(-0.5 * z * z) for z in row['z'].values()]))
        row['weight'] = c['seconds'] * BASIS_WEIGHT[t['basis']]
        rows.append(row)
    return rows, untargeted


def score_rates(rates, pool):
    """Score a drive's per-condition rates against a pool: rows {condition, metric, value, target, spread, z, seconds}."""
    rows, untargeted = [], []
    for cond, r in rates.items():
        for m, vs in r.items():
            if m == 'seconds' or vs[0] is None:
                continue
            t = (pool['conditions'].get(cond) or {}).get(m)
            if t is None:
                untargeted.append((cond, m))
                continue
            rows.append({'condition': cond, 'metric': m, 'value': vs[0], 'target': t['p50'], 'spread': t['spread'],
                         'z': z_of(vs[0], t['p50'], t['spread']), 'seconds': vs[1], 'n_drives': t['n_drives']})
    return rows, untargeted


def overall(cell_rows, rate_rows):
    """The overall score 0..100: two thirds the time-weighted mean of exp(-z^2/2) over the scored cells (estimated cells at
    half or quarter weight), one third the plain mean over the rates (the 'all' condition and each bin). 100 is on target
    in every cell; about 61 is a cell one spread off. Also the time-weighted mean |z|."""
    out = {'cells': None, 'rates': None, 'score': None, 'mean_abs_z': None}
    if cell_rows:
        w = np.array([r['weight'] for r in cell_rows])
        out['cells'] = float(100.0 * np.sum(w * np.array([r['like'] for r in cell_rows])) / np.sum(w))
        out['mean_abs_z'] = float(np.sum(w * np.array([r['mean_abs_z'] for r in cell_rows])) / np.sum(w))
    if rate_rows:
        out['rates'] = float(100.0 * np.mean([math.exp(-0.5 * r['z'] ** 2) for r in rate_rows]))
    if out['cells'] is not None and out['rates'] is not None:
        out['score'] = 2.0 / 3.0 * out['cells'] + 1.0 / 3.0 * out['rates']
    else:
        out['score'] = out['cells'] if out['cells'] is not None else out['rates']
    return out


def compare(path, bench, views=None, forced_pool=None):
    """Score a capture. Returns a dict per view ('played', 'column', 'replay'): the drive's facts, the pool used, cell and rate
    scores. A view that cannot be made is listed under 'unavailable' with the reason."""
    configure_store()
    probe = read_drive(path, None)
    if not probe.ok:
        raise RuntimeError("%s: %s" % (path, probe.why))
    game = probe.game
    have_force = probe.ext is not None
    want = views or (['played'] + (['column'] if game == 'rbr' else []) + (['replay'] if game == 'acr' else []))
    out = {'drive': probe.name, 'game': game, 'car': probe.car, 'class': car_class(probe.car), 'views': {}, 'unavailable': {}}
    for view in want:
        if view == 'played' and not have_force:
            out['unavailable'][view] = "no recorded force (ext_force): the capture was not recorded for FFB tuning"
            continue
        if view == 'column' and (game != 'rbr' or not have_force):
            out['unavailable'][view] = "needs an RBR drive with the game's raw force recorded"
            continue
        if view == 'replay' and game != 'acr':
            out['unavailable'][view] = "only ACR drives are replayed through RackForce (they carry tyre Mz)"
            continue
        d = {'played': lambda: probe, 'column': lambda: read_drive(path, 'rbr'), 'replay': lambda: read_drive(path, 'acr')}[view]()
        if not d.ok:
            out['unavailable'][view] = d.why
            continue
        try:
            derive(d, view)
        except ValueError as e:
            out['unavailable'][view] = str(e)
            continue
        if d.moving_s < MIN_MOVING_S / 2:
            out['unavailable'][view] = "only %.0f s of moving data" % d.moving_s
            continue
        pname, pool, why = pick_pool(bench, out['class'], forced_pool)
        cell_rows, no_target = score_cells(summarise_cells(d), pool)
        rate_rows, no_rate = score_rates(drive_rates(d), pool)
        out['views'][view] = {'moving_s': d.moving_s, 'fs_hz': d.fs, 'pool': pname, 'pool_why': why, 'prov': d.prov,
                              'native': d.native is not None, 'peak_source': getattr(d, 'peak_source', 'replay'),
                              'cells': cell_rows, 'rates': rate_rows, 'no_target_cells': no_target, 'no_target_rates': no_rate,
                              'overall': overall(cell_rows, rate_rows)}
    return out


# ---------------------------------------------------------------------------------------------------- text

def _g(x, unit=''):
    if x is None:
        return "-"
    return "%.3g%s" % (x, unit)


def report_build(b):
    L = ["FFB benchmark built %s (git %s), version %d" % (b['built'], b['git_head'], b['version']),
         "TARGET: %s." % b['target'],
         "Mapping (fixed): %s" % json.dumps(b['mapping']['wheel_peak_nm']) + " Nm peak; " + b['mapping']['note'],
         "Surface: %s." % b['surface'],
         "Bins: lateral load Fy_f/(m (b/L) g) %s; body slip %s; speed %s; pedal %s (%d cells)"
         % (", ".join(b['bins']['lat']), ", ".join(b['bins']['slip']), ", ".join(b['bins']['speed']),
            ", ".join(b['bins']['pedal']), b['bins']['n_cells']),
         "Quantities: torque_nm = restoring column torque (Nm: the kingpin torque over the ratio, before the power assist and the hand-force mapping); share = played "
         "share of full scale; trail_m = torque x ratio / Fy_f (m); nm_per_kn = torque per kN of Fy_f. Restoring is positive.",
         "", "Drives used (%d):" % len(b['drives'])]
    for u in b['drives']:
        L.append("  %s %s [%s] %.0f s moving, %.0f Hz; ratio %.2f (%s), mass %.0f kg (%s), trail %.1f mm (%s)"
                 % (u['drive'], u['car'], u['class'], u['moving_s'], u['fs_hz'], u['ratio'][0], u['ratio'][1], u['mass_kg'][0],
                    u['mass_kg'][1], u['trail_m'][0] * 1e3, u['trail_m'][1]))
    L.append("Drives excluded (%d):" % len(b['skipped']))
    by = {}
    for s in b['skipped']:
        by.setdefault(s['why'], []).append("%s%s" % (s['drive'], " (%s)" % s['car'] if s.get('car') else ""))
    for why, names in by.items():
        L.append("  %d: %s -- %s" % (len(names), why, ", ".join(names[:6]) + (" ..." if len(names) > 6 else "")))
    L.append("")
    for name, p in b['pools'].items():
        c = p['counts']
        L.append("Pool %s: %d drives, %d cars (%s); cells measured %d, interpolated %d, extrapolated %d, empty %d of %d "
                 "(%d of those hold a little data); %d conditions with rates"
                 % (name, p['n_drives'], p['n_cars'], ", ".join(p['cars']), c['measured'], c['interpolated'], c['extrapolated'],
                    c['empty'], b['bins']['n_cells'], c['sparse_empty'], len(p['conditions'])))
        f = p['fit']
        L.append("  fit t(beta) = %s" % ("none (too few measured cells)" if f is None else
                 "%.1f mm + %.1f mm / (1 + (beta/%g)^2), residual %.1f mm, %d cells, slip %.2f-%.2f"
                 % (f['c'] * 1e3, f['p'] * 1e3, f['b0'], f['resid'] * 1e3, f['n'], f['beta_range'][0], f['beta_range'][1])))
    L.append("")
    L.append("These targets are what ACR would do through today's code, replayed from telemetry. They are estimated (E), not recorded; "
             "the spread across cars is shown where there is more than one car, and one car means no spread across cars.")
    return "\n".join(L)


def report_compare(res, bench, top=10):
    L = ["FFB benchmark compare: %s (%s, %s, class %s)" % (res['drive'], res['game'], res['car'], res['class']),
         "TARGET: what ACR would do -- the owner's stated reference, which physics may overrule. A gap is a question, not a verdict.",
         "Benchmark built %s (git %s), %d drives" % (bench['built'], bench['git_head'], len(bench['drives']))]
    st = stale(bench)
    if st:
        L.append("STALE: " + "; ".join("%s changed since the benchmark (%s -> %s)" % (f, (a or 'none')[:8], (b or 'none')[:8])
                                       for f, a, b in st) + ". Rebuild before trusting the targets.")
    L.append("Numbers: R recorded (read from the capture: the played force, the telemetry); E estimated (Newton's Fy_f, the "
             "column torque derived from the played force by undoing a rally-class assist or recovered from the raw level, ratio and mass as noted, every target).")
    for view, why in res['unavailable'].items():
        L.append("view %s unavailable: %s" % (view, why))
    for view, v in res['views'].items():
        o = v['overall']
        L += ["", "=== view %s: %s ===" % (view, {'played': "the recorded played force (share R; column torque E = the assist undone from share x peak / rho)",
                                                   'column': "RBR column torque recovered from the game's raw level (E), played share R",
                                                   'replay': "today's chain replayed over the telemetry (E)"}[view]),
              "pool %s (%s); %.0f s moving, %.0f Hz%s; ratio %.2f (%s), mass %.0f kg (%s)"
              % (v['pool'], v['pool_why'], v['moving_s'], v['fs_hz'], ", native force writes" if v['native'] else "",
                 v['prov']['ratio'][0], v['prov']['ratio'][1], v['prov']['mass'][0], v['prov']['mass'][1]),
              "OVERALL SCORE %s / 100 (cells %s, rates %s; mean |z| %s; 100 = on target, ~61 = one spread off)"
              % (_g(o['score']), _g(o['cells']), _g(o['rates']), _g(o['mean_abs_z']))]
        cells = v['cells']
        L.append("%d cells scored; basis: %s" % (len(cells), ", ".join("%s %d" % (b, sum(1 for r in cells if r['basis'] == b))
                                                                       for b in BASIS_WEIGHT)))
        L.append("Ten worst cells (value vs target, in units of the spread):")
        worst = sorted(cells, key=lambda r: -r['mean_abs_z'])[:top]
        for r in worst:
            q = max(r['z'], key=lambda k: abs(r['z'][k]))
            L.append("  %-44s %5.1f s %-12s %s %s vs %s (spread %s) = %+.1f%s"
                     % (r['key'], r['seconds'], r['basis'], q, _g(r['value'][q], UNITS[q]), _g(r['target'][q]), _g(r['spread'][q]),
                        r['z'][q], "  OUT OF RANGE" if q in r['out_of_range'] else ""))
        oor = [r for r in cells if r['out_of_range']]
        L.append("Out of the reference's range (outside the span of the reference drives' own medians): %d cells%s"
                 % (len(oor), "" if not oor else ": " + "; ".join("%s (%s)" % (r['key'], ",".join(r['out_of_range'])) for r in
                                                                  sorted(oor, key=lambda r: -r['seconds'])[:8])))
        nt = v['no_target_cells']
        L.append("No target (empty in the reference): %d cells, %.0f s%s" % (len(nt), sum(s for _k, s in nt),
                 "" if not nt else ": " + "; ".join("%s %.0f s" % x for x in sorted(nt, key=lambda x: -x[1])[:5])))
        L.append("Rates by condition (value vs target in spreads; worst 8 of %d):" % len(v['rates']))
        for r in sorted(v['rates'], key=lambda r: -abs(r['z']))[:8]:
            L.append("  %-16s %-18s %s vs %s (spread %s, %d drives) = %+.1f"
                     % (r['condition'], r['metric'], _g(r['value']), _g(r['target']), _g(r['spread']), r['n_drives'], r['z']))
        if v['no_target_rates']:
            L.append("  no reference rate: %s" % ", ".join("%s/%s" % x for x in v['no_target_rates'][:6]))
    return "\n".join(L)
