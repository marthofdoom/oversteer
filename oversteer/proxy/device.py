"""Runtime for one proxy device.

A :class:`ProxyDevice` owns a thread that:

- waits until every required source is plugged in,
- creates the virtual (uinput) device with the identity and capabilities from
  the spec,
- grabs the sources and translates their events according to the mappings,
- forwards force feedback (uploads, erases, play/stop, gain, autocenter)
  from the virtual device to the ``ff`` source, translating effect ids,
- re-attaches sources when they are unplugged and plugged back in.
"""

import ctypes
from enum import Enum
import errno
from evdev import InputDevice, UInput, ecodes, ff, list_devices
import logging
import os
import select
import threading
import time

from .spec import code_name
from . import uinput_ff

# Types never copied from a passthrough source: we generate SYN ourselves and
# FF is handled by the passthrough machinery.
_NEVER_COPY = (ecodes.EV_SYN, ecodes.EV_FF, ecodes.EV_FF_STATUS)

_RESCAN_MIN = 1.0
_RESCAN_MAX = 10.0


class ProxyState(Enum):
    STOPPED = 'stopped'
    WAITING = 'waiting'      # virtual device not created: a required source is missing
    RUNNING = 'running'
    DEGRADED = 'degraded'    # virtual device exists but a source went away
    ERROR = 'error'


class _FakeEvent:
    __slots__ = ('type', 'code', 'value')

    def __init__(self, etype, code, value):
        self.type, self.code, self.value = etype, code, value


class _Source:
    """An attached physical device."""

    def __init__(self, spec, device):
        self.spec = spec
        self.device = device
        self.path = device.path
        self.absinfo = {}
        caps = device.capabilities(absinfo=True)
        for code, info in caps.get(ecodes.EV_ABS, []):
            self.absinfo[code] = info
        self.grabbed = False

    def grab(self):
        try:
            self.device.grab()
            self.grabbed = True
        except OSError as e:
            logging.warning("proxy: could not grab %s (%s): %s", self.device.name, self.path, e)

    def close(self):
        if self.grabbed:
            try:
                self.device.ungrab()
            except OSError:
                pass
            self.grabbed = False
        try:
            self.device.close()
        except OSError:
            pass


