"""Richard Burns Rally cars told apart by their gearing, with no install to read and nothing to set up.

NGP's packet names no car (rbr_install reads it from Cars.ini when the install is visible; the Flatpak sees none).
What the packet does give is the engine's rpm and `car.speed`, the speed the wheels read (km/h; it matches the
physics files' gearing within 0.3 % on marth's captures, wheelspin and slides included, where the ground speed reads
4-9 % off). Their quotient in each gear is the gearing, so the steps between the gears (the drop gear and the tyre
scale every gear alike, and a setup's drop gear option changes them all by one factor) are a fingerprint of the
gearbox, read after 40-150 m of driving. With the gear count seen and the highest rpm, that is matched:

1. against what the install named before in this car slot (entries of kind 'install' in the library below);
2. against the shipped table (data/telemetry/cars/rbr.json, car_data): one car whose gear set fits is that car,
   `rbr/<RSF id>`, only where no RSF car the table has no gearing for could share its gearbox (impostors(): the same
   gear count and drivetrain in the table's `names`). Several cars share a gearbox (the Fiesta WRC 2019 and the i20 WRC
   2021: the same gear list, drop gear and limiter), and no signal in the packet tells them apart: none is picked. The
   run is keyed to the candidate set in that car slot, `rbr/11+104@6` (named 'Ford Fiesta WRC 2019 / Hyundai i20
   Coupe WRC 2021 (car slot 6)'), or to the one fit that others could share, `rbr/106@5` ('Skoda Fabia RS Rally2 (by
   gearing, car slot 5)'), learnt on its own, with the shipped data where every candidate's agrees (car_data.entry);
3. against the cars met before (the library): a car the table has no gearing for gets a key of its own, `rbr/g<n>`,
   'RBR car #n (6 gears)', the same in every session, matched by tolerance (STEP_TOLERANCE), not by rounding; its
   number is past every one the library and the store have (a lost library never hands one out again).

A set or a car of ours that the install, or a lone fit no other car could share, later names is adopted: its
sessions, model and PBs move to that key (Store.merge_car), once. A gear set that neither fits nor misses counts as
a miss DECIDED_M into an epoch, so the wait ends. A stage loaded again within the epoch (a restart, or a trip to
the menus quicker than MENU_GAP) makes the known car provisional until the gearing since the load fits it again; a
miss is another car: a new epoch, and the runs since the load go to the shared key (ShiftLearner._write_split).

The library, `<data>/oversteer/rbr/cars.json`, is {"version": 1, "cars": [entry, ...], "last": {"key", "slot"}}; an
entry is {"key", "kind" ('local', 'set' or 'install'), "slot" (the packet's car.index, 0..7), "gears" ({gear: rpm per
m/s}, as first measured; later gears are added scaled to them), "top" (highest gear seen), "rpm" (highest rpm seen),
"name", "n" (local: the number), "first" and "last" (epoch seconds), "epochs" (stretches driven)}. Local and set
entries are bound to their slot: two cars with one gearbox in two slots stay two cars; "adopted" names the key an
entry became. It is written whole (a temporary file of its own, synced, then renamed over it) on the drive-log thread,
and only by the app: replays read it and never write it (nor adopt), so a capture replayed gives the key the live
session gave. A file that cannot be used is moved aside (`.bad`), never overwritten.

A car can change only in RSF's menus, which stop the packets: a stage loaded after MENU_GAP s of silence, another
stage, or another car slot begins a new epoch (Sample.car_epoch). The car of the epoch before carries on into it as
provisional (Sample.car_provisional: the lights use it, nothing is learnt under it) until the fingerprint confirms
it or names another car; another slot is another car for certain, so it starts unknown. Until a car is known the key
is rbr_install.UNKNOWN_CAR, shared, under which nothing is learnt (shift_learner.shared_car). The shift learner
re-keys every session of the epoch, and its run, when the car becomes known (ShiftLearner.recognise), and an epoch
that ends still provisional goes back to the shared key.
"""

import json
import logging
import math
import os
import statistics
import tempfile
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
MATCH_EVERY = 1.0                # s of the packets' clock between matches (and checks, once the car is known)
DECIDED_M = 1000.0               # m into an epoch (or GEAR_KEEP samples in every gear) after which a gear set that
                                 # neither fits nor misses counts as a miss: the wait ends
TORQUE_AGREE = 0.02              # a candidate set shares the shipped data where the torque curves agree this well

LOCAL_PREFIX = 'rbr/g'

_path = None                     # the library file (tests point it elsewhere); None: the user's data folder
_names = None                    # {car key: {'gears', 'drivetrain'}} of every RSF car the table names (set_names)


def library_path():
    return _path or os.path.join(rbr_install.data_dir(), FILE)


