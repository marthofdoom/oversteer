"""One in-process owner per wheel of the driver's replace-mode attributes
(ext_force, game_ffb_level; later the wheel correction a calibration
writes): whoever plays a force of its own on the wheel, or needs the
game's and the user's forces exactly as they are, holds the wheel first.

API (a wheel is its sysfs directory, Device.dev_path):

- acquire(wheel, owner, exclusive=False, on_inhibit=None, timeout=0.5)
  -> bool. `owner` is any hashable name ('countersteer-ffb', 'wheel-test',
  'calibration'). A free wheel, or one the same owner already holds, is
  granted at once. A shared (exclusive=False) owner should pass
  `on_inhibit`, a callable that asks it to let go: it is called (from the
  acquiring thread, without the module lock held) when an exclusive
  owner wants the wheel, and the shared owner must then stop writing,
  restore game_ffb_level 100 and call release() within `timeout`
  (Countersteer FFB does it within one 10 ms poll). acquire(exclusive=True)
  waits that long for it and is granted once it has; it is refused
  (False) if the wheel is held exclusively by someone else, if the holder
  has no on_inhibit, or if it did not let go in time. A shared acquire is
  refused while anyone else holds the wheel: Countersteer FFB never
  activates then.
- release(wheel, owner): let go (a no-op if `owner` does not hold it).
- current(wheel) -> (owner, exclusive) or None.

Exclusive owners: gui.start_test (the wheel test, released when the test
ends or is aborted) and, once it merges, the calibration (branch
ffb-calib): acquire(dev_path, 'calibration', exclusive=True) before its
first write, release() after restoring the defaults."""
import threading

_cond = threading.Condition()
_held = {}                  # wheel -> (owner, exclusive, on_inhibit)


def acquire(wheel, owner, exclusive=False, on_inhibit=None, timeout=0.5):
    with _cond:
        held = _held.get(wheel)
        if held is None or held[0] == owner:
            _held[wheel] = (owner, exclusive, on_inhibit)
            return True
        if not exclusive or held[1] or held[2] is None:
            return False
        holder, inhibit = held[0], held[2]
    inhibit()                                   # outside the lock: the holder may release() from here
    with _cond:
        _cond.wait_for(lambda: _held.get(wheel, (None,))[0] != holder, timeout)
        if wheel in _held and _held[wheel][0] == holder:
            return False
        if wheel in _held:                      # someone else got in first
            return False
        _held[wheel] = (owner, exclusive, on_inhibit)
        return True


def release(wheel, owner):
    with _cond:
        held = _held.get(wheel)
        if held is not None and held[0] == owner:
            del _held[wheel]
            _cond.notify_all()


def current(wheel):
    with _cond:
        held = _held.get(wheel)
        return None if held is None else held[:2]