class ProxyDevice:

    def __init__(self, spec, on_change=None):
        self.spec = spec
        self.on_change = on_change
        self.state = ProxyState.STOPPED
        self.error = None
        self.devnode = None
        self.ui = None
        self.sources = {}                 # key -> _Source
        self.last_values = {}             # (type, code) -> last value written
        self.events_in = 0
        self.events_out = 0
        self.ff_effects = {}              # virtual id -> real id
        self._ff_cache = {}               # virtual id -> bytes of the uploaded effect
        self._key_state = {}              # (source key, to_code) -> pressed, for ABS -> KEY
        self._auto_invert = {}            # (source key, from_code) -> bool, for 'auto' inversion
        self._pressed_by = {}             # source key -> set of virtual key codes it holds down
        self._ff_state = {}               # FF_GAIN / FF_AUTOCENTER last values, replayed on reconnect
        self._rules = {}                  # (source key, from_type, from_code) -> [Mapping]
        for m in spec.mappings:
            self._rules.setdefault((m.source, m.from_type, m.from_code), []).append(m)
        self._thread = None
        self._lock = threading.Lock()
        self._wake_r, self._wake_w = os.pipe()
        self._stop = False
        self._rescan_now = False

    # --- public API -------------------------------------------------------

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, name='proxy:' + self.spec.id, daemon=True)
        self._thread.start()

    def stop(self, timeout=3.0):
        self._stop = True
        self._wake()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        for fd in (self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass
        self._wake_r = self._wake_w = -1

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def poke(self):
        """Ask the proxy to look for missing sources right away."""
        self._rescan_now = True
        self._wake()

    def missing_sources(self):
        with self._lock:
            return [key for key in self.spec.sources if key not in self.sources]

    def attached_sources(self):
        with self._lock:
            return {key: src.path for key, src in self.sources.items()}

    def status(self):
        with self._lock:
            return {
                'id': self.spec.id,
                'state': self.state.value,
                'error': self.error,
                'devnode': self.devnode,
                'sources': {key: (self.sources[key].path if key in self.sources else None)
                            for key in self.spec.sources},
                'events_in': self.events_in,
                'events_out': self.events_out,
                'ff_effects': len(self.ff_effects),
            }

    # --- internals --------------------------------------------------------

    def _wake(self):
        if self._wake_w < 0:
            return
        try:
            os.write(self._wake_w, b'x')
        except OSError:
            pass

    def _set_state(self, state, error=None):
        changed = state != self.state or error != self.error
        self.state = state
        self.error = error
        if changed:
            logging.info("proxy %s: %s%s", self.spec.id, state.value, (' (' + error + ')') if error else '')
            if self.on_change is not None:
                self.on_change(self)

    def _run(self):
        try:
            self._set_state(ProxyState.WAITING)
            backoff = _RESCAN_MIN
            next_scan = 0
            while not self._stop:
                now = time.monotonic()
                if self.missing_sources() and (self._rescan_now or now >= next_scan):
                    self._rescan_now = False
                    found = self._scan_sources()
                    backoff = _RESCAN_MIN if found else min(backoff * 2, _RESCAN_MAX)
                    next_scan = time.monotonic() + backoff
                    if self.ui is None and self._required_present():
                        self._create_device()
                    self._update_state()
                timeout = max(0.05, next_scan - time.monotonic()) if self.missing_sources() else None
                self._pump(timeout)
        except Exception as e:
            logging.exception("proxy %s: fatal error", self.spec.id)
            self._set_state(ProxyState.ERROR, str(e))
        finally:
            self._teardown()
            if self.state != ProxyState.ERROR:
                self._set_state(ProxyState.STOPPED)

    def _required_present(self):
        with self._lock:
            return all(not s.required or key in self.sources for key, s in self.spec.sources.items())

    def _update_state(self):
        if self.ui is None:
            self._set_state(ProxyState.WAITING)
        elif self.missing_sources():
            self._set_state(ProxyState.DEGRADED)
        else:
            self._set_state(ProxyState.RUNNING)

    def _scan_sources(self):
        """Attach any unattached source that is now present. Returns True if one was found."""
        found = False
        own = self.devnode
        for path in list_devices():
            with self._lock:
                attached = {src.path for src in self.sources.values()}
                wanted = [s for key, s in self.spec.sources.items() if key not in self.sources]
            if not wanted:
                break
            if path == own or path in attached:
                continue
            try:
                device = InputDevice(path)
                spec = next((s for s in wanted if s.matches(device)), None)
                if spec is None:
                    device.close()
                    continue
                self._attach(spec, device)
                found = True
            except OSError as e:          # unplugged between open and use
                logging.debug("proxy %s: %s: %s", self.spec.id, path, e)
                continue
        return found

    def _attach(self, spec, device):
        source = _Source(spec, device)
        if spec.grab:
            source.grab()
        with self._lock:
            self.sources[spec.key] = source
        # 'auto' inversion: an axis parked in its upper half at attach time is
        # inverted so games see it released, not pulled.
        for (key, etype, code), rules in self._rules.items():
            if key != spec.key or etype != ecodes.EV_ABS:
                continue
            info = source.absinfo.get(code)
            if info is not None and any(r.invert == 'auto' for r in rules):
                # Only a clear rest position decides; a centred axis stays as is
                span = info.max - info.min
                if span > 0 and info.value >= info.min + span * 3 // 4:
                    self._auto_invert[(key, code)] = True
                elif span > 0 and info.value <= info.min + span // 4:
                    self._auto_invert[(key, code)] = False
                else:
                    self._auto_invert.setdefault((key, code), False)
        logging.info("proxy %s: attached %s = %s (%s)", self.spec.id, spec.key, device.name, device.path)
        if self.ui is not None:
            self._sync_axes(source)
            if spec.key == self.spec.ff_source:
                self._restore_effects(source)
        if self.on_change is not None:
            self.on_change(self)

    def _detach(self, key, reason=''):
        with self._lock:
            source = self.sources.pop(key, None)
        if source is None:
            return
        logging.info("proxy %s: lost %s (%s) %s", self.spec.id, key, source.path, reason)
        source.close()
        self._release_keys_from(key)
        if key == self.spec.ff_source:
            # Effect ids on the real device are gone; keep the cache so they
            # can be re-uploaded when it comes back.
            self.ff_effects.clear()
        self._rescan_now = True
        self._update_state()

    def _build_capabilities(self):
        caps = {}
        spec = self.spec
        if spec.passthrough is not None:
            with self._lock:
                source = self.sources[spec.passthrough]
            for etype, codes in source.device.capabilities(absinfo=True).items():
                if etype in _NEVER_COPY:
                    continue
                caps[etype] = list(codes)
        keys = list(caps.get(ecodes.EV_KEY, []))
        keys += [c for c in spec.key_codes() if c not in keys]
        if keys:
            caps[ecodes.EV_KEY] = keys
        abs_codes = {c for c, _ in caps.get(ecodes.EV_ABS, [])}
        abs_list = list(caps.get(ecodes.EV_ABS, []))
        for code, info in spec.abs.items():
            if code in abs_codes:
                abs_list = [entry for entry in abs_list if entry[0] != code]
            abs_list.append((code, info.as_tuple()))
        if abs_list:
            caps[ecodes.EV_ABS] = abs_list
        max_effects = 0
        if spec.ff_source is not None:
            with self._lock:
                ffsrc = self.sources.get(spec.ff_source)
            if ffsrc is not None:
                ff_caps = ffsrc.device.capabilities().get(ecodes.EV_FF, [])
                if ff_caps:
                    caps[ecodes.EV_FF] = list(ff_caps)
                    max_effects = ffsrc.device.ff_effects_count
        return caps, max_effects

    def _create_device(self):
        caps, max_effects = self._build_capabilities()
        ident = self.spec.identity
        kwargs = dict(name=ident.name, vendor=ident.vendor, product=ident.product,
                      version=ident.version, bustype=ident.bustype)
        if ident.phys:
            kwargs['phys'] = ident.phys
        if max_effects:
            kwargs['max_effects'] = max_effects
        self.ui = UInput(caps, **kwargs)
        self.devnode = self.ui.device.path
        logging.info("proxy %s: created %s as %r", self.spec.id, self.devnode, ident.name)
        with self._lock:
            sources = list(self.sources.values())
        for source in sources:
            self._sync_axes(source)

    def _sync_axes(self, source):
        """Push the source's current axis positions and pressed keys through
        the mappings so the virtual device starts coherent (a pedal already
        pressed, a shifter already in gear)."""
        wrote = False
        key = source.spec.key
        for code, info in source.absinfo.items():
            rules = self._rules.get((key, ecodes.EV_ABS, code))
            if rules:
                for rule in rules:
                    if rule.to_type == ecodes.EV_ABS:
                        wrote |= self._apply(rule, source, _FakeEvent(ecodes.EV_ABS, code, info.value))
            elif self.spec.passthrough == key:
                self._write(ecodes.EV_ABS, code, info.value)
                wrote = True
        try:
            pressed = set(source.device.active_keys())
        except OSError:
            pressed = set()
        for code in pressed:
            rules = self._rules.get((key, ecodes.EV_KEY, code))
            if rules:
                for rule in rules:
                    wrote |= self._apply(rule, source, _FakeEvent(ecodes.EV_KEY, code, 1))
            elif self.spec.passthrough == key:
                self._write(ecodes.EV_KEY, code, 1, key)
                wrote = True
        if wrote:
            self.ui.syn()

    def _release_keys_from(self, key):
        """A source went away: release every virtual key it could have been
        holding (a shifter unplugged in gear must not leave the gear engaged)."""
        if self.ui is None:
            return
        wrote = False
        for code in sorted(self._pressed_by.get(key, ())):
            self._write(ecodes.EV_KEY, code, 0)
            wrote = True
        self._pressed_by[key] = set()
        for (src, etype, code), rules in self._rules.items():
            if src != key or etype != ecodes.EV_ABS:
                continue
            for rule in rules:
                if rule.to_type == ecodes.EV_ABS and rule.to_code in self.spec.abs:
                    self._write(ecodes.EV_ABS, rule.to_code, self.spec.abs[rule.to_code].min)
                    wrote = True
        for state_key in [k for k in self._key_state if k[0] == key]:
            self._key_state[state_key] = False
        if wrote:
            self.ui.syn()

    def _teardown(self):
        with self._lock:
            sources = list(self.sources.items())
            self.sources.clear()
        for _, source in sources:
            source.close()
        if self.ui is not None:
            try:
                self.ui.close()
            except OSError:
                pass
            self.ui = None
            self.devnode = None
        self.ff_effects.clear()

    def _pump(self, timeout):
        """Wait for events on any source or the virtual device and handle them."""
        with self._lock:
            fds = {src.device.fd: key for key, src in self.sources.items()}
        watch = list(fds) + [self._wake_r]
        if self.ui is not None:
            watch.append(self.ui.fd)
        try:
            readable, _, _ = select.select(watch, [], [], timeout)
        except (OSError, ValueError):
            # A source fd was closed under us; the next loop re-evaluates.
            return
        for fd in readable:
            if fd == self._wake_r:
                os.read(self._wake_r, 4096)
            elif self.ui is not None and fd == self.ui.fd:
                self._handle_virtual_events()
            elif fd in fds:
                self._handle_source_events(fds[fd])

    def _handle_source_events(self, key):
        with self._lock:
            source = self.sources.get(key)
        if source is None:
            return
        try:
            events = list(source.device.read())
        except OSError as e:
            if e.errno == errno.ENODEV:
                self._detach(key, 'unplugged')
            else:
                self._detach(key, str(e))
            return
        if self.ui is None:
            return
        wrote = False
        passthrough = self.spec.passthrough == key
        for event in events:
            self.events_in += 1
            if event.type == ecodes.EV_SYN:
                continue
            rules = self._rules.get((key, event.type, event.code))
            if rules:
                for rule in rules:
                    wrote |= self._apply(rule, source, event)
            elif passthrough and event.type not in _NEVER_COPY:
                self._write(event.type, event.code, event.value, key)
                wrote = True
        if wrote:
            self.ui.syn()

    def _write(self, etype, code, value, source_key=None):
        self.ui.write(etype, code, value)
        self.last_values[(etype, code)] = value
        self.events_out += 1
        if etype == ecodes.EV_KEY and source_key is not None:
            held = self._pressed_by.setdefault(source_key, set())
            if value:
                held.add(code)
            else:
                held.discard(code)

    def _normalise(self, rule, source, value):
        info = source.absinfo.get(rule.from_code)
        if info is None or info.max == info.min:
            return 0.0
        norm = (value - info.min) / (info.max - info.min)
        norm = min(1.0, max(0.0, norm))
        invert = rule.invert if rule.invert != 'auto' else self._auto_invert.get((rule.source, rule.from_code), False)
        if invert:
            norm = 1.0 - norm
        if rule.deadzone:
            # Deadzone around the centre of travel, keeping full range at the ends.
            centred = norm * 2 - 1
            if abs(centred) < rule.deadzone:
                centred = 0.0
            else:
                sign = 1 if centred > 0 else -1
                centred = sign * (abs(centred) - rule.deadzone) / (1 - rule.deadzone)
            norm = (centred + 1) / 2
        return norm

    def _apply(self, rule, source, event):
        """Translate one source event through one rule. Returns True if something was written."""
        if rule.from_type == ecodes.EV_ABS and rule.to_type == ecodes.EV_ABS:
            if rule.raw:
                self._write(ecodes.EV_ABS, rule.to_code, event.value)
                return True
            target = self.spec.abs[rule.to_code]
            norm = self._normalise(rule, source, event.value)
            value = int(round(target.min + norm * (target.max - target.min)))
            self._write(ecodes.EV_ABS, rule.to_code, value)
            return True
        if rule.from_type == ecodes.EV_KEY and rule.to_type == ecodes.EV_KEY:
            value = event.value
            if rule.invert is True and value in (0, 1):
                value = 1 - value
            self._write(ecodes.EV_KEY, rule.to_code, value, rule.source)
            return True
        if rule.from_type == ecodes.EV_ABS and rule.to_type == ecodes.EV_KEY:
            norm = self._normalise(rule, source, event.value)
            state_key = (rule.source, rule.to_code)
            was = self._key_state.get(state_key, False)
            # Hysteresis: release a few percent past the press threshold
            margin = 0.05 if was else 0.0
            pressed = ('gt' in rule.when and norm > rule.when['gt'] - margin) or \
                      ('lt' in rule.when and norm < rule.when['lt'] + margin)
            if was == pressed:
                return False
            self._key_state[state_key] = pressed
            self._write(ecodes.EV_KEY, rule.to_code, 1 if pressed else 0, rule.source)
            return True
        if rule.from_type == ecodes.EV_KEY and rule.to_type == ecodes.EV_ABS:
            if event.value == 2:      # key repeat
                return False
            target = self.spec.abs[rule.to_code]
            pressed = bool(event.value) != (rule.invert is True)
            self._write(ecodes.EV_ABS, rule.to_code, target.max if pressed else target.min)
            return True
        return False

    # --- force feedback passthrough -------------------------------------

    def _ff_device(self):
        if self.spec.ff_source is None:
            return None
        with self._lock:
            source = self.sources.get(self.spec.ff_source)
        return source.device if source is not None else None

    def _handle_virtual_events(self):
        try:
            events = list(self.ui.read())
        except OSError:
            return
        for event in events:
            try:
                if event.type == ecodes.EV_UINPUT:
                    if event.code == ecodes.UI_FF_UPLOAD:
                        self._ff_upload(event.value)
                    elif event.code == ecodes.UI_FF_ERASE:
                        self._ff_erase(event.value)
                elif event.type == ecodes.EV_FF:
                    self._ff_play(event.code, event.value)
            except OSError as e:
                # A stale request (the game went away, the kernel timed the
                # request out) must not take the virtual device down.
                logging.warning("proxy %s: force feedback request failed: %s", self.spec.id, e)

    def _ff_upload(self, request_id):
        upload = uinput_ff.begin_upload(self.ui.fd, request_id)
        virtual_id = upload.effect.id
        device = self._ff_device()
        upload.retval = 0
        try:
            if device is None:
                # Accept the effect so the game keeps working; it plays once the wheel is back.
                self._ff_cache[virtual_id] = bytes(memoryview(upload.effect).tobytes())
            else:
                data = bytes(memoryview(upload.effect).tobytes())
                effect = ff.Effect.from_buffer_copy(data)
                effect.id = self.ff_effects.get(virtual_id, -1)
                real_id = uinput_ff.upload_effect(device.fd, effect)
                self.ff_effects[virtual_id] = real_id
                self._ff_cache[virtual_id] = data
        except OSError as e:
            logging.warning("proxy %s: effect upload failed: %s", self.spec.id, e)
            upload.retval = -(e.errno or errno.EIO)
        finally:
            uinput_ff.end_upload(self.ui.fd, upload)

    def _ff_erase(self, request_id):
        erase = uinput_ff.begin_erase(self.ui.fd, request_id)
        virtual_id = erase.effect_id
        erase.retval = 0
        try:
            self._ff_cache.pop(virtual_id, None)
            real_id = self.ff_effects.pop(virtual_id, None)
            device = self._ff_device()
            if real_id is not None and device is not None:
                uinput_ff.erase_effect(device.fd, real_id)
        except OSError as e:
            logging.warning("proxy %s: effect erase failed: %s", self.spec.id, e)
            erase.retval = -(e.errno or errno.EIO)
        finally:
            uinput_ff.end_erase(self.ui.fd, erase)

    def _ff_play(self, code, value):
        device = self._ff_device()
        if device is None:
            return
        if code in (ecodes.FF_GAIN, ecodes.FF_AUTOCENTER):
            self._ff_state[code] = value
            real = code
        else:
            real = self.ff_effects.get(code)
            if real is None:
                return
        try:
            device.write(ecodes.EV_FF, real, value)
        except OSError as e:
            logging.warning("proxy %s: effect play failed: %s", self.spec.id, e)

    def _restore_effects(self, source):
        """Re-upload effects a game had loaded before the wheel went away,
        and the last gain / autocenter it set."""
        for code, value in self._ff_state.items():
            try:
                source.device.write(ecodes.EV_FF, code, value)
            except OSError as e:
                logging.warning("proxy %s: could not restore %s: %s", self.spec.id, code_name(ecodes.EV_FF, code), e)
        for virtual_id, data in list(self._ff_cache.items()):
            effect = ff.Effect.from_buffer_copy(data)
            effect.id = -1
            try:
                self.ff_effects[virtual_id] = uinput_ff.upload_effect(source.device.fd, effect)
            except OSError as e:
                logging.warning("proxy %s: could not restore effect %d: %s", self.spec.id, virtual_id, e)
        if self._ff_cache:
            logging.info("proxy %s: restored %d effect(s) on %s", self.spec.id, len(self.ff_effects), source.path)

    def describe(self):
        """Human readable summary for CLI output."""
        lines = ["{} [{}] {}".format(self.spec.id, self.state.value, self.spec.name)]
        if self.devnode:
            lines.append("  virtual: {} ({})".format(self.devnode, self.spec.identity.name))
        for key, spec in self.spec.sources.items():
            src = self.sources.get(key)
            where = "{} ({})".format(src.device.name, src.path) if src else "missing"
            lines.append("  source {}: {}".format(key, where))
        for m in self.spec.mappings:
            lines.append("  map {}.{} -> {}{}".format(m.source, code_name(m.from_type, m.from_code),
                                                     code_name(m.to_type, m.to_code),
                                                     ' (inverted)' if m.invert is True else (' (auto invert)' if m.invert == 'auto' else '')))
        if self.spec.ff_source:
            lines.append("  force feedback -> {} ({} effect(s) loaded)".format(self.spec.ff_source, len(self.ff_effects)))
        return "\n".join(lines)
