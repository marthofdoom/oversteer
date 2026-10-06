#!/usr/bin/env python3
"""Corner geometry and crests from the driven path, checked against the
game's own pace notes.

For every ACR stage with runs in the captures, the driven path's
curvature is built per run on a 5 m grid of the distance along the stage
(course curvature: yaw rate plus the rate of the body slip angle, over
the speed; no positions needed), and the runs are merged into a median
profile. Corners are the stretches over 1/400 1/m; each gets its
direction, apex (the tightest point), minimum radius, turn angle,
length, and whether it tightens or opens. A radius -> pace-note grade
scale is learnt from the notes (leave-one-stage-out) and scored.

Crests come from the vertical acceleration over the speed squared (the
car-frame a_z is kinematic in ACR: a crest at speed reads negative),
merged over runs the same way, and are checked against the notes'
OverCrest / Jump calls.

The pace notes are Koenvh1/PacenotePal's (MPL-2.0) extraction of the
game's DT_Pacenote tables, used here as validation ground truth only and
not shipped. Fetch them once with

    gh api "repos/Koenvh1/PacenotePal/contents/pacenotes/<file>.yml" --jq .content | base64 -d

into $PACENOTES (default ~/.cache/oversteer-extrapolation/pacenotes).

    python3 research/extrapolation/corners.py
"""
import json
import os
import re

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

import common  # noqa: E402

STEP = 5.0
PACENOTES = os.environ.get('PACENOTES', os.path.expanduser('~/.cache/oversteer-extrapolation/pacenotes'))
# capture track name -> pace-note file (the stage table's stage names)
STAGES = {'Wales Afon Bidno': 'Afon Bidno - Severn', 'Greece Loutraki - Aghii Theodor': 'Loutraki - Aghii Theodori',
          'Greece Elatia': 'Elatia', 'Alsace For_t': 'Forêt de Munster', 'Alsace Steigenbach': 'Steigenbach',
          'Alsace Obersteigen': 'Obersteigen', 'Alsace Sommet': 'Sommet de Munster',
          'Greece Aghii Theodori - Loutrak': 'Aghii Theodori - Loutraki'}
CORNER_K = 1 / 400.0           # 1/m: a corner is tighter than a 400 m radius
GRADES = ['HP', '1', '2', '3', '4', '5', '6', 'flat']


def read_notes(stage):
    path = os.path.join(PACENOTES, stage + '.yml')
    if not os.path.exists(path):
        return None
    notes, d = [], None
    for line in open(path, encoding='utf-8'):
        m = re.match(r'- distance: ([\d.]+)', line)
        if m:
            d = float(m.group(1))
        m = re.match(r'\s+notes: \[(.*)\]', line)
        if m and d is not None:
            notes.append((d, [x.strip() for x in m.group(1).split(',')]))
    return notes


def corner_notes(notes):
    """(distance, direction +1 left / -1 right, grade index into GRADES,
    tokens) for the notes that call a corner."""
    out = []
    for d, toks in notes:
        for tok in toks:
            m = re.match(r'(Left|Right)(\d|HP|OpenHP|Square|Flat|Kink)$', tok)
            if not m:
                continue
            g = m.group(2)
            grade = {'HP': 0, 'OpenHP': 0, 'Square': 1, 'Flat': 7, 'Kink': 7}.get(g)
            if grade is None:
                grade = int(g)
            out.append((d, 1 if m.group(1) == 'Left' else -1, grade, toks))
            break
    return out


def crest_notes(notes):
    return [d for d, toks in notes if any('Crest' in t or 'Jump' in t for t in toks)]


def run_profiles(cap, idx, grid):
    t, v = cap['t'][idx], cap['speed'][idx]
    d = cap['lap_distance'][idx]
    vel = cap['vel'][idx]
    beta = np.arctan2(vel[:, 1], np.maximum(vel[:, 0], 0.5))
    beta_rate = np.gradient(common.smooth(beta, 9), t)
    k = (cap['yaw_rate'][idx] + beta_rate) / np.maximum(v, 3.0)
    az = np.clip(cap['accel'][idx][:, 2], -30, 30)
    kv = az / np.maximum(v, 8.0) ** 2
    ok = (v > 4) & np.isfinite(k)
    # leave out the stretch around a stop (an off) so it does not bend the median
    k = np.where(ok, common.smooth(k, 15), np.nan)
    kv = np.where(v > 12, common.smooth(kv, 31), np.nan)
    order = np.argsort(d)
    kk = common.unwrap_resample(d[ok], k[ok], grid)
    kvv = common.unwrap_resample(d[v > 12], kv[v > 12], grid) if (v > 12).sum() > 50 else grid * np.nan
    del order
    return kk, kvv


