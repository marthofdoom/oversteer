"""The game's Steam build per session (steam_build, sessions.game_build): Valve's KeyValues files read, every library
found, the build stored with the session, carried through the migration and the merge tool, and the PB on a build
told from the all-time PB."""
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess

import pytest

from oversteer import run_analysis, steam_build
from oversteer.drive_log import DriveLog
from oversteer.shift_learner import ShiftLearner
from oversteer.telemetry_store import open_store, open_reader

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data', 'steam')
STAGE = 'acr:wales:afon-bidno-severn'


def fixture(name):
    with open(os.path.join(DATA, name)) as f:
        return f.read()


@pytest.fixture(autouse=True)
def fresh_cache():
    steam_build.clear_cache()
    yield
    steam_build.clear_cache()


def steam(tmp_path, flatpak_root=False):
    """A native Steam root (FH5 and the runtime in it) and a second library (FH6 and ACR); with `flatpak_root` a
    Flatpak Steam root too, with a third library (the old bare-path format) holding nothing we know. Returns
    (roots, the second library)."""
    root, games = tmp_path / 'home' / '.local' / 'share' / 'Steam', tmp_path / 'games' / 'Steam'
    for lib in (root, games):
        (lib / 'steamapps').mkdir(parents=True)
    (root / 'steamapps' / 'libraryfolders.vdf').write_text(
        fixture('libraryfolders.vdf').replace('@ROOT@', str(root)).replace('@GAMES@', str(games)))
    shutil.copy(os.path.join(DATA, 'appmanifest_1551360.acf'), str(root / 'steamapps'))
    for appid in (2483190, 3917090):
        shutil.copy(os.path.join(DATA, 'appmanifest_{}.acf'.format(appid)), str(games / 'steamapps'))
    roots = [str(root), str(tmp_path / 'nowhere')]
    if flatpak_root:
        flat, old = tmp_path / 'flat' / 'Steam', tmp_path / 'old'
        (flat / 'steamapps').mkdir(parents=True)
        (old / 'steamapps').mkdir(parents=True)
        (flat / 'steamapps' / 'libraryfolders.vdf').write_text(fixture('libraryfolders-old.vdf').replace('@OLD@', str(old)))
        roots.append(str(flat))
    return roots, str(games)


# -- the files --

def test_an_app_manifest_parses():
    state = steam_build.parse_vdf(fixture('appmanifest_3917090.acf'))['appstate']
    assert state['appid'] == '3917090' and state['name'] == 'Assetto Corsa Rally'
    assert state['buildid'] == '25170642' and state['lastupdated'] == '1789214731'
    assert state['installeddepots']['3917093']['size'] == '25605434980'       # nested blocks
    assert state['userconfig'] == {'language': 'english'}


def test_keyvalues_comments_escapes_and_a_file_cut_short():
    data = steam_build.parse_vdf('// a comment\n"A" { "Path" "C:\\\\Games\\\\Steam" "q" "say \\"hi\\"" // more\n'
                                 '"inner" { "k" "v" } bare value }')
    assert data == {'a': {'path': 'C:\\Games\\Steam', 'q': 'say "hi"', 'inner': {'k': 'v'}, 'bare': 'value'}}
    assert steam_build.parse_vdf('"AppState" { "buildid" "12"')['appstate'] == {'buildid': '12'}
    assert steam_build.parse_vdf('') == {} and steam_build.parse_vdf('}}} "x"') == {}


def test_every_library_once_with_its_apps(tmp_path):
    roots, games = steam(tmp_path, flatpak_root=True)
    roots.insert(1, roots[0] + '/')                                     # one root named twice is one library
    libs = steam_build.libraries(roots)
    paths = [p for p, _ in libs]
    assert paths == [roots[0], games, str(tmp_path / 'old'), roots[-1]]
    apps = dict(libs)
    assert apps[roots[0]] == {228980, 1551360} and apps[games] == {2483190, 3917090}
    assert apps[str(tmp_path / 'old')] is None                          # the old format lists no apps: looked in


