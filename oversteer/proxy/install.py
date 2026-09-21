"""Install / remove the system side of the proxies: the udev hide rules,
the system copy of the enabled specs and the ``oversteer-proxy`` service.

Everything here needs root. ``install()`` re-executes itself through pkexec
when called as a normal user, the same way Oversteer installs its udev
rules.
"""

import glob
import json
import os
import shutil
import subprocess
import sys

from .manager import HIDE_RULES_FILE, SYSTEM_DIR, BUILTIN_DIR, load_specs, hide_rules, user_dir

UNIT_FILE = '/etc/systemd/system/oversteer-proxy.service'
UNIT_TEMPLATE = """[Unit]
Description=Oversteer proxy devices (virtual wheels, shifters, handbrakes)
After=systemd-udev-settle.service
Wants=systemd-udev-settle.service

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=3s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def _root():
    return os.geteuid() == 0


def _run(cmd):
    subprocess.run(cmd, check=False)


def _rules_for(specs):
    text = hide_rules(specs)
    return text if any(l.startswith('SUBSYSTEM') for l in text.splitlines()) else None


def _reapply_permissions(specs):
    """Apply the new rules to devices that are already plugged in."""
    _run(['udevadm', 'control', '--reload-rules'])
    _run(['udevadm', 'trigger', '--subsystem-match=input', '--action=change'])
    # logind's uaccess ACLs are not removed by a re-trigger; drop them by hand.
    try:
        import pyudev
        context = pyudev.Context()
        hidden = set()
        for spec in specs.values():
            if spec.enabled:
                for src in spec.sources.values():
                    if src.hide and src.vendor is not None and src.product is not None:
                        hidden.add(('{:04x}'.format(src.vendor), '{:04x}'.format(src.product)))
        for dev in context.list_devices(subsystem='input'):
            node = dev.device_node
            if not node:
                continue
            usb = dev.find_parent('usb', 'usb_device')
            if usb is None:
                continue
            key = (usb.attributes.asstring('idVendor'), usb.attributes.asstring('idProduct'))
            if key in hidden:
                _run(['setfacl', '-b', node])
                os.chmod(node, 0o600)
                os.chown(node, 0, 0)
    except Exception as e:  # best effort; a replug applies the rule anyway
        print("note: could not re-apply permissions to plugged devices: {}".format(e), file=sys.stderr)


def install(exec_start, spec_dirs=None):
    """Install the hide rules, the system specs and the service. Runs via
    pkexec when not root. Returns the process return code."""
    if not _root():
        cmd = ['pkexec', sys.executable, '-m', 'oversteer.proxy.install', 'install', exec_start]
        cmd += spec_dirs or [user_dir()]
        env = dict(os.environ)
        env['PYTHONPATH'] = os.pathsep.join(p for p in [os.path.dirname(os.path.dirname(os.path.dirname(__file__))), env.get('PYTHONPATH', '')] if p)
        return subprocess.call(cmd, env=env)

    dirs = [BUILTIN_DIR] + list(spec_dirs or [])
    specs, errors = load_specs(dirs)
    for path, err in errors:
        print("skipping {}: {}".format(path, err), file=sys.stderr)

    os.makedirs(SYSTEM_DIR, exist_ok=True)
    for old in glob.glob(os.path.join(SYSTEM_DIR, '*.json')):
        os.remove(old)
    for spec in specs.values():
        spec.save(os.path.join(SYSTEM_DIR, spec.id + '.json'))

    rules = _rules_for(specs)
    if rules:
        with open(HIDE_RULES_FILE, 'w') as f:
            f.write(rules)
    elif os.path.exists(HIDE_RULES_FILE):
        os.remove(HIDE_RULES_FILE)

    with open(UNIT_FILE, 'w') as f:
        f.write(UNIT_TEMPLATE.format(exec_start=exec_start))
    _run(['systemctl', 'daemon-reload'])
    if any(s.enabled for s in specs.values()):
        _run(['systemctl', 'enable', '--now', 'oversteer-proxy.service'])
        _run(['systemctl', 'restart', 'oversteer-proxy.service'])
    else:
        _run(['systemctl', 'disable', '--now', 'oversteer-proxy.service'])
    _reapply_permissions(specs)
    print("proxy service installed: {} enabled".format(sum(1 for s in specs.values() if s.enabled)))
    return 0


def remove():
    """Remove the service, the hide rules and the system specs."""
    if not _root():
        env = dict(os.environ)
        env['PYTHONPATH'] = os.pathsep.join(p for p in [os.path.dirname(os.path.dirname(os.path.dirname(__file__))), env.get('PYTHONPATH', '')] if p)
        return subprocess.call(['pkexec', sys.executable, '-m', 'oversteer.proxy.install', 'remove'], env=env)
    _run(['systemctl', 'disable', '--now', 'oversteer-proxy.service'])
    for path in (UNIT_FILE, HIDE_RULES_FILE):
        if os.path.exists(path):
            os.remove(path)
    shutil.rmtree(SYSTEM_DIR, ignore_errors=True)
    _run(['systemctl', 'daemon-reload'])
    _run(['udevadm', 'control', '--reload-rules'])
    _run(['udevadm', 'trigger', '--subsystem-match=input', '--action=change'])
    print("proxy service removed; replug hidden devices to restore their permissions")
    return 0


if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == 'install':
        sys.exit(install(sys.argv[2], sys.argv[3:]))
    if len(sys.argv) >= 2 and sys.argv[1] == 'remove':
        sys.exit(remove())
    print("usage: python3 -m oversteer.proxy.install install <exec-start> [spec-dir...] | remove", file=sys.stderr)
    sys.exit(2)
