"""Richard Burns Rally cars told apart by their gearing (oversteer/rbr_fingerprint.py): the comparison, the shipped
table, candidate sets, the library of cars met before (its file), epochs and the provisional car, and the shift
learner re-keying a session and run once the car is known. Packets are synthetic (tests/test_rbr.packet)."""
import json
import os
import sqlite3

import pytest

from oversteer import car_data, rbr_fingerprint as fp
from oversteer.shift_learner import ShiftLearner
from oversteer.telemetry import Telemetry
from oversteer.telemetry_formats import decode_sample
from tests.test_rbr import packet
from tests.test_telemetry import FakeLeds

WRC = [3.0, 2.3846, 1.9286, 1.5789, 1.3182, 1.12]            # the Fiesta WRC 2019's and the i20 WRC 2021's gears
OTHER = [3.3333, 2.2857, 1.6875, 1.3125, 1.0625]             # the Fabia Rally2's (gravel)
SCALE = 4.375 * 60 / (2 * 3.14159265 * 0.315)                # rpm per m/s per unit of gear ratio (drop gear 35/8)
TORQUE = [[1000, 260.0], [5500, 450.0], [8500, 290.0]]


def entry(name, gears, limiter=8500, torque=TORQUE, screen=None):
    return {'name': name, 'screen_name': screen or 'Car ' + name, 'game': 'rbr', 'limiter_rpm': limiter,
            'torque_curve': [tuple(p) for p in torque], 'final_drive': 4.375, 'tyre_radius_m': {'gravel': [0.315] * 2},
            'gear_sets': [{'id': 'gravel', 'gears': list(gears), 'primary': 1.0, 'default': True}]}


@pytest.fixture
def table():
    """A shipped table of our own: set() it to {key: entry}; the RSF cars that exist are those, plus `others`
    ({key: (gears, drivetrain)}: cars the table has no gearing for)."""
    class Table:
        def set(self, cars, others=None):
            car_data.set_cars(dict(cars))
            names = {k: {'gears': len(e['gear_sets'][0]['gears']), 'drivetrain': e.get('drivetrain')}
                     for k, e in cars.items()}
            names.update({k: {'gears': g, 'drivetrain': d} for k, (g, d) in (others or {}).items()})
            fp.set_names(names)
    yield Table()
    car_data.set_cars(None)
    fp.set_names(None)


class Drive:
    """Packets of a car driving through some gears: its steps advance 12 a packet (60 a second)."""

    def __init__(self, gears=WRC, scale=SCALE, slot=6, stage=81, steps=2000):
        self.gears, self.scale, self.slot, self.stage, self.steps = gears, scale, slot, stage, steps
        self.t = 100.0            # arrival time
        self.d = 10.0
        self.clock0 = None

    def packet(self, gear, speed, rpm=None, clutch=0.0, wheels=None):
        """(arrival time, packet)"""
        self.steps += 12
        self.t += 12 / 720.0
        if self.clock0 is None:
            self.clock0 = self.steps
        self.d += speed * 12 / 720.0
        rpm = rpm if rpm is not None else (self.gears[gear - 1] * self.scale * (wheels or speed) if gear >= 1 else 1000.0)
        return self.t, packet(**{'totalSteps': self.steps, 'stage.index': self.stage, 'car.index': self.slot,
                         'control.gear': gear + 1, 'control.clutch': clutch, 'control.throttle': 0.3,
                         'car.speed': (wheels or speed) * 3.6, 'car.velocities.surge': speed, 'car.engine.rpm': rpm,
                         'stage.raceTime': (self.steps - self.clock0) / 720.0, 'stage.driveLineLocation': self.d,
                         'stage.distanceToEnd': 5000.0 - self.d, 'car.positionX': self.d})

    def through(self, gears, seconds=1.0, rpm=5000.0):
        """Packets: `seconds` in each of `gears` at `rpm` (with a moment after each change, which is not counted)."""
        out = []
        for gear in gears:
            speed = rpm / (self.gears[gear - 1] * self.scale)
            for _ in range(int(seconds * 60)):
                out.append(self.packet(gear, speed))
        return out

    def load(self, stage=None, slot=None, gap=0.0):
        """A stage loaded again (or another one, or another car slot) after `gap` s of silence."""
        self.steps = 100
        self.t += gap
        self.clock0 = None
        self.d = 10.0
        if stage is not None:
            self.stage = stage
        if slot is not None:
            self.slot = slot


def identify(rec, drive, packets):
    """Each packet through the recogniser; the last sample and every event."""
    events, sample = [], None
    for t, data in packets:
        sample = decode_sample(data)
        events += rec.identify(t, sample)
    return sample, events


# -- the comparison --

