#!/usr/bin/env python3
"""Read the real sector (split) lines of the Assetto Corsa Rally stages from an
installed game and print where each lies along the road spline, in the units of
the shared memory's distance (m along the stage's centre spline).

    scripts/acr-sectors.py [--game-dir DIR] [--retoc PATH] [--work DIR] [--json PATH]

Needs `retoc` (github.com/trumank/retoc, MIT; it fetches the Oodle library it
needs on first use) to turn the game's IoStore containers (Oodle-compressed,
not encrypted) into legacy packages; everything else is read here. The game's
files are only read, never written; the work directory holds the converted
level cells of one map at a time and is emptied after each map. Only derived
numbers are printed, never game data.

What is read (UE 5.6, cooked, unversioned properties), per stage map
(`Levels/<Map>/_Generated_/*.umap`, the world-partition cells):
- `RaceSector` actors (/Script/acr), one per line: the start (sector index 0),
  each split, and the finish (the last index). Each sits in the cell of its
  variant's gameplay data layer; that cell also holds the variant's
  `PacenoteSetupActor`, whose DT_Pacenote<...> import names the variant. The
  actor's location is its root component, a BoxComponent named `Trigger`
  (property 125 RelativeLocation; 0 BoxExtent, 126 RelativeRotation and
  127 RelativeScale3D when not the class default).
- The `SplinesActor<Forward|Reverse>` (/Script/dmphysics) and its
  `CenterSpline` (a DMSplineComponent): an FInterpCurveVector of points about
  20 m apart. Its length is the shared memory's trackSplineLength (Cwmbiga:
  12077.93 m here, 12077.95 m from the game) and the projection of the car's
  position on it is the shared memory's distance (marth's runs: median
  difference 0.0-0.1 m). Cut variants use their direction's full spline.

A line's distance is the projection of its location on the centre spline.
Against the game's clock (marth's v4 captures): the clock stops when the
car's distance reaches the finish line (Loutraki reverse: 10463.2 vs 10463.0 m,
Sommet de Munster: 10477.6 vs 10476.5), and the car stands 2.6-2.9 m short
of the start line when the clock starts (its centre, with the nose at the line).
"""
import argparse
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_DIR = '/mnt/gaming/Steam/steamapps/common/Assetto Corsa Rally'
STAGES = os.path.join(HERE, '..', 'data', 'telemetry', 'stages', 'acr.json')
WORK = os.path.expanduser('~/.cache/oversteer-re/acr-sectors')
MAPS = ('AlsaceS2Munster', 'AlsaceS4Saverne', 'WalesS3HafrenNorth', 'WalesS4HafrenSouth',
        'MonteCarloS1Bollene', 'MonteCarloS2Sisteron', 'GreeceS3Elatia', 'GreeceS4Loutraki',
        'LivignoIceDrivingSchool')
STEPS = 64                   # samples per spline segment for its length and projections
BOX_EXTENT, BOX_LOCATION, BOX_ROTATION, BOX_SCALE = 0, 125, 126, 127   # BoxComponent property indices


# -- legacy packages (as retoc writes them for UE 5.6: cooked, unversioned) --

def _fstring(b, p):
    n = struct.unpack_from('<i', b, p)[0]; p += 4
    if n < 0:
        return b[p:p - 2 * n].decode('utf-16-le').rstrip('\0'), p - 2 * n
    return b[p:p + n].decode('utf-8', 'replace').rstrip('\0'), p + n


