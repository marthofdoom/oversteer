"""Issue hunt 2026-10: how often ACR's stage clock (bridge v4) updates while the car moves, per capture.

    python3 research/issue-hunt/clock_cadence.py

A clock that only steps once a second (seen on 2026-10-22 Cwmbiga) stands still for ~1 s between steps, which
drive_log's CLOCK_STOPPED (1.0 s) reads as the finish once within FINISH_CLOCK_BEFORE of the line.
"""
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from oversteer.telemetry_capture import read_capture  # noqa: E402
from oversteer.telemetry_formats import decode_sample  # noqa: E402

FOLDERS = ('~/.local/share/oversteer/captures', '~/.var/app/io.github.berarma.Oversteer/data/oversteer/captures')

for path in sorted((p for d in FOLDERS for p in glob.glob(os.path.join(os.path.expanduser(d), '*.ovcap.gz'))),
                   key=os.path.basename):
    _m, rec = read_capture(path)
    last = changed = None
    steps = []
    game = None
    for t, _a, data in rec:
        s = decode_sample(data)
        if s is None or s.stage_time is None:
            continue
        game = s.game
        if s.stage_time != last:
            if changed is not None and (s.speed or 0) > 5 and 0 < s.stage_time - last < 5:
                steps.append(t - changed)
            changed, last = t, s.stage_time
    if game != 'acr' or not steps:
        continue
    steps.sort()
    print(os.path.basename(path), 'clock steps %d, median %.3f s, p90 %.3f s, share >= 0.9 s %.2f' % (
        len(steps), steps[len(steps) // 2], steps[int(len(steps) * 0.9)], sum(1 for x in steps if x >= 0.9) / len(steps)))
