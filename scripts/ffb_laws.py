"""The physics laws a game's force is checked against, and one recording's own constants (scripts/ffb-new-game.py).

The laws are fitted from ACR's tyres (the only game that sends Fy, Fz and Mz) and are about the tyre, not the car: the
pneumatic trail at no slip against the surface's grip, how it fades with the grip in use before the peak and with slip over
peak slip past it. Every quantity is defined the same way for every game, per 60 s stretch of a drive:
  r     front axle side force over front load (tyres where sent, else Newton's Fy_f over the static or sent load)
  mu    the top of the upper envelope (p80) of r against the front slip        (tools.envelope_fit)
  peak  the slip where the envelope first reaches 97 % of its top, per grip band of the drive, in the slip's own unit
  u     r / mu, the grip in use;   x  slip / peak
so a game is compared with the law at the same physical cause, never with another recording. Read only."""
import json
import os

import numpy as np

import ffb_tools as tools

LAWS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ffb_laws.json')
STRETCH_S = 60.0
FZ_MIN = 1500.0                 # N on the front axle: under it r says nothing
USE = (0.0, 0.5, 0.8, 0.9, 1.0, 99.0)           # u before the peak (x < 1)
PAST = (1.0, 1.3, 1.7, 2.5, 99.0)               # x from the peak on
NO_SLIP = (0.5, 0.6)            # u and x under these: the trail at no slip
FLAG = 0.15                     # a cell's median of rack / law outside 1 +- this is flagged ...
REACHED_S = 0.5                 # s in the envelope's top bin for a stretch to count as having reached the limit
PEAK_SHARE = 0.97               # the peak slip: the first bin whose envelope reaches this share of the top
MU_MAX = 2.0                    # a stretch's grip top over this is noise, not grip (a sanity bound: Forza's aero inflates Newton over the static load)
TOL_MAX = 0.5                   # ... and never wider than this, however far ACR's drives spread (a third of the law must flag)
MIN_S = 3.0                     # ... when it holds this many seconds
LOCK_FLAG = 0.15
GRIP_SPREAD = 0.3               # the grip must spread this far over a drive's stretches for a slope of trail against grip
PEAK_FLAG = 0.25
PROBE_KEYS = ('fzf', 'steer', 'vx', 'vy', 'rack_mu', 'gs_mu', 'rack_peak', 'lock')
EXTRA_CHANNELS = ('tyre_mz',)


def _num(v):
    return float(v) if v is not None and np.isfinite(v) else float('nan')


def probe(rack, s):
    """What the laws need beside simulate_stages' columns: the front load, the steering and velocity, the rack's own grip, peak
    slip and lock."""
    load = tools._pair(getattr(s, 'tyre_load', None))
    vel = s.vel if s.vel is not None and len(s.vel) > 1 else (None, None)
    grip = getattr(rack, '_grip', None)
    return {'fzf': load[0] + load[1] if load else float('nan'), 'steer': _num(getattr(s, 'steer', None)),
            'vx': _num(vel[0]), 'vy': _num(vel[1]), 'rack_mu': _num(getattr(rack, 'grip_mu', None)),
            'gs_mu': _num(grip.mu() if grip is not None else None), 'rack_peak': _num(getattr(rack, 'slip_peak', None)),
            'lock': _num(getattr(rack, 'lock', None))}


def _envelope(x, y, dt, bins, min_s=0.25):
    """(centres, p80 of y, seconds) over the slip bins holding at least `min_s` (and 15 samples); None under three bins."""
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    idx = np.digitize(x, bins) - 1
    need = max(15, min_s / max(dt, 1e-6))
    got = [(0.5 * (bins[j] + bins[j + 1]), float(np.percentile(y[idx == j], 80)), (idx == j).sum() * dt)
           for j in range(len(bins) - 1) if (idx == j).sum() >= need]
    return tuple(np.array(v) for v in zip(*got)) if len(got) >= 3 else None