def detect_corners(grid, k):
    out = []
    a = np.abs(np.nan_to_num(k))
    on = a > CORNER_K
    i = 0
    while i < len(on):
        if not on[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(on) and on[j + 1] and np.sign(k[j + 1]) == np.sign(k[i]):
            j += 1
        seg = slice(i, j + 1)
        apex = i + int(np.argmax(a[seg]))
        angle = float(np.nansum(k[seg]) * STEP)
        half = (i + j) // 2
        r_in = 1 / max(np.nanmax(a[i:half + 1]), 1e-6)
        r_out = 1 / max(np.nanmax(a[half:j + 1]), 1e-6)
        out.append({'d0': grid[i], 'd1': grid[j], 'apex': grid[apex], 'dir': int(np.sign(k[apex])),
                    'radius': 1 / a[apex], 'angle_deg': abs(np.degrees(angle)), 'length': grid[j] - grid[i] + STEP,
                    'shape': 'tightens' if r_out < 0.7 * r_in else ('opens' if r_in < 0.7 * r_out else 'constant')})
        i = j + 1
    # keep the corners that turn the car at least 15 degrees, or are tighter than 100 m
    return [c for c in out if c['angle_deg'] >= 15 or c['radius'] < 100]


def match(notes_c, corners, lead=(-40, 160)):
    """For each corner note: the nearest detected corner of the same
    direction whose apex lies lead[0]..lead[1] m after the note."""
    pairs = []
    for d, direction, grade, _toks in notes_c:
        best = None
        for c in corners:
            off = c['apex'] - d
            if c['dir'] == direction and lead[0] <= off <= lead[1]:
                if best is None or abs(off - 50) < abs(best['apex'] - d - 50):
                    best = c
        pairs.append((d, direction, grade, best))
    return pairs


def learn_scale(pairs):
    """Radius thresholds between grades: the geometric mean of the
    neighbouring grades' median radii."""
    med = {}
    for g in range(8):
        rs = [c['radius'] for _d, _dir, gg, c in pairs if c is not None and gg == g]
        if len(rs) >= 2:
            med[g] = float(np.median(rs))
    return med


def classify(radius, med):
    if not med:
        return None
    gs = sorted(med)
    return min(gs, key=lambda g: abs(np.log(radius) - np.log(med[g])))


def main():
    allr = common.all_runs('acr')
    stages = {}
    for track, stage in STAGES.items():
        notes = read_notes(stage)
        runs = [r for r in allr if r[3] == track]
        if notes is None or not runs:
            continue
        lo = min(np.nanmin(r[1]['lap_distance'][r[2]]) for r in runs)
        hi = max(np.nanmax(r[1]['lap_distance'][r[2]]) for r in runs)
        grid = np.arange(lo, hi, STEP)
        ks, kvs = [], []
        for _name, cap, idx, _tr, _car, _g in runs:
            k, kv = run_profiles(cap, idx, grid)
            ks.append(k)
            kvs.append(kv)
        ks, kvs = np.array(ks), np.array(kvs)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            k = np.nanmedian(ks, axis=0)
            kv = np.nanmedian(kvs, axis=0)
        covered = np.isfinite(k)
        cn = [n for n in corner_notes(notes) if lo + 50 <= n[0] <= hi - 100]
        corners = detect_corners(grid, k)
        stages[track] = {'stage': stage, 'grid': grid, 'k': k, 'kv': kv, 'runs': len(runs), 'notes': notes,
                         'corner_notes': cn, 'corners': corners, 'pairs': match(cn, corners),
                         'span': (lo, hi), 'repeat': repeatability(ks)}
    # leave-one-stage-out grade scale
    report = {'stages': {}, 'overall': {}}
    allpairs, conf = [], np.zeros((8, 8), int)
    for track, s in stages.items():
        train = [p for t2, s2 in stages.items() if t2 != track for p in s2['pairs']]
        med = learn_scale(train)
        matched = [p for p in s['pairs'] if p[3] is not None]
        offs = [p[3]['apex'] - p[0] for p in matched]
        exact = within1 = 0
        for d, _dir, g, c in matched:
            pg = classify(c['radius'], med)
            conf[g, pg] += 1
            exact += pg == g
            within1 += abs(pg - g) <= 1
        sharp = [c for c in s['corners'] if c['radius'] < 150]
        explained = sum(1 for c in sharp if any(c['dir'] == n[1] and -40 <= c['apex'] - n[0] <= 160
                                                for n in s['corner_notes']))
        rho = spearman([p[2] for p in matched], [p[3]['radius'] for p in matched])
        report['stages'][track] = {
            'stage': s['stage'], 'runs': s['runs'], 'span_m': [round(x) for x in s['span']],
            'corner_notes': len(s['corner_notes']), 'detected_corners': len(s['corners']),
            'recall': round(len(matched) / max(1, len(s['corner_notes'])), 3),
            'recall_grade_1_to_4_and_HP': round(
                sum(1 for p in s['pairs'] if p[2] <= 4 and p[3] is not None)
                / max(1, sum(1 for p in s['pairs'] if p[2] <= 4)), 3),
            'precision_r_lt_150m': round(explained / max(1, len(sharp)), 3),
            'apex_after_note_m_median': round(float(np.median(offs)), 1) if offs else None,
            'grade_exact': round(exact / max(1, len(matched)), 3),
            'grade_within_1': round(within1 / max(1, len(matched)), 3),
            'spearman_grade_vs_radius': round(rho, 3) if rho is not None else None,
            'curvature_pairwise_r_between_runs': s['repeat'],
        }
        allpairs += s['pairs']
    med_all = learn_scale(allpairs)
    report['overall']['median_radius_by_grade_m'] = {GRADES[g]: round(r, 1) for g, r in sorted(med_all.items())}
    n = conf.sum()
    report['overall']['grade_exact'] = round(np.trace(conf) / max(1, n), 3)
    report['overall']['grade_within_1'] = round(sum(conf[i, j] for i in range(8) for j in range(8) if abs(i - j) <= 1)
                                                / max(1, n), 3)
    report['overall']['confusion_rows_note_cols_predicted'] = {GRADES[i]: conf[i].tolist() for i in range(8)
                                                               if conf[i].sum()}
    report['crests'] = crest_check(stages)
    plot(stages)
    with open(os.path.join(common.OUT, 'corners.json'), 'w') as f:
        json.dump(report, f, indent=1)
    print(json.dumps(report, indent=1))


def repeatability(ks):
    rows = [np.nan_to_num(k) for k in ks if np.isfinite(k).mean() > 0.6]
    if len(rows) < 2:
        return None
    full = np.array([r for r in rows])
    m = np.isfinite(ks[0])
    c = np.corrcoef(full)
    return round(float(np.median(c[np.triu_indices(len(rows), 1)])), 3)


def spearman(a, b):
    if len(a) < 4:
        return None
    ra = np.argsort(np.argsort(a))
    rb = np.argsort(np.argsort(b))
    return float(np.corrcoef(ra, rb)[0, 1])


def crest_check(stages, kv_crest=-1 / 250.0):
    """Crests detected from a_z / v^2 (vertical radius under 250 m) against
    the OverCrest / Jump notes: the share of notes with a detected crest
    0..120 m after them, and the share of detected crests with a note
    within 150 m before them; the same for crests placed at random, as the
    baseline."""
    out = {}
    rng = np.random.default_rng(1)
    for track, s in stages.items():
        grid, kv = s['grid'], s['kv']
        lo, hi = s['span']
        cn = [d for d in crest_notes(s['notes']) if lo + 50 <= d <= hi - 100]
        if not cn or np.isfinite(kv).mean() < 0.3:
            continue
        peaks = []
        a = np.nan_to_num(kv)
        for i in range(1, len(a) - 1):
            if a[i] < kv_crest and a[i] <= a[i - 1] and a[i] <= a[i + 1]:
                if not peaks or grid[i] - peaks[-1] > 40:
                    peaks.append(grid[i])
        hit = sum(1 for d in cn if any(0 <= p - d <= 120 for p in peaks))
        prec = sum(1 for p in peaks if any(0 <= p - d <= 150 for d in cn))
        rand = rng.uniform(lo, hi, size=(200, len(peaks)))
        base_hit = np.mean([sum(1 for d in cn if any(0 <= p - d <= 120 for p in r)) / len(cn) for r in rand])
        out[track] = {'crest_notes': len(cn), 'detected': len(peaks), 'recall': round(hit / len(cn), 3),
                      'precision': round(prec / max(1, len(peaks)), 3), 'recall_if_random': round(float(base_hit), 3)}
    return out


def plot(stages):
    track = 'Wales Afon Bidno'
    if track not in stages:
        return
    s = stages[track]
    fig, ax = plt.subplots(2, 1, figsize=(15, 7), sharex=True)
    ax[0].plot(s['grid'], s['k'] * 1000, 'k', lw=0.8, label='median course curvature of %d runs' % s['runs'])
    for d, direction, grade, c in s['pairs']:
        col = 'g' if c is not None else 'r'
        ax[0].annotate(GRADES[grade], (d, direction * 45), color=col, fontsize=7, ha='center')
    for c in s['corners']:
        ax[0].plot(c['apex'], c['dir'] / c['radius'] * 1000, 'o', ms=3, color='C1')
    ax[0].set_ylabel('curvature 1/km (left +)')
    ax[0].set_title('ACR Wales Afon Bidno: corners from the driven path (orange apexes) vs the game\'s pace notes '
                    '(green matched, red missed)', fontsize=9)
    ax[0].set_ylim(-60, 60)
    ax[1].plot(s['grid'], s['kv'] * 1000, 'k', lw=0.8)
    for d in crest_notes(s['notes']):
        ax[1].axvline(d, color='r', lw=0.6)
    ax[1].set_ylabel('a_z / v^2  1/km (crest -)')
    ax[1].set_xlabel('distance along the stage m')
    ax[1].set_title('Vertical curvature from a_z / v^2; red lines: OverCrest / Jump notes', fontsize=9)
    ax[1].set_ylim(-20, 20)
    common.savefig(fig, 'corners_wales.png')


if __name__ == '__main__':
    main()
