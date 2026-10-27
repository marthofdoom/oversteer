#!/usr/bin/env python3
"""Build data/telemetry/cars/rbr.json (Richard Burns Rally car names, and the engine and gearing of the cars whose NGP
physics the install holds) from an RSF install, read-only. Run it again after loading more cars in RSF.

    python3 research/rbr/car_table.py ROOT [OUT]

ROOT is the folder holding RichardBurnsRally.ini (marth's: /mnt/gaming/RBR/RBR). Every car in rsfdata/cache/cars.json
gets a name (`names`, key rbr/<RSFCarID>, the key rbr_install.car_in_slot makes). A car also gets an entry of
car_data's format where its physics are on disk, from the first of:

- rsfdata/cars/<path>/ or rsfdata/cars/<path>.zip (RSF's download of the car, `path` in cars.json), and
- a slot folder rsfdata/Physics/<slot>/ that holds an RSF car, marked by a file named after the car (the stock RBR cars'
  slots have no such file and are not shipped: no stock car can be told from RSF's by a slot's name).

An entry is read from common.lsp (Engine Tq_NN_RPM_NGP and Tq_NN_NGP, ControlUnit RPMLimit and GearNUpshift, Drive
DriveType and NumberOfGears, Car InverseMass and EffWheelRadius, Drive DriveTrainLoss_NGP from r_<surface>.lsp), the
options file r_<surface>.lsp (GearListNNPinion/Gear) and the default setup setups/d_<surface>.lsp (GearId0..,
FinalDriveId, DropGearId). Torque in Nm is the normalised curve times the peak torque cars_data.json lists. A car
that an earlier run read and this install no longer holds keeps its entry from the existing file, which is why the file
is written here (OUT, default data/telemetry/cars/rbr.json) and not to stdout: a shell redirect would empty it first."""
import datetime
import json
import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from oversteer import rbr_install  # noqa: E402 (repo module, read-only use of the install)

SURFACES = ('gravel', 'tarmac', 'snow')                    # the default setups; gravel is the default set
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'data', 'telemetry', 'cars', 'rbr.json')
_VALUE = re.compile(r'^\s*([A-Za-z_]\w*)\s+([-+]?\d+\.?\d*(?:[eE][-+]?\d+)?)\s*$')
_TORQUE = re.compile(r'(\d+(?:\.\d+)?)\s*Nm\s*/\s*(\d+)\s*rpm', re.I)


class Dir:
    def __init__(self, path, label):
        self.path, self.label = path, label

    def read(self, rel):
        path = rbr_install.resolve(self.path, rel)
        if path is None or not os.path.isfile(path):
            return None
        with open(path, encoding='latin-1') as f:
            return f.read()

    def has(self, rel):
        path = rbr_install.resolve(self.path, rel)
        return path is not None and os.path.isfile(path)

    def names(self):
        return os.listdir(self.path)


class Zip(Dir):
    def __init__(self, path, label):
        super().__init__(path, label)
        with zipfile.ZipFile(path) as z:
            self.files = {n.split('/', 1)[1].lower(): n for n in z.namelist() if '/' in n and not n.endswith('/')}

    def read(self, rel):
        member = self.files.get(rel.replace('\\', '/').lower())
        if member is None:
            return None
        with zipfile.ZipFile(self.path) as z:
            return z.read(member).decode('latin-1')

    def has(self, rel):
        return rel.replace('\\', '/').lower() in self.files

    def names(self):
        return [n for n in self.files if '/' not in n]


def lsp(text):
    """{name: first number} of a Lisp-ish physics file: its `Name value` lines, ';' comments cut. The first one wins,
    so the car's own InverseMass is read, not the doors'."""
    found = {}
    for line in (text or '').splitlines():
        m = _VALUE.match(line.split(';', 1)[0])
        if m:
            found.setdefault(m.group(1), float(m.group(2)))
    return found


