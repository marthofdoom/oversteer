"""Objective A/B numbers for Countersteer FFB: "only replace forces we can improve" (marth).

Two force signals over the same drive, both in fractions of full scale (-1..1):

- game: the game's own force. A capture has finalFF (Sample.game_ffb), which stands in for the
  driver's game_ffb_raw (not in captures).
- ours: what the engine would send, computed offline: RackForce (fitted to finalFF as it would be
  to the raw level, ready only when it fits) plus Texture on the same samples, on the capture's own
  clock, clamped to full scale like FfbEngine._play does.

and the reference they are judged against, the front tyres' Mz (FL + FR).

Metrics (each of them for 'game' and for 'ours'; the series are resampled on a uniform grid at the
capture's rate first, and only moving, ready samples with data on both sides count, see `analyse`):

- corr: Pearson r of the force with front Mz at zero lag.
- lag_ms: the lag at the peak of the cross-correlation with front Mz, parabolic-interpolated, over
  +-MAX_LAG_MS. Positive: the force comes after the Mz.
- clip: the share of samples at or above CLIP_AT of full scale.
- band_rms, band_share: RMS (fraction of full scale) and share of the variance in the BAND (5 to
  30 Hz) -- the "detail" in the force, from windowed FFTs of the contiguous runs.
- going light: where the front tyres' combined slip is past the peak (the slip at which binned
  mean |Mz| is greatest) a real tyre's Mz falls off. An event is a fall of the smoothed |signal| by
  LIGHT_DROP of its own last LIGHT_WINDOW seconds' maximum. recall: the share of Mz events that a
  force event follows within LIGHT_TOL; precision: the share of force events that an Mz event
  matches. 'ours' and 'game' are scored the same way, so the two are comparable.
- rms_diff (ours only): RMS of ours minus game, fraction of full scale.

Pure functions on numpy arrays, apart from `replay_forces` which drives the engine's own classes."""
import math

import numpy as np

CLIP_AT = 0.98
BAND = (5.0, 30.0)
MAX_LAG_MS = 250.0
MIN_RUN = 2.0                            # s: shorter contiguous stretches are not analysed for lag and band
GAP = 0.1                                # s: grid points further than this from a real sample are not valid
MIN_SPEED = 5.0 / 3.6                    # m/s (the engine's MIN_SPEED)
LIGHT_SMOOTH = 0.05                      # s
LIGHT_WINDOW = 0.5                       # s
LIGHT_DROP = 0.15                        # share of the window's maximum
LIGHT_GAP = 0.3                          # s: events closer than this are one
LIGHT_TOL = 0.25                         # s
LOW_RATE = 200.0                         # Hz: below it the capture was thinned (record for FFB tuning)
SLIP_BINS = 20


def runs(mask, min_len):
    """[(start, stop)) of the stretches of true values at least min_len long."""
    mask = np.asarray(mask, dtype=bool)
    if not mask.size:
        return []
    edges = np.flatnonzero(np.diff(np.concatenate(([0], mask.view(np.int8), [0]))))
    return [(int(a), int(b)) for a, b in zip(edges[0::2], edges[1::2]) if b - a >= min_len]


def estimate_rate(t):
    """Median sample rate (Hz) of the times t, or None."""
    t = np.asarray(t, dtype=float)
    if t.size < 3:
        return None
    dt = np.diff(t)
    dt = dt[dt > 0]
    return float(1.0 / np.median(dt)) if dt.size else None


