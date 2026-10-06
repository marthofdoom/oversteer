#!/usr/bin/env python3
"""Merge one Oversteer telemetry database (SOURCE) into another (TARGET):
the runs a development build recorded, ported into the Flatpak app's file.

    scripts/merge-telemetry-db.py TARGET SOURCE [--map-profile SRC=DST ...] [--backfill PROFILE]
                                  [--dry-run] [--backup-dir DIR] [--workdir DIR]

Neither file is written until the very end. Both are copied through SQLite (a
copy of the file would miss what is still in its write-ahead log) into
--workdir, and both copies are opened with telemetry_store.open_store, which
brings them to this code's schema (v2 to v3 and the tables made on open). The
merge runs on the target's copy in one transaction (any error rolls it back),
and is checked (every reference, every trace decodes, integrity). --backfill
then runs the app's re-analysis on the merged copy (backfill()). --dry-run
prints the plan (per table: rows inserted, matched/mapped, skipped; the
duplicates) and stops there: the merged copy is left in --workdir to look at.
A real run then checks the target was not written since it was copied, copies
it into --backup-dir (required), and copies the merged file over it through
SQLite's backup API (one transaction on the target: all or nothing). Any error
before that leaves both real files as they were. SOURCE is only ever read.
Run it with the app closed. Running it again adds nothing and writes nothing.

Profiles: rows keep their profile unless --map-profile renames it (the same
wheel profile saved under another name by the other build: ACR="AC Rally").

Every table of the schema, and what the merge does with it:

  primary (recorded; nothing can make it again)
    cars          matched by (profile, key, or a legacy key of it), else inserted. A matched car keeps the target's
                  learnt model unless the source's has more data (model_weight: gears learnt, then pulls, then
                  samples), the name the user gave it, and the class and drivetrain it has (the source's fill a NULL)
    tunes         matched by (car, first_seen), else inserted
    stages        union by key: a new key is inserted; a known one gets the source's name, location and length
                  where it has none, and its priors where it has none (or the source's was set by the user and the
                  target's was not). `runs` counts the runs ported onto it
    sessions      matched by (profile, car, start within DUP_SECONDS), else inserted; a session whose runs are all
                  duplicates goes to the session of the first one's copy
    runs          inserted in start order unless a duplicate (below). run_class is cleared on a run with a trace,
                  so the backfill works it over (its corners, events, metrics, class) as the app does after an
                  upgrade; a run with no trace keeps its class and its derived rows (nothing could make them again)
    traces, laps, segments, labels, run_start, run_clock, run_stop, run_finish
                  ported with their run (run_finish: the line a run was timed at, so the re-timing leaves it be).
                  A duplicate's label is added to the kept copy where that copy has none
    shifts        ported with their run (the backfill rewrites flags, d and band); one with no run is ported with a
                  session that was inserted, and skipped where the session was matched (recorded in both)
    captures      by path (unique): inserted when the target has none, its session mapped
  partly primary
    metrics       the launch metrics the backfill cannot work out again (drive_log.BACKFILL_KEEP) are ported; the
                  rest is derived (below). A run without a trace keeps all of its metrics
  derived (worked out again from the primary rows: not ported)
    corners, events, metrics   the backfill (drive_log.backfill_step) writes them from the trace
    stage_potential            dropped in the target for every stage that receives a run: rebuilt (potentials_missing)
    envelopes                  not ported; replaced whenever a potential of the car is rebuilt
  state
    stage_lines   not ported, and dropped in the target for every ACR stage that receives a run, so the repair at the
                  next start (drive_log.requeue_moved_stages) queues the stage's runs and drops its potentials: the
                  target's runs there are classed again with the new ones
    coach_state   per (profile, car mapped, tip): inserted when absent, else one row of both: first_shown the
                  earliest, last_shown the latest, times the larger (not the sum: re-running stays the same), value
                  and quiet the more recently shown's
    calibration   per (game, kind): the target's is kept; the source's inserted only where the target has none

Duplicates: the same drive recorded by both files, a source run of the same game and car key (any profile) with a
start within DUP_SECONDS of a target run and the same stage (or either unknown), or a byte-identical trace. One copy
is kept: the target's, unless the source's has more data (data_score: on the game's clock, a recorded start
place, trace version, positions in the trace, trace rows), when the target's run is replaced by it.
"""
import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
from oversteer import telemetry_store                                     # noqa: E402
from oversteer.drive_log import BACKFILL_KEEP                              # noqa: E402