def test_compare_scales_out_the_drop_gear_and_needs_three_gears():
    ref = {g + 1: r for g, r in enumerate(WRC)}
    est = {g: ref[g] * SCALE * 0.914 for g in (2, 3, 4)}           # a setup's drop gear option: every gear alike
    assert fp.compare(ref, est)[0] == 'match'
    assert fp.compare(ref, {g: est[g] for g in (2, 3)})[0] is None        # two gears: one step, wait
    est[3] *= 1.015
    assert fp.compare(ref, est)[0] is None                               # between the tolerance and a miss: wait
    est[3] *= 1.02
    assert fp.compare(ref, est)[0] == 'miss'
    fabia = {g + 1: r for g, r in enumerate(OTHER)}
    assert fp.compare(fabia, {g: ref[g] for g in (1, 2, 3)})[0] == 'miss'


# -- the shipped table --

def test_one_shipped_car_whose_gearing_fits_is_that_car_after_a_few_gears(table):
    table.set({'rbr/11': entry('11', WRC, screen='Ford Fiesta WRC 2019'), 'rbr/106': entry('106', OTHER, 7500)})
    rec, drive = fp.Recogniser(fp.Library()), Drive()
    sample, _ = identify(rec, drive, drive.through([2, 3], 1.0))
    assert sample.car == 'rbr/unknown' and sample.car_epoch == 1 and not sample.car_provisional
    sample, events = identify(rec, drive, drive.through([4], 1.0))
    assert sample.car == 'rbr/11' and sample.car_name == 'Ford Fiesta WRC 2019'
    assert rec.fp.distance < 200.0
    [(kind, saved)] = events                                            # a car the table names needs no entry...
    assert kind == 'save' and saved['cars'] == [] and saved['last'] == {'key': 'rbr/11', 'slot': 6}   # ...but is the last


def test_a_gearbox_the_wheels_spin_or_the_clutch_slips_in_is_read_from_the_wheels(table):
    table.set({'rbr/11': entry('11', WRC)})
    rec, drive = fp.Recogniser(fp.Library()), Drive()
    packets = []
    for gear in (2, 3, 4):
        speed = 5000.0 / (WRC[gear - 1] * SCALE)
        packets += [drive.packet(gear, speed * 0.9, wheels=speed) for _ in range(60)]          # sliding: the ground lags
        packets += [drive.packet(gear, speed, rpm=9000.0, clutch=1.0) for _ in range(60)]     # clutch down: not counted
    sample, _ = identify(rec, drive, packets)
    assert sample.car == 'rbr/11'


def test_cars_one_gearbox_fits_are_a_candidate_set_with_their_shared_data(table):
    table.set({'rbr/11': entry('11', WRC, screen='Ford Fiesta WRC 2019'),
               'rbr/104': entry('104', WRC, screen='Hyundai i20 Coupe WRC 2021')})
    rec, drive = fp.Recogniser(fp.Library(writable=True)), Drive(slot=6)
    sample, events = identify(rec, drive, drive.through([2, 3, 4], 1.0))
    assert sample.car == 'rbr/11+104@6'
    assert sample.car_name == 'Ford Fiesta WRC 2019 / Hyundai i20 Coupe WRC 2021 (car slot 6)'
    assert car_data.entry('rbr/11+104@6')['limiter_rpm'] == 8500           # the lights' limiter, the best changes up
    assert [e[0] for e in events] == ['save'] and events[0][1]['cars'][0]['kind'] == 'set'
    other = fp.Recogniser(fp.Library())
    sample, _ = identify(other, Drive(slot=5), Drive(slot=5).through([2, 3, 4], 1.0))
    assert sample.car == 'rbr/11+104@5'                                    # the other slot: another car


def test_candidates_whose_data_differ_share_none(table):
    table.set({'rbr/1': entry('1', WRC), 'rbr/2': entry('2', WRC, torque=[[1000, 200.0], [8500, 300.0]])})
    assert car_data.entry('rbr/1+2@5') is None
    table.set({'rbr/1': entry('1', WRC), 'rbr/2': entry('2', WRC, limiter=8000)})
    assert car_data.entry('rbr/1+2@5') is None
    assert car_data.entry('rbr/1+3@5') is None                              # one of them not in the table


def test_a_car_revving_past_a_shipped_cars_limiter_is_not_it(table):
    table.set({'rbr/11': entry('11', WRC, limiter=7000)})
    rec, drive = fp.Recogniser(fp.Library()), Drive()
    sample, _ = identify(rec, drive, drive.through([2, 3, 4, 5], 1.0, rpm=8000.0))
    assert sample.car == 'rbr/g1'


# -- the library --

