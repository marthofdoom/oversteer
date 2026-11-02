import errno
from locale import gettext as _
from evdev import ecodes, ff
import logging
import numpy as np
from threading import Thread
import time

class TestAborted(Exception):
    """A test that can't go on (no force feedback, no answer from the user); the message is shown."""
    __test__ = False


class Test:
    __test__ = False   # not a pytest class
    # How long test 1 waits for the user to feel the wheel move and press a button
    ACTION_TIMEOUT = 120
    # The tests push with at most this share of full force (a wheel left unattended or a button never pressed
    # must not slam into its stop at 100 %), and not further into the lock than this share of the travel
    MAX_FORCE = 0.6
    LOCK_LIMIT = 0.9

    def __init__(self, device, callback, restore=None):
        self.device = device
        # The settings to put back afterwards (keys range, ff_gain, autocenter), when the application
        # knows them: a driver without the sysfs files can't read them back (it answers 100 and 0)
        self.restore = restore or {}
        self.input_device = self.device.get_input_device()
        self.notify = callback
        self.collecting_data = False
        self.awaiting_action = False
        self.minimum_level = 0
        self.input_values = []
        self.output_values = []
        self.action_triggered = False
        self.test_starttime = time.time()
        self.current_range = None
        self.current_ff_gain = None
        self.current_autocenter = None
        self.effects = []

    def get_input_values(self):
        return self.input_values

    def get_output_values(self):
        return self.output_values

    def get_minimum_level(self):
        return self.minimum_level

    def is_collecting_data(self):
        return self.collecting_data

    def is_awaiting_action(self):
        return self.awaiting_action

    def trigger_action(self):
        self.action_triggered = True

    def has_output_data(self):
        """True when wheel positions arrived during the run: past the seed point at t=0 there is at
        least one sample. Without them the charts have nothing to work on."""
        return len(self.output_values) > 1

    def start(self):
        self.input_values = []
        self.output_values = []
        self.effects = []
        if self.input_device is None:
            raise TestAborted(_('The wheel\'s input device could not be opened.'))

        # Save wheel settings
        self.current_range = self._setting('range', self.device.get_range)
        self.current_ff_gain = self._setting('ff_gain', self.device.get_ff_gain)
        self.current_autocenter = self._setting('autocenter', self.device.get_autocenter)

        # Prepare wheel
        try:
            for effect_id in range(self.input_device.ff_effects_count):
                self.input_device.erase_effect(effect_id)
        except OSError:
            pass

        self.device.set_range(900)
        self.device.set_ff_gain(100)
        self.device.set_autocenter(0)

    def _setting(self, key, read):
        value = self.restore.get(key)
        return value if value is not None else read()

    def stop(self):
        # Drop any effect a failed run left behind
        for effect in list(self.effects):
            try:
                self.erase_effect(effect)
            except OSError:
                pass
        self.effects = []

        # Restore wheel settings (those that were read before the run got started)
        if self.current_range is not None:
            self.device.set_range(self.current_range)
        if self.current_ff_gain is not None:
            self.device.set_ff_gain(self.current_ff_gain)
        if self.current_autocenter is not None:
            self.device.set_autocenter(self.current_autocenter)
        self.current_range = self.current_ff_gain = self.current_autocenter = None

    def upload_effect(self, effect):
        try:
            effect.id = self.input_device.upload_effect(effect)
        except OSError as e:
            if e.errno == errno.ENOSYS:
                raise TestAborted(_('This wheel or its driver does not support the force feedback effects the '
                                    'tests use.')) from e
            raise
        self.effects.append(effect)
        return effect.id

    def create_left_effect(self, level = 0x7fff):
        left_effect = ff.Effect(
            ecodes.FF_CONSTANT, -1, 0x4000,
            ff.Trigger(0, 0),
            ff.Replay(0, 0),
            ff.EffectType(ff_constant_effect=ff.Constant(level=level))
        )
        self.upload_effect(left_effect)
        return left_effect

    def create_right_effect(self, level = 0x7fff):
        right_effect = ff.Effect(
            ecodes.FF_CONSTANT, -1, 0xc000,
            ff.Trigger(0, 0),
            ff.Replay(0, 0),
            ff.EffectType(ff_constant_effect=ff.Constant(level=level))
        )
        self.upload_effect(right_effect)
        return right_effect

    def update_effect(self, effect):
        self.input_device.upload_effect(effect)

    def erase_effect(self, effect):
        if effect in self.effects:
            self.effects.remove(effect)
        self.input_device.erase_effect(effect.id)

    def run(self, test_id):
        test = (self.test1, self.test2, self.test3)[test_id]
        Thread(target=self._guarded, args=(test,), daemon=True).start()

    def _guarded(self, test):
        """Run one test so that it always ends: the wheel's settings are put back and the application is
        told, with 'end' when it finished or 'error' and a message when it could not."""
        error = None
        try:
            self.start()
            test()
        except TestAborted as e:
            error = str(e)
        except OSError as e:
            logging.exception("wheel test")
            error = str(e)
        finally:
            self.collecting_data = False
            self.awaiting_action = False
            try:
                self.stop()
            except OSError:
                logging.exception("restoring the wheel after the test")
        if error is None:
            self.notify()
        else:
            self.notify('error', error)

    def append_data(self, timestamp, value):
        if self.collecting_data is False:
            return
        self.output_values.append((timestamp - self.test_starttime, (value - 32768) / 32768))

    def seed_axis_position(self):
        # Move the wheel a bit to start collecting data
        effect = self.create_left_effect(0x2000)
        self.input_device.write(ecodes.EV_FF, effect.id, 1)
        time.sleep(0.1)
        self.input_device.write(ecodes.EV_FF, effect.id, 0)
        self.erase_effect(effect)

    def level_cap(self):
        return int(0x7fff * self.MAX_FORCE)

    def near_lock(self, sign):
        """True when the wheel is within the lock margin in that direction (+1 right, -1 left): the push is skipped."""
        try:
            value = self.device.get_last_axis_value(ecodes.ABS_X)
        except (KeyError, AttributeError):
            return False
        if value is None:
            return False
        return sign * (value - 32768) / 32768 > self.LOCK_LIMIT

    def center_wheel(self):
        # Center wheel
        self.device.set_autocenter(100)
        time.sleep(1)
        self.device.set_autocenter(0)

        # Wait for wheel to stabilize and all events to arrive
        time.sleep(0.5)

    # Minimum torque test
    def test1(self):
        self.center_wheel()

        # Start test
        self.action_triggered = False
        self.awaiting_action = True
        deadline = time.monotonic() + self.ACTION_TIMEOUT
        while not self.action_triggered:
            if time.monotonic() > deadline:
                raise TestAborted(_('Timed out waiting for you to press a button on the wheel.'))
            time.sleep(0.5)
        self.action_triggered = False
        self.notify('running')

        left_effect = self.create_left_effect(0)
        right_effect = self.create_right_effect(0)
        self.input_device.write(ecodes.EV_FF, left_effect.id, 1)
        self.input_device.write(ecodes.EV_FF, right_effect.id, 1)

        # Increasing force square effect (up to MAX_FORCE, for no longer than the wait for the button)
        deadline = time.monotonic() + self.ACTION_TIMEOUT
        for level in range(0, self.level_cap(), 30):
            if time.monotonic() > deadline:
                self.erase_effect(left_effect)
                self.erase_effect(right_effect)
                raise TestAborted(_('Timed out waiting for you to press a button on the wheel.'))
            left_effect.u.ff_constant_effect.level = 0 if self.near_lock(-1) else level
            self.update_effect(left_effect)
            time.sleep(0.1)
            left_effect.u.ff_constant_effect.level = 0
            self.update_effect(left_effect)
            right_effect.u.ff_constant_effect.level = 0 if self.near_lock(1) else level
            self.update_effect(right_effect)
            time.sleep(0.1)
            right_effect.u.ff_constant_effect.level = 0
            self.update_effect(right_effect)
            if self.action_triggered:
                self.minimum_level = level
                break

        self.erase_effect(left_effect)
        self.erase_effect(right_effect)

        self.awaiting_action = False

    def test2(self):
        self.seed_axis_position()
        self.center_wheel()

        left_effect = self.create_left_effect(0)
        right_effect = self.create_right_effect(0)

        # Set the starting points for the test
        starting_wheel_pos = (self.device.get_last_axis_value(ecodes.ABS_X) - 32768) / 32768
        self.input_values = [(0, 0)]
        self.output_values = [(0, starting_wheel_pos)]
        self.test_starttime = time.time()
        time.sleep(0.1)

        # Start collecting data
        self.collecting_data = True

        direction = 1
        for level in np.arange(0, self.level_cap() + 1, self.level_cap() / 50):
            level = int(round(level))

            if direction == 1:
                # Move wheel right
                right_effect.u.ff_constant_effect.level = 0 if self.near_lock(1) else level
                self.update_effect(right_effect)
                self.input_device.write(ecodes.EV_FF, right_effect.id, 1)
                self.input_values.append((time.time() - self.test_starttime, level / 0x7fff))
                time.sleep(0.3)
                self.input_device.write(ecodes.EV_FF, right_effect.id, 0)
            else:
                # Move wheel left
                left_effect.u.ff_constant_effect.level = 0 if self.near_lock(-1) else level
                self.update_effect(left_effect)
                self.input_device.write(ecodes.EV_FF, left_effect.id, 1)
                self.input_values.append((time.time() - self.test_starttime, -level / 0x7fff))
                time.sleep(0.3)
                self.input_device.write(ecodes.EV_FF, left_effect.id, 0)
            direction = 3 - direction

        self.input_values.append((time.time() - self.test_starttime, 0))

        # Stop collecting data
        self.collecting_data = False

        self.erase_effect(left_effect)
        self.erase_effect(right_effect)

    def test3(self):
        self.seed_axis_position()
        self.center_wheel()

        left_effect = self.create_left_effect(self.level_cap())
        right_effect = self.create_right_effect(self.level_cap())

        # Set the starting points for the test
        starting_wheel_pos = (self.device.get_last_axis_value(ecodes.ABS_X) - 32768) / 32768
        self.input_values = [(0, 0)]
        self.output_values = [(0, starting_wheel_pos)]
        self.test_starttime = time.time()
        time.sleep(0.1)

        # Start collecting data
        self.collecting_data = True

        # Move wheel right at top speed
        self.input_values.append((time.time() - self.test_starttime, 1))
        if not self.near_lock(1):
            self.input_device.write(ecodes.EV_FF, right_effect.id, 1)
        time.sleep(0.3)
        self.input_device.write(ecodes.EV_FF, right_effect.id, 0)

        # Move wheel left at top speed
        self.input_values.append((time.time() - self.test_starttime, -1))
        if not self.near_lock(-1):
            self.input_device.write(ecodes.EV_FF, left_effect.id, 1)
        time.sleep(0.3)
        self.input_device.write(ecodes.EV_FF, left_effect.id, 0)

        # Move wheel right at top speed
        self.input_values.append((time.time() - self.test_starttime, 1))
        if not self.near_lock(1):
            self.input_device.write(ecodes.EV_FF, right_effect.id, 1)
        time.sleep(0.3)
        self.input_device.write(ecodes.EV_FF, right_effect.id, 0)
        self.input_values.append((time.time() - self.test_starttime, 0))

        # Keep collecting deceleration data
        time.sleep(0.5)
        self.input_values.append((time.time() - self.test_starttime, 0))

        # Stop collecting data
        self.collecting_data = False

        self.erase_effect(left_effect)
        self.erase_effect(right_effect)
