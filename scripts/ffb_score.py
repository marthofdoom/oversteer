"""Score one recorded drive against the CURRENT Countersteer FFB code, offline (scripts/ffb-score.py is the CLI).

The drive goes through the engine's own classes (RackForce, Texture, FfbEngine._shape) the way FfbEngine._play does,
and the result is scored four ways. Every number is in the returned dict (JSON-able); report() prints the compact text.

  physics   our rim torque against the tyre forces (front Mz, Fy, Fx; caster trail, scrub, ratio, assist) worked out
            again from first principles (consistency: exact unless gated, with the gates counted), the sign of our
            force against the game's by case (cornering L/R, drift, past peak, lock-up, airborne, landing), the lag
            against the game's force. The game's force is a sanity reference only.
  wheel     the output through the driver's shaping (LUT, dead zone and its fade) and the calibrated G29's torque
            curve (tests/data/g29-fit-result.json): delivered against intended torque, tracking error and delay.
  dynamics  hands-off slides found in the drive, simulated on the G29 model (inertia, friction, damping, latency)
            under the model's restoring torque, with the driver's local aligning spring when RackForce.align_on:
            settle time and overshoot to theta_a.
  safety    clip and budget share, the worst 90 s thermal mean square against 0.315, chatter, slew, texture share,
            output standing still.

Inputs: marth's ACR dump (~/.cache/ffb-slip/dump.npz: ~4 snapshots a second, so the engine is fed a linear
interpolation at `hz` and only the real snapshots are scored; metrics that need the real rate are skipped with a
note) and "Record for FFB tuning" captures (.ovcap.gz, full rate). Absent fields are skipped with a note. Nothing
here touches a wheel, sysfs or the running app; the car-property store is redirected to a temporary folder.
"""
import contextlib
import importlib.util
import json
import math
import os
import shutil
import sys
import tempfile
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
from oversteer import ffb_carprops as cp  # noqa: E402
from oversteer import ffb_engine as fe  # noqa: E402
from oversteer import ffb_handforce as fh  # noqa: E402
from oversteer import ffb_metrics as fm  # noqa: E402
from oversteer import ffb_record  # noqa: E402
from oversteer import ffb_spring  # noqa: E402
from oversteer import ffb_texture as ft  # noqa: E402
from oversteer import wheel_calibration as wcal  # noqa: E402
from oversteer import wheel_torque  # noqa: E402
from oversteer.telemetry_formats import Sample  # noqa: E402

_spec = importlib.util.spec_from_file_location('ffb_physics_report', os.path.join(HERE, 'ffb_physics_report.py'))
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)               # driver_shape, torque_fn, thermal_mean, load_dump (the driver's shaping)

SCHEMA = 1
FULL = fe.FULL
G = 9.81
DUMP = R.DUMP
WHEEL = R.WHEEL
THERMAL_MEAN_LIMIT = 0.315                # mean square of the output (FS^2) over 90 s the task holds the engine to
FULL_RATE = 200.0                         # Hz: the real rate above which the rate-bound metrics are judged
SIGN_MIN_GAME = 0.02                      # FS: the game's force under this says nothing about the sign
SIGN_MIN_OURS = 0.005
CASES = ('cornering_left', 'cornering_right', 'drift', 'past_peak', 'lock_up', 'airborne', 'landing', 'all_moving')


# ------------------------------------------------------------------------------------------------ drives

class Drive:
    """A drive: stream(hz) yields (t, Sample, real) in time order; `real` rows are the recorded ones (scored), the
    others are interpolated between sparse snapshots (they only feed the engine). `segment(t)` is not needed: a gap
    over a second starts a new segment."""

    def __init__(self, source, kind, notes):
        self.source, self.kind, self.notes = source, kind, list(notes)

    def stream(self, hz):
        raise NotImplementedError


class DumpDrive(Drive):
    def __init__(self, path=DUMP, limit=None):
        super().__init__(path, 'dump', ["dump: ~4 snapshots/s, engine fed an interpolation; rate-bound metrics skipped",
                                        "dump: no car names (one car per segment), no surface, no hands/wheel angle"])
        self.d = R.load_dump(path)
        self.n = len(self.d['t']) if limit is None else min(limit, len(self.d['t']))

    def _sample(self, v, gear, car):
        s = Sample(6000.0, 7500.0, gear=gear, speed=float(v['speed']) / 3.6, car=car, game='acr')
        s.steer = -float(v['steer'])
        s.game_ffb = float(v['ffb'])
        s.tyre_mz, s.tyre_fx, s.tyre_fy = tuple(v['mz']), tuple(v['fx']), tuple(v['fy'])
        s.tyre_slip_angle, s.tyre_slip_ratio, s.tyre_load = tuple(v['sa']), tuple(v['sr']), tuple(v['load'])
        s.brake, s.throttle = float(v['brake']), float(v['gas'])
        loc, acc, ang = v['locvel'], v['accG'], v['angvel']
        s.vel = (float(loc[2]), float(loc[0]), float(loc[1]))
        s.accel = (float(acc[2]) * G, float(acc[0]) * G, float(acc[1]) * G)
        s.accel_kind = 'kinematic'
        s.yaw_rate = float(ang[1])
        s.ang_vel = (float(ang[2]), float(ang[0]), float(ang[1]))
        s.wheel_rot, s.susp = tuple(v['wspd']), tuple(v['susp'])
        return s

    def stream(self, hz):
        d, n = self.d, self.n
        keys = ('speed', 'steer', 'ffb', 'mz', 'fx', 'fy', 'sa', 'sr', 'load', 'brake', 'gas', 'locvel', 'accG',
                'angvel', 'wspd', 'susp')
        t0 = d['t'][0]
        for i in range(n):
            car = 'acr/seg%d' % d['seg'][i]
            gear = int(d['gear'][i])
            yield d['t'][i] - t0, self._sample({k: d[k][i] for k in keys}, gear, car), True
            if i + 1 < n and d['seg'][i + 1] == d['seg'][i] and hz:
                gap = d['t'][i + 1] - d['t'][i]
                k = int(gap * hz)
                for j in range(1, k):
                    w = j / k
                    v = {key: d[key][i] + (d[key][i + 1] - d[key][i]) * w for key in keys}
                    yield d['t'][i] - t0 + gap * w, self._sample(v, gear, car), False


