"""Repairs of the stored telemetry data that older code worked out wrong (data version 1, Oversteer 0.16).

Since 0.15.4 the runs are analysed differently: the finish and its time (ACR's coarse clock, DiRT's and WRC
Generations' official time, no post-finish phantom runs), the PB (any finished run, offs too), DiRT's plan (it was
mirrored), DiRT's stage keys (by the length the game sends) and car names, and the counter-steer (the steer read a
lag before the yaw). What was stored before is stale. Two parts mend it:

- `migrate(store)`: in the app, on the drive-log thread, once (gated by the store's data_version), first thing after
  the store is opened. A backup of the file is taken first, through SQLite (`<db>.pre-0.16-v<from>-v<to>-<date-time>.bak`,
  kept; one per repair, recorded in store_meta so a repair that crashed part way reuses its own). Then,
  in one transaction: the DiRT phantom runs go, the DiRT stages are matched again and their runs moved to the new
  key, the DiRT cars get their shipped names, every stored potential and envelope goes (potentials_missing and the
  backfill build them again), and every run with a trace is queued to be worked over again (corners, events,
  metrics but the launch's, class) in the `reanalyse` queue: the reference runs first, oldest first. A queued run
  keeps its class until it is rewritten, so no PB leaves the PB lookups meanwhile, and no trace is pruned while the
  queue holds runs (Store.prune_traces). A crash before the commit leaves the file as it was, to be repaired on the
  next start; the queue is worked a few runs at a time from drive_log.backfill_step and survives a restart.

- `retime_from_captures(path)`: the result times. A trace does not hold the finish exactly (DiRT's traces end up to
  0.1 s before the official time or run on 30 s past it; ACR's run 170 stops 360 m before the line), so the times
  are taken from the captures, which hold every packet: each capture that holds a finished run is replayed through
  the current code (telemetry_capture.replay, the live path) into a scratch store, on the live file's learnt finish
  lines, and a stored run that the replay started at the same moment takes the replay's result time, course and
  finish rows (run_clock, run_stop, run_start, run_finish), and its trace where the stored one stops short of the
  replay's finish; every matched run gets the replay's game-clock row (run_clock), whatever its time, so the ranking
  (Reader.ON_CLOCK) never leaves one out. A run the replay timed at another finish line is left alone. It writes the
  live file, so it is a command for when the app is closed (it takes the file's exclusive lock and refuses
  otherwise), after a backup (`<db>.pre-retime-<date-time>.bak`); a dry run only reads. A run it cannot match keeps
  its time and is listed.
"""

import argparse
import glob
import json
import logging
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.parse

from . import car_data, coach_context, stage_tables
from .telemetry_store import (CODEMASTERS_NAME, DATA_VERSION, REANALYSE_DDL, RUN_CLOCK_DDL, RUN_FINISH_DDL,
                              RUN_START_DDL, RUN_STOP_DDL, STAGE_SENT_TOLERANCE, STORE_META_DDL, TRACE_CHANNELS,
                              VERSION, Store, _backup, unpack_trace)

T = {name: i for i, name in enumerate(TRACE_CHANNELS)}

REPAIR = 'repair-0.16'          # store_meta: what migrate() did (JSON)
RETIMED = 'retime-0.16'         # store_meta: the runs retime_from_captures() re-timed, {run: [old, new, capture]}
BACKUP_TAG = 'pre-0.16'
RETIME_BACKUP_TAG = 'pre-retime'

# A DiRT run that began where the finished run before it in the session ended (the game resets its clock on the
# results screen, the old run tracker took that for a restart and a new run): provably no attempt at a stage
PHANTOM_DISTANCE = 200.0        # m: longer is a real run
PHANTOM_NEAR = 25.0             # m (x, z) between the run's first trace row and the last of the finished run before it

# A stored trace this much short of where the replay's finish is (in driven distance) stops before the finish
TRACE_SHORT = 50.0              # m
MATCH_STARTED = 1.0             # s between a stored run's start and the replay's: the same run
# s: a smaller change of the result is the same time (an ACR run re-timed to the flying finish from its 10 Hz trace
# replays 0.01-0.03 s apart from it): left as it is, course, finish rows and all
RETIME_MIN = 0.05
STOP_SHORT = 25.0               # m: an ACR clock stop this far before the stage's finish line was no finish


