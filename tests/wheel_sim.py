"""A simulated force feedback wheel behind the calibration's hardware interface (oversteer.wheel_calibration):
no sysfs, no evdev. Simulated time only advances in sleep_until, so a two-minute run takes a few seconds.

The wheel: inertia, Coulomb friction with higher static friction that varies around the rim, viscous damping,
a motor with a dead zone and a non-linear torque curve, the driver's 2 ms tick, 8-bit force steps, ext_force
under gain and ext_force_cap, the game's force (a spring) under game_ffb_level, the centring spring, a latency
from the tick to the torque, an encoder coarser than the axis, reports every millisecond on change, end
stops, and the driver's ext_force watchdog. The shaping attributes are kept (and validated like the driver
does) but not applied to the physics: during a run they must be identity, which the tests check.

Disturbances: grab(t) puts a hand on the rim at time t (more inertia, a stiff spring to where it was and
damping), unplug(t) makes every read and write fail from t.
"""
import errno
import math

FULL = 32767


class SimWheel:
    def __init__(self, inertia=1 / 5000.0, coulomb=0.035, static=0.05, static_wobble=0.25, damping=4e-5,
                 dead_zone=0.06, curve=3.0, latency=0.012, encoder_deg=360 / 4096, ext_sign=-1,
                 max_range=900, range=540, gain=110, autocenter=30, start_deg=0.0, game_spring=0.002,
                 stop_deg=450.0, dt=0.0005, tick=0.002, report=0.001):
        self.J = inertia                    # full scale per deg/s^2
        self.coulomb = coulomb              # full scale
        self.static = static
        self.static_wobble = static_wobble
        self.b = damping                    # full scale per deg/s
        self.dead_zone = dead_zone
        self.curve = curve                  # 0: linear past the dead zone; larger: stronger at the bottom
        self.latency = latency
        self.q = encoder_deg
        self.ext_sign = ext_sign
        self._max_range = max_range
        self.range = range
        self.gain = gain
        self.autocenter = autocenter
        self.game_spring = game_spring      # full scale per degree, what the game plays
        self.stop_deg = stop_deg
        self.dt = dt
        self.tick = tick
        self.tick_s = tick
        self.report = report
        self.attrs = {'game_ffb_level': '100', 'ext_force': '0', 'ext_force_cap': '80', 'ffb_lut': 'identity',
                      'ffb_deadband': '0', 'ffb_dither': '0', 'ffb_soft_clip': '0'}
        self.t = 100.0
        self.theta = start_deg
        self.omega = 0.0
        self.stuck = True
        self.motor = 0.0                    # the torque command in effect (full scale, after latency)
        self.pipeline = []                  # (time it takes effect, command)
        self.next_tick = self.t
        self.next_report = self.t
        self.game_level = 1.0
        self.last_force_write = -1e9
        self.reports = []
        self.last_raw = None
        self.writes = []                    # (time, name, value)
        self.attempts = []                  # every write tried, also those that failed
        self.max_abs_theta = abs(start_deg)
        self.max_abs_omega = 0.0
        self.max_motor = 0.0
        self.open_count = 0
        self.closed = True
        self.grab_at = None
        self.grab_pos = None
        self.unplug_at = None
        self.teleports = []
        self.high_runs = []                 # (start, end) of motor commands above 60 %
        self._high_start = None
        self.impure_ticks = 0
        self.asked = {}                     # settings asked for by the application during a run
        self.held = False
        self.blind_at = None                # from then on no reports arrive (another program grabbed the node)
        self.stall_high = None              # (after, seconds): from then, one pause this long during a pulse

    # --- disturbances ---

    def grab(self, after):
        self.grab_at = self.t + after

    def unplug(self, after):
        self.unplug_at = self.t + after

    def _gone(self):
        return self.unplug_at is not None and self.t >= self.unplug_at

    def _fail(self):
        if self._gone():
            raise OSError(errno.ENODEV, "No such device")

    # --- the motor ---

    def torque_of(self, u):
        """The motor's torque (share of full) for a command u (signed share of full)."""
        a = abs(u)
        if a <= self.dead_zone:
            return 0.0
        v = (min(a, 1.0) - self.dead_zone) / (1.0 - self.dead_zone)
        if self.curve > 0:
            v = (1 - math.exp(-self.curve * v)) / (1 - math.exp(-self.curve))
        return math.copysign(v, u)

    def static_at(self, theta):
        return self.static * (1 + self.static_wobble * math.sin(math.radians(theta) * 3))

    def _driver_tick(self):
        """What the driver sends this tick: game force under game_ffb_level, ext_force under gain and cap,
        the 8-bit step; the watchdog fades a stale ext_force."""
        target = int(self.attrs['game_ffb_level']) / 100.0
        step = self.tick / 0.2                          # 100 % per 200 ms
        self.game_level += max(-step, min(step, target - self.game_level))
        game = -self.game_spring * self.theta * self.game_level * self.ext_sign     # centring, in ext_force's sign
        ext = int(self.attrs['ext_force']) / FULL
        age = self.t - self.last_force_write
        if age > 0.030:
            ext *= max(0.0, 1 - (age - 0.030) / 0.100)
        ext *= self.gain / 100.0
        cap = int(self.attrs['ext_force_cap']) / 100.0
        ext = max(-cap, min(cap, ext))
        u = max(-1.0, min(1.0, game * self.gain / 100.0 + ext))
        steps = math.floor((u * FULL + 32768) / 256) * 256 - 32768
        u = steps / FULL
        self.pipeline.append((self.t + self.latency, u))
        if int(self.attrs['ext_force']):
            # a test force played with the game's force still in, a centring spring, or shaping on
            shaped = any(self.attrs[n] not in ('identity', '0') for n in ('ffb_lut', 'ffb_deadband', 'ffb_dither',
                                                                          'ffb_soft_clip'))
            if self.game_level > 0.001 or self.autocenter or shaped or self.gain != 100 or \
                    self.attrs['ext_force_cap'] != '100':
                self.impure_ticks += 1

    def _step(self):
        dt = self.dt
        if self.t >= self.next_tick:
            self._driver_tick()
            self.next_tick += self.tick
        while self.pipeline and self.pipeline[0][0] <= self.t:
            self.motor = self.pipeline.pop(0)[1]
        if abs(self.motor) > 0.6 + 1.0 / 128:          # (the 8-bit step makes -60 % a little more)
            if self._high_start is None:
                self._high_start = self.t
        elif self._high_start is not None:
            self.high_runs.append((self._high_start, self.t))
            self._high_start = None
        self.max_motor = max(self.max_motor, abs(self.motor))
        tau = self.ext_sign * self.torque_of(self.motor)
        tau -= 0.004 * self.theta * self.autocenter / 100.0
        J = self.J
        b = self.b
        if abs(self.theta) > self.stop_deg:
            tau -= 0.05 * (self.theta - math.copysign(self.stop_deg, self.theta))
            b += 0.002
        if self.grab_at is not None and self.t >= self.grab_at:
            if self.grab_pos is None:
                self.grab_pos = self.theta
            J *= 5
            tau -= 0.02 * (self.theta - self.grab_pos)
            b += 0.002
        if self.stuck:
            if abs(tau) <= self.static_at(self.theta):
                self.omega = 0.0
            else:
                self.stuck = False
                self.omega += (tau - math.copysign(self.coulomb, tau)) / J * dt
        else:
            fric = math.copysign(self.coulomb, self.omega) if self.omega else math.copysign(self.coulomb, tau)
            new = self.omega + (tau - fric - b * self.omega) / J * dt
            if new * self.omega < 0 or new == 0:
                if abs(tau) <= self.static_at(self.theta):
                    new = 0.0
                    self.stuck = True
            self.omega = new
        self.theta += self.omega * dt
        self.max_abs_theta = max(self.max_abs_theta, abs(self.theta))
        self.max_abs_omega = max(self.max_abs_omega, abs(self.omega))
        self.t += dt
        if self.t >= self.next_report:
            raw = self.raw()
            if raw != self.last_raw:
                self.reports.append((self.next_report, raw))
                self.last_raw = raw
            self.next_report += self.report

    def raw(self):
        q = math.floor(self.theta / self.q) * self.q
        raw = 32768 + int(round(q / (self.range / 2.0) * 32768))
        return max(0, min(65535, raw))

    # --- the hardware interface ---

    def open(self):
        self._fail()
        self.open_count += 1
        self.closed = False
        self.reports = []
        self.last_raw = self.raw()

    def close(self):
        self.closed = True

    def now(self):
        return self.t

    def sleep_until(self, t):
        if self.stall_high is not None and self.t >= self.stall_high[0] and \
                abs(int(self.attrs['ext_force'])) > 0.6 * FULL + 256:
            t += self.stall_high[1]                 # the process is held up (a collector, the scheduler)
            self.stall_high = None
        while self.t < t:
            self._step()

    def read(self, name):
        self._fail()
        return self.attrs[name]

    def write(self, name, value):
        self.attempts.append((self.t, name, str(value)))
        self._fail()
        value = str(value).strip()
        if name == 'ffb_lut':
            if value not in ('', 'identity'):
                vals = [int(v) for v in value.split()]
                if not 2 <= len(vals) <= 65 or any(not 0 <= v <= FULL for v in vals) or \
                        any(b < a for a, b in zip(vals, vals[1:])):
                    raise OSError(errno.EINVAL, "Invalid argument")
            else:
                value = 'identity'
        elif name in ('game_ffb_level', 'ext_force_cap', 'ffb_soft_clip'):
            value = str(min(int(value), 100))
        elif name == 'ext_force':
            value = str(max(-32768, min(32767, int(value))))
            self.last_force_write = self.t
        self.writes.append((self.t, name, value))
        self.attrs[name] = value

    def force(self, raw):
        self.write('ext_force', int(raw))

    def poll(self):
        self._fail()
        out, self.reports = self.reports, []
        if self.blind_at is not None and self.t >= self.blind_at:
            return []
        return out

    def position_now(self):
        self._fail()
        return self.raw()

    def blind(self, after):
        self.blind_at = self.t + after

    def position_raw(self):
        self._fail()
        return self.raw()

    def get_gain(self):
        self._fail()
        return self.gain

    def set_gain(self, v):
        self.attempts.append((self.t, 'gain', v))
        self._fail()
        self.writes.append((self.t, 'gain', v))
        self.gain = v

    def get_autocenter(self):
        self._fail()
        return self.autocenter

    def set_autocenter(self, v):
        self.attempts.append((self.t, 'autocenter', v))
        self._fail()
        self.writes.append((self.t, 'autocenter', v))
        self.autocenter = v

    def get_range(self):
        self._fail()
        return self.range

    def set_range(self, v):
        self.attempts.append((self.t, 'range', v))
        self._fail()
        self.writes.append((self.t, 'range', v))
        self.range = v

    def max_range(self):
        return self._max_range

    def hold(self):
        self.held = True

    def release(self):
        self.held = False
        asked, self.asked = self.asked, {}
        return asked

    # --- for the tests ---

    def last(self, name):
        for _t, n, v in reversed(self.writes):
            if n == name:
                return v
        return None
