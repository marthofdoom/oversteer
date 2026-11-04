"""FFB parity: one recorded drive set against a reference drive (scripts/ffb-parity.py is the CLI).

Both drives are "Record for FFB tuning" captures (.ovcap.gz with its .ffb.gz and .wheel.gz sidecars). Every measure is
worked out from what was RECORDED (the force the engine wrote, ext_force; the game's telemetry; the wheel's position)
by the same code for both drives, so a gap between two games is a gap in what the driver was given. Nothing here runs
the force chain: the audit it comes from (~/.cache/ffb-rbr-gap/, 2026-11-04) replayed the chain to split the force
into parts, which a capture alone cannot do; those parts are not measured here.

Measures are marked R (read from the recording) or E (estimated: needs an assumption, named in the label). A band with
too little data is None and printed as "too little data", never as a number.

Conventions: ext_force is positive to the left, as the engine writes it; Newton's front lateral force Fy_f
(ffb_forza.front_lateral) is positive left, so a force restoring the wheel has the sign opposite Fy_f ("along").
Grip is body slip beta < 0.05, a slide beta > 0.1 (beta = |atan(vy / |vx|)|), driving speed > 10 m/s.
"""
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))

try:
    import numpy as np
    import scipy.signal as sg
    MISSING = None
except ImportError as _e:                          # the CLI prints this; the module stays importable for the message
    np = sg = None
    MISSING = "ffb-parity needs numpy and scipy (%s). Install them: pip install numpy scipy" % _e

FULL = 32767.0                                   # ext_force full scale
DEFAULT_PEAK = 3.5                               # Nm: wheel_torque.DEFAULT_PEAK_NM, the G29 class
MIN_S = 4.0                                      # seconds of data a band needs
TOL = 0.25                                       # a measure this far off the reference is flagged
FLAT_TOL = 0.02                                  # the steering ratio must be this flat
SPEED = 10.0                                     # m/s: driving
BETA_GRIP, BETA_SLIDE = 0.05, 0.1
MIN_PLAYING = 30.0                               # s of playing time a drive needs for anything at all
PLAY_GAP = 0.03                                  # s: an ext_force write this recent means the force is playing
AY_BANDS = ((2.0, 5.0), (5.0, 9.0))
SLIP_BANDS = ((0.05, 0.1), (0.1, 0.2), (0.2, 0.4), (0.4, 0.7), (0.7, 9.9))
FREQ_BANDS = ((0.5, 3.0), (3.0, 15.0), (15.0, 30.0), (30.0, 60.0), (60.0, 120.0))
# header fields that decide whether two drives' absolute levels can be compared (name -> where it is read)
MAPPING_FIELDS = ('wheel_peak', 'ratio', 'power_class', 'column_damping', 'mapping', 'knee')


# ---------------------------------------------------------------------------------------------------- signal tools

def lowpass(x, fs, hz):
    """Zero-phase 2nd-order Butterworth; the signal is returned as is when hz is not below Nyquist."""
    if hz >= fs / 2.0 - 1e-9 or len(x) < 16:
        return np.asarray(x, float)
    b, a = sg.butter(2, hz / (fs / 2.0))
    return sg.filtfilt(b, a, x)


def runs(mask, fs, minlen=0.0):
    """[(start, stop)] of the True stretches of `mask` lasting at least `minlen` seconds."""
    d = np.diff(np.concatenate([[0], np.asarray(mask, int), [0]]))
    return [(a, b) for a, b in zip(np.where(d == 1)[0], np.where(d == -1)[0]) if (b - a) / fs >= minlen]


