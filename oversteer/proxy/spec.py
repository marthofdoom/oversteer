"""Declarative description of a proxy device.

A spec is plain JSON so it can be shipped with Oversteer, edited by hand and
shared between users. Example (the built-in ANNX handbrake proxy)::

    {
      "id": "annx-fanatec-handbrake",
      "name": "ANNX handbrake as Fanatec ClubSport Handbrake",
      "identity": {"name": "Fanatec ClubSport Handbrake", "vendor": "0eb7",
                   "product": "00e5", "version": "0001", "bustype": "usb"},
      "sources": {"hb": {"match": {"vendor": "8086", "product": "0a01"}}},
      "capabilities": {"keys": ["BTN_TRIGGER"],
                       "abs": {"ABS_Z": {"min": 0, "max": 65535}}},
      "mappings": [{"source": "hb", "from": "ABS_THROTTLE", "to": "ABS_Z",
                    "invert": true}]
    }

Codes are written by their evdev names ("ABS_Z", "BTN_TRIGGER"); numbers are
accepted too. Vendor/product/version are hex strings, as udev reports them.
"""

from dataclasses import dataclass, field
from evdev import ecodes
import json
import re

BUSTYPES = {
    'usb': ecodes.BUS_USB,
    'bluetooth': ecodes.BUS_BLUETOOTH,
    'virtual': ecodes.BUS_VIRTUAL,
}

ID_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')


class SpecError(ValueError):
    pass


def _hex(value, what):
    if value is None:
        return None
    try:
        number = value if isinstance(value, int) else int(str(value), 16)
    except (ValueError, TypeError):
        raise SpecError("{}: not a hex number: {!r}".format(what, value))
    if not 0 <= number <= 0xffff:
        raise SpecError("{}: out of range: {!r}".format(what, value))
    return number


_TEXT_RE = re.compile(r'^[\x20-\x7e]{1,200}$')


def _text(value, what, allow_empty=False):
    """Printable ASCII only: these strings end up in udev rules, unit files
    and device names."""
    if value is None or (value == '' and allow_empty):
        return value
    if not isinstance(value, str) or not _TEXT_RE.match(value):
        raise SpecError("{}: must be printable text without control characters (1-200 chars)".format(what))
    return value


def _pattern(value, what):
    if value is None:
        return None
    _text(value, what)
    try:
        re.compile(value)
    except re.error as e:
        raise SpecError("{}: bad pattern {!r}: {}".format(what, value, e))
    return value


def code_from_name(name, ev_type=None):
    """Resolve "ABS_Z" / "BTN_TRIGGER" / 300 / "0x12c" / "ABS:11" to (ev_type, code).

    Numbers are for codes without an evdev name (the G29 shifter's gears
    1-3 are 300-302); they take the type given, a "KEY:"/"ABS:" prefix, or KEY."""
    if isinstance(name, str):
        m = re.match(r'^(KEY|ABS):(0x[0-9a-fA-F]+|[0-9]+)$', name)
        if m:
            ev_type = ecodes.EV_KEY if m.group(1) == 'KEY' else ecodes.EV_ABS
            name = int(m.group(2), 0)
        elif re.match(r'^(0x[0-9a-fA-F]+|[0-9]+)$', name):
            name = int(name, 0)
    if isinstance(name, bool) or not isinstance(name, (int, str)):
        raise SpecError("bad event code: {!r}".format(name))
    if isinstance(name, int):
        if not 0 <= name <= ecodes.KEY_MAX:
            raise SpecError("event code out of range: {}".format(name))
        return (ev_type if ev_type is not None else ecodes.EV_KEY), name
    prefix = str(name).split('_', 1)[0]
    if prefix == 'ABS':
        table, etype = ecodes.ecodes, ecodes.EV_ABS
    elif prefix in ('BTN', 'KEY'):
        table, etype = ecodes.ecodes, ecodes.EV_KEY
    elif prefix == 'REL':
        table, etype = ecodes.ecodes, ecodes.EV_REL
    else:
        raise SpecError("unknown event code: {!r}".format(name))
    if name not in table:
        raise SpecError("unknown event code: {!r}".format(name))
    if ev_type is not None and ev_type != etype:
        raise SpecError("event code {!r} is not of the expected type".format(name))
    return etype, table[name]


def code_name(ev_type, code):
    names = {ecodes.EV_ABS: ecodes.ABS, ecodes.EV_KEY: ecodes.BTN, ecodes.EV_REL: ecodes.REL}.get(ev_type, {})
    name = names.get(code)
    if name is None and ev_type == ecodes.EV_KEY:
        name = ecodes.KEY.get(code)
    if isinstance(name, (tuple, list)):
        name = name[0]
    return name or str(code)