class ListDrive(Drive):
    """(t, Sample) pairs, all real: a full-rate capture or a synthetic drive."""

    def __init__(self, items, source='memory', kind='capture', notes=()):
        super().__init__(source, kind, notes)
        self.items = items

    def stream(self, hz):
        last = None
        for t, s in self.items:
            if last is not None and not t > last:
                continue
            last = t
            yield t, s, True


def load_capture(path):
    from oversteer.telemetry_capture import read_capture
    from oversteer.telemetry_formats import decode_sample
    meta, records = read_capture(path)
    items = []
    for t, _addr, data in records:
        s = decode_sample(data)
        if s is not None and s.game_ffb is not None:
            items.append((t, s))
    notes = []
    if not meta.get('ffb_tuning'):
        notes.append("capture header is not ffb_tuning: probably thinned, rate-bound metrics may be skipped")
    if items and items[0][1].accel is None:
        notes.append("capture: no acceleration (cornering cases fall back on the tyres' Fy)")
    return ListDrive(items, path, 'capture', notes)


def load_drive(path, limit=None):
    if path.endswith('.npz'):
        return DumpDrive(path, limit)
    drive = load_capture(path)
    if limit is not None:
        drive.items = drive.items[:limit]
    return drive


# ------------------------------------------------------------------------------------------------ the engine run

@contextlib.contextmanager
def _sandbox():
    """The car-property store and the INI settings away from the user's files (deterministic, nothing written)."""
    tmp = tempfile.mkdtemp(prefix='ffb-score-')
    kept = (cp.STORE.path, cp.ASYNC, cp.CONFIG_PATH)
    cp.STORE.use(os.path.join(tmp, 'props.json'))
    cp.ASYNC = False
    cp.CONFIG_PATH = os.path.join(tmp, 'no-config.ini')
    try:
        yield tmp
    finally:
        cp.STORE.use(kept[0])
        cp.ASYNC, cp.CONFIG_PATH = kept[1], kept[2]
        shutil.rmtree(tmp, ignore_errors=True)


def _mean(a, b):
    return 0.5 * (a + b)


def run(drive, hz=50.0, wheel_peak=None, texture_level=None, align_on=None, mapping=None):
    """Drive the engine's classes with the drive. Returns a dict of numpy arrays, one entry per REAL row."""
    keys = ('t', 'ready', 'speed', 'ay', 'steer', 'alpha_f', 'alpha_r', 'sr_f', 'brake', 'load', 'game', 'base',
            'tex', 'intended', 'shaped', 'rim_engine', 'rim_model', 'scale', 'x_top', 'peak', 'ceiling', 'reverse', 'source',
            'rho', 'rim_mm', 'last_torque', 'theta', 'theta_a', 'ratio', 'trail', 'scrub', 'cls', 'stiff', 'lock', 'seg')
    rec = {k: [] for k in keys}
    with _sandbox():
        rack = fe.RackForce()
        rack.wheel_peak = wheel_peak
        if mapping is not None:
            rack.mapping = mapping
        if align_on is not None:
            rack.align_on = bool(align_on)
        texture = ft.Texture()
        if texture_level is not None:
            texture.set(texture_level)
        shape = types.SimpleNamespace(_smooth=None, _smooth_at=0.0, _spike=None)
        seg, last_t = 0, None
        for t, s, real in drive.stream(hz):
            if last_t is not None and t - last_t > 1.0:
                seg += 1
            last_t = t
            raw = None if s.game_ffb is None else s.game_ffb * FULL
            rack.feed(t, s, raw)
            ready = bool(rack.ready)
            base = tex = intended = shaped = 0.0
            if ready:
                base = float(rack.force(s))
                tex = float(texture.force(t, s, base))
                intended = min(max(base + tex, -FULL), FULL)
                shaped = float(fe.FfbEngine._shape(shape, t, intended))
            else:
                shape._smooth = shape._spike = None
            if not real:
                continue
            sa, sr = s.tyre_slip_angle, s.tyre_slip_ratio
            front = lambda x: math.nan if x is None else _mean(x[0], x[1])
            rear = lambda x: math.nan if x is None else _mean(x[2], x[3])
            rec['t'].append(t)
            rec['seg'].append(seg)
            rec['ready'].append(ready)
            rec['speed'].append(s.speed or 0.0)
            rec['ay'].append(s.accel[1] if s.accel is not None else math.nan)
            rec['steer'].append(math.nan if s.steer is None else s.steer)
            rec['alpha_f'].append(front(sa))
            rec['alpha_r'].append(rear(sa))
            rec['sr_f'].append(front(sr))
            rec['brake'].append(s.brake or 0.0)
            rec['load'].append(math.nan if s.tyre_load is None else float(sum(s.tyre_load)))
            rec['game'].append(math.nan if s.game_ffb is None else s.game_ffb)
            rec['base'].append(base / FULL)
            rec['tex'].append(tex / FULL)
            rec['intended'].append(intended / FULL)
            rec['shaped'].append(shaped / FULL)
            if ready:
                rim = rack.rim_torque(s)
                peak = rack.wheel_peak or wheel_torque.DEFAULT_PEAK_NM
                src = rack.power_steering()
                cls = 0.0 if cp.power_settings()[0] == 'off' else rack.assist * cp.power_settings()[1]
                rec['rim_engine'].append(math.nan if rim is None else rim)
                rec['rim_model'].append(_first_principles_rim(s, rack, cls, rack.classic()))
                rec['scale'].append(rack.impedance())
                rec['x_top'].append(rack.top_torque())
                rec['peak'].append(peak)
                rec['rho'].append(rack.rho())
                rec['rim_mm'].append(rack.rim_mm())
                rec['last_torque'].append(math.nan if rack.last_torque is None else rack.last_torque)
                rec['ceiling'].append(rack._ceiling())
                vel = s.vel
                rec['reverse'].append(bool(vel is not None and vel[0] is not None and vel[0] < -fe.REVERSE_SPEED))
                rec['source'].append(src)
                al = rack.align(s)
                rec['theta'].append(math.nan if al is None else al['theta_deg'])
                rec['theta_a'].append(math.nan if al is None else al['theta_a_deg'])
                rec['ratio'].append(rack.ratio)
                rec['trail'].append(rack.trail)
                rec['scrub'].append(rack.scrub)
                rec['cls'].append(cls)
                rec['stiff'].append(rack.align_stiffness())
                rec['lock'].append(rack.lock)
            else:
                for k in ('rim_engine', 'rim_model', 'scale', 'x_top', 'peak', 'rho', 'rim_mm', 'last_torque', 'ceiling', 'theta', 'theta_a', 'ratio', 'trail',
                          'scrub', 'cls', 'stiff', 'lock'):
                    rec[k].append(math.nan)
                rec['reverse'].append(False)
                rec['source'].append('')
        rack.reset(None)
        out = {k: (np.array(v) if k != 'source' else np.array(v, dtype=object)) for k, v in rec.items()}
        out['align_on'] = bool(rack.align_on)
        out['mapping'] = rack.mapping
        out['align_k_limit'] = float(rack.align_k_limit)
        out['wheel_peak'] = rack.wheel_peak or wheel_torque.DEFAULT_PEAK_NM
    return out


