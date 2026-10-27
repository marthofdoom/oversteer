"""Richard Burns Rally cars told apart by their gearing, with no install to read and nothing to set up.

NGP's packet names no car (rbr_install reads it from Cars.ini when the install is visible; the Flatpak sees none).
What the packet does give is the engine's rpm and `car.speed`, the speed the wheels read (km/h; it matches the
physics files' gearing within 0.3 % on marth's captures, wheelspin and slides included, where the ground speed reads
4-9 % off). Their quotient in each gear is the gearing, so the steps between the gears (the drop gear and the tyre
scale every gear alike, and a setup's drop gear option changes them all by one factor) are a fingerprint of the
gearbox, read after 40-150 m of driving. With the gear count seen and the highest rpm, that is matched:

1. against what the install named before in this car slot (entries of kind 'install' in the library below);
2. against the shipped table (data/telemetry/cars/rbr.json, car_data): one car whose gear set fits is that car,
   `rbr/<RSF id>`. Several cars share a gearbox (the Fiesta WRC 2019 and the i20 WRC 2021: the same gear list, drop
   gear and limiter), and no signal in the packet tells them apart: none is picked. The run is keyed to the candidate
   set in that car slot, `rbr/11+104@6` (named 'Ford Fiesta WRC 2019 / Hyundai i20 Coupe WRC 2021'), learnt on its
   own, with the shipped data where every candidate's agrees (car_data.entry);
3. against the cars met before (the library): a car the table has no gearing for gets a key of its own, `rbr/g<n>`,
   'RBR car #n (6 gears)', the same in every session, matched by tolerance (STEP_TOLERANCE), not by rounding.

The library, `<data>/oversteer/rbr/cars.json`, is {"version": 1, "cars": [entry, ...], "last": {"key", "slot"}}; an
entry is {"key", "kind" ('local', 'set' or 'install'), "slot" (the packet's car.index, 0..7), "gears" ({gear: rpm per
m/s}, as first measured; later gears are added scaled to them), "top" (highest gear seen), "rpm" (highest rpm seen),
"name", "n" (local: the number), "first" and "last" (epoch seconds), "epochs" (stretches driven)}. Local and set
entries are bound to their slot: two cars with one gearbox in two slots stay two cars. It is written whole (a
temporary file, then a rename) on the drive-log thread, and only by the app: replays read it and never write it, so
a capture replayed gives the key the live session gave.

A car can change only in RSF's menus, which stop the packets: a stage loaded after MENU_GAP s of silence, another
stage, or another car slot begins a new epoch (Sample.car_epoch). The car of the epoch before carries on into it as
provisional (Sample.car_provisional: the lights use it, nothing is learnt under it) until the fingerprint confirms
it or names another car; another slot is another car for certain, so it starts unknown. Until a car is known the key
is rbr_install.UNKNOWN_CAR, shared, under which nothing is learnt (shift_learner.shared_car). The shift learner
re-keys the epoch's session and run when the car becomes known (ShiftLearner.recognise).
"""

import json
import logging
import math
import os
import statistics
import time

from . import car_data, rbr_install

VERSION = 1
FILE = 'cars.json'
GEAR_MIN = 30                    # samples (one in FEED_STEPS steps, 60 a second) before a gear's ratio counts
GEAR_KEEP = 200                  # most recent samples kept per gear (the median of them)
GEARS_MIN = 3                    # gears known before a car is matched (two independent steps)
STEP_TOLERANCE = 0.01            # every gear's ratio, relative to the highest one compared, within this: a match...
STEP_MISS = 0.02                 # ...beyond this: not that car (between them: wait for more samples)
MINT_GEARS = 4                   # gears known before a car no candidate fits gets a key of its own
LIMITER_OVER = 0.03              # rpm seen this far over a car's limiter: not that car
MENU_GAP = 30.0                  # s of silence before a stage load that could hide a change of car (restarts: 14-21 s)
FEED_STEPS = 12                  # NGP steps between the samples taken (720 a second: 60 a second)
STEPS_PER_S = 720.0
STEP_BACK = 720                  # steps the counter goes back by: a stage (re)load (drive_log.RBR_STEP_BACK)
SETTLED = 0.25                   # s in a gear before its samples count (shift_learner.SETTLED)
CLUTCH_OUT = 0.1
MIN_SPEED = 5.0                  # m/s of the wheels
MATCH_EVERY = 1.0                # s of the packets' clock between matches while the car is not known
TORQUE_AGREE = 0.02              # a candidate set shares the shipped data where the torque curves agree this well

