"""Replay marth's DiRT Rally 2.0 captures (read-only) through the current code into a scratch database.

    python3 research/dr2/replay.py OUT.db [CAPTURE ...]

With no captures, every capture in the Flatpak's captures folder holding 264-byte packets. Writes only OUT.db (keep
it under ~/.cache/oversteer-dr2/, never a live database).
"""
import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from oversteer.telemetry_capture import read_capture, replay  # noqa: E402
from oversteer.shift_learner import ShiftLearner  # noqa: E402

FOLDER = '~/.var/app/io.github.berarma.Oversteer/data/oversteer/captures'


def has_dr2(path):
    _meta, records = read_capture(path)
    for n, (_t, _addr, data) in enumerate(records):
        if len(data) == 264:
            return True
        if n > 2000:
            return False
    return False


def main(db, paths):
    for f in (db, db + '-wal', db + '-shm'):
        if os.path.exists(f):
            os.remove(f)
    if not paths:
        paths = sorted(p for p in glob.glob(os.path.join(os.path.expanduser(FOLDER), '*.ovcap.gz')) if has_dr2(p))
    for path in paths:
        t0 = time.time()
        meta, records = read_capture(path)
        learner = ShiftLearner(db, profile='DR2')
        replay(records, learner, started=meta.get('started'))
        learner.close()
        print(os.path.basename(path), '%.0f s' % (time.time() - t0), flush=True)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2:])
