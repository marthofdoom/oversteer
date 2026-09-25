"""What kind of driving a run was, and on what: discipline, surface and
wet, each a Verdict with the evidence that decided it
(docs/telemetry-coaching.md, sections 8.5 and 8.6).

Evidence or unknown: every answer comes with its confidence, tied to the
kind of evidence that decided ('game' when the game itself says so,
then 'high', 'medium', 'low'), and the sentences shown to the driver.
"Unknown, because this game sends no position" is a complete answer.

The measured surface classifier is here with its calibration, but it
answers only for a game it has been calibrated for from the driver's own
labelled runs (calibrate()); until then the game's own word (the route
table) is the only surface evidence.
"""

import math

CONFIDENCE = ('low', 'medium', 'high', 'game')

DISCIPLINES = ('rally-stage', 'hillclimb', 'circuit', 'rallycross', 'drift', 'free-roam', 'time-attack')
SURFACES = ('tarmac', 'gravel', 'snow', 'ice')
GAMES = {'forza-fh': 'Forza Horizon', 'forza-fm': 'Forza Motorsport', 'forza': 'Forza', 'dirt': 'DiRT Rally',
         'wrcg': 'WRC Generations', 'eawrc': 'EA SPORTS WRC', 'acpmf': 'Assetto Corsa', 'beamng': 'BeamNG.drive',
         'lfs': 'Live for Speed', 'acr': 'Assetto Corsa Rally', 'acc': 'Assetto Corsa Competizione',
         'ac': 'Assetto Corsa'}

MIN_TIME = 90.0                  # s moving a run needs before its shape says anything
MIN_DISTANCE = 2000.0            # m
CELL = 10.0                      # m: positions hashed for loop closures
CLOSURE_AFTER = 500.0            # m of path before coming back counts as a loop
CLOSURE_HEADING = 45.0           # degrees: coming back the same way round
LAP_SPREAD = 0.3                 # laps agree within this (jokers, pit lanes)
HILL_GRADE = 0.04                # net climb over the run
HILL_DESCENT = 0.10              # share of the run going down, at most
STAGE_TIME = (120.0, 900.0)      # s: a stage is one continuous 2-15 minute run
LAUNCH = 0.5                     # s with the revs held before moving: a launch
STANDING = 2.0                   # s standing before moving: a standing start
PRIOR_RUNS = 3                   # runs of a stage classified alike make it a prior
PUDDLES = 0.02                   # share of packets with a wheel in a puddle: wet
LABEL_RUNS = 3                   # labelled runs per class before a surface is calibrated
DEPLOY_HOLDOUT = 0.90            # leave-one-run-out accuracy a calibration needs (calibrate)
AMBIGUOUS = math.log(4.0)        # log-likelihoods closer than this: the pair, not one (calibrate)
VOTES = 5                        # voting segments a run needs
SURFACE_FEATURES = ('mu_p95', 'spin', 'rough', 'lr_corr', 'post_peak', 'rumble')
FEATURE_SHARE = 0.8              # a feature is used where at least this share of segments have it
CHI2_99 = {1: 6.63, 2: 9.21, 3: 11.34, 4: 13.28, 5: 15.09, 6: 16.81}   # reject: unlike every class


class Verdict:
    """value: a class or 'unknown'; confidence: 'game', 'high', 'medium',
    'low', None with unknown; evidence: sentences shown to the driver;
    missing: what would have been needed; prior: what past runs of the
    same place say, shown next to the verdict and never merged into it."""

    __slots__ = ('value', 'confidence', 'evidence', 'missing', 'prior')

    def __init__(self, value='unknown', confidence=None, evidence=None, missing=None, prior=None):
        self.value, self.confidence = value, confidence if value != 'unknown' else None
        self.evidence, self.missing, self.prior = list(evidence or []), list(missing or []), prior

    def __repr__(self):
        return 'Verdict({!r}, {!r}, {!r})'.format(self.value, self.confidence, self.evidence)


def _game(summary):
    return GAMES.get(summary.get('game'), summary.get('game') or 'this game')


# -- discipline --