# -- the version-gated migration (in the app) --

BACKUP_META = 'repair_backup'   # store_meta: the backup taken for the repair under way (a crash part way reuses it)


def _key_parts(key):
    """(length, start z or None) of a DiRT key 'dirt:<length>:<z>', or None."""
    parts = key.split(':')
    try:
        return float(parts[1]), (float(parts[2]) if len(parts) > 2 else None)
    except (IndexError, ValueError):
        return None


def _trace(store, run):
    try:
        return store.trace(run)
    except Exception:
        logging.exception("data repair: can't read the trace of run %s", run)
        return None


def _stage_length(store, key):
    row = store.stage(key)
    if row and row.get('length'):
        return row['length']
    parts = _key_parts(key)
    return parts[0] if parts else None


def phantom_runs(store):
    """[(run, the finished run it followed)] of the DiRT runs that are post-finish phantoms: unfinished (finished
    NULL or 0), under PHANTOM_DISTANCE driven and in the trace, never of a class that can be a PB, unlabelled, the
    next run of its session after a finished run on a stage of the same length (the length DiRT sends), and its trace
    beginning within PHANTOM_NEAR of where that run's trace ended: at the finish. Anything less provable stays."""
    found = []
    rows = store._do(
        "SELECT r.id, r.session, r.started, r.distance, r.stage, r.run_class FROM runs r "
        "JOIN sessions s ON s.id = r.session JOIN cars c ON c.id = s.car WHERE c.game = 'dirt' "
        "AND r.stage LIKE 'dirt:%' AND (r.finished IS NULL OR r.finished = 0) AND r.distance IS NOT NULL "
        "AND r.distance < ? AND r.ended IS NOT NULL AND r.id NOT IN (SELECT run FROM labels) "
        "ORDER BY r.id", (PHANTOM_DISTANCE,)).fetchall()
    for run, session, started, distance, stage, klass in rows:
        if klass in coach_context.PB_CLASSES:
            continue
        before = store._do('SELECT id, finished, stage FROM runs WHERE session = ? AND (started < ? OR '
                           '(started = ? AND id < ?)) ORDER BY started DESC, id DESC LIMIT 1',
                           (session, started, started, run)).fetchone()
        if before is None or before[1] != 1 or not before[2] or not before[2].startswith('dirt:'):
            continue
        mine, theirs = _stage_length(store, stage), _stage_length(store, before[2])
        if mine is None or theirs is None or abs(mine - theirs) > STAGE_SENT_TOLERANCE:
            continue
        trace, finished = _trace(store, run), _trace(store, before[0])
        if not trace or not finished:
            continue
        if max(row[T['distance']] for row in trace) >= PHANTOM_DISTANCE:
            continue
        a, b = trace[0], finished[-1]
        gap = math.hypot(a[T['x']] - b[T['x']], a[T['z']] - b[T['z']])
        if math.isfinite(gap) and gap <= PHANTOM_NEAR:
            found.append((run, before[0]))
    return found


def delete_phantoms(store):
    """Delete the phantom runs (phantom_runs()); their traces, corners and rows in other tables go with them. Returns
    the ids deleted."""
    gone = []
    for run, after in phantom_runs(store):
        store.drop_run(run)
        gone.append(run)
        logging.info("data repair: run %s deleted: a DiRT phantom that began at the finish of run %s", run, after)
    return gone


def _drop_stage_data(store, key):
    """Forget what is stored per stage (its potentials, its lines' fingerprint, the coach's memory of its places)."""
    store.drop_stage_potentials(key)
    store._do('DELETE FROM stage_lines WHERE stage = ?', (key,))
    store._do("DELETE FROM coach_state WHERE tip LIKE ? ESCAPE '\\' OR tip LIKE ? ESCAPE '\\'",
              ('%:' + _like(key), '%:' + _like(key) + ':%'))


def _like(text):
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def _recount(store, key):
    store._do('UPDATE stages SET runs = (SELECT COUNT(*) FROM runs WHERE stage = ? AND ended IS NOT NULL) '
              'WHERE key = ?', (key, key))