def correlation(a, b, mask=None):
    """Pearson r of a and b over the true values of mask (None: not defined)."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if mask is not None:
        a, b = a[mask], b[mask]
    if a.size < 3:
        return None
    a, b = a - a.mean(), b - b.mean()
    den = math.sqrt(float(a @ a) * float(b @ b))
    return float(a @ b) / den if den > 0 else None


def lag_ms(ref, x, rate, valid=None, max_ms=MAX_LAG_MS):
    """The lag (ms) of x behind ref at the peak of their cross-correlation, summed over the contiguous
    stretches of `valid`; positive: x comes later. None if there is too little to judge."""
    ref, x = np.asarray(ref, dtype=float), np.asarray(x, dtype=float)
    valid = np.ones(ref.shape, dtype=bool) if valid is None else valid
    span = int(round(max_ms / 1000.0 * rate))
    total = np.zeros(2 * span + 1)
    used = 0
    for a, b in runs(valid, max(int(MIN_RUN * rate), 2 * span + 2)):
        r, s = ref[a:b] - ref[a:b].mean(), x[a:b] - x[a:b].mean()
        size = 1 << (2 * (b - a) - 1).bit_length()
        c = np.fft.irfft(np.fft.rfft(s, size) * np.conj(np.fft.rfft(r, size)), size)   # c[k] = sum r[n] s[n+k]
        total += np.concatenate((c[-span:], c[:span + 1])) if span else c[:1]
        used += b - a
    if not used or not total.any():
        return None
    k = int(np.argmax(total))
    offset = 0.0
    if 0 < k < total.size - 1:
        y0, y1, y2 = total[k - 1], total[k], total[k + 1]
        den = y0 - 2 * y1 + y2
        offset = 0.5 * (y0 - y2) / den if den else 0.0
    return (k - span + offset) / rate * 1000.0


def clipping_share(x, mask=None, at=CLIP_AT):
    x = np.asarray(x, dtype=float)
    x = x if mask is None else x[mask]
    return float(np.mean(np.abs(x) >= at)) if x.size else None


def band_energy(x, rate, valid=None, band=BAND):
    """(band_rms, band_share): the RMS of x inside `band` Hz and the share of its variance there, from Hann
    windowed FFTs of the contiguous stretches of `valid` (time-weighted). (None, None) with too little."""
    x = np.asarray(x, dtype=float)
    valid = np.ones(x.shape, dtype=bool) if valid is None else valid
    band_var = total_var = weight = 0.0
    for a, b in runs(valid, int(MIN_RUN * rate)):
        seg = x[a:b] - x[a:b].mean()
        n = seg.size
        win = np.hanning(n)
        power = np.abs(np.fft.rfft(seg * win)) ** 2
        freq = np.fft.rfftfreq(n, 1.0 / rate)
        scale = 2.0 / (n * float(win @ win))
        total_var += n * scale * float(power[1:].sum())
        band_var += n * scale * float(power[(freq >= band[0]) & (freq <= band[1])].sum())
        weight += n
    if not weight or total_var <= 0:
        return None, None
    return math.sqrt(band_var / weight), band_var / total_var


def rms(x, mask=None):
    x = np.asarray(x, dtype=float)
    x = x if mask is None else x[mask]
    return float(np.sqrt(np.mean(x * x))) if x.size else None


def slip_peak(slip, mz, mask=None, bins=SLIP_BINS):
    """The slip at which |Mz| is greatest on average (binned from 0 up to the 99th percentile of the slip), or None."""
    slip, mz = np.asarray(slip, dtype=float), np.abs(np.asarray(mz, dtype=float))
    if mask is not None:
        slip, mz = slip[mask], mz[mask]
    if slip.size < 10 * bins:
        return None
    top = float(np.percentile(slip, 99))
    if top <= 0:
        return None
    edges = np.linspace(0.0, top, bins + 1)
    index = np.clip(np.digitize(slip, edges) - 1, 0, bins - 1)
    best, best_mean = None, -1.0
    for i in range(bins):
        sel = index == i
        if sel.sum() >= max(10, slip.size // (4 * bins)):
            mean = float(mz[sel].mean())
            if mean > best_mean:
                best, best_mean = i, mean
    return None if best is None else float(edges[best + 1])


def drop_events(x, rate, valid=None, smooth=LIGHT_SMOOTH, window=LIGHT_WINDOW, drop=LIGHT_DROP, gap=LIGHT_GAP):
    """Indices where |x| (smoothed) falls below (1 - drop) of its own maximum over the last `window` seconds:
    the first of each fall, at least `gap` seconds apart. Maxima under a tenth of the signal's 98th percentile
    are noise and never start one."""
    ax = np.abs(np.asarray(x, dtype=float))
    k = max(int(round(smooth * rate)), 1)
    ax = np.convolve(ax, np.ones(k) / k, mode='same')
    w = max(int(round(window * rate)), 2)
    # Windowed maximum without scipy: a sliding_window_view over the past w samples
    padded = np.concatenate((np.full(w - 1, ax[0]), ax))
    peak = np.lib.stride_tricks.sliding_window_view(padded, w).max(axis=1)
    floor = 0.1 * float(np.percentile(ax, 98))
    falling = (ax < (1.0 - drop) * peak) & (peak > floor)
    if valid is not None:
        falling &= valid
    starts = np.flatnonzero(falling & ~np.concatenate(([False], falling[:-1])))
    keep, last = [], -10 ** 9
    for i in starts:
        if i - last >= gap * rate:
            keep.append(int(i))
            last = i
    return keep


def going_light(mz, force, slip, rate, valid=None, tol=LIGHT_TOL):
    """Recall and precision of `force` dropping where front Mz drops, at slip past the peak (module
    docstring). {'mz_events', 'force_events', 'recall', 'precision', 'slip_peak'}; the ratios None with no
    events to judge."""
    out = {'mz_events': 0, 'force_events': 0, 'recall': None, 'precision': None, 'slip_peak': None}
    slip = np.asarray(slip, dtype=float)
    peak = slip_peak(slip, mz, valid)
    if peak is None:
        return out
    out['slip_peak'] = peak
    past = slip > peak
    mz_ev = [i for i in drop_events(mz, rate, valid) if past[i]]
    f_ev = [i for i in drop_events(force, rate, valid) if past[i]]
    out['mz_events'], out['force_events'] = len(mz_ev), len(f_ev)
    reach = tol * rate
    if mz_ev:
        out['recall'] = sum(any(0 <= j - i <= reach or 0 <= i - j <= reach / 2 for j in f_ev) for i in mz_ev) / len(mz_ev)
    if f_ev:
        out['precision'] = sum(any(0 <= j - i <= reach or 0 <= i - j <= reach / 2 for i in mz_ev) for j in f_ev) / len(f_ev)
    return out


def metrics(force, mz, slip, rate, valid, ref_force=None):
    """All of the above for one force against front Mz, over the `valid` samples."""
    band_rms, band_share = band_energy(force, rate, valid)
    out = {
        'corr': correlation(force, mz, valid),
        'lag_ms': lag_ms(mz, force, rate, valid),
        'clip': clipping_share(force, valid),
        'rms': rms(force, valid),
        'band_rms': band_rms,
        'band_share': band_share,
        'light': going_light(mz, force, slip, rate, valid),
    }
    if ref_force is not None:
        out['rms_diff'] = rms(np.asarray(force) - np.asarray(ref_force), valid)
    return out


def front_slip(sample):
    """The front tyres' combined slip (the larger of FL and FR), else their slip angle; None without either."""
    for values in (sample.combined_slip, sample.tyre_slip_angle):
        if values is not None:
            front = [abs(v) for v in values[:2] if v is not None and math.isfinite(v)]
            if front:
                return max(front)
    return None