def band_rms(x, mask, fs, bands, seg=1.0):
    """(RMS of `x` per band, seconds used): Welch over the contiguous stretches of `mask` lasting at least `seg`."""
    n = int(seg * fs)
    power, weight = None, 0
    for a, b in runs(mask, fs, seg):
        f, p = sg.welch(x[a:b], fs=fs, nperseg=n, noverlap=n // 2, detrend='linear', window='hann')
        power = p * (b - a) if power is None else power + p * (b - a)
        weight += b - a
    if power is None:
        return [None] * len(bands), 0.0
    power = power / weight
    df = f[1] - f[0]
    return [float(np.sqrt(np.sum(power[(f >= lo) & (f < hi)]) * df)) for lo, hi in bands], weight / fs


def band_stats(values, mask, dt, min_s=MIN_S):
    """(median, p90, seconds) of `values` where `mask`, None when the mask holds fewer than `min_s` seconds."""
    seconds = float(dt[mask].sum())
    if seconds < min_s or mask.sum() < 20:
        return None
    v = values[mask]
    return float(np.median(v)), float(np.percentile(v, 90)), seconds


def find_falls(x, mask, fs, hold=0.25):
    """Sudden falls of |x| (the audit's m2_falls): >= .12 down within 60 ms from >= .15, to < 55 % of the start, the
    bottom taken within the next 100 ms, the next search a quarter second on. Returns [(start, bottom, sustained)];
    sustained: |x| stays below 55 % of the start for the whole `hold` after the bottom."""
    w, k = int(round(0.06 * fs)), int(round(hold * fs))
    a = np.abs(x)
    out, i, n = [], w, len(x)
    look = max(int(round(0.1 * fs)), 1)
    while i < n - w - k:
        if mask[i] and a[i - w] >= 0.15 and a[i - w] - a[i] >= 0.12 and a[i] < 0.55 * a[i - w]:
            s = i - w
            j = i + int(np.argmin(a[i:i + look]))
            out.append((s, j, bool(np.all(a[j:j + k] < 0.55 * a[s]))))
            i = j + k
        else:
            i += 1
    return out


def count_steps(x, mask, fs, threshold, window=0.025):
    """Rising edges per drive of |x[t] - x[t - window]| >= threshold while `mask` (a run counts once)."""
    n = max(int(round(window * fs)), 1)
    if len(x) <= n:
        return 0
    d = np.abs(x[n:] - x[:-n]) >= threshold
    return len(runs(d & mask[n:], fs))


def step_lag(a, b, fs, max_s=0.25, mask=None):
    """Seconds by which `b` lags `a` (positive: b later), by cross-correlation of the two 1-8 Hz bands."""
    if len(a) < int(4 * fs) or fs < 20:
        return 0.0
    hi = min(8.0, fs / 2.0 * 0.9)
    bb, aa = sg.butter(2, [1.0 / (fs / 2.0), hi / (fs / 2.0)], 'band')
    x, y = sg.filtfilt(bb, aa, a), sg.filtfilt(bb, aa, b)
    m = np.ones(len(x), bool) if mask is None else mask
    n = int(max_s * fs)
    lags = np.arange(-n, n + 1)
    c = [np.sum((x * np.roll(y, -l))[m]) for l in lags]
    return float(lags[int(np.argmax(np.abs(c)))] / fs)


# ---------------------------------------------------------------------------------------------------- loading

class Drive:
    """One capture's arrays on its telemetry sample times. `ok` is False with `why` when it cannot be read."""
    ok = False
    why = ''


def captures_folders():
    """The captures folders that exist: the app's own and the Flatpak's (a machine can have both)."""
    from oversteer import diagnostics
    flat = os.path.join(os.path.expanduser('~'), '.var', 'app', 'io.github.berarma.Oversteer', 'data', 'oversteer', 'captures')
    plain = os.path.join(diagnostics.data_home(), 'oversteer', 'captures')
    found = [f for f in (plain, flat) if os.path.isdir(f)]
    return found or [plain]


def resolve(name, folders=None):
    """A capture's path from a name (with or without .ovcap.gz) relative to a captures folder, or a full path."""
    folders = folders or captures_folders()
    for base in [''] + list(folders):
        cand = os.path.join(base, name) if base else name
        for tail in ('', '.ovcap.gz'):
            if os.path.isfile(cand + tail) and (cand + tail).endswith('.ovcap.gz'):
                return os.path.abspath(cand + tail)
    raise FileNotFoundError("no capture %r (looked in %s)" % (name, ", ".join(folders)))


def list_tuning(folders=None):
    """[(name, path)] of the captures with an FFB sidecar, oldest first (by name: they are stamped by date)."""
    out = {}
    for folder in folders or captures_folders():
        for f in os.listdir(folder):
            if f.endswith('.ovcap.gz') and os.path.exists(os.path.join(folder, f + '.ffb.gz')):
                out[f[:-len('.ovcap.gz')]] = os.path.join(folder, f)
    return sorted(out.items())


def game_of(path, limit=400):
    """The game key of a capture from its first decodable packets, None if unknown."""
    from oversteer import telemetry_capture as tc, telemetry_formats as tf
    _meta, recs = tc.read_capture(path)
    for i, (_t, _a, data) in enumerate(recs):
        if i > limit:
            break
        try:
            s = tf.decode_sample(data)
        except Exception:
            continue
        if s is not None and getattr(s, 'game', None):
            return s.game
    return None


def has_force(path):
    """True when the capture's sidecar holds ext_force records (a drive with the stage off has none)."""
    from oversteer import ffb_record
    try:
        _meta, frames = ffb_record.load(path + ffb_record.SUFFIX)
        return sum(1 for _t, name, _v in frames if name == 'ext_force') >= 50
    except (OSError, ValueError):
        return False


def default_pair(folders=None):
    """(newest, newest of a different game) as paths, among the captures with a recorded force; the second None when
    there is none."""
    items = list_tuning(folders)
    newest = None
    for _name, path in reversed(items):
        if has_force(path):
            newest = path
            break
    if newest is None:
        raise FileNotFoundError("no captures with recorded force in %s" % ", ".join(folders or captures_folders()))
    game = game_of(newest)
    for _name, path in reversed(items):
        if path != newest and has_force(path) and game_of(path) != game:
            return newest, path
    return newest, None


def _quad(v, n):
    if v is None:
        return [math.nan] * n
    return [float(x) if x is not None else math.nan for x in list(v)[:n]] + [math.nan] * max(0, n - len(v))


def load_drive(path, peak=None):
    """Decode a capture and its sidecars into a Drive (arrays over the telemetry samples, nothing written)."""
    from oversteer import car_data, ffb_forza, ffb_record, telemetry_capture as tc, telemetry_formats as tf
    d = Drive()
    d.path = path
    d.name = os.path.basename(path)[:-len('.ovcap.gz')] if path.endswith('.ovcap.gz') else os.path.basename(path)
    ffb_path = path + ffb_record.SUFFIX
    if not os.path.exists(ffb_path):
        d.why = "no .ffb.gz sidecar: not recorded for FFB tuning"
        return d
    meta, recs = tc.read_capture(path)
    d.capmeta = meta
    rows, car, game, drivetrain = [], None, None, None
    for t, _a, data in recs:
        try:
            s = tf.decode_sample(data)
        except Exception:
            continue
        if s is None:
            continue
        car = s.car or car
        game = game or getattr(s, 'game', None)
        drivetrain = getattr(s, 'drivetrain', None) or drivetrain
        acc, vel = _quad(s.accel, 3), _quad(s.vel, 3)
        lo = _quad(s.tyre_load, 4)
        sv = _quad(getattr(s, 'susp_vel', None), 4)
        rows.append([t, _f(s.speed), _f(s.rpm), _f(getattr(s, 'throttle', None)), _f(getattr(s, 'brake', None)),
                     _f(getattr(s, 'steer', None)), _f(getattr(s, 'yaw_rate', None)), acc[1], vel[0], vel[1],
                     lo[0], lo[1], sv[0], sv[1]])
    if len(rows) < 100:
        d.why = "fewer than 100 decodable packets"
        return d
    a = np.array(rows, float)
    a = a[np.isfinite(a[:, 0])]
    order = np.argsort(a[:, 0], kind='stable')
    a = a[order]
    d.t = a[:, 0]
    (d.speed, d.rpm, d.throttle, d.brake, d.steer, d.yaw, d.ay, d.vx, d.vy, d.lFL, d.lFR, d.svFL, d.svFR) = \
        (a[:, i] for i in range(1, 14))
    d.car, d.game, d.drivetrain = car, game, drivetrain
    dt = np.diff(d.t)
    d.fs = 1.0 / float(np.median(dt[dt > 0])) if (dt > 0).any() else 0.0
    d.dt = np.minimum(np.maximum(np.diff(d.t, prepend=d.t[0]), 0.0), 0.05)
    ffb_meta, frames = ffb_record.load(ffb_path)
    ch = {}
    for t, name, v in list(frames):
        ch.setdefault(name, []).append((t,) + tuple(v))
    d.ch = {k: np.array(v) for k, v in ch.items()}
    d.header = ffb_meta
    ef = d.ch.get('ext_force')
    if ef is None or len(ef) < 50:
        d.why = "no ext_force records: the force the engine wrote was not recorded"
        return d
    d.ext = ef
    j = np.clip(np.searchsorted(ef[:, 0], d.t), 1, len(ef) - 1)
    near = np.minimum(np.abs(ef[j, 0] - d.t), np.abs(ef[j - 1, 0] - d.t))
    d.playing = near < PLAY_GAP
    d.rec = np.interp(d.t, ef[:, 0], ef[:, 1]) / FULL
    # the wheel's position: ABS_X (EV_ABS 3, code 0) in degrees from the centre, positive right
    d.wheel_deg, d.wheel_range = None, None
    wp = path + '.wheel.gz'
    if os.path.exists(wp):
        wm, wr = tc.read_wheel(wp)
        w = np.array([(t, v) for t, ty, code, v in list(wr) if ty == 3 and code == 0], float)
        try:
            rng = float(wm.get('range'))
        except (TypeError, ValueError):
            rng = None
        axes = wm.get('axes')
        lo_hi = (0.0, 65535.0)
        if isinstance(axes, dict) and '0' in axes:
            lo_hi = (float(axes['0'][0]), float(axes['0'][1]))
        if len(w) > 100 and rng:
            pos = (np.interp(d.t, w[:, 0], w[:, 1]) - lo_hi[0]) / (lo_hi[1] - lo_hi[0]) - 0.5
            d.wheel_deg, d.wheel_range = pos * rng, rng
    # Newton's front lateral force; mass from the shipped car table (1390 kg, the engine's own floor figure, if absent)
    entry = car_data.entry(car) if car else None
    d.mass = (entry.get('mass_kg') if entry else None) or 1390.0
    d.mass_known = bool(entry and entry.get('mass_kg'))
    bl = ffb_forza.front_share(drivetrain or 'awd')
    yaw = lowpass(np.nan_to_num(d.yaw), d.fs, 20.0)
    rdot = np.gradient(yaw, d.t) if len(d.t) > 2 else np.zeros_like(d.t)
    ay20 = lowpass(np.nan_to_num(d.ay), d.fs, 20.0)
    d.fyf = np.array([ffb_forza.front_lateral(d.mass, bl, x, y) for x, y in zip(ay20, rdot)])
    d.beta = np.abs(np.arctan2(d.vy, np.abs(d.vx)))
    d.drive = d.playing & (d.speed > SPEED) & np.isfinite(d.ay) & np.isfinite(d.beta)
    d.peak, d.peak_source = _peak(ffb_meta, peak)
    d.ok = True
    return d


def _f(v):
    return float(v) if v is not None else math.nan


def _peak(header, override):
    if override:
        return float(override), 'given'
    prof = ((header or {}).get('settings') or {}).get('profile') or {}
    for key in ('wheel_peak', 'peak_nm', 'wheel_peak_nm'):
        if isinstance(prof.get(key), (int, float)):
            return float(prof[key]), 'recorded'
    return DEFAULT_PEAK, 'assumed'


# ---------------------------------------------------------------------------------------------------- measures

class Result:
    """The ordered measures of one drive: key -> dict(label, unit, kind, value, why)."""

    def __init__(self):
        self.items = {}

    def add(self, key, label, unit, kind, value, why=None):
        if value is not None and not (isinstance(value, float) and math.isfinite(value)):
            value, why = None, why or "not finite"
        self.items[key] = {'label': label, 'unit': unit, 'kind': kind, 'value': value,
                           'why': (why or "too little data") if value is None else None}

    def value(self, key):
        item = self.items.get(key)
        return None if item is None else item['value']


def header_facts(d):
    """The comparable header fields: {name: value or None (not recorded)} plus the build and version."""
    h = d.header or {}
    st = h.get('settings') or {}
    prof, learnt = st.get('profile') or {}, st.get('learnt') or {}
    peak = prof.get('wheel_peak') if isinstance(prof.get('wheel_peak'), (int, float)) else None
    dmp = d.ch.get('ffb_dyn_damping')
    return {'build': h.get('build'), 'oversteer': h.get('oversteer'),
            'wheel_peak': peak, 'ratio': learnt.get('ratio'), 'power_class': prof.get('power_class'),
            'column_damping': prof.get('column_damping'), 'mapping': prof.get('mapping'), 'knee': prof.get('knee'),
            'damping_written': float(dmp[0, 1]) if dmp is not None and len(dmp) else None,
            'settings': st if st else None}


def measure(d):
    """All the measures of a readable Drive as a Result."""
    r = Result()
    pk = d.peak
    absrec = np.abs(d.rec)
    dm = d.drive
    drive_s = float(d.dt[dm].sum())
    r.add('time.driving_s', "driving time (playing, speed > 10 m/s)", 's', 'R', drive_s)
    if drive_s < MIN_PLAYING:
        r.add('time.too_short', "drive", '', 'R', None, "under %.0f s of driving: no measure is reliable" % MIN_PLAYING)
        d.grip_force = None
        return r
    corner = dm & (np.abs(d.ay) > 3.0)
    grip = dm & (d.beta < BETA_GRIP)
    gf = band_stats(absrec, grip & (np.abs(d.ay) > 3.0), d.dt, MIN_S)
    d.grip_force = gf[0] if gf else None
    r.add('grip.force_fs', "own grip force (median |force|, grip, |ay| > 3)", 'FS', 'R', d.grip_force)
    r.add('grip.force_nm', "own grip force (at peak %.2f Nm, %s)" % (pk, d.peak_source), 'Nm',
          'R' if d.peak_source != 'assumed' else 'E', None if d.grip_force is None else d.grip_force * pk)

    # --- absolute played force by band
    def force_rows(tag, label, mask):
        st = band_stats(absrec, mask, d.dt)
        for q, i in (('p50', 0), ('p90', 1)):
            v = None if st is None else st[i]
            r.add('force.%s.%s_fs' % (tag, q), "%s |force| %s" % (label, q), 'FS', 'R', v)
            r.add('force.%s.%s_nm' % (tag, q), "%s |force| %s at %.2f Nm (%s peak)" % (label, q, pk, d.peak_source), 'Nm',
                  'R' if d.peak_source != 'assumed' else 'E', None if v is None else v * pk)
            r.add('force.%s.%s_rel' % (tag, q), "%s |force| %s / own grip force" % (label, q), 'x', 'R',
                  None if (v is None or not d.grip_force) else v / d.grip_force)
    for lo, hi in AY_BANDS:
        force_rows('grip_ay%g_%g' % (lo, hi), "grip |ay| %g-%g" % (lo, hi), grip & (np.abs(d.ay) >= lo) & (np.abs(d.ay) < hi))
    for lo, hi in SLIP_BANDS:
        force_rows('slip%g_%g' % (lo, hi), "slip %g-%g" % (lo, hi) if hi < 9 else "slip over %g" % lo,
                   dm & (d.beta >= lo) & (d.beta < hi) & (np.abs(d.fyf) > 500))

    # --- force against lateral load, effective trail (from the played rim force, so it includes the mapping)
    c3, f3 = lowpass(d.rec * pk, d.fs, 3.0), lowpass(d.fyf, d.fs, 3.0)
    fm = grip & (np.abs(f3) > 800) & (np.abs(d.ay) < 9.0)
    if d.dt[fm].sum() >= MIN_S:
        k = -float(np.sum(c3[fm] * f3[fm]) / np.sum(f3[fm] ** 2)) * 1e3
        cc = float(np.corrcoef(c3[fm], f3[fm])[0, 1])
    else:
        k = cc = None
    r.add('load.rim_nm_per_kn', "rim force per kN of Newton Fy_f (grip, 3 Hz, through the origin)", 'Nm/kN', 'E', k)
    r.add('load.corr', "correlation of that fit", 'r', 'E', None if cc is None else -cc)
    ratio = (d.header.get('settings') or {}).get('learnt', {}).get('ratio') if d.header else None
    r.add('load.eff_trail_mm', "effective trail at the kingpin = Nm/kN x recorded ratio^2 (linear mapping, rim size ignored)", 'mm', 'E',
          None if (k is None or not isinstance(ratio, (int, float))) else k * ratio * ratio,
          "steering ratio not recorded in the header" if k is not None and not isinstance(ratio, (int, float)) else None)
    c8 = lowpass(d.rec, d.fs, 8.0)
    f8 = lowpass(d.fyf, d.fs, 3.0)
    for tag, label, mask in (('corner', "cornering |ay| > 3", corner & (np.abs(f8) > 500)),
                             ('slide', "sliding beta > 0.1", dm & (d.beta > BETA_SLIDE) & (np.abs(f8) > 500))):
        r.add('oppose.' + tag, "share of time the force opposes Newton's Fy_f, %s" % label, 'share', 'E',
              float(np.mean(c8[mask] * f8[mask] > 0)) if d.dt[mask].sum() >= MIN_S else None)

    # --- band energy of the played force
    bands, secs = band_rms(d.rec * pk, dm, d.fs, FREQ_BANDS)
    for (lo, hi), v in zip(FREQ_BANDS, bands):
        r.add('band.%g_%g' % (lo, hi), "played force band %g-%g Hz RMS%s" % (lo, hi, " (from 0.5)" if lo == 0.5 else ""),
              'Nm', 'R' if d.peak_source != 'assumed' else 'E', v if secs >= MIN_S else None)

    # --- steps
    minutes = drive_s / 60.0
    for thr in (0.1, 0.2):
        r.add('steps.%g' % thr, "sharp steps >= %g FS within 25 ms" % thr, '/min', 'R',
              count_steps(d.rec, dm, d.fs, thr) / minutes)

    # --- time under 3 % and 10 % of grip force while cornering, with and without the driver's spring
    spring = _spring(d)
    for share in (0.03, 0.10):
        tag = '%d' % round(share * 100)
        if d.grip_force and d.dt[corner].sum() >= MIN_S:
            under = float(np.mean(absrec[corner] < share * d.grip_force))
        else:
            under = None
        r.add('low.%s' % tag, "cornering time under %s %% of own grip force" % tag, 'share', 'R', under)
        if spring is None:
            r.add('low.%s_spring' % tag, "... counting the driver's spring too", 'share', 'E', None,
                  "ffb_align or the wheel position was not recorded")
        else:
            tot = np.abs(d.rec + spring)
            r.add('low.%s_spring' % tag, "... counting the driver's spring too (estimated spring)", 'share', 'E',
                  float(np.mean(tot[corner] < share * d.grip_force)) if (d.grip_force and d.dt[corner].sum() >= MIN_S) else None)

    # --- sudden falls
    falls = find_falls(d.rec, dm, d.fs)
    sl = dm & (d.beta > BETA_SLIDE)
    slide_min = float(d.dt[sl].sum()) / 60.0
    r.add('falls.per_min', "sudden falls per minute of driving (>= .12 FS in 60 ms to < 55 %)", '/min', 'R', len(falls) / minutes)
    nslide = sum(1 for s, _j, _u in falls if sl[s])
    r.add('falls.per_slide_min', "sudden falls per minute of sliding", '/min', 'R', nslide / slide_min if slide_min * 60 >= MIN_S else None)
    r.add('falls.sustained_per_min', "... of which sustained 250 ms or more", '/min', 'R', sum(1 for f in falls if f[2]) / minutes)
    fy20 = np.abs(lowpass(d.fyf, d.fs, 20.0))
    if len(falls) >= 10:
        cross = float(np.mean([np.sign(d.rec[s]) != np.sign(d.rec[j]) for s, j, _u in falls]))
        lat = float(np.mean([fy20[j] / max(fy20[s], 200.0) < 0.6 for s, j, _u in falls]))
    else:
        cross = lat = None
    r.add('falls.crossing', "share of falls that cross zero", 'share', 'R', cross, "fewer than 10 falls")
    r.add('falls.lateral_fell', "share of falls where Newton's lateral force fell below 60 % too", 'share', 'E', lat, "fewer than 10 falls")

    # --- impact pulses and engine hum
    ev, answered = _impacts(d, dm)
    r.add('impact.events_per_min', "front-load jumps (>= 40 % of the median in 50 ms, 300 ms apart) per minute", '/min', 'R', ev / minutes)
    r.add('impact.answered', "share of those followed within 100 ms by a force transient >= 0.01 FS above 20 Hz", 'share', 'E',
          answered if ev >= 10 else None, "fewer than 10 load jumps")
    hum = _hum(d)
    r.add('hum.nm', "engine hum: line at the firing frequency, RMS", 'Nm', 'E', None if hum is None else hum[0] * pk,
          "no steady-rpm stretch long enough")
    r.add('hum.snr_db', "engine hum: line over its neighbours", 'dB', 'E', None if hum is None else hum[1],
          "no steady-rpm stretch long enough")

    # --- runtime fit evidence
    _fit_evidence(d, r)
    return r


def _spring(d):
    """The driver's aligning spring in FS (positive left), estimated: clamp(k (target - position), +-tau), from the
    ffb_align records (target, k in uFS/deg, tau in 1e-4 FS) held until the next one and the wheel's position."""
    al = d.ch.get('ffb_align')
    if al is None or len(al) < 5 or d.wheel_deg is None:
        return None
    i = np.clip(np.searchsorted(al[:, 0], d.t, side='right') - 1, 0, len(al) - 1)
    half = d.wheel_range / 2.0
    target_deg = al[i, 1] / 32767.0 * half                 # ABS_X degrees from the centre (right positive)
    k = al[i, 2] * 1e-6
    tau = al[i, 3] * 1e-4
    s = np.clip(k * (d.wheel_deg - target_deg), -tau, tau)
    s[d.t < al[0, 0]] = 0.0
    return s


def _impacts(d, mask):
    """(front-load jumps, share answered by a force transient): the texture's impact pulses are not recorded, so what the
    game's load did is read from the telemetry and the played force is looked at after each jump."""
    if d.fs < 60 or not np.isfinite(d.lFL).any():
        return 0, 0.0
    front = np.nan_to_num(np.maximum(d.lFL, 0) + np.maximum(d.lFR, 0))
    base = float(np.median(front[d.speed > 5])) if (d.speed > 5).any() else 0.0
    if base <= 0:
        return 0, 0.0
    n, gap = max(int(round(0.05 * d.fs)), 1), int(0.3 * d.fs)
    rise = front[n:] - front[:-n]
    hp = np.abs(d.rec - lowpass(d.rec, d.fs, 20.0))
    count = hit = 0
    last = -10 ** 9
    for i in np.where((rise >= 0.4 * base) & mask[n:])[0] + n:
        if i - last > gap:
            count += 1
            hit += bool(hp[i:i + int(0.1 * d.fs) + 1].max() >= 0.01)
        last = i
    return count, (hit / count if count else 0.0)


def _hum(d):
    """(line RMS in FS, line over neighbours in dB), medians over 0.5 s windows of steady rpm at the firing frequency
    rpm / 30 (folded below 100 Hz, as the audit's m3)."""
    n = int(0.5 * d.fs)
    if n < 32:
        return None
    win = np.hanning(n)
    freqs = np.fft.rfftfreq(n, 1.0 / d.fs)
    amps, snrs = [], []
    for i in range(0, len(d.t) - n, n):
        if not d.playing[i:i + n].all() or d.speed[i] < 5:
            continue
        rp = d.rpm[i:i + n]
        if not np.isfinite(rp).all() or rp.std() / max(rp.mean(), 1.0) > 0.03 or rp.mean() < 900:
            continue
        f0 = rp.mean() / 30.0
        if f0 > freqs[-1] - 10:
            continue
        x = np.abs(np.fft.rfft((d.rec[i:i + n] - d.rec[i:i + n].mean()) * win))
        b = int(np.argmin(np.abs(freqs - f0)))
        peak = x[max(b - 1, 0):b + 2].max()
        side = np.median(np.r_[x[max(b - 12, 0):max(b - 3, 1)], x[b + 4:b + 13]])
        amps.append(peak * 2.0 / win.sum() / math.sqrt(2.0))
        snrs.append(20 * math.log10(max(peak, 1e-12) / max(side, 1e-12)))
    if len(amps) < 10:
        return None
    return float(np.median(amps)), float(np.median(snrs))


def _fit_evidence(d, r):
    """What the recording can say about the runtime fitted values (the STRENGTH fit and the wheel's dyn fit)."""
    raw = d.ch.get('game_ffb_raw')
    d.fit_windows = None
    st = ((d.header or {}).get('settings') or {}).get('learnt') or {}
    kept = st.get('rbr_strength')
    r.add('fit.rbr_strength_header', "RBR strength kept at the start (header)", '', 'R', float(kept) if isinstance(kept, (int, float)) else None,
          "not in the header")
    if raw is not None and len(raw) > 2000:
        top = float(np.percentile(np.abs(raw[:, 1]), 99.9))
        clip = float(np.mean(np.abs(raw[:, 1]) >= 0.98 * top)) if top > 0 else 0.0
        r.add('fit.raw_clip_share', "share of the game's raw force at its clip (99.9th percentile)", 'share', 'E', clip)
        # force over the game's force in 15 s windows of cornering: a fit applied mid-drive that lightens the wheel
        # shows as windows well below the drive's upper quartile (indicative: corners differ between windows)
        top_raw = float(np.percentile(np.abs(raw[:, 1]), 99.9))
        ratios, spans = [], []
        for a in np.arange(d.t[0], d.t[-1] - 15.0, 15.0):
            m = (d.t >= a) & (d.t < a + 15.0) & d.drive & (np.abs(d.rec) > 0.1)
            rm = (raw[:, 0] >= a) & (raw[:, 0] < a + 15.0) & (np.abs(raw[:, 1]) > 0.1 * top_raw)
            if m.sum() < 500 or rm.sum() < 150:
                continue
            ratios.append(float(np.median(np.abs(d.rec[m])) / (np.median(np.abs(raw[rm, 1])) / top_raw)))
            spans.append(float(a))
        if len(ratios) >= 4:
            base = float(np.percentile(ratios, 75))
            low = [(s_, x / base) for s_, x in zip(spans, ratios) if x < 0.85 * base]
            r.add('fit.low_windows', "15 s windows with force / game force under 85 % of the drive's upper quartile", 'count', 'E', float(len(low)))
            r.add('fit.low_window_min', "lowest window over that upper quartile (a fit lightening the wheel shows < 1)", 'x', 'E', min(x for x in ratios) / base)
            d.fit_windows = low
        else:
            d.fit_windows = []
    live, wheel = d.ch.get('ffb_dyn_live'), d.ch.get('ffb_dyn_wheel')
    if live is not None and wheel is not None and len(live) and len(wheel) and float(live[:, 3].max()) > 0:
        wf, wd = float(wheel[-1, 3]), float(wheel[-1, 1])
        lf, ld = float(np.median(live[:, 1])), float(np.median(live[:, 2]))
        r.add('fit.wheel_friction_written', "wheel fit: friction written (ffb_dyn_wheel)", 'driver units', 'R', wf)
        r.add('fit.wheel_friction_live', "wheel friction measured live (median ffb_dyn_live; units may differ from the written)", 'driver units', 'R', lf)
        r.add('fit.wheel_damping_written', "wheel fit: damping written (ffb_dyn_wheel)", 'driver units', 'R', wd)
        r.add('fit.wheel_damping_live', "wheel damping measured live (median ffb_dyn_live)", 'driver units', 'R', ld)


def steering_ratio(d):
    """Wheel degrees per unit of the game's steer (centre to full lock), by speed band and by wheel angle, with each
    bin's deviation from the whole drive's. Returns None with the reason when the wheel position is missing."""
    if d.wheel_deg is None:
        return None, "no wheel position recorded"
    if not np.isfinite(d.steer).any():
        return None, "the game sends no steering angle"
    fs = d.fs
    ok = d.playing & (d.speed > 3) & np.isfinite(d.steer)
    pos, st = np.nan_to_num(d.wheel_deg), np.nan_to_num(d.steer)
    lag = step_lag(pos, st, fs, mask=ok)
    if abs(lag) > 1e-9:
        st = np.interp(d.t + lag, d.t, st)             # st(t + lag) lines the game's steer up with the wheel
    pl, sl_ = lowpass(pos, fs, 2.0), lowpass(st, fs, 2.0)
    active = ok & (np.abs(sl_) > 0.02)

    def gain(m):
        if d.dt[m].sum() < MIN_S:
            return None
        return abs(float(np.sum(pl[m] * sl_[m]) / np.sum(sl_[m] ** 2)))
    whole = gain(active)
    if whole is None:
        return None, "too little steering to fit"
    out = {'whole': whole, 'lag_ms': lag * 1e3, 'speed': [], 'angle': []}
    for lo, hi in ((3, 10), (10, 20), (20, 30), (30, 45), (45, 200)):
        g = gain(active & (d.speed >= lo) & (d.speed < hi))
        out['speed'].append(("%g-%g m/s" % (lo, hi if hi < 200 else math.inf), g))
    for lo, hi in ((0, 30), (30, 90), (90, 180), (180, 720)):
        g = gain(active & (np.abs(pl) >= lo) & (np.abs(pl) < hi))
        out['angle'].append(("wheel %g-%g deg" % (lo, hi), g))
    for key in ('speed', 'angle'):
        out[key] = [(n, g, None if g is None else g / whole - 1.0) for n, g in out[key]]
    return out, None


# ---------------------------------------------------------------------------------------------------- comparison

def stale_fields(h_drive, h_ref):
    """The header fields that differ (or are unknown on one side) between the drive and its reference, as
    [(field, drive value, reference value)]. A field recorded on neither side is not listed; one recorded on only
    one side is, as unknown."""
    out = []
    for key in ('build',) + MAPPING_FIELDS + ('damping_written',):
        a, b = h_drive.get(key), h_ref.get(key)
        if a is None and b is None:
            continue
        if a != b:
            out.append((key, a, b))
    return out


def flag_measures(r, ref, tol=TOL, skip_nm=False):
    """[(key, value, reference value, relative difference)] of the measures more than `tol` off the reference."""
    out = []
    for key, item in r.items.items():
        a, b = item['value'], ref.value(key)
        if a is None or b is None or key.startswith(('time.', 'fit.')) or (skip_nm and key.endswith(('_nm', 'force_nm'))):
            continue
        scale = max(abs(a), abs(b))
        if scale < 1e-9:
            continue
        diff = (a - b) / max(abs(b), 0.2 * scale)
        if abs(diff) > tol:
            out.append((key, a, b, diff))
    return out


# ---------------------------------------------------------------------------------------------------- the report

def _fmt(v, unit):
    if v is None:
        return "-"
    if unit in ('FS', 'share', 'x', 'r'):
        return "%.3f" % v
    if unit in ('/min', 'mm', 'dB', 's', 'count'):
        return "%.1f" % v
    if unit == 'Nm' or unit == 'Nm/kN':
        return "%.3f" % v
    return "%.4g" % v


def report(d, ref, r, rr, flags, stale, steer, steer_ref, notes):
    lines = []
    w = lines.append
    w("FFB PARITY  drive %s  against reference %s" % (d.name, ref.name if ref else "(none)"))
    w("")
    w("FLAGS")
    if ref is None:
        w("  No reference drive: nothing to compare. The drive's own numbers are below.")
    else:
        if stale:
            w("  REFERENCE STALE: absolute levels are not comparable. Differing: " + "; ".join(
                "%s drive=%s ref=%s" % (k, 'not recorded' if a is None else a, 'not recorded' if b is None else b) for k, a, b in stale))
        else:
            w("  Reference current: build and mapping fields match (those recorded).")
        for key, a, b, diff in flags:
            it = r.items[key]
            w("  %+4.0f%%  %s: %s %s vs %s  [%s]" % (100 * diff, it['label'], _fmt(a, it['unit']), it['unit'], _fmt(b, it['unit']), it['kind']))
        if not flags:
            w("  No measure is more than %.0f %% off the reference." % (100 * TOL))
        gone = [it['label'] for k, it in r.items.items() if it['value'] is None and not k.startswith('time.')]
        if gone:
            w("  Too little data in the drive for %d measures (marked '-', reason in the table)." % len(gone))
    for n in notes:
        w("  NOTE " + n)
    w("")
    w("R = read from the recording, E = estimated (assumption in the label). Peak: drive %s %.2f Nm%s." % (
        d.peak_source, d.peak, "" if ref is None else ", reference %s %.2f Nm" % (ref.peak_source, ref.peak)))
    w("Newton's Fy_f uses the car table's mass (drive %.0f kg%s%s)." % (
        d.mass, "" if d.mass_known else " ASSUMED",
        "" if ref is None else ", reference %.0f kg%s" % (ref.mass, "" if ref.mass_known else " ASSUMED")))
    w("")
    w("%-3s %-86s %12s %12s  %s" % ("", "measure", "drive", "reference", "difference"))
    flagged = {f[0]: f[3] for f in flags}
    for key, it in r.items.items():
        a = it['value']
        b = None if rr is None else rr.value(key)
        mark = ""
        if key in flagged:
            mark = "<< %+.0f%%" % (100 * flagged[key])
        txt_a = _fmt(a, it['unit']) if a is not None else "-"
        txt_b = "" if rr is None else (_fmt(b, it['unit']) if b is not None else "-")
        w("%-3s %-86s %12s %12s  %s" % (it['kind'], (it['label'] + " [" + it['unit'] + "]")[:86], txt_a, txt_b, mark))
        if a is None and it['why']:
            w("      drive: %s" % it['why'])
        if b is None and rr is not None and key in rr.items and rr.items[key]['why']:
            w("      reference: %s" % rr.items[key]['why'])
    w("")
    w("STEERING RATIO  wheel degrees per unit of the game's steer (E: wheel position lags the packet, removed), flat to %.0f %%" % (100 * FLAT_TOL))
    for tag, s in (("drive", steer), ("reference", steer_ref)):
        if tag == "reference" and ref is None:
            continue
        data, why = s
        if data is None:
            w("  %s: %s" % (tag, why))
            continue
        bad = [(n, dev) for key in ('speed', 'angle') for n, g, dev in data[key] if dev is not None and abs(dev) > FLAT_TOL]
        w("  %s: whole drive %.1f deg per unit (steer lag %.0f ms)%s" % (
            tag, data['whole'], data['lag_ms'], "  NOT FLAT: " + ", ".join("%s %+.1f%%" % (n, 100 * x) for n, x in bad) if bad else "  flat"))
        for key in ('speed', 'angle'):
            w("    " + "  ".join("%s %s" % (n, "-" if g is None else "%.1f (%+.1f%%)" % (g, 100 * dev)) for n, g, dev in data[key]))
    w("")
    w("RUNTIME FITTED VALUES")
    for tag, dr in (("drive", d), ("reference", ref)):
        if dr is None:
            continue
        w("  %s %s: %s" % (tag, dr.name, _fit_text(dr)))
    return "\n".join(lines)


def _fit_text(d):
    parts = []
    st = ((d.header or {}).get('settings') or {}).get('learnt')
    if st:
        parts.append("header learnt " + ", ".join("%s=%s" % (k, st[k]) for k in sorted(st) if st[k] is not None))
    else:
        parts.append("no settings header")
    if d.fit_windows is None:
        parts.append("no game_ffb_raw channel to check a fit against: the log must be checked")
    elif d.fit_windows:
        parts.append("force / game force under 85 %% of the upper quartile in %d windows (%s: start s, ratio): a fit lightening the "
                     "wheel mid-drive is possible; check the log for 'STRENGTH applied' against its evidence" % (len(d.fit_windows), ", ".join("%.0fs:%.2f" % w for w in d.fit_windows[:6])))
    else:
        parts.append("force / game force steady over 30 s windows (no mid-drive fit change seen); the log is the only source "
                     "of which value was applied, check it for 'applied' against 'evidence'")
    return "; ".join(parts)


def run(drive_path, ref_path=None, peak=None):
    """(report text, dict for JSON)."""
    if MISSING:
        raise RuntimeError(MISSING)
    d = load_drive(drive_path, peak)
    if not d.ok:
        raise RuntimeError("%s: %s" % (drive_path, d.why))
    ref = None
    notes = []
    if ref_path:
        ref = load_drive(ref_path, peak)
        if not ref.ok:
            notes.append("reference %s unusable: %s" % (ref_path, ref.why))
            ref = None
    r = measure(d)
    rr = measure(ref) if ref else None
    hd = header_facts(d)
    hr = header_facts(ref) if ref else None
    stale = stale_fields(hd, hr) if ref else []
    flags = flag_measures(r, rr, skip_nm=bool(ref) and ref.peak == d.peak) if ref else []
    if d.peak_source == 'assumed' and (ref is None or ref.peak_source == 'assumed'):
        notes.append("the wheel peak is not recorded in either header: Nm figures assume %.1f Nm and are only comparable if both ran on the same wheel" % d.peak)
    if d.game and ref and ref.game == d.game:
        notes.append("same game on both sides")
    text = report(d, ref, r, rr, flags, stale, steering_ratio(d), steering_ratio(ref) if ref else None, notes)
    out = {'drive': d.name, 'reference': ref.name if ref else None,
           'headers': {'drive': hd, 'reference': hr}, 'stale': [list(s) for s in stale],
           'flags': [{'key': k, 'drive': a, 'reference': b, 'relative': x} for k, a, b, x in flags],
           'measures': {k: {**it, 'reference': None if rr is None else rr.value(k)} for k, it in r.items.items()}}
    return text, out
