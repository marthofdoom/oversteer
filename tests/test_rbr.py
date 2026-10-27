"""Richard Burns Rally (RallySimFans, NGP plugin): the 664-byte decoder, the install's stage and car names, and the
run lifecycle on synthetic packet sequences (docs/richard-burns-rally.md). There is no capture yet: the packets here
are built from the layout in research/rbr/ngp_packet.py with the values the doc gives for its sample (Rally School
Stage, slot 5, before the start)."""
import importlib.util
import json
import math
import os
import struct

import pytest

from oversteer import rbr_install, stage_tables, telemetry_formats
from oversteer.telemetry_formats import RBR_SIZE, decode_sample
from tests.test_drive_log import drive_runs

_spec = importlib.util.spec_from_file_location(
    'ngp_packet', os.path.join(os.path.dirname(__file__), '..', 'research', 'rbr', 'ngp_packet.py'))
ngp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ngp)

K = 273.15


def packet(**over):
    """A 664-byte packet: the doc's sample before the start, with `over` ({layout name: value}) set on it."""
    v = dict.fromkeys((n for n, _ in ngp.FIELDS), 0)
    v.update({'totalSteps': 1839, 'stage.index': 71, 'stage.progress': 0.0234, 'stage.raceTime': 0.0,
              'stage.driveLineLocation': 1.475, 'stage.distanceToEnd': 2261.7, 'control.gear': 1, 'car.index': 5,
              'car.positionX': 7.5, 'car.positionY': 45.1, 'car.positionZ': 4.1, 'car.yaw': 91.5,
              'car.engine.rpm': 1000.0, 'car.engine.radiatorCoolantTemperature': 362.0,
              'car.engine.engineCoolantTemperature': 362.0, 'car.engine.engineTemperature': 362.0})
    for w in ngp.WHEELS:
        p = 'car.suspension%s.' % w
        v.update({p + 'springDeflection': 0.195, p + 'strutForce': -2540.0, p + 'springForce': 2000.0,
                  p + 'wheel.tire.pressure': 220000.0, p + 'wheel.tire.temperature': 343.0,
                  p + 'wheel.tire.carcassTemperature': 343.0, p + 'wheel.tire.treadTemperature': 343.0,
                  p + 'wheel.brakeDisk.temperature': 300.0, p + 'wheel.brakeDisk.layerTemperature': 300.0})
        for s in range(1, 9):
            v[p + 'wheel.tire.segment%d.temperature' % s] = 343.0
    for name in over:
        assert name in v, name
    v.update(over)
    return struct.pack(ngp.FORMAT, *[v[n] for n, _ in ngp.FIELDS])


# -- the decoder --

def test_the_format_is_the_researched_layout():
    assert RBR_SIZE == ngp.SIZE == 664
    assert struct.calcsize(telemetry_formats.RBR_FORMAT) == struct.calcsize(ngp.FORMAT)
    assert telemetry_formats.RBR_FORMAT[1:] == ngp.FORMAT[1:]       # code for code, so every offset is the same


def test_664_bytes_is_rbr_and_nothing_else():
    assert decode_sample(packet()).game == 'rbr'
    assert decode_sample(packet()[:-4]) is None                      # 660: Codemasters range, but not 664
    assert decode_sample(packet() + b'\0' * 4) is None
    garbage = struct.pack('<%df' % 166, *([float('nan')] * 166))
    assert decode_sample(garbage) is None                            # no rpm


def test_the_docs_sample_packet():
    s = decode_sample(packet())
    assert s.game == 'rbr' and s.stage == 'rbr:71' and s.steps == 1839
    assert s.car == 'rbr/unknown' and s.car_name == 'RBR car'       # no install: nothing is learnt under it
    assert s.gear == 0 and s.rpm == 1000.0 and s.stage_time == 0.0
    assert s.engine_temp == pytest.approx(362.0 - K) and s.radiator_temp == s.block_temp == s.engine_temp
    assert s.tyre_pressure == (220.0,) * 4
    assert s.tyre_temp == pytest.approx((343.0 - K,) * 4) and s.tyre_carcass_temp == s.tyre_tread_temp == s.tyre_temp
    assert s.susp == pytest.approx((0.195,) * 4) and s.tyre_load == (2540.0,) * 4
    assert s.pos == (pytest.approx(7.5), pytest.approx(45.1), pytest.approx(4.1))
    assert s.attitude[0] == pytest.approx(math.radians(91.5))
    assert s.lap_distance == pytest.approx(1.475) and s.stage_length == pytest.approx(2263.175)
    assert s.dist_to_end == pytest.approx(2261.7)
    assert s.game_time == pytest.approx(1839 / 720.0)
    assert s.speed == 0.0 and s.accel == (0.0, 0.0, 0.0) and s.accel_kind == 'kinematic'
    assert len(s.tyre_seg_temp[0]) == 8 and len(s.tyre_seg_wear[3]) == 8 and s.tyre_wear == (0.0,) * 4


