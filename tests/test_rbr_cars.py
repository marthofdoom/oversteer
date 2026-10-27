import importlib.util
import json
import math
import os
import zipfile

from oversteer import car_data
from oversteer.shift_learner import CarModel, RATIO_MIN
from oversteer.telemetry import Sample, Telemetry
from tests.test_telemetry import FakeLeds

HERE = os.path.dirname(__file__)
TABLE = os.path.join(HERE, '..', 'data', 'telemetry', 'cars', 'rbr.json')
FIESTA = 'rbr/11'
# rpm per m/s in each gear from marth's captures of 2026-10-26 (the packet's wheel speed, clutch up, the mode of ~100k
# packets): verified on the i20 Coupe WRC 2021 (all gears) and the Fiesta WRC 2019 (gears 1-3; same gear list), default setup
FIESTA_RPM_PER_MS = [398, 316, 256, 209, 175, 149]


def test_the_shipped_rbr_cars_load_under_the_rsf_car_id_and_every_rsf_car_is_named():
    cars = car_data.load()
    fiesta = cars[FIESTA]
    assert fiesta['limiter_rpm'] == 8500 and fiesta['drivetrain'] == 'awd' and fiesta['screen_name'] == 'Ford Fiesta WRC 2019'
    gears = car_data.default_set(fiesta)['gears']
    assert gears == [3.0, 2.3846, 1.9286, 1.5789, 1.3182, 1.12]
    # rpm per m/s = gear x drop gear x 60 / (2 pi r) is what the game's wheel speed gave, in every gear
    for gear, measured in zip(gears, FIESTA_RPM_PER_MS):
        assert abs(gear * fiesta['final_drive'] * 60 / (2 * math.pi * 0.315) / measured - 1) < 0.005
    assert 'turbo' not in fiesta                             # NGP's Turbo 0 is the stock flag, not a statement
    assert max(fiesta['torque_curve'], key=lambda p: p[1]) == (5500.0, 450.0)          # 450 Nm at 5500 rpm, RSF's figure
    assert {'rbr/104', 'rbr/106', 'rbr/107'} <= set(cars)
    with open(TABLE, encoding='utf-8') as f:
        table = json.load(f)
    assert len(table['names']) >= 102
    for entry in table['cars']:
        assert 'rbr/' + entry['name'] in table['names'] and entry['limiter_rpm'] and len(entry['torque_curve']) >= 10
        assert sum(1 for s in entry['gear_sets'] if s['default']) == 1
    assert car_data.display_name(FIESTA) == 'Ford Fiesta WRC 2019'
    assert car_data.display_name('rbr/104') == 'Hyundai i20 Coupe WRC 2021'
    assert car_data.display_name('rbr/91') == 'Abarth Grande Punto S2000'      # a car the table has no physics for
    assert car_data.entry('rbr/91') is None


def test_rbr_sends_no_max_rpm_so_the_lights_use_the_shipped_limiter(monkeypatch):
    from oversteer import telemetry as module
    fed = []

    class Learner:
        def feed(self, now, sample, limiter, *args):
            fed.append((limiter, args[-1] if args else None))

        def shift_rpm(self, gear):
            return None
    current = {}
    monkeypatch.setattr(module, 'decode_sample', lambda data: current['sample'])
    telemetry = Telemetry(FakeLeds(), shift=0.85, learner=Learner())
    current['sample'] = Sample(6000.0, None, gear=3, speed=30.0, car=FIESTA, game='rbr')
    telemetry.handle(0.0, b'x', ('127.0.0.1', 1))
    assert fed[-1] == (8500.0, 'game') and telemetry.last_max_rpm == 8500.0
    current['sample'] = Sample(6000.0, None, gear=3, speed=30.0, car='rbr/unknown', game='rbr')
    telemetry.handle(0.1, b'x', ('127.0.0.1', 1))
    assert fed[-1] == (6000.0, 'seen')                       # a car no table knows: the highest rpm seen


def test_a_car_key_the_table_names_is_named_without_the_install():
    from oversteer.telemetry_formats import decode_sample
    from tests.test_rbr import packet
    named = decode_sample(packet(), {'key': FIESTA, 'id': 11, 'name': None, 'drivetrain': None, 'gears': None})
    assert named.car == FIESTA and named.car_name == 'Ford Fiesta WRC 2019'
    unknown = decode_sample(packet(), {'key': 'rbr/unknown', 'id': None, 'name': None, 'drivetrain': None, 'gears': None})
    assert unknown.car_name == 'RBR car'


def test_the_learner_takes_the_fiestas_gear_set_and_best_upshifts_from_the_shipped_data():
    car = CarModel(FIESTA, 'Ford Fiesta WRC 2019')
    car.ratios = {gear: [rpm] * RATIO_MIN for gear, rpm in enumerate(FIESTA_RPM_PER_MS, 1)}
    gear_set, miss = car.gear_set()
    assert gear_set['id'] == 'gravel+tarmac+snow' and miss < 0.01
    assert car.game_data() is not None and car.gearing_aside() is None and car.top_gear() == 6
    for gear in (1, 2, 3, 4, 5):
        best = car.best_for(gear)
        assert best['source'] == 'game' and 5000 < best['rpm'] <= 8500
    assert car.best_for(6) is None                           # top gear