def rekey_dirt_stages(store):
    """Match every stored DiRT stage key again (Store.match_stage on its stored length and the key's start z): a key
    that is now a shipped stage's other key has its runs and sessions moved to that one, the potentials and
    per-stage data of both dropped, and the old row deleted. Then the DiRT stage rows outside the table that no run
    or session refers to go. Returns ({old: new}, [deleted keys])."""
    moved = {}
    for key, length in store._do("SELECT key, length FROM stages WHERE game = 'dirt' AND key LIKE 'dirt:%'").fetchall():
        if stage_tables.entry(key) is not None:
            continue                                     # a table key stays what it is
        parts = _key_parts(key)
        if parts is None:
            continue
        length = length or parts[0]
        if not store._do('SELECT 1 FROM runs WHERE stage = ? UNION SELECT 1 FROM sessions WHERE stage = ?',
                         (key, key)).fetchone():
            continue                                     # nothing on it: it goes with the orphans below
        new = store.match_stage('dirt', length, parts[1])
        if new == key or stage_tables.entry(new) is None:
            continue
        entry = stage_tables.entry(new)
        _, prior = stage_tables.surface_of(entry)
        store.upsert_stage(new, 'dirt', entry.get('length_m'), entry.get('stage'), entry.get('location'), prior,
                           'table' if prior else None)
        store._do('UPDATE runs SET stage = ? WHERE stage = ?', (new, key))
        store._do('UPDATE sessions SET stage = ? WHERE stage = ?', (new, key))
        _drop_stage_data(store, key)
        _drop_stage_data(store, new)
        _recount(store, new)
        moved[key] = new
        logging.info("data repair: DiRT stage %s is %s (%s)", key, new, entry.get('stage'))
    deleted = []
    for (key,) in store._do("SELECT key FROM stages WHERE game = 'dirt' AND key LIKE 'dirt:%'").fetchall():
        if stage_tables.entry(key) is not None:
            continue
        if store._do('SELECT 1 FROM runs WHERE stage = ? UNION SELECT 1 FROM sessions WHERE stage = ?',
                     (key, key)).fetchone():
            continue
        _drop_stage_data(store, key)
        store._do('DELETE FROM stages WHERE key = ?', (key,))
        deleted.append(key)
    if deleted:
        logging.info("data repair: DiRT stage rows no run is on deleted: %s", ', '.join(deleted))
    return moved, deleted


def rename_dirt_cars(store):
    """DiRT cars named by the decoder ('N rpm, N gears', or no name) and not by the user get their shipped name
    (car_data.display_name). Returns {car id: (old, new)}."""
    renamed = {}
    for car, key, name, model in store._do(
            "SELECT id, key, name, model FROM cars WHERE game = 'dirt' AND NOT user_named").fetchall():
        if name and not CODEMASTERS_NAME.match(name):
            continue
        shipped = car_data.display_name(key)
        if not shipped or shipped == name:
            continue
        try:
            data = json.loads(model)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict) and 'name' in data:
            data['name'] = shipped
            store._do('UPDATE cars SET name = ?, model = ? WHERE id = ?', (shipped, json.dumps(data), car))
        else:
            store._do('UPDATE cars SET name = ? WHERE id = ?', (shipped, car))
        renamed[car] = (name, shipped)
        logging.info("data repair: car %s (%s) is %s", car, key, shipped)
    return renamed


def drop_potentials(store):
    """Every stored potential and envelope: (potentials, envelopes) dropped. The algorithm (potential.ALGO) is only
    hashed into their data_version, so an older one cannot be told from a row; every row an older build stored is
    from before ALGO 7, and the runs they are built from are worked over again anyway. potential.after_run (from the
    backfill) and potentials_missing build them again."""
    potentials = store._do('DELETE FROM stage_potential').rowcount
    envelopes = store._do('DELETE FROM envelopes').rowcount
    return potentials, envelopes