def test_a_car_the_table_does_not_know_keeps_its_key_across_sessions(table, tmp_path):
    table.set({'rbr/106': entry('106', OTHER, 7500)})
    path = str(tmp_path / 'lib' / 'cars.json')
    library = fp.Library(path, writable=True)
    rec, drive = fp.Recogniser(library), Drive(slot=6)
    sample, events = identify(rec, drive, drive.through([2, 3, 4], 1.0))
    assert sample.car == 'rbr/unknown'                                     # three gears: waits for a fourth to mint
    sample, events = identify(rec, drive, drive.through([5], 1.0))
    assert sample.car == 'rbr/g1' and sample.car_name == 'RBR car #1 (5 gears)'
    assert library.write(events[-1][1]) and not os.path.exists(path + '.tmp')
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    assert data['version'] == fp.VERSION and data['last'] == {'key': 'rbr/g1', 'slot': 6}
    [car] = data['cars']
    assert car['key'] == 'rbr/g1' and car['kind'] == 'local' and car['slot'] == 6 and car['n'] == 1
    assert sorted(car['gears']) == ['2', '3', '4', '5'] and car['gears']['3'] == pytest.approx(WRC[2] * SCALE, rel=1e-3)

    # The next session (the app started again): the same car, another drop gear option, gears 3 to 6, slot 6
    rec = fp.Recogniser(fp.Library(path))
    drive = Drive(slot=6, scale=SCALE * 0.914)
    sample, _ = identify(rec, drive, drive.through([3], 0.2))
    assert sample.car == 'rbr/g1' and sample.car_provisional                # the last car, carried over
    sample, events = identify(rec, drive, drive.through([3, 4, 5, 6], 1.0))
    assert sample.car == 'rbr/g1' and not sample.car_provisional
    rec.close()
    assert rec.library.entry('rbr/g1')['name'] == 'RBR car #1 (6 gears)' and rec.library.entry('rbr/g1')['epochs'] == 2
    assert rec.library.entry('rbr/g1')['gears'][6] == pytest.approx(WRC[5] * SCALE, rel=1e-3)   # scaled to the first

    # Another gearbox, and the same one in another slot: two more cars
    for gears, slot, key in (([3.5, 2.6, 2.0, 1.6, 1.3], 6, 'rbr/g2'), (WRC, 5, 'rbr/g3')):
        rec.library.last = None
        drive = Drive(gears=gears, slot=slot)
        sample, _ = identify(fp.Recogniser(rec.library), drive, drive.through([1, 2, 3, 4], 1.0))
        assert sample.car == key


def test_a_library_of_another_version_or_broken_is_not_used(tmp_path):
    path = tmp_path / 'cars.json'
    path.write_text(json.dumps({'version': 99, 'cars': [{'key': 'rbr/g1', 'slot': 5, 'gears': {'1': 1}}]}))
    assert fp.Library(str(path)).load().cars == []
    path.write_text('{not json')
    assert fp.Library(str(path)).load().cars == []
    path.write_text(json.dumps({'version': 1, 'cars': [{'key': 'rbr/g1'}, {'key': 'rbr/g2', 'slot': 5,
                                                                             'gears': {'2': 300.0}}]}))
    assert [e['key'] for e in fp.Library(str(path)).load().cars] == ['rbr/g2']


def test_the_installs_car_is_recorded_so_its_slot_is_known_without_it(table, tmp_path):
    table.set({})
    library = fp.Library(str(tmp_path / 'cars.json'), writable=True)
    rec, drive = fp.Recogniser(library), Drive(slot=5)
    for t, data in drive.through([2, 3, 4], 1.0):
        s = decode_sample(data)
        s.car, s.car_name = 'rbr/11', 'Ford Fiesta WRC 2019'              # as the install's Cars.ini names it
        rec.identify(t, s)
        assert s.car == 'rbr/11'
    entry = library.entry('rbr/11')
    assert entry['kind'] == 'install' and entry['slot'] == 5 and len(entry['gears']) == 3
    rec = fp.Recogniser(library)
    drive = Drive(slot=5)
    sample, _ = identify(rec, drive, drive.through([2, 3, 4], 1.0))
    assert sample.car == 'rbr/11' and sample.car_name == 'Ford Fiesta WRC 2019'


def test_a_car_of_ours_the_table_later_names_is_adopted(table, tmp_path):
    table.set({})
    library = fp.Library(str(tmp_path / 'cars.json'))
    rec, drive = fp.Recogniser(library), Drive(slot=6)
    sample, _ = identify(rec, drive, drive.through([2, 3, 4, 5], 1.0))
    assert sample.car == 'rbr/g1'
    table.set({'rbr/104': entry('104', WRC, screen='Hyundai i20 Coupe WRC 2021')})
    rec = fp.Recogniser(library)
    drive = Drive(slot=6)
    sample, events = identify(rec, drive, drive.through([2, 3, 4], 1.0))
    assert sample.car == 'rbr/104' and ('adopt', 'rbr/g1', 'rbr/104') in events


# -- epochs and the provisional car --