def sources(root):
    """[(RSF car name, Dir)] of the physics on disk that are an RSF car's, as (path or marker, source): by cars.json's
    `path` first, then the marked slot folders."""
    found = {}
    base = rbr_install.resolve(root, 'rsfdata/cars')
    physics = rbr_install.resolve(root, 'rsfdata/Physics')
    for row in json.load(open(rbr_install.resolve(root, 'rsfdata/cache/cars.json'), encoding='utf-8')):
        path = row.get('path') or ''
        if not base or not path:
            continue
        folder, archive = os.path.join(base, path), os.path.join(base, path + '.zip')
        if os.path.isdir(folder) and Dir(folder, '').has('common.lsp'):
            found[row['id']] = Dir(folder, 'rsfdata/cars/' + path)
        elif os.path.isfile(archive):
            try:
                found[row['id']] = Zip(archive, 'rsfdata/cars/%s.zip' % path)
            except (OSError, zipfile.BadZipFile):
                pass
    slots = {}
    for slot in sorted(os.listdir(physics)) if physics else ():
        folder = Dir(os.path.join(physics, slot), 'rsfdata/Physics/' + slot)
        if folder.has('common.lsp'):
            slots[slot] = folder
    names = {}
    for row in json.load(open(rbr_install.resolve(root, 'rsfdata/cache/cars.json'), encoding='utf-8')):
        names.setdefault(row['name'], row['id'])
    for slot, folder in slots.items():
        marker = next((n for n in folder.names() if n in names and folder.has(n)), None)
        if marker and names[marker] not in found:
            found[names[marker]] = folder
    return found


def ratio(options, index, invert=False):
    """A GearList entry as a ratio: Gear / Pinion (teeth), None where it is the 0 / 0 of neutral. The drop gear's pair is
    stored the other way round (Pinion 35, Gear 8 is the 4.375 reduction: marth's Fiesta captures)."""
    pinion, gear = options.get('GearList%02dPinion' % index), options.get('GearList%02dGear' % index)
    if not pinion or not gear:
        return None
    return pinion / gear if invert else gear / pinion


def setup_gearing(source, surface, forward):
    """{'gears': [1st..], 'reverse', 'final'} of the default setup of `surface`, or None where the car has none."""
    setup, options = lsp(source.read('setups/d_%s.lsp' % surface)), lsp(source.read('r_%s.lsp' % surface))
    if 'GearId2' not in setup or not options:
        return None
    gears = [ratio(options, int(setup['GearId%d' % g])) for g in range(2, 2 + forward) if 'GearId%d' % g in setup]
    if len(gears) != forward or not all(gears):
        return None
    reverse = ratio(options, int(setup['GearId0'])) if 'GearId0' in setup else None
    final = ratio(options, int(setup['FinalDriveId'])) if setup.get('FinalDriveId') else None
    drop = ratio(options, int(setup['DropGearId']), invert=True) if setup.get('DropGearId') else None
    if not drop:
        return None                              # no drop gear, no overall ratio: not shipped
    return {'gears': gears, 'reverse': abs(reverse) if reverse else None, 'final': drop, 'transfer': final,
            'loss': options.get('DriveTrainLoss_NGP')}


def torque_curve(common, peak_nm, limiter):
    """[[rpm, Nm]...] up to the limiter (the point at the limiter interpolated), and the rpm of the curve's peak."""
    points = []
    for i in range(32):
        rpm, norm = common.get('Tq_%02d_RPM_NGP' % i), common.get('Tq_%02d_NGP' % i)
        if rpm is None or norm is None:
            break
        if not points or rpm > points[-1][0]:
            points.append((rpm, norm))
    cut = []
    for (r0, t0), (r1, t1) in zip(points, points[1:] + [(None, None)]):
        if r0 <= limiter:
            cut.append((r0, t0))
        if r1 is not None and r0 < limiter < r1:
            cut.append((limiter, t0 + (t1 - t0) * (limiter - r0) / (r1 - r0)))
    peak = max(cut, key=lambda p: p[1]) if cut else (0, 0)
    return [[round(r), round(t * peak_nm, 1)] for r, t in cut], peak


