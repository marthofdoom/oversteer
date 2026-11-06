"""The library behind the one-off FFB analyses that became tools: scripts/ffb-sim-match.py, ffb-wheel-model.py,
calib-check.py and new-game-check.py. Nothing here touches a wheel, the proxy, a game or the telemetry database; user
files (stored calibration, runs, profiles, captures) are only read."""
import configparser
import contextlib
import glob
import gzip
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffb_situations as sit  # noqa: E402

FLATPAK = os.path.expanduser('~/.var/app/io.github.berarma.Oversteer')
DEFAULT_PEAK = 3.08                     # Nm: wheel_torque.DEFAULT_PEAK_NM, the G29 class an unknown wheel is taken as
DEFAULT_RADIUS = 0.1395                 # m: the rim radius of the hanging-weight check
G = 9.80665


def config_dir():
    """The app's config folder: the Flatpak's when it exists, else $XDG_CONFIG_HOME/oversteer."""
    flat = os.path.join(FLATPAK, 'config', 'oversteer')
    if os.path.isdir(flat):
        return flat
    return os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'oversteer')


def captures_dir():
    flat = os.path.join(FLATPAK, 'data', 'oversteer', 'captures')
    if os.path.isdir(flat):
        return flat
    return os.path.join(os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share'), 'oversteer', 'captures')


def capture_path(name):
    """A capture by name (with or without .ovcap.gz) in the captures folder, or a full path."""
    if os.path.isabs(name):
        return name
    return os.path.join(captures_dir(), name if name.endswith('.ovcap.gz') else name + '.ovcap.gz')


# --- ffb-sim-match -------------------------------------------------------------------------------------------------

class NoRack(Exception):
    """The game has no rack class yet (or could not be told from the capture)."""


# game key (ffb_parity.game_of) -> (rack class name in ffb_engine, the game's profile, power class when the profile has none)
RACKS = {'acr': ('RackForce', 'AC Rally.ini', 'rally'),
         'rbr': ('RbrRack', 'RBR.ini', 'rally'),
         'forza-fh': ('ForzaRack', 'Forza.ini', 'street')}


def rack_for(game, name=''):
    if game is None:
        raise NoRack("%s: could not tell which game this capture is from (no decodable packets)" % (name or 'capture'))
    if game not in RACKS:
        raise NoRack("%s is game '%s': no rack for it yet (have: %s); the force code for it is still to be written in "
                     "oversteer/ffb_engine.py" % (name or 'capture', game, ', '.join(sorted(RACKS))))
    return RACKS[game]


def profile_power_class(game, config=None):
    """The countersteer_power_class of the game's profile in the user's profiles, else the default for the game."""
    _rack, profile, default = rack_for(game)
    path = os.path.join(config or config_dir(), 'profiles', profile)
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read(path)
        value = cp['DEFAULT'].get('countersteer_power_class', '').strip().lower()
    except (configparser.Error, OSError):
        value = ''
    return value if value in ('rally', 'street', 'off') else default


def sample_ok(s, min_speed):
    return bool(s.vel and s.accel and s.vel[0] and (s.speed or 0.0) >= min_speed)


def situation_key(s):
    """The ffb-match situation of a sample: lateral acceleration, body slip, speed and pedals."""
    brake, throttle = s.brake or 0.0, s.throttle or 0.0
    return (int(sit.band(sit.AY, abs(s.accel[1]))), int(sit.band(sit.SLIP, abs(np.arctan2(s.vel[1], abs(s.vel[0]))))),
            int(sit.band(sit.SPEED, s.speed)), 2 if brake > 0.1 else (1 if throttle > 0.1 else 0))


def sample_spacing(times):
    """The capture's real sample spacing (s): the median step of the first 2000 samples."""
    t = np.asarray(times[:2000], float)
    return float(np.median(np.diff(t))) if len(t) > 1 else 0.0


def _build_rack(game, power_class, peak=None):
    """(rack, the rack's fresh trail/ratio/mass, feed(t, s)): today's rack of the game with the aligning spring off."""
    from oversteer import ffb_carprops as cp, ffb_engine as fe, wheel_torque as wt
    cls_name = rack_for(game)[0]
    cp.set_power_class(power_class)
    rack = getattr(fe, cls_name)()
    fresh = {k: getattr(rack, k, None) for k in ('trail', 'ratio', 'mass')}
    rack.wheel_peak = peak or wt.DEFAULT_PEAK_NM
    rack.align_on = False
    rbr = game == 'rbr'
    if rbr:
        rack.reset(None)

    def feed(t, s):
        if rbr:
            rack.set_car(s.car)
            rack.ready = True
        rack.feed(t, s, None if game != 'acr' or s.game_ffb is None else s.game_ffb * fe.FULL)

    return rack, fresh, feed


def simulate(items, game, power_class, peak=None):
    """Today's rack over recorded samples [(t, Sample)], two passes (the first lets the rack learn), whole force
    (align_on off). Returns {'cells': {key: [share of full force]}, 'secs': {key: seconds}, 'info': {...}}.
    Call inside ffb_score._sandbox() so no user file is read or written."""
    from oversteer import ffb_engine as fe
    rack, fresh, feed = _build_rack(game, power_class, peak)
    for t, s in items:
        feed(t, s)
    cells, scored = {}, 0
    car = None
    for t, s in items:
        feed(t, s)
        car = s.car or car
        if not rack.ready or not sample_ok(s, sit.SPEED[0]):
            continue
        scored += 1
        cells.setdefault(situation_key(s), []).append(abs(rack.force(s)) / fe.FULL)
    dt = sample_spacing([t for t, _ in items])
    learnt = {k: getattr(rack, k, None) for k in ('trail', 'ratio', 'mass')}
    return {'cells': cells, 'secs': {k: len(v) * dt for k, v in cells.items()},
            'info': {'game': game, 'class': power_class, 'samples': len(items), 'scored': scored, 'dt': dt,
                     'learnt': learnt, 'default': fresh, 'car': car, 'peak': rack.wheel_peak}}


def info_lines(name, info):
    """The header lines of one simulated capture, with plain warnings for an unknown car or a value left at its default."""
    learnt = ', '.join('%s %s' % (k, ('%.4g' % v) if v is not None else '-') for k, v in info['learnt'].items())
    lines = ['%s: game %s, power class %s, %d samples, %d scored, spacing %.1f ms, wheel peak %.2f Nm' % (
        name, info['game'], info['class'], info['samples'], info['scored'], info['dt'] * 1e3, info['peak']),
        '    learnt: %s; car %s' % (learnt, str(info['car'])[:40] if info['car'] else 'unknown')]
    if not info['car'] or str(info['car']).endswith('/unknown'):
        lines.append('    WARNING: the car is unknown: the car-specific values are the defaults')
    for k, v in info['learnt'].items():
        if k != 'trail' and v is not None and info['default'].get(k) is not None and abs(v - info['default'][k]) < 1e-12:
            lines.append('    WARNING: %s equals the default %.4g (not learnt from this drive)' % (k, v))
    if not info['scored']:
        lines.append('    WARNING: no sample was scored (rack never ready, or no moving data)')
    return lines


def pool_sims(sims):
    """Merge simulated captures into one set of cells and seconds."""
    cells, secs = {}, {}
    for sim in sims:
        for k, v in sim['cells'].items():
            cells.setdefault(k, []).extend(v)
            secs[k] = secs.get(k, 0.0) + sim['secs'][k]
    return cells, secs


def sim_match(a_names, b_names, class_a=None, class_b=None, peak=None, min_s=1.0, loader=None, game_of=None):
    """Run ffb-sim-match. `loader(path)` gives [(t, Sample)] (default: the capture's decodable samples) and `game_of(path)`
    the game key. Returns (header lines, rows, summary line). Raises NoRack."""
    import ffb_benchmark as fb
    import ffb_parity as fp
    import ffb_score as fs
    loader = loader or (lambda path: list(fb._walk(path)[1]))
    game_of = game_of or fp.game_of
    lines, sides = [], []
    with fs._sandbox():
        for tag, names, cls in (('A', a_names, class_a), ('B', b_names, class_b)):
            sims = []
            for name in names:
                path = capture_path(name)
                game = game_of(path)
                rack_for(game, os.path.basename(path)[:15])
                power = cls or profile_power_class(game)
                sim = simulate(loader(path), game, power, peak)
                lines += [tag + '  ' + ln for ln in info_lines(os.path.basename(path)[:15], sim['info'])]
                sims.append(sim)
            sides.append(pool_sims(sims))
    rows = sit.compare(sides[0][0], sides[1][0], min_s, sides[0][1], sides[1][1])
    return lines, rows, sit.summary(rows)[0]


# --- ffb-force-curve --------------------------------------------------------------------------------------------------

def _pair(v):
    """(a, b) floats of a per-wheel tuple's first two entries, or None."""
    if v is None or len(v) < 2:
        return None
    a, b = v[0], v[1]
    if a is None or b is None or not (np.isfinite(a) and np.isfinite(b)):
        return None
    return float(a), float(b)


def _slip_acr(s, rack):
    p = _pair(s.tyre_slip_angle)
    return None if p is None else 0.5 * (abs(p[0]) + abs(p[1]))


def _slip_forza(s, rack):
    p = _pair(s.slip_angle)
    return None if p is None else 0.5 * (abs(p[0]) + abs(p[1]))


def _slip_rbr(s, rack):
    a = rack._slip(s)
    return None if a is None or not np.isfinite(a) else abs(a)


def _norm_rbr():
    from oversteer import ffb_engine as fe
    return fe.RbrRack.DERIVED_SLIP_PEAK        # read at use, so --set reaches it


# The normalised slip theta, per game, in one place: (raw slip of a sample, what it is divided by, a text for the header).
# The divisor: a number, a function giving one (read when the drive is binned), or 'stretch' (ACR: the peak found per
# 60 s stretch of the drive from the tyres' Fy / Fz, acr_peaks).
THETA_TABLE = {
    'acr': (_slip_acr, 'stretch', "mean |tyre_slip_angle| of the fronts over the per-stretch peak (p80 of Fy/Fz reaching 97 % of the top)"),
    'forza-fh': (_slip_forza, 0.7, "mean |slip_angle| of the fronts over 0.7"),
    'rbr': (_slip_rbr, _norm_rbr, "|RbrRack._slip| over RbrRack.DERIVED_SLIP_PEAK"),
}
FYF_TEXT = {'acr': "the tyres' |Fy_FL + Fy_FR|", 'forza-fh': "the front_lateral value ForzaRack._front used", 'rbr': "RbrRack.fy_f"}
STRETCH_S = 60.0
ACR_PEAK_BINS = np.array([.005, .01, .02, .03, .05, .07, .1, .13, .17, .22, .3, .4, .6, 1])
ACR_PEAK_FZ = 800.0                     # N: wheels carrying less say nothing about the grip
ACR_PEAK_MIN = 800                      # wheel samples a stretch needs


def acr_peaks(t, sa, fy, fz):
    """The peak slip (rad) per sample, from its 60 s stretch of the drive (calib-check's item3a): the slip bin where the p80 of
    Fy/Fz first reaches 97 % of the stretch's top. `sa`, `fy`, `fz` are (n, 2) arrays of |slip angle|, |Fy| and Fz of the
    front wheels. A stretch with too little data takes the median of the others'; NaN when none has any."""
    t = np.asarray(t, float)
    seg = ((t - t[0]) // STRETCH_S).astype(int) if len(t) else np.zeros(0, int)
    peak = {}
    bs = ACR_PEAK_BINS
    for g in np.unique(seg):
        m = seg == g
        x, f, z = sa[m].ravel(), fy[m].ravel(), fz[m].ravel()
        ok = z > ACR_PEAK_FZ
        x, mu = x[ok], f[ok] / z[ok]
        if len(x) < ACR_PEAK_MIN:
            continue
        vals = np.full(len(bs) - 1, np.nan)
        for j in range(len(bs) - 1):
            k = (x >= bs[j]) & (x < bs[j + 1])
            if k.sum() >= 15:
                vals[j] = np.percentile(mu[k], 80)
        if np.isnan(vals).all():
            continue
        j = int(np.argmax(vals >= .97 * np.nanmax(vals)))
        peak[g] = (bs[j] + bs[j + 1]) / 2
    fallback = float(np.median(list(peak.values()))) if peak else float('nan')
    return np.array([peak.get(g, fallback) for g in seg]) if len(seg) else np.zeros(0)


def _theta(game, cols):
    """theta per sample of a simulated drive (cols from simulate_rows)."""
    _fn, norm, _text = THETA_TABLE[game]
    slip = np.asarray(cols['slip'], float)
    if norm == 'stretch':
        peak = acr_peaks(cols['t'], np.column_stack([cols['sa0'], cols['sa1']]),
                         np.column_stack([cols['fy0'], cols['fy1']]), np.column_stack([cols['fz0'], cols['fz1']]))
        return slip / peak
    return slip / (norm() if callable(norm) else norm)


def simulate_rows(items, game, power_class, peak=None):
    """Today's rack over the samples as simulate does (two passes, align_on off), one row per scored sample: the rim force in
    Nm (share x peak), the front axle's lateral force Fyf (N) and the raw front slip, as the game's rack believes them:
      ACR  |tyre_fy[0] + tyre_fy[1]|, the game's own (with the columns for Newton's check and the peak per stretch);
      Forza  the value ffb_forza.front_lateral gave ForzaRack._front for the sample (recorded by wrapping the call);
      RBR  rack.fy_f, what RbrRack._derive made of the same Newton route.
    Returns {'cols': {name: array}, 'theta': array, 'info': {...}, 'mass': (kg or None, text)}. Call inside _sandbox()."""
    from oversteer import car_data, ffb_engine as fe, ffb_forza
    rack, fresh, feed = _build_rack(game, power_class, peak)
    slip_fn = THETA_TABLE[game][0]
    last = {}
    orig = ffb_forza.front_lateral
    if game == 'forza-fh':
        def recorder(*a, **k):
            last['v'] = orig(*a, **k)
            return last['v']
        ffb_forza.front_lateral = recorder
    names = ('t', 'nm', 'fyf', 'slip', 'ay', 'yaw', 'bl', 'fysum', 'sa0', 'sa1', 'fy0', 'fy1', 'fz0', 'fz1')
    cols = {k: [] for k in names}
    car, scored = None, 0
    try:
        for t, s in items:
            feed(t, s)
        for t, s in items:
            feed(t, s)
            car = s.car or car
            if not rack.ready or not sample_ok(s, sit.SPEED[0]):
                continue
            last['v'] = None
            force = rack.force(s)
            slip = slip_fn(s, rack)
            if slip is None:
                continue
            extra = {}
            if game == 'acr':
                fy, fz, sa = _pair(s.tyre_fy), _pair(s.tyre_load), _pair(s.tyre_slip_angle)
                if fy is None or fz is None or sa is None or s.yaw_rate is None or not np.isfinite(s.yaw_rate):
                    continue
                fyf = abs(fy[0] + fy[1])
                extra = {'fysum': fy[0] + fy[1], 'sa0': abs(sa[0]), 'sa1': abs(sa[1]), 'fy0': abs(fy[0]), 'fy1': abs(fy[1]),
                         'fz0': fz[0], 'fz1': fz[1]}
            elif game == 'forza-fh':
                if last['v'] is None:
                    continue
                fyf = abs(last['v'])
            else:
                fyf = getattr(rack, 'fy_f', None)
                if fyf is None or not np.isfinite(fyf):
                    continue
                fyf = abs(fyf)
            scored += 1
            cols['t'].append(t)
            cols['nm'].append(abs(force) / fe.FULL * rack.wheel_peak)
            cols['fyf'].append(fyf)
            cols['slip'].append(slip)
            cols['ay'].append(s.accel[1])
            cols['yaw'].append(s.yaw_rate if s.yaw_rate is not None else float('nan'))
            cols['bl'].append(ffb_forza.front_share(getattr(s, 'drivetrain', None)))
            for k, v in extra.items():
                cols[k].append(v)
        learnt = {k: getattr(rack, k, None) for k in ('trail', 'ratio', 'mass')}
        entry = car_data.entry(rack.car) if game == 'rbr' and getattr(rack, 'car', None) else None
    finally:
        ffb_forza.front_lateral = orig
    cols = {k: np.array(v, float) for k, v in cols.items() if v}
    dt = sample_spacing([t for t, _ in items])
    info = {'game': game, 'class': power_class, 'samples': len(items), 'scored': scored, 'dt': dt, 'learnt': learnt,
            'default': fresh, 'car': car, 'peak': rack.wheel_peak}
    if game == 'forza-fh':
        m = learnt['mass']
        default = fresh['mass'] is not None and abs(m - fresh['mass']) < 1e-9
        mass = (m, 'ForzaRack mass %.0f kg, %s' % (m, 'the default (not learnt)' if default else 'learnt'))
    elif game == 'rbr':
        m = entry.get('mass_kg') if entry else None
        mass = (m or rack.FLOOR_MASS, 'RbrRack mass %.0f kg, %s' % (m or rack.FLOOR_MASS, 'from the car table' if m else 'the default FLOOR_MASS (car not in the table)'))
    else:
        mass = (None, 'the mass is not sent; solved below')
    theta = _theta(game, cols) if scored else np.zeros(0)
    return {'cols': cols, 'theta': theta, 'info': info, 'mass': mass}


def newton_check(cols, bins=sit.FYF, rdot_window=0.03):
    """ACR: Newton's Fy_f (ffb_forza.front_lateral with unit mass, the car's b/L from the drivetrain, and the yaw acceleration
    rdot, a central difference over +-rdot_window s of the yaw rate) against the tyres' Fy_f (signed sum of the fronts), with the
    one best mass by least squares, with and without rdot. ACR's rack has no rdot of its own. Returns (lines, mass with rdot)."""
    from oversteer import ffb_forza
    t, ay, yaw, bl, fy = (cols[k] for k in ('t', 'ay', 'yaw', 'bl', 'fysum'))
    if len(t) < 100:
        return ['  too few samples'], None
    dt = max(float(np.median(np.diff(t))), 1e-4)
    k = max(int(round(rdot_window / dt)), 1)
    n = len(t)
    lo, hi = np.clip(np.arange(n) - k, 0, n - 1), np.clip(np.arange(n) + k, 0, n - 1)
    span = t[hi] - t[lo]
    good = (span > 0) & (span < 4 * rdot_window)
    rd = np.zeros(n)
    rd[good] = (yaw[hi] - yaw[lo])[good] / span[good]
    out, result, final = [], None, None
    for name, di in (('ay only', 0.0), ('ay and rdot', ffb_forza.DI)):
        x = np.array([ffb_forza.front_lateral(1.0, b, a, r, di=di) for b, a, r in zip(bl, ay, rd)])
        m = float(np.dot(fy, x) / np.dot(x, x))
        sign = ''
        if m < 0:
            m, x, sign = -m, -x, ' (tyre Fy has the opposite sign to ay)'
        newton = m * x
        err = float(np.sqrt(np.mean((newton - fy) ** 2)))
        rms = float(np.sqrt(np.mean(fy ** 2)))
        out.append('  %-12s best mass %.0f kg%s; r %.3f; rms error %.0f N of rms Fy_f %.0f N (%.1f %%)' % (
            name, m, sign, float(np.corrcoef(newton, fy)[0, 1]), err, rms, err / rms * 100))
        if di:
            result, final = m, (np.abs(newton), np.abs(fy))
    out.append("  with rdot, by the tyres' |Fy_f| band (N): median Newton | median tyres | Newton / tyres | s")
    i = sit.band(bins, final[1])
    for j in range(len(bins) - 1):
        sel = i == j
        if sel.sum():
            nn, tt = float(np.median(final[0][sel])), float(np.median(final[1][sel]))
            out.append('  %-10s %8.0f %8.0f %8.2f %7.1f' % (sit.curve_label(bins, j), nn, tt, nn / tt if tt else float('nan'), sel.sum() * dt))
    return out, result


def pool_rows(sims):
    """Per-sample arrays nm, fyf, theta, dt of simulated captures, joined."""
    out = {k: [] for k in ('nm', 'fyf', 'theta', 'dt')}
    for sim in sims:
        n = len(sim['theta'])
        if not n:
            continue
        out['nm'].append(sim['cols']['nm'])
        out['fyf'].append(sim['cols']['fyf'])
        out['theta'].append(sim['theta'])
        out['dt'].append(np.full(n, sim['info']['dt']))
    return {k: np.concatenate(v) if v else np.zeros(0) for k, v in out.items()}


TRAIL_FYF_MIN = 300.0                   # N: under this Nm / Fyf means nothing


def trail_lines(a, b):
    """Median Nm per unit of Fyf (mm, the effective trail through the mapping) by theta band, A and B, samples with Fyf over
    TRAIL_FYF_MIN."""
    def per(side):
        out = {}
        ok = (side['fyf'] >= TRAIL_FYF_MIN) & np.isfinite(side['theta']) & np.isfinite(side['nm'])
        j = sit.band(sit.THETA, side['theta'])
        for k in range(len(sit.THETA) - 1):
            m = ok & (j == k)
            if m.any():
                out[k] = (float(np.median(side['nm'][m] / side['fyf'][m] * 1000.0)), float(side['dt'][m].sum()))
        return out
    pa, pb = per(a), per(b)
    lines = ['effective trail, median Nm per unit of Fyf (mm), samples with Fyf >= %.0f N' % TRAIL_FYF_MIN,
             '%-8s | %8s %6s | %8s %6s | %8s %7s' % ('theta', 'A mm', 's A', 'B mm', 's B', 'A-B mm', '% of B')]
    for k in range(len(sit.THETA) - 1):
        if k not in pa and k not in pb:
            continue
        x, y = pa.get(k), pb.get(k)

        def cell(v):
            return '%8.3f %6.1f' % v if v else '%8s %6s' % ('.', '.')
        diff = ('%+8.3f %7.1f' % (x[0] - y[0], (x[0] - y[0]) / y[0] * 100.0)) if x and y and y[0] > 0 else '%8s %7s' % ('.', '.')
        lines.append('%-8s | %s | %s | %s' % (sit.curve_label(sit.THETA, k), cell(x), cell(y), diff))
    return lines


def _resolve(name):
    """(owner, attribute, value) of a dotted constant: 'ffb_forza.TP_S_HALF' (a module of oversteer), 'RbrRack.DERIVED_SLIP_PEAK'
    (a class of ffb_engine) or 'ffb_engine.RbrRack.X'. ValueError for a name that does not exist or is not a number."""
    import importlib
    import types
    parts = name.split('.')
    if len(parts) < 2 or not all(parts):
        raise ValueError("--set %s: want MODULE.NAME or CLASS.NAME (like ffb_forza.TP_S_HALF)" % name)
    try:
        obj, rest = importlib.import_module('oversteer.' + parts[0]), parts[1:]
    except ImportError:
        from oversteer import ffb_engine
        if not hasattr(ffb_engine, parts[0]):
            raise ValueError("--set %s: no module oversteer.%s and no class %s in ffb_engine" % (name, parts[0], parts[0]))
        obj, rest = ffb_engine, parts
    for p in rest[:-1]:
        if not hasattr(obj, p):
            raise ValueError("--set %s: %s has no %s" % (name, getattr(obj, '__name__', obj), p))
        obj = getattr(obj, p)
        if not isinstance(obj, (types.ModuleType, type)):
            raise ValueError("--set %s: %s is not a module or class" % (name, p))
    attr = rest[-1]
    if not hasattr(obj, attr):
        raise ValueError("--set %s: %s has no %s" % (name, getattr(obj, '__name__', obj), attr))
    value = getattr(obj, attr)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("--set %s: %s is not a number (it is %s)" % (name, attr, type(value).__name__))
    return obj, attr, value


def cache_warnings(owner, attr, old):
    """Places that hold their own copy of a constant the override does not reach: another module or class with the same name and
    value, and a function whose default for a parameter of that name was fixed when it was defined."""
    import inspect
    out = []
    for modname, mod in sorted(sys.modules.items()):
        if not modname.startswith('oversteer.') or mod is None:
            continue
        for k, v in list(vars(mod).items()):
            if k == attr and v is not owner and not isinstance(v, bool) and v == old:
                out.append('%s.%s holds its own copy (%r): not reached' % (modname, k, v))
            if inspect.isclass(v) and v.__module__ == modname and v is not owner and attr in vars(v) and vars(v)[attr] == old:
                out.append('class %s.%s holds its own copy (%r): not reached' % (modname, v.__name__, old))
            if inspect.isfunction(v) and v.__module__ == modname:
                for p in inspect.signature(v).parameters.values():
                    if p.name.lower() == attr.lower() and p.default == old:
                        out.append('%s.%s() has %s=%r fixed as a default: not reached' % (modname, k, p.name, old))
    return out


@contextlib.contextmanager
def overrides(specs):
    """Temporarily set module or class constants given as 'NAME=VALUE' (NAME dotted: ffb_forza.TP_S_HALF, RbrRack.DERIVED_SLIP_PEAK);
    yields the header lines (what was set, and any copy the setting does not reach). Everything is validated before anything is
    set, and restored on exit. ValueError for a bad spec, a name that does not exist or one that is not a number."""
    todo = []
    for spec in specs or ():
        name, eq, text = spec.partition('=')
        if not eq:
            raise ValueError("--set %s: want NAME=VALUE" % spec)
        owner, attr, old = _resolve(name.strip())
        try:
            value = float(text)
        except ValueError:
            raise ValueError("--set %s: %r is not a number" % (spec, text))
        if not np.isfinite(value):
            raise ValueError("--set %s: %r is not finite" % (spec, text))
        if isinstance(old, int) and value == int(value):
            value = int(value)
        todo.append((name.strip(), owner, attr, old, value))
    saved, lines = [], []
    try:
        for name, owner, attr, old, value in todo:
            inherited = isinstance(owner, type) and attr not in vars(owner)
            saved.append((owner, attr, old, inherited))
            setattr(owner, attr, value)
            lines.append('SET %s = %r (was %r)%s' % (name, value, old, (' [inherited: set on %s only]' % owner.__name__) if inherited else ''))
            lines += ['    WARNING: ' + w for w in cache_warnings(owner, attr, old)]
        yield lines
    finally:
        for owner, attr, old, inherited in reversed(saved):
            if inherited:
                delattr(owner, attr)
            else:
                setattr(owner, attr, old)


def force_curve(a_names, b_names, class_a=None, class_b=None, peak=None, min_s=1.0, sets=(), loader=None, game_of=None):
    """Run ffb-force-curve. Returns {'head', 'summary', 'trail', 'newton', 'rows', 'knee_nm'} (lists of lines; rows are
    ffb_situations.curve_rows). Raises NoRack, ValueError (a bad --set)."""
    import ffb_benchmark as fb
    import ffb_parity as fp
    import ffb_score as fs
    from oversteer import wheel_torque as wt
    loader = loader or (lambda path: list(fb._walk(path)[1]))
    game_of = game_of or fp.game_of
    head, newton, sides = [], [], []
    with fs._sandbox(), overrides(sets) as set_lines:
        head += set_lines
        for tag, names, cls in (('A', a_names, class_a), ('B', b_names, class_b)):
            sims = []
            for name in names:
                path = capture_path(name)
                game = game_of(path)
                rack_for(game, os.path.basename(path)[:15])
                power = cls or profile_power_class(game)
                sim = simulate_rows(loader(path), game, power, peak)
                short = os.path.basename(path)[:15]
                head += [tag + '  ' + ln for ln in info_lines(short, sim['info'])]
                head.append('%s    theta: %s; Fyf: %s' % (tag, THETA_TABLE[game][2], FYF_TEXT[game]))
                head.append('%s    %s' % (tag, sim['mass'][1]))
                if game == 'acr' and len(sim['cols'].get('t', ())):
                    text, _mass = newton_check(sim['cols'])
                    newton += ['%s %s: Newton against the tyres (%d samples)' % (tag, short, len(sim['cols']['t']))] + text
                sims.append(sim)
            sides.append(pool_rows(sims))
    a, b = sides
    knee_nm = wt.KNEE * (peak or wt.DEFAULT_PEAK_NM)
    rows = sit.curve_rows(sit.curve_cells(a['nm'], a['fyf'], a['theta'], a['dt']),
                          sit.curve_cells(b['nm'], b['fyf'], b['theta'], b['dt']), min_s)
    return {'head': head, 'summary': sit.curve_summary(rows, knee_nm), 'trail': trail_lines(a, b), 'newton': newton,
            'rows': rows, 'knee_nm': knee_nm}


# --- ffb-stages ---------------------------------------------------------------------------------------------------------

def yaw_accel(t, yaw, window=0.03):
    """The yaw acceleration (rad/s2): a central difference of the yaw rate over +-window s (0 where the span is not about that)."""
    t, yaw = np.asarray(t, float), np.asarray(yaw, float)
    n = len(t)
    dt = max(float(np.median(np.diff(t))), 1e-4) if n > 1 else 1e-4
    k = max(int(round(window / dt)), 1)
    lo, hi = np.clip(np.arange(n) - k, 0, n - 1), np.clip(np.arange(n) + k, 0, n - 1)
    span = t[hi] - t[lo]
    good = (span > 0) & (span < 4 * window)
    rd = np.zeros(n)
    rd[good] = (yaw[hi] - yaw[lo])[good] / span[good]
    return rd


def check_overrides(game, mass=None, ratio=None):
    """ValueError for a mass given for ACR (its force takes none) or a value that is not a positive number."""
    for name, v in (('mass', mass), ('ratio', ratio)):
        if v is not None and not (np.isfinite(v) and v > 0):
            raise ValueError('%s must be a positive number (got %r)' % (name, v))
    if mass is not None and game == 'acr':
        raise ValueError("a mass cannot be set for ACR: its force does not take one (it is not sent; ffb-stages prints an estimate)")


def pin_values(game, rack, mass=None, ratio=None):
    """Put the what-if mass and ratio back into the rack. Called before the first sample and after every feed: set_car, reset and
    the learners restore the table's or learnt values (RbrRack.set_car -> _table_frame, RackForce._learn_ratio, MassLearner).
    RBR: the derived path divides by DERIVED_RATIO (not rack.ratio, which is only the aligning spring's frame), set on the instance;
    its mass is the car table's, replaced by `rbr_mass` below. Forza: mass_l.mass. ACR: ratio only."""
    if ratio is not None:
        rack.ratio = ratio
        if game == 'rbr':
            rack.DERIVED_RATIO = ratio
    if mass is not None and game == 'forza-fh':
        rack.mass_l.mass = mass


@contextlib.contextmanager
def rbr_mass(mass):
    """Inside, car_data.entry gives a copy of the entry with mass_kg replaced (RbrRack._derive reads the mass there); None: as it is."""
    from oversteer import car_data
    if mass is None:
        yield
        return
    orig = car_data.entry

    def entry(key):
        found = orig(key)
        return dict(found or {}, mass_kg=mass)
    car_data.entry = entry
    try:
        yield
    finally:
        car_data.entry = orig


STAGE_FYF_MIN = TRAIL_FYF_MIN


def simulate_stages(items, game, power_class, peak=None, mass=None, ratio=None):
    """Today's rack over the samples (two passes, align_on off) as simulate_rows does it, one row per scored sample with the value
    each stage of the chain had in the rack's own code (read from the rack or the same call recorded, see ffb_situations.STAGES).
    Signs: kingpin, column, driver, rim are |.|; caster and pneu/Mz carry the sign of the kingpin (so they add up to it).
    `mass`, `ratio`: what-ifs, pinned (pin_values, rbr_mass). Returns {'cols', 'info', 'in_force', 'raw'}. Call inside _sandbox()."""
    from oversteer import car_data, ffb_carprops, ffb_engine as fe, ffb_forza
    check_overrides(game, mass, ratio)
    with rbr_mass(mass if game == 'rbr' else None):
        rack, fresh, base_feed = _build_rack(game, power_class, peak)

        def feed(t, s):
            pin_values(game, rack, mass, ratio)
            base_feed(t, s)
            pin_values(game, rack, mass, ratio)
        names = [k for k, _n in sit.STAGES] + ['t', 'ay_s', 'yaw', 'fy_all', 'fy_front']
        cols = {k: [] for k in names}
        rec = {}
        orig_fl, orig_kp = ffb_forza.front_lateral, ffb_forza.kingpin
        if game == 'forza-fh':
            def fl(*a, **k):
                rec['fy'] = orig_fl(*a, **k)
                return rec['fy']

            def kp(*a, **k):
                rec['kp'] = orig_kp(*a, **k)
                return rec['kp']
            ffb_forza.front_lateral, ffb_forza.kingpin = fl, kp
        car, scored = None, 0
        try:
            for t, s in items:
                feed(t, s)
            for t, s in items:
                feed(t, s)
                car = s.car or car
                if not rack.ready or not sample_ok(s, sit.STAGE_SPEED[0]):
                    continue
                rec.clear()
                force = rack.force(s)
                bl = ffb_forza.front_share(getattr(s, 'drivetrain', None))
                nan = float('nan')
                if game == 'acr':
                    fy, fz, sa = _pair(s.tyre_fy), _pair(s.tyre_load), _pair(s.tyre_slip_angle)
                    if fy is None or fz is None or sa is None or s.yaw_rate is None or not np.isfinite(s.yaw_rate):
                        continue
                    side = fy[0] + fy[1]
                    fyf, slip, m = abs(side), _slip_acr(s, rack), nan
                    kingpin = rack._front(s, rack.trail, rack.pull_scrub())
                    if kingpin is None:
                        continue
                    ratio_now = rack.ratio
                    column = kingpin / ratio_now
                    caster = -rack.trail * side
                elif game == 'forza-fh':
                    if 'fy' not in rec or 'kp' not in rec:
                        continue
                    v = rack._slip(s)
                    fyf, slip, m = abs(rec['fy']), (None if v is None or not np.isfinite(v) else abs(v)), rack.mass
                    kingpin = rec['kp']
                    ratio_now = rack.ratio
                    column = kingpin / ratio_now
                    caster = -ffb_forza.CASTER * rec['fy']
                else:
                    fy_f = getattr(rack, 'fy_f', None)
                    if fy_f is None or not np.isfinite(fy_f) or rack.derived is None or rack.derived_raw is None:
                        continue
                    entry = car_data.entry(rack.car)
                    m = (entry.get('mass_kg') if entry else None) or rack.FLOOR_MASS
                    fyf, slip = abs(fy_f), _slip_rbr(s, rack)
                    ratio_now = rack.DERIVED_RATIO
                    column = rack.derived * rack._fresh()
                    kingpin = column * ratio_now
                    raw = rack.derived_raw * ratio_now
                    caster = -ffb_carprops.RALLY_TRAIL * fy_f * (kingpin / raw if raw else 0.0)
                sign = 1.0 if kingpin >= 0 else -1.0
                driver = abs(rack.last_torque)
                rho = rack.rho()
                share = abs(force) / fe.FULL
                if slip is None:
                    slip = nan
                scored += 1
                row = {'ay': abs(s.accel[1]), 'speed': s.speed, 'slip': slip, 'fyf': fyf, 'mass': m, 'bl': bl,
                       'kingpin': abs(kingpin), 'caster': caster * sign, 'pneu': (kingpin - caster) * sign,
                       'trail_mm': abs(kingpin) / fyf * 1000.0 if fyf >= STAGE_FYF_MIN else nan,
                       'ratio': ratio_now, 'column': abs(column), 'driver': driver,
                       'gain': driver / abs(column) if column else nan, 'rho': rho, 'rim': driver * rho, 'share': share,
                       'played': share * rack.wheel_peak, 't': t, 'ay_s': s.accel[1],
                       'yaw': s.yaw_rate if s.yaw_rate is not None else nan, 'fy_all': nan, 'fy_front': nan}
                if game == 'acr' and s.tyre_fy is not None and len(s.tyre_fy) >= 4:
                    allfy = sum(s.tyre_fy[:4])
                    row['fy_all'] = allfy if np.isfinite(allfy) else nan
                    row['fy_front'] = side
                for k in names:
                    cols[k].append(row[k])
            learnt = {k: getattr(rack, k, None) for k in ('trail', 'ratio', 'mass')}
            source = rack.power_steering()
            table = ffb_carprops.table_ratio(car_data.entry(rack.car)) if getattr(rack, 'car', None) else None
            ratio_text = _ratio_text(game, rack, ratio, table)
        finally:
            ffb_forza.front_lateral, ffb_forza.kingpin = orig_fl, orig_kp
    cols = {k: np.array(v, float) for k, v in cols.items()}
    dt = sample_spacing([t for t, _ in items])
    info = {'game': game, 'class': power_class, 'samples': len(items), 'scored': scored, 'dt': dt, 'learnt': learnt,
            'default': fresh, 'car': car, 'peak': rack.wheel_peak}
    pick = (lambda k: cols[k][np.isfinite(cols[k])]) if scored else (lambda k: np.zeros(0))

    def span(k):
        v = pick(k)
        return 'median %.4g (%.4g-%.4g)' % (np.median(v), v.min(), v.max()) if len(v) else 'none'
    in_force = ['in force: mass %s; ratio %s (%s); power steering %s; rho %s' % (
        span('mass') if game != 'acr' else 'not used by the force', span('ratio'), ratio_text, source, span('rho'))]
    return {'cols': cols, 'info': info, 'in_force': in_force}


def _ratio_text(game, rack, pinned, table):
    if pinned is not None:
        return 'what-if, pinned'
    if game == 'rbr':
        return 'RbrRack.DERIVED_RATIO, a class constant; the car table %s only frames the spring' % ('%.2f' % table if table else 'has none')
    if game == 'forza-fh':
        return 'ForzaRack.STEER_RATIO, a constant'
    if table and abs(rack.ratio - table) < 1e-9:
        return 'from the car table'
    if not rack.ratio_known:
        return 'the default STEER_RATIO, not learnt and not in a table'
    return 'learnt in this replay (starts at %s)' % ('the table %.2f' % table if table else 'the default')


MASS_AY_MIN = 3.0                       # m/s2: the true-mass fit uses |ay| over this ...
MASS_RDOT_MAX = 0.3                     # rad/s2: ... and |yaw acceleration| under this


def acr_true_mass(cols):
    """(mass kg, front mass kg, front share, samples) of one ACR drive: least squares through the origin of the sum of all four
    tyres' Fy on ay (and of the front pair's), |ay| over MASS_AY_MIN and |yaw acceleration| under MASS_RDOT_MAX. None when too few."""
    ay, tot, fr = cols['ay_s'], cols['fy_all'], cols['fy_front']
    if not len(ay):
        return None
    rd = yaw_accel(cols['t'], cols['yaw'])
    ok = (np.abs(ay) > MASS_AY_MIN) & (np.abs(rd) < MASS_RDOT_MAX) & np.isfinite(tot) & np.isfinite(fr)
    if ok.sum() < 100:
        return None
    a = ay[ok]
    m, mf = abs(float(np.dot(tot[ok], a) / np.dot(a, a))), abs(float(np.dot(fr[ok], a) / np.dot(a, a)))
    return m, mf, mf / m if m else float('nan'), int(ok.sum())


def mass_lines(tag, name, cols):
    r = acr_true_mass(cols)
    if r is None:
        return ['%s %s: true mass: too few samples with |ay| > %g and |yaw acceleration| < %g' % (tag, name, MASS_AY_MIN, MASS_RDOT_MAX)]
    return ['%s %s: true mass %.0f kg (sum of all four tyre Fy over ay), front %.0f kg = %.1f %% of it; %d samples, |ay| > %g, |yaw accel| < %g'
            % (tag, name, r[0], r[1], r[2] * 100, r[3], MASS_AY_MIN, MASS_RDOT_MAX)]


def stages(a_names, b_names, class_a=None, class_b=None, peak=None, min_s=1.0, sets=(), mass=(None, None), ratio=(None, None),
           loader=None, game_of=None):
    """Run ffb-stages. Returns {'head', 'summary', 'cells', 'mass'} (lists of lines). Raises NoRack, ValueError (a bad --set, a
    mass for ACR)."""
    import ffb_benchmark as fb
    import ffb_parity as fp
    import ffb_score as fs
    loader = loader or (lambda path: list(fb._walk(path)[1]))
    game_of = game_of or fp.game_of
    head, mass_out, sides = [], [], []
    with fs._sandbox(), overrides(sets) as set_lines:
        head += set_lines
        for n, tag in enumerate('AB'):
            pool = []
            for name in (a_names, b_names)[n]:
                path = capture_path(name)
                game = game_of(path)
                rack_for(game, os.path.basename(path)[:15])
                check_overrides(game, mass[n], ratio[n])
                power = (class_a, class_b)[n] or profile_power_class(game)
                sim = simulate_stages(loader(path), game, power, peak, mass[n], ratio[n])
                short = os.path.basename(path)[:15]
                head += [tag + '  ' + ln for ln in info_lines(short, sim['info'])]
                head += [tag + '    ' + ln for ln in sim['in_force']]
                if game == 'acr':
                    mass_out += mass_lines(tag, short, sim['cols'])
                pool.append((sim['cols'], sim['info']['dt']))
            sides.append(sit.stage_pool(pool))
    a, b = (sit.stage_cells(c, d) for c, d in sides)
    return {'head': head, 'summary': sit.stage_summary(a, b, min_s), 'cells': sit.stage_cell_lines(a, b, min_s), 'mass': mass_out}


# --- ffb-wheel-model ------------------------------------------------------------------------------------------------

def stored_result(store_path=None, key=None):
    """(key, the stored wheel_calibration.json entry's 'result' dict); the newest entry unless `key` names one."""
    path = store_path or os.path.join(config_dir(), 'wheel_calibration.json')
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError("cannot read the calibration store %s: %s" % (path, e))
    entries = {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get('result'), dict)}
    if key is not None:
        if key not in entries:
            raise ValueError("no entry '%s' in %s (have: %s)" % (key, path, ', '.join(entries) or 'none'))
    elif entries:
        key = max(entries, key=lambda k: str(entries[k]['result'].get('date', '')))
    else:
        raise ValueError("no calibration result in %s" % path)
    return key, entries[key]['result']


def delivered(asked_nm, shaping, curve, peak, ask_peak=None):
    """Nm the wheel (real peak `peak`, real curve from the result `curve`) delivers for an asked torque: the build turns the
    ask into a share of the peak it assumes (`ask_peak`, default `peak`), through the LUT and dead zone of the result
    `shaping` (wheel_calib.shaped), then the measured curve (share of the torque at full scale at each knot) times the peak."""
    from oversteer import wheel_calib as wc
    share = min(max(asked_nm / (ask_peak or peak), 0.0), 1.0)
    shaped = wc.shaped(share * 32767, shaping['lut'], shaping['deadband']) / 32767
    return float(np.interp(shaped, curve['knots'], curve['curve'])) * peak


def delivered_table(asked, new, new_peak, old=None, old_ask_peak=None):
    lines = []
    if old is None:
        lines.append('asked Nm | the stored result delivers (peak %.2f)' % new_peak)
    else:
        lines.append('asked Nm | old shaping delivers (asked against a peak of %.2f) | stored shaping delivers (peak %.2f); both on the stored curve' % (old_ask_peak, new_peak))
    for nm in asked:
        n = delivered(nm, new, new, new_peak)
        text = '%5.2f | %5.2f Nm (%4.0f %%)' % (nm, n, n / nm * 100 if nm else 0.0)
        if old is not None:
            o = delivered(nm, old, new, new_peak, old_ask_peak)
            text = '%5.2f | %5.2f Nm (%4.0f %%) | %5.2f Nm (%4.0f %%)' % (nm, o, o / nm * 100 if nm else 0.0, n, n / nm * 100 if nm else 0.0)
        lines.append(text)
    return lines


def weight_check(kg, share, result, peak, radius=DEFAULT_RADIUS):
    """(motor Nm, load Nm): the curve at `share` of full scale, net of the fit's friction, against a weight hanging at the rim."""
    motor = (float(np.interp(share, result['knots'], result['curve'])) - float(result.get('friction') or 0.0)) * peak
    return motor, kg * G * radius


def load_old_result(path):
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        data = json.load(f)
    result = (data.get('summary') or {}).get('result')
    if not isinstance(result, dict):
        raise ValueError("%s has no fitted result (the run did not finish)" % path)
    return result


# --- calib-check ----------------------------------------------------------------------------------------------------

def runs_dir():
    return os.path.join(config_dir(), 'calibration-runs')


def newest_run(directory):
    files = glob.glob(os.path.join(directory, '*.json.gz'))
    return max(files, key=os.path.getmtime) if files else None


def load_run_raw(path):
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("not a calibration recording: %s" % path)
    return data


def _pos_speed(rec, half, derivative=1):
    """(grid, speed) deg/s of a recording, or None when the position record is too short."""
    from oversteer import wheel_calib as wc
    if len(rec.get('pos_t', [])) < 2 * half + 12:
        return None
    grid, th = wc.resample(rec['pos_t'], rec['pos'])
    if len(grid) < 2 * half + 12:
        return None
    coefs = wc.savgol(half, .001)
    return grid, np.convolve(th, coefs[derivative][::-1], 'same')


def _steps(c):
    """Start and end indexes of each stretch of constant command."""
    if len(c) < 2:
        return np.array([], int), np.array([], int)
    ch = np.flatnonzero(np.abs(np.diff(c)) > 1e-4)
    return np.r_[0, ch + 1], np.r_[ch + 1, len(c) - 1]


def pulse_tables(data):
    """({level: [(dur ms, v_start, v_end, peak)]}, [(t from start, dur s, end speed)] run-ups). Empty where the run has no data."""
    from oversteer import wheel_calib as wc
    r = data.get('rec') or {}
    if len(r.get('cmd', [])) < 2:
        return {}, []
    got = _pos_speed(r, 4)
    if got is None:
        return {}, []
    grid, om = got
    ct, c = np.array(r['cmd_t'], float), np.array(r['cmd'], float)
    kick = float((data.get('summary') or {}).get('kick') or 0.35)
    st, en = _steps(c)
    seen, runups = {}, []
    for i, (a, b) in enumerate(zip(st, en)):
        if i == 0:
            continue
        w, pv = c[a], c[st[i - 1]]
        if abs(abs(w) - kick) < 1e-3 and ct[b] - ct[a] > .03:
            s = np.sign(w)
            runups.append((ct[a] - (data.get('t0') or ct[0]), ct[b] - ct[a], float(np.interp(ct[b], grid, om)) * s))
        if abs(pv) < .3 or pv * w > 0:
            continue
        lv = min(wc.KNOTS, key=lambda k: abs(k - abs(w)))
        if abs(lv - abs(w)) > 1e-3 or lv < .2:
            continue
        s = np.sign(w)
        m = (grid >= ct[a]) & (grid < ct[b] + .06)
        if not m.any():
            continue
        seen.setdefault(lv, []).append(((ct[b] - ct[a]) * 1e3, float(np.interp(ct[a], grid, om)) * s,
                                        float(np.interp(ct[b], grid, om)) * s, float((om[m] * s).max())))
    return seen, runups


def pulse_lines(seen):
    if not seen:
        return ['no pulses recorded']
    out = ['level   n   dur ms  v_start  v_end  peak  worst peak']
    for lv, v in sorted(seen.items()):
        med = np.median(v, axis=0)
        out.append('%5.2f %3d  %7.0f %8.0f %6.0f %5.0f  %6.0f' % (lv, len(v), med[0], med[1], med[2], med[3], max(x[3] for x in v)))
    return out


def runup_lines(runups):
    if not runups:
        return ['no run-ups recorded']
    R = np.array(runups)
    out = ['run-ups n %d; duration ms p50/p95/max %s; end speed deg/s p5/p50 %s' % (
        len(R), np.round(np.percentile(R[:, 1] * 1e3, [50, 95, 100])), np.round(np.percentile(R[:, 2], [5, 50])))]
    out.append('last 12 run-ups (t s, duration ms, end speed): %s' % [(round(x[0]), round(x[1] * 1e3), round(x[2])) for x in runups[-12:]])
    return out


def odd_push_lines(data):
    """The run's own log of odd pushes, and the spread of the recorded push accelerations by direction."""
    out = []
    for entry in data.get('log') or []:
        text = entry[1] if isinstance(entry, (list, tuple)) and len(entry) > 1 else str(entry)
        if 'odd push' in text:
            out.append('log: %s' % text)
    for d, v in sorted(((data.get('summary') or {}).get('kick_accels') or {}).items()):
        v = np.asarray(v, float)
        if len(v):
            med = float(np.median(v))
            out.append('push accel, direction %s: n %d, median %.0f, min %.0f, max %.0f, under 60 %% of the median: %d'
                       % (d, len(v), med, v.min(), v.max(), int((v < 0.6 * med).sum())))
    return out or ['no push accelerations recorded']


def refit_lines(data):
    """Today's wheel_calib.fit_dynamics on the run: raw curve, uncertainty, speed term. A refusal is a line, not a crash."""
    from oversteer import wheel_calib as wc
    try:
        rec = wc.Recording.from_dict(data['rec'])
        F = wc.fit_dynamics(rec)
    except (wc.FitError, KeyError, ValueError, TypeError, IndexError) as e:
        return ['refit refused: %s' % e]
    q = F['curve']
    top = q[-1] if abs(q[-1]) > 1e-9 else 1.0
    return ['fit r2 %.3f, a_fs %.0f, friction %.0f, latency %.1f ms, rows %d' % (F['r2'], q[-1], F['friction'], F['latency'] * 1000, F['rows']),
            'k  ' + ' '.join('%5.2f' % k for k in wc.KNOTS),
            'raw' + ' '.join('%5.2f' % x for x in q / top),
            'se ' + ' '.join('%5.2f' % x for x in F['curve_se'] / top),
            'H  ' + ' '.join('%5.1f' % x for x in F['speed_term'])]


ACC_BINS = [-300, -200, -150, -100, -70, -40, -15, 15, 40, 70, 100, 150, 200, 300]


def accel_pool(datas):
    """{level: [(speed, accel) along the force]} of every held level (>= 45 ms), from 30 ms into the hold."""
    pool = {}
    for data in datas:
        r = data.get('rec') or {}
        if len(r.get('cmd', [])) < 2:
            continue
        v = _pos_speed(r, 8, 1)
        a = _pos_speed(r, 8, 2)
        if v is None or a is None:
            continue
        grid, om = v
        ac = a[1]
        ct, c = np.array(r['cmd_t'], float), np.array(r['cmd'], float)
        st, en = _steps(c)
        for x, y in zip(st, en):
            if ct[y] - ct[x] < .045:
                continue
            w = c[x]
            s = np.sign(w) or 1
            m = (grid >= ct[x] + .030) & (grid <= ct[y] + .002)
            if m.any():
                pool.setdefault(round(abs(w), 2), []).append(np.column_stack([om[m] * s, ac[m] * s]))
    return pool


def accel_lines(pool, min_total=60, min_bin=25):
    out = ['accel along the force (deg/s^2) by speed along the force (deg/s) at a held level; negative = moving against it',
           '       ' + ' '.join('%6d' % ((ACC_BINS[i] + ACC_BINS[i + 1]) // 2) for i in range(len(ACC_BINS) - 1))]
    rows = 0
    for lv in sorted(pool):
        A = np.vstack(pool[lv])
        if len(A) < min_total:
            continue
        cells = []
        for i in range(len(ACC_BINS) - 1):
            m = (A[:, 0] >= ACC_BINS[i]) & (A[:, 0] < ACC_BINS[i + 1])
            cells.append(np.median(A[m, 1]) if m.sum() >= min_bin else np.nan)
        out.append('%5.2f  ' % lv + ' '.join('%6.0f' % x if x == x else '     .' for x in cells) + '  n=%d' % len(A))
        rows += 1
    if not rows:
        out.append('(too little held-level data)')
    return out


def run_report(path, data):
    out = ['== %s' % os.path.basename(path), 'date %s, outcome %s%s, proxied %s' % (
        data.get('date'), data.get('outcome'), (': ' + data['message']) if data.get('message') else '', data.get('proxied')),
        'recorded: %d commands, %d positions' % (len((data.get('rec') or {}).get('cmd', [])), len((data.get('rec') or {}).get('pos', [])))]
    log = data.get('log') or []
    out.append('log (%d lines):' % len(log))
    out += ['  ' + (str(e[1]) if isinstance(e, (list, tuple)) and len(e) > 1 else str(e)) for e in log]
    try:
        seen, runups = pulse_tables(data)
    except (ValueError, KeyError, TypeError) as e:
        seen, runups = {}, []
        out.append('pulses unreadable: %s' % e)
    out += ['', 'pulses by level (median per level)'] + pulse_lines(seen)
    out += ['', 'run-ups'] + runup_lines(runups)
    out += ['', 'odd pushes'] + odd_push_lines(data)
    out += ['', 'refit with today\'s fit_dynamics'] + refit_lines(data)
    return out


# --- new-game-check -------------------------------------------------------------------------------------------------

# the Sample fields the force code reads (RackForce, RbrRack, ForzaRack and the situation bands)
CHANNELS = ('speed', 'vel', 'accel', 'steer', 'yaw_rate', 'throttle', 'brake', 'car', 'drivetrain', 'game_time',
            'tyre_load', 'tyre_fx', 'tyre_fy', 'tyre_slip_angle', 'tyre_side_slip', 'slip_angle', 'combined_slip',
            'wheel_speed', 'wheel_rot', 'rpm', 'rack_force', 'puddle', 'rumble', 'game_ffb')


def channel_report(samples, channels=CHANNELS):
    """({channel: share of samples carrying it}, [present], [missing]); `samples` are Sample objects."""
    n = len(samples)
    share = {}
    for ch in channels:
        have = sum(1 for s in samples if getattr(s, ch, None) not in (None, (), []))
        share[ch] = have / n if n else 0.0
    present = [c for c in channels if share[c] > 0]
    return share, present, [c for c in channels if share[c] == 0]


# --- ffb-components -----------------------------------------------------------------------------------------------------

GRID_HZ = 60.0
BANDS = (('0-1 Hz', None, 60), ('1-6 Hz', 10, 60), ('6-30 Hz', 2, 10))      # (name, short mean, long mean) in grid samples


def moving_mean(x, n):
    x = np.asarray(x, float)
    return x if n <= 1 else np.convolve(x, np.ones(n) / n, 'same')


def band_of(x, short, long):
    """A band by differences of moving means: (mean over `long`) alone, else (mean over `short`) - (mean over `long`)."""
    return moving_mean(x, long) if short is None else moving_mean(x, short) - moving_mean(x, long)


def joint_fit(y, causes):
    """Least squares of y on the causes (dict name -> array, same length, intercept removed): {name: (coef, rms Nm, r2 rise
    when added last, input rms)} and the full r2."""
    names = list(causes)
    X = np.column_stack([np.asarray(causes[n], float) - np.mean(causes[n]) for n in names])
    y = np.asarray(y, float) - np.mean(y)
    yy = float(y @ y) or 1e-12

    def r2(cols):
        if not cols:
            return 0.0
        b, *_ = np.linalg.lstsq(X[:, cols], y, rcond=None)
        r = y - X[:, cols] @ b
        return 1.0 - float(r @ r) / yy
    b, *_ = np.linalg.lstsq(X, y, rcond=None)
    full = r2(list(range(len(names))))
    out = {n: (float(b[i]), float(np.std(X[:, i] * b[i])), full - r2([j for j in range(len(names)) if j != i]),
               float(np.std(X[:, i]))) for i, n in enumerate(names)}
    return out, full


def yaw_events(F, yaw, ay, hz=GRID_HZ, horizons=(0.1, 0.2, 0.3, 0.5), min_yaw=0.3, window=0.6, min_force=0.2):
    """The settle after a turn: yaw rate falling from over min_yaw to under half of it within `window` s with |ay| falling.
    Returns (count, {horizon: median force change signed relative to the turn's force}, {horizon: fraction of events where
    the force then has the opposite sign to the turn's})."""
    F, yaw, ay = (np.asarray(a, float) for a in (F, yaw, ay))
    ys, w, n, i = moving_mean(yaw, 3), int(window * hz), len(F), 0
    Fs = moving_mean(F, 7)
    ch = {h: [] for h in horizons}
    flips = {h: [] for h in horizons}
    count = 0
    while i < n - 1:
        s = 1.0 if ys[i] > 0 else -1.0
        if s * ys[i] > min_yaw and abs(Fs[i]) >= min_force:
            seg = s * ys[i + 1:i + 1 + w]
            hit = np.nonzero(seg < 0.5 * s * ys[i])[0]
            if len(hit) and abs(ay[i + 1 + hit[0]]) < abs(ay[i]):
                j = i + 1 + int(hit[0])
                sf = 1.0 if Fs[i] > 0 else -1.0
                if j + int(max(horizons) * hz) < n:
                    count += 1
                    for h in horizons:
                        k = j + int(round(h * hz))
                        ch[h].append(sf * (Fs[k] - Fs[j]))
                        flips[h].append(sf * Fs[k] < 0)
                i = j + w
                continue
        i += 1
    return (count, {h: float(np.median(v)) if v else math.nan for h, v in ch.items()},
            {h: float(np.mean(v)) if v else math.nan for h, v in flips.items()})