class Package:
    """Name map, imports and exports of a legacy .umap/.uasset; export data from the .uexp beside it."""

    def __init__(self, path):
        self.path = path
        b = open(path, 'rb').read()
        tag, legacy = struct.unpack_from('<Ii', b, 0)
        if tag != 0x9E2A83C1 or legacy != -9:
            raise ValueError('{}: not a UE 5.6 legacy package'.format(path))
        p = 4 + 4 * 5 + 20                       # versions (unversioned: zeros), saved hash
        self.header_size, ncustom = struct.unpack_from('<ii', b, p); p += 8
        p += ncustom * 20
        _name, p = _fstring(b, p)
        p += 4                                   # package flags
        ncount, noff = struct.unpack_from('<ii', b, p); p += 8
        p += 8 + 8                               # soft object paths, gatherable text
        ecount, eoff, icount, ioff = struct.unpack_from('<iiii', b, p)
        self.names = []
        q = noff
        for _ in range(ncount):
            s, q = _fstring(b, q)
            self.names.append(s); q += 4
        name = lambda i, n: self.names[i] if not n else '{}_{}'.format(self.names[i], n - 1)
        self.imports = []
        for k in range(icount):
            cp, cpn, cn, cnn, outer, on, onn, _opt = struct.unpack_from('<iiiiiiii', b, ioff + 32 * k)
            self.imports.append({'class': name(cn, cnn), 'name': name(on, onn), 'outer': outer})
        self.exports = {}
        for k in range(ecount):
            c, _s, _t, outer, on, onn, _f, size, off = struct.unpack_from('<iiiiiiIqq', b, eoff + 96 * k)
            cls = self.imports[-c - 1]['name'] if c < 0 else '?'
            self.exports[k + 1] = {'class': cls, 'name': name(on, onn), 'outer': outer, 'off': off, 'size': size}
        self._exp = None

    def data(self, export):
        if self._exp is None:
            self._exp = open(self.path[:-5] + '.uexp', 'rb')
        self._exp.seek(export['off'] - self.header_size)
        return self._exp.read(export['size'])

    def close(self):
        if self._exp:
            self._exp.close()


def unversioned_header(b, p=0):
    """The property indices an unversioned struct serializes, each with whether its value is
    present (False: zero, nothing stored), and the offset of the first value."""
    frags = []
    while True:
        f = struct.unpack_from('<H', b, p)[0]; p += 2
        frags.append(f)
        if f & 0x100:
            break
    total = sum(f >> 9 for f in frags)
    mask = 0
    if any(f & 0x80 for f in frags):
        if total <= 8:
            mask = b[p]; p += 1
        elif total <= 16:
            mask = struct.unpack_from('<H', b, p)[0]; p += 2
        else:
            for w in range((total + 31) // 32):
                mask |= struct.unpack_from('<I', b, p)[0] << (32 * w); p += 4
    props, idx, bit = [], 0, 0
    for f in frags:
        idx += f & 0x7f
        for _ in range(f >> 9):
            props.append((idx, not (mask >> bit) & 1)); idx += 1; bit += 1
    return props, p


def box_component(b):
    """{property index: (x, y, z)} of a cooked BoxComponent (only its vector/rotator properties)."""
    props, p = unversioned_header(b)
    out = {}
    for idx, present in props:
        if idx not in (BOX_EXTENT, BOX_LOCATION, BOX_ROTATION, BOX_SCALE):
            raise ValueError('unexpected BoxComponent property {}'.format(idx))
        if present:
            out[idx] = struct.unpack_from('<3d', b, p); p += 24
        else:
            out[idx] = (0.0, 0.0, 0.0)
    return out


def race_sector_index(b):
    """A RaceSector's sector index (property 2, an int; 0 is not stored). Properties 0 and 1 are
    object references (Trigger, SplineLocations)."""
    props, p = unversioned_header(b)
    present = [i for i, there in props if there]
    if present[:2] != [0, 1]:
        raise ValueError('unexpected RaceSector layout {}'.format(present))
    index = struct.unpack_from('<i', b, p + 8)[0] if 2 in present else 0
    label = re.search(rb'Sector(\d+)\x00', b)
    if label and int(label.group(1)) != index:
        raise ValueError('RaceSector index {} against its name {}'.format(index, label.group(1)))
    return index


def interp_curve(b, p):
    """An FInterpCurveVector: [(in_val, out_val, arrive_tangent, leave_tangent)], or None."""
    n = struct.unpack_from('<i', b, p)[0]; p += 4
    if not 2 <= n <= 1000000:
        return None
    points = []
    for i in range(n):
        props, p = unversioned_header(b, p)
        v = [0.0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)]
        for idx, present in props:
            if not present:
                continue
            if idx == 0:
                v[0] = struct.unpack_from('<f', b, p)[0]; p += 4
            elif idx < 4:
                v[idx] = struct.unpack_from('<3d', b, p); p += 24
            elif idx == 4:
                p += 1                       # interp mode
            else:
                return None
        if abs(v[0] - i) > 1e-3:
            return None
        points.append(tuple(v))
    return points