def _first_principles_rim(s, rack, cls, classic=False):
    """The driver's rim torque (Nm) worked out again from the tyre forces: kingpin torque (FL+FR Mz, the caster arm
    -trail*(Fy_FL+Fy_FR), the scrub couple only with Drive pull on, as the engine plays it) through the column ratio and the boost curve (the
    class the engine uses), where the model's assist applies. NaN without Mz. The classic mapping has no hydraulic
    top end."""
    mz, fy, fx = s.tyre_mz, s.tyre_fy, s.tyre_fx
    if mz is None:
        return math.nan
    tq = mz[0] + mz[1]
    if fy is not None:
        tq -= rack.trail * (fy[0] + fy[1])
    if not math.isfinite(tq):
        return math.nan
    scrub = rack.pull_scrub()
    if scrub and fx is not None and math.isfinite(fx[0] - fx[1]):
        tq -= scrub * (fx[0] - fx[1])
    return cp.driver_from_column(tq / rack.ratio, s.speed, cls, not classic)


# ------------------------------------------------------------------------------------------------ the wheel

def load_wheel(path=WHEEL):
    with open(path) as f:
        return json.load(f)


class WheelFit:
    """The calibrated wheel: the driver's shaping (LUT, dead zone and its fade, optional derate) and the fitted torque
    curve tau(u), as FS shares."""

    def __init__(self, result, friction_comp=0, derate=1.0):
        self.result = result
        self.dead = wcal.compensated_deadband(result, friction_comp)
        self.lut = wcal.compensated_lut(result, friction_comp)
        self.tau = R.torque_fn(result)
        self.derate = derate
        self.latency_ms = float(result.get('fit_latency_ms') or 4.0)

    def command(self, level_fs):
        return R.driver_shape(int(round(level_fs * FULL)), self.lut, self.dead, self.derate) / FULL

    def deliver(self, level_fs):
        return self.tau(self.command(level_fs))


# ------------------------------------------------------------------------------------------------ scoring

def _num(x):
    """JSON-safe: finite floats rounded to 6 significant digits, everything else None."""
    if isinstance(x, (np.floating, float)):
        x = float(x)
        return float(f'{x:.6g}') if math.isfinite(x) else None
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    return x


def _clean(obj):
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return _num(obj)


def _rms(x):
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    return float(np.sqrt(np.mean(x * x))) if x.size else None


def _rate(tr):
    """Median real-row rate (Hz) inside segments."""
    t, seg = tr['t'], tr['seg']
    if len(t) < 3:
        return None
    dt = np.diff(t)[np.diff(seg) == 0]
    dt = dt[dt > 0]
    return float(1.0 / np.median(dt)) if dt.size else None


def _duration(tr):
    """Seconds of recorded drive: the gaps inside segments (a dump's clock steps between its stretches)."""
    t, seg = tr['t'], tr['seg']
    if len(t) < 2:
        return 0.0
    dt = np.diff(t)[np.diff(seg) == 0]
    return float(dt[(dt > 0) & (dt < 1.0)].sum())