def _path(trace, x, z, step=CELL):
    """Positions (x, z) about `step` metres apart along the trace, with the
    distance driven to each; None without positions."""
    points = []
    driven = 0.0
    last = None
    for row in trace:
        px, pz = row[x], row[z]
        if px != px or pz != pz:                     # NaN: no position
            return None
        if last is None:
            points.append((px, pz, 0.0))
            last = (px, pz)
            continue
        hop = math.hypot(px - last[0], pz - last[1])
        if hop >= step:
            driven += hop
            points.append((px, pz, driven))
            last = (px, pz)
    return points


def loops(points):
    """(times the path came back to where it started, the same way round,
    after CLOSURE_AFTER metres, [lap lengths])."""
    if len(points) < 3:
        return 0, []
    x0, z0, _ = points[0]
    heading0 = math.atan2(points[1][1] - z0, points[1][0] - x0)
    back, laps, since = 0, [], 0.0
    for i in range(1, len(points) - 1):
        x, z, d = points[i]
        if d - since < CLOSURE_AFTER or math.hypot(x - x0, z - z0) > CELL:
            continue
        heading = math.atan2(points[i + 1][1] - z, points[i + 1][0] - x)
        turn = abs((math.degrees(heading - heading0) + 180.0) % 360.0 - 180.0)
        if turn <= CLOSURE_HEADING:
            back += 1
            laps.append(d - since)
            since = d
    return back, laps


def climb(trace, y, distance):
    """(net grade, share of the path going down) from the trace's heights
    every 50 m, or None without heights."""
    heights = []
    last_d = None
    for row in trace:
        h, d = row[y], row[distance]
        if h != h or d != d:
            return None
        if last_d is None or d - last_d >= 50.0:
            heights.append((d, h))
            last_d = d
    if len(heights) < 3 or heights[-1][0] - heights[0][0] <= 0:
        return None
    length = heights[-1][0] - heights[0][0]
    down = sum(b[0] - a[0] for a, b in zip(heights, heights[1:]) if b[1] < a[1] - 0.5)
    return (heights[-1][1] - heights[0][1]) / length, down / length


PROFILE_WORDS = (('rallycross', 'rallycross'), ('hillclimb', 'hillclimb'), ('hill', 'hillclimb'),
                 ('rally', 'rally-stage'), ('circuit', 'circuit'), ('track', 'circuit'), ('drift', 'drift'))


