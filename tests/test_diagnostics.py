"""The diagnostics log, the excepthooks, the wheel sidecar and the bundle. No wheel is opened."""
import io
import json
import logging
import os
import sys
import threading
import zipfile

import pytest

from oversteer import diagnostics as dg
from oversteer import telemetry_capture as tc


@pytest.fixture
def clean_logging():
    root = logging.getLogger()
    before = list(root.handlers), root.level
    hooks = sys.excepthook, threading.excepthook, sys.unraisablehook
    yield
    for h in list(root.handlers):
        if h not in before[0]:
            root.removeHandler(h)
            h.close()
    root.setLevel(before[1])
    sys.excepthook, threading.excepthook, sys.unraisablehook = hooks
    dg._file_handler = dg._stream_handler = None


def test_log_folder_native_and_flatpak():
    assert dg.log_dir({}, '/home/u') == '/home/u/.local/share/oversteer/logs'
    assert dg.log_dir({'XDG_DATA_HOME': '/x'}, '/home/u') == '/x/oversteer/logs'
    flatpak = {'FLATPAK_ID': 'io.github.berarma.Oversteer'}
    assert dg.log_path(flatpak, '/home/u') == \
        '/home/u/.var/app/io.github.berarma.Oversteer/data/oversteer/logs/oversteer.log'
    # the sandbox's own XDG_DATA_HOME is the same place
    both = dict(flatpak, XDG_DATA_HOME='/home/u/.var/app/io.github.berarma.Oversteer/data')
    assert dg.log_path(both, '/home/u') == dg.log_path(flatpak, '/home/u')


def test_setup_levels_format_and_rotation(tmp_path, clean_logging):
    path = dg.setup(folder=str(tmp_path), stream=None)
    assert path == str(tmp_path / 'oversteer.log')
    logging.debug("hidden")
    logging.info("shown")
    handler = dg._file_handler
    assert handler.maxBytes == 2 * 1024 * 1024 and handler.backupCount == 4     # 5 files
    handler.flush()
    text = open(path).read()
    assert "shown" in text and "hidden" not in text
    assert "INFO [MainThread] test_diagnostics: shown" in text
    dg.set_debug(True)
    logging.debug("now hidden no more")
    handler.flush()
    assert "now hidden no more" in open(path).read()
    # rotation: a small limit rolls the file over, five files at most
    handler.maxBytes = 200
    for i in range(80):
        logging.info("filler line %d with some padding to fill the file", i)
    names = sorted(os.listdir(tmp_path))
    assert names == ['oversteer.log', 'oversteer.log.1', 'oversteer.log.2', 'oversteer.log.3', 'oversteer.log.4']


def test_setup_stream_warns_only_unless_debug(tmp_path, clean_logging):
    out = io.StringIO()
    dg.setup(folder=str(tmp_path), stream=out)
    logging.info("quiet")
    logging.warning("loud")
    assert "loud" in out.getvalue() and "quiet" not in out.getvalue()
    out2 = io.StringIO()
    dg.setup(debug=True, folder=str(tmp_path), stream=out2)
    logging.debug("chatty")
    assert "chatty" in out2.getvalue()


def test_setup_without_file_still_passes_debug(clean_logging):
    buf = io.StringIO()
    assert dg.setup(debug=True, stream=buf, to_file=False) is None
    logging.debug("proxy debug line")
    assert "proxy debug line" in buf.getvalue()


def test_setup_survives_an_unwritable_folder(tmp_path, clean_logging):
    blocked = tmp_path / 'f'
    blocked.write_text('x')
    assert dg.setup(folder=str(blocked / 'logs'), stream=None) is None


def test_no_log_file_for_the_proxy_service(tmp_path, clean_logging):
    assert dg.setup(folder=str(tmp_path), stream=None, to_file=False) is None
    assert os.listdir(tmp_path) == []


def test_debug_preference(tmp_path):
    ini = tmp_path / 'config.ini'
    assert dg.read_debug_preference(str(ini)) is False
    ini.write_text("[DEFAULT]\ndiagnostics_debug = 1\n")
    assert dg.read_debug_preference(str(ini)) is True