def test_the_build_is_found_in_whichever_library_holds_the_game(tmp_path):
    roots, games = steam(tmp_path)
    acr = steam_build.find('acr', roots)
    assert acr == {'appid': 3917090, 'build': '25170642', 'updated': 1789214731, 'played': 1793405804,
                   'path': os.path.join(games, 'steamapps', 'appmanifest_3917090.acf')}
    # Forza Horizon 5 and 6 send one format: the one Steam started last
    assert steam_build.find('forza-fh', roots)['appid'] == 2483190
    os.remove(os.path.join(games, 'steamapps', 'appmanifest_2483190.acf'))
    assert steam_build.find('forza-fh', roots)['build'] == '19000000'
    # not installed, not on Steam, not a game we know, no Steam at all: unknown
    assert steam_build.find('beamng', roots) is None and steam_build.find('rbr', roots) is None
    assert steam_build.find('lfs', roots) is None and steam_build.find('nope', roots) is None
    assert steam_build.find('acr', [str(tmp_path / 'nowhere')]) is None


def test_a_listed_library_without_the_manifest_or_with_a_broken_one_is_skipped(tmp_path):
    roots, games = steam(tmp_path)
    acf = os.path.join(games, 'steamapps', 'appmanifest_3917090.acf')
    with open(acf, 'w') as f:
        f.write('"AppState" { "appid" "3917090" "buildid" "0" }')        # being installed: no build yet
    assert steam_build.find('acr', roots) is None
    os.remove(acf)
    assert steam_build.find('acr', roots) is None
    # a library whose list lacks the app is not read (no read per library per app through the host)
    seen = []

    def read(path):
        seen.append(os.path.basename(path))
        return steam_build.read_text(path, host=False)
    steam_build.find('forza-fh', roots, read)
    assert 'appmanifest_1551360.acf' in seen and seen.count('appmanifest_2483190.acf') == 1


def test_a_root_seen_under_two_names_is_read_once(tmp_path):
    roots, games = steam(tmp_path)
    link = tmp_path / 'home' / '.steam'
    link.mkdir()
    (link / 'steam').symlink_to(roots[0])
    reads = []

    def read(path):                             # the sandbox resolves no symlink of the host's: realpath cannot help
        reads.append(path)
        return steam_build.read_text(path, host=False)
    libs = steam_build.libraries([roots[0], str(link / 'steam')], read)
    assert [p for p, _ in libs] == [roots[0], games] and len(reads) == 2
    steam_build.find('acr', [roots[0], str(link / 'steam')], read)
    assert sum(p.endswith('appmanifest_3917090.acf') for p in reads) == 1


def test_unreadable_files_mean_an_unknown_build(tmp_path):
    roots, _ = steam(tmp_path)
    assert steam_build.find('acr', roots, read=lambda path: None) is None


def test_inside_the_flatpak_the_host_reads_what_the_sandbox_cannot(tmp_path, monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[-1].endswith('present.vdf'):
            return subprocess.CompletedProcess(cmd, 0, stdout=b'"x" { "y" "1" }', stderr=b'')
        return subprocess.CompletedProcess(cmd, 1, stdout=b'', stderr=b"head: cannot open 'x' for reading\n")
    monkeypatch.setattr(steam_build.subprocess, 'run', run)
    assert steam_build.read_text(str(tmp_path / 'present.vdf'), host=True) == '"x" { "y" "1" }'
    assert calls[0][:2] == ['flatpak-spawn', '--host'] and calls[0][-1] == str(tmp_path / 'present.vdf')
    assert steam_build.read_text(str(tmp_path / 'absent.vdf'), host=True) is None
    assert steam_build.read_text(str(tmp_path / 'absent.vdf'), host=False) is None and len(calls) == 2
    # a file the sandbox can read is read directly
    (tmp_path / 'here.vdf').write_text('"a" "b"')
    assert steam_build.read_text(str(tmp_path / 'here.vdf'), host=True) == '"a" "b"' and len(calls) == 2

    # flatpak-spawn itself failing (no portal) is not tried again for a while
    def broken(cmd, **kw):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, 3)
    monkeypatch.setattr(steam_build.subprocess, 'run', broken)
    assert steam_build.read_text(str(tmp_path / 'present.vdf'), host=True) is None
    assert steam_build.read_text(str(tmp_path / 'present.vdf'), host=True) is None and len(calls) == 3
    # and so is flatpak-spawn exiting with an error of its own (the portal refused); head's own is a missing file
    steam_build.clear_cache()

    def refused(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout=b'', stderr=b'Portal call failed: access denied\n')
    monkeypatch.setattr(steam_build.subprocess, 'run', refused)
    assert steam_build.read_text(str(tmp_path / 'present.vdf'), host=True) is None
    assert steam_build.read_text(str(tmp_path / 'present.vdf'), host=True) is None and len(calls) == 4


