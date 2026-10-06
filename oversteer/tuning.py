"""Tuning advice: what the runs on one setup say about it
(docs/telemetry-coaching.md, section 10).

A setup is a tune: one gearing of the car (telemetry_store, tunes). The
advice reads the metrics of the runs driven on the car's current tune,
per stage and surface where those matter, and says what it saw, the
levers as a family (never a number of clicks) and whether it is a matter
of setup or of driving. Gearing is fixed or class-limited for many rally
cars, so every setup lever is "if the setup allows", and where driving
can do the same it is said first.

Not built yet, for want of the data: bottoming and spring/damper balance
(suspension travel is not in the trace, and DiRT's and WRC Generations'
units are unverified), an open differential and brake balance (both need
each wheel's speed in the trace). See the design's section 15.
"""

from .coach import pool, loose

RUNS = 3                         # runs on the tune before any advice
LIMITER_TOP = 1.0                # s per run on the limiter in top gear: the final drive is short (calibrate)
TOP_USED = 0.05                  # share of the time in top gear...
TOP_PEAK = 0.85                  # ...never above this share of the limiter: the final drive is long (calibrate)
EXIT_LOW = 0.5                   # share of a gear's exits that bogged below the power band: the gear is long (calibrate)
EXITS = 10                       # exits in the gear before it is judged
EXIT_LOW_LOOSE = 0.7             # on loose surfaces and in FWD cars, where a low exit is technique more often...
EXITS_LOOSE = 15                 # ...more of them, and more of them judged
EXIT_SPUN = 0.3                  # share of the exits in the gear below that spun: it would have spun, the long gear is right
SPUN_EXITS = 5                   # such exits before that is believed
COUNTER_STEER = 0.3              # share of cornering time steering against the turn on tarmac (calibrate)
BALANCE = 0.03                   # lock per g of the balance fit either side of neutral (calibrate)
NOTES = 2                        # notes shown at a time

ORDINALS = {1: '1st', 2: '2nd', 3: '3rd'}


def ordinal(n):
    return ORDINALS.get(n, '{}th'.format(n))


class Note:
    """One piece of tuning advice. kind: 'setup' or 'driving' (what to
    change first); levers: the setup family ('final drive', 'gear 3',
    'anti-roll bars'...), empty for driving alone."""

    __slots__ = ('id', 'kind', 'text', 'evidence', 'levers', 'weight')

    def __init__(self, id, kind, text, evidence=None, levers=None, weight=0.0):
        self.id, self.kind, self.text = id, kind, text
        self.evidence, self.levers, self.weight = list(evidence or []), list(levers or []), weight

    def to_dict(self):
        return {'id': self.id, 'kind': self.kind, 'text': self.text, 'evidence': list(self.evidence),
                'levers': list(self.levers)}

    def __repr__(self):
        return 'Note({!r}, {!r})'.format(self.id, self.text)


def _on_tune(rows, tune):
    """The rows driven on `tune` (a tunes row): its sessions, and the
    sessions not yet closed (their tune is written when they end) since
    it was first seen."""
    if tune is None:
        return [r for r in rows if r['tune'] is None]
    return [r for r in rows if r['tune'] == tune['id'] or (r['tune'] is None and r['started'] >= tune['first_seen'])]


def _runs(rows, name):
    """{run: value} of one per-run metric."""
    return {r['run']: r['value'] for r in rows if r['name'] == name and r['run'] is not None}


def _stage_name(stage):
    return 'stage ' + stage if stage else 'this stage'


def advice(reader, profile, car_id, surface=None, limit=NOTES):
    """[Note] for the car's current tune, the weightiest first, at most
    `limit`. With `surface` known, the per-surface rules read only runs on
    it; unknown, the car's runs on the tune. A tune with fewer than RUNS
    runs gets one note saying what is still being collected."""
    tunes = reader.tunes(car_id)
    tune = tunes[-1] if tunes else None
    rows = _on_tune(reader.metrics(profile, car_id, 40), tune)
    runs = {r['run'] for r in rows if r['run'] is not None}
    if len(runs) < RUNS:
        return [Note('collecting', 'driving', 'Tuning advice waits for {} runs on this setup ({} so far).'.format(
            RUNS, len(runs)))]
    if surface not in (None, 'unknown'):
        surface_rows = [r for r in rows if r['surface'] == surface]
    else:
        surface_rows = rows
    notes = []
    notes += _final_drive(rows)
    notes += _long_gears(surface_rows, _drivetrain(reader, car_id))
    notes += _balance(rows, surface, reader.metrics(profile, car_id, 40), tunes)
    notes.sort(key=lambda n: -n.weight)
    return notes[:limit]