def test_gears_are_shifted_one():
    for raw, gear in ((0, -1), (1, 0), (2, 1), (6, 5)):
        assert decode_sample(packet(**{'control.gear': raw})).gear == gear
    assert decode_sample(packet(**{'control.gear': 99})).gear is None


def test_every_wheel_channel_lands_on_its_wheel():
    """Distinct values per wheel and channel: the order is LF, RF, LB, RB in the packet and FL, FR, RL, RR here."""
    over = {}
    for i, w in enumerate(ngp.WHEELS):
        p = 'car.suspension%s.' % w
        over.update({p + 'springDeflection': 0.1 + i, p + 'damper.pistonVelocity': 0.2 + i,
                     p + 'wheel.tire.pressure': 200000.0 + 1000.0 * i, p + 'wheel.tire.temperature': K + 50 + i,
                     p + 'wheel.brakeDisk.temperature': K + 400 + i, p + 'wheel.brakeDisk.wear': 0.3 + i,
                     p + 'rollbarForce': 1 + i, p + 'springForce': 10 + i, p + 'damperForce': 100 + i,
                     p + 'strutForce': -1000 - i, p + 'damper.damage': 0.5 + i, p + 'helperSpringIsActive': i % 2,
                     p + 'wheel.tire.currentSegment': 3 + i})
        for seg in range(1, 9):
            over[p + 'wheel.tire.segment%d.temperature' % seg] = K + 60 + i * 10 + seg
            over[p + 'wheel.tire.segment%d.wear' % seg] = 0.1 * seg + i
    s = decode_sample(packet(**over))
    assert s.susp == pytest.approx((0.1, 1.1, 2.1, 3.1)) and s.susp_vel == pytest.approx((0.2, 1.2, 2.2, 3.2))
    assert s.tyre_pressure == pytest.approx((200.0, 201.0, 202.0, 203.0))
    assert s.tyre_temp == pytest.approx((50.0, 51.0, 52.0, 53.0))
    assert s.brake_temp == pytest.approx((400.0, 401.0, 402.0, 403.0)) and s.brake_wear == pytest.approx((0.3, 1.3, 2.3, 3.3))
    assert s.susp_forces[2] == (3.0, 12.0, 102.0, -1002.0) and s.tyre_load == (1000.0, 1001.0, 1002.0, 1003.0)
    assert s.damper_damage == pytest.approx((0.5, 1.5, 2.5, 3.5)) and s.helper_spring == (0, 1, 0, 1)
    assert s.tyre_contact == (3, 4, 5, 6)
    assert s.tyre_seg_temp[1] == pytest.approx(tuple(70.0 + seg for seg in range(1, 9)))
    assert s.tyre_seg_wear[3][4] == pytest.approx(0.5 + 3)
    assert s.tyre_wear[0] == pytest.approx(sum(0.1 * seg for seg in range(1, 9)) / 8)


def test_the_body_channels():
    over = {'car.velocities.surge': 20.0, 'car.velocities.sway': 1.0, 'car.velocities.heave': 0.5,
            'car.velocities.roll': 10.0, 'car.velocities.pitch': 20.0, 'car.velocities.yaw': 30.0,
            'car.accelerations.surge': 2.0, 'car.accelerations.sway': 3.0, 'car.accelerations.heave': 4.0,
            'car.roll': 5.0, 'car.pitch': -6.0, 'car.yaw': 90.0, 'control.steering': 0.5, 'control.throttle': 0.7,
            'control.brake': 0.2, 'control.handbrake': 1.0, 'control.clutch': 0.3,
            'control.footbrakePressure': 11.0, 'control.handbrakePressure': 12.0}
    s = decode_sample(packet(**over))
    assert s.vel == (20.0, 1.0, 0.5) and s.accel == (2.0, 3.0, 4.0)
    assert s.ang_vel == pytest.approx(tuple(math.radians(a) for a in (10.0, 20.0, 30.0)))
    assert s.yaw_rate == pytest.approx(math.radians(30.0))
    assert s.attitude == pytest.approx((math.radians(90.0), math.radians(-6.0), math.radians(5.0)))
    assert (s.throttle, s.brake, s.handbrake, s.clutch) == pytest.approx((0.7, 0.2, 1.0, 0.3))
    assert s.pedal_pressure == (11.0, 12.0) and s.steer == pytest.approx(-0.5)
    assert s.speed == pytest.approx(math.sqrt(400 + 1 + 0.25))