def test_the_rbr_car_table_reads_the_physics_of_an_install(tmp_path):
    """research/rbr/car_table.py on a small install: a car from its folder, one from a zip, a slot folder marked with
    the car's name, and a stock slot that is not shipped."""
    spec = importlib.util.spec_from_file_location('rbr_car_table', os.path.join(HERE, '..', 'research', 'rbr', 'car_table.py'))
    table = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(table)
    root = tmp_path / 'RBR'
    cache = root / 'rsfdata' / 'cache'
    cache.mkdir(parents=True)
    cars = [{'id': '1', 'name': 'Car One', 'path': 'One_ngp6'}, {'id': '2', 'name': 'Car Two', 'path': 'Two_ngp6'},
            {'id': '3', 'name': 'Car Three', 'path': 'Three_ngp6'}, {'id': '4', 'name': 'Car Four', 'path': 'Four_ngp6'}]
    (cache / 'cars.json').write_text(json.dumps(cars))
    (cache / 'cars_data.json').write_text(json.dumps([
        {'id': str(i), 'torque': '300Nm / 4000rpm', 'drive_train': 'RWD', 'transmission': '5 gears', 'year': '2000'}
        for i in range(1, 5)]))
    curve = ''.join('  Tq_%02d_RPM_NGP %d\n  Tq_%02d_NGP %s\n' % (i, 1000 * i, i, v) for i, v in
                    enumerate([0.4, 0.6, 0.8, 0.95, 1.0, 0.9, 0.8, 0.0]))
    common = ('InverseMass 0.001\nEffWheelRadius 0.3\n' + curve + 'NumberOfGears 7\nGear2Upshift 6000\nGear3Upshift 6100\n'
              'Gear4Upshift 6200\nGear5Upshift 6300\nGear6Upshift 0\nRPMLimit 6500.0\nInverseMass 0.2\n')
    options = ('GearList00Pinion 0\nGearList00Gear 0\nGearList01Pinion 10\nGearList01Gear -30\nGearList02Pinion 0\n'
               'GearList02Gear 0\nGearList03Pinion 10\nGearList03Gear 30\nGearList04Pinion 10\nGearList04Gear 20\n'
               'GearList05Pinion 10\nGearList05Gear 15\nGearList06Pinion 10\nGearList06Gear 12\nGearList07Pinion 10\n'
               'GearList07Gear 10\nGearList08Pinion 40\nGearList08Gear 10\nGearList09Pinion 8\nGearList09Gear 8\n'
               'DriveTrainLoss_NGP 10\n')
    setup = 'GearId0 1\nGearId1 2\nGearId2 3\nGearId3 4\nGearId4 5\nGearId5 6\nGearId6 7\nFinalDriveId 9\nDropGearId 8\n'

    def write(folder, tarmac_shorter=False):
        (folder / 'setups').mkdir(parents=True)
        (folder / 'common.lsp').write_text(common)
        for surface in ('gravel', 'tarmac', 'snow'):
            (folder / ('r_%s.lsp' % surface)).write_text(options)
            (folder / 'setups' / ('d_%s.lsp' % surface)).write_text(
                setup.replace('GearId2 3', 'GearId2 4') if surface == 'tarmac' and tarmac_shorter else setup)
    write(root / 'rsfdata' / 'cars' / 'One_ngp6', tarmac_shorter=True)
    write(tmp_path / 'zipped')
    with zipfile.ZipFile(root / 'rsfdata' / 'cars' / 'Two_ngp6.zip', 'w') as z:
        for path in (tmp_path / 'zipped').rglob('*'):
            if path.is_file():
                z.write(path, 'Two_ngp6/' + str(path.relative_to(tmp_path / 'zipped')))
    write(root / 'rsfdata' / 'Physics' / 's_i2003')
    (root / 'rsfdata' / 'Physics' / 's_i2003' / 'Car Three').write_text('revision 2 / 2020-01-01\n')
    write(root / 'rsfdata' / 'Physics' / 's_i2000')                   # stock: no marker file
    found = table.sources(str(root))
    assert sorted(found) == ['1', '2', '3']
    entry, note = table.build_car(cars[0], {'torque': '300Nm / 4000rpm', 'drivetrain': 'rwd'}, found['1'])
    assert note is None and entry['name'] == '1' and entry['limiter_rpm'] == 6500 and entry['mass_kg'] == 1000.0
    assert entry['final_drive'] == 4.0 and entry['transfer_ratio'] == 1.0 and entry['gearbox_efficiency'] == 0.9
    assert entry['torque_curve'][-1] == [6500, 120.0] and [4000, 300.0] in entry['torque_curve']      # cut at the limiter
    assert entry['auto_upshift_rpm'] == [6000, 6100, 6200, 6300]
    sets = {s['id']: s for s in entry['gear_sets']}
    assert sets['gravel+snow']['gears'] == [3.0, 2.0, 1.5, 1.2, 1.0] and sets['gravel+snow']['default']
    assert sets['tarmac']['gears'] == [2.0, 2.0, 1.5, 1.2, 1.0] and not sets['tarmac']['default']
    assert sets['gravel+snow']['reverse'] == 3.0 and 'turbo' not in entry
    no_drop = tmp_path / 'nodrop'
    write(no_drop)
    (no_drop / 'setups' / 'd_gravel.lsp').write_text(setup.replace('DropGearId 8\n', ''))
    for surface in ('tarmac', 'snow'):
        (no_drop / 'setups' / ('d_%s.lsp' % surface)).unlink()
    assert table.build_car(cars[3], {'torque': '300Nm / 4000rpm'}, table.Dir(str(no_drop), 'x'))[0] is None
    two, _ = table.build_car(cars[1], {'torque': '300Nm / 4000rpm'}, found['2'])
    assert [s['id'] for s in two['gear_sets']] == ['gravel+tarmac+snow']
    assert found['3'].label.endswith('s_i2003')
