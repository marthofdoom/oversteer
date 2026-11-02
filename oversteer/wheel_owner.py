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
  With inhibit=False an exclusive acquire never asks anyone to let go: it
  is granted only on a free wheel (the effect demos: they must not cut
  Countersteer FFB off mid-drive).
- release(wheel, owner): let go (a no-op if `owner` does not hold it).
- hold_for_demo(wheel, seconds) -> bool: the FFB tab's and the web page's
  "try" effects: exclusive for the demo's length, never inhibiting;
  False if anyone holds the wheel (the caller says who: current()).
- current(wheel) -> (owner, exclusive) or None.

Exclusive owners: gui.start_test (the wheel test, released when the test
ends or is aborted) and, once it merges, the calibration (branch
ffb-calib): acquire(dev_path, 'calibration', exclusive=True) before its
first write, release() after restoring the defaults."""
import threading

_cond = threading.Condition()
_held = {}                  # wheel -> (owner, exclusive, on_inhibit)


DEMO = 'demo'
DEMO_SECONDS = 2.3          # Device.play_demo's 2 s and the 0.2 s it waits after
_demo_generation = {}       # wheel -> the newest demo's number: only its timer lets go


def acquire(wheel, owner, exclusive=False, on_inhibit=None, timeout=0.5, inhibit=True):
    with _cond:
        held = _held.get(wheel)
        if held is None or held[0] == owner:
            _held[wheel] = (owner, exclusive, on_inhibit)
            return True
        if not exclusive or not inhibit or held[1] or held[2] is None:
            return False
        holder, ask_to_let_go = held[0], held[2]
    ask_to_let_go()                             # outside the lock: the holder may release() from here
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


def hold_for_demo(wheel, seconds):
    if wheel is None:
        return True
    with _cond:
        held = _held.get(wheel)
        if held is not None and held[0] == DEMO:
            return False                        # one at a time: a second would stack on the first's force
    if not acquire(wheel, DEMO, exclusive=True, inhibit=False):
        return False
    with _cond:
        generation = _demo_generation[wheel] = _demo_generation.get(wheel, 0) + 1

    def done():
        with _cond:
            if _demo_generation.get(wheel) != generation:
                return                          # a newer demo holds it now
        release(wheel, DEMO)
    timer = threading.Timer(seconds, done)
    timer.daemon = True
    timer.start()
    return True


def demo_refusal(wheel):
    """Why a demo may not play now (English, for the caller to translate), or None."""
    held = current(wheel)
    if held is None:
        return None
    if held[0] == DEMO:
        return "An effect is still playing"
    if held[0] == 'countersteer-ffb':
        return "Countersteer FFB is playing: switch it off to try the effects"
    return "The wheel is in use ({})".format(held[0])
