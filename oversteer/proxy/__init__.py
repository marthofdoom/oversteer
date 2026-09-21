"""Proxy devices: re-present physical input devices to games as virtual
(uinput) devices with a chosen identity, capability set and event mapping.

A proxy is described by a :class:`ProxySpec` (JSON on disk) and run by a
:class:`ProxyDevice`. The :class:`ProxyManager` loads specs, tracks hot-plug
and runs the enabled proxies, each on its own thread.
"""

from .spec import ProxySpec, SourceSpec, Mapping, AbsSpec, Identity, SpecError
from .device import ProxyDevice, ProxyState
from .manager import ProxyManager

__all__ = [
    'ProxySpec', 'SourceSpec', 'Mapping', 'AbsSpec', 'Identity', 'SpecError',
    'ProxyDevice', 'ProxyState', 'ProxyManager',
]