def test_a_restart_keeps_the_car_and_a_trip_to_the_menus_makes_it_provisional(table):
    table.set({'rbr/11': entry('11', WRC), 'rbr/106': entry('106', OTHER, 7500)})
    rec, drive = fp.Recogniser(fp.Library()), Drive(slot=6)
    sample, _ = identify(rec, drive, drive.through([2, 3, 4], 1.0))
    assert sample.car == 'rbr/11' and sample.car_epoch == 1
    drive.load(gap=15.0)                                                   # a restart from the pause menu
    sample, _ = identify(rec, drive, drive.through([1], 1.5))
    assert sample.car == 'rbr/11' and sample.car_epoch == 1 and sample.car_provisional     # re-checked...
    sample, _ = identify(rec, drive, drive.through([2, 3], 1.0))
    assert sample.car == 'rbr/11' and sample.car_epoch == 1 and not sample.car_provisional  # ...and fits again
    drive.load(gap=fp.MENU_GAP + 1)                                        # the menus: the car may have changed
    sample, _ = identify(rec, drive, drive.through([1], 0.2))
    assert sample.car == 'rbr/11' and sample.car_epoch == 2 and sample.car_provisional
    sample, _ = identify(rec, drive, drive.through([2, 3, 4], 1.0))
    assert sample.car == 'rbr/11' and not sample.car_provisional           # confirmed
    drive.load(stage=90)                                                   # another stage, in another car
    drive.gears = OTHER
    sample, _ = identify(rec, drive, drive.through([1], 0.2))
    assert sample.car == 'rbr/11' and sample.car_epoch == 3 and sample.car_provisional
    sample, _ = identify(rec, drive, drive.through([1, 2, 3], 1.0))
    assert sample.car == 'rbr/106' and sample.car_epoch == 3 and not sample.car_provisional    # corrected
    drive.load(slot=2)                                                     # another slot: another car for certain
    sample, _ = identify(rec, drive, drive.through([1], 0.2))
    assert sample.car == 'rbr/unknown' and sample.car_epoch == 4 and not sample.car_provisional


# -- the shift learner: re-keying, nothing learnt under a car not known --

def learner_drive(tmp_path, name='t.db'):
    learner = ShiftLearner(str(tmp_path / name))
    learner.rbr_cars.library.writable = True                               # as the app
    telemetry = Telemetry(FakeLeds(), learner=learner)
    return learner, telemetry


def feed(telemetry, drive, packets):
    for t, data in packets:
        telemetry.handle(t, data, ('127.0.0.1', 0))


def runs_by_car(path):
    db = sqlite3.connect(path)
    try:
        return db.execute('SELECT c.key, COUNT(DISTINCT s.id), COUNT(r.id) FROM sessions s JOIN cars c ON c.id = s.car '
                          'LEFT JOIN runs r ON r.session = s.id GROUP BY c.key ORDER BY c.key').fetchall()
    finally:
        db.close()


def test_the_run_started_unknown_is_rekeyed_to_the_car_and_learns_from_then_on(table, tmp_path):
    table.set({'rbr/11': entry('11', WRC)})
    learner, telemetry = learner_drive(tmp_path)
    drive = Drive(slot=5)
    feed(telemetry, drive, drive.through([2, 3], 1.5))
    assert learner.car.key == 'rbr/unknown' and learner.runs.run is not None and not learner.car.ratios
    run = learner.runs.run
    feed(telemetry, drive, drive.through([4, 5, 4], 1.5))
    assert learner.car.key == 'rbr/11' and learner.runs.run == run         # the same run, under the car now
    assert learner.runs._summary['car'] == 'rbr/11'
    assert learner.car.ratios.get(4)                                       # learnt once the car is known
    learner.close()
    assert runs_by_car(str(tmp_path / 't.db')) == [('rbr/11', 1, 1)]       # the unknown car's row is gone


def test_nothing_is_learnt_under_a_provisional_car_and_a_correction_moves_the_session(table, tmp_path):
    table.set({'rbr/11': entry('11', WRC), 'rbr/106': entry('106', OTHER, 7500)})
    learner, telemetry = learner_drive(tmp_path)
    drive = Drive(slot=5)
    feed(telemetry, drive, drive.through([2, 3, 4, 3], 2.5))
    assert learner.car.key == 'rbr/11'
    learnt = {g: len(v) for g, v in learner.car.ratios.items()}
    drive.load(stage=90, gap=fp.MENU_GAP + 5)                               # the menus; another car (the Fabia)
    drive.gears = OTHER
    feed(telemetry, drive, drive.through([1], 1.0))
    assert learner.car.key == 'rbr/11' and learner._provisional
    assert {g: len(v) for g, v in learner.car.ratios.items()} == learnt     # the Fiesta's model untouched
    feed(telemetry, drive, drive.through([2, 3, 2], 2.5))
    assert learner.car.key == 'rbr/106' and not learner._provisional
    learner.close()
    assert runs_by_car(str(tmp_path / 't.db')) == [('rbr/106', 1, 1), ('rbr/11', 1, 1)]   # two sessions, one each