DUP_SECONDS = 2.0

PRIMARY = ('cars', 'tunes', 'stages', 'sessions', 'runs', 'traces', 'laps', 'segments', 'shifts', 'labels',
           'captures', 'run_finish', 'run_start', 'run_clock', 'run_stop')
PARTLY = ('metrics',)
DERIVED = ('corners', 'events', 'envelopes', 'stage_potential')
STATE = ('stage_lines', 'coach_state', 'calibration')
KNOWN = set(PRIMARY + PARTLY + DERIVED + STATE)
RUN_TABLES = ('run_start', 'run_clock', 'run_stop', 'run_finish')      # one row per run, keyed by it
PRIOR_RANK = {'user': 2}


class MergeError(Exception):
    pass


# -- files --

def copy_db(src, dest, journal=None):
    """A consistent copy of the database at `src` into `dest`, through SQLite (what is in the WAL too), opening
    `src` read-only; `journal` sets the copy's journal mode ('delete': one file, for a backup)."""
    if not os.path.exists(src):
        raise MergeError('{} does not exist'.format(src))
    for suffix in ('', '-wal', '-shm'):
        if os.path.exists(dest + suffix):
            os.remove(dest + suffix)
    db = sqlite3.connect(_ro(src), uri=True)
    try:
        out = sqlite3.connect(dest)
        try:
            db.backup(out)
            if journal:
                out.execute('PRAGMA journal_mode = {}'.format(journal))
        finally:
            out.close()
    finally:
        db.close()


def fingerprint(path, schema=True):
    """What would show the file was written since it was read: its schema version (`schema`: a copy through the
    backup API has its own) and user version, and the count and highest id of the tables a run writes."""
    db = sqlite3.connect(_ro(path), uri=True)
    try:
        out = list(db.execute('PRAGMA schema_version').fetchone()) if schema else []
        out += list(db.execute('PRAGMA user_version').fetchone())
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table in ('cars', 'sessions', 'runs', 'shifts', 'traces', 'coach_state', 'metrics'):
            if table in tables:
                out += list(db.execute('SELECT COUNT(*), MAX(rowid) FROM {}'.format(table)).fetchone())
        out += list(db.execute('SELECT MAX(updated) FROM cars').fetchone()) if 'cars' in tables else []
        return tuple(out)
    finally:
        db.close()


# -- helpers --

def columns(db, table):
    return [r[1] for r in db.execute('PRAGMA table_info({})'.format(table))]


def tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                     "AND name NOT LIKE 'sqlite_%'")}


def insert(db, table, row):
    names = sorted(row)
    return db.execute('INSERT INTO {} ({}) VALUES ({})'.format(table, ', '.join(names), ', '.join('?' * len(names))),
                      tuple(row[n] for n in names)).lastrowid