def queue_order(store, runs=None):
    """The runs with a trace (all, or those in `runs`) in the order to work them over: first the reference runs (each
    finished run of a class that ranks quicker than every earlier one of its car on its stage, overall or in its
    wetness: the runs coach_context.reference_run can pick, the PBs and the PB history among them), oldest first, so
    a run's reference has its new corners when the run is compared with it; then the rest, oldest first."""
    rows = store._do('SELECT r.id, r.stage, s.car, r.wet, r.finished, r.result_time, r.run_class FROM runs r '
                     'JOIN sessions s ON s.id = r.session WHERE r.ended IS NOT NULL AND r.id IN (SELECT run FROM traces) '
                     'ORDER BY r.started, r.id').fetchall()
    wanted = None if runs is None else set(runs)
    best, best_wet = {}, {}
    first, rest = [], []
    for run, stage, car, wet, finished, result, klass in rows:
        reference = False
        if finished == 1 and result is not None and stage and klass in coach_context.PB_CLASSES:
            if result < best.get((stage, car), math.inf):
                best[(stage, car)] = result
                reference = True
            if result < best_wet.get((stage, car, wet), math.inf):
                best_wet[(stage, car, wet)] = result
                reference = True
        if wanted is not None and run not in wanted:
            continue
        (first if reference else rest).append(run)
    return first + rest


def requeue(store, runs=()):
    """The reanalyse queue in queue_order(), with `runs` added to what is queued. Returns its length."""
    queued = {r[0] for r in store._do('SELECT run FROM reanalyse').fetchall()} | set(runs)
    order = queue_order(store, queued)
    store._do('DELETE FROM reanalyse')
    store.queue_reanalysis(order)
    return len(order)


def suspect_times(store):
    """[(run, stage, result_time, why)] of the finished runs whose time is likely the old finish rule's and that no
    re-timing from the captures has touched: DiRT and WRC Generations (read at 99 % of the distance), and ACR runs on
    the game's clock whose clock stop is well before the stage's finish line (a coarse clock taken for a stopped
    one)."""
    retimed = _retimed(store)
    out = []
    for run, stage, result, game in store._do(
            "SELECT r.id, r.stage, r.result_time, c.game FROM runs r JOIN sessions s ON s.id = r.session "
            "JOIN cars c ON c.id = s.car WHERE r.finished = 1 AND c.game IN ('dirt', 'wrcg') "
            "AND r.id NOT IN (SELECT run FROM run_clock) ORDER BY r.id").fetchall():     # DiRT's official time: on it
        if str(run) not in retimed:
            out.append((run, stage, result, '{} time read at 99 % of the distance'.format(game)))
    for run, stage, result, stop in store._do(
            "SELECT r.id, r.stage, r.result_time, p.stop_m FROM runs r JOIN run_stop p ON p.run = r.id "
            "WHERE r.finished = 1 AND r.stage LIKE 'acr:%' ORDER BY r.id").fetchall():
        line = (stage_tables.entry(stage) or {}).get('finish_m')
        if line and stop < line - STOP_SHORT and str(run) not in retimed:
            out.append((run, stage, result, 'the game clock stood still {:.0f} m before the line'.format(line - stop)))
    return out


def _retimed(store):
    try:
        return json.loads(store.meta(RETIMED) or '{}')
    except ValueError:
        return {}


