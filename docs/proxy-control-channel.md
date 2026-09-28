# Proxy control channel, auto-fold and daemon-managed permissions: design

Branch `launch-limiter`, written against e45f9c8. ClickUp 86e3cyqme (the
proxy pipeline rework); the hotkey part is 86e3dme66. Nothing here is built
yet. This document is the plan and the recovery point: the checklist in §14
says what is done, and each step's notes say what was deferred and why.

Questions only the owner can answer are marked **[Q1]**…**[Q10]** where they
come up and collected in §15. Each has a recommended answer so the build can
start on it; steps that depend on an answer say so.

## 1. Where we are

- `oversteer-proxy.service` runs `python3 -m oversteer.proxy.daemon` as root
  from a root-owned copy in `/usr/local/lib/oversteer-proxy`. It loads specs
  from the built-in `profiles/` and `/etc/oversteer/proxies`, runs each
  enabled one as a `ProxyDevice` thread (grab sources, create the uinput
  device, map events, pass force feedback through), follows hot-plug through
  a pyudev monitor, and writes `/run/oversteer/proxies.json` (0644) at least
  once a minute.
- Everything privileged goes through `install.install()`: the GUI writes
  candidate specs under `~/.config/oversteer/proxies/.candidate-*`, runs
  `pkexec … -m oversteer.proxy.install install --specs <dir>` (via
  `flatpak-spawn --host` in the Flatpak), which copies the daemon, writes the
  specs to `/etc`, writes two udev rule files (`71-…` drops the `uaccess`
  tag, `99-zz-…` sets `MODE="0600", GROUP="root"` for every hidden
  source's VID:PID), restarts the service and strips ACLs from plugged
  nodes.
- The handbrake Invert box (`gui.set_handbrake_invert`) edits the user spec
  and runs that whole install again: a password prompt and a service
  restart, which drops the virtual device a running game holds.
- A handbrake plugged in after the spec was built is not in the spec: the
  user has to tick it on the Devices tab and reinstall (prompt again).
- `SourceSpec.matches()` refuses other proxies' devices only by
  `phys.startswith('py-evdev-uinput')`, which any uinput client can avoid.
  `Equipment._classify()` calls a device virtual from `bustype`, which any
  uinput client sets. Neither is a trust boundary today; §6 makes one.

## 2. What it has to do

| # | Requirement | Where |
|---|---|---|
| 1 | A runtime control channel: a bounded, validated command set (axis invert, spec on/off, add/remove a source of a known kind) the desktop user can use without a prompt | §3, §4 |
| 1a | A root process never acts on user-supplied identities, match rules or paths: commands carry booleans, enums and names the daemon already has | §4.3, §8 |
| 2 | Auto-fold: specs describe sources by kind as well as by id, so a handbrake, shifter or button box plugged in later is grabbed and mapped without a rebuild | §6 |
| 3 | The daemon makes a source root-only when it grabs it and restores it on release; udev rules cover only boot ordering | §7 |
| 4 | The handbrake Invert box is instant, like the pedal boxes; plugging a handbrake in just works | §10 |
| 5 | Later: hotkeys that reach the daemon (swallow bound wheel buttons from the game) over the same channel | §12 |

Non-goals: letting the channel create specs, identities or match rules
(that stays a pkexec install); remote control; running the daemon without
root; changing the force feedback path.

## 3. The channel

### 3.1 Unix socket, not a watched file

`/run/oversteer/control.sock`, `AF_UNIX`, `SOCK_SEQPACKET`, owned by root,
mode 0666, in the root-owned 0755 `RuntimeDirectory`.

Why not a watched file (`/run/oversteer/requests/…`):

- a file carries no trustworthy author: the daemon would have to trust the
  file's owner, which means a user-writable directory in `/run/oversteer`,
  which means symlink, hard-link and rename races against a root reader;
- there is no reply, so the GUI cannot tell "applied" from "refused" from
  "the daemon is too old", and every refusal becomes a log line nobody reads;
- rate limiting a file is polling plus hoping; a socket has connections to
  count and close.

Why `SOCK_SEQPACKET`: one request per message, so there is no framing to get
wrong and an oversized request is detectable (`MSG_TRUNC`), and `SO_PEERCRED`
works as on a stream socket. The hotkey event stream (§12) fits the same
socket: the daemon sends messages on a connection the client keeps open.

Mode 0666 is deliberate: file permissions are not the check (§3.2), and
0666 is what lets the Flatpak's uid connect without a group being set up
first. Anyone can connect; only an authorised peer gets anything but
`denied`.

### 3.2 Who may use it

On `accept()` the daemon reads `SO_PEERCRED` (`struct ucred`: pid, uid, gid,
kernel-filled at `connect()`, translated into the daemon's namespace, so a
Flatpak client shows its real host uid). The connection is authorised if:

- `uid == 0`, or
- `uid` is in the policy file `/etc/oversteer/control.json`
  (`{"users": [1000], "groups": []}`, root-owned 0644), written by the
  installer from `PKEXEC_UID` (or `SUDO_UID`): the user who authenticated the
  install is the one allowed to re-parameterise it. Running the installer
  again as another user adds that user.

**[Q1]** Should the active seat's user (logind's `seat0` active uid, the same
person `uaccess` gives the wheel to) also be allowed without being listed?
Recommendation: no. The install-time user list is explicit, needs no parsing
of logind's non-API `/run/systemd/seats/seat0` and no D-Bus client in the
daemon, and a second user on the machine can get in with one prompt. A
`groups` list is supported by the file format (checked with the peer's
primary gid and `/proc/<pid>/status` `Groups:` read at accept time) but left
empty by the installer unless the owner wants an `oversteer` group.

Authorisation is decided once per connection at accept time. The peer's
pid is logged, never trusted for anything.

### 3.3 How the Flatpak reaches it

The manifest already has `--filesystem=/run/oversteer:ro`. A read-only bind
mount does not stop `connect()` on a socket inside it: `connect()` needs
write permission on the socket inode, and the kernel refuses writes on a
read-only mount only for regular files, directories and symlinks (the same
reason mounting `docker.sock:ro` is known not to protect Docker). So the
existing permission is enough and no new finish-arg is needed. The Flatpak
cannot write anything else under `/run/oversteer`, which stays as it is.

Two things to make this reliable:

- **`RuntimeDirectoryPreserve=yes`** in the unit. Today `/run/oversteer` is
  removed when the service stops and recreated when it starts. A Flatpak
  started before that keeps its bind mount on the old, deleted directory: it
  would see neither the new socket nor the new status file until it
  restarts. Preserving the directory keeps the inode, so the bind mount
  follows the new socket. (This also fixes a quiet existing bug with the
  status file after a service restart.)
- If `/run/oversteer` does not exist when the Flatpak starts (service never
  installed), the `--filesystem` entry is silently skipped. The GUI then
  sees no socket and uses the pkexec path, which is what it must do anyway
  with no service. After the first install the GUI tells the user once that
  a restart of Oversteer makes later changes instant (only in the Flatpak,
  and only when `/run/oversteer` is empty inside the sandbox while the
  host's `/etc/oversteer/proxies`, visible through `host-etc`, has enabled
  specs: the status file is behind the same missing mount, so it cannot be
  the evidence).

Verification on the rig is step 5 of the build plan: `connect()` from inside
the Flatpak through the `:ro` mount. Fallback if it fails for a reason not
foreseen here: `flatpak-spawn --host /usr/bin/python3 -m
oversteer.proxy.control_client <json>` with `PYTHONPATH` set to the
root-owned daemon copy, which the Flatpak already has permission to spawn
(`--talk-name=org.freedesktop.Flatpak`). It costs a process per command, so
it is only a fallback. `--filesystem=/run/oversteer:rw` is not needed and not
wanted: it would let the sandbox replace the status file's directory entries
if the directory mode were ever loosened.

### 3.4 Limits

| Limit | Value | Why |
|---|---|---|
| Request size | 1024 bytes, `MSG_TRUNC` = `too-large` and close | the largest valid request is ~200 bytes |
| Reply size | 16 KiB | `hello`/`get` with every candidate listed |
| Connections | 8 in total, 2 per uid; the oldest idle one is closed first | a flood of connects cannot starve the GUI |
| Idle timeout | 30 s without a request (subscriptions excepted, §12) | |
| Unauthorised peer | one `denied` reply, then close | |
| Request rate | token bucket per uid: 10/s, burst 20 | a slider or a stuck key cannot hammer the daemon |
| Device-changing ops (`set_enabled`, `fold`, `unfold`) | 1 per 2 s per spec; `busy` otherwise | each one grabs, releases and re-triggers udev |
| State writes | coalesced, at most 1 per second | §5 |

The server is one thread with a `selectors` loop over the listening socket
and the connections, non-blocking throughout. It never touches a device
itself: it validates, then hands a command to the manager, which hands it to
the proxy thread (§4.4). The event path (source read → uinput write) takes no
new lock.

## 4. Protocol

### 4.1 Messages

Every message is one UTF-8 JSON object. Requests:

```json
{"v": 1, "id": 7, "op": "set_invert", "spec": "combined-wheel",
 "source": "handbrake1", "axis": "ABS_THROTTLE", "value": true}
```

Replies:

```json
{"v": 1, "id": 7, "ok": true,
 "result": {"inverted_axes": {"handbrake1:6": true}}}
{"v": 1, "id": 7, "ok": false, "error": "unknown-axis",
 "message": "handbrake1 has no mapped axis ABS_RX"}
```

`id` is the client's (integer 0..2^31-1), echoed back so a client can tell
replies from pushed events (§12). `message` is for logs and dialogs; clients
act on `error` only.

### 4.2 Verbs

| op | Arguments (all required unless marked) | Effect |
|---|---|---|
| `hello` | – | `{protocol: 1, daemon: "<version>", ops: [...], uid: <peer uid>}`; no rate cost |
| `get` | `spec` (optional) | runtime state: per spec `enabled`, `inverted_axes`, `sources`, `runtime` overlay, `candidates` (§6.4) |
| `set_invert` | `spec`, `source`, `axis`, `value`: `true`/`false`/`"auto"` | direction of one mapped axis, applied live |
| `set_enabled` | `spec`, `value`: bool | start or stop an installed spec's proxy, grabbing/releasing and hiding/restoring its sources |
| `fold` | `spec`, `handle` | add a daemon-listed candidate device to the spec's slot for its kind (§6) |
| `unfold` | `spec`, `source` | remove a folded source; the device is remembered as excluded from auto-fold for that spec |
| `include` | `spec`, `usb_id` | forget an exclusion (so auto-fold may take the device again); `usb_id` must be in that spec's excluded list |
| `reset` | `spec` | drop every runtime override for the spec: back to what `/etc` says |
| `watch_keys`, `swallow` | §12 | reserved for 86e3dme66, answered `unknown-op` until built |

### 4.3 Validation

Done in `control.validate()` before anything reaches the manager, against a
snapshot of the manager's state taken under its lock. In order:

1. Size ≤ 1024, UTF-8, `json.loads` inside `try` (deep nesting raises
   `RecursionError`, caught like any other parse error), top level an object.
2. `v == 1`, else `unsupported-version`. `op` in the table, else
   `unknown-op`.
3. The object's keys are exactly `{"v", "id", "op"}` plus that verb's
   arguments: an unknown key is `bad-request`, not ignored, so a newer
   client cannot believe an older daemon applied something it dropped.
4. Types are exact: `value` for `set_enabled` is `True` or `False`
   (`isinstance(x, bool)`; `1` is refused); `set_invert` also takes the
   literal string `"auto"`; `id` is an `int` that is not a `bool`.
5. `spec` matches `spec.ID_RE`, ≤ 64 chars, and is a spec the daemon has
   loaded from `/etc/oversteer/proxies` (**[Q6]**: built-in specs only if
   also installed there) → else `unknown-spec`.
6. `source` is a key of that spec's effective sources (installed + folded)
   → else `unknown-source`. For `unfold` it must be a folded source or a
   declared `"required": false` source; a required one is `not-allowed`.
7. `axis` is an evdev ABS name (`"ABS_THROTTLE"`) or an integer 0..`ABS_MAX`,
   resolved with `spec.code_from_name(axis, EV_ABS)`, and there must be a
   mapping in the effective spec with that `source` and `from_code` whose
   `from_type` is `EV_ABS` → else `unknown-axis`. The daemon never learns
   an axis from the request; it looks one up.
8. `handle` matches `^c-[0-9a-f]{12}$` and is in the current candidate table
   (§6.4) → else `stale-handle` (the device went away or was replugged).