def test_excepthooks_log_tracebacks(tmp_path, clean_logging):
    path = dg.setup(folder=str(tmp_path), stream=None)
    chained = []
    sys.excepthook = lambda *a: chained.append('main')                 # what was installed before still runs
    threading.excepthook = lambda a: chained.append('thread')
    dg.install_excepthooks()
    try:
        raise ValueError("main boom")
    except ValueError:
        sys.excepthook(*sys.exc_info())

    def work():
        raise RuntimeError("thread boom")
    t = threading.Thread(target=work, name='worker-x')
    t.start()
    t.join()
    dg._file_handler.flush()
    text = open(path).read()
    assert "uncaught exception" in text and "ValueError: main boom" in text and "Traceback" in text
    assert "uncaught exception in thread worker-x" in text and "RuntimeError: thread boom" in text
    assert chained == ['main', 'thread']


def _fake_capture(folder, name, game_packet=None):
    path = os.path.join(folder, name + tc.SUFFIX)
    with tc.CaptureWriter(path, version='t') as w:
        w.write(10.0, ('127.0.0.1', 0), game_packet or b'\0' * 12)
    return path


class FakeWheel:
    """A stand-in for the wheel's evdev node: events handed out batch by batch."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.closed = False

    def info(self):
        return {'device': 'Fake wheel', 'axes': {0: [0, 65535, 32768], 1: [0, 255, 0]}}

    def read(self, timeout):
        if not self.batches:
            raise OSError("gone")
        return self.batches.pop(0)

    def close(self):
        self.closed = True


def test_wheel_sidecar_round_trip(tmp_path):
    ticks = iter([100.5, 100.75, 101.0])
    source = FakeWheel([[(3, 0, 40000), (3, 2, 12)], [], [(1, 288, 1), (3, 0, -5)]])
    path = str(tmp_path / 'a.ovcap.gz.wheel.gz')
    sampler = tc.WheelSampler(source, path, origin=100.0, header={'range': 900, 'sensitivity': 50},
                              clock=lambda: next(ticks))
    assert sampler.pump() and sampler.pump() and sampler.pump()
    assert not sampler.pump()                       # the wheel went away
    sampler.stop()
    assert source.closed
    meta, records = tc.read_wheel(path)
    assert meta['range'] == 900 and meta['sensitivity'] == 50 and meta['device'] == 'Fake wheel'
    assert meta['axes']['0'] == [0, 65535, 32768] and meta['fields'] == 't type code value'
    assert list(records) == [(0.5, 3, 0, 40000), (0.5, 3, 2, 12), (1.0, 1, 288, 1), (1.0, 3, 0, -5)]


def test_wheel_sidecar_truncated_tail_and_bad_file(tmp_path):
    path = str(tmp_path / 'w.wheel.gz')
    source = FakeWheel([[(3, 0, 1)]])
    s = tc.WheelSampler(source, path, 0.0, clock=lambda: 1.0)
    s.pump()
    s.stop()
    import gzip
    raw = gzip.open(path).read()
    with gzip.open(path, 'wb') as f:
        f.write(raw + b'\x01\x02')                  # half a record
    assert len(list(tc.read_wheel(path)[1])) == 1
    bad = tmp_path / 'bad.gz'
    with gzip.open(bad, 'wb') as f:
        f.write(b'nope\n')
    with pytest.raises(ValueError):
        tc.read_wheel(str(bad))


def test_recorder_writes_the_sidecar_on_the_captures_clock(tmp_path):
    source = FakeWheel([])
    source.read = lambda timeout: (__import__('time').sleep(0.01) or [])   # idle wheel, no OSError
    rec = tc.Recorder(str(tmp_path), version='t', threaded=False, clock=lambda: 50.0,
                      wheel=lambda: (source, {'range': 540, 'sensitivity': 50}))
    rec.packet(7.0, ('127.0.0.1', 1), b'abcd')
    sampler = rec._sampler
    assert sampler is not None and sampler.origin == 7.0
    path = rec.path
    rec.close()
    assert source.closed
    meta, _records = tc.read_wheel(path + tc.WHEEL_SUFFIX)
    assert meta['range'] == 540 and meta['capture'] == os.path.basename(path) and meta['started'] == 50.0
    # the .ovcap itself is unchanged: it still reads as one
    cap_meta, packets = tc.read_capture(path)
    assert list(packets)[0][2] == b'abcd' and 'range' not in cap_meta
    assert tc.sidecars(path) == [path + tc.WHEEL_SUFFIX]


def test_recorder_without_a_wheel_or_with_an_unreadable_one(tmp_path):
    rec = tc.Recorder(str(tmp_path / 'a'), threaded=False, wheel=lambda: None)
    rec.packet(1.0, ('127.0.0.1', 1), b'x')
    rec.close()
    assert not [f for f in os.listdir(tmp_path / 'a') if f.endswith(tc.WHEEL_SUFFIX)]

    def boom():
        raise OSError("no node")
    rec = tc.Recorder(str(tmp_path / 'b'), threaded=False, wheel=boom)
    rec.packet(1.0, ('127.0.0.1', 1), b'x')
    rec.close()
    assert rec.error is None and len(os.listdir(tmp_path / 'b')) == 1


def test_pruning_takes_the_wheel_sidecar_with_the_capture(tmp_path):
    path = _fake_capture(str(tmp_path), 'old')
    (tmp_path / ('old' + tc.SUFFIX + tc.WHEEL_SUFFIX)).write_bytes(b'x')
    assert tc.prune(str(tmp_path), 0) == [path]
    assert os.listdir(tmp_path) == []


def test_bundle_contents(tmp_path):
    caps = tmp_path / 'captures'
    caps.mkdir()
    paths = []
    for i, name in enumerate(['a', 'b', 'c', 'd']):
        p = _fake_capture(str(caps), name)
        os.utime(p, (1.7e9 + i, 1.7e9 + i))
        paths.append(p)
    for p in paths:
        with open(p + tc.WHEEL_SUFFIX, 'wb') as f:
            f.write(b'w')
    (caps / 'telemetry.db').write_bytes(b'SECRET')
    logs = tmp_path / 'logs'
    logs.mkdir()
    (logs / 'oversteer.log').write_text('line\n')
    (logs / 'oversteer.log.1').write_text('old\n')
    config = tmp_path / 'config.ini'
    config.write_text("[DEFAULT]\nweb_token = hunter2\nlocale = en\n")
    out = tmp_path / 'bundle.zip'
    n = dg.make_bundle(str(out), '9.9', str(caps), str(config), count=2, logs=dg.log_files(str(logs)))
    with zipfile.ZipFile(out) as z:
        names = sorted(z.namelist())
        assert names == ['captures/c.ovcap.gz', 'captures/c.ovcap.gz.wheel.gz', 'captures/d.ovcap.gz',
                         'captures/d.ovcap.gz.wheel.gz', 'config.ini', 'logs/oversteer.log', 'logs/oversteer.log.1',
                         'versions.txt']
        assert n == len(names)
        everything = b''.join(z.read(x) for x in names)
        versions = z.read('versions.txt').decode()
        config_text = z.read('config.ini').decode()
    assert b'SECRET' not in everything and 'hunter2' not in config_text
    assert 'web_token = (removed)' in config_text and 'locale = en' in config_text
    assert 'Oversteer: 9.9' in versions and 'Python:' in versions
    assert dg.bundle_size(str(caps), count=2, logs=dg.log_files(str(logs))) > 0
    assert dg.human_size(512) == '512 bytes' and dg.human_size(3 * 1024 * 1024) == '3.0 MB'


def test_bundle_cli_flag(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    from oversteer import application
    from oversteer.application import Application
    monkeypatch.setattr(application, 'save_config_path', lambda name: str(tmp_path))
    app = Application('1.2.3', str(tmp_path), str(tmp_path))
    out = tmp_path / 'b.zip'
    monkeypatch.setattr(dg, 'setup', lambda **kw: None)
    monkeypatch.setattr(dg, 'install_excepthooks', lambda: None)
    with pytest.raises(SystemExit) as e:
        app.run(['oversteer', '--diagnostics-bundle', str(out)])
    assert e.value.code == 0
    with zipfile.ZipFile(out) as z:
        assert 'versions.txt' in z.namelist()


def test_capture_size_counts_the_wheel_sidecar(tmp_path):
    path = _fake_capture(str(tmp_path), 'a')
    plain = os.path.getsize(path)
    (tmp_path / ('a' + tc.SUFFIX + tc.WHEEL_SUFFIX)).write_bytes(b'x' * 100)
    assert tc.list_captures(str(tmp_path))[0][1] == plain + 100
    assert tc.capture_summary(str(tmp_path))[1] == plain + 100


def test_recorder_closes_the_wheel_when_the_sampler_fails(tmp_path, monkeypatch):
    source = FakeWheel([])

    def boom(*args, **kwargs):
        raise OSError("no sidecar")
    monkeypatch.setattr(tc, 'WheelSampler', boom)
    rec = tc.Recorder(str(tmp_path), threaded=False, wheel=lambda: (source, {}))
    rec.packet(1.0, ('127.0.0.1', 1), b'x')
    rec.close()
    assert source.closed


def test_recorder_finishes_the_sampler_when_start_fails(tmp_path, monkeypatch):
    source = FakeWheel([])

    def boom(self):
        raise RuntimeError("no thread")
    monkeypatch.setattr(tc.WheelSampler, 'start', boom)
    rec = tc.Recorder(str(tmp_path), threaded=False, wheel=lambda: (source, {}))
    rec.packet(1.0, ('127.0.0.1', 1), b'x')
    assert rec._sampler is None
    rec.close()
    assert source.closed


def test_web_log_escapes_control_characters(clean_logging, caplog):
    from oversteer import telemetry_web as tw
    h = tw.Handler.__new__(tw.Handler)
    with caplog.at_level(logging.DEBUG):
        h.log_message("%s", "GET /a\nFAKE line\x1b[0m\x7f")
    assert "\n" not in caplog.messages[-1]
    assert "\\x0aFAKE line\\x1b[0m\\x7f" in caplog.messages[-1]


def test_web_log_masks_the_pairing_token(clean_logging, caplog):
    from oversteer.telemetry_web import Handler
    caplog.set_level(logging.DEBUG)
    Handler.log_message(None, '"%s" %s %s', 'GET /?pair=SECRETX&a=1 HTTP/1.1', '200', '-')
    assert 'SECRETX' not in caplog.text and 'pair=(removed)&a=1' in caplog.text


def test_bundle_logs_carry_neither_the_token_nor_the_pair_link(tmp_path):
    logs = tmp_path / 'logs'
    logs.mkdir()
    (logs / 'oversteer.log').write_text('x hunter2 y\nGET /?pair=SECRETX HTTP/1.1\n')
    config = tmp_path / 'config.ini'
    config.write_text("[DEFAULT]\nweb_token = hunter2\n")
    out = tmp_path / 'b.zip'
    dg.make_bundle(str(out), '1', str(tmp_path / 'none'), str(config), logs=dg.log_files(str(logs)))
    with zipfile.ZipFile(out) as z:
        text = z.read('logs/oversteer.log').decode()
    assert 'hunter2' not in text and 'SECRETX' not in text and 'pair=(removed)' in text


def test_root_level_follows_the_handlers(clean_logging, tmp_path):
    dg.setup(folder=str(tmp_path), stream=None)
    assert logging.getLogger().level == logging.INFO
    dg.set_debug(True)
    assert logging.getLogger().level == logging.DEBUG
    dg.set_debug(False)
    assert logging.getLogger().level == logging.INFO


def test_redacted_log_masks_ipv4_but_keeps_loopback():
    text = b'from 192.168.1.42 and 10.0.7.9, local 127.0.0.1, version 1.2.3\n'
    out = dg.redacted_log(text)
    assert b'192.168.x.x' in out and b'10.0.x.x' in out
    assert b'192.168.1.42' not in out and b'10.0.7.9' not in out
    assert b'127.0.0.1' in out and b'1.2.3' in out


def test_bundle_count_is_what_was_added(tmp_path):
    logs = tmp_path / 'logs'
    logs.mkdir()
    (logs / 'oversteer.log').write_text('x\n')
    out = tmp_path / 'b.zip'
    n = dg.make_bundle(str(out), '1', str(tmp_path / 'none'), None, logs=dg.log_files(str(logs)))
    with zipfile.ZipFile(out) as z:
        assert sorted(z.namelist()) == ['logs/oversteer.log', 'versions.txt']
    assert n == 2


def test_file_debug_leaves_the_stream_at_warning(tmp_path, clean_logging):
    out = io.StringIO()
    path = dg.setup(folder=str(tmp_path), stream=out, file_debug=True)
    logging.debug("file only")
    dg._file_handler.flush()
    assert "file only" in open(path).read()
    assert "file only" not in out.getvalue()