def migrate(store):
    """Bring the stored data to DATA_VERSION (see the module's docstring), once: nothing when the store says it is
    there. The drive-log thread runs it first after the store is opened (DriveLog). Returns the summary dict, or None
    when nothing was done (already done, or the backup or the repair failed: tried again on the next start)."""
    version = store.data_version()
    if version >= DATA_VERSION:
        return None
    path = store.path
    dest = store.meta(BACKUP_META)
    if path and not (dest and os.path.exists(dest)):
        # One backup per repair (per transition and time: a file repaired, then merged with an older one, is
        # repaired again after a backup of its own); recorded, so a repair that crashed part way reuses its own
        store.commit()
        dest = '{}.{}-v{}-v{}-{}.bak'.format(path, BACKUP_TAG, version, DATA_VERSION, time.strftime('%Y%m%d-%H%M%S'))
        try:
            _backup(store.db, dest)
        except (OSError, sqlite3.Error) as e:
            logging.warning("data repair: no backup could be taken (%s): the data repair waits for the next start", e)
            try:
                os.remove(dest)
            except OSError:
                pass
            return None
        store.begin()
        store.set_meta(BACKUP_META, dest)
        store.commit()
        logging.info("data repair: the database was backed up to %s", dest)
    started = time.monotonic()
    store.begin()
    store.savepoint('data_repair')
    try:
        runs_before = store._do('SELECT COUNT(*) FROM runs').fetchone()[0]
        phantoms = delete_phantoms(store)
        moved, deleted = rekey_dirt_stages(store)
        cars = rename_dirt_cars(store)
        potentials, envelopes = drop_potentials(store)
        store._do('DELETE FROM reanalyse')
        order = queue_order(store)
        store.queue_reanalysis(order)
        suspects = suspect_times(store)
        summary = {'at': time.time(), 'runs_before': runs_before, 'phantoms_deleted': phantoms,
                   'stages_moved': moved, 'stages_deleted': deleted,
                   'cars_renamed': {str(k): v for k, v in cars.items()}, 'potentials_dropped': potentials,
                   'envelopes_dropped': envelopes, 'queued': len(order),
                   'times_not_fixed': [list(s) for s in suspects], 'backup': dest}
        store.set_meta(REPAIR, json.dumps(summary))
        store.set_meta(BACKUP_META, None)
        store.set_meta('data_version', DATA_VERSION)
        store.release_savepoint('data_repair')
    except Exception:
        logging.exception("data repair: failed, nothing was changed; tried again on the next start")
        store.rollback_savepoint('data_repair')
        return None
    store.commit()
    logging.info("data repair to data version %d done in %.1f s: %d phantom DiRT runs deleted (%s), %d DiRT stages "
                 "moved (%s), %d stage rows deleted, %d cars renamed, %d potentials and %d envelopes dropped, %d runs "
                 "queued to be worked over again", DATA_VERSION, time.monotonic() - started, len(phantoms),
                 ', '.join(map(str, phantoms)) or '-', len(moved),
                 ', '.join('{} -> {}'.format(a, b) for a, b in moved.items()) or '-', len(deleted), len(cars),
                 potentials, envelopes, len(order))
    for run, stage, result, why in suspects:
        logging.warning("data repair: run %s on %s keeps its time %s (%s): run 'oversteer --retime-from-captures' "
                        "(Flatpak: 'flatpak run io.github.berarma.Oversteer --retime-from-captures') with Oversteer closed "
                        "to take it from the capture", run, stage,
                        'none' if result is None else '{:.3f} s'.format(result), why)
    return summary


# -- re-timing from the captures (a command, with the app closed) --

class Locked(Exception):
    """The database is open elsewhere (Oversteer is running)."""


def _open_exclusive(path):
    """The live file's writer for a command: its exclusive lock taken and held until closed, so it fails at once when
    Oversteer (or anything) has the file open, and nothing opens it meanwhile."""
    db = sqlite3.connect(path, isolation_level=None, timeout=0.5)
    try:
        db.execute('PRAGMA locking_mode = EXCLUSIVE')
        db.execute('BEGIN EXCLUSIVE')
        db.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()
        db.execute('COMMIT')
    except sqlite3.OperationalError as e:
        db.close()
        raise Locked(str(e))
    db.execute('PRAGMA foreign_keys = ON')
    _check_version(db, path)
    for ddl in (RUN_FINISH_DDL, RUN_START_DDL, RUN_CLOCK_DDL, RUN_STOP_DDL, STORE_META_DDL, REANALYSE_DDL):
        db.execute(ddl)                                  # as open_store makes them (a file this build never opened)
    return db


def _open_read_only(path):
    """The file for a dry run: read only, no lock and no table made (it may be open in the app: WAL lets it read)."""
    db = sqlite3.connect('file:{}?mode=ro'.format(urllib.parse.quote(path)), uri=True, isolation_level=None,
                         timeout=5.0)
    _check_version(db, path)
    return db


def _check_version(db, path):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version != VERSION:
        db.close()
        raise sqlite3.DatabaseError('{} is at schema version {}, not {}: open it in Oversteer once first'.format(
            path, version, VERSION))