def model_weight(text):
    """How much a stored car model (CarModel.to_dict() as JSON) has learnt: (gears learnt, pulls, samples)."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return (0, 0, 0)
    if not isinstance(data, dict):
        return (0, 0, 0)
    samples = 0
    for field in ('power', 'power_g', 'ratios', 'upshifts', 'tyre_radius', 'drive'):
        value = data.get(field)
        if isinstance(value, dict):
            samples += sum(len(v) for v in value.values() if isinstance(v, list))
    try:
        pulls = int(data.get('pulls') or 0)
    except (TypeError, ValueError):
        pulls = 0
    return (len(data.get('ratios') or {}), pulls, samples)


def data_score(db, run):
    """How much one copy of a drive holds: (on the game's clock, a recorded start place, trace version, positions in
    the trace, trace rows)."""
    clock = db.execute('SELECT 1 FROM run_clock WHERE run = ?', (run,)).fetchone() is not None
    start = db.execute('SELECT 1 FROM run_start WHERE run = ?', (run,)).fetchone() is not None
    row = db.execute('SELECT version, data FROM traces WHERE run = ?', (run,)).fetchone()
    version = positions = rows = 0
    if row is not None and row[0] in telemetry_store.TRACE_VERSIONS:
        version = row[0]
        trace = telemetry_store.unpack_trace(row[1], row[0])
        rows = len(trace)
        x = telemetry_store.TRACE_CHANNELS.index('x')
        positions = int(any(math.isfinite(r[x]) for r in trace))
    return (int(clock), int(start), version, positions, rows)


class Plan:
    def __init__(self):
        self.counts = {}
        self.notes = []
        self.duplicates = []
        self.cars = []
        self.profiles = {}

    def add(self, table, what, n=1):
        counts = self.counts.setdefault(table, {})
        counts[what] = counts.get(what, 0) + n

    def note(self, text):
        self.notes.append(text)

    def changes(self):
        return sum(n for t in self.counts.values() for w, n in t.items() if w in ('insert', 'update', 'delete',
                                                                                 'replace'))

    def lines(self):
        out = ['profiles: ' + ', '.join('{!r} -> {!r}'.format(a, b) for a, b in sorted(self.profiles.items()))]
        out.append('cars:')
        out += ['  ' + c for c in self.cars]
        out.append('per table (insert / update / map: matched an existing row / skip / delete / replace):')
        order = PRIMARY + PARTLY + DERIVED + STATE
        for table in order:
            counts = self.counts.get(table, {})
            kind = ('primary' if table in PRIMARY else 'partly primary' if table in PARTLY
                    else 'derived' if table in DERIVED else 'state')
            out.append('  {:<16} {:<15} {}'.format(table, kind, ', '.join(
                '{} {}'.format(w, n) for w, n in sorted(counts.items())) or '-'))
        out.append('duplicates found: {}'.format(len(self.duplicates)))
        out += ['  ' + d for d in self.duplicates]
        out += ['note: ' + n for n in self.notes]
        return out


# -- the merge --

def merge(tdb, sdb, profile_map=None, plan=None):
    """Merge the source connection `sdb` into the target connection `tdb` (both at this code's schema), inside the
    caller's transaction. Returns the Plan."""
    profile_map = dict(profile_map or {})
    plan = plan or Plan()
    for name, db in (('target', tdb), ('source', sdb)):
        unknown = tables(db) - KNOWN
        if unknown:
            raise MergeError('the {} has tables this merge does not know: {}'.format(name, ', '.join(sorted(unknown))))
    for table in KNOWN:
        if table in tables(sdb) and columns(sdb, table) != columns(tdb, table):
            raise MergeError('{} has other columns in the source and the target'.format(table))
    sdb.row_factory = sqlite3.Row

    def prof(p):
        return profile_map.get(p, p)

    for (p,) in sdb.execute('SELECT DISTINCT profile FROM cars UNION SELECT DISTINCT profile FROM sessions '
                            'UNION SELECT DISTINCT profile FROM coach_state'):
        plan.profiles[p] = prof(p)
        if not tdb.execute('SELECT 1 FROM cars WHERE profile = ? UNION SELECT 1 FROM sessions WHERE profile = ?',
                           (prof(p), prof(p))).fetchone():
            plan.note('source profile {!r} is {!r} in the target, which has no car or session under it: its cars '
                      'come in as new cars (--map-profile renames it)'.format(p, prof(p)))

    # cars
    car_map = {}
    for c in sdb.execute('SELECT * FROM cars ORDER BY id').fetchall():
        c = dict(c)
        profile = prof(c['profile'])
        found = None
        for key in [c['key']] + telemetry_store.legacy_keys(c['key']):
            found = tdb.execute('SELECT id, key, model, user_named, name, car_class, drivetrain, updated FROM cars '
                                'WHERE profile = ? AND key = ?', (profile, key)).fetchone()
            if found:
                break
        if found is None:
            row = {k: v for k, v in c.items() if k != 'id'}
            row['profile'] = profile
            car_map[c['id']] = insert(tdb, 'cars', row)
            plan.add('cars', 'insert')
            plan.cars.append('{} {!r} {}: new car {}'.format(c['id'], c['profile'], c['key'], car_map[c['id']]))
            continue
        tid, tkey, tmodel, tnamed, tname, tclass, tdrive, tupdated = found
        car_map[c['id']] = tid
        fields, why = {}, []
        sw, tw = model_weight(c['model']), model_weight(tmodel)
        if sw > tw:
            try:
                data = json.loads(c['model'])
                data['key'] = tkey
                fields['model'] = json.dumps(data)
            except (TypeError, ValueError):
                fields['model'] = c['model']
            why.append('model from the source (learnt {} > {})'.format(sw, tw))
        else:
            why.append('model kept (learnt {} >= source {})'.format(tw, sw))
        if c['user_named'] and not tnamed and c['name'] and c['name'] != tname:
            fields.update(name=c['name'], user_named=1)
        if tclass is None and c['car_class'] is not None:
            fields['car_class'] = c['car_class']
        if tdrive is None and c['drivetrain'] is not None:
            fields['drivetrain'] = c['drivetrain']
        if c['updated'] is not None and (tupdated is None or c['updated'] > tupdated) and 'model' in fields:
            fields['updated'] = c['updated']
        if fields:
            names = sorted(fields)
            tdb.execute('UPDATE cars SET {} WHERE id = ?'.format(', '.join(n + ' = ?' for n in names)),
                        tuple(fields[n] for n in names) + (tid,))
            plan.add('cars', 'update')
        else:
            plan.add('cars', 'map')
        plan.cars.append('{} {!r} {}: is target car {} ({}{})'.format(
            c['id'], c['profile'], c['key'], tid, '; '.join(why),
            '; set ' + ', '.join(sorted(fields)) if fields else ''))

    # tunes
    tune_map = {}
    for t in sdb.execute('SELECT * FROM tunes ORDER BY id').fetchall():
        t = dict(t)
        car = car_map[t['car']]
        found = tdb.execute('SELECT id FROM tunes WHERE car = ? AND abs(first_seen - ?) < 0.001',
                            (car, t['first_seen'])).fetchone()
        if found:
            tune_map[t['id']] = found[0]
            plan.add('tunes', 'map')
        else:
            row = {k: v for k, v in t.items() if k != 'id'}
            row['car'] = car
            tune_map[t['id']] = insert(tdb, 'tunes', row)
            plan.add('tunes', 'insert')

    # stages (all of them: a stage known only to the source is a known stage, with its priors)
    for s in sdb.execute('SELECT * FROM stages ORDER BY key').fetchall():
        s = dict(s)
        t = tdb.execute('SELECT * FROM stages WHERE key = ?', (s['key'],)).fetchone()
        if t is None:
            row = dict(s, runs=0)
            insert(tdb, 'stages', row)
            plan.add('stages', 'insert')
            continue
        t = dict(zip(columns(tdb, 'stages'), t))
        fields = {}
        for name in ('name', 'location', 'length'):
            if t[name] is None and s[name] is not None:
                fields[name] = s[name]
        for prior in ('surface_prior', 'discipline_prior'):
            src = prior + '_source'
            if s[prior] is None:
                continue
            if t[prior] is None or (PRIOR_RANK.get(s[src], 0) > PRIOR_RANK.get(t[src], 0)):
                if (t[prior], t[src]) != (s[prior], s[src]):
                    fields[prior], fields[src] = s[prior], s[src]
        if fields:
            names = sorted(fields)
            tdb.execute('UPDATE stages SET {} WHERE key = ?'.format(', '.join(n + ' = ?' for n in names)),
                        tuple(fields[n] for n in names) + (s['key'],))
            plan.add('stages', 'update')
        else:
            plan.add('stages', 'map')

    # the runs: duplicates first (they decide where a session goes)
    hashes = {}
    for run, data in tdb.execute('SELECT run, data FROM traces'):
        hashes.setdefault(hashlib.sha256(data).hexdigest(), run)
    src_runs = [dict(r) for r in sdb.execute(
        'SELECT r.*, s.car AS _car, c.key AS _key, c.game AS _game, s.profile AS _profile FROM runs r '
        'JOIN sessions s ON s.id = r.session JOIN cars c ON c.id = s.car ORDER BY r.started, r.id')]
    dup = {}                                        # source run -> (target run, keep the source's copy)
    for r in src_runs:
        found = tdb.execute(
            'SELECT r.id, r.started, r.stage FROM runs r JOIN sessions s ON s.id = r.session JOIN cars c ON '
            'c.id = s.car WHERE c.game = ? AND c.key IN ({}) AND abs(r.started - ?) <= ? '
            'AND (r.stage IS ? OR r.stage IS NULL OR ? IS NULL) ORDER BY abs(r.started - ?) LIMIT 1'.format(
                ', '.join('?' * (1 + len(telemetry_store.legacy_keys(r['_key']))))),
            (r['_game'], r['_key'], *telemetry_store.legacy_keys(r['_key']), r['started'], DUP_SECONDS,
             r['stage'], r['stage'], r['started'])).fetchone()
        why = 'same car, stage and start ({:+.2f} s)'.format(found[1] - r['started']) if found else None
        if found is None:
            data = sdb.execute('SELECT data FROM traces WHERE run = ?', (r['id'],)).fetchone()
            other = hashes.get(hashlib.sha256(data[0]).hexdigest()) if data else None
            if other is not None:
                found, why = (other,), 'identical trace'
        if found is None:
            continue
        mine, theirs = data_score(sdb, r['id']), data_score(tdb, found[0])
        keep_source = mine > theirs
        dup[r['id']] = (found[0], keep_source)
        plan.duplicates.append('source run {} ({} {} {}) = target run {}: {}; keeping the {} copy '
                               '(score {} vs {})'.format(
            r['id'], r['_key'], r['stage'], time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['started'])),
            found[0], why, 'source' if keep_source else 'target', mine, theirs))

    # sessions
    session_map, session_new = {}, set()
    by_session = {}
    for r in src_runs:
        by_session.setdefault(r['session'], []).append(r)
    for s in sdb.execute('SELECT * FROM sessions ORDER BY started, id').fetchall():
        s = dict(s)
        profile, car = prof(s['profile']), car_map[s['car']]
        found = tdb.execute('SELECT id FROM sessions WHERE profile = ? AND car = ? AND abs(started - ?) <= ? '
                            'ORDER BY abs(started - ?) LIMIT 1',
                            (profile, car, s['started'], DUP_SECONDS, s['started'])).fetchone()
        runs = by_session.get(s['id'], [])
        if found is None and runs and all(r['id'] in dup for r in runs):
            found = tdb.execute('SELECT session FROM runs WHERE id = ?', (dup[runs[0]['id']][0],)).fetchone()
        if found is not None:
            session_map[s['id']] = found[0]
            plan.add('sessions', 'map')
            continue
        row = {k: v for k, v in s.items() if k != 'id'}
        row.update(profile=profile, car=car, tune=tune_map.get(s['tune']) if s['tune'] is not None else None)
        session_map[s['id']] = insert(tdb, 'sessions', row)
        session_new.add(s['id'])
        plan.add('sessions', 'insert')

    # runs
    run_map, ported, touched_stages = {}, [], set()
    for r in src_runs:
        if r['id'] in dup:
            target, keep_source = dup[r['id']]
            if not keep_source:
                run_map[r['id']] = target
                plan.add('runs', 'skip')
                label = sdb.execute('SELECT * FROM labels WHERE run = ?', (r['id'],)).fetchone()
                if label is not None and not tdb.execute('SELECT 1 FROM labels WHERE run = ?', (target,)).fetchone():
                    insert(tdb, 'labels', dict(dict(label), run=target))
                    plan.add('labels', 'insert')
                continue
            # the source's copy has more: the target's goes (its label stays where the source has none)
            label = tdb.execute('SELECT * FROM labels WHERE run = ?', (target,)).fetchone()
            label = dict(zip(columns(tdb, 'labels'), label)) if label else None
            stage = tdb.execute('SELECT stage FROM runs WHERE id = ?', (target,)).fetchone()[0]
            tdb.execute('DELETE FROM shifts WHERE run = ?', (target,))
            tdb.execute('DELETE FROM runs WHERE id = ?', (target,))
            if stage is not None:
                tdb.execute('UPDATE stages SET runs = MAX(runs - 1, 0) WHERE key = ?', (stage,))
            plan.add('runs', 'replace')
        else:
            label = None
        row = {k: v for k, v in r.items() if k != 'id' and not k.startswith('_')}
        row['session'] = session_map[r['session']]
        has_trace = sdb.execute('SELECT 1 FROM traces WHERE run = ?', (r['id'],)).fetchone() is not None
        if has_trace:
            row['run_class'] = None                  # the backfill works it over
        new = insert(tdb, 'runs', row)
        run_map[r['id']] = new
        ported.append((r['id'], new, has_trace))
        plan.add('runs', 'insert')
        if r['stage'] is not None:
            tdb.execute('UPDATE stages SET runs = runs + 1 WHERE key = ?', (r['stage'],))
            touched_stages.add(r['stage'])
        if label is not None and not sdb.execute('SELECT 1 FROM labels WHERE run = ?', (r['id'],)).fetchone():
            insert(tdb, 'labels', dict(label, run=new))
            plan.add('labels', 'insert')

    # what comes with a ported run
    for old, new, has_trace in ported:
        for table in ('traces', 'labels') + RUN_TABLES:
            for row in sdb.execute('SELECT * FROM {} WHERE run = ?'.format(table), (old,)).fetchall():
                insert(tdb, table, dict(dict(row), run=new))
                plan.add(table, 'insert')
        for table in ('laps', 'segments') + (() if has_trace else ('corners', 'events')):
            for row in sdb.execute('SELECT * FROM {} WHERE run = ? ORDER BY id'.format(table), (old,)).fetchall():
                row = {k: v for k, v in dict(row).items() if k != 'id'}
                insert(tdb, table, dict(row, run=new))
                plan.add(table, 'insert')
        if has_trace:
            n = sdb.execute('SELECT COUNT(*) FROM corners WHERE run = ?', (old,)).fetchone()[0]
            m = sdb.execute('SELECT COUNT(*) FROM events WHERE run = ?', (old,)).fetchone()[0]
            plan.add('corners', 'skip', n)
            plan.add('events', 'skip', m)
        for row in sdb.execute('SELECT * FROM metrics WHERE run = ? ORDER BY id', (old,)).fetchall():
            row = dict(row)
            if has_trace and row['name'] not in BACKFILL_KEEP:
                plan.add('metrics', 'skip')
                continue
            row.pop('id')
            row.update(run=new, session=session_map[row['session']])
            insert(tdb, 'metrics', row)
            plan.add('metrics', 'insert')
    ported_ids = {old for old, _, _ in ported}

    # shifts
    for h in sdb.execute('SELECT * FROM shifts ORDER BY session, at, id').fetchall():
        h = dict(h)
        if h['run'] is not None:
            if h['run'] not in ported_ids:
                plan.add('shifts', 'skip')
                continue
        elif h['session'] not in session_new:
            plan.add('shifts', 'skip')
            continue
        h.pop('id')
        h.update(session=session_map[h['session']], run=run_map.get(h['run']) if h['run'] is not None else None)
        insert(tdb, 'shifts', h)
        plan.add('shifts', 'insert')
    for m in sdb.execute('SELECT * FROM metrics WHERE run IS NULL ORDER BY id').fetchall():
        m = dict(m)
        if m['session'] not in session_new:
            plan.add('metrics', 'skip')
            continue
        m.pop('id')
        m['session'] = session_map[m['session']]
        insert(tdb, 'metrics', m)
        plan.add('metrics', 'insert')

    # captures
    for c in sdb.execute('SELECT * FROM captures ORDER BY id').fetchall():
        c = dict(c)
        if tdb.execute('SELECT 1 FROM captures WHERE path = ?', (c['path'],)).fetchone():
            plan.add('captures', 'map')
            continue
        c.pop('id')
        c['session'] = session_map.get(c['session']) if c['session'] is not None else None
        insert(tdb, 'captures', c)
        plan.add('captures', 'insert')

    # calibration
    for c in sdb.execute('SELECT * FROM calibration').fetchall():
        c = dict(c)
        if tdb.execute('SELECT 1 FROM calibration WHERE game = ? AND kind = ?', (c['game'], c['kind'])).fetchone():
            plan.add('calibration', 'skip')
        else:
            insert(tdb, 'calibration', c)
            plan.add('calibration', 'insert')

    # coach_state
    for c in sdb.execute('SELECT * FROM coach_state').fetchall():
        c = dict(c)
        car = 0 if not c['car'] else car_map.get(c['car'])
        if car is None:
            plan.add('coach_state', 'skip')
            continue
        profile = prof(c['profile'])
        t = tdb.execute('SELECT first_shown, last_shown, times, value, quiet FROM coach_state WHERE profile = ? '
                        'AND car = ? AND tip = ?', (profile, car, c['tip'])).fetchone()
        if t is None:
            insert(tdb, 'coach_state', dict(c, profile=profile, car=car))
            plan.add('coach_state', 'insert')
            continue
        firsts = [x for x in (t[0], c['first_shown']) if x is not None]
        lasts = [x for x in (t[1], c['last_shown']) if x is not None]
        later = (c['value'], c['quiet']) if (c['last_shown'] or 0) > (t[1] or 0) else (t[3], t[4])
        merged = (min(firsts) if firsts else None, max(lasts) if lasts else None,
                  max(t[2] or 0, c['times'] or 0)) + later
        if merged != tuple(t):
            tdb.execute('UPDATE coach_state SET first_shown = ?, last_shown = ?, times = ?, value = ?, quiet = ? '
                        'WHERE profile = ? AND car = ? AND tip = ?', merged + (profile, car, c['tip']))
            plan.add('coach_state', 'update')
        else:
            plan.add('coach_state', 'map')

    # derived and state rows the new runs make stale
    for stage in sorted(touched_stages):
        n = tdb.execute('DELETE FROM stage_potential WHERE stage = ?', (stage,)).rowcount
        if n:
            plan.add('stage_potential', 'delete', n)
        n = tdb.execute('DELETE FROM stage_lines WHERE stage = ?', (stage,)).rowcount
        if n:
            plan.add('stage_lines', 'delete', n)
    for table in ('envelopes', 'stage_potential', 'stage_lines'):
        n = sdb.execute('SELECT COUNT(*) FROM {}'.format(table)).fetchone()[0]
        if n:
            plan.add(table, 'skip', n)
    if ported:
        plan.note('{} run(s) ported; {} with a trace queued for the backfill (run_class cleared); stages receiving '
                  'runs: {}'.format(len(ported), sum(1 for p in ported if p[2]), ', '.join(sorted(touched_stages))))
    return plan