def spline_points(b):
    """The position curve of a cooked spline component (found right after its property header)."""
    for start in range(0, 96):
        try:
            pts = interp_curve(b, start)
        except (struct.error, IndexError):
            pts = None
        if pts:
            return pts
    raise ValueError('no spline curve found')


# -- geometry (UE units: cm; x, y plan, z up = ACR's x, z plan, y up) --

def polyline(points, steps=STEPS):
    """Dense samples (x, y, z, distance in m) along a Hermite spline."""
    out, dist, prev = [], 0.0, None
    for i in range(len(points) - 1):
        p0, t0 = points[i][1], points[i][3]
        p1, t1 = points[i + 1][1], points[i + 1][2]
        for k in range(steps + (1 if i == len(points) - 2 else 0)):
            t = k / steps
            t2, t3 = t * t, t * t * t
            h = (2 * t3 - 3 * t2 + 1, t3 - 2 * t2 + t, -2 * t3 + 3 * t2, t3 - t2)
            q = tuple(h[0] * a + h[1] * b + h[2] * c + h[3] * d for a, b, c, d in zip(p0, t0, p1, t1))
            if prev is not None:
                dist += math.dist(q, prev) / 100.0
            out.append((q[0], q[1], q[2], dist))
            prev = q
    return out


def project(poly, loc):
    """(distance along the polyline in m, distance from it in m) of a point."""
    best = None
    for a, b in zip(poly, poly[1:]):
        ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        l2 = ab[0] * ab[0] + ab[1] * ab[1] + ab[2] * ab[2] or 1e-9
        t = ((loc[0] - a[0]) * ab[0] + (loc[1] - a[1]) * ab[1] + (loc[2] - a[2]) * ab[2]) / l2
        t = min(1.0, max(0.0, t))
        d = math.dist((a[0] + t * ab[0], a[1] + t * ab[1], a[2] + t * ab[2]), loc)
        if best is None or d < best[1]:
            best = (a[3] + t * (b[3] - a[3]), d)
    return best[0], best[1] / 100.0


# -- the game's variants --

def variant_key(text, location):
    """(location, cut number or 0, 'Forward'|'Reverse') of a pace-note table or variant id."""
    rest = text.split(location, 1)[1] if location in text else None
    if rest is None:
        return None
    cut = re.search(r'(?:Cut|Short)(\d)', rest)
    direction = 'Reverse' if 'Reverse' in rest else 'Forward'
    return (location, int(cut.group(1)) if cut else 0, direction)