def test_a_provisional_car_never_confirmed_leaves_its_session_to_the_shared_key(table, tmp_path):
    table.set({'rbr/11': entry('11', WRC)})
    learner, telemetry = learner_drive(tmp_path)
    drive = Drive(slot=5)
    feed(telemetry, drive, drive.through([2, 3, 4, 3], 2.5))
    assert learner.car.key == 'rbr/11'
    drive.load(stage=90, gap=fp.MENU_GAP + 5)
    feed(telemetry, drive, drive.through([1], 12.0))                       # 1st gear only: never confirmed
    assert learner.car.key == 'rbr/11' and learner._provisional
    learner.close()
    assert runs_by_car(str(tmp_path / 't.db')) == [('rbr/11', 1, 1), ('rbr/unknown', 1, 1)]


def test_an_adopted_car_brings_its_sessions_and_model_to_the_tables_key(table, tmp_path):
    table.set({})
    learner, telemetry = learner_drive(tmp_path)
    drive = Drive(slot=6)
    feed(telemetry, drive, drive.through([2, 3, 4, 5, 4], 2.5))
    assert learner.car.key == 'rbr/g1'
    learnt = sum(len(v) for v in learner.car.ratios.values())
    learner.close()
    table.set({'rbr/104': entry('104', WRC, screen='Hyundai i20 Coupe WRC 2021')})
    learner, telemetry = learner_drive(tmp_path)
    drive = Drive(slot=6, stage=90)
    feed(telemetry, drive, drive.through([2, 3, 4, 3], 2.5))
    assert learner.car.key == 'rbr/104' and sum(len(v) for v in learner.car.ratios.values()) >= learnt
    learner.close()
    assert runs_by_car(str(tmp_path / 't.db')) == [('rbr/104', 2, 2)]
    db = sqlite3.connect(str(tmp_path / 't.db'))
    assert db.execute("SELECT name FROM cars WHERE key = 'rbr/104'").fetchone()[0] == 'Hyundai i20 Coupe WRC 2021'
    db.close()


def test_a_replay_reads_the_library_and_never_writes_it(table, tmp_path):
    from oversteer import telemetry_capture as tc
    table.set({})
    drive = Drive(slot=6)
    packets = [(t - 100.0, ('127.0.0.1', 0), p) for t, p in drive.through([2, 3, 4, 5], 1.5)]
    learner = ShiftLearner(str(tmp_path / 'a.db'))
    learner.rbr_cars.library.writable = True                               # as the app: the library is written
    tc.replay(packets, learner)
    assert learner.car.key == 'rbr/g1'
    learner.close()
    path = fp.library_path()
    with open(path, encoding='utf-8') as f:
        written = f.read()
    for name in ('b.db', 'c.db'):
        learner = ShiftLearner(str(tmp_path / name))                       # a replay: read-only
        tc.replay(packets, learner)
        assert learner.car.key == 'rbr/g1'                                 # the key the live session gave
        learner.close()
    with open(path, encoding='utf-8') as f:
        assert f.read() == written
    os.remove(path)
    learner = ShiftLearner(str(tmp_path / 'd.db'))
    tc.replay(packets, learner)
    assert learner.car.key == 'rbr/g1' and not os.path.exists(path)        # minted in memory only
    learner.close()


# -- the review's fixes (0.17) --

def test_a_lone_fit_a_car_without_gearing_could_share_is_a_set_of_one_in_its_slot(table, tmp_path):
    table.set({'rbr/11': entry('11', WRC, screen='Ford Fiesta WRC 2019')}, others={'rbr/50': (6, None)})
    library = fp.Library(str(tmp_path / 'cars.json'), writable=True)
    rec, drive = fp.Recogniser(library), Drive(slot=6)
    sample, events = identify(rec, drive, drive.through([2, 3, 4], 1.0))
    assert sample.car == 'rbr/11@6' and sample.car_name == 'Ford Fiesta WRC 2019 (by gearing, car slot 6)'
    assert car_data.entry('rbr/11@6')['limiter_rpm'] == 8500 and library.entry('rbr/11@6')['kind'] == 'set'
    assert not [e for e in events if e[0] == 'adopt']
    table.set({'rbr/11': entry('11', WRC)}, others={'rbr/50': (5, None)})      # no car could share it: rbr/11...
    rec = fp.Recogniser(library)
    drive = Drive(slot=6)
    sample, events = identify(rec, drive, drive.through([2, 3, 4], 1.0))
    assert sample.car == 'rbr/11' and ('adopt', 'rbr/11@6', 'rbr/11') in events      # ...and the set is it now
    rec = fp.Recogniser(library)
    drive = Drive(slot=6)
    sample, events = identify(rec, drive, drive.through([2, 3, 4], 1.0))
    assert sample.car == 'rbr/11' and not [e for e in events if e[0] == 'adopt']      # adopted once


def test_a_gear_set_neither_fitting_nor_missing_stops_the_wait_a_kilometre_in(table):
    near = [r * (1.015 if i == 1 else 1.0) for i, r in enumerate(WRC)]           # one gear 1.5 % off
    table.set({'rbr/11': entry('11', near)})
    rec, drive = fp.Recogniser(fp.Library()), Drive()
    sample, _ = identify(rec, drive, drive.through([2, 3, 4, 5], 2.0))
    assert sample.car == 'rbr/unknown' and rec.fp.distance < fp.DECIDED_M
    sample, _ = identify(rec, drive, drive.through([5], 30.0))
    assert rec.fp.distance >= fp.DECIDED_M and sample.car == 'rbr/g1'


