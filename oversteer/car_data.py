"""The shipped car data (data/telemetry/cars/<game>.json): what a game's
own files say about each car's engine and gearing, so the best change up
is known exactly from the first drive (docs/telemetry-coaching.md,
section 8.1). Derived numbers only: torque curves, ratios, limits.

A file is {"game": ..., "cars": [entry, ...]} plus notes. An entry has
`name` (the car as the game names it in its telemetry, as the bridge or
the game sends it: the car key is '<game>/<name>'), `limiter_rpm`,
`torque_curve` ([[rpm, Nm], ...], linear between points), `gear_sets`
([{id, gears: [1st, 2nd, ...], primary, reverse, default}]),
`final_drive`, `gearbox_efficiency`, `tyre_radius_m` ({surface: [front,
rear]}), `mass_kg`, `drivetrain`, `shift_lights_rpm` (the game's own)
and `sources`.
"""

import json
import logging
import os
import sys

DATADIR = None                   # the installed data folder (share/oversteer), set by the application

_cars = None


def find_dir(datadir=None):
    """The folder of the car files: installed with the telemetry data, or
    in the source tree."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    for base in (datadir, DATADIR):
        if base:
            candidates.append(os.path.join(base, 'telemetry', 'cars'))
    candidates.append(os.path.join(here, '..', 'data', 'telemetry', 'cars'))
    candidates.append(os.path.join(sys.prefix, 'share', 'oversteer', 'telemetry', 'cars'))
    for path in candidates:
        if os.path.isdir(path):
            return os.path.normpath(path)
    return None


def load(directory=None):
    """{car key: entry} from every file in `directory` (found when None).
    A file that does not parse, or an entry without a usable torque curve
    and gearing, is logged and skipped."""
    directory = directory or find_dir()
    cars = {}
    if not directory:
        return cars
    for name in sorted(os.listdir(directory)):
        if not name.endswith('.json'):
            continue
        try:
            with open(os.path.join(directory, name), encoding='utf-8') as f:
                table = json.load(f)
            game = table['game']
            for entry in table['cars']:
                curve = [(float(r), float(t)) for r, t in entry['torque_curve']]
                sets = [s for s in entry.get('gear_sets') or [] if len(s.get('gears') or []) >= 2]
                if len(curve) < 2 or not sets or not entry.get('limiter_rpm'):
                    logging.warning("car data: %s: %s has no usable curve or gearing", name, entry.get('name'))
                    continue
                entry = dict(entry, torque_curve=sorted(curve), gear_sets=sets, game=game)
                key = '{}/{}'.format(game, entry['name'])
                if key in cars:
                    logging.warning("car data: %s: %s twice, the first kept", name, key)
                    continue
                cars[key] = entry
        except (OSError, ValueError, KeyError, TypeError) as e:
            logging.warning("car data: %s not loaded: %s", name, e)
    return cars


def cars():
    """The shipped cars, loaded once."""
    global _cars
    if _cars is None:
        _cars = load()
    return _cars


def set_cars(value):
    """Replace the loaded cars (tests; None loads them again)."""
    global _cars
    _cars = value


def entry(key):
    """The shipped entry of a car key ('acr/Skoda Fabia RS Rally2'), or
    None."""
    return cars().get(key) if key else None


def torque(entry, rpm):
    """The engine's full-load torque (Nm) at `rpm`, linear between the
    curve's points; 0 outside it."""
    curve = entry['torque_curve']
    if rpm < curve[0][0] or rpm > curve[-1][0]:
        return 0.0
    for (r0, t0), (r1, t1) in zip(curve, curve[1:]):
        if r0 <= rpm <= r1:
            return t0 + (t1 - t0) * (rpm - r0) / (r1 - r0) if r1 > r0 else t1
    return 0.0


def default_set(entry):
    sets = entry['gear_sets']
    return next((s for s in sets if s.get('default')), sets[0])


def unexplained_miss(gear_set, learnt, tolerance):
    """How far the learnt ratios ({gear: rpm per m/s}) stray from the gear
    set's where wheelspin does not explain it, or None with no two
    adjacent gears learnt. The final drive and the tyre scale every gear
    alike, so they are scaled out by the highest learnt gear, the one that
    spins least. Steady wheelspin reads as a shorter gear (more rpm per
    m/s) in the low gears only, the more the lower: the lowest gears
    shorter than the set's, up to a run of at least two gears from the top
    that fit within `tolerance`, are taken for spin (a Fabia's 1st and 2nd
    on gravel read 4-6 % short that way). Any other gear off (above a gear
    that is not short, which spins more), and a gear longer than the
    set's, counts."""
    ratios = gear_set['gears']
    gears = sorted(g for g, v in learnt.items() if v and 1 <= g <= len(ratios))
    if not any(g + 1 in gears for g in gears):
        return None
    top = gears[-1]
    scale = learnt[top] / ratios[top - 1]
    off = {g: learnt[g] / (scale * ratios[g - 1]) - 1.0 for g in gears}
    run = [top]
    for g in reversed(gears[:-1]):
        if g != run[-1] - 1 or abs(off[g]) > tolerance:
            break
        run.append(g)
    spin = set()
    if len(run) >= 2:
        for g in gears:
            if g >= run[-1] or off[g] <= 0:
                break
            spin.add(g)
    return max(abs(v) for g, v in off.items() if g not in spin)          # the top gear is never spin


def match_set(entry, learnt):
    """(gear set, worst mismatch) of the car's sets that fits learnt
    ratios best ({gear: rpm per m/s}). Only the steps between gears are
    compared: the final drive and the tyre scale every gear alike. With
    fewer than two learnt gears the default set, mismatch None."""
    gears = sorted(g for g, v in learnt.items() if v)
    pairs = [(g, g + 1) for g in gears if g + 1 in learnt and learnt[g + 1]]
    if not pairs:
        return default_set(entry), None
    best = None
    for s in entry['gear_sets']:
        ratios = s['gears']
        if max(b for _, b in pairs) > len(ratios):
            continue
        misses = [abs((learnt[a] / learnt[b]) / (ratios[a - 1] / ratios[b - 1]) - 1.0) for a, b in pairs]
        worst = max(misses)
        rank = (sum(misses) / len(misses), not s.get('default'))
        if best is None or rank < best[0]:
            best = (rank, s, worst)
    if best is None:
        return default_set(entry), None
    return best[1], best[2]