@dataclass
class Identity:
    """How the virtual device presents itself."""
    name: str
    vendor: int = 0
    product: int = 0
    version: int = 0
    bustype: int = ecodes.BUS_USB
    phys: str = None

    @classmethod
    def from_dict(cls, data):
        if 'name' not in data:
            raise SpecError("identity.name is required")
        _text(data['name'], 'identity.name')
        _text(data.get('phys'), 'identity.phys')
        bustype = data.get('bustype', 'usb')
        if isinstance(bustype, str):
            if bustype not in BUSTYPES:
                raise SpecError("identity.bustype must be one of {}".format(', '.join(BUSTYPES)))
            bustype = BUSTYPES[bustype]
        return cls(
            name=data['name'],
            vendor=_hex(data.get('vendor', 0), 'identity.vendor') or 0,
            product=_hex(data.get('product', 0), 'identity.product') or 0,
            version=_hex(data.get('version', 0), 'identity.version') or 0,
            bustype=bustype,
            phys=data.get('phys'),
        )

    def to_dict(self):
        bus = next((k for k, v in BUSTYPES.items() if v == self.bustype), self.bustype)
        data = {'name': self.name, 'vendor': '{:04x}'.format(self.vendor),
                'product': '{:04x}'.format(self.product), 'version': '{:04x}'.format(self.version),
                'bustype': bus}
        if self.phys:
            data['phys'] = self.phys
        return data


@dataclass
class SourceSpec:
    """A physical device the proxy reads from."""
    key: str
    vendor: int = None
    product: int = None
    name: str = None          # regular expression matched against the evdev name
    phys: str = None          # regular expression matched against phys
    grab: bool = True         # take exclusive access so games don't see the events
    hide: bool = True         # make the real device root-only (udev rule) so games can't open it
    required: bool = True     # proxy stays down until this source is present

    @classmethod
    def from_dict(cls, key, data):
        if not ID_RE.match(str(key)):
            raise SpecError("source key {!r}: lowercase letters, digits, '.', '_' or '-'".format(key))
        if not isinstance(data, dict):
            raise SpecError("source {!r} must be an object".format(key))
        match = data.get('match', {})
        if not isinstance(match, dict) or not match:
            raise SpecError("source {!r} needs a 'match' block".format(key))
        source = cls(
            key=key,
            vendor=_hex(match.get('vendor'), 'source.match.vendor'),
            product=_hex(match.get('product'), 'source.match.product'),
            name=_pattern(match.get('name'), 'source.match.name'),
            phys=_pattern(match.get('phys'), 'source.match.phys'),
            grab=bool(data.get('grab', True)),
            hide=bool(data.get('hide', True)),
            required=bool(data.get('required', True)),
        )
        if source.vendor is None and source.product is None and not source.name and not source.phys:
            raise SpecError("source {!r}: match needs a vendor/product, name or phys".format(key))
        return source

    def to_dict(self):
        match = {}
        if self.vendor is not None:
            match['vendor'] = '{:04x}'.format(self.vendor)
        if self.product is not None:
            match['product'] = '{:04x}'.format(self.product)
        if self.name is not None:
            match['name'] = self.name
        if self.phys is not None:
            match['phys'] = self.phys
        return {'match': match, 'grab': self.grab, 'hide': self.hide, 'required': self.required}

    def matches(self, device):
        """device: evdev.InputDevice. Virtual devices (other proxies) and
        keyboards never match: a source must be a controller."""
        info = device.info
        if (device.phys or '').startswith('py-evdev-uinput') and self.phys is None:
            return False               # another proxy's virtual device, unless asked for by phys
        caps = device.capabilities()
        if ecodes.EV_ABS not in caps and not any(c >= ecodes.BTN_MISC for c in caps.get(ecodes.EV_KEY, [])):
            return False
        if self.vendor is not None and info.vendor != self.vendor:
            return False
        if self.product is not None and info.product != self.product:
            return False
        if self.name is not None and not re.search(self.name, device.name or ''):
            return False
        if self.phys is not None and not re.search(self.phys, device.phys or ''):
            return False
        return True