def test_a_known_car_whose_gearing_stops_fitting_after_a_quick_restart_is_another(table):
    table.set({'rbr/11': entry('11', WRC), 'rbr/106': entry('106', OTHER, 7500)})
    rec, drive = fp.Recogniser(fp.Library()), Drive(slot=6)
    sample, _ = identify(rec, drive, drive.through([2, 3, 4], 1.0))
    assert sample.car == 'rbr/11' and sample.car_epoch == 1
    drive.load(gap=10.0)                                                   # a quick trip, the same stage, the Fabia
    drive.gears = OTHER
    sample, _ = identify(rec, drive, drive.through([1, 2, 3], 1.0))
    sample, _ = identify(rec, drive, drive.through([1, 2, 3], 1.0))
    assert sample.car == 'rbr/106' and sample.car_epoch == 2


def test_a_new_local_car_never_takes_a_number_the_store_has(table, tmp_path):
    table.set({})
    path = tmp_path / 'cars.json'
    path.write_text('{"version": 1, "cars": [')                            # broken
    library = fp.Library(str(path), writable=True)
    rec, drive = fp.Recogniser(library, taken=lambda: ['rbr/g7', 'rbr/gx', 'rbr/104']), Drive()
    sample, events = identify(rec, drive, drive.through([2, 3, 4, 5], 1.0))
    assert sample.car == 'rbr/g8'
    assert library.write(events[-1][1])
    [aside] = [p for p in os.listdir(tmp_path) if p.endswith('.bad')]
    assert (tmp_path / aside).read_text() == '{"version": 1, "cars": ['    # never overwritten: moved aside
    assert json.loads(path.read_text())['cars'][0]['key'] == 'rbr/g8'
    assert sorted(os.listdir(tmp_path)) == sorted(['cars.json', aside])  # no temporary file left
    assert library.write(library.snapshot()) and len([p for p in os.listdir(tmp_path) if p.endswith('.bad')]) == 1


def test_the_learner_numbers_past_the_stores_cars(table, tmp_path):
    table.set({})
    learner, telemetry = learner_drive(tmp_path)
    learner.log.store.car_id('other profile', 'rbr/g5', 'rbr')
    learner.log.store.commit()
    drive = Drive()
    feed(telemetry, drive, drive.through([2, 3, 4, 5], 1.5))
    assert learner.car.key == 'rbr/g6'
    learner.close()


def test_every_session_of_an_unconfirmed_epoch_goes_to_the_shared_key(table, tmp_path):
    table.set({'rbr/11': entry('11', WRC)})
    learner, telemetry = learner_drive(tmp_path)
    drive = Drive(slot=5)
    feed(telemetry, drive, drive.through([2, 3, 4, 3], 2.5))
    assert learner.car.key == 'rbr/11'
    drive.load(stage=90, gap=fp.MENU_GAP + 5)                              # the menus: provisional
    feed(telemetry, drive, drive.through([1], 12.0))
    drive.t += 150.0                                                       # a long pause: the session ends, not the epoch
    feed(telemetry, drive, drive.through([1], 12.0))
    assert learner._provisional and len(learner._epoch_sessions) == 2
    drive.load(stage=91, gap=fp.MENU_GAP + 5)                              # the menus again, never confirmed
    feed(telemetry, drive, drive.through([1], 0.5))
    learner.close()
    assert runs_by_car(str(tmp_path / 't.db')) == [('rbr/11', 1, 1), ('rbr/unknown', 2, 2)]


def test_a_rekey_the_drive_log_refuses_is_posted_with_the_next_packet(table, tmp_path):
    table.set({'rbr/11': entry('11', WRC)})
    learner, telemetry = learner_drive(tmp_path)
    post = learner.log.post
    refused = []

    def full(fn, *args):
        if getattr(fn, '__name__', '') == '_write_rekey' and not refused:
            refused.append(fn)
            return False
        return post(fn, *args)
    learner.log.post = full
    drive = Drive(slot=5)
    feed(telemetry, drive, drive.through([2, 3, 4, 3], 2.5))
    assert refused and learner.car.key == 'rbr/11' and not learner._rbr_posts
    learner.close()
    assert runs_by_car(str(tmp_path / 't.db')) == [('rbr/11', 1, 1)]


