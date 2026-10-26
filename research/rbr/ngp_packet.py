#!/usr/bin/env python3
"""Richard Burns Rally (RallySimFans, NGP plugin) UDP telemetry: the packet
layout and a read-only dump tool (docs/richard-burns-rally.md).

The layout is the NGP plugin's rbr::telemetry::data::TelemetryData
(#pragma pack 1, little-endian, 664 bytes), as shipped in the NGP plugin
zip and copied verbatim by fixfactory/SliProSuperPro and
mika-n/RBRUDPTelemetryLogger. Nothing here imports Oversteer.

Usage:
    ngp_packet.py --layout                 print the offset table (markdown)
    ngp_packet.py FILE.bin                 decode one raw 664-byte packet
    ngp_packet.py --raw-dir DIR            decode every *.bin in DIR (one packet each)
    ngp_packet.py --capture FILE.ovcap.gz  summarise the 664-byte packets of an
                                           Oversteer capture (lifecycle: steps,
                                           stage, clock, progress, distances)

A capture is read with oversteer.telemetry_capture.read_capture (read-only).
"""

import argparse
import math
import os
import struct
import sys

SIZE = 664
WHEELS = ('LF', 'RF', 'LB', 'RB')                  # the packet's order: front left, front right, rear left, rear right


def _layout():
    """[(name, struct code)] in packet order."""
    fields = [('totalSteps', 'I'),
              ('stage.index', 'i'), ('stage.progress', 'f'), ('stage.raceTime', 'f'),
              ('stage.driveLineLocation', 'f'), ('stage.distanceToEnd', 'f'),
              ('control.steering', 'f'), ('control.throttle', 'f'), ('control.brake', 'f'),
              ('control.handbrake', 'f'), ('control.clutch', 'f'), ('control.gear', 'i'),
              ('control.footbrakePressure', 'f'), ('control.handbrakePressure', 'f'),
              ('car.index', 'i'), ('car.speed', 'f'),
              ('car.positionX', 'f'), ('car.positionY', 'f'), ('car.positionZ', 'f'),
              ('car.roll', 'f'), ('car.pitch', 'f'), ('car.yaw', 'f')]
    for block in ('velocities', 'accelerations'):
        fields += [('car.%s.%s' % (block, a), 'f') for a in ('surge', 'sway', 'heave', 'roll', 'pitch', 'yaw')]
    fields += [('car.engine.' + n, 'f') for n in ('rpm', 'radiatorCoolantTemperature', 'engineCoolantTemperature',
                                                  'engineTemperature')]
    for w in WHEELS:
        p = 'car.suspension%s.' % w
        fields += [(p + 'springDeflection', 'f'), (p + 'rollbarForce', 'f'), (p + 'springForce', 'f'),
                   (p + 'damperForce', 'f'), (p + 'strutForce', 'f'), (p + 'helperSpringIsActive', 'i'),
                   (p + 'damper.damage', 'f'), (p + 'damper.pistonVelocity', 'f'),
                   (p + 'wheel.brakeDisk.layerTemperature', 'f'), (p + 'wheel.brakeDisk.temperature', 'f'),
                   (p + 'wheel.brakeDisk.wear', 'f'),
                   (p + 'wheel.tire.pressure', 'f'), (p + 'wheel.tire.temperature', 'f'),
                   (p + 'wheel.tire.carcassTemperature', 'f'), (p + 'wheel.tire.treadTemperature', 'f'),
                   (p + 'wheel.tire.currentSegment', 'I')]
        for s in range(1, 9):
            fields += [(p + 'wheel.tire.segment%d.temperature' % s, 'f'), (p + 'wheel.tire.segment%d.wear' % s, 'f')]
    return fields


FIELDS = _layout()
FORMAT = '<' + ''.join(code for _, code in FIELDS)
OFFSETS = {}
_o = 0
for _name, _code in FIELDS:
    OFFSETS[_name] = _o
    _o += struct.calcsize('<' + _code)
assert struct.calcsize(FORMAT) == SIZE == _o, (struct.calcsize(FORMAT), _o)


def decode(data):
    """{name: value} of one packet, or None if it is not 664 bytes."""
    if len(data) != SIZE:
        return None
    return dict(zip((n for n, _ in FIELDS), struct.unpack(FORMAT, data)))


def print_layout():
    print('| offset | type | field |')
    print('|---|---|---|')
    for name, code in FIELDS:
        if '.segment' in name and not name.endswith('segment1.temperature'):
            continue
        label = name.replace('segment1.temperature', 'segment1..8 {temperature, wear}')
        print('| %d | %s | %s |' % (OFFSETS[name], {'f': 'f32', 'i': 's32', 'I': 'u32'}[code], label))


