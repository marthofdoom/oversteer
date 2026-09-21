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
    if isinstance(value, int):
        return value
    try:
        return int(str(value), 16)
    except ValueError:
        raise SpecError("{}: not a hex number: {!r}".format(what, value))


def code_from_name(name, ev_type=None):
    """Resolve "ABS_Z" / "BTN_TRIGGER" / 300 / "0x12c" to (ev_type, code).

    Numbers are for codes without an evdev name (the G29 shifter's gears
    1-3 are 300-302); they take the type given, or KEY."""
    if isinstance(name, str) and re.match(r'^(0x[0-9a-fA-F]+|[0-9]+)$', name):
        name = int(name, 0)
    if isinstance(name, int):
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
        match = data.get('match', {})
        if not match:
            raise SpecError("source {!r} needs a 'match' block".format(key))
        source = cls(
            key=key,
            vendor=_hex(match.get('vendor'), 'source.match.vendor'),
            product=_hex(match.get('product'), 'source.match.product'),
            name=match.get('name'),
            phys=match.get('phys'),
            grab=bool(data.get('grab', True)),
            hide=bool(data.get('hide', True)),
            required=bool(data.get('required', True)),
        )
        for pattern in (source.name, source.phys):
            if pattern is not None:
                try:
                    re.compile(pattern)
                except re.error as e:
                    raise SpecError("source {!r}: bad pattern {!r}: {}".format(key, pattern, e))
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
        """device: evdev.InputDevice"""
        info = device.info
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
        except TypeError as e:
            raise SpecError("bad abs info: {}".format(e))
        if spec.max <= spec.min:
            raise SpecError("abs max must be greater than min")
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
    invert: bool = False
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
        from_type, from_code = code_from_name(data['from'])
        to_type, to_code = code_from_name(data['to'], from_type if isinstance(data['to'], (int, str)) and
                                          re.match(r'^(0x[0-9a-fA-F]+|[0-9]+)$', str(data['to'])) else None)
        if from_type not in (ecodes.EV_ABS, ecodes.EV_KEY) or to_type not in (ecodes.EV_ABS, ecodes.EV_KEY):
            raise SpecError("mapping {!r}: only ABS and KEY events can be mapped".format(data['from']))
        when = data.get('when')
        if from_type == ecodes.EV_ABS and to_type == ecodes.EV_KEY:
            if not when or not any(k in when for k in ('gt', 'lt')):
                raise SpecError("mapping {!r}: ABS -> KEY needs 'when': {{\"gt\"|\"lt\": value}}".format(data['from']))
        deadzone = float(data.get('deadzone', 0))
        if not 0 <= deadzone < 1:
            raise SpecError("mapping {!r}: deadzone must be within [0, 1)".format(data['from']))
        return cls(source=source, from_type=from_type, from_code=from_code, to_type=to_type,
                   to_code=to_code, invert=bool(data.get('invert', False)),
                   raw=bool(data.get('raw', False)), deadzone=deadzone, when=when)

    def to_dict(self):
        data = {'source': self.source, 'from': code_name(self.from_type, self.from_code),
                'to': code_name(self.to_type, self.to_code)}
        if self.invert:
            data['invert'] = True
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
        if not isinstance(data, dict):
            raise SpecError("spec must be a JSON object")
        pid = data.get('id')
        if not pid or not ID_RE.match(pid):
            raise SpecError("id must be lowercase letters, digits, '.', '_' or '-'")
        if 'identity' not in data:
            raise SpecError("identity is required")
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
            description=data.get('description', ''),
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