def test_lookup_reads_once_per_cache_period(monkeypatch):
    found = []
    monkeypatch.setattr(steam_build, 'find', lambda game: found.append(game) or {'appid': 3917090, 'build': '42'})
    assert steam_build.lookup('acr', now=100.0) == '42'
    assert steam_build.lookup('acr', now=100.0 + steam_build.CACHE_S / 2) == '42' and found == ['acr']
    assert steam_build.lookup('acr', now=101.0 + steam_build.CACHE_S) == '42' and found == ['acr', 'acr']
    assert steam_build.lookup('rbr') is None and steam_build.lookup(None) is None and len(found) == 2

    def strange(game):
        raise ValueError('a strange file')
    monkeypatch.setattr(steam_build, 'find', strange)
    steam_build.clear_cache()
    assert steam_build.lookup('acr') is None


# -- the store --

def test_a_session_keeps_its_build_and_the_app_reads_it_at_the_start(tmp_path):
    from tests.sim import Course, STAGE as SIM_STAGE, course_samples, feed_course
    learner = ShiftLearner(str(tmp_path / 'telemetry.db'), threaded=False)
    assert learner.log.game_build is None                       # a replay or a test: not the build installed now
    asked = []
    learner.log.game_build = lambda game: asked.append(game) or '25170642'
    feed_course(learner, course_samples(Course(SIM_STAGE))[:1500])
    learner.save()
    builds = learner.db.execute('SELECT game, game_build FROM sessions').fetchall()
    assert builds and all(b == '25170642' for _, b in builds) and asked and set(asked) == {builds[0][0]}
    learner.close()
    log = DriveLog(str(tmp_path / 'app.db'))
    assert log.game_build is steam_build.lookup                 # the app's: read from Steam
    log.close()


def _strip_build(path):
    """The file as a build before the column made it (user_version 3, sessions without game_build)."""
    db = sqlite3.connect(path)
    db.execute('PRAGMA foreign_keys = OFF')
    db.execute('PRAGMA legacy_alter_table = ON')
    old = [(r[1], r[2], r[5]) for r in db.execute('PRAGMA table_info(sessions)') if r[1] != 'game_build']
    db.execute('CREATE TABLE sessions_x ({})'.format(', '.join(
        '{} {}{}'.format(n, t, ' PRIMARY KEY' if pk else '') for n, t, pk in old)))
    db.execute('INSERT INTO sessions_x SELECT {0} FROM sessions'.format(', '.join(n for n, _, _ in old)))
    db.execute('DROP TABLE sessions')
    db.execute('ALTER TABLE sessions_x RENAME TO sessions')
    db.commit()
    db.close()


def _columns(db, table):
    return [r[1] for r in db.execute('PRAGMA table_info({})'.format(table))]


def test_an_older_file_gets_the_column_without_a_new_version(tmp_path):
    path = str(tmp_path / 'telemetry.db')
    store = open_store(path)
    car = store.car_id('p', 'acr/car', 'acr', 'Car', {})
    session = store.start_session('p', car, 'acr', 10.0)
    store.commit()
    store.db.close()
    _strip_build(path)
    reader = open_reader(path)                                  # before the writer opened it: unknown, no error
    assert reader.run_build(1) is None and reader.stage_pb_builds(STAGE, car, '1') == {'pb': None, 'build': None}
    reader.db.close()
    store = open_store(path)
    fresh = open_store(str(tmp_path / 'fresh.db'))
    assert _columns(store.db, 'sessions') == _columns(fresh.db, 'sessions')
    assert store.db.execute('PRAGMA user_version').fetchone()[0] == 3        # an older build still opens it
    assert store.db.execute('SELECT game_build FROM sessions WHERE id = ?', (session,)).fetchone() == (None,)
    store.db.close()
    assert _columns(open_store(path).db, 'sessions') == _columns(fresh.db, 'sessions')   # once