def set_library_path(path):
    """Use another library file (tests); None: the user's."""
    global _path
    _path = path


def set_names(names):
    """Use these {car key: {'gears', 'drivetrain'}} as the RSF cars that exist (tests); None: the table's `names`."""
    global _names
    _names = names


def rsf_cars():
    """{car key: {'gears', 'drivetrain'}} of every RSF car the shipped table names (its `names`, read once)."""
    global _names
    if _names is None:
        names = {}
        directory = car_data.find_dir()
        try:
            with open(os.path.join(directory, 'rbr.json'), encoding='utf-8') as f:
                for key, rows in (json.load(f).get('names') or {}).items():
                    row = rows[0] if rows else {}
                    names[key] = {'gears': row.get('gears'), 'drivetrain': row.get('drivetrain')}
        except (OSError, TypeError, ValueError, AttributeError) as e:
            logging.warning("rbr cars: the table's names not read: %s", e)
        _names = names
    return _names


def impostors(key):
    """The RSF cars the table has no gearing for that could have the gearbox of the shipped car `key`: the same
    forward gear count and drivetrain (or not known). One gearbox can serve many cars (the Fiesta's and the i20's are
    one), so a lone fit names its car only where no such car exists."""
    names = rsf_cars()
    if not names:
        return ['(the table names no cars: any could)']    # nothing tells the fit apart from a car not in the table
    shipped = car_data.cars()
    entry = shipped.get(key) or {}
    gears = len(car_data.default_set(entry)['gears']) if entry.get('gear_sets') else None
    drive = entry.get('drivetrain')
    return sorted(k for k, v in names.items() if k != key and k not in shipped
                  and (v.get('gears') is None or gears is None or v['gears'] == gears)
                  and (v.get('drivetrain') is None or drive is None or v['drivetrain'] == drive))


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
    """The key of the shipped cars `keys`, which one gearbox fits, in car slot `slot`: 'rbr/11+104@6'; 'rbr/106@5'
    for one the table has the gearing of where other cars could share it (impostors)."""
    ids = sorted((k.split('/', 1)[1] for k in keys), key=lambda i: (len(i), i))
    return 'rbr/{}@{}'.format('+'.join(ids), slot)


def set_name(keys, slot):
    if len(keys) == 1:
        return '{} (by gearing, car slot {})'.format(_shared_names(keys), slot)
    return '{} (car slot {})'.format(_shared_names(sorted(keys, key=lambda k: (len(k), k))), slot)


class Library:
    """The cars met before (see the module's docstring). Read once; changed on the listener thread under the shift
    learner's lock; written by write() on the drive-log thread from a copy (snapshot())."""

    def __init__(self, path=None, writable=False):
        self.path = path
        self.writable = writable
        self.cars = []
        self.last = None
        self.broken = False      # the file was there and could not be used: moved aside before the first write
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
            self.broken = True
            return self
        if not isinstance(data, dict) or data.get('version') != VERSION:
            logging.warning("rbr cars: %s is of another version, not used", path)
            self.broken = True
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
        """Write a snapshot() whole (drive-log thread): a temporary file of its own in the same folder, synced, then
        renamed over the file. A file that could not be used when read is moved aside first (`.bad`), never
        overwritten. True when written."""
        path = path or self.path or library_path()
        tmp = None
        try:
            folder = os.path.dirname(path)
            os.makedirs(folder, exist_ok=True)
            if self.broken and os.path.exists(path):
                aside = '{}.{}.bad'.format(path, time.strftime('%Y%m%d-%H%M%S'))
                os.replace(path, aside)
                logging.warning("rbr cars: %s could not be used; moved aside to %s", path, aside)
            self.broken = False
            fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + '.', suffix='.tmp', dir=folder)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return True
        except OSError as e:
            logging.warning("rbr cars: %s not written: %s", path, e)
            if tmp is not None:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return False

    def next_number(self, taken=()):
        """The number of a new local car: past every one the library has, and every `taken` key (the store's
        rbr/g<n> cars, so a library lost or broken never hands a number out again)."""
        numbers = [e.get('n') or 0 for e in self.cars]
        for key in taken:
            if key.startswith(LOCAL_PREFIX) and key[len(LOCAL_PREFIX):].isdigit():
                numbers.append(int(key[len(LOCAL_PREFIX):]))
        return max(numbers + [0]) + 1

    def entry(self, key, slot=None):
        """The entry of `key` (in car slot `slot`, where given), or None."""
        return next((e for e in self.cars if e['key'] == key and (slot is None or e['slot'] == slot)), None)