def check(tdb):
    """Raise MergeError on a broken reference anywhere or a trace that does not decode."""
    bad = tdb.execute('PRAGMA foreign_key_check').fetchall()
    if bad:
        raise MergeError('foreign key check failed: {}'.format(bad[:10]))
    orphans = {
        'coach_state.car': 'SELECT COUNT(*) FROM coach_state WHERE car != 0 AND car NOT IN (SELECT id FROM cars)',
        'stage_potential.stage': 'SELECT COUNT(*) FROM stage_potential WHERE stage NOT IN (SELECT key FROM stages)',
        'sessions.profile': 'SELECT COUNT(*) FROM sessions s JOIN cars c ON c.id = s.car WHERE s.profile != c.profile',
        'shifts.run/session': 'SELECT COUNT(*) FROM shifts h JOIN runs r ON r.id = h.run WHERE r.session != h.session',
        'metrics.run/session': 'SELECT COUNT(*) FROM metrics m JOIN runs r ON r.id = m.run '
                               'WHERE r.session != m.session',
    }
    for name, sql in orphans.items():
        n = tdb.execute(sql).fetchone()[0]
        if n:
            raise MergeError('{}: {} rows point nowhere or disagree'.format(name, n))
    for run, version, data in tdb.execute('SELECT run, version, data FROM traces').fetchall():
        rows = telemetry_store.unpack_trace(data, version)
        if not rows:
            raise MergeError('trace of run {} is empty'.format(run))
    ok = tdb.execute('PRAGMA integrity_check').fetchone()[0]
    if ok != 'ok':
        raise MergeError('integrity check: {}'.format(ok))