def test_the_upgrade_from_version_2_has_the_column_too(tmp_path):
    from tests.test_store import v2_database
    path = str(tmp_path / 'telemetry.db')
    v2_database(path)
    store = open_store(path)
    assert _columns(store.db, 'sessions') == _columns(open_store(str(tmp_path / 'fresh.db')).db, 'sessions')
    assert store.db.execute('SELECT game_build FROM sessions').fetchall() == [(None,)]


def drive(store, car, started, result, build, run_class='clean', stage=STAGE, finished=1):
    session = store.start_session('p', car, 'acr', started, stage=stage, game_build=build)
    run = store.start_run(session, 1, started + 1, stage=stage)
    store.end_run(run, ended=started + 200, distance=5000.0, finished=finished, result_time=result,
                  run_class=run_class)
    return run


def test_the_pb_on_a_build_beside_the_all_time_pb(tmp_path):
    store = open_store(str(tmp_path / 'telemetry.db'))
    car = store.car_id('p', 'acr/car', 'acr', 'Car', {})
    other = store.car_id('p', 'acr/other', 'acr', 'Other', {})
    old = drive(store, car, 1000.0, 141.6, None)                # before builds were kept
    drive(store, car, 2000.0, 150.0, '100')
    best_100 = drive(store, car, 3000.0, 145.2, '100', run_class='off')     # a PB with offs counts
    drive(store, car, 4000.0, 120.0, '200', run_class='partial')            # a partial run is no finish
    drive(store, car, 5000.0, 130.0, '200', finished=0)
    on_200 = drive(store, car, 6000.0, 143.0, '200')
    drive(store, other, 7000.0, 100.0, '200')                   # another car
    drive(store, car, 8000.0, 100.0, '200', stage='acr:wales:other')        # another stage
    store.commit()
    assert store.stage_pb_builds(STAGE, car, '200') == {'pb': {'run': old, 'time': 141.6, 'build': None},
                                                        'build': {'run': on_200, 'time': 143.0}}
    assert store.stage_pb_builds(STAGE, car, '100')['build'] == {'run': best_100, 'time': 145.2}
    assert store.stage_pb_builds(STAGE, car, '300')['build'] is None
    assert store.stage_pb_builds(STAGE, car)['build'] is None
    assert store.stage_pb_builds('acr:nowhere', car, '200') == {'pb': None, 'build': None}
    assert store.run_build(on_200) == '200' and store.run_build(old) is None and store.run_build(999) is None
    # the Run view's header: shown where the run's build is known and the all-time PB was set on another
    # an all-time PB of no known build (set before builds were kept) is not another build: nothing to say
    assert run_analysis.build_pb(store, store.run(on_200)) is None
    assert run_analysis.build_pb(store, store.run(old)) is None              # its build is unknown
    quickest = drive(store, car, 9000.0, 140.0, '200')
    store.commit()
    assert store.stage_pb_builds(STAGE, car, '200')['pb'] == {'run': quickest, 'time': 140.0, 'build': '200'}
    assert run_analysis.build_pb(store, store.run(on_200)) is None           # the PB is on this build
    assert run_analysis.build_pb(store, store.run(best_100)) == {'pb': 140.0, 'pb_build': '200', 'build': '100',
                                                                 'time': 145.2, 'run': best_100}
    assert run_analysis.head(store, best_100)['build_pb']['time'] == 145.2


def test_a_run_moved_to_a_session_of_its_own_keeps_its_build(tmp_path):
    store = open_store(str(tmp_path / 'telemetry.db'))
    car = store.car_id('p', 'acr/car', 'acr', 'Car', {})
    other = store.car_id('p', 'acr/other', 'acr', 'Other', {})
    run = drive(store, car, 1000.0, 141.6, '100')
    [new] = store.split_runs([run], other)
    assert store.db.execute('SELECT game_build FROM sessions WHERE id = ?', (new,)).fetchone() == ('100',)


# -- the merge tool --