LOCAL_PREFIX = 'rbr/g'

_path = None                     # the library file (tests point it elsewhere); None: the user's data folder


def library_path():
    return _path or os.path.join(rbr_install.data_dir(), FILE)


def set_library_path(path):
    """Use another library file (tests); None: the user's."""
    global _path
    _path = path


def fingerprint_key(key):
    """Whether a car key was made here (a local car or a candidate set), not named by the game or the table."""
    return bool(key) and key.startswith('rbr/') and (key.startswith(LOCAL_PREFIX) or '@' in key)


def compare(ref, est):
    """(verdict, worst, gears): `est` ({gear: rpm per m/s}) against `ref` (the same, or a gear set's ratios in any
    scale) over the gears both have, each relative to the highest of them, so the drop gear and the tyre scale out.
    'match' with GEARS_MIN gears or more all within STEP_TOLERANCE, 'miss' with one beyond STEP_MISS, else None."""
    common = sorted(g for g in est if ref.get(g))
    if len(common) < 2:
        return None, None, len(common)
    top = common[-1]
    worst = max(abs((est[g] / est[top]) / (ref[g] / ref[top]) - 1.0) for g in common)
    if worst > STEP_MISS:
        return 'miss', worst, len(common)
    if len(common) >= GEARS_MIN and worst <= STEP_TOLERANCE:
        return 'match', worst, len(common)
    return None, worst, len(common)


class Fingerprint:
    """The gearing measured in one epoch: rpm per m/s of the wheels per gear, the highest gear and rpm seen."""

    def __init__(self):
        self.ratios = {}         # gear -> [rpm per m/s]
        self.top = 0
        self.rpm = 0.0
        self.distance = 0.0      # m driven (the wheels), for the log
        self.seconds = 0.0       # s of packets
        self._gear = None
        self._since = None
        self._t = None
        self.grew = False        # a gear reached GEAR_MIN with the last sample: worth matching at once

    def feed(self, t, sample):
        """One sample at `t` (s of the packets' own clock)."""
        self.grew = False
        speed = sample.wheels_speed
        if self._t is not None and 0.0 < t - self._t < 0.5:
            self.seconds += t - self._t
            if speed:
                self.distance += abs(speed) * (t - self._t)
        self._t = t
        gear = sample.gear
        if gear != self._gear or self._since is None or t < self._since:
            self._gear, self._since = gear, t
        if gear is None or gear < 1:
            return
        if speed is None or speed < MIN_SPEED or (sample.clutch or 0.0) > CLUTCH_OUT or t - self._since < SETTLED:
            return
        self.top = max(self.top, gear)
        self.rpm = max(self.rpm, sample.rpm or 0.0)          # (a revving clutch or a change down's over-rev: not counted)
        values = self.ratios.setdefault(gear, [])
        values.append(sample.rpm / speed)
        del values[:-GEAR_KEEP]
        self.grew = len(values) == GEAR_MIN

    def estimates(self):
        """{gear: rpm per m/s} of the gears with GEAR_MIN samples."""
        return {g: statistics.median(v) for g, v in self.ratios.items() if len(v) >= GEAR_MIN}


def _shared_names(keys):
    return ' / '.join(car_data.display_name(k) or k for k in keys)


def shipped_cars():
    """[(key, name, [gear set ratios {gear: ratio}], limiter)] of the shipped RBR cars (car_data)."""
    out = []
    for key, entry in sorted(car_data.cars().items()):
        if entry.get('game') != 'rbr':
            continue
        sets = [{i + 1: r for i, r in enumerate(s['gears'])} for s in entry['gear_sets']]
        out.append((key, car_data.display_name(key) or entry.get('screen_name') or key, sets,
                    float(entry.get('limiter_rpm') or 0.0)))
    return out