def build_car(row, data, source):
    """(entry, notes) of one car, or (None, why)."""
    common = lsp(source.read('common.lsp'))
    limiter = common.get('RPMLimit')
    n_gears = common.get('NumberOfGears')
    peak = _TORQUE.search(data.get('torque') or '')
    if not limiter or not n_gears or not peak or not common.get('InverseMass'):
        return None, 'no limiter, gear count, listed torque or mass'
    forward = int(n_gears) - 2                   # reverse and neutral are two of NumberOfGears
    sets = {}
    for surface in SURFACES:
        gearing = setup_gearing(source, surface, forward)
        if gearing:
            sets[surface] = gearing
    if not sets:
        return None, 'no readable default setup with a drop gear'
    base = sets.get('gravel') or sets[next(iter(sets))]
    groups = []                                  # surfaces whose gearing is the same share a set
    for surface, g in sets.items():
        same = next((x for x in groups if x[1]['gears'] == g['gears'] and abs(x[1]['final'] - g['final']) < 1e-9), None)
        if same:
            same[0].append(surface)
        else:
            groups.append(([surface], g))
    gear_sets = [{'id': '+'.join(surfaces), 'gears': [round(x, 4) for x in g['gears']],
                  'primary': round(g['final'] / base['final'], 4), 'reverse': round(g['reverse'], 4) if g['reverse'] else None,
                  'default': base is g or g['gears'] == base['gears'] and abs(g['final'] - base['final']) < 1e-9}
                 for surfaces, g in groups]
    curve, curve_peak = torque_curve(common, float(peak.group(1)), limiter)
    if len(curve) < 2 or not curve_peak[1]:
        return None, 'no torque curve'
    radius = common.get('EffWheelRadius')
    uppers = [common.get('Gear%dUpshift' % (g + 1)) for g in range(1, forward)]
    entry = {
        'name': str(row['id']),
        'screen_name': row['name'],
        'drivetrain': data.get('drivetrain'),
        'mass_kg': round(1.0 / common['InverseMass'], 1),
        'limiter_rpm': int(limiter),
        'torque_curve': curve,
        'gear_sets': gear_sets,
        'final_drive': round(base['final'], 4),
        'transfer_ratio': round(base['transfer'], 4) if base['transfer'] else None,
        'gearbox_efficiency': round(1.0 - base['loss'] / 100.0, 3) if base['loss'] is not None else None,
        'tyre_radius_m': {s: [radius, radius] for s in sets} if radius else None,
        'turbo': True if common.get('Turbo') == 1 else None,          # NGP's Turbo 0 is the stock flag: boost is in the curve
        'auto_upshift_rpm': [int(u) for u in uppers] if all(uppers) else None,
        'sources': ['%s: common.lsp (Engine Tq_NN_RPM_NGP and Tq_NN_NGP, ControlUnit RPMLimit and GearNUpshift, Car '
                    'InverseMass and EffWheelRadius), r_<surface>.lsp (GearList, DriveTrainLoss_NGP), setups/d_<surface>.lsp '
                    '(GearId, FinalDriveId, DropGearId); revision %s' % (source.label, _revision(source, row)),
                    'rsfdata/cache/cars_data.json: peak torque %s Nm at %s rpm' % (peak.group(1), peak.group(2))],
    }
    entry = {k: v for k, v in entry.items() if v is not None}
    for g in gear_sets:
        if g['reverse'] is None:
            del g['reverse']
    note = None
    if abs(curve_peak[0] - float(peak.group(2))) > 500 or abs(curve_peak[1] - 1.0) > 0.01:
        note = 'curve peaks at %d rpm, %.2f of the peak (listed: %s rpm)' % (curve_peak[0], curve_peak[1], peak.group(2))
    return entry, note