def classify_discipline(summary, trace, channels):
    """A Verdict on the run's discipline. `summary` is the run tracker's
    (game, laps, stage_length, packets, standing, launch, stops,
    moving_time, distance, profile...); `trace` its 10 Hz rows with
    `channels` naming the columns."""
    c = {name: i for i, name in enumerate(channels)}
    game = summary.get('game')
    evidence, missing = [], []
    profile = summary.get('profile')
    profile_line = "Oversteer profile '{}'.".format(profile) if profile and profile != '_no_profile' else None

    # 1. The game's own word
    laps, length = summary.get('laps') or 0, summary.get('stage_length')
    verdict = None
    if game == 'eawrc':
        verdict = Verdict('rally-stage', 'game', ['EA SPORTS WRC sends telemetry from its stages only.'])
    elif game in ('dirt', 'wrcg') and laps > 1:
        kind = 'rallycross' if length and length < 1500.0 else 'circuit'
        verdict = Verdict(kind, 'game', ['{} sends {} laps{}.'.format(
            _game(summary), laps, ' of {:.1f} km'.format(length / 1000.0) if length else '')])
    elif game in ('dirt', 'wrcg') and length and length >= 1000.0:
        verdict = Verdict('rally-stage', 'game', ['{} sends one stage of {:.1f} km.'.format(
            _game(summary), length / 1000.0)])
    elif game == 'forza-fm' and summary.get('stage'):
        verdict = Verdict('circuit', 'game', ['Forza Motorsport names the track ({}).'.format(summary['stage'])])
    if verdict is not None:
        if profile_line:
            verdict.evidence.append(profile_line)
        return verdict

    # 2, 3, 5: the run's shape, once there is enough of it
    moving, distance = summary.get('moving_time') or 0.0, summary.get('distance') or 0.0
    enough = moving >= MIN_TIME and distance >= MIN_DISTANCE
    points = _path(trace, c['x'], c['z']) if trace else None
    answers = []                                     # (class, confidence, sentence)
    if not enough:
        missing.append('a longer run: {:.0f} s and {:.1f} km moving, {:.0f} s and {:.0f} km needed'.format(
            moving, distance / 1000.0, MIN_TIME, MIN_DISTANCE / 1000.0))
    elif points is None:
        missing.append('the car\'s position, which {} does not send'.format(_game(summary)))
    else:
        back, lap_lengths = loops(points)
        standing = summary.get('standing') or 0.0
        launched = (summary.get('launch') or 0.0) >= LAUNCH
        stops = summary.get('stops') or 0
        if back:
            steady = len(lap_lengths) < 2 or max(lap_lengths) <= (1 + LAP_SPREAD) * min(lap_lengths)
            confidence = 'high' if back >= 2 and steady else 'medium'
            sentence = 'Came back round to the start {} time{} ({}).'.format(
                back, '' if back == 1 else 's', ', '.join('{:.1f} km'.format(v / 1000.0) for v in lap_lengths))
            if standing < 0.5:
                answers.append(('time-attack', confidence, sentence + ' From a rolling start.'))
            else:
                answers.append(('circuit', confidence, sentence))
        else:
            evidence.append('Never came back to the start over {:.1f} km: point to point.'.format(distance / 1000.0))
            if (launched or standing >= STANDING) and stops == 0 and STAGE_TIME[0] <= moving <= STAGE_TIME[1]:
                answers.append(('rally-stage', 'medium', '{} then {:.0f} min without stopping.'.format(
                    'A launch' if launched else 'A standing start', moving / 60.0)))
            elif stops >= 2 and not launched:
                answers.append(('free-roam', 'low', 'Stopped {} times on the way, no launch.'.format(stops)))
        grade = climb(trace, c['y'], c['distance'])
        if grade is not None and grade[0] >= HILL_GRADE and grade[1] < HILL_DESCENT:
            answers.append(('hillclimb', 'medium', 'Climbed {:.0f} % on average, going down {:.0f} % of the '
                            'way.'.format(grade[0] * 100, grade[1] * 100)))
    classes = {a[0] for a in answers}
    if 'hillclimb' in classes and classes <= {'hillclimb', 'rally-stage'}:
        classes = {'hillclimb'}                      # a hillclimb is a stage uphill
    if len(classes) == 1:
        value = classes.pop()
        best = max((a for a in answers if a[0] == value), key=lambda a: CONFIDENCE.index(a[1]))
        verdict = Verdict(value, best[1], evidence + [a[2] for a in answers])
    elif len(classes) > 1:
        verdict = Verdict('unknown', None, evidence + [a[2] for a in answers] + ['These disagree.'], missing)
    else:
        verdict = Verdict('unknown', None, evidence, missing)
    if profile_line:
        verdict.evidence.append(profile_line)
        if verdict.value == 'unknown':
            name = profile.lower()
            word = next((cls for w, cls in PROFILE_WORDS if w in name), None)
            if word is not None:
                verdict.value, verdict.confidence = word, 'low'
    return verdict


# -- surface and wet --

# The game's own word on surfaces: locations that are one surface
# throughout, by the location's name as the game gives it. A route on a
# mixed location (Monte-Carlo, Central Europe) is a prior only. EA SPORTS
# WRC sends location ids, not names: the id -> name table waits for the
# game's readme/ids.json (see the design's §5.3), so these do not fire
# for it yet, and the names are to be checked against that file.
LOCATION_SURFACES = {
    'eawrc': {
        'Rally Sweden': 'snow', 'Croatia Rally': 'tarmac', 'Forum8 Rally Japan': 'tarmac',
        'Rally Iberia': 'tarmac', 'Rally Mediterraneo': 'tarmac', 'Rally Mexico': 'gravel',
        'Rally de Portugal': 'gravel', 'Rally Italia Sardegna': 'gravel', 'Safari Rally Kenya': 'gravel',
        'Rally Estonia': 'gravel', 'Secto Rally Finland': 'gravel', 'Acropolis Rally Greece': 'gravel',
        'Rally Chile': 'gravel', 'Fanatec Rally Oceania': 'gravel', 'Agon By AOC Rally Pacifico': 'gravel',
        'Rally Scandia': 'gravel',
        'Rally Monte-Carlo': 'mixed:tarmac,snow',
    },
}
EAWRC_LOCATIONS = {}             # location id -> name (from ids.json, not shipped yet)