def set_key(keys, slot):
    """The key of the shipped cars `keys`, which one gearbox fits, in car slot `slot`: 'rbr/11+104@6'."""
    ids = sorted((k.split('/', 1)[1] for k in keys), key=lambda i: (len(i), i))
    return 'rbr/{}@{}'.format('+'.join(ids), slot)


def set_name(keys, slot):
    return '{} (car slot {})'.format(_shared_names(sorted(keys, key=lambda k: (len(k), k))), slot)


class Library:
    """The cars met before (see the module's docstring). Read once; changed on the listener thread under the shift
    learner's lock; written by write() on the drive-log thread from a copy (snapshot())."""

    def __init__(self, path=None, writable=False):
        self.path = path
        self.writable = writable
        self.cars = []
        self.last = None
        self._loaded = False

    def load(self):
        if self._loaded:
            return self
        self._loaded = True
        path = self.path or library_path()
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            return self
        except (OSError, ValueError) as e:
            logging.warning("rbr cars: %s not read: %s", path, e)
            return self
        if not isinstance(data, dict) or data.get('version') != VERSION:
            logging.warning("rbr cars: %s is of another version, not used", path)
            return self
        for entry in data.get('cars') or []:
            try:
                entry = dict(entry, gears={int(g): float(v) for g, v in entry['gears'].items()},
                             slot=int(entry['slot']), key=str(entry['key']))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            self.cars.append(entry)
        last = data.get('last')
        self.last = last if isinstance(last, dict) and last.get('key') else None
        return self

    def snapshot(self):
        return {'version': VERSION, 'cars': [dict(e, gears={str(g): round(v, 3) for g, v in sorted(e['gears'].items())})
                                             for e in self.cars], 'last': dict(self.last) if self.last else None,
                'notes': "Richard Burns Rally cars Oversteer told apart by their gearing (oversteer/rbr_fingerprint.py)."}

    def write(self, data, path=None):
        """Write a snapshot() whole: a temporary file, then a rename (drive-log thread). True when written."""
        path = path or self.path or library_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, path)
            return True
        except OSError as e:
            logging.warning("rbr cars: %s not written: %s", path, e)
            return False

    def next_number(self):
        return max([e.get('n') or 0 for e in self.cars] + [0]) + 1

    def entry(self, key, slot=None):
        """The entry of `key` (in car slot `slot`, where given), or None."""
        return next((e for e in self.cars if e['key'] == key and (slot is None or e['slot'] == slot)), None)


