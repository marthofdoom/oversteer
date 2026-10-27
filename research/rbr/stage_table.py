#!/usr/bin/env python3
"""Build data/telemetry/stages/rbr.json (Richard Burns Rally stage names) from an RSF install, once, read-only.

    python3 research/rbr/stage_table.py ROOT > data/telemetry/stages/rbr.json

ROOT is the folder holding RichardBurnsRally.ini (marth's: /mnt/gaming/RBR/RBR). The packet's `stage.index` is the map
id; the table maps it to the facts RallySimFans' own stage list holds (rsfdata/cache/stages_data.json, and
Maps/Tracks.ini for what it lacks): name, length, surface and its shares, country. Only those facts are kept (no
author, no files). Stage 41 is every Beyond the Box stage: it names no stage and is left out. The same reader runs
in the app on an install it finds (oversteer/rbr_install.py), so a custom stage the table lacks is named there."""
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from oversteer import rbr_install  # noqa: E402 (repo module, read-only use of the install)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    root = sys.argv[1]
    stages = [dict({k: v for k, v in e.items() if k not in ('author', 'sources')}, confidence='high')
              for e in rbr_install.build_stages(root) if e['id'] != 41]
    out = {
        'game': 'rbr',
        'title': 'Richard Burns Rally (RallySimFans) stage names',
        'keys': "The stage key is rbr:<map id>, the id the NGP telemetry sends as stage.index. A stage not listed (a custom "
                "one added later) is shown as 'RBR stage <id>' with the length the packet gives, or named from the "
                "player's own install where Oversteer finds it.",
        'notes': "Facts only (id, name, length in m, surface and its shares, country code), read once from a "
                 "RallySimFans install: rsfdata/cache/stages_data.json and Maps/Tracks.ini, %s; built by "
                 "research/rbr/stage_table.py. Lengths are RSF's, mostly rounded to 100 m; the packet's driveline "
                 "length is the exact one." % datetime.date.today().isoformat(),
        'stages': stages,
    }
    json.dump(out, sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
