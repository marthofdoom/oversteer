import json
import os

from oversteer import car_data
from oversteer.stage_tables import bridge_track

SHIPPED = os.path.join(os.path.dirname(__file__), '..', 'data', 'telemetry', 'cars', 'acr.json')


def test_the_shipped_acr_cars_load_under_the_names_the_bridge_sends():
    cars = car_data.load()
    fabia = cars['acr/Skoda Fabia RS Rally2']
    assert fabia['limiter_rpm'] == 7500 and fabia['drivetrain'] == 'awd'
    assert car_data.default_set(fabia)['gears'] == [3.583, 2.538, 1.867, 1.412, 1.136]
    assert abs(fabia['final_drive'] - 4.231) < 1e-3
    assert fabia['shift_lights_rpm']['shift'] == 6900
    assert 'acr/Peugeot 208 Rally4' in cars                  # marth's other car
    with open(SHIPPED, encoding='utf-8') as f:
        table = json.load(f)
    for entry in table['cars']:
        # The key is the game's name as the bridge sends it: ASCII, '_' for
        # anything else, cut to 31 characters
        assert entry['name'] == bridge_track(entry['screen_name'])
        assert len(entry['torque_curve']) >= 10 and entry['gear_sets']
        assert sum(1 for s in entry['gear_sets'] if s['default']) == 1
    assert 'acr/Lancia Fulvia Coup_ HF 1.6' in cars


def test_torque_is_linear_between_the_points():
    fabia = car_data.entry('acr/Skoda Fabia RS Rally2')
    assert car_data.torque(fabia, 7500) == 297.0
    assert abs(car_data.torque(fabia, 7125) - (320 + 309) / 2) < 1e-9
    assert car_data.torque(fabia, 9000) == 0.0


def test_the_gear_set_in_use_is_told_by_the_steps_between_learnt_ratios():
    polo = car_data.entry('acr/VW Polo GTI R5')
    # rpm per m/s = gear x final x 60 / (2 pi r): the scale does not matter
    learnt = {g: r * 13.3 for g, r in enumerate([3.333, 2.385, 1.813, 1.438, 1.182], 1)}
    found, miss = car_data.match_set(polo, learnt)
    assert found['id'] == 'VWPoloGTIR5Set0' and miss < 1e-6
    found, miss = car_data.match_set(polo, {})
    assert found['default'] and miss is None


def test_a_gearing_off_the_game_data_is_told_from_wheelspin():
    """The final drive and tyre scale every gear alike; steady wheelspin
    shortens the low gears only (the Fabia's 1st and 2nd read 4-6 % short
    on gravel against a 3rd to 5th that fit): what is left is the miss."""
    gear_set = {'gears': [3.583, 2.538, 1.867, 1.412, 1.136]}
    fits = {g + 1: r * 125.0 * 1.02 for g, r in enumerate(gear_set['gears'])}
    assert car_data.unexplained_miss(gear_set, fits, 0.04) < 1e-9
    spin = {**fits, 1: fits[1] * 1.036, 2: fits[2] * 1.056}
    assert car_data.unexplained_miss(gear_set, spin, 0.04) < 1e-9
    longer = {**fits, 2: fits[2] * 0.94}                 # longer than the set: no spin does that
    assert abs(car_data.unexplained_miss(gear_set, longer, 0.04) - 0.06) < 1e-9
    third = {**fits, 3: fits[3] * 1.06}                        # shorter, but above gears that fit: not spin
    assert abs(car_data.unexplained_miss(gear_set, third, 0.04) - 0.06) < 1e-9
    assert car_data.unexplained_miss(gear_set, {1: fits[1], 3: fits[3]}, 0.04) is None     # no two adjacent
    assert abs(car_data.unexplained_miss(gear_set, {4: fits[4] * 1.05, 5: fits[5]}, 0.04) - 0.05) < 1e-9
