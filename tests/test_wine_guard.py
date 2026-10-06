import subprocess

from oversteer.proxy import manager


def make_proc(tmp_path, comms):
    for pid, comm in comms.items():
        d = tmp_path / str(pid)
        d.mkdir()
        (d / 'comm').write_text(comm + '\n')
    (tmp_path / 'self').mkdir()  # no comm file: must be skipped


def test_proc_scan_finds_wineserver(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, 'in_flatpak', lambda: False)
    make_proc(tmp_path, {1: 'systemd', 2: 'wineserver'})
    assert manager.wine_running(proc=str(tmp_path)) is True


def test_proc_scan_finds_wineserver64(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, 'in_flatpak', lambda: False)
    make_proc(tmp_path, {7: 'wineserver64'})
    assert manager.wine_running(proc=str(tmp_path)) is True


def test_proc_scan_without_wine(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, 'in_flatpak', lambda: False)
    make_proc(tmp_path, {1: 'systemd', 2: 'bash'})
    assert manager.wine_running(proc=str(tmp_path)) is False


def test_proc_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(manager, 'in_flatpak', lambda: False)
    assert manager.wine_running(proc=str(tmp_path / 'nope')) is False


def _flatpak(monkeypatch, run):
    monkeypatch.setattr(manager, 'in_flatpak', lambda: True)
    monkeypatch.setattr(manager.subprocess, 'run', run)


def test_flatpak_pgrep_match(monkeypatch):
    calls = []
    def run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0)
    _flatpak(monkeypatch, run)
    assert manager.wine_running() is True
    assert calls[0][0][:3] == ['flatpak-spawn', '--host', 'pgrep']
    assert calls[0][1]['timeout'] == 2


def test_flatpak_pgrep_no_match(monkeypatch):
    _flatpak(monkeypatch, lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
    assert manager.wine_running() is False


def test_flatpak_pgrep_raises(monkeypatch):
    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 2)
    _flatpak(monkeypatch, run)
    assert manager.wine_running() is False
    def run2(cmd, **kw):
        raise FileNotFoundError('flatpak-spawn')
    monkeypatch.setattr(manager.subprocess, 'run', run2)
    assert manager.wine_running() is False
