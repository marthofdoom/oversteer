"""Replay marth's captures (read-only) through the current code into a scratch store, for validate.py.

    python3 research/diagnosis/replay_captures.py OUT.db

Reads ~/.local/share/oversteer/captures and the Flatpak's captures folder, oldest first; writes only OUT.db (never
a live database). Put OUT.db under a scratch directory, not the repository.
"""

import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from oversteer.telemetry_capture import read_capture, replay  # noqa: E402
from oversteer.shift_learner import ShiftLearner  # noqa: E402

FOLDERS = ('~/.local/share/oversteer/captures', '~/.var/app/io.github.berarma.Oversteer/data/oversteer/captures')


def main(db):
    for f in (db, db + '-wal', db + '-shm'):
        if os.path.exists(f):
            os.remove(f)
    files = sorted((p for d in FOLDERS for p in glob.glob(os.path.join(os.path.expanduser(d), '*.ovcap.gz'))),
                   key=os.path.basename)
    for path in files:
        t0 = time.time()
        meta, records = read_capture(path)
        learner = ShiftLearner(db, profile='ACR')
        replay(records, learner, started=meta.get('started'))
        learner.close()
        print(os.path.basename(path), '%.0f s' % (time.time() - t0), flush=True)


if __name__ == '__main__':
    main(sys.argv[1])