def test_the_speed_is_the_velocity_not_the_wheels():
    """marth's captures: car.speed is km/h of the wheels (62 with the car held, spinning), the velocity vector
    (m/s) is the ground speed."""
    over = {'car.velocities.surge': 5.0, 'car.speed': 62.0}
    assert decode_sample(packet(**over)).speed == pytest.approx(5.0)
    nan = float('nan')
    assert decode_sample(packet(**{'car.velocities.surge': nan, 'car.speed': 90.0})).speed == pytest.approx(25.0)


def test_beyond_the_box_names_no_stage():
    assert decode_sample(packet(**{'stage.index': 41})).stage is None
    assert decode_sample(packet(**{'stage.index': 42})).stage == 'rbr:42'


def test_an_odd_drive_line_gives_no_distance():
    s = decode_sample(packet(**{'stage.driveLineLocation': -5.0}))
    assert s.lap_distance is None and s.stage_length is None


# -- the install: stage and car names --

STAGES = [
    {"id": "71", "name": "Rally School Stage", "length": "2200", "surface_id": "2", "stage_id": "71",
     "short_country": "GB", "tarmac": "0", "gravel": "100", "snow": "0", "author": "Warthog Games"},
    {"id": "12", "name": "Sikakama", "length": "10200", "surface_id": "3", "stage_id": "12",
     "short_country": "FI", "tarmac": "0", "gravel": "5", "snow": "95"},
    {"id": "300", "name": "Mixed Pass", "length": "8000", "surface_id": "1", "stage_id": "300",
     "short_country": "IT", "tarmac": "50", "gravel": "50", "snow": "0"},
]
TRACKS_INI = ('[Map71]\r\nTrackName = "Maps\\track-71"\r\n\r\n***\r\n'
              '[Map500]\r\nStageName = "Only In Tracks"\r\nLength = 3.4\r\nSurface = 2\r\nCountryCode = "SE"\r\n')
CARS = [{"id": "91", "name": "Abarth Grande Punto S2000", "path": "x"},
        {"id": "5", "name": "Subaru Impreza S12 WRC", "path": "y"}]