def case_masks(tr):
    """Boolean masks of the cases over the real rows (only where ours plays: `ready`)."""
    ready, speed = tr['ready'], tr['speed']
    moving = ready & (speed > fe.MIN_SPEED)
    ay, af, ar, srf = tr['ay'], tr['alpha_f'], tr['alpha_r'], tr['sr_f']
    load, brake, t, seg = tr['load'], tr['brake'], tr['t'], tr['seg']
    with np.errstate(invalid='ignore'):
        steady = np.abs(af) < 0.2
        left = moving & (ay > 3.0) & steady
        right = moving & (ay < -3.0) & steady
        peak = moving & (np.abs(af) > 0.4)                        # past the tyres' peak (RackForce's comment)
        drift = moving & (np.abs(ar) > 0.25) & ~peak              # the rear steps out, the fronts still bite
        lock = moving & (brake > 0.3) & (srf < -0.3)
        air = moving & (load < 500.0)                             # (a standing car's load reads 0 in the dump)
    landing = np.zeros_like(moving)
    prev_air = np.zeros_like(moving)
    prev_air[1:] = air[:-1] & (np.diff(seg) == 0) & (np.diff(t) < 1.0)
    landing = moving & prev_air & ~air
    return {'cornering_left': left, 'cornering_right': right, 'drift': drift, 'past_peak': peak, 'lock_up': lock,
            'airborne': air, 'landing': landing, 'all_moving': moving}


def hand_force(tr):
    """The force in the hand (rim torque in Nm and as a force in N, torque / (rim_mm / 2000)) of the played G29 force and the
    car's model force (the rim torque times rho), p50 / p90 at cruise, cornering and the limit (ffb_handforce's cases)."""
    ready, speed = tr['ready'], tr['speed']
    played_nm = tr['base'] * tr['peak']          # (base is already a share of full scale)
    model_nm = tr['last_torque'] * tr['rho']
    rim = tr['rim_mm']
    case = np.array([fh.case_of(sp, ay, af) if rd and sp > fe.MIN_SPEED else None
                     for rd, sp, ay, af in zip(ready, speed, tr['ay'], tr['alpha_f'])], dtype=object)
    out = {}
    for name in fh.CASES:
        sel = (case == name) & np.isfinite(played_nm) & np.isfinite(model_nm) & np.isfinite(rim)
        row = {'rows': int(sel.sum())}
        for kind, nm in (('played', played_nm), ('model', model_nm)):
            for unit, v in (('nm', np.abs(nm[sel])), ('n', np.abs(fh.newtons(nm[sel], rim[sel])))):
                for q in (50, 90):
                    row['%s_%s_p%d' % (kind, unit, q)] = float(np.percentile(v, q)) if v.size else None
        out[name] = row
    return out


def physics(tr):
    out = {}
    out['hand_force'] = hand_force(tr)
    ready = tr['ready']
    n_ready = int(ready.sum())
    out['playing_rows'] = n_ready
    if not n_ready:
        out['note'] = "the engine never became ready on this drive"
        return out
    # consistency: expected share from first principles against what the engine plays
    sel = ready & np.isfinite(tr['rim_model'])
    peak, scale, ceiling = tr['peak'], tr['scale'], tr['ceiling']
    classic = tr['mapping'] != 'knee'
    with np.errstate(invalid='ignore'):
        if classic:
            share = scale * tr['rim_model'] / peak
        else:
            share = np.array([wheel_torque.rim_share(r * q, p, 1.0, x * q, wheel_torque.KNEE) if np.isfinite(r) and np.isfinite(x) else np.nan
                              for r, p, x, q in zip(tr['rim_model'], peak, tr['x_top'], tr['rho'])])
        exp = np.clip(share, -wheel_torque.TOP, wheel_torque.TOP)
        exp = np.clip(exp, -ceiling, ceiling)
        budget = sel & (np.abs(share) >= wheel_torque.TOP - 1e-9)
        game_src = sel & (tr['source'] == 'game')
        rev = sel & tr['reverse']
        free = sel & ~budget & ~game_src & ~rev
        err = tr['base'] - exp
    out['consistency'] = {
        'rows': int(free.sum()),
        'rms_fs': _rms(err[free]),
        'worst_fs': float(np.nanmax(np.abs(err[free]))) if free.any() else None,
        'gated_budget_share': float(budget.sum() / max(sel.sum(), 1)),
        'gated_game_assist_share': float(game_src.sum() / max(sel.sum(), 1)),
        'gated_reverse_share': float(rev.sum() / max(sel.sum(), 1)),
        'game_assist_rms_fs': _rms(err[game_src & ~budget]),
    }
    # signs against the game, by case
    masks = case_masks(tr)
    cases = {}
    ours, game = tr['base'], tr['game']
    for name in CASES:
        m = masks[name] & np.isfinite(game)
        row = {'rows': int(m.sum())}
        if m.sum() >= 3:
            both = m & (np.abs(game) > SIGN_MIN_GAME) & (np.abs(ours) > SIGN_MIN_OURS)
            row['sign_rows'] = int(both.sum())
            row['sign_agree'] = float(np.mean(np.sign(ours[both]) == np.sign(game[both]))) if both.any() else None
            row['r'] = fm.correlation(game, ours, m)
            big = m & (np.abs(game) > SIGN_MIN_GAME)
            row['ours_over_game'] = float(np.median(np.abs(ours[big]) / np.abs(game[big]))) if big.any() else None
            row['ours_rms_fs'] = _rms(ours[m])
        cases[name] = row
    out['cases'] = cases
    rate = _rate(tr)
    if rate is not None and rate >= FULL_RATE:
        lag = fm.lag_ms(game, ours, rate, valid=ready & np.isfinite(game) & (tr['speed'] > fe.MIN_SPEED))
        out['lag_ms'] = lag
    else:
        out['lag_ms'] = None
        out['lag_note'] = "skipped: real rate %s Hz is under %d" % (None if rate is None else round(rate), FULL_RATE)
    return out


