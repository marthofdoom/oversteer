#!/usr/bin/env python3
"""Read-only survey of a RallySimFans (RSF) Richard Burns Rally install: what
a decoder needs from disk to name stages and cars (docs/richard-burns-rally.md,
section 3). Opens files for reading only; writes nothing next to the game.

Usage:
    rsf_install.py ROOT --telemetry         the [NGP] UDP keys of RichardBurnsRally.ini
    rsf_install.py ROOT --stages [--csv F]  every stage: id, name, length, surface (stages_data.json,
                                            Maps/Tracks.ini and the per-stage Maps/*/Tracks<id>.ini)
    rsf_install.py ROOT --cars              the eight Cars/Cars.ini slots and the RSF car list
    rsf_install.py ROOT --car-now           the car RSF has put in a slot (RSFCarID) and its data
    rsf_install.py ROOT --markers ID        start / split / finish / end distances of stage ID's
                                            .dls Driveline set, and the .trk driveline's length
    rsf_install.py --dls FILE.dls           the markers of one .dls file
    rsf_install.py --trk FILE.trk           the driveline of one .trk file (count, length, z range)

ROOT is the folder holding RichardBurnsRally_SSE.exe (under Wine: inside the
prefix's drive_c, e.g. .../drive_c/Richard Burns Rally). Paths in the game's
files use backslashes and Windows case; they are resolved case-insensitively.

The .dls and .trk layouts follow ReRBR's reverse-engineering notes
(github.com/JodusNodus/rerbr, notes/formats.md, crates/rbr-formats/src/dls.rs
and markers.rs; GPL-3.0, read for the format only, no code copied). They are
UNVERIFIED here: no RBR file was available when this was written. Run it on
the install and check the finish distance against stages_data.json's length
and the packet's distanceToEnd before trusting it.
"""

import argparse
import configparser
import csv
import json
import math
import os
import struct
import sys

SURFACE_RSF = {1: 'tarmac', 2: 'gravel', 3: 'snow'}          # stages_data.json surface_id (NGPCarMenu RaceStatDB.cpp)
SURFACE_TRACKS_INI = {0: 'tarmac', 1: 'gravel', 2: 'snow'}   # Tracks.ini Surface (same source)
SLOT_FOLDERS = ('c_xsara', 'h_accent', 'mg_zr', 'm_lancer', 'p_206', 's_i2003', 't_coroll', 's_i2000')
MARKER_TAGS = {0x15: 'start', 0x16: 'finish', 0x17: 'split', 0x18: 'end'}
DLS_MAGIC = b'MINAATAD'


# -- paths and ini files --

def resolve(root, rel):
    """`rel` (backslashes, any case) under `root`, or None."""
    path = root
    for part in rel.replace('\\', '/').split('/'):
        if not part or part == '.':
            continue
        try:
            names = os.listdir(path)
        except OSError:
            return None
        match = next((n for n in names if n.lower() == part.lower()), None)
        if match is None:
            return None
        path = os.path.join(path, match)
    return path


def read_ini(path):
    """{section: {key: value}} of an RBR ini (cp1252, quoted values, keys as written)."""
    parser = configparser.RawConfigParser(strict=False, interpolation=None, comment_prefixes=(';', '#'),
                                          inline_comment_prefixes=(';',))
    parser.optionxform = str
    with open(path, encoding='cp1252', errors='replace') as f:
        text = f.read()
    try:
        parser.read_string(text)
    except configparser.MissingSectionHeaderError:
        parser.read_string('[_top]\n' + text)
    return {s: {k: v.strip().strip('"') for k, v in parser.items(s)} for s in parser.sections()}