class Recogniser:
    """Gives each Richard Burns Rally sample its car (see the module's docstring). Fed every decoded RBR packet on
    the listener thread, under the shift learner's lock. identify() returns what the learner has to do besides: a
    list of ('adopt', old key, new key) (a car known under a key of ours that the table now names) and ('save',
    snapshot) (the library changed)."""

    def __init__(self, library=None):
        self.library = library if library is not None else Library()
        self.epoch = 0
        self.key = None          # the epoch's car, None while unknown
        self.name = None
        self.provisional = False
        self.exact = None        # the key the install (or a capture's header) named
        self.slot = None
        self.stage = None
        self.fp = Fingerprint()
        self._steps = None       # the last packet's
        self._fed = None         # the last sample's taken
        self._arrived = None     # arrival time of the last packet
        self._match_at = None
        self._recorded = None    # (key, epoch): its fingerprint went into the library

    # -- per packet --

    def identify(self, now, sample):
        events = []
        library = self.library.load()
        steps = sample.steps if sample.steps is not None else 0
        exact = sample.car if sample.car and sample.car != rbr_install.UNKNOWN_CAR else None
        slot = sample.car_slot
        loaded = self._steps is not None and steps < self._steps - STEP_BACK
        gap = now - self._arrived if self._arrived is not None else 0.0
        boundary = (self._steps is None or slot != self.slot or exact != self.exact
                    or (sample.stage is not None and self.stage is not None and sample.stage != self.stage)
                    or (loaded and gap >= MENU_GAP))
        if boundary:
            events += self._close_epoch()
            if self._steps is None:
                last = library.last or {}
                carry = last['key'] if last.get('key') and last.get('slot') == slot else None   # the app's last car
            else:
                carry = self.key if slot == self.slot else None          # another slot is another car
            self.epoch += 1
            self.fp = Fingerprint()
            self._fed = self._match_at = None
            self.slot, self.exact = slot, exact
            if exact is not None:
                self.key, self.name, self.provisional = exact, sample.car_name, False
            elif carry is not None:
                self.key, self.name, self.provisional = carry, self._name_of(carry), True
                logging.info("rbr car: epoch %d starts as %s (provisional)", self.epoch, carry)
            else:
                self.key = self.name = None
                self.provisional = False
        if sample.stage is not None:
            self.stage = sample.stage
        self._steps, self._arrived = steps, now
        if self._fed is None or steps < self._fed or steps - self._fed >= FEED_STEPS:
            self._fed = steps
            t = steps / STEPS_PER_S
            self.fp.feed(t, sample)
            if (self.key is None or self.provisional or exact is not None) and (
                    self.fp.grew or self._match_at is None or t < self._match_at or t - self._match_at >= MATCH_EVERY):
                self._match_at = t
                events += self._match(exact)
        if self.key is not None:
            sample.car, sample.car_name = self.key, self.name or sample.car_name
        sample.car_epoch, sample.car_provisional = self.epoch, self.provisional
        return events

    # -- matching --

    def _name_of(self, key):
        entry = self.library.entry(key)
        return (entry or {}).get('name') or car_data.display_name(key) or key

    def _match(self, exact):
        est = self.fp.estimates()
        if len(est) < GEARS_MIN:
            return []
        if exact is not None:
            # The install named it: its gearing is recorded (once an epoch, and again at its end), so this slot's car
            # is known without the install later
            return self._record(exact, 'install', est, name=self.name) if self._recorded != (exact, self.epoch) else []
        found = self.find(est, self.slot, self.fp.top, self.fp.rpm)
        if found is None:
            return []
        key, name, kind, adopt = found
        events = [('adopt', old, key) for old in adopt]
        before, was = self.key, self.provisional
        self.key, self.name, self.provisional = key, name, False
        if before == key and was:
            logging.info("rbr car: %s confirmed by its gearing after %.0f m, %.0f s", key, self.fp.distance,
                         self.fp.seconds)
        elif before != key:
            logging.info("rbr car: %s (%s) from its gearing after %.0f m, %.0f s: %s%s", key, name, self.fp.distance,
                         self.fp.seconds, ', '.join('%d: %.1f' % (g, v) for g, v in sorted(est.items())),
                         ' (was %s)' % before if before else '')
        self.library.last = {'key': key, 'slot': self.slot}
        return events + self._record(key, kind, est, name=name)

    def find(self, est, slot, top, rpm):
        """(key, name, kind, [library keys it adopts]) of the car `est` fits, or None to wait for more."""
        library = self.library
        waiting = False
        for entry in library.cars:
            if entry.get('kind') == 'install' and entry['slot'] == slot:
                verdict = compare(entry['gears'], est)[0]
                if verdict == 'match':
                    return entry['key'], entry.get('name') or self._name_of(entry['key']), 'install', []
        fits = []
        for key, name, sets, limiter in shipped_cars():
            if limiter and rpm > limiter * (1.0 + LIMITER_OVER):
                continue
            verdicts = [compare(s, est)[0] for s in sets if top <= len(s)]
            if 'match' in verdicts:
                fits.append((key, name))
            elif verdicts and all(v == 'miss' for v in verdicts):
                continue
            elif verdicts:
                waiting = True
        ours = []
        for entry in library.cars:
            if entry.get('kind') in ('local', 'set') and entry['slot'] == slot:
                verdict, worst, n = compare(entry['gears'], est)
                if verdict == 'match':
                    ours.append((worst, entry))
                elif verdict is None and len(est) < MINT_GEARS:
                    waiting = True
        ours.sort(key=lambda x: x[0])
        if len(fits) == 1:
            key, name = fits[0]
            return key, name, 'shipped', [e['key'] for _, e in ours if e['key'] != key]
        if ours:
            entry = ours[0][1]
            return entry['key'], entry.get('name') or entry['key'], entry['kind'], []
        if len(fits) > 1:
            keys = [k for k, _ in fits]
            return set_key(keys, slot), set_name(keys, slot), 'set', []
        if waiting or len(est) < MINT_GEARS:
            return None
        n = library.next_number()
        return '{}{}'.format(LOCAL_PREFIX, n), 'RBR car #{} ({} gears)'.format(n, top), 'local', []

    def _record(self, key, kind, est, name=None):
        """Put the epoch's gearing into the library: a new entry, or the gears an entry lacks (scaled to it).
        Cars the table names alone need none: the table finds them again."""
        if kind == 'shipped':
            return []
        library = self.library
        entry = library.entry(key, self.slot)
        now = time.time()
        if entry is None:
            entry = {'key': key, 'kind': kind, 'slot': self.slot, 'gears': dict(est), 'top': self.fp.top,
                     'rpm': round(self.fp.rpm), 'name': name, 'first': round(now), 'last': round(now), 'epochs': 0}
            if key.startswith(LOCAL_PREFIX):
                entry['n'] = int(key[len(LOCAL_PREFIX):])
            library.cars.append(entry)
        else:
            common = [g for g in est if g in entry['gears']]
            scale = statistics.median(entry['gears'][g] / est[g] for g in common) if common else 1.0
            for g, v in est.items():
                entry['gears'].setdefault(g, v * scale)
            entry['top'] = max(entry.get('top') or 0, self.fp.top)
            entry['rpm'] = max(entry.get('rpm') or 0, round(self.fp.rpm))
            entry['last'] = round(now)
            if kind == 'local' and entry.get('n'):
                entry['name'] = self.name = 'RBR car #{} ({} gears)'.format(entry['n'], entry['top'])
        if self._recorded != (key, self.epoch):
            self._recorded = (key, self.epoch)
            entry['epochs'] = (entry.get('epochs') or 0) + 1
        return [('save', library.snapshot())]

    def _close_epoch(self):
        """The epoch ends: what it measured since its car was known goes into the library."""
        if self.key is None or self.provisional or self._steps is None:
            return []
        est = self.fp.estimates()
        if len(est) < GEARS_MIN:
            return []
        entry = self.library.entry(self.key, self.slot)
        kind = entry['kind'] if entry is not None else ('install' if self.exact else 'shipped')
        return self._record(self.key, kind, est, name=self.name)

    def close(self):
        """The session is over (the app quits): the epoch's gearing is kept."""
        return self._close_epoch()


def shared_entry(key):
    """The shipped data of a candidate set key ('rbr/11+104@6') where every candidate's agrees (one limiter, one
    default gearing, torque within TORQUE_AGREE at each point): the first candidate's entry; else None."""
    game, _, rest = key.partition('/')
    ids = rest.split('@', 1)[0].split('+')
    entries = [car_data.cars().get('{}/{}'.format(game, i)) for i in ids]
    if len(entries) < 2 or any(e is None for e in entries):
        return None
    first = entries[0]
    for other in entries[1:]:
        if other.get('limiter_rpm') != first.get('limiter_rpm') \
                or car_data.default_set(other)['gears'] != car_data.default_set(first)['gears'] \
                or other.get('final_drive') != first.get('final_drive'):
            return None
        for a, b in ((first, other), (other, first)):
            for rpm, nm in a['torque_curve']:
                there = car_data.torque(b, rpm)
                if nm and not math.isclose(there, nm, rel_tol=TORQUE_AGREE):
                    return None
    return first