CARS_DATA = [{"car_id": "5", "drive_train": "AWD", "transmission": "6 gears", "power": "300bhp"}]


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A tiny RSF install and a fresh user data folder."""
    root = tmp_path / 'Richard Burns Rally'
    (root / 'rsfdata' / 'cache').mkdir(parents=True)
    (root / 'Maps').mkdir()
    (root / 'Cars').mkdir()
    (root / 'RichardBurnsRally.ini').write_text('[NGP]\r\nudpTelemetry=1\r\n')
    (root / 'rsfdata' / 'cache' / 'stages_data.json').write_text(json.dumps(STAGES))
    (root / 'rsfdata' / 'cache' / 'cars.json').write_text(json.dumps(CARS))
    (root / 'rsfdata' / 'cache' / 'cars_data.json').write_text(json.dumps(CARS_DATA))
    (root / 'Maps' / 'Tracks.ini').write_bytes(TRACKS_INI.encode('cp1252'))
    (root / 'Cars' / 'Cars.ini').write_text('[Car05]\r\nFileName = "Cars\\impreza03\\impreza03.sgc"\r\n'
                                            'CarName = "Subaru"\r\nRSFCarID = 5\r\n')
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    monkeypatch.setenv(rbr_install.ENV, str(root))
    monkeypatch.setattr(rbr_install, 'CAR_RECHECK', 0.0)
    rbr_install.forget()
    stage_tables.set_tables(None)
    yield root
    rbr_install.forget()
    stage_tables.set_tables(None)


def listing(root):
    return sorted((os.path.join(d, n), os.stat(os.path.join(d, n)).st_mtime_ns)
                  for d, _, names in os.walk(root) for n in names)


def test_the_stage_table_is_built_from_the_install_and_cached_locally(install, tmp_path):
    before = listing(install)
    table = rbr_install.stage_table()
    by_id = {e['id']: e for e in table['stages']}
    assert set(by_id) == {12, 71, 300, 500}
    assert by_id[71]['stage'] == 'Rally School Stage' and by_id[71]['length_m'] == 2200 and by_id[71]['surface'] == 'gravel'
    assert by_id[12]['surface'] == 'mixed' and by_id[12]['surface_parts'] == {'gravel': 0.05, 'snow': 0.95}
    assert by_id[500]['stage'] == 'Only In Tracks' and by_id[500]['length_m'] == 3400 and by_id[500]['surface'] == 'snow'            # Tracks.ini: 0 tarmac, 1 gravel, 2 snow
    assert os.path.isfile(tmp_path / 'data' / 'oversteer' / 'rbr' / 'stages.json')
    assert listing(install) == before                                # the install is only ever read
    # the cache is what the next call reads, until the install's files change
    cached = rbr_install.stage_table()
    assert cached == table
    (install / 'rsfdata' / 'cache' / 'stages_data.json').write_text(json.dumps(STAGES[:1]))
    assert {e['id'] for e in rbr_install.stage_table()['stages']} == {71, 500}


def test_stage_tables_load_the_local_table_with_the_ids_as_keys(install):
    entries = stage_tables.tables()['rbr']
    assert entries['rbr:71']['stage'] == 'Rally School Stage' and entries['rbr:71']['location'] == 'GB'
    assert stage_tables.surface_of(entries['rbr:12']) == ('snow', None)             # 5 % gravel leaves it snow
    assert stage_tables.surface_of(entries['rbr:300'])[0] is None                   # 50/50: only a prior
    assert stage_tables.entry('rbr:71')['length_m'] == 2200


def test_no_install_no_table(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    rbr_install.forget()
    assert rbr_install.stage_table() is None and 'rbr' not in stage_tables.load()
    assert rbr_install.car_in_slot() is None


def test_the_root_can_be_a_file_in_the_data_folder(install, tmp_path, monkeypatch):
    monkeypatch.delenv(rbr_install.ENV)
    os.makedirs(tmp_path / 'data' / 'oversteer' / 'rbr')
    (tmp_path / 'data' / 'oversteer' / 'rbr' / 'root').write_text(str(install) + '\n')
    rbr_install.forget()
    assert rbr_install.find_root() == str(install)


def test_the_car_is_named_from_cars_ini_and_rsf_json(install):
    car = rbr_install.car_in_slot()
    assert car == {'key': 'rbr/5', 'id': 5, 'name': 'Subaru Impreza S12 WRC', 'drivetrain': 'awd', 'gears': 6}
    s = decode_sample(packet())
    assert s.car == 'rbr/5' and s.car_name == 'Subaru Impreza S12 WRC' and s.drivetrain == 'awd' and s.gears == 6


def test_a_car_change_is_seen_when_cars_ini_changes(install):
    assert decode_sample(packet()).car == 'rbr/5'
    (install / 'Cars' / 'Cars.ini').write_text('[Car05]\r\nRSFCarID = 91\r\n')
    assert decode_sample(packet()).car_name == 'Abarth Grande Punto S2000'
    (install / 'Cars' / 'Cars.ini').write_text('[Car05]\r\nCarName = "Whatever"\r\n')
    s = decode_sample(packet())
    assert s.car == 'rbr/unknown' and s.car_name == 'Whatever'                      # no RSFCarID: a shared key


# -- the run lifecycle --

DT = 1.0 / 60
LENGTH = 2300.0
SPEED = 25.0


class Stage:
    """Packets of one attempt at a stage, as RSF/NGP sends them."""

    def __init__(self, t0=0.0, steps0=1839, stage=71, length=LENGTH, hz=60):
        self.t, self.steps, self.stage, self.length = t0, steps0, stage, length
        self.dt, self.spp = 1.0 / hz, int(720 / hz)           # packets a second: 60, or the real 720
        self.out = []
        self.s = 0.0
        self.clock = 0.0
        self.last = None

    def send(self, speed, clock, s, repeat=False, to_end=None):
        if not repeat:
            self.steps += self.spp
        self.t += self.dt
        data = packet(**{'totalSteps': self.steps, 'stage.index': self.stage, 'stage.raceTime': clock,
                         'stage.driveLineLocation': 1.5 + s,
                         'stage.distanceToEnd': max(0.0, self.length - s) if to_end is None else to_end,
                         'control.gear': 3, 'car.speed': speed * 3.6, 'car.velocities.surge': speed,
                         'car.engine.rpm': 4000.0, 'control.throttle': 0.8 if speed else 0.0,
                         'car.positionX': 100.0 + s, 'car.positionY': 50.0, 'car.positionZ': 4.0})
        self.out.append((self.t, data))
        self.last = data

    def countdown(self, seconds=5.0):
        for _ in range(int(seconds / self.dt)):
            self.send(0.0, 0.0, 0.0)
        return self

    def drive(self, until, pause_at=None, pause=0.0):
        """From the go to `until` metres: the clock runs, the car goes SPEED m/s (a pause: the same packet again)."""
        paused = False
        while self.s < until:
            if pause_at is not None and not paused and self.s >= pause_at:
                paused = True
                for _ in range(int(pause / self.dt)):
                    self.t += self.dt
                    self.out.append((self.t, self.last))
            self.s += SPEED * self.dt
            self.clock += self.dt
            self.send(SPEED, self.clock, self.s)
        return self

    def finish(self, roll_on=20.0, results=10.0):
        """The clock stops at the line while the car drives on; then the results page repeats one packet."""
        result = self.clock
        for _ in range(int(roll_on / self.dt)):
            self.s += SPEED * self.dt
            self.send(SPEED, result, self.s)
        for _ in range(int(results / self.dt)):
            self.t += self.dt
            self.out.append((self.t, self.last))
        return self

    def samples(self):
        return [(t, decode_sample(data), 0.8) for t, data in self.out]


def runs_of(tmp_path, *attempts, **kw):
    learner, reader, session = drive_runs(tmp_path, *[a.samples() for a in attempts], **kw)
    return learner, reader, [r for r in session['runs'] if r['distance'] > 100]


def test_a_stage_is_one_run_finished_on_the_frozen_clock(tmp_path):
    stage = Stage().countdown().drive(LENGTH)
    result = stage.clock
    stage.finish()
    learner, reader, runs = runs_of(tmp_path, stage)
    [run] = runs                                                    # no phantom run from the roll-on or the results page
    assert run['finished'] == 1 and run['stage'] == 'rbr:71'
    assert run['result_time'] == pytest.approx(result, abs=1e-3)
    assert 2280 < run['distance'] < 2330
    learner.close()


def test_the_stage_is_named_rbr_stage_id_without_an_install(tmp_path):
    stage = Stage().countdown().drive(LENGTH).finish()
    learner, reader, runs = runs_of(tmp_path, stage)
    row = learner.log.store.stage('rbr:71')
    assert row['name'] == 'RBR stage 71' and row['game'] == 'rbr' and row['length'] == pytest.approx(LENGTH + 1.5, abs=1)
    learner.close()


def test_the_stage_is_named_from_the_install(install, tmp_path):
    stage = Stage().countdown().drive(LENGTH).finish()
    learner, reader, runs = runs_of(tmp_path, stage)
    row = learner.log.store.stage('rbr:71')
    assert row['name'] == 'Rally School Stage' and row['length'] == 2200 and row['location'] == 'GB'
    assert runs[0]['stage'] == 'rbr:71'
    learner.close()


def test_a_restart_is_a_new_run_and_the_steps_going_back_say_so(tmp_path):
    first = Stage().countdown().drive(800.0)                        # restarted from the pause menu after ~30 s
    second = Stage(t0=first.t + 3.0, steps0=300).countdown(3.0).drive(LENGTH).finish()
    result = second.clock
    learner, reader, runs = runs_of(tmp_path, first, second)
    assert [r['finished'] for r in runs] == [0, 1]
    assert runs[1]['result_time'] == pytest.approx(result, abs=1e-3)
    learner.close()


def test_a_restart_while_the_clock_still_reads_zero_is_seen_by_the_steps(tmp_path):
    """The steps go back at the stage load although the old run's last packet and the new one both have the car
    moving and the clock at 0 (no clock reset to read)."""
    first = Stage().countdown().drive(600.0)
    second = Stage(t0=first.t + 2.0, steps0=100).countdown(1.0).drive(LENGTH).finish()
    learner, reader, runs = runs_of(tmp_path, first, second)
    assert len(runs) == 2 and runs[0]['finished'] == 0 and runs[1]['finished'] == 1
    learner.close()


def test_a_pause_mid_stage_repeats_the_step_and_changes_nothing(tmp_path):
    stage = Stage().countdown().drive(LENGTH, pause_at=900.0, pause=30.0)
    result = stage.clock
    stage.finish()
    learner, reader, runs = runs_of(tmp_path, stage)
    [run] = runs
    assert run['finished'] == 1 and run['result_time'] == pytest.approx(result, abs=1e-3)
    assert run['duration'] < result + 5.0                           # the pause is not in the run's time
    learner.close()


def test_packets_stopping_mid_stage_then_resuming_is_one_run(tmp_path):
    stage = Stage().countdown().drive(1000.0)
    stage.t += 25.0                                                 # the game sends nothing for a pause
    stage.drive(LENGTH).finish()
    learner, reader, runs = runs_of(tmp_path, stage)
    [run] = runs
    assert run['finished'] == 1
    learner.close()


def test_a_retired_stage_ends_unfinished_and_the_next_stage_is_another_run(tmp_path):
    first = Stage().countdown().drive(1200.0)                       # retire: the packets simply stop
    second = Stage(t0=first.t + 60.0, steps0=200, stage=12, length=3000.0).countdown().drive(3000.0).finish()
    learner, reader, runs = runs_of(tmp_path, first, second)
    assert [(r['stage'], r['finished']) for r in runs] == [('rbr:71', 0), ('rbr:12', 1)]
    learner.close()


def test_the_results_page_and_the_roll_on_start_no_run_before_the_next_load(tmp_path):
    stage = Stage().countdown().drive(LENGTH).finish(roll_on=30.0, results=60.0)
    nothing = Stage(t0=stage.t + 1.0, steps0=50).countdown(10.0)    # the next stage's countdown: standing
    learner, reader, runs = runs_of(tmp_path, stage, nothing)
    assert len(runs) == 1
    learner.close()


def test_the_clock_stopping_without_a_distance_to_the_end_is_still_the_finish(tmp_path):
    """distanceToEnd not reaching 0 (not proven on a capture): the clock standing still for RBR_STOPPED with the
    car moving finishes the run all the same."""
    stage = Stage().countdown()
    stage.drive(LENGTH - 5.0)
    result = stage.clock
    for _ in range(int(20.0 / DT)):
        stage.s += SPEED * DT
        stage.send(SPEED, result, stage.s, to_end=500.0)
    learner, reader, runs = runs_of(tmp_path, stage)
    [run] = runs
    assert run['finished'] == 1 and run['result_time'] == pytest.approx(result, abs=1e-3)
    learner.close()


# -- marth's captures of 2026-10-26 (stage 81, a 394 m map RSF's list lacks), cut down to fixtures --

DATA = os.path.join(os.path.dirname(__file__), 'data')


def capture_packets(name):
    from oversteer import telemetry_capture as tc
    _meta, records = tc.read_capture(os.path.join(DATA, name))
    return [(t, addr, data) for t, addr, data in records]


def test_the_capture_decodes_as_the_research_says():
    packets = capture_packets('rbr-clock-start.ovcap.gz')
    samples = [decode_sample(d) for _, _, d in packets]
    assert all(s is not None and s.game == 'rbr' and s.stage == 'rbr:81' for s in samples)
    assert all(abs(s.stage_length - 394.31) < 0.05 for s in samples)       # driveLineLocation + distanceToEnd: metres
    assert samples[0].stage_time == 0.0 and samples[-1].stage_time > 0.3   # the clock starts in the slice
    assert [s.steps for s in samples] == list(range(samples[0].steps, samples[0].steps + len(samples)))   # +1 a packet
    moving = [s for s in samples if s.speed > 15.0]
    assert moving
    raw = [struct.unpack_from('<f', d, 60)[0] for _, _, d in packets]
    for s, r in zip(samples, raw):
        if s.speed > 15.0 and s.stage_time > 0:
            assert 3.4 < r / s.speed < 3.8                                 # car.speed is km/h
    assert samples[0].engine_temp > 70.0 and samples[0].tyre_pressure[0] == pytest.approx(220.0, abs=5.0)


def test_a_car_rolling_before_the_clock_starts_is_no_run_and_the_restart_ends_the_run(tmp_path):
    samples = [(t, decode_sample(d), 0.8) for t, _, d in capture_packets('rbr-run-restart.ovcap.gz')]
    learner, reader, session = drive_runs(tmp_path, samples)
    [run] = session['runs']
    assert run['finished'] == 0 and 120 < run['distance'] < 160                # ended by the steps going back
    assert reader.trace(run['id'])[0][0] < 0.5                                 # on the game's clock from its start
    row = learner.log.store.stage('rbr:81')
    assert row['name'] == 'RBR stage 81' and row['length'] == pytest.approx(394.3, abs=0.1)
    learner.close()


def test_a_stage_the_install_lacks_is_named_by_its_id(install, tmp_path):
    samples = [(t, decode_sample(d), 0.8) for t, _, d in capture_packets('rbr-run-restart.ovcap.gz')]
    learner, reader, session = drive_runs(tmp_path, samples)
    assert learner.log.store.stage('rbr:81')['name'] == 'RBR stage 81'
    assert session['runs'][0]['stage'] == 'rbr:81'
    learner.close()


# -- captures are saved at 60 Hz --

def write_capture(path, packets):
    from oversteer import telemetry_capture as tc
    with tc.CaptureWriter(str(path), port=5310) as w:
        for t, addr, data in packets:
            w.write(t, addr, data)
    return w


def test_the_capture_of_a_full_rate_slice_is_thinned_and_keeps_what_changes(tmp_path):
    from oversteer import telemetry_capture as tc
    packets = capture_packets('rbr-clock-start.ovcap.gz')
    w = write_capture(tmp_path / 'thin.ovcap.gz', packets)
    kept = list(tc.read_capture(str(tmp_path / 'thin.ovcap.gz'))[1])
    assert len(kept) * 4 <= len(packets) and len(kept) >= len(packets) / 14        # about 12 times fewer
    clocks = [struct.unpack_from('<f', d, 12)[0] for _, _, d in kept]
    first_running = next(i for i, c in enumerate(clocks) if c > 0)
    assert clocks[first_running - 1] == 0.0                                     # the clock's start is there
    assert kept[0][2] == packets[0][2] and kept[-1][2] == packets[-1][2]        # the first and the last
    assert w.thinned >= len(packets) - len(kept)


def test_thinning_is_for_the_formats_that_ask_for_it(tmp_path):
    from oversteer import telemetry_capture as tc
    other = [(i * 0.001, ('127.0.0.1', 1), bytes([i % 256]) * 232) for i in range(200)]
    write_capture(tmp_path / 'a.ovcap.gz', other)
    assert len(list(tc.read_capture(str(tmp_path / 'a.ovcap.gz'))[1])) == 200


def test_the_thinned_capture_of_a_stage_gives_the_same_run(tmp_path):
    """The same finish, result and restart from the 60 Hz file as from every packet (720 a second)."""
    from oversteer import telemetry_capture as tc
    first = Stage(hz=720, length=500.0).countdown(0.5).drive(500.0)
    first.finish(roll_on=3.0, results=1.0)
    second = Stage(t0=first.t + 5.0, steps0=100, hz=720, length=500.0).countdown(0.5).drive(300.0)   # then restarted
    third = Stage(t0=second.t + 5.0, steps0=50, hz=720, length=500.0).countdown(0.5).drive(500.0).finish(3.0, 1.0)
    everything = first.out + second.out + third.out
    results = []
    for name, packets in (('full', everything), ('thin', None)):
        if packets is None:
            write_capture(tmp_path / 'thin.ovcap.gz', [(t, ('127.0.0.1', 1), d) for t, d in everything])
            packets = [(t, d) for t, _a, d in tc.read_capture(str(tmp_path / 'thin.ovcap.gz'))[1]]
            assert len(packets) * 4 < len(everything)
        learner, reader, session = drive_runs(tmp_path, [(t, decode_sample(d), 0.8) for t, d in packets],
                                              name=name + '.db')
        results.append([(r['finished'], r['result_time'], round(r['distance'], -1)) for r in session['runs']
                        if r['distance'] > 100])
        learner.close()
    full, thin = results
    assert [f for f, _, _ in full] == [1, 0, 1] == [f for f, _, _ in thin]
    for (_, a, da), (_, b, db) in zip(full, thin):
        assert (a is None) == (b is None) and (a is None or abs(a - b) < 1e-3) and da == db
