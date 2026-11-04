"""The build id and the settings snapshot: the capture and sidecar headers carry them, an old sidecar still loads, a
changed setting is logged, the build falls back to "unknown"."""
import gzip
import json
import logging
import struct

from oversteer import buildinfo, ffb_engine, ffb_record, telemetry_capture


def test_build_fallback():
    assert buildinfo.clean('') == 'unknown'
    assert buildinfo.clean(None) == 'unknown'
    assert buildinfo.clean('@BUILD@') == 'unknown'
    assert buildinfo.clean('abc123def-dirty') == 'abc123def-dirty'


def test_capture_header_has_build(tmp_path, monkeypatch):
    monkeypatch.setattr(buildinfo, 'BUILD', 'abc123def')
    path = str(tmp_path / 'c.ovcap.gz')
    telemetry_capture.CaptureWriter(path, version='9.9')._file.close()
    meta, _ = telemetry_capture.read_capture(path)
    assert meta['build'] == 'abc123def' and meta['oversteer'] == '9.9'


def test_sidecar_header_and_change(tmp_path, monkeypatch, caplog):
    engine = ffb_engine.FfbEngine()
    monkeypatch.setattr(ffb_record, 'settings_source', engine.settings_snapshot)
    path = str(tmp_path / 'c.ovcap.gz.ffb.gz')
    sampler = ffb_record.FfbSampler(path, 100.0, {'build': 'abc123def', 'oversteer': '9.9'})
    sampler.start()
    try:
        ffb_record.check(100.5)                 # nothing changed
        assert not sampler.changes
        engine._strength = 0.7
        with caplog.at_level(logging.INFO):
            ffb_record.check(102.5)
        assert [(round(t, 1), k, new) for t, k, old, new in sampler.changes] == [(2.5, 'strength', 70.0)]
        assert 'strength' in caplog.text and '2.50 s' in caplog.text
    finally:
        sampler.stop()
    meta, _ = ffb_record.load(path)
    assert meta['build'] == 'abc123def'
    profile, learnt = meta['settings']['profile'], meta['settings']['learnt']
    assert profile['strength'] == 100.0                       # the value at the start
    for key in ('knee', 'mapping', 'drive_pull', 'kick', 'rim_mm', 'car_wheel_mm', 'power_class', 'texture_level',
                'texture_parts', 'column', 'align', 'forza_model', 'column_damping'):
        assert key in profile
    for key in ('car', 'ratio', 'lock', 'trail_m', 'forza_mass_kg', 'rbr_strength'):
        assert key in learnt


def test_old_sidecar_loads(tmp_path):
    path = str(tmp_path / 'old.ffb.gz')
    with gzip.open(path, 'wb') as f:
        f.write(ffb_record.MAGIC)
        f.write(json.dumps({'capture': 'x', 'channels': ffb_record.CHANNELS, 'oversteer': '0.17.0'}).encode() + b'\n')
        f.write(ffb_record.RECORD.pack(1.5, ffb_record.CHANNELS['ext_force'], 0.25, 0, 0, 0, 0))
    meta, records = ffb_record.load(path)
    assert 'build' not in meta and 'settings' not in meta
    assert list(records) == [(1.5, 'ext_force', (0.25, 0.0, 0.0, 0.0, 0.0))]