def wheel(tr, fit):
    ready = tr['ready']
    if not ready.any():
        return {'note': "never ready"}
    inten = tr['shaped'][ready]
    deliv = np.array([fit.deliver(v) for v in inten])
    err = deliv - inten
    f = float(fit.result.get('friction') or 0.0)
    out = {
        'rows': int(ready.sum()),
        'tracking_rms_fs': _rms(err),
        'tracking_worst_fs': float(np.max(np.abs(err))),
        'tracking_bias_fs': float(np.mean(err)),
        'tracking_rms_fs_above_dead_zone': _rms(err[np.abs(inten) > fit.dead / FULL]),
        'dead_zone_share': float(np.mean(np.abs(inten) <= fit.dead / FULL)),
        'below_friction_share': float(np.mean(np.abs(inten) < f)),
        'friction_fs': f,
        'deliver_over_intend': float(np.sum(np.abs(deliv)) / max(np.sum(np.abs(inten)), 1e-12)),
        'wheel_latency_ms': fit.latency_ms,
    }
    rate = _rate(tr)
    if rate is not None and rate >= FULL_RATE:
        lag = fm.lag_ms(tr['intended'], tr['shaped'], rate, valid=ready)
        out['shaper_lag_ms'] = lag
        out['delay_ms'] = fit.latency_ms + (lag or 0.0)
    else:
        out['shaper_lag_ms'] = None
        out['delay_ms'] = fit.latency_ms
        out['delay_note'] = "the fitted wheel latency only: the shaper's lag needs the real rate"
    return out


def _sim_slide(rack, fit, theta0, theta_a, speed, cls, x_top, peak, align_on, k_limit, delay_s, seconds, h=0.002,
               scale=None, column_damping=None, rho=1.0, gear=None):
    """A released wheel (steering wheel degrees) at theta0 under the model's restoring torque toward theta_a, through
    the driver's shaping and the fitted wheel. The engine's part sees the state `delay_s` late (the telemetry loop);
    with align_on the driver's spring (ffb_spring.DriverSpring) renders the engine-subtracted part locally (no delay
    in its position). `gear`: the driver's crossing stage (ffb_gear.Crossing) on the summed force, before the shaping.
    Returns (settle_s or None, overshoot share of the first error, zero crossings, final error)."""
    result = fit.result
    a_fs, f, beta = result['a_fs'], result['friction'], result['damping']
    # the column damping the driver adds (ffb_dynamics.column_targets: classic, a total with the wheel's own included;
    # knee, on top of the wheel's own)
    classic = scale is not None
    column = fe.ffb_dynamics.COLUMN_DAMPING_NMS_RAD * math.pi / 180.0 / peak
    if classic:
        beta = max(beta, column * scale)
    else:
        beta += column * rho * rho * (fe.RackForce.DAMPING_SCALE if column_damping is None else column_damping / 100.0)
    stick = fit.tau(result['breakaway'])
    lat = delay_s + fit.latency_ms / 1000.0
    delay = int(round(lat / h))
    deg = 180.0 / math.pi
    ratio, lock = rack.ratio, rack.lock
    probe = Sample(6000.0, 7500.0, speed=speed, car='acr/probe', game='acr')
    probe.tyre_load = (3000.0,) * 4

    def split(theta):
        # the engine's align() at this angle error: alpha = (theta - theta_a) / (ratio deg)
        probe.steer = theta / (lock * ratio * deg)
        alpha = (theta - theta_a) / (ratio * deg)
        probe.tyre_slip_angle = (alpha, alpha, 0.0, 0.0)
        probe.speed = speed
        return rack.align(probe)

    spring_e, spring_d = ffb_spring.DriverSpring(), ffb_spring.DriverSpring()
    hist = [theta0] * (delay + 2)
    theta, omega = theta0, 0.0
    e0 = theta0 - theta_a
    n = int(seconds / h)
    errs = np.zeros(n)
    tol = max(2.0, 0.05 * abs(e0))
    for i in range(n):
        seen = hist[-delay - 1] if delay else theta
        al = split(seen)
        if al is None:
            return None
        # the model's rim torque -> share of full scale (the engine's chain without the budget ceiling's reverse bits)
        torque = al['model']
        if classic:
            share = max(-wheel_torque.STEER_BUDGET, min(wheel_torque.STEER_BUDGET, scale * torque / peak))
        else:
            share = wheel_torque.rim_share(torque * rho, peak, 1.0, x_top * rho, wheel_torque.KNEE)
        cmd = share
        if align_on:
            k_fs = (scale * al['k'] / peak) if classic else \
                al['k'] * rho * wheel_torque.rim_share_slope(torque * rho, peak, 1.0, x_top * rho, wheel_torque.KNEE) / peak
            tau = wheel_torque.STEER_BUDGET
            cmd -= spring_e.force(al['theta_a_deg'], al['theta_deg'], k_fs, tau, k_limit, h)
            al_now = split(theta)
            k_now = (scale * al_now['k'] / peak) if classic else \
                al_now['k'] * rho * wheel_torque.rim_share_slope(al_now['model'] * rho, peak, 1.0, x_top * rho, wheel_torque.KNEE) / peak
            cmd += spring_d.force(al_now['theta_a_deg'], theta, k_now, tau, k_limit, h)
        cmd = min(max(cmd, -1.0), 1.0)
        if gear is not None:
            cmd = gear.step(cmd, h, omega, False)        # released: the observer's hands off
        u = fit.command(cmd)
        tq = fit.tau(u)
        if omega == 0.0 and abs(tq) <= stick:
            acc = 0.0
        else:
            sgn = math.copysign(1.0, omega) if omega != 0.0 else math.copysign(1.0, tq)
            acc = a_fs * (tq - f * sgn - beta * omega)
        new = omega + acc * h
        if omega != 0.0 and new * omega < 0.0 and abs(tq) <= stick:
            new = 0.0
        omega = new
        theta += omega * h
        hist.append(theta)
        hist.pop(0)
        errs[i] = theta - theta_a
    inside = np.abs(errs) <= tol
    settle = None
    if inside[-1]:
        outside = np.flatnonzero(~inside)
        settle = float((outside[-1] + 1) * h) if outside.size else 0.0
    sign0 = math.copysign(1.0, e0)
    over = float(max(0.0, np.max(-sign0 * errs)) / max(abs(e0), 1e-9))
    crossings = int(np.sum(np.diff(np.sign(errs)) != 0))
    return settle, over, crossings, float(errs[-1])