def test_a_replay_adopts_nothing(table, tmp_path):
    table.set({})
    learner, telemetry = learner_drive(tmp_path)
    drive = Drive(slot=6)
    feed(telemetry, drive, drive.through([2, 3, 4, 5, 4], 2.5))
    assert learner.car.key == 'rbr/g1'
    learner.close()
    table.set({'rbr/104': entry('104', WRC)})
    learner, telemetry = learner_drive(tmp_path)
    learner.rbr_cars.library.writable = False                              # as a replay
    drive = Drive(slot=6, stage=90)
    feed(telemetry, drive, drive.through([2, 3, 4, 3], 2.5))
    assert learner.car.key == 'rbr/104'
    learner.close()
    assert ('rbr/g1', 1, 1) in runs_by_car(str(tmp_path / 't.db'))        # g1 kept its own


def test_another_game_ends_the_rbr_epoch(table, tmp_path):
    from oversteer.telemetry_formats import Sample
    table.set({'rbr/11': entry('11', WRC)})
    learner, telemetry = learner_drive(tmp_path)
    drive = Drive()
    feed(telemetry, drive, drive.through([2], 0.5))
    assert learner._rbr_epoch == 1
    learner.feed(drive.t + 1, Sample(3000.0, 7000.0, gear=2, speed=10.0, car='forza/1', game='forza'), 7000.0, 0.3, 0.0)
    assert learner._rbr_epoch is None and learner._epoch_sessions == [] and not learner._provisional
    learner.close()


def test_a_sets_shared_data_is_worked_out_once(table, monkeypatch):
    table.set({'rbr/11': entry('11', WRC), 'rbr/104': entry('104', WRC)})
    calls = []
    real = fp.shared_entry
    monkeypatch.setattr(fp, 'shared_entry', lambda key: calls.append(key) or real(key))
    assert car_data.entry('rbr/11+104@6') is car_data.entry('rbr/11+104@6') is not None
    assert calls == ['rbr/11+104@6']


def test_a_reference_offered_again_for_the_run_is_taken_again():
    from oversteer.live_buffer import LiveBuffer
    live = LiveBuffer()
    live.start_run(1, 0.0, 'rbr', 'rbr:81')
    live.offer_reference(1, None, {'key': 'rbr:81', 'name': 'A', 'length': 1000.0}, 5)
    live._adopt()
    assert live._run['id'] == 5
    live.offer_reference(1, None, {'key': 'rbr:81', 'name': 'A', 'length': 1000.0}, 6)
    live._adopt()
    assert live._run['id'] == 6


def _store_with_runs(tmp_path):
    """Two cars; car A's run on stage S at 100 s (its PB), car B's earlier run at 90 s; both with traces, and stored
    potentials on two stages and envelopes on two surfaces for both."""
    from oversteer.telemetry_store import open_store
    store = open_store(str(tmp_path / 's.db'))
    a = store.car_id('p', 'rbr/g1', 'rbr')
    b = store.car_id('p', 'rbr/unknown', 'rbr')
    sb = store.start_session('p', b, 'rbr', 1000.0)
    sa = store.start_session('p', a, 'rbr', 2000.0)
    runs = {}
    for name, session, started, result in (('b1', sb, 1000.0, 90.0), ('a1', sa, 2000.0, 100.0)):
        run = store.start_run(session, 1, started, 'rbr:1', 'rbr', 1000.0)
        store.end_run(run, ended=started + result, finished=1, result_time=result, distance=1000.0)
        store._do("UPDATE runs SET run_class = 'clean', surface = 'gravel' WHERE id = ?", (run,))
        store._do('INSERT INTO traces (run, version, data) VALUES (?, 2, zeroblob(1000))', (run,))
        runs[name] = run
    for car in (a, b):
        for stage in ('rbr:1', 'rbr:2'):
            store._do("INSERT INTO stage_potential (stage, car, data_version, sections, profile) VALUES "
                      "(?, ?, 'x', '[]', '{}')", (stage, car))
        for surface in ('gravel', 'tarmac'):
            store._do("INSERT INTO envelopes (car, surface, data_version, data) VALUES (?, ?, 'x', '{}')",
                      (car, surface))
    store.commit()
    return store, a, b, sa, sb, runs


def test_moving_a_session_keeps_every_pb_and_drops_only_its_stages_potentials(tmp_path):
    store, a, b, sa, sb, runs = _store_with_runs(tmp_path)
    assert store.move_session(sa, b) == a
    assert {r[0] for r in store._do('SELECT run FROM pb_keep')} == {runs['a1'], runs['b1']}
    left = set(store._do('SELECT stage, car FROM stage_potential').fetchall())
    assert left == {('rbr:2', a), ('rbr:2', b)}                            # the moved run's stage only
    assert set(store._do('SELECT car, surface FROM envelopes').fetchall()) == {(a, 'tarmac'), (b, 'tarmac')}
    assert store.prune_traces(cap=1) == 0                                  # a1 is no PB of b's now, and still kept
    assert {r[0] for r in store._do('SELECT run FROM traces')} == {runs['a1'], runs['b1']}


