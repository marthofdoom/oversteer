"""The coaching derivations: the database holds together, covers every
Sample field and every metric the coach writes, and answers per game."""
import os
import re

from oversteer import coaching_matrix as m
from oversteer.telemetry_formats import Sample

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')


def _statuses(kind):
    return set(m.load()['statuses'][kind])


def test_it_loads_with_its_schema_version():
    data = m.load()
    assert data['schema_version'] == m.SCHEMA_VERSION
    assert data['channels'] and data['games'] and data['aspects']
    assert m.games()[0] == data['reference_game'] == 'acr'


def test_the_structure_validates():
    data = m.load()
    for name, c in data['channels'].items():
        assert {'kind', 'unit', 'description', 'rate'} <= set(c), name
        assert c['kind'] in ('sample', 'derived', 'rig', 'game'), name
        for via in c.get('derive_from') or []:
            for token in via:
                if not token.startswith('static:'):
                    assert token in data['channels'], (name, token)
    for name, c in data['rig']['channels'].items():
        assert data['channels'][name]['kind'] == 'rig' and c['status'] == 'confirmed'
    for game, entry in data['games'].items():
        assert {'name', 'format', 'channels', 'static', 'gaps'} <= set(entry), game
        for name, c in entry['channels'].items():
            assert name in data['channels'], (game, name)
            assert c['status'] in _statuses('channel'), (game, name)
            if c['status'] == 'confirmed':
                assert c.get('evidence'), (game, name)          # a confirmation cites its capture
            for token in c.get('via') or []:
                assert token.startswith('static:') or token in data['channels'], (game, token)
        for name, s in entry['static'].items():
            assert s['status'] in _statuses('static'), (game, name)
    ids = [a['id'] for a in data['aspects']]
    assert len(ids) == len(set(ids))
    for a in data['aspects']:
        assert {'id', 'name', 'category', 'description', 'derivations', 'fallbacks', 'gates', 'outputs',
                'validation', 'implementation', 'acr'} <= set(a), a['id']
        assert a['derivations'] and a['outputs']
        assert {'discipline', 'surface'} <= set(a['gates']), a['id']
        assert a['validation']['status'] in _statuses('validation'), a['id']
        assert a['implementation']['status'] in _statuses('implementation'), a['id']
        assert a['acr']['coverage'] in _statuses('acr_coverage'), a['id']
        dids = [d['id'] for d in a['derivations']]
        assert len(dids) == len(set(dids)), a['id']
        for d in a['derivations']:
            assert d['formula'] and d['status'] in _statuses('derivation'), (a['id'], d['id'])
        if a['validation']['status'] == 'validated':
            assert a['validation'].get('evidence'), a['id']
        if a['implementation']['status'] == 'implemented':
            assert any(d['status'] == 'implemented' for d in a['derivations']), a['id']


def test_every_reference_exists():
    data = m.load()
    ids = {a['id'] for a in data['aspects']}
    statics = {name for entry in data['games'].values() for name in entry['static']}
    for a in data['aspects']:
        for d in a['derivations']:
            for part in ('requires', 'optional'):
                need = d.get(part) or {}
                for name in need.get('channels') or []:
                    assert name in data['channels'], (a['id'], d['id'], name)
                for name in need.get('aspects') or []:
                    assert name in ids and name != a['id'], (a['id'], d['id'], name)
                for name in need.get('static') or []:
                    assert name in statics, (a['id'], d['id'], name)


def test_every_sample_field_is_catalogued():
    data = m.load()
    samples = {name for name, c in data['channels'].items() if c['kind'] == 'sample'}
    assert set(Sample.__slots__) == samples


def _written_metrics():
    names = set()
    for module in ('coach.py', 'drive_log.py'):
        with open(os.path.join(ROOT, 'oversteer', module), encoding='utf-8') as f:
            names |= set(re.findall(r"'name': '([a-z_.0-9]+)'", f.read()))
    return names


