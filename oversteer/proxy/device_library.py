"""Library of known device profiles.

A device profile describes what a real controller looks like to evdev (its
identity and capability set), so a proxy identity can be checked against
something known to work. One JSON file per device in ``device_profiles/``;
this is separate from ``profiles/``, which holds proxy *specs*. The identity
block (name/vendor/product/version/bustype/phys) is the same shape as a spec's
``identity`` and ``abs`` entries use the same fields as spec.AbsSpec.

Fields: id, name, vendor, product, version (hex strings), bustype, phys (a
regular expression), kind, verified, anti_cheat_class, notes, capabilities
{keys, abs, ff} and, for a capture, capture {date, source, complete, ...}.
Missing capability data is left out, never guessed: an ``abs`` entry of ``{}``
means the axis exists but its range was not captured.

``verified`` is true only for a complete capture from real hardware on
marth's rig (``capture.complete`` true). ``anti_cheat_class`` is one of
CLASSES; the policy for what each class allows lives in docs/anti-cheat.md.
"""

import json
import os
import re

from oversteer.proxy.spec import AbsSpec, Identity, SpecError, code_from_name
from evdev import ecodes

LIBRARY_DIR = os.path.join(os.path.dirname(__file__), 'device_profiles')
CLASSES = ('passthrough-only', 'emulate-ok', 'unknown')
KINDS = ('wheel', 'shifter', 'handbrake', 'pedals', 'buttonbox')
_KNOWN = {'id', 'name', 'vendor', 'product', 'version', 'bustype', 'phys', 'kind', 'verified',
          'anti_cheat_class', 'notes', 'capabilities', 'capture'}


class ProfileError(SpecError):
    pass


def validate(data):
    """Check one profile dict; raises ProfileError. Returns it unchanged."""
    try:
        return _validate(data)
    except SpecError as e:
        if isinstance(e, ProfileError):
            raise
        raise ProfileError(str(e))


def _validate(data):
    if not isinstance(data, dict):
        raise ProfileError("profile must be an object")
    extra = set(data) - _KNOWN
    if extra:
        raise ProfileError("unknown fields: {}".format(', '.join(sorted(extra))))
    if not re.match(r'^[a-z0-9][a-z0-9._-]*$', str(data.get('id', ''))):
        raise ProfileError("id: lowercase letters, digits, '.', '_' or '-'")
    Identity.from_dict({k: data[k] for k in ('name', 'vendor', 'product', 'version', 'bustype', 'phys')
                        if k in data})
    if 'vendor' not in data:
        raise ProfileError("vendor is required")
    if 'phys' in data:
        try:
            re.compile(data['phys'])
        except re.error as e:
            raise ProfileError("phys: bad pattern: {}".format(e))
    if data.get('kind') not in KINDS:
        raise ProfileError("kind must be one of {}".format(', '.join(KINDS)))
    if not isinstance(data.get('verified'), bool):
        raise ProfileError("verified must be true or false")
    if data.get('anti_cheat_class') not in CLASSES:
        raise ProfileError("anti_cheat_class must be one of {}".format(', '.join(CLASSES)))
    if not isinstance(data.get('notes'), str):
        raise ProfileError("notes must be a string")
    capture = data.get('capture')
    if capture is not None:
        if not isinstance(capture, dict) or not re.match(r'^\d{4}-\d{2}-\d{2}$', str(capture.get('date', ''))) \
                or not capture.get('source') or not isinstance(capture.get('complete'), bool):
            raise ProfileError("capture needs date (YYYY-MM-DD), source and complete")
    if data['verified']:
        if not capture or not capture['complete'] or capture['source'] != "marth's rig":
            raise ProfileError("verified needs a complete capture from marth's rig")
        if not data.get('capabilities', {}).get('abs') and not data.get('capabilities', {}).get('keys'):
            raise ProfileError("verified needs a capability set")
    caps = data.get('capabilities', {})
    if not isinstance(caps, dict) or set(caps) - {'keys', 'abs', 'ff'}:
        raise ProfileError("capabilities may only hold keys, abs and ff")
    for name in caps.get('keys', []):
        code_from_name(name, ecodes.EV_KEY)
    abs_ = caps.get('abs', {})
    if not isinstance(abs_, dict):
        raise ProfileError("capabilities.abs must be an object")
    for name, info in abs_.items():
        code_from_name(name, ecodes.EV_ABS)
        if not isinstance(info, dict):
            raise ProfileError("capabilities.abs.{} must be an object".format(name))
        if info:
            if 'min' not in info or 'max' not in info:
                raise ProfileError("capabilities.abs.{}: min and max go together".format(name))
            AbsSpec.from_dict(info)
        elif data['verified']:
            raise ProfileError("capabilities.abs.{}: a verified profile needs the range".format(name))
    for name in caps.get('ff', []):
        if not isinstance(name, str) or not (name in ecodes.ecodes and name.startswith('FF_')):
            raise ProfileError("capabilities.ff: unknown effect {!r}".format(name))
    return data


def identity(data):
    """The profile as a spec.Identity (a profile with no product id gives product 0)."""
    return Identity.from_dict({k: data[k] for k in ('name', 'vendor', 'product', 'version', 'bustype', 'phys')
                               if k in data})


def load_profiles(directory=LIBRARY_DIR):
    """All profiles in the directory, sorted by id. Raises ProfileError naming the bad file."""
    profiles = []
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith('.json'):
            continue
        try:
            with open(os.path.join(directory, filename)) as f:
                data = validate(json.load(f))
        except (ValueError, ProfileError) as e:
            raise ProfileError("{}: {}".format(filename, e))
        if filename != data['id'] + '.json':
            raise ProfileError("{}: file name must match id {!r}".format(filename, data['id']))
        profiles.append(data)
    ids = [p['id'] for p in profiles]
    if len(ids) != len(set(ids)):
        raise ProfileError("duplicate profile ids")
    return profiles


def by_id(vendor, product, directory=LIBRARY_DIR):
    """Profiles with this USB vendor/product (ints or hex strings); profiles without a product id never match."""
    vendor = vendor if isinstance(vendor, int) else int(vendor, 16)
    product = product if isinstance(product, int) else int(product, 16)
    return [p for p in load_profiles(directory)
            if 'product' in p and int(p['vendor'], 16) == vendor and int(p['product'], 16) == product]