def dynamics(tr, fit, events=24, seconds=3.0, delay_s=0.020, column_damping=None):
    ready = tr['ready']
    with np.errstate(invalid='ignore'):
        cand = ready & (tr['speed'] > 8.0) & (np.abs(tr['alpha_f']) > 0.12) & np.isfinite(tr['theta_a']) \
            & (np.abs(tr['theta'] - tr['theta_a']) > 10.0)
    idx = np.flatnonzero(cand)
    out = {'slides_found': int(len(idx)), 'align_on': bool(tr['align_on']), 'telemetry_delay_s': delay_s}
    if not len(idx):
        out['note'] = "no slide with a front slip angle over 0.12 rad found"
        return out
    pick = idx[np.linspace(0, len(idx) - 1, min(events, len(idx))).astype(int)]
    rack = fe.RackForce()
    rack.mapping = tr['mapping']
    rows = []
    with _sandbox():
        for i in pick:
            rack.reset(None)
            rack.ratio, rack.lock = float(tr['ratio'][i]), float(tr['lock'][i])
            rack.assist = float(tr['cls'][i])
            # the car's stiffness as the drive learnt it
            rack.align_stiffness = (lambda v: (lambda: v))(float(tr['stiff'][i]))
            res = _sim_slide(rack, fit, float(tr['theta'][i]), float(tr['theta_a'][i]), float(tr['speed'][i]),
                             float(tr['cls'][i]), float(tr['x_top'][i]), float(tr['peak'][i]), bool(tr['align_on']),
                             float(tr['align_k_limit']), delay_s, seconds,
                             scale=float(tr['scale'][i]) if rack.classic() else None, column_damping=column_damping, rho=float(tr['rho'][i]))
            if res is not None:
                rows.append(res)
    if not rows:
        out['note'] = "no usable slide"
        return out
    settled = [r[0] for r in rows if r[0] is not None]
    out.update({
        'simulated': len(rows),
        'unsettled_share': 1.0 - len(settled) / len(rows),
        'settle_s_median': float(np.median(settled)) if settled else None,
        'settle_s_p90': float(np.percentile(settled, 90)) if settled else None,
        'overshoot_median': float(np.median([r[1] for r in rows])),
        'overshoot_worst': float(max(r[1] for r in rows)),
        'zero_crossings_median': float(np.median([r[2] for r in rows])),
        'final_error_deg_median': float(np.median([abs(r[3]) for r in rows])),
        'window_s': seconds,
    })
    return out


def safety(tr):
    ready = tr['ready']
    out = {}
    if not ready.any():
        return {'note': "never ready"}
    sh, base, tex, inten = tr['shaped'][ready], tr['base'][ready], tr['tex'][ready], tr['intended'][ready]
    out['clip_share'] = float(np.mean(np.abs(inten) >= fm.CLIP_AT))
    out['budget_share'] = float(np.mean(np.abs(base) >= wheel_torque.TOP - 1e-3))
    out['peak_abs_fs'] = float(np.max(np.abs(sh)))
    # thermal: the driver's 90 s mean square (a gap over a second counts as 0 output)
    th = R.thermal_mean(tr['t'], np.where(ready, tr['shaped'], 0.0))
    out['thermal_mean_square_max'] = float(th.max()) if len(th) else None
    out['thermal_limit'] = THERMAL_MEAN_LIMIT
    out['thermal_ratio'] = float(th.max() / THERMAL_MEAN_LIMIT) if len(th) else None
    # texture
    out['texture_rms_fs'] = _rms(tex)
    out['texture_p99_fs'] = float(np.percentile(np.abs(tex), 99))
    out['texture_over_steering_rms'] = (_rms(tex) / _rms(base)) if _rms(base) else None
    # standing still
    still = ready & (tr['speed'] < 0.5)
    out['standing_rows'] = int(still.sum())
    out['standing_rms_fs'] = _rms(tr['shaped'][still]) if still.any() else None
    out['standing_max_fs'] = float(np.max(np.abs(tr['shaped'][still]))) if still.any() else None
    rate = _rate(tr)
    if rate is not None and rate >= FULL_RATE:
        t, seg = tr['t'], tr['seg']
        # chatter: sign reversals per second of the output (a Schmitt trigger at 2 % of FS), per playing stretch
        rev = dur = 0.0
        for a, b in fm.runs(ready, int(fm.MIN_RUN * rate)):
            x = tr['shaped'][a:b]
            state, flips = 0, 0
            for v in x:
                if abs(v) < 0.02:
                    continue
                s = 1 if v > 0 else -1
                if state and s != state:
                    flips += 1
                state = s
            rev += flips
            dur += t[b - 1] - t[a]
        out['sign_reversals_per_s'] = rev / dur if dur else None
        hf, share = fm.band_energy(tr['shaped'], rate, valid=ready, band=(30.0, min(rate / 2 - 1, 120.0)))
        out['hf_rms_fs_30_120hz'], out['hf_share_30_120hz'] = hf, share
        dt = np.diff(t)
        step = np.abs(np.diff(tr['shaped'])) * FULL
        ok = ready[1:] & ready[:-1] & (dt > 0) & (dt < 0.1)
        limit = fe.SLEW * dt + 1.0
        out['slew_violation_share'] = float(np.mean(step[ok] > limit[ok])) if ok.any() else None
        out['slew_limited_share'] = float(np.mean(np.abs(np.diff(tr['intended'])[ok]) * FULL > fe.SLEW * dt[ok] + 1.0)) \
            if ok.any() else None
    else:
        out['rate_note'] = "chatter, high-frequency power and slew need the real rate (%s Hz here): skipped" % (
            None if rate is None else round(rate))
    return out