def replay_forces(items, texture_level=None):
    """Drive the engine's own RackForce and Texture with (t, Sample) pairs on the capture clock, the
    game's raw level being finalFF at full scale. Samples without finalFF or front Mz are left out.
    Returns a dict of lists: t, mz, game, ours (fractions of full scale), slip, speed, ready (bool),
    lap_distance, stage_length, sector, stage_time."""
    from .ffb_engine import FULL, RackForce, front_mz
    from .ffb_texture import Texture

    rack, texture = RackForce(), Texture()
    if texture_level is not None:
        texture.set(texture_level)
    out = {k: [] for k in ('t', 'mz', 'game', 'ours', 'slip', 'speed', 'ready', 'lap_distance', 'stage_length',
                           'sector', 'stage_time')}
    for t, s in items:
        mz = front_mz(s)
        if s.game_ffb is None or mz is None:
            continue
        rack.feed(t, s, s.game_ffb * FULL, True)
        base = rack.force(s)
        value = min(max(base + texture.force(t, s, base), -FULL), FULL)
        slip = front_slip(s)
        out['t'].append(t)
        out['mz'].append(mz)
        out['game'].append(s.game_ffb)
        out['ours'].append(value / FULL)
        out['slip'].append(math.nan if slip is None else slip)
        out['speed'].append(s.speed or 0.0)
        out['ready'].append(bool(rack.ready))
        out['lap_distance'].append(math.nan if s.lap_distance is None else s.lap_distance)
        out['stage_length'].append(math.nan if not s.stage_length else s.stage_length)
        out['sector'].append(-1 if s.sector is None else s.sector)
        out['stage_time'].append(math.nan if s.stage_time is None else s.stage_time)
    return out