def test_merging_a_car_keeps_every_pb(tmp_path):
    store, a, b, sa, sb, runs = _store_with_runs(tmp_path)
    assert store.merge_car('p', 'rbr/g1', 'rbr/unknown') == 1
    assert store._do("SELECT COUNT(*) FROM cars WHERE key = 'rbr/g1'").fetchone()[0] == 0
    assert store._do('SELECT COUNT(*) FROM runs').fetchone()[0] == 2
    assert store.prune_traces(cap=1) == 0
    assert {r[0] for r in store._do('SELECT run FROM traces')} == {runs['a1'], runs['b1']}


# -- the re-review's fixes --

def test_after_a_quick_restart_nothing_is_learnt_until_the_gearing_fits_and_another_cars_runs_go_apart(table, tmp_path):
    table.set({'rbr/11': entry('11', WRC), 'rbr/106': entry('106', OTHER, 7500)})
    learner, telemetry = learner_drive(tmp_path)
    drive = Drive(slot=5)
    feed(telemetry, drive, drive.through([2, 3, 4, 3], 2.5))
    assert learner.car.key == 'rbr/11' and not learner._provisional
    fiesta = learner.car
    learnt = {g: len(v) for g, v in fiesta.ratios.items()}
    drive.load(gap=15.0)                                                   # a quick trip: the Fabia, the same stage
    drive.gears = OTHER
    feed(telemetry, drive, drive.through([1], 8.0) + drive.through([2], 2.5))
    assert learner.car is fiesta and learner._provisional                  # the lights still use it...
    assert {g: len(v) for g, v in fiesta.ratios.items()} == learnt         # ...nothing is learnt under it
    feed(telemetry, drive, drive.through([3, 2, 1, 2, 3], 2.5))
    assert learner.car.key == 'rbr/106'
    assert {g: len(v) for g, v in fiesta.ratios.items()} == learnt
    learner.close()
    # the Fiesta's run stays; the run driven since the load (before the miss) goes to the shared key
    assert runs_by_car(str(tmp_path / 't.db')) == [('rbr/106', 1, 1), ('rbr/11', 1, 1), ('rbr/unknown', 1, 1)]


def test_a_quick_restart_in_the_same_car_learns_again_once_it_fits(table, tmp_path):
    table.set({'rbr/11': entry('11', WRC)})
    learner, telemetry = learner_drive(tmp_path)
    drive = Drive(slot=5)
    feed(telemetry, drive, drive.through([2, 3, 4, 3], 2.5))
    drive.load(gap=15.0)
    feed(telemetry, drive, drive.through([2, 3, 4, 3], 2.5))
    assert learner.car.key == 'rbr/11' and not learner._provisional and learner._recheck_from is None
    learner.close()
    assert runs_by_car(str(tmp_path / 't.db')) == [('rbr/11', 1, 2)]       # one session, both runs


def test_with_no_names_read_no_lone_fit_is_taken_for_the_car(table):
    table.set({'rbr/11': entry('11', WRC)})
    fp.set_names({})                                                       # the table's names could not be read
    rec, drive = fp.Recogniser(fp.Library()), Drive(slot=6)
    sample, _ = identify(rec, drive, drive.through([2, 3, 4], 1.0))
    assert sample.car == 'rbr/11@6'


def test_the_library_is_written_only_once_the_writes_before_it_are_committed(table, tmp_path):
    learner = ShiftLearner(str(tmp_path / 't.db'))
    learner.rbr_cars.library.writable = True
    data = fp.Library().snapshot()
    learner.log._handle(learner._write_library, (data,))
    assert not os.path.exists(fp.library_path())                           # not before the commit...
    learner.log._run_after_commit()
    assert json.loads(open(fp.library_path()).read())['version'] == fp.VERSION    # ...after it
    learner.log._handle(learner._write_library, (dict(data, last={'key': 'x', 'slot': 1}),))
    learner.log._after_commit = []                                         # a commit that failed drops it
    assert json.loads(open(fp.library_path()).read())['last'] is None
    learner.close()


def test_split_runs_move_with_their_shifts_and_metrics_and_keep_every_pb(tmp_path):
    store, a, b, sa, sb, runs = _store_with_runs(tmp_path)
    store._do('INSERT INTO shifts (session, run, at, gear, gear_to, direction, rpm) VALUES (?, ?, 1.0, 2, 3, '
              "'up', 7000.0)", (sa, runs['a1']))
    store._do("INSERT INTO metrics (session, run, name, value, count) VALUES (?, ?, 'x', 1.0, 1)", (sa, runs['a1']))
    new = store.split_runs([runs['a1']], b)
    assert store._do('SELECT car FROM sessions WHERE id = ?', (new,)).fetchone()[0] == b
    for table in ('runs', 'shifts', 'metrics'):
        column = 'id' if table == 'runs' else 'run'
        assert store._do('SELECT session FROM {} WHERE {} = ?'.format(table, column), (runs['a1'],)).fetchone()[0] == new
    assert {r[0] for r in store._do('SELECT run FROM pb_keep')} == {runs['a1'], runs['b1']}
    assert store.prune_traces(cap=1) == 0
    assert store.split_runs([], b) is None