def _row(db, table, run):
    """The value of a run's row in one of FINISH_TABLES, as a 1-tuple, or None (no row, or no such table)."""
    try:
        return db.execute('SELECT {} FROM {} WHERE run = ?'.format(FINISH_COLUMNS[table], table), (run,)).fetchone()
    except sqlite3.OperationalError:
        return None


def capture_files(folders):
    """[(path, started, ended, bytes)] of the captures in `folders`, oldest first: started from the file's header,
    ended its last change."""
    from .telemetry_capture import read_capture
    out = []
    for folder in folders:
        for path in sorted(glob.glob(os.path.join(os.path.expanduser(folder), '*.ovcap.gz'))):
            try:
                meta, records = read_capture(path)
                records.close()
            except (ValueError, OSError):
                continue
            started = meta.get('started')
            if started is None:
                continue
            out.append((path, float(started), os.path.getmtime(path), os.path.getsize(path)))
    return sorted(out, key=lambda c: c[1])


FINISH_TABLES = ('run_clock', 'run_stop', 'run_start', 'run_finish')
FINISH_COLUMNS = {'run_clock': 'clock', 'run_stop': 'stop_m', 'run_start': 'start_m', 'run_finish': 'finish_m'}
REPLAY_RATE = 1.0e6             # bytes of capture (gzip) replayed a second, about (marth's 200 MB: 3.5 min)
LINE_MOVED = 0.5                # m: a replay timed at a finish line this far from the stored run's is another line


def _replay_runs(path, scratch, learnt=None):
    """The runs the current code makes of a capture: [dict of started, finished, result_time, course, game, distance,
    trace (rows), and per FINISH_TABLES its value or None (no row)], from a replay into the scratch file. `learnt`:
    the live file's learnt finish lines (Store.learn_finishes), which the replay runs on as the app would; the
    scratch store's own (from the replayed runs alone) are not used."""
    from .shift_learner import ShiftLearner
    from .telemetry_capture import read_capture, replay
    for f in (scratch, scratch + '-wal', scratch + '-shm'):
        if os.path.exists(f):
            os.remove(f)
    meta, records = read_capture(path)
    learner = ShiftLearner(scratch)
    lines = dict(learnt or {})

    def live_lines():
        stage_tables.set_learnt(lines)
        return lines
    learner.log.store.learn_finishes = live_lines        # a replayed clock stop must not move the live lines
    live_lines()
    try:
        replay(records, learner, started=meta.get('started'), recorded_car=meta.get('rbr_car'))
    finally:
        learner.close()
    db = sqlite3.connect(scratch)
    try:
        out = []
        for run, started, finished, result, course, distance, game in db.execute(
                'SELECT r.id, r.started, r.finished, r.result_time, r.course, r.distance, c.game FROM runs r '
                'JOIN sessions s ON s.id = r.session JOIN cars c ON c.id = s.car WHERE r.ended IS NOT NULL').fetchall():
            row = {'started': started, 'finished': finished, 'result_time': result, 'course': course,
                   'distance': distance, 'game': game}
            for table in FINISH_TABLES:
                row[table] = _row(db, table, run)
            blob = db.execute('SELECT version, data FROM traces WHERE run = ?', (run,)).fetchone()
            row['trace'] = (blob[0], blob[1]) if blob else None
            out.append(row)
        return out
    finally:
        db.close()


def _other_line(db, run, new):
    """The stored run was timed at a finish line (run_finish) other than the replay's: (stored, replay's) in m."""
    old, replayed = _row(db, 'run_finish', run), new['run_finish']
    if old and old[0] is not None and replayed and replayed[0] is not None and abs(old[0] - replayed[0]) > LINE_MOVED:
        return old[0], replayed[0]
    return None