def _grid(series, rate):
    """Every series on a uniform grid at `rate`, plus `valid` (a real sample within GAP)."""
    t = np.asarray(series['t'], dtype=float)
    grid = np.arange(t[0], t[-1], 1.0 / rate)
    near = np.searchsorted(t, grid)
    left, right = np.clip(near - 1, 0, t.size - 1), np.clip(near, 0, t.size - 1)
    gap = np.minimum(np.abs(grid - t[left]), np.abs(t[right] - grid))
    out = {'t': grid, 'near': gap <= GAP}
    for name, values in series.items():
        if name == 't':
            continue
        v = np.asarray(values, dtype=float)
        if name in ('ready', 'sector'):
            out[name] = v[np.where(np.abs(grid - t[left]) <= np.abs(t[right] - grid), left, right)]
        elif name == 'slip' or name.startswith('lap') or name.startswith('stage'):
            good = np.isfinite(v)
            out[name] = np.interp(grid, t[good], v[good]) if good.any() else np.full(grid.shape, np.nan)
        else:
            out[name] = np.interp(grid, t, v)
    return out


def split_sections(g, count):
    """[(label, start, stop)) of the grid by the game's sector where it has one, else `count` equal slices of
    the stage's length by lap_distance (count 0: none). A new run is where lap_distance goes back."""
    n = g['t'].size
    if n == 0:
        return []
    dist, length = g['lap_distance'], g['stage_length']
    if np.isfinite(g['sector']).any() and (g['sector'] >= 0).any():
        key = g['sector'].astype(int)
        label = lambda k: 'sector %d' % (k + 1)           # noqa: E731
    elif count and np.isfinite(dist).any() and np.isfinite(length).any():
        top = float(np.nanmax(length))
        key = np.where(np.isfinite(dist), np.clip((dist / top * count).astype(int), 0, count - 1), -1)
        label = lambda k: 'section %d/%d' % (k + 1, count)  # noqa: E731
    else:
        return []
    out = []
    for k in sorted(set(key.tolist()) - {-1}):
        for a, b in runs(key == k, 1):
            out.append((label(k), a, b))
    return sorted(out, key=lambda x: x[1])


