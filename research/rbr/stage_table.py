#!/usr/bin/env python3
"""Build data/telemetry/stages/rbr.json (Richard Burns Rally stage names) from an RSF install, once, read-only.

    python3 research/rbr/stage_table.py ROOT > data/telemetry/stages/rbr.json

ROOT is the folder holding RichardBurnsRally.ini (marth's: /mnt/gaming/RBR/RBR). The packet's `stage.index` is the map
id; the table maps it to the facts RallySimFans' own stage list holds (rsfdata/cache/stages_data.json, and
Maps/Tracks.ini for what it lacks): name, length, surface and its shares, country. Only those facts are kept (no
author, no files). Stage 41 is every Beyond the Box stage: it names no stage and is left out. The same reader runs
in the app on an install it finds (oversteer/rbr_install.py), so a custom stage the table lacks is named there.

Each stage the install has a .dls for also gets `sector_lines_m`: [start, split..., finish] in metres along the
stage, the Driveline markers of Maps/**/track-<id>_<L>.dls (research/rbr/rsf_install.py dls_markers). That is the
distance domain of the packet's driveLineLocation (`lap_distance`): the stage clock stops at the finish marker.
The five time-of-day variants of a stage carry the same markers (checked here, a disagreement is reported)."""
import collections
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from oversteer import rbr_install  # noqa: E402 (repo module, read-only use of the install)
import rsf_install  # noqa: E402 (this folder: the .dls reader)

LINES_SOURCE = "the stage's .dls Driveline markers (start, split, finish)"


def dls_files(root):
    """{stage id: {time-of-day letter: path}} of the install's Maps/**/track-<id>_<L>.dls."""
    found = collections.defaultdict(dict)
    for folder, _, names in os.walk(os.path.join(root, 'Maps')):
        for name in names:
            m = re.match(r'track-(\d+)_(\w)\.dls$', name, re.I)
            if m:
                found[int(m.group(1))][m.group(2).upper()] = os.path.join(folder, name)
    return found


def sector_lines(files):
    """(lines, confidence) of one stage from its .dls variants, or None: [start, split..., finish] when the files
    hold a finish and at least one split in rising order. A .dls with no start marker puts the start at 0 m
    ('medium'). Variants that disagree are reported on stderr and the first in sorted letter order (E, M, N, O, S) is used."""
    read = {}
    for letter, path in sorted(files.items()):
        try:
            with open(path, 'rb') as f:
                keys = rsf_install.dls_markers(f.read())[2]
        except (OSError, ValueError, rsf_install.struct.error):
            continue
        read[letter] = tuple((n, round(d, 1)) for n, d, _ in keys if n in ('start', 'split', 'finish'))
    if len(set(read.values())) > 1:
        print('sector lines: the variants of %s disagree: %s' % (sorted(files.values())[0], read), file=sys.stderr)
    for letter in sorted(read):
        markers = read[letter]
        start = [d for n, d in markers if n == 'start']
        splits = [d for n, d in markers if n == 'split']
        finish = [d for n, d in markers if n == 'finish']
        if len(finish) != 1 or not splits or len(start) > 1:
            continue
        lines = [start[0] if start else 0.0] + splits + finish
        if any(b <= a for a, b in zip(lines, lines[1:])):
            continue
        return lines, 'high' if start else 'medium'
    return None


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    root = sys.argv[1]
    stages = [dict({k: v for k, v in e.items() if k not in ('author', 'sources')}, confidence='high')
              for e in rbr_install.build_stages(root) if e['id'] != 41]
    files = dls_files(root)
    for e in stages:
        found = sector_lines(files[e['id']]) if e['id'] in files else None
        if found:
            e['sector_lines_m'], e['sector_lines_confidence'] = found
            e['sector_lines_source'] = LINES_SOURCE
    out = {
        'game': 'rbr',
        'title': 'Richard Burns Rally (RallySimFans) stage names',
        'keys': "The stage key is rbr:<map id>, the id the NGP telemetry sends as stage.index. A stage not listed (a custom "
                "one added later) is shown as 'RBR stage <id>' with the length the packet gives, or named from the "
                "player's own install where Oversteer finds it.",
        'notes': "Facts only (id, name, length in m, surface and its shares, country code), read once from a "
                 "RallySimFans install: rsfdata/cache/stages_data.json and Maps/Tracks.ini, %s; built by "
                 "research/rbr/stage_table.py. Lengths are RSF's, mostly rounded to 100 m; the packet's driveline "
                 "length is the exact one. sector_lines_m: the stage's .dls start, split and finish markers in m "
                 "along the driveline (the packet's driveLineLocation)." % datetime.date.today().isoformat(),
        'stages': stages,
    }
    json.dump(out, sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