def read_json(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        return json.load(f)


def _int(v, default=None):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _float(v, default=None):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


# -- telemetry settings --

def telemetry(root):
    path = resolve(root, 'RichardBurnsRally.ini')
    if path is None:
        sys.exit('no RichardBurnsRally.ini under ' + root)
    ngp = read_ini(path).get('NGP', {})
    keys = {k: v for k, v in ngp.items() if 'udp' in k.lower() or 'telemetry' in k.lower()}
    for k, v in sorted(keys.items()):
        print('%s = %s' % (k, v))
    endpoints = keys.get('udpTelemetryEndpoints')
    if endpoints:
        print('endpoints:', [e.strip() for e in endpoints.split(',') if e.strip()])
    if not keys:
        print('(no UDP telemetry keys in [NGP])')


# -- stages --

def stages(root):
    """{id: {...}} from every source found, each value with its source."""
    out = {}
    data = resolve(root, 'rsfdata/cache/stages_data.json')
    if data:
        for row in read_json(data):
            sid = _int(row.get('stage_id', row.get('id')))
            if sid is None:
                continue
            e = out.setdefault(sid, {'id': sid})
            e.update(name=row.get('name'), length_m=_int(row.get('length')),
                     surface=SURFACE_RSF.get(_int(row.get('surface_id'))),
                     surface_parts={s: _int(row.get(s), 0) for s in ('tarmac', 'gravel', 'snow')},
                     country=row.get('short_country'), author=row.get('author'),
                     deftime_s=_int(row.get('deftime')), source='stages_data.json')
    maps = resolve(root, 'Maps')
    inis = []
    tracks = resolve(root, 'Maps/Tracks.ini')
    if tracks:
        inis.append(tracks)
    if maps:
        for sub in sorted(os.listdir(maps)):
            folder = os.path.join(maps, sub)
            if os.path.isdir(folder):
                inis += [os.path.join(folder, n) for n in os.listdir(folder)
                         if n.lower().startswith('tracks') and n.lower().endswith('.ini')]
    for path in inis:
        for section, keys in read_ini(path).items():
            if not section.lower().startswith('map'):
                continue
            sid = _int(section[3:])
            if sid is None:
                continue
            e = out.setdefault(sid, {'id': sid, 'source': os.path.basename(path)})
            e.setdefault('track_name', keys.get('TrackName'))
            if not e.get('name') and keys.get('StageName'):
                e['name'] = keys['StageName']
            if e.get('length_m') is None and _float(keys.get('Length')):
                e['length_m'] = round(_float(keys['Length']) * 1000)       # km to 0.1 km in Tracks.ini
            if e.get('surface') is None and _int(keys.get('Surface')) is not None:
                e['surface'] = SURFACE_TRACKS_INI.get(_int(keys['Surface']))
    return out


def print_stages(root, csv_path=None):
    rows = sorted(stages(root).values(), key=lambda e: e['id'])
    cols = ('id', 'name', 'length_m', 'surface', 'country', 'track_name', 'source')
    if csv_path:
        with open(csv_path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(cols)
            for e in rows:
                w.writerow([e.get(c) for c in cols])
    for e in rows:
        print('%5d  %-40s %7s m  %-7s %s' % (e['id'], (e.get('name') or '?')[:40], e.get('length_m'),
                                             e.get('surface'), e.get('track_name') or ''))
    lengths = {}
    for e in rows:
        if e.get('length_m'):
            lengths.setdefault(e['length_m'], []).append(e['id'])
    print('%d stages; %d lengths shared by more than one id (length alone does not name a stage)'
          % (len(rows), sum(1 for v in lengths.values() if len(v) > 1)))


# -- cars --

def cars(root):
    path = resolve(root, 'Cars/Cars.ini')
    slots = {}
    if path:
        for section, keys in read_ini(path).items():
            if section.lower().startswith('car') and _int(section[3:]) is not None:
                slots[_int(section[3:])] = keys
    rsf = {}
    cj = resolve(root, 'rsfdata/cache/cars.json')
    if cj:
        for row in read_json(cj):
            rsf[_int(row.get('id'))] = dict(row)
    cdj = resolve(root, 'rsfdata/cache/cars_data.json')
    if cdj:
        for row in read_json(cdj):
            cid = _int(row.get('car_id'))
            if cid in rsf:
                rsf[cid]['data'] = row
    return slots, rsf


def common_lsp(root, slot):
    """RPMLimit, NumberOfGears and Gear*Upshift of the slot's physics (the
    plain-text NGP keys; a pack may be encrypted, then nothing)."""
    folder = SLOT_FOLDERS[slot] if 0 <= slot < len(SLOT_FOLDERS) else None
    if folder is None:
        return {}
    for rel in ('Physics/%s/common.lsp' % folder, 'rsfdata/Physics/%s/common.lsp' % folder):
        path = resolve(root, rel)
        if path:
            out = {'file': rel}
            with open(path, encoding='cp1252', errors='replace') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and (parts[0] in ('RPMLimit', 'NumberOfGears', 'MaxSteeringLock',
                                                         'SteeringRackRatio', 'InverseMass')
                                            or (parts[0].startswith('Gear') and parts[0].endswith('Upshift'))):
                        out[parts[0]] = _float(parts[1])
            return out
    return {}


def print_cars(root, now=False):
    slots, rsf = cars(root)
    for slot in sorted(slots):
        keys = slots[slot]
        rid = _int(keys.get('RSFCarID'))
        print('slot %d (%s): %s  RSFCarID=%s  RSFCarPhysics=%s' % (
            slot, SLOT_FOLDERS[slot] if slot < len(SLOT_FOLDERS) else '?', keys.get('CarName'), rid,
            keys.get('RSFCarPhysics')))
        if now and rid is not None:
            car = rsf.get(rid, {})
            print('   rsf: %s (pack %s, group %s, ngp %s)' % (car.get('name'), car.get('path'),
                                                              car.get('base_group_id'), car.get('ngp')))
            d = car.get('data') or {}
            for k in ('power', 'torque', 'drive_train', 'transmission', 'weight', 'wdf', 'steering_wheel', 'year'):
                if d.get(k):
                    print('   %s: %s' % (k, d[k]))
            print('   physics:', common_lsp(root, slot))
    if not now:
        print('%d RSF cars in rsfdata/cache/cars.json, %d with cars_data' % (
            len(rsf), sum(1 for c in rsf.values() if c.get('data'))))


# -- stage files: .dls markers, .trk driveline --

def dls_markers(data):
    """[(tag name, distance m)] of the Driveline animation set, sorted.
    Header: 'MINAATAD', u32 version (18), u32 minor, ten u32 table offsets.
    Table 0: u32 count, count x (u32 record offset, u32); a record is 17 u32,
    word 0 the name's offset in the string block, word 6 the key count,
    word 14 the keys' offset; a key is {u32 tag, u32 flags, f32 distance}.
    Table 7: u32 length, the string block."""
    if data[:8] != DLS_MAGIC:
        raise ValueError('not a .dls (no MINAATAD)')
    version, minor = struct.unpack_from('<II', data, 8)
    offsets = struct.unpack_from('<10I', data, 16)
    slen = struct.unpack_from('<I', data, offsets[7])[0]
    strings = data[offsets[7] + 4:offsets[7] + 4 + slen]
    count = struct.unpack_from('<I', data, offsets[0])[0]
    for i in range(count):
        record = struct.unpack_from('<I', data, offsets[0] + 4 + 8 * i)[0]
        fields = struct.unpack_from('<17I', data, record)
        name = strings[fields[0]:].split(b'\0', 1)[0].decode('latin-1')
        if name.lower() != 'driveline':
            continue
        keys = [struct.unpack_from('<IIf', data, fields[14] + 12 * k) for k in range(fields[6])]
        keys.sort(key=lambda k: k[2])
        return version, minor, [(MARKER_TAGS.get(tag, 'note 0x%x' % tag), dist, flags) for tag, flags, dist in keys]
    raise ValueError('no Driveline set')


def trk_driveline(data):
    """[(x, y, z, distance)] of the .trk driveline chunk (type 3, subtype 0x14):
    u32 count, then 32-byte points {3 f32 position (z up), 3 f32 tangent,
    f32 distance, u16, u16}. Chunks are framed as u32 header length, u32 type,
    u32 subtype, u32 size, payload; the framing is checked, and if it does
    not hold the file is searched for a run of points with rising distance."""
    pos = 0
    while pos + 16 <= len(data):
        hl, ctype, sub, size = struct.unpack_from('<4I', data, pos)
        start = pos + 16
        if not (hl in (8, 16) and start + size <= len(data)):
            break
        if (ctype, sub) == (3, 0x14):
            n = struct.unpack_from('<I', data, start)[0]
            return [struct.unpack_from('<3f12xf', data, start + 4 + 32 * i) for i in range(n)]
        pos = start + size
    # Fallback: the longest stride-32 run of finite points whose distance rises from about 0
    best = []
    for base in range(0, min(len(data) - 36, 1 << 22), 4):
        n = struct.unpack_from('<I', data, base)[0]
        if not 10 <= n <= 200000 or base + 4 + 32 * n > len(data):
            continue
        pts = [struct.unpack_from('<3f12xf', data, base + 4 + 32 * i) for i in range(min(n, 50))]
        if all(all(math.isfinite(c) for c in p) for p in pts) and abs(pts[0][3]) < 5 \
                and all(b[3] > a[3] for a, b in zip(pts, pts[1:])):
            best = [struct.unpack_from('<3f12xf', data, base + 4 + 32 * i) for i in range(n)]
            break
    return best


def stage_files(root, sid):
    """The .dls and .trk files of stage `sid` (one of each time-of-day letter M N E O S)."""
    e = stages(root).get(sid, {})
    base = e.get('track_name') or 'Maps\\track-%d' % sid
    folder, stem = os.path.split(base.replace('\\', '/'))
    path = resolve(root, folder)
    found = []
    if path:
        for name in sorted(os.listdir(path)):
            low = name.lower()
            if low.startswith(stem.lower() + '_') and low.endswith(('.dls', '.trk')):
                found.append(os.path.join(path, name))
    return e, found


def print_markers(root, sid):
    e, files = stage_files(root, sid)
    print('stage %d: %s, %s m, %s' % (sid, e.get('name'), e.get('length_m'), e.get('surface')))
    for path in files:
        with open(path, 'rb') as f:
            data = f.read()
        print('==', os.path.basename(path))
        try:
            if path.lower().endswith('.dls'):
                print_dls(data)
            else:
                print_trk(data)
        except (ValueError, struct.error) as err:
            print('  not read:', err)


def print_dls(data):
    version, minor, keys = dls_markers(data)
    print('  version %d.%d, %d keys' % (version, minor, len(keys)))
    lines = [(n, d) for n, d, _ in keys if n in ('start', 'split', 'finish', 'end')]
    for n, d in lines:
        print('  %-7s %10.2f m' % (n, d))
    start = next((d for n, d in lines if n == 'start'), None)
    finish = next((d for n, d in lines if n == 'finish'), None)
    if start is not None and finish is not None:
        print('  start to finish %.1f m; %d splits; %d pace notes' % (
            finish - start, sum(1 for n, _ in lines if n == 'split'), len(keys) - len(lines)))


def print_trk(data):
    pts = trk_driveline(data)
    if not pts:
        print('  no driveline found')
        return
    zs = [p[2] for p in pts]
    print('  %d points, distance %.1f..%.1f m, z %.1f..%.1f, first (%.1f, %.1f, %.1f)' % (
        len(pts), pts[0][3], pts[-1][3], min(zs), max(zs), *pts[0][:3]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('root', nargs='?')
    ap.add_argument('--telemetry', action='store_true')
    ap.add_argument('--stages', action='store_true')
    ap.add_argument('--csv')
    ap.add_argument('--cars', action='store_true')
    ap.add_argument('--car-now', action='store_true')
    ap.add_argument('--markers', type=int)
    ap.add_argument('--dls')
    ap.add_argument('--trk')
    a = ap.parse_args()
    if a.dls:
        with open(a.dls, 'rb') as f:
            print_dls(f.read())
        return
    if a.trk:
        with open(a.trk, 'rb') as f:
            print_trk(f.read())
        return
    if not a.root:
        ap.print_help()
        return
    if a.telemetry:
        telemetry(a.root)
    if a.stages:
        print_stages(a.root, a.csv)
    if a.cars or a.car_now:
        print_cars(a.root, now=a.car_now)
    if a.markers is not None:
        print_markers(a.root, a.markers)


if __name__ == '__main__':
    main()