def _revision(source, row):
    marker = next((n for n in source.names() if n.lower() == row['name'].lower()), None)
    text = source.read(marker) if marker else None
    m = re.search(r'revision\s+(.+)', text or '')
    return m.group(1).strip() if m else (row.get('rev') or '?')


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    root = sys.argv[1]
    listed = json.load(open(rbr_install.resolve(root, 'rsfdata/cache/cars.json'), encoding='utf-8'))
    extra = {r['id']: r for r in json.load(open(rbr_install.resolve(root, 'rsfdata/cache/cars_data.json'), encoding='utf-8'))}
    cars_by_id = {r['id']: r for r in listed}
    names, built = {}, {}
    for row in listed:
        data = extra.get(row['id'], {})
        word = re.match(r'\s*([A-Za-z0-9]+)', data.get('drive_train') or '')
        drive = rbr_install.DRIVETRAINS.get(word.group(1).lower()) if word else None
        gears = re.match(r'\s*(\d+)\s*gear', data.get('transmission') or '', re.I)
        fact = {'name': row['name'], 'path': row.get('path'), 'year': data.get('year') or None, 'drivetrain': drive,
                'gears': int(gears.group(1)) if gears else None}
        names['rbr/%s' % row['id']] = [{k: v for k, v in fact.items() if v}]
    for cid, source in sorted(sources(root).items(), key=lambda kv: int(kv[0])):
        row = cars_by_id[cid]
        data = dict(extra.get(cid, {}), drivetrain=names['rbr/%s' % cid][0].get('drivetrain'))
        entry, note = build_car(row, data, source)
        if entry is None:
            print('rbr car %s %s: not built: %s' % (cid, row['name'], note), file=sys.stderr)
            continue
        if note:
            print('rbr car %s %s: %s' % (cid, row['name'], note), file=sys.stderr)
        built[cid] = entry
    target = os.path.normpath(sys.argv[2] if len(sys.argv) > 2 else OUTPUT)
    if os.path.isfile(target):                               # a car an earlier run read keeps its entry
        try:
            for entry in json.load(open(target, encoding='utf-8')).get('cars', []):
                if entry['name'] not in built and 'rbr/%s' % entry['name'] in names:
                    built[entry['name']] = entry
        except (OSError, ValueError, KeyError):
            pass
    out = {
        'game': 'rbr',
        'title': 'Richard Burns Rally (RallySimFans) car names, engines and gearing',
        'keys': "name is the RSFCarID as a string: the car key is rbr/<RSFCarID>, the one Oversteer makes from the install's "
                "Cars/Cars.ini [Car05] (rbr_install.car_in_slot). screen_name is RSF's name of the car. `names` lists every car "
                "RSF's car list (rsfdata/cache/cars.json) has, with the facts cars_data.json holds (path, year, drivetrain, "
                "gear count); `cars` only those whose NGP physics have been on the install.",
        'notes': "Derived numbers only, read from RSF's physics files of the cars on marth's install, %s; built by "
                 "research/rbr/car_table.py. torque_curve: [rpm, Nm], the engine's full-load torque: the normalised curve "
                 "(Tq_NN_NGP, 0..1) times the peak torque RSF lists for the car (cars_data.json), cut at the limiter; the curve "
                 "peaks at the listed rpm. limiter_rpm: ControlUnit RPMLimit (the rpm seen reached is up to 0.6 %% "
                 "above it). gear_sets: one per distinct default setup (gravel, tarmac, snow; the surfaces that share one are "
                 "joined in its id), gravel the default; gears are the setup's GearId entries 2.. (GearId0 is reverse, 1 neutral) "
                 "as GearList Gear / Pinion teeth. final_drive: the setup's DropGearId entry, whose pair is stored the other way "
                 "(Pinion / Gear: 35 / 8 = 4.375 on the Fiesta); a set whose drop gear differs carries the quotient in "
                 "`primary`, so the overall ratio is gear x primary x final_drive. transfer_ratio: the FinalDriveId entry "
                 "(Gear / Pinion, 62 / 63 on the Fiesta): NOT in the overall ratio, the Fiesta's and i20's captures fit "
                 "without it to 0.3 %%. The player's own setup may change the drop gear (DropGearOptions): the "
"overall ratio then differs, the steps between gears (the best upshift) do not. gearbox_efficiency: "
                 "1 - DriveTrainLoss_NGP / 100. tyre_radius_m: EffWheelRadius (one value for every surface). auto_upshift_rpm: "
                 "ControlUnit GearNUpshift for 1st.. (the AI's automatic gearbox). mass_kg: 1 / InverseMass. A slot folder "
                 "of rsfdata/Physics counts only where a file named after the RSF car marks it; the others hold stock Richard "
                 "Burns Rally cars and are not shipped. Verified on the i20 Coupe WRC 2021 (all gears) and the Fiesta WRC 2019 (gears 1-3) against marth's captures of "
                 "2026-10-26 (docs/richard-burns-rally.md)." % datetime.date.today().isoformat(),
        'cars': [built[k] for k in sorted(built, key=int)],
        'names': names,
    }
    with open(target, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
        f.write('\n')


if __name__ == '__main__':
    main()