def _merge_db():
    spec = importlib.util.spec_from_file_location('merge_db', os.path.join(HERE, '..', 'scripts',
                                                                           'merge-telemetry-db.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_merge_tool_carries_the_build(tmp_path):
    merge_db = _merge_db()
    target, source = str(tmp_path / 'target.db'), str(tmp_path / 'source.db')
    t = open_store(target)
    car = t.car_id('p', 'acr/car', 'acr', 'Car', {'ratios': {'1': [100.0]}})
    drive(t, car, 1000.0, 150.0, None)                          # the same session, its build unknown here
    t.commit()
    t.db.close()
    s = open_store(source)
    car = s.car_id('p', 'acr/car', 'acr', 'Car', {'ratios': {'1': [100.0]}})
    drive(s, car, 1000.0, 150.0, '100')
    drive(s, car, 5000.0, 140.0, '200')                         # one the target has not
    s.commit()
    s.db.close()
    old = str(tmp_path / 'old.db')                              # and a source from before the column
    shutil.copy(source, old)
    _strip_build(old)
    lines = []
    merge_db.run_merge(target, source, dry_run=False, workdir=str(tmp_path / 'work'),
                       backup_dir=str(tmp_path / 'backup'), out=lines.append)
    db = sqlite3.connect(target)
    assert db.execute('SELECT started, game_build FROM sessions ORDER BY started').fetchall() == [
        (1000.0, '100'), (5000.0, '200')]
    db.close()
    lines = []
    merge_db.run_merge(target, old, dry_run=True, workdir=str(tmp_path / 'work2'), out=lines.append)
    assert any('nothing to merge' in line for line in lines)    # brought to this schema, nothing lost or added


def test_the_pb_is_ranked_over_every_run_however_many(tmp_path):
    """coach_context.stage_pb_id and the PB on a build are one definition, over all of the car's runs (an old PB is
    never hidden by newer runs)."""
    from oversteer import coach_context
    store = open_store(str(tmp_path / 'telemetry.db'))
    car = store.car_id('p', 'acr/car', 'acr', 'Car', {})
    pb = drive(store, car, 1000.0, 130.0, '100')
    for i in range(80):
        drive(store, car, 2000.0 + i * 300, 140.0 + i % 7, '200')
    tie = drive(store, car, 50000.0, 140.0, '200')                       # of equal times, the newer
    store.commit()
    assert coach_context.stage_pb_id(store, STAGE, car) == pb == store.stage_pb_builds(STAGE, car)['pb']['run']
    assert store.stage_pb_builds(STAGE, car, '200')['build'] == {'run': tie, 'time': 140.0}
    assert coach_context.stage_pb_id(store, STAGE, None) == pb                # any car's
    assert coach_context.stage_pb_id(store, 'acr:nowhere', car) is None
    plan = ' '.join(r[-1] for r in store.db.execute(
        'EXPLAIN QUERY PLAN SELECT r.id FROM runs r JOIN sessions s ON r.session = s.id WHERE r.stage = ? '
        'AND s.car = ?', (STAGE, car)).fetchall())
    assert 'runs_stage' in plan                                         # by the stage's index, not a scan


def test_the_header_text_of_the_pb_on_a_build():
    from oversteer.gtk_run_view import _build_pb
    head = {'build_pb': {'pb': 141.6, 'time': 143.0}}
    assert _build_pb(head, {'role': 'pb', 'time': 141.6}) == 'on this build 2:23.0'
    assert _build_pb(head, {'role': 'best', 'time': 150.0}) == 'PB 2:21.6 · on this build 2:23.0'
    assert _build_pb(head, None) == 'PB 2:21.6 · on this build 2:23.0' and _build_pb({'build_pb': None}, None) == ''


@pytest.mark.skipif(shutil.which('node') is None, reason='no node')
def test_the_web_header_says_the_same():
    page = open(os.path.join(HERE, '..', 'data', 'telemetry', 'web', 'index.html')).read()
    start = page.index('function rvBuildPb(H, ref) {')
    body = page[start:page.index('\n}\n', start) + 3]
    script = ('const clock = (t) => Math.floor(t / 60) + ":" + (t % 60).toFixed(1).padStart(4, "0");\n' + body +
              'const H = {build_pb: {pb: 141.6, time: 143.0}};\n'
              'console.log(JSON.stringify([rvBuildPb(H, {role: "pb", time: 141.6}), rvBuildPb(H, {role: "best", '
              'time: 150}), rvBuildPb(H, null), rvBuildPb({build_pb: null}, null)]));')
    out = subprocess.run(['node', '-e', script], stdout=subprocess.PIPE, check=True).stdout.decode()
    assert json.loads(out) == [' · on this build 2:23.0', ' · PB 2:21.6 · on this build 2:23.0',
                               ' · PB 2:21.6 · on this build 2:23.0', '']