def analyse(items, sections=0, texture_level=None, only_ready=True, rate=None):
    """The A/B numbers for (t, Sample) pairs. Returns a JSON-able dict: meta (samples, rate, ready share,
    valid seconds), game, ours, sections [{label, seconds, game, ours}], or {'error': why}."""
    series = replay_forces(items, texture_level)
    if len(series['t']) < 10:
        return {'error': 'no samples with finalFF and front Mz (an ACR/AC capture from the version 5 bridge is needed)'}
    raw_rate = estimate_rate(series['t'])
    rate = float(rate or min(max(round(raw_rate or 60.0), 30), 333))
    g = _grid(series, rate)
    ready = g['ready'] > 0.5
    moving = g['speed'] > MIN_SPEED
    base = g['near'] & moving
    valid = base & ready if only_ready and (base & ready).sum() >= MIN_RUN * rate else base
    slip = g['slip']
    out = {'meta': {
        'samples': len(series['t']), 'capture_rate_hz': raw_rate, 'grid_rate_hz': rate,
        'ready_share': float(ready[base].mean()) if base.any() else 0.0,
        'seconds': float(valid.sum() / rate), 'only_ready': bool(valid is not base),
        'low_rate': bool(raw_rate is not None and raw_rate < LOW_RATE),
    }}
    out['game'] = metrics(g['game'], g['mz'], slip, rate, valid)
    out['ours'] = metrics(g['ours'], g['mz'], slip, rate, valid, ref_force=g['game'])
    out['sections'] = []
    for label, a, b in split_sections(g, sections):
        v = np.zeros_like(valid)
        v[a:b] = valid[a:b]
        if v.sum() < MIN_RUN * rate:
            continue
        out['sections'].append({'label': label, 'seconds': float(v.sum() / rate),
                                'game': metrics(g['game'], g['mz'], slip, rate, v),
                                'ours': metrics(g['ours'], g['mz'], slip, rate, v, ref_force=g['game'])})
    return out


def _num(value, fmt):
    return '-' if value is None else fmt % value


ROWS = (
    ('corr with front Mz', lambda m: _num(m['corr'], '%.3f')),
    ('lag vs Mz (ms)', lambda m: _num(m['lag_ms'], '%+.1f')),
    ('clipping (>=98 % FS)', lambda m: _num(None if m['clip'] is None else m['clip'] * 100, '%.2f %%')),
    ('rms (% FS)', lambda m: _num(None if m['rms'] is None else m['rms'] * 100, '%.2f')),
    ('5-30 Hz rms (% FS)', lambda m: _num(None if m['band_rms'] is None else m['band_rms'] * 100, '%.3f')),
    ('5-30 Hz share of var', lambda m: _num(None if m['band_share'] is None else m['band_share'] * 100, '%.2f %%')),
    ('going light recall', lambda m: _num(m['light']['recall'], '%.2f')),
    ('going light precision', lambda m: _num(m['light']['precision'], '%.2f')),
    ('going light events Mz/force', lambda m: '%d/%d' % (m['light']['mz_events'], m['light']['force_events'])),
    ('rms diff vs game (% FS)', lambda m: _num(None if m.get('rms_diff') is None else m['rms_diff'] * 100, '%.2f')),
)


def table(result, title=''):
    """The result of `analyse` as a compact text table (game | ours)."""
    if 'error' in result:
        return '%s: %s' % (title or 'ffb-ab', result['error'])
    meta = result['meta']
    lines = []
    if title:
        lines.append(title)
    lines.append('%d samples, capture %s Hz, grid %.0f Hz, %.1f s judged (%s), engine ready %.0f %% of the time moving'
                 % (meta['samples'], _num(meta['capture_rate_hz'], '%.0f'), meta['grid_rate_hz'], meta['seconds'],
                    'ready only' if meta['only_ready'] else 'all moving', meta['ready_share'] * 100))
    if meta['low_rate']:
        lines.append('WARNING: the capture is thinned (%.0f Hz): detail and lag are blurred. Use "Record for FFB tuning".'
                     % meta['capture_rate_hz'])

    def block(game, ours, head):
        out = ['%-30s %12s %12s' % (head, 'game', 'ours')]
        for name, fn in ROWS:
            out.append('%-30s %12s %12s' % (name, fn(game), fn(ours)))
        return out

    lines += block(result['game'], result['ours'], 'whole drive')
    for sec in result['sections']:
        lines += block(sec['game'], sec['ours'], '%s (%.0f s)' % (sec['label'], sec['seconds']))
    return '\n'.join(lines)