def location_name(game, stage, known=None):
    """The location's name for a stage key, where known: from the stage's
    row, or for EA SPORTS WRC from its location id."""
    if known:
        return known
    if game == 'eawrc' and stage and stage.startswith('eawrc:'):
        try:
            return EAWRC_LOCATIONS.get(int(stage.split(':')[1]))
        except ValueError:
            return None
    return None


def route_surface(game, location):
    """(surface, prior): what the route table says for a location. A
    single-surface location is the game's word (surface); a mixed one
    only a prior."""
    table = LOCATION_SURFACES.get(game, {})
    surface = table.get(location) if location else None
    if surface is None:
        return None, None
    if surface.startswith('mixed:'):
        return None, surface
    return surface, None


def _standardise(values, model):
    return [(v - m) / s for v, m, s in zip(values, model['mean'], model['std'])]


def _vector(features, names):
    values = [features.get(n) for n in names]
    return None if any(v is None for v in values) else values


def classify_segment(model, features):
    """(surface, margin) of one segment by a calibration model; 'unknown'
    when it is unlike every calibrated class (the reject rule), the pair's
    name ('loose-low' for snow and wet gravel) when two are too close.
    None when the segment lacks a feature the model needs."""
    values = _vector(features, model['features'])
    if values is None:
        return None
    x = _standardise(values, model)
    scores = []
    for name, cls in model['classes'].items():
        d2 = sum((a - m) ** 2 / v for a, m, v in zip(x, cls['mean'], cls['var']))
        loglik = -0.5 * (d2 + sum(math.log(2 * math.pi * v) for v in cls['var']))
        scores.append((loglik, d2, name))
    scores.sort(reverse=True)
    best = scores[0]
    if best[1] > CHI2_99.get(len(x), 3 * len(x)):
        return 'unknown', 0.0
    if len(scores) > 1:
        margin = best[0] - scores[1][0]
        if margin < AMBIGUOUS:
            pair = {best[2], scores[1][2]}
            return ('loose-low' if pair == {'snow', 'gravel'} else 'unknown'), margin
        return best[2], margin
    return best[2], float('inf')


def fit(labelled, names):
    """A diagonal Gaussian per class on standardised features, from
    [(run, surface, [feature dicts])]."""
    rows = [(surface, _vector(f, names)) for _, surface, segments in labelled for f in segments]
    rows = [(s, v) for s, v in rows if v is not None]
    if not rows:
        return None
    n = len(rows)
    mean = [sum(v[i] for _, v in rows) / n for i in range(len(names))]
    std = [max(1e-6, math.sqrt(sum((v[i] - mean[i]) ** 2 for _, v in rows) / n)) for i in range(len(names))]
    model = {'features': list(names), 'mean': mean, 'std': std, 'classes': {}}
    for surface in sorted({s for s, _ in rows}):
        xs = [_standardise(v, model) for s, v in rows if s == surface]
        m = [sum(x[i] for x in xs) / len(xs) for i in range(len(names))]
        var = [max(0.05, sum((x[i] - m[i]) ** 2 for x in xs) / len(xs)) for i in range(len(names))]
        model['classes'][surface] = {'mean': m, 'var': var, 'segments': len(xs)}
    return model