def _final_drive(rows):
    """Per stage: on the limiter in top gear run after run (short), or top
    gear used but never near the limiter (long). One long straight is
    normal for a Rally2 on tarmac, so it takes RUNS runs of the stage."""
    notes = []
    stages = {}
    for r in rows:
        if r['stage']:
            stages.setdefault(r['stage'], []).append(r)
    for stage, stage_rows in sorted(stages.items()):
        name = _stage_name(stage)
        top = _runs(stage_rows, 'limiter.top')
        over = [v for v in top.values() if v > LIMITER_TOP]
        if len(top) >= RUNS and len(over) >= RUNS:
            mean = sum(over) / len(over)
            notes.append(Note('final-drive.short:' + stage, 'setup', 'On {} you sat on the limiter in top gear for '
                              '{:.1f} s a run ({} of {} runs): lift before the limiter in top, or lengthen the final '
                              'drive or top gear if the setup allows.'.format(name, mean, len(over), len(top)),
                              ['{} runs on this setup.'.format(len(top))], ['final drive', 'top gear'], mean))
        share, peak = _runs(stage_rows, 'top.share'), _runs(stage_rows, 'top.peak')
        long_runs = [run for run in share if share[run] > TOP_USED and run in peak and peak[run] < TOP_PEAK]
        if len(share) >= RUNS and len(long_runs) >= RUNS:
            used = sum(share[r] for r in long_runs) / len(long_runs)
            highest = max(peak[r] for r in long_runs)
            notes.append(Note('final-drive.long:' + stage, 'setup', 'On {} top gear did not get past {:.0f} % of the '
                              'limiter though you used it {:.0f} % of the time: shorten the final drive if the setup '
                              'allows.'.format(name, highest * 100, used * 100),
                              ['{} runs on this setup.'.format(len(share))], ['final drive'], used * 10))
    return notes


def _drivetrain(reader, car_id):
    """'fwd', 'rwd', 'awd' or None: the shipped one where the game's files
    have it, else the learnt one."""
    row = reader.car_by_id(car_id) if car_id is not None else None
    if row is None or not row['model'].get('key'):
        return None
    from .shift_learner import CarModel
    try:
        model = CarModel.from_dict(row['model'])
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return (model.shipped or {}).get('drivetrain') or model.drivetrain


def _long_gears(rows, drivetrain=None):
    """Exits where the revs a second after the throttle went back on were
    below the power band and the car accelerated slowly (a bog: the gear is
    long), gear by gear. On loose surfaces and in FWD cars a low exit is
    technique more often, so the note needs more of them. Never for 1st,
    and silent where the exits in the gear below spun (the wheels could not
    take the shorter gear's drive either), which for 2nd needs the
    1st-gear exits measured."""
    notes = []
    soft = [r for r in rows if r['surface'] not in (None, 'unknown') and not loose(r['surface'])]
    firm = {id(r) for r in soft}
    for hard, group in ((True, [r for r in rows if id(r) not in firm]), (False, soft)):    # an unknown surface counts as loose
        hard = hard or drivetrain == 'fwd'
        limit, need = (EXIT_LOW_LOOSE, EXITS_LOOSE) if hard else (EXIT_LOW, EXITS)
        by_gear, spun = {}, {}
        for r in group:
            if r['gear'] is None:
                continue
            if r['name'] == 'exit.low':
                by_gear.setdefault(r['gear'], []).append(r)
            elif r['name'] == 'exit.spin':
                spun.setdefault(r['gear'], []).append(r)
        for gear, gear_rows in sorted(by_gear.items()):
            share, exits = pool(gear_rows)
            if exits < need or share <= limit or gear <= 1:
                continue
            below, below_exits = pool(spun.get(gear - 1, []))
            if below_exits >= SPUN_EXITS and below > EXIT_SPUN:
                continue                                 # the gear below spun there: the long gear was the right one
            if gear == 2 and below_exits < SPUN_EXITS:
                continue                                 # nothing says 1st would not have spun
            if any(n.id == 'gear.long:{}'.format(gear) for n in notes):
                continue
            notes.append(Note('gear.long:{}'.format(gear), 'driving', 'Out of corners in {} the revs were below the '
                              'power band and the car was slow to pull on {:.0f} % of {} exits: use {} there, or '
                              'shorten {} if the setup allows.'.format(ordinal(gear), share * 100, exits,
                                                                        ordinal(gear - 1), ordinal(gear)),
                              levers=['gear {}'.format(gear)], weight=share * 5))
    return notes