def _fmt(v):
    if isinstance(v, float):
        if not math.isfinite(v):
            return repr(v)
        return '%.6g' % v
    return str(v)


def dump(d):
    """One packet, the segment arrays folded to a line per wheel."""
    for name, _ in FIELDS:
        if '.segment' in name:
            continue
        print('%-50s %s' % (name, _fmt(d[name])))
    for w in WHEELS:
        p = 'car.suspension%s.wheel.tire.segment' % w
        temps = [d['%s%d.temperature' % (p, s)] for s in range(1, 9)]
        wear = [d['%s%d.wear' % (p, s)] for s in range(1, 9)]
        print('%s segments T: %s' % (w, ' '.join(_fmt(t) for t in temps)))
        print('%s segments wear: %s' % (w, ' '.join(_fmt(t) for t in wear)))
    # Derived checks the doc's "verify" items rely on
    pos = (d['car.positionX'], d['car.positionY'], d['car.positionZ'])
    print('-- derived --')
    print('drive line + distance to end (m?)       %s' % _fmt(d['stage.driveLineLocation'] + d['stage.distanceToEnd']))
    print('|velocities.surge,sway,heave| (m/s?)    %s' % _fmt(math.sqrt(sum(d['car.velocities.' + a] ** 2
                                                                          for a in ('surge', 'sway', 'heave')))))
    print('speed / 3.6                             %s' % _fmt(d['car.speed'] / 3.6))
    print('position                                %s' % ', '.join(_fmt(c) for c in pos))


def _capture_packets(path):
    """(t, bytes) of a capture, through the repo's own reader."""
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, '..', '..'))
    from oversteer import telemetry_capture                     # noqa: E402 (repo module, read-only use)
    _meta, records = telemetry_capture.read_capture(path)
    for t, _source, data in records:
        yield t, data


def summarise(packets):
    """The lifecycle a decoder must learn: every change of the stage, car,
    totalSteps going back, the clock starting/stopping, gaps; and the
    min/max of each field over the 664-byte packets."""
    last, last_t, n = None, None, 0
    lo, hi = {}, {}
    for t, data in packets:
        d = decode(data)
        if d is None:
            continue
        n += 1
        for k, v in d.items():
            if isinstance(v, float) and not math.isfinite(v):
                continue
            lo[k] = min(lo.get(k, v), v)
            hi[k] = max(hi.get(k, v), v)
        notes = []
        if last is None:
            notes.append('first')
        else:
            if d['stage.index'] != last['stage.index']:
                notes.append('stage %d->%d' % (last['stage.index'], d['stage.index']))
            if d['car.index'] != last['car.index']:
                notes.append('car %d->%d' % (last['car.index'], d['car.index']))
            if d['totalSteps'] < last['totalSteps']:
                notes.append('steps back %d->%d' % (last['totalSteps'], d['totalSteps']))
            if (last['stage.raceTime'] == 0) != (d['stage.raceTime'] == 0):
                notes.append('clock %s' % ('starts' if last['stage.raceTime'] == 0 else 'to 0'))
            if d['stage.raceTime'] == last['stage.raceTime'] and d['totalSteps'] != last['totalSteps'] \
                    and d['car.speed'] > 5:
                notes.append('clock still, moving')
            if last_t is not None and t - last_t > 0.5:
                notes.append('gap %.1f s' % (t - last_t))
        if notes:
            print('%10.3f steps=%d stage=%d car=%d clock=%.3f progress=%.4f drive=%.1f to_end=%.1f speed=%.1f  %s' % (
                t, d['totalSteps'], d['stage.index'], d['car.index'], d['stage.raceTime'], d['stage.progress'],
                d['stage.driveLineLocation'], d['stage.distanceToEnd'], d['car.speed'], '; '.join(notes)))
        last, last_t = d, t
    print('%d packets of %d bytes' % (n, SIZE))
    for name, _ in FIELDS:
        if name in lo and '.segment' not in name:
            print('%-50s %12s %12s' % (name, _fmt(lo[name]), _fmt(hi[name])))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('file', nargs='?')
    ap.add_argument('--layout', action='store_true')
    ap.add_argument('--raw-dir')
    ap.add_argument('--capture')
    a = ap.parse_args()
    if a.layout:
        print_layout()
    elif a.capture:
        summarise(_capture_packets(a.capture))
    elif a.raw_dir:
        for name in sorted(os.listdir(a.raw_dir)):
            if name.endswith('.bin'):
                print('==', name)
                d = decode(open(os.path.join(a.raw_dir, name), 'rb').read())
                dump(d) if d else print('not %d bytes' % SIZE)
    elif a.file:
        d = decode(open(a.file, 'rb').read())
        if d is None:
            sys.exit('not a %d-byte packet' % SIZE)
        dump(d)
    else:
        ap.print_help()


if __name__ == '__main__':
    main()