def stamp(path):
    """{"build", "oversteer", "settings"} from the capture's `.ffb.gz` sidecar header (the capture's own header for the
    build), whatever an older file lacks left out."""
    out = {}
    try:
        from oversteer import telemetry_capture as tc
        meta, _ = tc.read_capture(path)
        out.update({k: meta[k] for k in ('build', 'oversteer') if meta.get(k)})
    except Exception:
        pass
    try:
        meta, _ = ffb_record.load(path + ffb_record.SUFFIX)
        out.update({k: meta[k] for k in ('build', 'oversteer', 'settings') if meta.get(k)})
    except Exception:
        pass
    return out


def score(drive, hz=50.0, wheel_path=WHEEL, friction_comp=0, derate=1.0, wheel_peak=None, texture_level=None,
          align_on=None, events=24, mapping=None, column_damping=None, power_class=None):
    """Everything, as one JSON-able dict. `power_class` (rally / street / off) sets the assist class for this scoring."""
    if power_class is not None:
        kept = cp.power_class()
        cp.set_power_class(power_class)
        try:
            return score(drive, hz, wheel_path, friction_comp, derate, wheel_peak, texture_level, align_on, events, mapping,
                         column_damping)
        finally:
            cp.set_power_class(kept)
    notes = list(drive.notes)
    tr = run(drive, hz=hz, wheel_peak=wheel_peak, texture_level=texture_level, align_on=align_on,
             mapping=mapping)
    fit = WheelFit(load_wheel(wheel_path), friction_comp, derate)
    result = {
        'schema': SCHEMA, 'source': drive.source, 'kind': drive.kind, 'stamp': stamp(str(drive.source)), 'rows': int(len(tr['t'])),
        'duration_s': _duration(tr),
        'real_rate_hz': _rate(tr), 'engine_hz': hz, 'wheel': os.path.basename(wheel_path),
        'align_on': tr['align_on'], 'mapping': tr['mapping'], 'ready_share': float(np.mean(tr['ready'])) if len(tr['t']) else 0.0,
    }
    if not len(tr['t']):
        notes.append("no sample with the game's force (finalFF, bridge version 5): nothing to score")
        result.update(physics={'note': 'no data'}, wheel_fit={'note': 'no data'}, dynamics={'note': 'no data'},
                      safety={'note': 'no data'}, notes=notes)
        return _clean(result)
    if not tr['ready'].any():
        notes.append("the engine was never ready on this drive: no force to score")
    result['physics'] = physics(tr)
    result['wheel_fit'] = wheel(tr, fit)
    result['dynamics'] = dynamics(tr, fit, events=events, column_damping=column_damping)
    result['safety'] = safety(tr)
    result['notes'] = notes
    return _clean(result)


# ------------------------------------------------------------------------------------------------ report and compare

def flatten(d, prefix=''):
    out = {}
    for k, v in d.items():
        key = prefix + k
        if isinstance(v, dict):
            out.update(flatten(v, key + '.'))
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[key] = v
    return out


def _f(x, fmt='%.3f'):
    return '-' if x is None else (fmt % x)