def causes(cols, dt, bins=None):
    """Per sample: r, mu, peak, reached (the stretch's envelope has a top), u, x, the stretch index and the rack's pneumatic
    trail tp (m; its pneumatic kingpin part over Fy_f, NaN under tools.TRAIL_FYF_MIN)."""
    bins = tools.TYRE_BINS['rad'] if bins is None else bins
    t, slip, fyf = cols['t'], np.abs(cols['slip']), np.abs(cols['fyf'])
    n = len(t)
    static = cols['mass'] * cols['bl'] * tools.G             # NaN where the game sends no mass (ACR: the loads are sent)
    fz = np.where(np.isfinite(cols['fzf']), cols['fzf'], static)
    both = np.isfinite(fz) & np.isfinite(static)
    fz = np.where(both, np.maximum(fz, 0.5 * static), fz)   # (light fronts over a crest say nothing about the grip)
    with np.errstate(divide='ignore', invalid='ignore'):
        # (rows per kg, a game without a rack: mass 1, so the floor is the same share of a 1000 kg car's)
        floor = np.where(np.isfinite(cols['mass']) & (cols['mass'] < 10), FZ_MIN * cols['mass'] / 1000.0, FZ_MIN)
        r = np.where(fz > floor, fyf / fz, np.nan)
        seg = ((t - t[0]) // STRETCH_S).astype(int) if n else np.zeros(0, int)
        mu, peak, reached = np.full(n, np.nan), np.full(n, np.nan), np.zeros(n, bool)
        for g in np.unique(seg):
            m = seg == g
            env = _envelope(slip[m], r[m], dt, bins)
            if env is None:
                continue
            _cen, top, secs = env
            k = int(np.argmax(top))
            if not top[k] <= MU_MAX:
                print('  stretch %d: grip top %.2f over MU_MAX %.1f, left unreached' % (g, top[k], MU_MAX))
                continue
            mu[m] = top[k]
            # the limit was reached: the envelope stops rising (its top is not a still-rising last bin) and holds some time
            reached[m] = secs[k] >= REACHED_S and not (k == len(top) - 1 and top[k] > 1.03 * top[k - 1])
        # the peak slip per grip band, from all its stretches with a top together (one stretch's is a coarse bin)
        band = np.array([tools.mu_band(v) if np.isfinite(v) else -1 for v in mu])
        for bi in range(len(tools.MU_BAND_NAMES)):
            m = (band == bi) & reached
            env = _envelope(slip[m], r[m] / mu[m], dt, bins) if m.any() else None
            if env is not None:
                cen, top, _secs = env
                want = PEAK_SHARE * top.max()
                k = int(np.argmax(top >= want))
                # (between the bin centres: the crossing of the share, not the bin's centre)
                peak[band == bi] = cen[k] if k == 0 else cen[k - 1] + (cen[k] - cen[k - 1]) * (want - top[k - 1]) / (top[k] - top[k - 1])
        tp = cols['pneu'] / np.where(fyf >= tools.TRAIL_FYF_MIN, fyf, np.nan)
        return {'r': r, 'mu': mu, 'peak': peak, 'reached': reached, 'u': r / mu, 'x': slip / peak, 'seg': seg, 'tp': tp}


def stretch_points(c, min_n=200):
    """[(mu, peak, no-slip trail m or NaN, samples)] per stretch whose envelope reached a top."""
    out = []
    for g in np.unique(c['seg']):
        m = (c['seg'] == g) & c['reached']
        if not m.any():
            continue
        k = m & (c['u'] < NO_SLIP[0]) & (c['x'] < NO_SLIP[1]) & np.isfinite(c['tp'])
        out.append((float(c['mu'][m][0]), float(c['peak'][m][0]), float(np.median(c['tp'][k])) if k.sum() >= min_n else float('nan'),
                    int(m.sum())))
    return out


def _line(pts, spread=0.15):
    """(a, b, n) of tp0 = a + b mu through stretch points with a no-slip trail; b 0 with under three or a mu spread under `spread`."""
    p = np.array([(mu, tp, n) for mu, _pk, tp, n in pts if np.isfinite(tp)], float).reshape(-1, 3)
    if len(p) >= 3 and np.ptp(p[:, 0]) >= spread:
        b, a = np.polyfit(p[:, 0], p[:, 1], 1, w=np.sqrt(p[:, 2]))
        return float(a), float(b), len(p)
    return (float(np.median(p[:, 1])) if len(p) else float('nan')), 0.0, len(p)


def _cells(c, ok):
    """[(name, mask)]: the grip-in-use cells before the peak, then the slip-over-peak cells past it."""
    out = []
    for lo, hi in zip(USE, USE[1:]):
        out.append(('u %.1f-%s' % (lo, '%.1f' % hi if hi < 9 else 'up'), ok & (c['x'] < 1) & (c['u'] >= lo) & (c['u'] < hi)))
    for lo, hi in zip(PAST, PAST[1:]):
        out.append(('x %.1f-%s' % (lo, '%.1f' % hi if hi < 9 else 'up'), ok & (c['x'] >= lo) & (c['x'] < hi)))
    return out


def fit_laws(drives, names=()):
    """The laws from ACR drives [(cols, causes)]: tp0 = a + b mu over the stretches, then the median per cell of each sample's
    trail over its stretch's tp0 (per sample: ratios of cell medians are not trails), and the peak slip per grip band."""
    pts = [p for _cols, c in drives for p in stretch_points(c)]
    a, b, n = _line(pts)
    if not n or not b:
        raise ValueError('the ACR drives give no line of no-slip trail against grip (%d stretches with a top)' % n)
    mus = [p[0] for p in pts if np.isfinite(p[2])]
    fac, secs = {}, {}
    for cols, c in drives:
        ok = c['reached'] & np.isfinite(c['tp']) & np.isfinite(c['u']) & np.isfinite(c['x'])
        with np.errstate(invalid='ignore'):
            f = c['tp'] / (a + b * c['mu'])
        for name, m in _cells(c, ok):
            fac.setdefault(name, []).append(f[m])
    cells = {k: float(np.median(np.concatenate(v))) for k, v in fac.items() if sum(len(x) for x in v) >= 200}
    # how far one drive's own median sits from the law in each cell: the law is no firmer than that
    spread = {k: float(max([abs(np.median(x) / cells[k] - 1) for x in fac[k] if len(x) >= 200] or [0.0])) for k in cells}
    peaks = {}
    for mu, pk, _tp, _n in pts:
        peaks.setdefault(tools.MU_BAND_NAMES[tools.mu_band(mu)], []).append(pk)
    return {'tp0': [a, b], 'mu_range': [float(min(mus)), float(max(mus))], 'cells': cells, 'spread': spread, 'stretches': n,
            'peak_slip_rad': {k: float(np.median(v)) for k, v in peaks.items()}, 'sources': list(names),
            'note': 'ACR tyres: pneumatic trail (m) = (tp0[0] + tp0[1] mu) x cells[grip in use u before the peak, slip/peak x past it]'}


def load_laws(path=None):
    with open(path or LAWS_PATH) as f:
        return json.load(f)


def law_tp(laws, mu, u, x):
    """The law's pneumatic trail (m) per sample; mu held to the range the law was fitted over. NaN outside the cells."""
    a, b = laws['tp0']
    mu = np.clip(mu, *laws['mu_range'])
    c = {'u': np.asarray(u, float), 'x': np.asarray(x, float)}
    out = np.full(len(c['u']), np.nan)
    for name, m in _cells(c, np.isfinite(c['u']) & np.isfinite(c['x'])):
        if name in laws['cells']:
            out[m] = laws['cells'][name]
    return (a + b * mu) * out


def trail_check(laws, c, dt):
    """Rows (mu band, cell, seconds, rack mm, law mm, median of rack / law per sample, flagged, tolerance) over the stretches
    with a top. The tolerance: FLAG, or what ACR's own drives spread by in that cell where that is more, at most TOL_MAX."""
    law = law_tp(laws, c['mu'], c['u'], c['x'])
    ok = c['reached'] & np.isfinite(c['tp']) & np.isfinite(law)
    rows = []
    band = np.array([tools.mu_band(m) if np.isfinite(m) else -1 for m in c['mu']])
    for bi, bname in enumerate(tools.MU_BAND_NAMES):
        for name, m in _cells(c, ok & (band == bi)):
            secs = m.sum() * dt
            if secs < MIN_S:
                continue
            ratio = float(np.median(c['tp'][m] / law[m]))
            tol = min(max(FLAG, laws.get('spread', {}).get(name, 0.0)), TOL_MAX)
            rows.append((bname, name, secs, float(np.median(c['tp'][m])) * 1000, float(np.median(law[m])) * 1000, ratio,
                         abs(ratio - 1) > tol, tol))
    return rows


def grip_response(laws, c):
    """The rack's no-slip trail against the grip: {'a', 'b', 'n', 'mu': (lo, hi), 'at': {mu: (rack mm, law mm)}, 'verdict'}."""
    la, lb = laws['tp0']
    lo_ok, hi_ok = laws['mu_range'][0] - 0.2, laws['mu_range'][1] + 0.2
    pts = [p for p in stretch_points(c) if np.isfinite(p[2]) and lo_ok <= p[0] <= hi_ok]
    a, b, n = _line(pts, GRIP_SPREAD)
    res = {'a': a, 'b': b, 'n': n, 'mu': (min(p[0] for p in pts), max(p[0] for p in pts)) if pts else None, 'at': {}}
    if not n:
        res['verdict'] = 'no stretch with a grip top (within the law\'s grip range) and 200 samples at no slip: nothing to say'
        return res
    lo, hi = res['mu']
    for mu in sorted({round(lo, 2), round(hi, 2)}):
        k = min(max(mu, laws['mu_range'][0]), laws['mu_range'][1])
        res['at'][mu] = ((a + b * mu) * 1000, (la + lb * k) * 1000)
    off = any(abs(r / w - 1) > FLAG for r, w in res['at'].values())
    if not b:
        res['verdict'] = 'grip spread under %g: no slope (%d stretches, mu %.2f-%.2f)%s' % (
            GRIP_SPREAD, n, lo, hi, '  FLAG: level off' if off else '')
    else:
        flat = b < 0.5 * lb
        res['verdict'] = 'slope %.1f mm per unit of grip against the law\'s %.1f (%d stretches, mu %.2f-%.2f)%s' % (
            b * 1000, lb * 1000, n, lo, hi,
            '  FLAG: the trail does not follow the grip' if flat else ('  FLAG: level off' if off else ''))
    return res


def lock_fit(cols, wheelbase):
    """The road wheels' lock (rad per unit of steer) from the motion in steady driving (|a_y| under 3, body slip under 0.03,
    yaw acceleration under 0.5): steer = (L r / v) / lock + (K / lock) a_y, the K a_y term being the understeer (the axles' slip
    difference; without it understeer reads as a smaller lock: 'naive'). {'lock', 'signed', 'naive', 'K', 'n'} or None. Scales
    with the assumed wheelbase. On two RBR cars it gave the car table's lock within 4 %."""
    steer, vx, vy, yaw, ay = cols['steer'], cols['vx'], cols['vy'], cols['yaw'], cols['ay_s']
    with np.errstate(divide='ignore', invalid='ignore'):
        ok = np.isfinite(steer + vx + vy + yaw + ay) & (np.abs(vx) > 8) & (np.abs(ay) < 3) & (np.abs(vy / vx) < 0.03)
        ok &= np.abs(tools.yaw_accel(cols['t'], yaw, 0.1)) < 0.5
        if ok.sum() < 500:
            return None
        y = wheelbase * yaw[ok] / np.abs(vx[ok])
    sol = np.linalg.lstsq(np.column_stack([y, ay[ok]]), steer[ok], rcond=None)[0]
    naive = float(np.dot(y, y) / np.dot(y, steer[ok]))
    if not sol[0]:
        return None
    return {'lock': abs(1 / float(sol[0])), 'signed': 1 / float(sol[0]), 'naive': abs(naive), 'K': float(sol[1] / sol[0]) * np.sign(sol[0]),
            'n': int(ok.sum())}


def band_medians(c, values, dt, need_reached=True):
    """{mu band name: (median of values, seconds)} over the stretches with a top."""
    out = {}
    band = np.array([tools.mu_band(m) if np.isfinite(m) else -1 for m in c['mu']])
    for bi, name in enumerate(tools.MU_BAND_NAMES):
        m = (band == bi) & np.isfinite(values) & (c['reached'] | (not need_reached))
        if m.sum() * dt >= MIN_S:
            out[name] = (float(np.median(values[m])), float(m.sum() * dt))
    return out


def routes(share):
    """[(force stage, route, what it needs)] from the channels a capture carries ({channel: share of samples})."""
    has = lambda *ch: all(share.get(k, 0) > 0.5 for k in ch)      # noqa: E731
    out = []
    if has('tyre_fy'):
        out.append(('front side force', "the tyres' own Fy (ACR's route)", None))
    elif has('accel', 'yaw_rate'):
        out.append(('front side force', "Newton: factor m b/L (a_y + a yaw accel), ffb_forza.front_lateral", 'a mass source'))
    else:
        out.append(('front side force', 'NONE: no tyre Fy and no a_y with yaw rate', 'accel and yaw_rate in the decoder'))
    out.append(('front load', "the tyres' own Fz" if has('tyre_load') else 'static m b/L g', None if has('tyre_load') else 'a mass source'))
    if has('tyre_mz', 'tyre_fy'):
        out.append(('pneumatic trail', "the tyres' own Mz (ACR's route)", None))
    else:
        out.append(('pneumatic trail', 'the law: tp0(grip) x fade(grip in use, slip / peak slip)', 'grip and peak slip learnt live'))
    if has('tyre_slip_angle'):
        out.append(('front slip', 'tyre_slip_angle, radians', None))
    elif has('slip_angle'):
        out.append(('front slip', "the game's slip_angle, normalised (its peak is fitted below)", None))
    elif has('steer', 'vel', 'yaw_rate'):
        out.append(('front slip', 'kinematic: steer x lock - atan2(vy + a r, |vx|)', 'the lock (fitted below) or a car table'))
    else:
        out.append(('front slip', 'NONE', 'steer, vel and yaw_rate in the decoder'))
    if not has('tyre_fy'):
        mass = 'learnt from the power (ffb_forza.MassLearner)' if has('rpm', 'throttle', 'accel') and has('wheel_speed') \
            else 'a car table (car_data), keyed by the car channel' if has('car') else 'NONE'
        out.append(('mass', mass, 'tyre forces, the power or a table (the mass is not observable from the motion alone)'))
    out.append(('steering ratio', 'half the wheel range over the lock (fitted below); a car table where there is one', None))
    return out


def raw_cols(items, wheelbase, bl_of, factor):
    """simulate_stages' columns without a rack, for a game that has none yet: Newton's Fy_f per kg (or the tyres'), the game's
    slip or the kinematic one with the lock fitted from this drive. Returns (cols, slip unit, lock fit)."""
    rows = []
    for t, s in items:
        if not tools.sample_ok(s, 8.0) or s.yaw_rate is None or not np.isfinite(s.yaw_rate):
            continue
        fy, fz = tools._pair(getattr(s, 'tyre_fy', None)), tools._pair(getattr(s, 'tyre_load', None))
        sa, sn = tools._pair(getattr(s, 'tyre_slip_angle', None)), tools._pair(getattr(s, 'slip_angle', None))
        rows.append((t, s.accel[1], s.yaw_rate, s.vel[0], s.vel[1], _num(getattr(s, 'steer', None)), bl_of(s),
                     abs(fy[0] + fy[1]) if fy else np.nan, fz[0] + fz[1] if fz else np.nan,
                     0.5 * (abs(sa[0]) + abs(sa[1])) if sa else np.nan, 0.5 * (abs(sn[0]) + abs(sn[1])) if sn else np.nan,
                     s.speed))
    a = np.array(rows, float).reshape(-1, 12)
    cols = dict(zip(('t', 'ay_s', 'yaw', 'vx', 'vy', 'steer', 'bl', 'fyf', 'fzf', 'sa', 'sn', 'speed'), a.T))
    lock = lock_fit(cols, wheelbase)
    tyres = np.isfinite(cols['fyf']).mean() > 0.5 if len(a) else False
    if not tyres:
        rd = tools.yaw_accel(cols['t'], cols['yaw'], window=0.1)     # (0.03 s is two samples at 60 Hz: too noisy, as lock_fit found)
        cols['mass'] = np.ones(len(a))
        cols['fyf'] = factor * cols['bl'] * np.abs(cols['ay_s'] + wheelbase * (1 - cols['bl']) * rd)
        cols['fzf'] = np.full(len(a), np.nan)
    else:
        cols['mass'] = np.full(len(a), np.nan)
    if len(a) and np.isfinite(cols['sa']).mean() > 0.5:
        cols['slip'], unit = cols['sa'], 'rad'
    elif len(a) and np.isfinite(cols['sn']).mean() > 0.5:
        cols['slip'], unit = cols['sn'], 'norm'
    elif lock:
        front = wheelbase * (1 - cols['bl'])
        cols['slip'] = np.abs(cols['steer'] * lock['signed'] - np.arctan2(cols['vy'] + front * cols['yaw'], np.abs(cols['vx'])))
        unit = 'rad'
    else:
        cols['slip'], unit = np.full(len(a), np.nan), 'none'
    cols['pneu'] = np.full(len(a), np.nan)
    return cols, unit, lock


# ---- the trail in the grip RbrRack can see --------------------------------------------------------------------------------
# The law above speaks the drive's own grip (the top of the tyres' Fy/Fz envelope per stretch). RbrRack has no tyre forces: its
# grip is the held maximum of the smoothed Newton Fy_f / Fz (grip_mu), which sits lower than that top. So the rack's constants
# are refitted against its own estimator, run over ACR's drives: Newton's Fy_f from a_y and the yaw acceleration with the moving
# mass and GripScale's lateral factor, the same smoothing and held maximum (RbrRack._grip_trail itself), against ACR's tyre Mz / Fy.
RACK_USE = (0.0, 0.75, 0.85, 0.95, 1.05, 99.0)       # u bins before the peak (the rack's table has its points at their centres)
RACK_PAST = (1.0, 1.15, 1.5, 2.1, 3.0, 99.0)         # x bins from the peak on
RACK_MIN_N = 300                                      # samples a bin needs


def rack_estimates(cols):
    """What RbrRack's grip estimator reads over an ACR drive (cols from simulate_stages): per sample the rack's `mu` (held grip),
    `r` (smoothed Newton Fy_f / Fz), `u` = r / mu and `peak` (its learnt slip_peak, fed the tyres' slip), plus the Newton Fy_f
    `fyn`. Needs the drive's true mass (all four tyres' Fy over a_y); None without."""
    from oversteer import ffb_engine as fe, ffb_forza
    m = tools.acr_true_mass(cols)
    if not m:
        return None
    mass = m[0]
    t, bl = cols['t'], cols['bl']
    n = len(t)
    rd = tools.yaw_accel(t, cols['yaw'], window=float(np.median(np.diff(t))) if n > 1 else 0.03)
    rack, gs, lp = fe.RbrRack(), ffb_forza.GripScale(), ffb_forza.LowPass(fe.RbrRack.DERIVED_AY_HZ)
    out = {k: np.full(n, np.nan) for k in ('mu', 'r', 'u', 'peak', 'fyn')}
    last = None
    for i in range(n):
        dt = 0.0 if last is None else min(max(t[i] - last, 0.0), 0.1)
        last = t[i]
        gs.feed(t[i], cols['ay_s'][i], cols['speed'][i])
        ay = lp.step(t[i], min(max(cols['ay_s'][i], -ffb_forza.AY_MAX), ffb_forza.AY_MAX))
        fy = ffb_forza.front_lateral(mass, bl[i], ay, min(max(rd[i], -ffb_forza.RDOT_MAX), ffb_forza.RDOT_MAX),
                                     factor=ffb_forza.lateral_factor(gs.mu()))
        limit = bl[i] * mass * rack.FLOOR_AY
        fy = min(max(fy, -limit), limit)
        fz = cols['fzf'][i]
        if not np.isfinite(fz):
            continue
        a = cols['slip'][i]
        rack._grip_trail(abs(fy), max(fz, 0.5 * bl[i] * mass * ffb_forza.G), dt, cols['speed'][i], a if np.isfinite(a) else None)
        out['mu'][i], out['r'][i], out['peak'][i], out['fyn'][i] = rack.grip_mu, rack._grip_r, rack.slip_peak, abs(fy)
    out['u'] = out['r'] / out['mu']
    return out


def fit_rack_form(drives):
    """RbrRack's pneumatic-trail constants from ACR drives [(cols, causes)], in the rack's own grip estimate. Per stretch with a
    no-slip trail: the median of the rack's mu against the trail there gives tp0(mu) = B mu^N (log fit) and a + b mu (line), with
    the sums of squared relative residuals; then the median per bin of each sample's trail over the fitted tp0(mu) in the rack's
    use u (before the drive's peak) and the drive's x (past it). Returns a dict, None when no drive has a true mass."""
    pts, rows = [], []
    for cols, c in drives:
        est = rack_estimates(cols)
        if est is None:
            continue
        rows.append((cols, c, est))
        for g in np.unique(c['seg']):
            m = (c['seg'] == g) & c['reached']
            k = m & (est['u'] < NO_SLIP[0]) & (c['x'] < NO_SLIP[1]) & np.isfinite(c['tp']) & np.isfinite(est['mu'])
            if k.sum() >= 200:
                pts.append((float(np.median(est['mu'][k])), float(np.median(c['tp'][k])), float(np.median(c['mu'][k])),
                            float(np.median(est['peak'][m])), float(c['peak'][m][0]), int(k.sum())))
    if len(pts) < 3:
        return None
    p = np.array(pts)
    w = np.sqrt(p[:, 5])
    n_pow, lb = np.polyfit(np.log(p[:, 0]), np.log(p[:, 1]), 1, w=w)
    b_pow = float(np.exp(lb))
    slope, icpt = np.polyfit(p[:, 0], p[:, 1], 1, w=w)

    def rel(fit):
        return float(np.sqrt(np.mean(((fit - p[:, 1]) / p[:, 1]) ** 2)))
    use, past = {}, {}
    for cols, c, est in rows:
        ok = c['reached'] & np.isfinite(c['tp']) & np.isfinite(est['mu']) & np.isfinite(c['x'])
        with np.errstate(invalid='ignore'):
            f = c['tp'] / (b_pow * est['mu'] ** n_pow)
        for lo, hi in zip(RACK_USE, RACK_USE[1:]):
            use.setdefault((lo, hi), []).append(f[ok & (c['x'] < 1) & (est['u'] >= lo) & (est['u'] < hi)])
        for lo, hi in zip(RACK_PAST, RACK_PAST[1:]):
            past.setdefault((lo, hi), []).append(f[ok & (c['x'] >= lo) & (c['x'] < hi)])

    def med(d):
        return {k: (float(np.median(np.concatenate(v))), int(sum(len(x) for x in v))) for k, v in d.items() if sum(len(x) for x in v) >= RACK_MIN_N}
    return {'B': b_pow, 'N': float(n_pow), 'line': [float(icpt), float(slope)], 'rel_pow': rel(b_pow * p[:, 0] ** n_pow),
            'rel_line': rel(icpt + slope * p[:, 0]), 'stretches': [tuple(float(x) for x in r[:5]) for r in pts],
            'use': med(use), 'past': med(past), 'mu_ratio': float(np.median(p[:, 0] / p[:, 2])),
            'peak_ratio': float(np.median(p[:, 3] / p[:, 4]))}