@dataclass
class AbsSpec:
    min: int = 0
    max: int = 255
    fuzz: int = 0
    flat: int = 0
    resolution: int = 0

    @classmethod
    def from_dict(cls, data):
        try:
            spec = cls(**{k: int(v) for k, v in data.items()})
        except (TypeError, ValueError, AttributeError) as e:
            raise SpecError("bad abs info: {}".format(e))
        if spec.max <= spec.min or not (-0x80000000 <= spec.min and spec.max <= 0x7fffffff):
            raise SpecError("abs range must be increasing 32-bit values")
        return spec

    def to_dict(self):
        return {'min': self.min, 'max': self.max, 'fuzz': self.fuzz, 'flat': self.flat,
                'resolution': self.resolution}

    def as_tuple(self):
        return (0, self.min, self.max, self.fuzz, self.flat, self.resolution)


@dataclass
class Mapping:
    """One event translation rule.

    Supported translations:

    - ABS -> ABS: rescaled from the source range to the target range, optionally
      inverted. `deadzone` (0..1, fraction of travel around the centre) and
      `raw` (copy value untouched) are optional.
    - KEY -> KEY: value copied.
    - ABS -> KEY: pressed while the normalised source value satisfies `when`
      ({"gt": 0.5} or {"lt": 0.25}).
    - KEY -> ABS: pressed = target max, released = target min (or inverted).
    """
    source: str = None
    from_type: int = 0
    from_code: int = 0
    to_type: int = 0
    to_code: int = 0
    invert: object = False      # True / False / 'auto' (invert when the axis rests in its upper half)
    raw: bool = False
    deadzone: float = 0.0
    when: dict = None

    @classmethod
    def from_dict(cls, data, sources):
        if 'from' not in data or 'to' not in data:
            raise SpecError("mapping needs 'from' and 'to'")
        source = data.get('source')
        if source is None:
            if len(sources) != 1:
                raise SpecError("mapping {!r}: 'source' is required with several sources".format(data['from']))
            source = next(iter(sources))
        elif source not in sources:
            raise SpecError("mapping {!r}: unknown source {!r}".format(data['from'], source))
        from_type, from_code = code_from_name(data['from'], {'ABS': ecodes.EV_ABS, 'KEY': ecodes.EV_KEY}.get(data.get('from_type')))
        to_type, to_code = code_from_name(data['to'], from_type if isinstance(data['to'], (int, str)) and
                                          re.match(r'^(0x[0-9a-fA-F]+|[0-9]+)$', str(data['to'])) else None)
        if from_type not in (ecodes.EV_ABS, ecodes.EV_KEY) or to_type not in (ecodes.EV_ABS, ecodes.EV_KEY):
            raise SpecError("mapping {!r}: only ABS and KEY events can be mapped".format(data['from']))
        when = data.get('when')
        if from_type == ecodes.EV_ABS and to_type == ecodes.EV_KEY:
            if not isinstance(when, dict) or not any(k in when for k in ('gt', 'lt')):
                raise SpecError("mapping {!r}: ABS -> KEY needs 'when': {{\"gt\"|\"lt\": value}}".format(data['from']))
            try:
                when = {k: float(v) for k, v in when.items() if k in ('gt', 'lt')}
            except (TypeError, ValueError):
                raise SpecError("mapping {!r}: 'when' values must be numbers".format(data['from']))
            if not all(0 <= v <= 1 for v in when.values()):
                raise SpecError("mapping {!r}: 'when' thresholds are fractions 0..1".format(data['from']))
        else:
            when = None
        try:
            deadzone = float(data.get('deadzone', 0))
        except (TypeError, ValueError):
            raise SpecError("mapping {!r}: deadzone must be a number".format(data['from']))
        if not 0 <= deadzone < 1:
            raise SpecError("mapping {!r}: deadzone must be within [0, 1)".format(data['from']))
        invert = data.get('invert', False)
        if invert != 'auto':
            invert = bool(invert)
        elif from_type != ecodes.EV_ABS:
            raise SpecError("mapping {!r}: 'auto' inversion is for axes".format(data['from']))
        return cls(source=source, from_type=from_type, from_code=from_code, to_type=to_type,
                   to_code=to_code, invert=invert,
                   raw=bool(data.get('raw', False)), deadzone=deadzone, when=when)

    def to_dict(self):
        data = {'source': self.source, 'from': code_name(self.from_type, self.from_code),
                'to': code_name(self.to_type, self.to_code)}
        if self.invert:
            data['invert'] = self.invert
        if self.raw:
            data['raw'] = True
        if self.deadzone:
            data['deadzone'] = self.deadzone
        if self.when:
            data['when'] = self.when
        return data