def calibrate(labelled):
    """A surface calibration from labelled runs: [(run id, surface,
    [features of its voting segments])]. Returns (model or None, holdout
    accuracy or None, deployed, reason). Deployed only with LABEL_RUNS
    runs of each of two classes at least and a leave-one-run-out
    segment accuracy of DEPLOY_HOLDOUT."""
    labelled = [(run, surface, segments) for run, surface, segments in labelled
                if surface in SURFACES and segments]
    runs = {}
    for _, surface, _ in labelled:
        runs[surface] = runs.get(surface, 0) + 1
    ready = [s for s, n in runs.items() if n >= LABEL_RUNS]
    if len(ready) < 2:
        return None, None, False, 'label at least {} stages of two surfaces each ({})'.format(
            LABEL_RUNS, ', '.join('{} {}'.format(n, s) for s, n in sorted(runs.items())) or 'none yet')
    labelled = [item for item in labelled if item[1] in ready]
    segments = [f for _, _, fs in labelled for f in fs]
    names = [n for n in SURFACE_FEATURES
             if sum(1 for f in segments if f.get(n) is not None) >= FEATURE_SHARE * len(segments)]
    if not names:
        return None, None, False, 'this game sends too little to tell surfaces apart'
    right = total = 0
    for i, (_, surface, fs) in enumerate(labelled):
        model = fit(labelled[:i] + labelled[i + 1:], names)
        if model is None:
            continue
        for f in fs:
            answer = classify_segment(model, f)
            if answer is None:
                continue
            total += 1
            right += answer[0] == surface
    holdout = right / total if total else 0.0
    model = fit(labelled, names)
    deployed = holdout >= DEPLOY_HOLDOUT
    return model, holdout, deployed, None if deployed else 'held-out accuracy {:.0f} %, {:.0f} % needed'.format(
        holdout * 100, DEPLOY_HOLDOUT * 100)


