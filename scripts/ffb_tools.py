"""The library behind the one-off FFB analyses that became tools: scripts/ffb-sim-match.py, ffb-wheel-model.py,
calib-check.py and new-game-check.py. Nothing here touches a wheel, the proxy, a game or the telemetry database; user
files (stored calibration, runs, profiles, captures) are only read."""
import configparser
import glob
import gzip
import json
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


def simulate(items, game, power_class, peak=None):
    """Today's rack over recorded samples [(t, Sample)], two passes (the first lets the rack learn), whole force
    (align_on off). Returns {'cells': {key: [share of full force]}, 'secs': {key: seconds}, 'info': {...}}.
    Call inside ffb_score._sandbox() so no user file is read or written."""
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