@dataclass
class ProxySpec:
    id: str
    name: str
    identity: Identity
    sources: dict                 # key -> SourceSpec
    mappings: list = field(default_factory=list)
    keys: list = field(default_factory=list)       # extra EV_KEY codes to advertise
    abs: dict = field(default_factory=dict)        # code -> AbsSpec
    passthrough: str = None       # source key whose events/caps are copied 1:1
    ff_source: str = None         # source key that receives force feedback
    description: str = ''
    enabled: bool = False
    builtin: bool = False
    path: str = None

    @classmethod
    def from_dict(cls, data, builtin=False, path=None):
        try:
            return cls._from_dict(data, builtin, path)
        except SpecError:
            raise
        except (TypeError, ValueError, AttributeError, KeyError) as e:
            raise SpecError("malformed spec: {}".format(e))

    @classmethod
    def _from_dict(cls, data, builtin, path):
        if not isinstance(data, dict):
            raise SpecError("spec must be a JSON object")
        pid = data.get('id')
        if not isinstance(pid, str) or not ID_RE.match(pid) or len(pid) > 64:
            raise SpecError("id must be lowercase letters, digits, '.', '_' or '-'")
        if not isinstance(data.get('identity'), dict):
            raise SpecError("identity is required")
        _text(data.get('name', pid), 'name')
        if 'description' in data and not isinstance(data['description'], str):
            raise SpecError("description must be text")
        if not isinstance(data.get('sources', {}), dict):
            raise SpecError("sources must be an object")
        sources = {key: SourceSpec.from_dict(key, value) for key, value in data.get('sources', {}).items()}
        if not sources:
            raise SpecError("at least one source is required")
        passthrough = data.get('passthrough')
        if passthrough is not None and passthrough not in sources:
            raise SpecError("passthrough: unknown source {!r}".format(passthrough))
        ff_source = (data.get('ff') or {}).get('source')
        if ff_source is not None and ff_source not in sources:
            raise SpecError("ff.source: unknown source {!r}".format(ff_source))
        caps = data.get('capabilities', {})
        keys = []
        for name in caps.get('keys', []):
            keys.append(code_from_name(name, ecodes.EV_KEY)[1])
        abs_specs = {}
        for name, info in caps.get('abs', {}).items():
            abs_specs[code_from_name(name, ecodes.EV_ABS)[1]] = AbsSpec.from_dict(info)
        mappings = [Mapping.from_dict(m, sources) for m in data.get('mappings', [])]
        if not mappings and passthrough is None:
            raise SpecError("spec has neither mappings nor a passthrough source")
        for m in mappings:
            if m.to_type == ecodes.EV_ABS and m.to_code not in abs_specs:
                raise SpecError("mapping to {} needs capabilities.abs.{}".format(
                    code_name(m.to_type, m.to_code), code_name(m.to_type, m.to_code)))
        return cls(
            id=pid,
            name=data.get('name', pid),
            identity=Identity.from_dict(data['identity']),
            sources=sources,
            mappings=mappings,
            keys=keys,
            abs=abs_specs,
            passthrough=passthrough,
            ff_source=ff_source,
            description=re.sub(r'[^\x20-\x7e\n]', '?', data.get('description', ''))[:2000],
            enabled=bool(data.get('enabled', False)),
            builtin=builtin,
            path=path,
        )

    def to_dict(self):
        data = {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'enabled': self.enabled,
            'identity': self.identity.to_dict(),
            'sources': {k: v.to_dict() for k, v in self.sources.items()},
            'capabilities': {
                'keys': [code_name(ecodes.EV_KEY, c) for c in self.keys],
                'abs': {code_name(ecodes.EV_ABS, c): a.to_dict() for c, a in self.abs.items()},
            },
            'mappings': [m.to_dict() for m in self.mappings],
        }
        if self.passthrough:
            data['passthrough'] = self.passthrough
        if self.ff_source:
            data['ff'] = {'source': self.ff_source}
        return data

    @classmethod
    def load(cls, path, builtin=False):
        with open(path, 'r') as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as e:
                raise SpecError("{}: invalid JSON: {}".format(path, e))
        return cls.from_dict(data, builtin=builtin, path=path)

    def save(self, path):
        with open(path, 'w') as f:
            json.dump(self.to_dict(), f, indent=2)
            f.write('\n')
        self.path = path

    def key_codes(self):
        """All EV_KEY codes the virtual device advertises."""
        codes = set(self.keys)
        for m in self.mappings:
            if m.to_type == ecodes.EV_KEY:
                codes.add(m.to_code)
        return sorted(codes)