def classify_surface(summary, segments, calibration=None, location=None, prior=None):
    """A Verdict on the run's surface. `segments` are the run's stored
    segments (with features and pushed); `calibration` the game's
    (telemetry_store.Reader.calibration()); `location` its name where
    known; `prior` the stage's learnt or user prior, shown apart."""
    game = summary.get('game')
    surface, table_prior = route_surface(game, location)
    shown_prior = prior or table_prior
    if surface is not None:
        return Verdict(surface, 'game', ['{}: {} is a {} rally.'.format(_game(summary), location, surface)],
                       prior=shown_prior)
    evidence = []
    if location:
        evidence.append('{} has more than one surface.'.format(location))
    if calibration is None or not calibration.get('deployed'):
        reason = 'a calibration for {}: label a few stages of each surface'.format(_game(summary))
        if calibration is not None and calibration.get('holdout') is not None:
            reason += ' (held-out accuracy {:.0f} % so far)'.format(calibration['holdout'] * 100)
        return Verdict('unknown', None, evidence, [reason], shown_prior)
    voting = [s for s in segments if s.get('pushed')]
    if len(voting) < VOTES:
        evidence.append('Near the grip limit on {} stretch{} of 200 m, {} needed: never pushed enough to '
                        'tell.'.format(len(voting), '' if len(voting) == 1 else 'es', VOTES))
        return Verdict('unknown', None, evidence, ['more driving near the limit'], shown_prior)
    votes = {}
    margins = []
    for s in voting:
        answer = s.get('surface')
        if answer is None:
            found = classify_segment(calibration['model'], s['features'])
            if found is None:
                continue
            answer, margin = found
        else:
            margin = s.get('margin') or 0.0
        votes[answer] = votes.get(answer, 0) + 1
        margins.append(margin)
    counted = sum(votes.values())
    if not counted:
        return Verdict('unknown', None, evidence, ['the channels the calibration uses'], shown_prior)
    ranked = sorted(votes.items(), key=lambda kv: -kv[1])
    best, count = ranked[0]
    share = count / counted
    if best == 'unknown':
        evidence.append('{} of {} stretches were unlike any surface calibrated.'.format(count, counted))
        return Verdict('unknown', None, evidence, ['a calibration with this surface in it'], shown_prior)
    value = best
    if len(ranked) > 1 and ranked[1][0] not in ('unknown', best) and ranked[1][1] >= 0.25 * counted:
        value = 'mixed:{},{}'.format(*sorted((best, ranked[1][0])))
    median_margin = sorted(margins)[len(margins) // 2]
    if share >= 0.8 and median_margin >= 2 * AMBIGUOUS and counted >= 10:
        confidence = 'high'
    elif share >= 0.6:
        confidence = 'medium'
    else:
        confidence = 'low'
    evidence.append('Measured: {} of {} stretches near the limit say {}.'.format(count, counted, best))
    return Verdict(value, confidence, evidence, prior=shown_prior)


def classify_wet(summary):
    """A Verdict on whether it was wet: only where the game says so (Forza's
    puddles); a dry-looking run in a game that sends no weather is
    unknown, not dry."""
    puddles = summary.get('puddles')
    if puddles is None:
        return Verdict('unknown', None, [], ['word of rain, which {} does not send'.format(_game(summary))])
    if puddles >= PUDDLES:
        return Verdict('wet', 'game', ['A wheel was in a puddle {:.0f} % of the time.'.format(puddles * 100)])
    return Verdict('unknown', None, ['No puddles hit; {} says nothing of a damp road.'.format(_game(summary))],
                   ['word of rain'])


# -- at the end of a run (drive-log thread) --

def stage_prior(history, column):
    """The class the stage's last PRIOR_RUNS runs agree on, or None:
    `history` rows (discipline, discipline_conf, surface, surface_conf),
    newest first."""
    values = [row[column] for row in history[:PRIOR_RUNS]]
    if len(values) == PRIOR_RUNS and values[0] not in (None, 'unknown') and len(set(values)) == 1:
        return values[0]
    return None


def detect_run(store, run, summary, trace, channels):
    """The verdicts on a run just ended, as runs columns; stage priors are
    updated from them. Segments are classified where the game has a
    deployed calibration."""
    game = summary.get('game')
    stage_key = summary.get('stage')
    stage = store.stage(stage_key) if stage_key else None
    discipline = classify_discipline(summary, trace, channels)
    calibration = store.calibration(game, 'surface') if game else None
    segments = store.segments(run)
    if calibration is not None and calibration['deployed']:
        for segment in segments:
            if segment['pushed']:
                found = classify_segment(calibration['model'], segment['features'])
                if found is not None:
                    segment['surface'], segment['margin'] = found
                    store.set_segment_surface(run, segment['d0'], *found)
    location = location_name(game, stage_key, stage and stage.get('location'))
    prior = stage and stage.get('surface_prior')
    surface = classify_surface(summary, segments, calibration, location, prior)
    wet = classify_wet(summary)
    if stage is not None and discipline.prior is None and stage.get('discipline_prior'):
        discipline.prior = stage['discipline_prior']
    fields = {
        'discipline': discipline.value, 'discipline_conf': discipline.confidence,
        'discipline_evidence': discipline.evidence + ['Needs ' + m + '.' for m in discipline.missing],
        'surface': surface.value, 'surface_conf': surface.confidence,
        'surface_evidence': surface.evidence + ['Needs ' + m + '.' for m in surface.missing],
        'wet': wet.value, 'wet_evidence': wet.evidence + ['Needs ' + m + '.' for m in wet.missing],
        'detector_version': calibration['version'] if calibration and calibration['deployed'] else 0,
    }
    if stage_key:
        history = [(discipline.value, discipline.confidence, surface.value, surface.confidence)]
        history += list(store.stage_history(stage_key, PRIOR_RUNS, exclude=run))
        learnt = stage_prior(history, 0)
        if learnt is not None and stage and stage.get('discipline_prior_source') in (None, 'learnt'):
            store.set_stage_prior(stage_key, 'discipline', learnt, 'learnt')
        learnt = stage_prior(history, 2)
        if learnt is not None and stage and stage.get('surface_prior_source') in (None, 'learnt'):
            store.set_stage_prior(stage_key, 'surface', learnt, 'learnt')
    return fields


def calibrate_game(store, game):
    """Fit the game's surface calibration from its labelled runs and store
    it (a new version each time). Returns (version or None, holdout,
    deployed, reason)."""
    labelled = []
    for run, label in store.labels_for(game):
        if label.get('surface'):
            features = [s['features'] for s in store.segments(run) if s['pushed']]
            labelled.append((run, label['surface'], features))
    model, holdout, deployed, reason = calibrate(labelled)
    if model is None:
        return None, holdout, False, reason
    segments = sum(len(fs) for _, _, fs in labelled)
    version = store.save_calibration(game, 'surface', model, len(labelled), segments, holdout, deployed)
    return version, holdout, deployed, reason