def backfill(path, profile, out=print):
    """The re-analysis the app runs at its next start, run now on `path`: drive_log.repair_shipped (the stages' and
    cars' shipped data, the clock stops, the moved lines, the re-timing) and the backfill of every run with a trace
    and no class, then the potential of every stage and car that has none (ShiftLearner.backfill, which the app runs a
    few runs at a time; a stage and car whose potential comes to nothing ends a pass there, so passes go on until
    every one was tried). `profile` is the learner's (the app's current profile). Returns (steps, failed runs)."""
    from oversteer.shift_learner import ShiftLearner
    learner = ShiftLearner(database=path, profile=profile, threaded=False)
    try:
        steps = learner.backfill()
        store = learner.log.store
        while True:
            tried = learner.__dict__.get('_potential_tried', set())
            if not [m for m in store.potentials_missing() if (m[0], m[1]) not in tried]:
                break
            steps += learner.backfill()
        failed = sorted(learner.__dict__.get('_backfill_failed', ()))
        left = store.runs_to_backfill(1000)
        classes = store.db.execute("SELECT COALESCE(run_class, '-'), COUNT(*) FROM runs GROUP BY 1").fetchall()
        potentials = store.db.execute('SELECT COUNT(*) FROM stage_potential').fetchone()[0]
    finally:
        learner.close()
    out('backfill ({!r}): {} steps, failed runs {}, left {}, classes {}, potentials {}'.format(
        profile, steps, failed or 'none', left or 'none', dict(classes), potentials))
    return steps, failed