class Recogniser:
    """Gives each Richard Burns Rally sample its car (see the module's docstring). Fed every decoded RBR packet on
    the listener thread, under the shift learner's lock. identify() returns what the learner has to do besides: a
    list of ('adopt', old key, new key) (a car known under a key of ours that the install or the table alone now
    names) and ('save', snapshot) (the library changed). `taken`: a callable giving the store's rbr/g<n> keys."""

    def __init__(self, library=None, taken=None):
        self.library = library if library is not None else Library()
        self.taken = taken
        self.epoch = 0
        self.key = None          # the epoch's car, None while unknown
        self.name = None
        self.provisional = False
        self.exact = None        # the key the install (or a capture's header) named
        self.slot = None
        self.stage = None
        self.fp = Fingerprint()  # the epoch's gearing
        self.check = Fingerprint()   # since the stage was last loaded: what the known car is checked against
        self._steps = None       # the last packet's
        self._fed = None         # the last sample's taken
        self._arrived = None     # arrival time of the last packet
        self._match_at = None
        self._recorded = None    # (key, epoch): its fingerprint went into the library
        self._force = False      # the known car's gearing missed: the next packet begins an epoch, car unknown
        self.rechecking = False  # the stage was loaded again within the epoch: the car is provisional until `check` fits

    # -- per packet --

    def identify(self, now, sample):
        events = []
        library = self.library.load()
        steps = sample.steps if sample.steps is not None else 0
        exact = sample.car if sample.car and sample.car != rbr_install.UNKNOWN_CAR else None
        slot = sample.car_slot
        loaded = self._steps is not None and steps < self._steps - STEP_BACK
        gap = now - self._arrived if self._arrived is not None else 0.0
        forced, self._force = self._force, False
        boundary = (forced or self._steps is None or slot != self.slot or exact != self.exact
                    or (sample.stage is not None and self.stage is not None and sample.stage != self.stage)
                    or (loaded and gap >= MENU_GAP))
        if boundary:
            if forced:
                carry = None                             # (and what the epoch measured is two cars': not kept)
            else:
                events += self._close_epoch()
                if self._steps is None:
                    last = library.last or {}
                    carry = last['key'] if last.get('key') and last.get('slot') == slot else None   # the app's last car
                else:
                    carry = self.key if slot == self.slot else None          # another slot is another car
            self.epoch += 1
            self.rechecking = False
            self.fp, self.check = Fingerprint(), Fingerprint()
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
        elif loaded:
            # A restart, or a quicker trip to the menus than MENU_GAP: the car is checked again from here, and until
            # its gearing fits nothing is learnt under it (the lights go on using it)
            self.check = Fingerprint()
            if self.key is not None and self.exact is None and not self.provisional:
                self.provisional = self.rechecking = True
        if sample.stage is not None:
            self.stage = sample.stage
        self._steps, self._arrived = steps, now
        if self._fed is None or steps < self._fed or steps - self._fed >= FEED_STEPS:
            self._fed = steps
            t = steps / STEPS_PER_S
            self.fp.feed(t, sample)
            self.check.feed(t, sample)
            due = (self.fp.grew or self.check.grew or self._match_at is None or t < self._match_at
                   or t - self._match_at >= MATCH_EVERY)
            if due:
                self._match_at = t
                if self.rechecking:
                    self._check()
                elif self.key is None or self.provisional or exact is not None:
                    events += self._match(exact)
        if self.key is not None:
            sample.car, sample.car_name = self.key, self.name or sample.car_name
        sample.car_epoch, sample.car_provisional = self.epoch, self.provisional
        return events

    # -- matching --

    def _name_of(self, key):
        entry = self.library.entry(key)
        return (entry or {}).get('name') or car_data.display_name(key) or key

    def _decided(self, fp):
        """Enough driven that a gear set neither fitting nor missing will not come to fit: it counts as a miss."""
        est = fp.estimates()
        return fp.distance >= DECIDED_M or bool(est) and all(len(fp.ratios[g]) >= GEAR_KEEP for g in est)

    def _match(self, exact):
        est = self.fp.estimates()
        if len(est) < GEARS_MIN:
            return []
        if exact is not None:
            # The install named it: its gearing is recorded (once an epoch, and again at its end), so this slot's car
            # is known without the install later; a car of ours with that gearing in this slot is this one
            if self._recorded == (exact, self.epoch):
                return []
            events = self._adopt([e for e in self._ours(est, self.slot) if e['key'] != exact], exact)
            return events + self._record(exact, 'install', est, name=self.name)
        found = self.find(est, self.slot, self.fp.top, self.fp.rpm, self._decided(self.fp))
        if found is None:
            return []
        key, name, kind, adopt = found
        events = self._adopt(adopt, key)
        before, was = self.key, self.provisional
        self.key, self.name, self.provisional = key, name, False
        if before == key and was:
            logging.info("rbr car: %s confirmed by its gearing after %.0f m, %.0f s", key, self.fp.distance,
                         self.fp.seconds)
        elif before != key:
            logging.info("rbr car: %s (%s) from its gearing after %.0f m, %.0f s: %s%s", key, name, self.fp.distance,
                         self.fp.seconds, ', '.join('%d: %.1f' % (g, v) for g, v in sorted(est.items())),
                         ' (was %s)' % before if before else '')
        last = {'key': key, 'slot': self.slot}
        changed = self.library.last != last
        self.library.last = last
        recorded = self._record(key, kind, est, name=name)
        if changed and not recorded:
            recorded = [('save', self.library.snapshot())]           # the last car, for the next start
        return events + recorded

    def _adopt(self, entries, key):
        """Mark our entries as the car `key` from now on: ('adopt', old, key) once each."""
        events = []
        for entry in entries:
            if entry.get('adopted'):
                continue
            entry['adopted'] = key
            events.append(('adopt', entry['key'], key))
        return events

    def _ours(self, est, slot):
        """The library's local and candidate-set entries in `slot` that `est` fits, the closest first."""
        found = []
        for entry in self.library.cars:
            if entry.get('kind') in ('local', 'set') and entry['slot'] == slot:
                verdict, worst, _ = compare(entry['gears'], est)
                if verdict == 'match':
                    found.append((worst, entry))
        return [e for _, e in sorted(found, key=lambda x: x[0])]

    def _references(self, key):
        """The gearings the car `key` is known by: its library entries', its shipped gear sets (each candidate's, for
        a candidate set)."""
        refs = [e['gears'] for e in self.library.cars if e['key'] == key or e.get('adopted') == key]
        ids = key.split('/', 1)[1].split('@', 1)[0].split('+') if key.startswith('rbr/') else []
        for i in ids:
            entry = car_data.cars().get('rbr/' + i)
            if entry is not None:
                refs += [{n + 1: r for n, r in enumerate(s['gears'])} for s in entry['gear_sets']]
        return refs

    def _check(self):
        """The stage was loaded again within the epoch: the gearing driven since must fit the known car again (GEARS_MIN
        gears) before anything is learnt under it. A miss (or no fit DECIDED_M on) is another car, changed in the menus
        quicker than MENU_GAP: a new epoch, car unknown, and only what was driven since the load goes with it."""
        est = self.check.estimates()
        if len(est) < GEARS_MIN:
            return
        verdicts = [compare(r, est)[0] for r in self._references(self.key)]
        if 'match' in verdicts:
            self.provisional = self.rechecking = False
            logging.info("rbr car: %s confirmed again by its gearing after the stage load, %.0f m", self.key,
                         self.check.distance)
        elif (verdicts and all(v == 'miss' for v in verdicts)) or self._decided(self.check):
            logging.info("rbr car: the gearing since the stage load does not fit %s (%s): another car", self.key,
                         ', '.join('%d: %.1f' % (g, v) for g, v in sorted(est.items())))
            self._force = True

    def find(self, est, slot, top, rpm, decided=False):
        """(key, name, kind, [library entries it adopts]) of the car `est` fits, or None to wait for more. With
        `decided`, a gear set that neither fits nor misses counts as a miss."""
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
            elif verdicts and not decided and not all(v == 'miss' for v in verdicts):
                waiting = True
        ours = self._ours(est, slot)
        if not ours and not decided and len(est) < MINT_GEARS and any(
                e.get('kind') in ('local', 'set') and e['slot'] == slot and compare(e['gears'], est)[0] is None
                for e in library.cars):
            waiting = True
        if len(fits) == 1 and not impostors(fits[0][0]):
            # The table's one car with that gearbox, and no car it lacks could have it: that car
            key, name = fits[0]
            return key, name, 'shipped', [e for e in ours if e['key'] != key]
        if ours:
            entry = ours[0]
            if entry.get('adopted'):
                return entry['adopted'], car_data.display_name(entry['adopted']) or entry['adopted'], 'shipped', []
            return entry['key'], entry.get('name') or entry['key'], entry['kind'], []
        if fits:
            # Cars one gearbox fits, or one fit that a car the table has no gearing for could share: a set, bound to
            # the slot, with the data its members share (shared_entry)
            keys = [k for k, _ in fits]
            return set_key(keys, slot), set_name(keys, slot), 'set', []
        if waiting or len(est) < MINT_GEARS:
            return None
        n = library.next_number(self.taken() if self.taken is not None else ())
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
    default gearing, torque within TORQUE_AGREE at each point): the first candidate's entry; else None. A set of one
    ('rbr/106@5') has its car's."""
    game, _, rest = key.partition('/')
    ids = rest.split('@', 1)[0].split('+')
    entries = [car_data.cars().get('{}/{}'.format(game, i)) for i in ids]
    if not entries or any(e is None for e in entries):
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