def retime_from_captures(path, folders=None, dry_run=False, out=print):
    """Re-time the finished runs of the database at `path` from the captures (see the module's docstring). `folders`:
    where the captures are (default: the `captures` folder next to the file). With `dry_run` the file is only read
    (no lock, no table made, no backup). Raises Locked when the file is open elsewhere. Returns {'retimed': [(run,
    old, new, capture)], 'clocked': [runs given the game's clock row], 'unmatched': [(run, stage, result, why)],
    'backup': path or None}."""
    folders = folders or [os.path.join(os.path.dirname(os.path.abspath(path)), 'captures')]
    db = _open_read_only(path) if dry_run else _open_exclusive(path)
    store = Store(db, path)
    report = {'retimed': [], 'clocked': [], 'unmatched': [], 'backup': None}
    scratch_dir = tempfile.mkdtemp(prefix='oversteer-retime-')
    try:
        runs = db.execute("SELECT r.id, r.started, r.stage, r.result_time, r.course, c.game FROM runs r "
                          "JOIN sessions s ON s.id = r.session JOIN cars c ON c.id = s.car WHERE r.finished = 1 "
                          "AND r.result_time IS NOT NULL ORDER BY r.started").fetchall()
        try:
            learnt = store.learn_finishes()                 # the live file's lines, for the replays
        except sqlite3.OperationalError:
            learnt = {}
        captures = capture_files(folders)
        by_capture, sizes = {}, {}
        for run in runs:
            found = [c for c in captures if c[1] - 1.0 <= run[1] <= c[2] + 1.0]
            if found:
                by_capture.setdefault(found[-1][0], []).append(run)
                sizes[found[-1][0]] = found[-1][3]
            else:
                report['unmatched'].append((run[0], run[2], run[3], 'no capture holds it'))
        if by_capture:
            minutes = max(1, int(math.ceil(sum(sizes.values()) / REPLAY_RATE / 60.0)))
            out("Replaying {} captures: about {} minute{}.{}".format(
                len(by_capture), minutes, '' if minutes == 1 else 's',
                '' if dry_run else " Don't start Oversteer while this runs."))
        changes = []
        for capture, wanted in sorted(by_capture.items()):
            out('replaying {} ({} finished runs)'.format(os.path.basename(capture), len(wanted)))
            try:
                replayed = _replay_runs(capture, os.path.join(scratch_dir, 'replay.db'), learnt)
            except Exception as e:
                logging.exception("retime: replaying %s", capture)
                report['unmatched'].extend((r[0], r[2], r[3], 'the replay failed: {}'.format(e)) for r in wanted)
                continue
            for run, started, stage, result, course, game in wanted:
                near = [r for r in replayed if abs(r['started'] - started) <= MATCH_STARTED and r['game'] == game]
                if len(near) != 1:
                    report['unmatched'].append((run, stage, result, 'the replay started {} runs then'.format(len(near))))
                    continue
                new = near[0]
                if new['finished'] != 1 or new['result_time'] is None:
                    report['unmatched'].append((run, stage, result, 'the current code does not finish it'))
                    continue
                other = _other_line(db, run, new)
                if other is not None:
                    report['unmatched'].append((run, stage, result, 'the replay timed it at another finish line '
                                                '({:.1f} m, stored {:.1f} m)'.format(other[1], other[0])))
                    continue
                changes.append((run, stage, result, course, new, os.path.basename(capture)))
        stage_tables.set_learnt(learnt)                  # the live file's learnt lines again, after the replays
        todo = [c for c in changes if abs(c[4]['result_time'] - c[2]) >= RETIME_MIN]
        # the game's own clock, for every run the replay finished on it, whatever the time: once a car has a run on
        # the game's clock on a stage only its clock runs rank (Reader.ON_CLOCK), so a matched run without the row
        # would drop out of the ranking (the PB) next to a re-timed one with it
        clocked = [c for c in changes if c not in todo and c[4]['run_clock'] is not None
                   and _row(db, 'run_clock', c[0]) != c[4]['run_clock']]
        report['clocked'] = [c[0] for c in clocked]
        if dry_run or not (todo or clocked):
            report['retimed'] = [(c[0], c[2], c[4]['result_time'], c[5]) for c in todo]
            return report
        dest = '{}.{}-{}.bak'.format(path, RETIME_BACKUP_TAG, time.strftime('%Y%m%d-%H%M%S'))
        _backup(db, dest)
        report['backup'] = dest
        retimed = _retimed(store)
        db.execute('BEGIN')
        try:
            for run, stage, old, course, new, capture in clocked:
                db.execute('INSERT OR REPLACE INTO run_clock (run, clock) VALUES (?, ?)', (run, new['run_clock'][0]))
            for run, stage, old, course, new, capture in todo:
                store.update_run(run, result_time=new['result_time'], course=new['course'])
                for table in FINISH_TABLES:
                    if new[table] is not None:
                        db.execute('INSERT OR REPLACE INTO {} (run, {}) VALUES (?, ?)'.format(
                            table, FINISH_COLUMNS[table]), (run, new[table][0]))
                trace = _trace(store, run)
                if new['trace'] is not None and new['course'] is not None and trace \
                        and trace[-1][T['distance']] < new['course'] - TRACE_SHORT:
                    replay_rows = unpack_trace(new['trace'][1], new['trace'][0])
                    if replay_rows and replay_rows[-1][T['distance']] >= new['course'] - TRACE_SHORT:
                        db.execute('UPDATE traces SET version = ?, data = ? WHERE run = ?',
                                   (new['trace'][0], new['trace'][1], run))
                        out('run {}: its trace stopped {:.0f} m before the finish: the replay\'s is stored'.format(
                            run, new['course'] - trace[-1][T['distance']]))
                car = db.execute('SELECT s.car FROM runs r JOIN sessions s ON s.id = r.session WHERE r.id = ?',
                                 (run,)).fetchone()[0]
                if stage:
                    store.delete_potential(stage, car)
                retimed[str(run)] = [old, new['result_time'], capture]
                report['retimed'].append((run, old, new['result_time'], capture))
            store.set_meta(RETIMED, json.dumps(retimed))
            requeue(store, [c[0] for c in todo + clocked])
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            raise
        return report
    finally:
        db.close()
        shutil.rmtree(scratch_dir, ignore_errors=True)