def test_every_metric_the_coach_writes_maps_to_an_aspect():
    written = _written_metrics()
    assert {'shift.in_band', 'limiter.held', 'counter_steer', 'corner.loss'} <= written     # the grep works
    mapped = m.metric_aspects()
    assert written - set(mapped) == set()
    # and nothing claims a metric that is no longer written
    assert set(mapped) - written == set()


def test_acr_can_do_what_it_is_marked_for():
    usable = {a['id']: d for a, d, _ in m.available_aspects('acr')}
    for a in m.aspects():
        coverage = a['acr']['coverage']
        if coverage in ('validated', 'implemented'):
            assert a['id'] in usable, (a['id'], m.why_unavailable('acr', a['id']))
            assert usable[a['id']]['status'] == 'implemented', a['id']
        elif coverage == 'impossible':
            assert a['id'] not in usable and m.why_unavailable('acr', a['id']), a['id']
    assert m.why_unavailable('acr', 'shift.upshift') == []
    assert any('puddle' in line for line in m.why_unavailable('acr', 'context.wet'))


def test_acr_channels_as_the_captures_found():
    acr = m.game_channels('acr')
    for name in ('steer', 'yaw_rate', 'vel', 'accel', 'clutch', 'lap_distance', 'wheel_rot', 'pos'):
        assert acr[name]['status'] == 'confirmed', name
    for name in ('handbrake', 'slip_angle', 'susp_norm', 'surface_grip', 'game_time'):
        assert acr[name]['status'] == 'absent', name
    # derived from what it sends, and from the game's files
    assert acr['wheel_speed']['status'] == 'derivable'
    assert acr['gears'] == dict(acr['gears'], status='derivable', via=['static:car_data'])
    assert acr['body_slip']['via'] == ['vel']
    assert acr['shift_press']['source'] == 'rig'


def test_derivation_order_and_optional_channels():
    by_id = {a['id']: (d, missing) for a, d, missing in m.available_aspects('beamng')}
    # OutGauge: no yaw rate and no position, so no corners, and so no counter-steer
    assert 'context.corners' not in by_id and 'balance.counter_steer' not in by_id
    assert by_id['engine.limiter'][0]['id'] == 'trace'
    assert 'max_rpm' in by_id['engine.limiter'][1] or m.game_channels('beamng')['max_rpm']['status'] == 'derivable'
    # Forza has no stage table: the surface comes from measuring
    forza = {a['id']: d['id'] for a, d, _ in m.available_aspects('forza-fh')}
    assert forza['context.surface'] == 'measured'


def test_unknown_games_and_aspects_give_nothing():
    assert m.game_channels('no-such-game') == {}
    assert m.available_aspects('no-such-game') == []
    assert m.why_unavailable('no-such-game', 'shift.upshift') is None
    assert m.why_unavailable('acr', 'no.such.aspect') is None
    assert m.static_data('no-such-game') == {}
    assert m.aspect('no.such.aspect') is None


def test_a_missing_file_loads_empty_and_leaves_the_real_one(tmp_path):
    empty = m.load(str(tmp_path / 'none.json'))
    assert empty['aspects'] == [] and empty['games'] == {}
    (tmp_path / 'old.json').write_text('{"schema_version": 0}')
    assert m.load(str(tmp_path / 'old.json'))['channels'] == {}
    assert m.load()['aspects']                      # the cached database is untouched


def test_the_doc_is_in_sync():
    with open(os.path.join(ROOT, 'docs', 'coaching-derivations.md'), encoding='utf-8') as f:
        doc = f.read()
    start, end = doc.index(m.MARK_START), doc.index(m.MARK_END) + len(m.MARK_END)
    assert doc[start:end] + '\n' == m.markdown(), 'run: python3 -m oversteer.coaching_matrix --doc ' \
                                                   'docs/coaching-derivations.md'