def report(r, title=''):
    p, w, d, s = r['physics'], r['wheel_fit'], r['dynamics'], r['safety']
    lines = ['%s%s: %d rows, %.0f s, real rate %s Hz, engine %g Hz, ready %.0f %%, align_on %s' % (
        title + ' ' if title else '', os.path.basename(str(r['source'])), r['rows'], r['duration_s'],
        _f(r['real_rate_hz'], '%.0f'), r['engine_hz'], 100 * r['ready_share'], r['align_on'])]
    st = r.get('stamp') or {}
    if st:
        lines.append('BUILD    %s (Oversteer %s)' % (st.get('build', 'unknown'), st.get('oversteer', '-')))
        settings = st.get('settings')
        if settings:
            lines.append('SETTINGS ' + ', '.join('%s %s' % (k, json.dumps(v, sort_keys=True))
                                                for k, v in sorted(settings.get('profile', {}).items())))
            lines.append('LEARNT   ' + ', '.join('%s %s' % (k, json.dumps(v)) for k, v in sorted(settings.get('learnt', {}).items())))
    c = p.get('consistency')
    if c:
        lines += ['PHYSICS  consistency: rms %s FS, worst %s FS over %d free rows; gated: budget %.1f %%, game assist %.1f %% '
                  '(rms %s), reverse %.1f %%' % (_f(c['rms_fs'], '%.2g'), _f(c['worst_fs'], '%.2g'), c['rows'],
                                                 100 * c['gated_budget_share'], 100 * c['gated_game_assist_share'],
                                                 _f(c['game_assist_rms_fs'], '%.3f'), 100 * c['gated_reverse_share'])]
        lines.append('  %-16s %7s %7s %6s %7s' % ('case', 'rows', 'sign', 'r', 'ours/game'))
        for name in CASES:
            row = p['cases'][name]
            if row.get('sign_rows') is None:
                lines.append('  %-16s %7d %7s' % (name, row['rows'], '-'))
                continue
            lines.append('  %-16s %7d %7s %6s %7s' % (name, row['rows'], _f(row['sign_agree'], '%.3f'), _f(row['r'], '%.2f'),
                                                      _f(row['ours_over_game'], '%.2f')))
        lines.append('  lag vs the game: %s' % (('%s ms' % _f(p['lag_ms'], '%.1f')) if p.get('lag_ms') is not None
                                                else p.get('lag_note', '-')))
    else:
        lines.append('PHYSICS  ' + p.get('note', '-'))
    hf = p.get('hand_force')
    if hf:
        lines.append('  hand force, p50/p90 (played G29 | car model; Nm at the rim, N at the rim)')
        for name in fh.CASES:
            row = hf[name]
            lines.append('  %-16s %7d  played %s/%s Nm %s/%s N | model %s/%s Nm %s/%s N' % (
                name, row['rows'], _f(row['played_nm_p50'], '%.2f'), _f(row['played_nm_p90'], '%.2f'),
                _f(row['played_n_p50'], '%.1f'), _f(row['played_n_p90'], '%.1f'),
                _f(row['model_nm_p50'], '%.2f'), _f(row['model_nm_p90'], '%.2f'),
                _f(row['model_n_p50'], '%.1f'), _f(row['model_n_p90'], '%.1f')))
    if 'tracking_rms_fs' in w:
        lines.append('WHEEL    tracking rms %s FS (above the dead zone %s), worst %s, bias %s; dead zone %.1f %% of rows, '
                     'below friction (%s) %.1f %%; delivered/intended %s; delay %s ms%s' % (
                         _f(w['tracking_rms_fs'], '%.4f'), _f(w['tracking_rms_fs_above_dead_zone'], '%.4f'),
                         _f(w['tracking_worst_fs'], '%.3f'), _f(w['tracking_bias_fs'], '%+.4f'),
                         100 * w['dead_zone_share'], _f(w['friction_fs'], '%.3f'), 100 * w['below_friction_share'],
                         _f(w['deliver_over_intend'], '%.3f'), _f(w['delay_ms'], '%.1f'),
                         '' if 'delay_note' not in w else ' (%s)' % w['delay_note']))
    else:
        lines.append('WHEEL    ' + w.get('note', '-'))
    if 'simulated' in d:
        lines.append('DYNAMICS %d of %d slides simulated (align_on %s, %.0f ms telemetry delay): settle median %s s, p90 %s s, '
                     'unsettled in %.1f s: %.0f %%; overshoot median %.0f %%, worst %.0f %%; zero crossings median %.0f; '
                     'final error %s deg' % (
                         d['simulated'], d['slides_found'], d['align_on'], 1000 * d['telemetry_delay_s'],
                         _f(d['settle_s_median'], '%.2f'), _f(d['settle_s_p90'], '%.2f'), d['window_s'],
                         100 * d['unsettled_share'], 100 * d['overshoot_median'], 100 * d['overshoot_worst'],
                         d['zero_crossings_median'], _f(d['final_error_deg_median'], '%.1f')))
    else:
        lines.append('DYNAMICS ' + d.get('note', '-'))
    if 'clip_share' in s:
        lines.append('SAFETY   clip %.2f %%, budget %.2f %%, peak %.2f FS; thermal max %.3f of 0.315 (%.0f %%); texture rms %s FS '
                     '(p99 %s, %s of the steering rms); standing %d rows rms %s max %s%s' % (
                         100 * s['clip_share'], 100 * s['budget_share'], s['peak_abs_fs'], s['thermal_mean_square_max'],
                         100 * s['thermal_ratio'], _f(s['texture_rms_fs'], '%.4f'), _f(s['texture_p99_fs'], '%.4f'),
                         _f(s['texture_over_steering_rms'], '%.3f'), s['standing_rows'], _f(s['standing_rms_fs'], '%.4f'),
                         _f(s['standing_max_fs'], '%.4f'),
                         '' if 'sign_reversals_per_s' not in s else
                         '; chatter %s reversals/s, hf 30-120 Hz rms %s FS (%s of variance), slew violations %s, '
                         'slew-limited %s' % (_f(s['sign_reversals_per_s'], '%.2f'), _f(s['hf_rms_fs_30_120hz'], '%.4f'),
                                              _f(s['hf_share_30_120hz'], '%.3f'), _f(s['slew_violation_share'], '%.4f'),
                                              _f(s['slew_limited_share'], '%.4f'))))
        if 'rate_note' in s:
            lines.append('         ' + s['rate_note'])
    else:
        lines.append('SAFETY   ' + s.get('note', '-'))
    for n in r['notes']:
        lines.append('note: ' + n)
    return '\n'.join(lines)


def compare(a, b, label_a='A', label_b='B'):
    """A table of every numeric metric that differs between two results: A, B, delta, relative."""
    fa, fb = flatten(a), flatten(b)
    skip = ('schema', 'rows', 'duration_s', 'real_rate_hz', 'engine_hz')
    lines = ['%-52s %12s %12s %12s %8s' % ('metric', label_a, label_b, 'delta', 'rel')]
    same = 0
    for key in sorted(set(fa) | set(fb)):
        if key in skip:
            continue
        x, y = fa.get(key), fb.get(key)
        if x is None or y is None:
            lines.append('%-52s %12s %12s' % (key, _f(x, '%.5g'), _f(y, '%.5g')))
            continue
        if x == y:
            same += 1
            continue
        rel = (y - x) / abs(x) * 100 if x else math.nan
        lines.append('%-52s %12.5g %12.5g %+12.4g %7s' % (key, x, y, y - x, '-' if not math.isfinite(rel) else '%+.1f%%' % rel))
    lines.append('(%d metrics unchanged)' % same)
    return '\n'.join(lines)