def scan_map(cells):
    """({cell's variant key text: [gate dict]}, {spline name: points}) of one map's cells."""
    gates, splines = {}, {}
    for path in cells:
        pkg = Package(path)
        classes = {e['class'] for e in pkg.exports.values()}
        if 'RaceSector' in classes:
            tables = [i['name'] for i in pkg.imports if i['class'] == 'DataTable' and i['name'].startswith('DT_Pacenote')]
            found = []
            for k, e in pkg.exports.items():
                if e['class'] != 'RaceSector':
                    continue
                index = race_sector_index(pkg.data(e))
                trigger = [x for x in pkg.exports.values() if x['outer'] == k and x['class'] == 'BoxComponent']
                box = box_component(pkg.data(trigger[0]))
                found.append({'index': index, 'loc': box.get(BOX_LOCATION, (0.0, 0.0, 0.0)),
                              'yaw': box.get(BOX_ROTATION, (0.0, 0.0, 0.0))[1]})
            found.sort(key=lambda g: g['index'])
            key = tables[0][len('DT_Pacenote'):] if tables else os.path.basename(path)
            gates.setdefault(key, []).extend(found)
        if 'SplinesActor' in classes:
            for k, e in pkg.exports.items():
                if e['class'] != 'SplinesActor':
                    continue
                name = re.search(rb'SplinesActor(\w+)\x00', pkg.data(e))
                for x in pkg.exports.values():
                    if x['outer'] == k and x['name'] == 'CenterSpline':
                        splines[name.group(1).decode() if name else str(k)] = spline_points(pkg.data(x))
        pkg.close()
    return gates, splines


def convert(retoc, game_dir, level, work):
    paks = os.path.join(game_dir, 'acr', 'Content', 'Paks')
    subprocess.run([retoc, 'to-legacy', '--no-shaders', '--no-script-objects', '-f',
                    'Levels/{}/_Generated_'.format(level), paks, work], check=True, capture_output=True)
    cells = os.path.join(work, 'acr', 'Content', 'Levels', level, '_Generated_')
    return sorted(os.path.join(cells, f) for f in os.listdir(cells) if f.endswith('.umap'))


def read_map(retoc, game_dir, level, work):
    """{'splines': {name: length_m}, 'variants': {key: {'spline', 'gates_m', 'off_m'}}} of one map."""
    if os.path.exists(work):
        shutil.rmtree(work)
    try:
        gates, splines = scan_map(convert(retoc, game_dir, level, work))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    polys = {k: polyline(v) for k, v in splines.items()}
    out = {'splines': {k: round(v[-1][3], 2) for k, v in polys.items()}, 'variants': {}}
    for key, found in gates.items():
        if key.startswith('DT_') or key.endswith('.umap') or not any(d in key for d in ('Forward', 'Reverse')):
            # a circuit (Livigno: no pace notes): of the splines the lines lie on (within 10 m),
            # the one that meets them in their order (once round the lap)
            def fit(s):
                proj = [project(polys[s], g['loc']) for g in found]
                d = [x for x, _ in proj]
                descents = sum(1 for a, b in zip(d, d[1:] + d[:1]) if b < a)
                return (max(o for _, o in proj) > 10.0, descents, sum(o for _, o in proj))
            best = min(polys, key=fit)
        else:
            best = 'Reverse' if 'Reverse' in key else 'Forward'
        proj = [project(polys[best], g['loc']) for g in found]
        out['variants'][key] = {'spline': best, 'indices': [g['index'] for g in found],
                                'gates_m': [round(d, 1) for d, _ in proj], 'off_m': [round(o, 1) for _, o in proj]}
    return out


def match_variants(level, found, stages):
    """{game_variant_id: variant} for one map's variants."""
    location = re.sub(r'^[A-Za-z]+S\d', '', level)
    out = {}
    if level.startswith('Livigno'):
        # two circuits, forward and reverse; the stage table's main circuit is the 918 m one (Rim1)
        for key, v in found.items():
            if v['spline'].startswith('Rim1'):
                direction = 'Reverse' if v['spline'].endswith('Reverse') else 'Forward'
                out['LivignoTestTrack01Full' + direction] = dict(v, circuit=True)
        return out
    ids = {variant_key(s['game_variant_id'], location): s['game_variant_id'] for s in stages
           if variant_key(s['game_variant_id'], location)}
    for key, v in found.items():
        k = variant_key(key, location)
        if k in ids:
            out[ids[k]] = v
        else:
            print('{}: no stage for {}'.format(level, key), file=sys.stderr)
    return out