9. `usb_id` matches `^[0-9a-f]{4}:[0-9a-f]{4}$` and is in the spec's excluded
   list → else `unknown-device`.

Error codes: `bad-request`, `too-large`, `unsupported-version`,
`unknown-op`, `denied`, `rate-limited`, `busy`, `unknown-spec`,
`unknown-source`, `unknown-axis`, `unknown-device`, `stale-handle`,
`not-allowed`, `no-room`, `not-running`, `internal`.

Nothing from a request is ever used as a path, a regular expression, a
format string or a udev rule. Values that reach a log line are the
validated ones; a refused request is logged as its `op` (if it was one of the
table's) and error code only, with the peer uid and pid.

### 4.4 Applying a command

The manager gets typed calls, never the request:

```python
class ProxyManager:
    def set_invert(self, spec_id: str, source: str, code: int, value) -> dict
    def set_enabled(self, spec_id: str, value: bool) -> dict
    def fold(self, spec_id: str, handle: str) -> dict
    def unfold(self, spec_id: str, source: str) -> dict
    def include(self, spec_id: str, usb_id: str) -> dict
    def reset(self, spec_id: str) -> dict
    def runtime_state(self, spec_id: str = None) -> dict
```

Each updates the overlay (§5), then acts on the running `ProxyDevice`.
`ProxyDevice` gets a command queue drained by its own thread, woken through
the existing wake pipe, so mapping state is only ever changed on the thread
that reads it:

```python
class ProxyDevice:
    def submit(self, fn) -> concurrent.futures.Future   # fn(self) runs on the proxy thread
    def set_invert(self, source: str, code: int, value) -> Future
    def add_source(self, source: SourceSpec, mappings: list) -> Future
    def remove_source(self, key: str) -> Future
```

`_pump()` runs queued functions after `select()` returns on the wake fd. The
manager waits on the future with a 1 s timeout (`busy` if it expires; the
change still lands).

`set_invert` on the proxy thread: replace the matching `Mapping` objects in
`_rules` with copies carrying the new `invert` (the installed `ProxySpec` is
never mutated; `apply_overlay()` builds the effective one), drop the
`_auto_invert` entry when leaving `"auto"`, re-evaluate it from the current
position when entering `"auto"`, then write the axis's current value through
the new rule and `syn()`, so the game sees the new direction at once rather
than on the next movement. The current value must come from
`source.device.absinfo(code)` (a fresh `EVIOCGABS`), not
`source.absinfo`, which is the snapshot from open time.

`set_enabled(False)` stops the proxy (release, §7.3). `set_enabled(True)`
starts it through the same ordering `start()` uses (wheel first). One case
the ordering cannot fix by itself: enabling the combined wheel while one of
its companions (`combined-wheel-*`) is already running would put the
companion ahead of the wheel in enumeration, which games that take the first
device (FH6) mind. **[Q9]** Recommended: the manager stops the running
companions, starts the wheel, waits for its device node, and starts the
companions again; the reply lists them as `"restarted": [...]`. Enabling a
companion alone needs nothing special. The channel can only enable specs as
installed, so it can never start a device with capabilities its sources
lack.

## 5. Persistence

Runtime changes survive a daemon restart and a reboot **[Q2]**: they are
settings the user made, like the pedal Invert boxes, and a game started
before the GUI must see them.

- File: `/var/lib/oversteer/runtime.json`, from `StateDirectory=oversteer`
  (root, 0700). Written by the daemon only: `mkstemp` in the same directory,
  `fsync`, `os.replace`; read with `O_NOFOLLOW`, `fstat` must show a regular
  file owned by root, size ≤ 64 KiB, else the file is ignored and logged.
- Contents, per spec id:

```json
{"v": 1,
 "specs": {
   "combined-wheel": {
     "base": "sha256:…",
     "enabled": null,
     "invert": {"handbrake1:6": true},
     "folds": [{"key": "handbrake1", "kind": "handbrake", "usb_id": "8086:0a01",
                "name": "ANNX Handbrake", "slot": "handbrake"}],
     "excluded": ["1d6b:0104"]
   }}}
```

- `base` is the SHA-256 of the installed spec's canonical JSON
  (`json.dumps(spec.to_dict(), sort_keys=True)`). When a pkexec install
  changes the spec, `base` no longer matches and `enabled`, `invert` and
  `folds` for that spec are dropped: the install carries the user's current
  choices (the GUI mirrors every runtime change into the user spec, §10), so
  the overlay has nothing left to add. `excluded` is kept while the spec id
  exists. No coupling between installer and daemon is needed.
- Bounds, enforced on write and re-checked on load: specs that are installed
  only (≤ 16 entries); per spec ≤ 32 `invert` entries (only keys the
  effective spec has), ≤ 8 folds (never more than the spec's slots allow),
  ≤ 32 `excluded`. Every entry is re-validated against the installed spec on
  load exactly as a command would be; what fails is dropped and logged, never
  "repaired".
- A fold persists what the daemon read from sysfs (VID:PID, name) and the
  mappings are re-planned from the slot on load (§6.3), not stored: the
  stored record only says "this kind of device goes in this slot". It is
  data the daemon produced from kernel attributes, not user input.

```python
# oversteer/proxy/runtime.py
STATE_FILE = '/var/lib/oversteer/runtime.json'

@dataclass
class SpecOverlay:
    base: str
    enabled: Optional[bool] = None
    invert: dict = field(default_factory=dict)      # "source:code" -> True/False/'auto'
    folds: list = field(default_factory=list)       # [Fold]
    excluded: set = field(default_factory=set)      # {"vvvv:pppp"}

def spec_hash(spec: ProxySpec) -> str
def apply_overlay(spec: ProxySpec, overlay: SpecOverlay, planner) -> ProxySpec   # pure; returns a new spec
def validate_overlay(spec: ProxySpec, overlay: SpecOverlay) -> tuple[SpecOverlay, list[str]]

class OverlayStore:
    def __init__(self, path=STATE_FILE, clock=time.monotonic)
    def load(self, specs: dict) -> dict              # spec id -> SpecOverlay, pruned
    def update(self, spec_id: str, fn) -> SpecOverlay  # fn(overlay) mutates a copy; saved, coalesced
    def flush(self)                                  # on SIGTERM
```

## 6. Auto-fold: sources by kind

### 6.1 Spec additions

A spec may declare **slots**: kinds of equipment it takes when they turn up,
and where their controls go. The capabilities a slot needs are declared in
the spec up front, because a uinput device's capabilities cannot change
after it is created.

```json
"slots": {
  "handbrake": {"max": 1, "auto": true, "axes": ["ABS_THROTTLE"], "invert": "auto"},
  "shifter":   {"max": 1, "auto": true},
  "buttonbox": {"max": 1, "auto": false, "keys": ["BTN_TRIGGER_HAPPY20", "BTN_TRIGGER_HAPPY21"]}
}
```

- Slot names are the fixed kinds from `equipment.py`: `handbrake`,
  `shifter`, `pedals`, `buttonbox`. Anything else is a `SpecError`.
- `axes`/`keys` must be codes the spec already advertises in `capabilities`
  (validated in `ProxySpec._from_dict`) and not targets of another
  mapping. `shifter` needs neither when the wheel has G29 gear codes
  (`G29_GEAR_CODES`); otherwise it lists `keys`.
- `auto: true` folds a matching device as soon as it appears; `false` lists
  it as a candidate the user folds with one click (no prompt).
- `SourceSpec` gains an optional `"kind"`, written by the builder for every
  non-wheel source, so the GUI stops inferring kind from the key prefix.
- Specs without `slots` behave exactly as today.

**[Q3]** Reserving an axis changes the device's layout even with no handbrake
fitted. With the generic identity (FH6) that is harmless. With the G29
identity it is not: FH6 stops recognising a G29 with one axis more (the
reason `build_combined_specs` has a strict mode). Recommendation: the builder
reserves a handbrake slot on the combined device only for the generic
identity; with the wheel's own identity a handbrake slot is a separate
**companion spec** (`combined-wheel-handbrake`, Fanatec ClubSport Handbrake
identity, as `build_combined_specs` already makes) with a `handbrake` slot
and no fixed source, installed disabled-until-filled: it creates its virtual
device only when a handbrake is folded. A shifter slot needs no reservation
on a Logitech wheel (the gear codes are the wheel's own buttons).

### 6.2 What counts as equipment (the trust rule)

A folded source is grabbed by root and its events become a game's input, so
the daemon must not take a device an unprivileged process made. Rule, in
`equipment.hardware_backed(udevice) -> bool`:

- the input device's sysfs path is not under `/sys/devices/virtual/`
  (where uinput and uhid devices live), and
- it has a `usb`/`usb_device` ancestor (**[Q7]** or a Bluetooth `hci`
  ancestor for classic Bluetooth HID; recommended no until someone has one).

Decided from the sysfs topology, which only the kernel writes, never from
`bustype`, `phys`, name or VID:PID, which a uinput or uhid client sets
freely. A user who can open `/dev/uinput` (common on gaming desktops: Steam's
udev rules give it `uaccess`) can make a device claiming to be an ANNX
handbrake; it lands under `/sys/devices/virtual/input/` and is never a
candidate. Creating a device under a USB parent needs root (USB/IP,
`dummy_hcd`, gadget configfs) or physical access with hostile hardware,
which is out of scope.

The same check replaces the `py-evdev-uinput` test in
`SourceSpec.matches()` for every source the daemon attaches, installed or
folded. A spec may still opt out per source with `"virtual": true` (the
proxy-of-proxy case the `phys` rule existed for); such a source can only
come from a pkexec install. The daemon's own virtual devices are virtual,
so a combined device can never fold its own companion.

`ProxyDevice` and `ProxyManager` take the check as a parameter
(`trust=hardware_backed`) so the existing uinput-based tests pass
`trust=lambda d: True`.

Also excluded, as `list_equipment()` already does: udev's
`ID_INPUT_KEYBOARD`/`ID_INPUT_KEY` without axes, mice, touchpads, tablets,
switches; the wheel kind (a second wheel is never folded); gamepads.

### 6.3 Recognising the kind

`Equipment(udevice)` already classifies from sysfs capability bitmaps
without opening the node. Two levels:

- **Known**: VID:PID in a table (`KNOWN_SHIFTERS` today; add
  `KNOWN_HANDBRAKES = {'8086:0a01': 'ANNX'}`, `KNOWN_BUTTONBOXES`,
  `KNOWN_PEDALS`). Known devices fold automatically into an `auto` slot.
- **Guessed**: the `_classify()` heuristics (one axis ≤ 1 button =
  handbrake, …). **[Q4]** Recommendation: a guessed device is listed as a
  candidate but never folded automatically, even into an `auto` slot; the
  user folds it with a click. A guess is right for the handbrakes we have
  seen, but a wrong guess grabs a device away from the desktop (a USB volume
  knob with one axis) without anyone asking. One click, no password, is
  the cost.

The mappings for a folded device come from the same code that builds a spec
today, split out of `build_combined_spec()` into pure functions so the
builder and the daemon cannot disagree:

```python
# oversteer/proxy/equipment.py
def hardware_backed(udevice) -> bool
def shifter_mappings(dev, source_key, gear_codes, reverse_code, free_keys) -> (list[dict], list[str])
def button_mappings(dev, source_key, free_keys) -> (list[dict], list[str])
def axis_mappings(dev, source_key, targets, invert='auto') -> (list[dict], list[str])

class FoldError(Exception): ...   # .code in ('no-room', 'wrong-kind', 'not-allowed')

def plan_fold(spec: ProxySpec, slot: str, dev: Equipment, key: str) -> tuple[SourceSpec, list[Mapping]]
```

`plan_fold` builds the source's match from the device's own sysfs values
(`vendor`, `product`, `name` as `^re.escape(name)$`, plus `"kind"` and
`required: false`), uses only the slot's reserved codes (plus the wheel's
gear codes for a shifter), and raises `no-room` rather than taking a code the
slot did not reserve. The resulting `SourceSpec`/`Mapping`s go through the
same `from_dict` validation as an installed spec. Source keys are
`<kind><n>` (`handbrake1`), as the builder names them now.

### 6.4 Candidates and handles

The manager keeps a table of equipment it has seen on `add`/`remove` events
and at start-up:

```json
"candidates": [
  {"handle": "c-5f3a9b0c1d2e", "kind": "handbrake", "known": true,
   "usb_id": "8086:0a01", "name": "ANNX Handbrake",
   "spec": "combined-wheel", "slot": "handbrake",
   "state": "folded"}
]
```

`state` is one of `folded`, `available` (a slot has room; `fold` works),
`excluded`, `no-room` (a slot of that kind exists but is full), `no-slot`
(the spec has no slot for this kind: a rebuild is needed, the pkexec path).
The handle is a random token (`secrets.token_hex(6)`) bound to the device's
devpath and udev `USEC_INITIALIZED`; unplugging or replugging makes a new
handle, so a `fold` sent for a device that has meanwhile been replaced by
another in the same port is `stale-handle`, never a fold of the new one.
Candidates are published in the status file and in `get`.

### 6.5 Flow

1. udev `add` for an `input` event node → `Equipment(udevice)`; skip unless
   `hardware_backed` and a foldable kind.
2. For each enabled spec with a slot of that kind: if the device's
   `usb_id` is excluded there, list it as `excluded`; if it is already a
   declared (installed) source of the spec, do nothing (the running proxy
   picks it up as today); if the slot is `auto`, the device is known, and
   there is room, fold it; otherwise list it.
3. Fold: `plan_fold` → overlay `folds` += record → `ProxyDevice.add_source()`
   on the proxy thread: open, verify (§7.2), grab, hide, attach, sync axes.
   For a companion spec with no virtual device yet, the device is created
   now.
4. `remove`: the proxy detaches as today (`ENODEV`), the fold stays in the
   overlay so the device is taken again when it comes back; the candidate
   row goes away.
5. `unfold` (user): remove the source and its mappings (keys it held are
   released, its axes return to min, exactly `_release_keys_from`), release
   the device (§7.3), add its `usb_id` to `excluded`.

## 7. Daemon-managed permissions

### 7.1 What changes

The daemon, not only udev rules, makes a source's nodes root-only: at grab,
for every source (installed or folded), and puts them back at release. udev
rules stay for one job, boot ordering: a device present at boot must not be
readable by a game started before the daemon.

### 7.2 Hiding on grab

The nodes of a source are its `eventN`, the `jsN` under the same `inputN`,
and every `hidraw` node under the same USB device (today's rules hide hidraw
by VID:PID for the same reason: some games and Proton read HID directly).
All found through pyudev from the source's sysfs device, never from names in
a request.

For each node, in `permissions.NodeGuard.hide()`:

1. `fd = os.open(devnode, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC)`
   (the event node is the fd the proxy already opened for the grab).
2. `os.fstat(fd)`: `S_ISCHR` and `st_rdev == udevice.device_number`, else
   `race` and abort the attach. Once the fd is open it stays bound to that
   device; everything after this works on the fd, so a node reused by
   another device after an unplug cannot be touched by mistake.
3. For the event node, `EVIOCGID` / `EVIOCGNAME` on the fd must match the
   sysfs values the candidate was built from.
4. `EVIOCGRAB` (event node only), then `os.fchown(fd, 0, 0)`,
   `os.fchmod(fd, 0o600)`, `os.removexattr(fd, 'system.posix_acl_access')`
   (`ENODATA` ignored). Grab first, so the window in which a game that
   already has the node open sees events is as short as it can be.
5. Record `{devpath, devnode, rdev, usec_initialized, how: "daemon"}` in the
   journal `/run/oversteer/hidden.json` (root, 0600; `RuntimeDirectoryPreserve`
   keeps it across a restart, a reboot clears it, which is right).

Known limits: permissions only affect new `open()`s. An fd a game opened
before the grab gets no evdev or joydev events (the grab routes them all to
the daemon) but keeps reading a `hidraw` node it already had open.
**[Q8]** Accept that and document it (it only affects a device plugged in
mid-game, into a game that reads its hidraw), or go further, e.g. revoking
open hidraw fds, which Linux has no clean way to do short of unbinding the
driver? Recommendation: accept and document.

### 7.3 Restoring on release

Release happens on `unfold`, `set_enabled(False)`, spec removal and daemon
stop. The daemon does not replay saved modes and ACLs: it asks udev to
recompute them, by writing `change` to `/sys/<devpath>/uevent` for each
recorded node's device. udev re-runs every rule, including logind's
`uaccess` builtin on `change`, so the node ends up exactly as it would be
freshly plugged, including an ACL for whoever is on the seat *now*.

For a device the static hide rules also cover, that re-trigger would hide it
again. The rules therefore become conditional on a release flag:

```
# 99-zz-oversteer-proxy-hide.rules
SUBSYSTEM=="input", KERNEL=="event*|js*", ATTRS{idVendor}=="8086", ATTRS{idProduct}=="0a01", \
  TEST!="/run/oversteer/released/8086:0a01", MODE="0600", GROUP="root"
```

(and the same `TEST!=` on the `71-…` `TAG-="uaccess"` line and the hidraw
line). At boot `/run/oversteer/released/` is empty, so everything is hidden:
fail-closed, which is the boot-ordering job. The daemon creates
`released/<vid>:<pid>` (root, empty file, in its root-owned directory) before
re-triggering a device it lets go, and removes it before hiding one. A user
cannot create a flag: the directory is root's.

```python
# oversteer/proxy/permissions.py
HIDDEN_JOURNAL = '/run/oversteer/hidden.json'
RELEASED_DIR = '/run/oversteer/released'

@dataclass
class HiddenNode:
    devpath: str
    devnode: str
    rdev: int
    usec_initialized: str
    usb_id: str

class NodeOps:            # the real syscalls; tests pass a fake
    def open(self, path) -> int
    def fstat(self, fd) -> os.stat_result
    def fchown(self, fd, uid, gid)
    def fchmod(self, fd, mode)
    def remove_acl(self, fd)
    def write_uevent(self, devpath, action='change')
    def set_flag(self, usb_id, released: bool)

class NodeGuard:
    def __init__(self, ops=None, journal=HIDDEN_JOURNAL)
    def nodes_for(self, udevice) -> list          # (devnode, device_number, devpath)
    def hide(self, udevice, event_fd=None) -> list[HiddenNode]
    def release(self, usb_id: str, devpaths: list[str])
    def reconcile(self, keep: set) -> list[str]   # at start-up; returns what it released
```

### 7.4 Crash recovery

If the daemon dies, its uinput devices vanish with it (the kernel destroys
them when the fd closes) and its grabs end. The raw nodes stay 0600: games
see neither the combined device nor the raw ones. That is fail-closed, and
it is what happens today when the service is down.

- `Restart=on-failure` brings the daemon back in 3 s. At start it reads the
  journal and calls `reconcile(keep)`, `keep` being the devpaths its enabled
  specs are about to grab: a recorded node not kept is released (flag +
  re-trigger); a recorded node whose `usec_initialized` differs was replugged
  meanwhile and udev already set it up afresh, so it is dropped from the
  journal untouched.
- A clean stop (SIGTERM: `systemctl stop`, the installer's restart, a
  shutdown) releases everything the daemon hid before it exits and writes
  flags only for devices that are not covered by static rules; devices the
  static rules cover stay hidden (boot-ordering state), as today.
- **[Q5]** If the restart limit is hit (the daemon keeps crashing), devices
  stay hidden until a replug, a reboot, or `oversteer-proxy` removal.
  Alternative: `ExecStopPost=` running `python3 -m oversteer.proxy.daemon
  --release-all` when `$SERVICE_RESULT` is not `success`, which un-hides on
  every crash, including for the 3 s before the restart, where a running game
  could open the raw devices. Recommendation: stay fail-closed; the Devices
  tab already says when the service is not running and has a Start button,
  and `remove` already re-triggers.

### 7.5 Unit changes

```ini
[Service]
StateDirectory=oversteer
StateDirectoryMode=0700
RuntimeDirectory=oversteer
RuntimeDirectoryMode=0755
RuntimeDirectoryPreserve=yes
RestrictAddressFamilies=AF_UNIX AF_NETLINK
```

`ProtectKernelTunables=yes` makes `/sys` read-only, which blocks writing
`uevent`. Either drop it and add `ReadOnlyPaths=/proc/sys` (the tunables it
mainly protects), or keep it and add `ReadWritePaths=/sys/devices`; which one
systemd honours in which order has to be checked on the rig
(`systemd-analyze security oversteer-proxy`, then a real `unfold`).
`fchmod`/`fchown` on `/dev/input` nodes need no change: `ProtectSystem=strict`
leaves `/dev` writable and `DevicePolicy` governs `open()`, which the daemon
already has for `char-input`; `hidraw` needs `DeviceAllow=char-hidraw r` to be
opened for the fd-based permission change.

## 8. Threat model

| Actor | Can | Cannot | Worst case |
|---|---|---|---|
| A process as an authorised user (a malicious game mod, a compromised browser) | Everything the Invert box and Devices tab can do: flip a mapped axis, turn an installed spec off/on, fold/unfold a listed candidate, reset | Name a device, a match rule, an identity, a path; fold something not hardware-backed; fold a keyboard, mouse or wheel; exceed the slots' reserved codes; exceed rate limits | Sabotage while driving (handbrake reads pulled; combined device switched off, so the game loses the wheel); an unrelated USB device of a foldable kind grabbed away from the desktop until unfolded. That user could already write the wheel's sysfs attributes (0666 by Oversteer's own rules), kill the GUI, and usually create uinput devices, so this adds nuisance, not privilege |
| Another local user | Connect, get `denied` | Anything else | Uses up to 2 connection slots until closed; the 8-slot cap evicts idle ones |
| Any local process | Read the status file (as today) | Write under `/run/oversteer` or `/var/lib/oversteer` | Learns which devices are proxied |
| An unprivileged uinput/uhid client | Make a device claiming any VID:PID, name, phys, bustype | Be hardware-backed (§6.2), so never matched, folded, grabbed or chmod'ed | Nothing |
| Hostile USB hardware | Anything a USB device can | – | Out of scope (physical access) |

Specific attacks considered:

- **Parser DoS**: 1 KiB cap before parsing; `RecursionError` and every other
  exception caught per message (the connection gets `bad-request` or
  `internal`); the server thread never dies on input.
- **Flapping**: `set_enabled`/`fold` on and off to churn grabs and udev
  events: 1 per 2 s per spec, plus the per-uid bucket.
- **TOCTOU on device nodes**: identity is checked on the open fd
  (`st_rdev`, `EVIOCGID`) and all permission changes go through that fd; a
  replug between `add` and `fold` changes the handle (`stale-handle`).
- **Symlinks in /run**: `/run/oversteer`, its `released/` subdirectory and
  `/var/lib/oversteer` are created by systemd as root and never writable by
  users. Before `bind()`, a leftover `control.sock` is removed only if
  `lstat` shows a socket owned by root. All state files are written by
  `mkstemp` + `rename` in their own directory and read with `O_NOFOLLOW`.
- **uinput abuse by the daemon itself**: the channel cannot create or change
  a uinput device's identity or capabilities; the only uinput devices are
  those of installed specs, with installed identities. This also keeps the
  anti-cheat rule (1.0.0: no identity the owner has not chosen).
- **Log injection**: only validated values reach logs.
- **A tampered state file**: root-only directory; re-validated on load like a
  command; unreadable or oversized means ignored.

## 9. Migration

- The protocol arrives with a pkexec install (the daemon copy is root-owned
  and only an install replaces it). Until then the socket is missing and the
  GUI does what it does today.
- The status file gains a top-level
  `"control": {"protocol": 1, "socket": "/run/oversteer/control.sock"}`;
  proxies gain `"runtime"` (the overlay as applied) and the top level gains
  `"candidates"`.
- The installer additionally: writes `/etc/oversteer/control.json` from
  `PKEXEC_UID`/`SUDO_UID` (merging with the users already listed); adds
  `control.py`, `runtime.py`, `permissions.py` to `DAEMON_FILES`; writes the
  new unit and the `TEST!=` rule form; clears `released/` flags for specs it
  (re)installs; `remove()` also removes `/var/lib/oversteer` and the policy
  file.
- The builder (`build_combined_spec`) emits `slots` and `kind`; existing
  specs without them keep working and just get no auto-fold until rebuilt.
- The GUI keeps the pkexec path for what the channel cannot do: first
  install, building or rebuilding a spec (new identity, new match rules, new
  capabilities), anything answered `no-slot`, and every command when the
  socket is missing or `hello` fails.

## 10. GUI changes

A small client, importable from the GUI and the Flatpak, with no GTK in it:

```python
# oversteer/proxy/control_client.py
CONTROL_SOCKET = '/run/oversteer/control.sock'

class ControlUnavailable(Exception): ...          # no socket, refused connect, old daemon
class ControlRefused(Exception):                  # the daemon said ok: false
    code: str; message: str

class ControlClient:
    def __init__(self, path=CONTROL_SOCKET, timeout=0.5)
    def call(self, op: str, **args) -> dict
    def hello(self) -> dict
    def set_invert(self, spec: str, source: str, axis, value) -> dict
    def set_enabled(self, spec: str, value: bool) -> dict
    def fold(self, spec: str, handle: str) -> dict
    def unfold(self, spec: str, source: str) -> dict
    def include(self, spec: str, usb_id: str) -> dict
    def reset(self, spec: str) -> dict

def available(status: dict) -> bool   # status['control']['protocol'] >= 1 and the socket exists
```

**Handbrake Invert box** (`gui.set_handbrake_invert`): if the channel is
available, send `set_invert(proxy_id, source, from_code, state)` from a
worker thread (the reply takes milliseconds, but the GTK thread never waits
on a socket), then on `ok` save the same value into the user spec in
`~/.config/oversteer/proxies/` so the next install carries it, and update
`handbrake_invert` from the reply's `inverted_axes`. On `ControlRefused` show
the message and put the box back; on `ControlUnavailable` fall back to
today's pkexec reinstall. No `combine_busy` spinner on the fast path. The box
now behaves like the pedal boxes.

**[Q2, second half]** The pedal Invert boxes are per Oversteer profile (the
GUI writes them to the driver when a profile loads). Should the handbrake's
be too? Recommendation: no, one value per combined device, kept by the
daemon: the handbrake is not the wheel's, it has to be right when the game
starts without the GUI, and a per-profile value would mean the GUI pushing
`set_invert` on every profile load. Easy to add later if wanted.

**Devices tab**:

- The Combine switch, for a spec that is installed and unchanged: off/on is
  `set_enabled` (no prompt). Building, rebuilding, or changing the
  identity checkbox stays an install.
- Equipment rows show the candidate `state`: "in combined device",
  "folded automatically", "can be added" (tick = `fold`, no prompt),
  "left out" (excluded; tick = `include` + `fold`), "needs a rebuild"
  (`no-slot`/`no-room`: tick = today's rebuild with a prompt). Unticking a
  folded device is `unfold`.
- The row's tick is matched to a candidate by the daemon's handle, which the
  status file gives per row (by `sys_path` → devpath), never by building a
  match in the GUI.
- A "Keep room for a handbrake" option when building (defaults per §6.1
  **[Q3]**).
- `refresh_equipment()` reads `candidates` from the status file; the 5 s
  refresh stays.

## 11. Tests

All in `tests/test_proxy_control.py` and `tests/test_proxy_permissions.py`,
none touching a real device, none needing root; the existing uinput tests in
`tests/test_proxy.py` keep their skip-if and gain `trust=lambda d: True`.

- **Protocol** (table-driven): every verb's good form; unknown op, unknown
  key, missing key, `1` for `true`, `"true"` for `true`, `id` as bool,
  oversize, invalid UTF-8, non-object JSON, 10 000-deep nesting,
  `v: 2`; unknown spec, a built-in-only spec **[Q6]**, unknown source, an axis
  that is mapped on another source, a KEY code as axis, `unfold` of the
  wheel, a stale handle, `include` of a device not excluded.
- **Authorisation**: `Policy.allows()` with fake creds; a real
  `socketpair(AF_UNIX, SOCK_SEQPACKET)` checking `peer_cred()` returns our own
  uid and pid (works unprivileged); a server bound in `tmp_path` with a policy
  not listing us answers `denied` and closes.
- **Limits**: token bucket and per-spec device-op limit with a fake clock;
  connection cap evicts the oldest idle connection.
- **Overlay**: `apply_overlay` purity (the installed spec is unchanged);
  save/load round-trip in `tmp_path`; a changed `base` drops invert/folds and
  keeps `excluded`; bounds; a symlinked or oversized state file is ignored;
  write coalescing with a fake clock.
- **Fold planning**: `SimpleNamespace` equipment as in
  `test_shifter_sequential_buttons_are_mapped`: a handbrake into a slot with
  a reserved axis; a second handbrake → `no-room`; no slot → `no-slot`; the
  shifter planner and the builder give identical mappings for the same
  device (regression guard for the refactor); a known vs a guessed device
  with `auto: true` (**[Q4]**); a keyboard and a wheel are never candidates.
- **Trust**: `hardware_backed` over fake pyudev devices with sys paths under
  `/sys/devices/virtual/input/…`, `/sys/devices/virtual/misc/uhid/…` and
  `/sys/devices/pci…/usb1/1-2/…`; a virtual device with `bustype` USB and
  the ANNX VID:PID is still refused.
- **Permissions**: `NodeGuard` with a fake `NodeOps` recording calls: the
  order (grab before chmod), `st_rdev` mismatch aborts with nothing changed,
  the journal round-trip, `reconcile` releases what is not kept and drops a
  replugged node untouched, the `released/` flag is set before the
  re-trigger and cleared before hiding.
- **Rules**: `hide_rules()` output contains `TEST!="/run/oversteer/released/vvvv:pppp"`
  on all three line kinds and still sanitises labels.
- **Live invert** (uinput, skipped without `/dev/uinput`, like the existing
  ones): a running proxy flips `invert` through `set_invert()` and the
  virtual axis jumps to the mirrored value without the source moving.
- **Command queue**: `ProxyDevice.submit()` runs on the proxy thread (a fake
  `_pump` loop), futures time out cleanly on a stopped proxy.

## 12. Later: hotkeys through the daemon (86e3dme66)

The GUI reads wheel buttons from the combined virtual device. To keep a
bound button from also reaching the game, the daemon has to drop it before
the uinput write, and then the GUI no longer sees it on the virtual device,
so the daemon must hand it over on the channel. Same socket, same auth:

```json
{"v": 1, "id": 1, "op": "watch_keys", "spec": "combined-wheel",
 "keys": ["BTN_TRIGGER_HAPPY5", 706], "swallow": true}
```

- `keys` ≤ 32 codes, each one the virtual device advertises; `swallow` bool.
- The daemon replies `ok`, then pushes events on that connection:
  `{"v": 1, "ev": "key", "spec": "combined-wheel", "code": 706, "value": 1,
  "t": 12345.678}` (no `id`, which is how a client tells them from replies).
- Swallowing lasts exactly as long as the connection: when the GUI exits or
  crashes, the socket closes and the buttons reach the game again. No
  persistence, so a dead GUI can never leave buttons eaten.
- One watching connection per spec; a second one replaces the first.
  Per-connection send queue of 64 events, oldest dropped with an
  `{"ev": "overflow"}` marker, so a stalled GUI never blocks the proxy
  thread. The swallow set is a frozenset swapped atomically, read on the
  event path without a lock.
- The idle timeout does not apply to a watching connection.

Not built in this rework; the grammar above is reserved so v1 clients do not
collide with it.

## 13. Build plan

Small steps, each one committed with its tests, each leaving the GUI and
service working as before if the run stops there.

1. **Spec**: `slots`, `SourceSpec.kind`, `SourceSpec.virtual`; validation
   and `to_dict`; tests. No behaviour change.
2. **Trust**: `equipment.hardware_backed()`; `SourceSpec.matches()` uses it
   through a `trust` parameter on `ProxyDevice`/`ProxyManager` (daemon
   default strict, tests permissive); tests. First real hardening.
3. **Overlay**: `oversteer/proxy/runtime.py` (`SpecOverlay`, `spec_hash`,
   `apply_overlay`, `validate_overlay`, `OverlayStore`); tests.
4. **Live changes in the proxy**: `ProxyDevice.submit()`, `set_invert()` with
   the fresh-`absinfo` resync, `add_source()`, `remove_source()`; manager
   methods of §4.4 using the overlay; tests (fake + one uinput).
5. **Channel**: `oversteer/proxy/control.py` (parse, validate, `Policy`,
   limits, `ControlServer`), `control_client.py`, wiring in `daemon.py`,
   `control` in the status file; tests. Then on the rig, as the owner: a
   manual `set_invert` from a host shell and from inside the Flatpak (the
   `:ro` mount question of §3.3).
6. **Installer and unit**: policy file from `PKEXEC_UID`, `DAEMON_FILES`,
   unit changes of §7.5 (the sysfs-write question checked on the rig),
   `remove()` cleanup. Owner installs once with a prompt.
7. **GUI, invert and on/off**: handbrake Invert box and Combine switch
   through the channel with the pkexec fallback; user spec mirrored.
   Requirement 4a done here.
8. **Permissions**: `oversteer/proxy/permissions.py`, fd-based hiding at
   attach for every source, journal, `reconcile` at start, release on
   stop/disable, `TEST!=` rules; tests. Rig check: replug, disable, crash
   (`kill -9`) and restart, with `getfacl`/`ls -l` on the nodes.
9. **Fold planner**: split `build_combined_spec()` into the per-kind
   functions and `plan_fold()`; the builder emits `slots`/`kind` and the
   companion handbrake spec for the wheel identity (**[Q3]**); tests
   including the builder/planner equivalence.
10. **Auto-fold**: candidate table on `add`/`remove`, handles, `fold`,
    `unfold`, `include`, `reset`; `candidates` in status and `get`; tests.
    Rig check: plug the ANNX in after start, with and without a game
    running.
11. **GUI, Devices tab**: candidate states, tick = fold/unfold, "Keep room
    for a handbrake". Requirement 4b done here.
12. **Docs and changelog**; the Opus 5.5 diff review before release, as
    RELEASING.md says.
13. (Later, 86e3dme66) `watch_keys`/`swallow` of §12.

## 14. Progress

- [ ] 1 Spec: slots, kind, virtual
- [ ] 2 Trust rule
- [ ] 3 Overlay
- [ ] 4 Live changes in the proxy
- [ ] 5 Channel (+ rig: host and Flatpak connect)
- [ ] 6 Installer and unit (+ rig: sysfs write under the unit's sandbox)
- [ ] 7 GUI: Invert box and Combine switch
- [ ] 8 Daemon-managed permissions (+ rig: replug, disable, crash)
- [ ] 9 Fold planner
- [ ] 10 Auto-fold (+ rig: late handbrake)
- [ ] 11 GUI: Devices tab
- [ ] 12 Docs, changelog, review
- [ ] 13 Hotkeys through the daemon (later)

## 15. Open questions for the owner

| # | Question | Recommendation | Blocks |
|---|---|---|---|
| Q1 | Who may use the channel: only users who authenticated an install (`PKEXEC_UID`), or also whoever is active on seat0, or an `oversteer` group? | Install-time users only; group support in the format but unused | step 5 |
| Q2 | Do runtime changes survive a reboot (`/var/lib`) or only a daemon restart (`/run`)? Is the handbrake invert per Oversteer profile like the pedals, or one value per combined device? | Survive reboots; one value per device, kept by the daemon | steps 3, 7 |
| Q3 | Reserve a handbrake axis on the combined device up front? It changes the layout, which FH6 minds for the G29 identity | Reserve with the generic identity; with the wheel's identity use a companion handbrake device created when a handbrake is folded | step 9 |
| Q4 | Fold automatically only devices in the known tables, or also ones the heuristics guess? | Known only; guessed ones are one click (no password) | step 10 |
| Q5 | After a crash with the restart limit hit, stay fail-closed (devices hidden until replug/reboot) or un-hide in `ExecStopPost`? | Fail-closed | step 8 |
| Q6 | May the channel enable a built-in spec that was never installed to `/etc` (e.g. `annx-fanatec-handbrake`)? | No: installed specs only | step 5 |
| Q7 | Trust classic Bluetooth HID devices as sources (`hci` ancestor)? BLE through uhid can never be told from a user-made device | Not until someone has Bluetooth racing gear | step 2 |
| Q8 | A game that opened a device's hidraw node before the grab keeps reading it; accept and document? | Accept and document | step 8 |
| Q9 | When the combined device is enabled over the channel while a companion is already running, the companion enumerates first. Stop and restart the companions to keep the wheel first, or leave them and tell the user to restart the game? | Restart the companions after the wheel; owner's call because it drops a companion device a running game holds | step 7 |
| Q10 | Should a device folded at runtime be made "permanent" (a static hide rule for boot ordering) by the next install, offered as a button, or never? | Offered as a button on the Devices tab ("Hide from games at boot", one prompt) | step 11 |