def _ro(path):
    return 'file:{}?mode=ro'.format(path.replace('%', '%25').replace('?', '%3f').replace('#', '%23'))


def run_merge(target, source, profile_map=None, dry_run=True, workdir=None, backup_dir=None, out=print,
              backfill_profile=None):
    """The whole job (see the module's docstring). Returns the Plan. Everything is done on the work copies (the
    merge in one transaction, then with `backfill_profile` the app's re-analysis, see backfill()); only a real run
    then writes the target, and only when the merge changed something."""
    target, source = os.path.abspath(os.path.expanduser(target)), os.path.abspath(os.path.expanduser(source))
    if target == source:
        raise MergeError('the target and the source are the same file')
    stamp = time.strftime('%Y%m%d-%H%M%S')
    workdir = os.path.abspath(os.path.expanduser(workdir or os.path.join('~', '.cache', 'oversteer-merge',
                                                                        'work-' + stamp)))
    if not dry_run and not backup_dir:
        raise MergeError('a real merge needs --backup-dir')
    os.makedirs(workdir, exist_ok=True)
    before = fingerprint(target)
    work_target, work_source = os.path.join(workdir, 'target.db'), os.path.join(workdir, 'source.db')
    copy_db(target, work_target)
    copy_db(source, work_source)
    telemetry_store.open_store(work_source).db.close()       # brought to this schema
    store = telemetry_store.open_store(work_target)
    tdb = store.db
    sdb = sqlite3.connect(_ro(work_source), uri=True)
    try:
        counts_before = {t: tdb.execute('SELECT COUNT(*) FROM {}'.format(t)).fetchone()[0] for t in sorted(tables(tdb))}
        tdb.execute('BEGIN IMMEDIATE')
        try:
            plan = merge(tdb, sdb, profile_map)
            check(tdb)
            counts_after = {t: tdb.execute('SELECT COUNT(*) FROM {}'.format(t)).fetchone()[0]
                            for t in sorted(tables(tdb))}
        except BaseException:
            tdb.execute('ROLLBACK')
            raise
        tdb.execute('COMMIT')                        # on the work copy: the target is written below, or never
    finally:
        sdb.close()
    for line in plan.lines():
        out(line)
    changed = ['{} {}->{}'.format(t, counts_before.get(t, 0), n) for t, n in sorted(counts_after.items())
               if counts_before.get(t) != n]
    out('rows: ' + (', '.join(changed) or 'no change'))
    tdb.close()
    if not plan.changes():
        out('nothing to merge: {} left as it was'.format(target))
        return plan
    if backfill_profile is not None:
        backfill(work_target, backfill_profile, out)
    db = sqlite3.connect(work_target)
    try:
        check(db)
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    finally:
        db.close()
    if dry_run:
        out('dry run: {} not written; the merged copy is {}'.format(target, work_target))
        return plan
    if fingerprint(target) != before:
        raise MergeError('{} was written while merging (is the app running?): nothing written'.format(target))
    backup_dir = os.path.abspath(os.path.expanduser(backup_dir))
    os.makedirs(backup_dir, exist_ok=True)
    backup = os.path.join(backup_dir, 'telemetry-pre-merge-{}.db'.format(stamp))
    copy_db(target, backup, 'delete')
    if fingerprint(backup, schema=False) != before[1:]:
        raise MergeError('the backup {} does not match {}: nothing written'.format(backup, target))
    out('backup of the target: {}'.format(backup))
    merged = sqlite3.connect(work_target)
    dest = sqlite3.connect(target, timeout=10.0)
    try:
        merged.backup(dest)                          # one transaction on the target: all or nothing
    finally:
        dest.close()
        merged.close()
    if fingerprint(target, schema=False) != fingerprint(work_target, schema=False):
        raise MergeError('{} does not read back as the merged copy; restore {}'.format(target, backup))
    out('merged into {}'.format(target))
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('target')
    parser.add_argument('source')
    parser.add_argument('--map-profile', action='append', default=[], metavar='SRC=DST',
                        help="a source profile's name in the target (repeatable)")
    parser.add_argument('--dry-run', action='store_true', help='print the plan, write nothing')
    parser.add_argument('--backup-dir', help='where the target is copied before it is written (required to write)')
    parser.add_argument('--workdir', help='where the work copies go (default ~/.cache/oversteer-merge/work-<time>)')
    parser.add_argument('--backfill', metavar='PROFILE',
                        help="run the app's re-analysis (repair and backfill) on the merged copy before it is "
                             "written, as the app would with PROFILE loaded")
    args = parser.parse_args(argv)
    profile_map = {}
    for item in args.map_profile:
        src, sep, dst = item.partition('=')
        if not sep or not src or not dst:
            parser.error('--map-profile takes SRC=DST')
        profile_map[src] = dst
    try:
        run_merge(args.target, args.source, profile_map, args.dry_run, args.workdir, args.backup_dir,
                  backfill_profile=args.backfill)
    except (MergeError, sqlite3.Error) as e:
        print('merge failed, nothing written: {}'.format(e), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