def _balance(rows, surface, all_rows, tunes):
    """Driver-fitted balance, per surface (the steering the car asks for
    depends on the grip, so surfaces are never pooled): a car that measures
    oversteer with a driver catching slides on tarmac gets the setup hint;
    a balanced car with a driver sliding it gets the driving hint (the
    bends only: a hairpin is rotated, which looks like a slide). On loose
    surfaces steering against the slide is technique, not a fault. What
    remains of the balance is its change between setups on the same surface."""
    notes = []
    tarmac = [r for r in rows if r['surface'] == 'tarmac']
    fit = pool([r for r in tarmac if r['name'] == 'balance.gradient'])
    counter = pool([r for r in tarmac if r['name'] == 'counter_steer'])
    k = fit[0] if fit[1] else None
    if counter[1] >= 20 and counter[0] > COUNTER_STEER:
        evidence = ['Steering against the turn in {:.0f} % of {} tarmac bends.'.format(counter[0] * 100, counter[1])]
        if k is not None and k < -BALANCE:
            notes.append(Note('balance.oversteer', 'setup', 'On tarmac the car oversteers: you steer against a slide '
                              'in {:.0f} % of your cornering, and it asks for less lock as the grip used rises. '
                              'Soften the rear anti-roll bar or springs, or stiffen the front, if the setup '
                              'allows.'.format(counter[0] * 100),
                              evidence, ['anti-roll bars', 'springs', 'differential'], counter[0] * 5))
        else:
            notes.append(Note('balance.driver', 'driving', 'On tarmac you catch slides in {:.0f} % of your cornering, '
                              'but the car itself is balanced: turn in more gently and wait for the car before the '
                              'throttle.'.format(counter[0] * 100), evidence, weight=counter[0] * 4))
    # Against the setup before: the same driver on the same car and surface
    if len(tunes) >= 2:
        for where in sorted({r['surface'] for r in rows if r['surface'] and r['name'] == 'balance.gradient'}):
            now = pool([r for r in rows if r['name'] == 'balance.gradient' and r['surface'] == where])
            before = pool([r for r in _on_tune(all_rows, tunes[-2]) if r['name'] == 'balance.gradient'
                           and r['surface'] == where])
            if now[1] >= 200 and before[1] >= 200 and abs(now[0] - before[0]) > BALANCE:
                more = 'understeer' if now[0] > before[0] else 'oversteer'
                notes.append(Note('balance.change:' + where, 'setup', 'Since the last setup change the car asks for '
                                  '{} lock as the grip used rises on {}: more {} than before.'.format(
                                      'more' if more == 'understeer' else 'less', where, more), weight=0.5))
    return notes


# What each game sends of a setup, for the tab and the page: None where it
# sends it (and a tune holds it once measured), the reason otherwise
NOT_SENT = 'not sent by this game'


def tune_summary(reader, car_id, game):
    """The car's current tune for display: ratios, what changed, when, and
    the measured extras, each with a reason where it is missing."""
    tunes = reader.tunes(car_id)
    if not tunes:
        return None
    tune = tunes[-1]
    measured = game in ('acpmf', 'acc', 'ac', 'acr')
    out = {'id': tune['id'], 'ratios': tune['ratios'], 'change': tune['change'], 'first_seen': tune['first_seen'],
           'last_seen': tune['last_seen'], 'tunes': len(tunes)}
    for field in ('tyre_radius', 'ride_height_f', 'ride_height_r', 'brake_bias'):
        value = tune.get(field)
        if value is not None:
            out[field] = value
        elif field == 'tyre_radius' and game in ('forza-fh', 'forza-fm', 'forza'):
            out[field] = 'not measured yet'
        else:
            out[field] = 'not measured yet' if measured else NOT_SENT
    return out