def run_retime(path, folders=None, dry_run=False, out=print):
    """The command: re-time and say what changed. Returns the exit status."""
    if not os.path.exists(path):
        out('No telemetry database at {}'.format(path))
        return 1
    try:
        report = retime_from_captures(path, folders, dry_run, out)
    except Locked:
        out('The telemetry database is open: close Oversteer (and anything reading {}) and run this again.'.format(
            path))
        return 2
    except sqlite3.DatabaseError as e:
        out(str(e))
        return 1
    if report['backup']:
        out('Backup: {}'.format(report['backup']))
    verb = 'would be re-timed' if dry_run else 're-timed'
    for run, old, new, capture in report['retimed']:
        out('run {} {}: {:.3f} s -> {:.3f} s ({:+.3f} s) from {}'.format(run, verb, old, new, new - old, capture))
    if report['clocked']:
        out('{} on the game\'s clock: runs {}'.format('would be marked' if dry_run else 'marked',
                                                     ', '.join(map(str, report['clocked']))))
    for run, stage, result, why in report['unmatched']:
        out('run {} on {} keeps {}: {}'.format(run, stage, 'no time' if result is None else '{:.3f} s'.format(result),
                                               why))
    changed = report['retimed'] or report['clocked']
    out('{} runs {}, {} kept as they were.{}'.format(
        len(report['retimed']), verb, len(report['unmatched']),
        '' if dry_run or not changed else ' Oversteer works them over again when it next starts.'))
    return 0


def main(argv=None):
    from . import diagnostics
    parser = argparse.ArgumentParser(prog='python3 -m oversteer.data_repair',
                                     description='Repairs of the stored telemetry (Oversteer 0.16).')
    sub = parser.add_subparsers(dest='command', required=True)
    retime = sub.add_parser('retime', help='re-time the finished runs from the captures (Oversteer closed)')
    retime.add_argument('--db', default=os.path.join(diagnostics.data_home(), 'oversteer', 'telemetry.db'),
                        help="the telemetry database (default: the app's, as Oversteer finds it)")
    retime.add_argument('--captures', action='append', help='a folder of captures (default: next to the database)')
    retime.add_argument('--dry-run', action='store_true', help='say what would change, write nothing')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    return run_retime(args.db, args.captures, args.dry_run)


if __name__ == '__main__':
    sys.exit(main())