def estimate(entry):
    """The current estimate's sector bounds (oversteer.stage_tables.sector_bounds), if the app is beside."""
    try:
        sys.path.insert(0, os.path.join(HERE, '..'))
        from oversteer.stage_tables import sector_bounds
    except ImportError:
        return None
    b = sector_bounds(entry)
    return [round(b['start_m'], 1)] + [round(e, 1) for _, e in b['bounds']] if b else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--game-dir', default=GAME_DIR)
    ap.add_argument('--retoc', default=shutil.which('retoc') or os.path.expanduser('~/.cache/oversteer-re/target/release/retoc'))
    ap.add_argument('--work', default=WORK, help='scratch directory for one map\'s converted cells (emptied after each)')
    ap.add_argument('--stages', default=STAGES, help='the stage table, to name the variants and compare')
    ap.add_argument('--json', help='write {game_variant_id: {...}} here')
    ap.add_argument('maps', nargs='*', default=MAPS)
    args = ap.parse_args()
    stages = json.load(open(args.stages))['stages']
    by_id = {s['game_variant_id']: s for s in stages}
    result = {}
    for level in args.maps:
        found = read_map(args.retoc, args.game_dir, level, os.path.join(args.work, level))
        for vid, v in match_variants(level, found['variants'], stages).items():
            v['spline_length_m'] = found['splines'][v['spline']]
            result[vid] = v
    print('{:36s} {:>7s}  {}'.format('variant', 'spline', 'lines along the road spline (m): start, splits, finish'))
    for vid, v in sorted(result.items()):
        s = by_id.get(vid, {})
        sectors = [round(b - a, 1) for a, b in zip(v['gates_m'], v['gates_m'][1:])]
        if v.get('circuit'):            # lines met once round the lap, the last sector back to the first line
            g, length = v['gates_m'], v['spline_length_m']
            sectors = [round((b - a) % length, 1) for a, b in zip(g, g[1:] + g[:1])]
        print('{:36s} {:>7.1f}  {}'.format(vid, v['spline_length_m'], v['gates_m']))
        note = []
        if s.get('pacenote_first_m') is not None:
            v['start_to_first_note_m'] = round(s['pacenote_first_m'] - v['gates_m'][0], 1)
            v['finish_to_last_note_m'] = round(s['pacenote_last_m'] - v['gates_m'][-1], 1)
            print('{:44s} first note {} m past the start, last note (stop control) {} m past the finish'.format(
                '', v['start_to_first_note_m'], v['finish_to_last_note_m']))
            if v['start_to_first_note_m'] < 0:
                # three cuts (Hafren Forest, Zeli Reverse, Aghii Theodori Reverse): the stage table's first
                # note is the full stage's, and the cut's own start line is further on; their lengths agree
                note.append('start line past the table\'s first pace note')
            if not (v['start_to_first_note_m'] < 150 and 0 < v['finish_to_last_note_m'] < 400):
                note.append('lines out of place against the pace notes')
        if max(v['off_m']) > 40.0:
            note.append('a line {} m off the spline'.format(max(v['off_m'])))
        if s.get('sectors_km') and len(s['sectors_km']) != len(v['gates_m']) - (0 if v.get('circuit') else 1):
            note.append('{} lines for {} sectors'.format(len(v['gates_m']), len(s['sectors_km'])))
        print('{:44s} sectors {} (table {})'.format('', [round(x / 1000, 2) for x in sectors], s.get('sectors_km')))
        est = None if v.get('circuit') else estimate(s)
        if est:
            print('{:44s} estimate {}  diff {}'.format('', est, [round(a - b, 1) for a, b in zip(est, v['gates_m'])]))
        if note:
            print('{:44s} ! {}'.format('', '; '.join(note)))
    missing = [s['game_variant_id'] for s in stages if s['game_variant_id'] not in result]
    print('{} of {} stages found{}'.format(len(result), len(stages), '; missing: ' + ', '.join(missing) if missing else ''))
    if args.json:
        with open(args.json, 'w') as f:
            json.dump(result, f, indent=1, sort_keys=True)


if __name__ == '__main__':
    main()
