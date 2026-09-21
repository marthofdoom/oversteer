"""Install / remove the system side of the proxies: a root-owned copy of the
daemon, the udev hide rules, the system copy of the enabled specs and the
``oversteer-proxy`` service.

Everything here needs root. ``install()`` re-executes itself through pkexec
when called as a normal user (through ``flatpak-spawn --host`` inside a
Flatpak), the same way Oversteer installs its udev rules.

The service never executes files the user can edit: the daemon code is
copied to DAEMON_DIR (root-owned) at install time, whatever the GUI runs
from (a source tree, a system install or a Flatpak).
"""

import os
import shutil
import subprocess
import sys

from .manager import HIDE_RULES_FILE, HIDE_TAG_RULES_FILE, SYSTEM_DIR, BUILTIN_DIR, load_specs, hide_rules, user_dir

UNIT_FILE = '/etc/systemd/system/oversteer-proxy.service'
DAEMON_DIR = '/usr/local/lib/oversteer-proxy'
DAEMON_FILES = ['__init__.py', 'wheel_ids.py', 'proxy/__init__.py', 'proxy/spec.py', 'proxy/device.py',
                'proxy/uinput_ff.py', 'proxy/manager.py', 'proxy/equipment.py', 'proxy/daemon.py',
                'proxy/install.py']
HOST_PYTHON = '/usr/bin/python3'
UNIT_TEMPLATE = """[Unit]
Description=Oversteer proxy devices (virtual wheels, shifters, handbrakes)
After=systemd-udev-settle.service
Wants=systemd-udev-settle.service

[Service]
Type=simple
Environment="PYTHONPATH={daemon_dir}"
ExecStart={python} -m oversteer.proxy.daemon
Restart=on-failure
RestartSec=3s
StandardOutput=journal
StandardError=journal
# The daemon needs /dev/input and /dev/uinput, nothing else.
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RuntimeDirectory=oversteer
RuntimeDirectoryMode=0755
DevicePolicy=closed
DeviceAllow=char-input rw
DeviceAllow=/dev/uinput rw

[Install]
WantedBy=multi-user.target
"""


def _root():
    return os.geteuid() == 0


def _run(cmd):
    subprocess.run(cmd, check=False)


def _in_flatpak():
    return os.path.exists('/.flatpak-info')


def _host_path(path):
    """A path as the host sees it. Inside Flatpak the app's own files live
    under app-path on the host; the config directory is the same real path."""
    if not _in_flatpak():
        return path
    app_files = None
    try:
        with open('/.flatpak-info') as f:
            for line in f:
                if line.startswith('app-path='):
                    app_files = line.split('=', 1)[1].strip()
    except OSError:
        pass
    if app_files and path.startswith('/app/'):
        return os.path.join(app_files, path[len('/app/'):])
    return path


def _elevated(args):
    """Run this module as root with the given arguments; returns the exit code."""
    package_root = _host_path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    cmd = ['pkexec', 'env', 'PYTHONPATH=' + package_root, HOST_PYTHON, '-m', 'oversteer.proxy.install'] + args
    if _in_flatpak():
        cmd = ['flatpak-spawn', '--host'] + cmd
    try:
        return subprocess.call(cmd)
    except FileNotFoundError:
        print("pkexec is not available; run as root: {}".format(' '.join(cmd)), file=sys.stderr)
        return 127


def _host_python_ok():
    try:
        subprocess.run([HOST_PYTHON, '-c', 'import evdev, pyudev'], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def _copy_daemon(package_dir):
    """Copy the daemon subset of the package into DAEMON_DIR, root-owned."""
    target = os.path.join(DAEMON_DIR, 'oversteer')
    profiles = os.path.join(package_dir, 'proxy', 'profiles')
    files = DAEMON_FILES + ['proxy/profiles/' + f for f in os.listdir(profiles) if f.endswith('.json')]
    for sub in (DAEMON_DIR, target, os.path.join(target, 'proxy'), os.path.join(target, 'proxy', 'profiles')):
        os.makedirs(sub, mode=0o755, exist_ok=True)
        os.chmod(sub, 0o755)
        os.chown(sub, 0, 0)
    for rel in files:
        dst = os.path.join(target, rel)
        shutil.copyfile(os.path.join(package_dir, rel), dst)
        os.chmod(dst, 0o644)
        os.chown(dst, 0, 0)
    # Drop anything left from an earlier version
    for root, dirs, names in os.walk(target):
        for name in names:
            rel = os.path.relpath(os.path.join(root, name), target)
            if rel not in files:
                os.remove(os.path.join(root, name))


def _rules_for(specs):
    text = hide_rules(specs)
    return text if any(l.startswith('SUBSYSTEM') for l in text.splitlines()) else None


def _reapply_permissions(specs):
    """Apply the new rules to devices that are already plugged in."""
    _run(['udevadm', 'control', '--reload-rules'])
    _run(['udevadm', 'trigger', '--subsystem-match=input', '--subsystem-match=hidraw', '--action=change'])
    _run(['udevadm', 'settle', '--timeout=5'])
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
        for subsystem in ('input', 'hidraw'):
            for dev in context.list_devices(subsystem=subsystem):
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


def install(spec_dirs=None):
    """Install the daemon copy, the hide rules, the system specs and the
    service. Runs via pkexec when not root. Returns the exit code."""
    if not _root():
        return _elevated(['install', '--specs'] + [_host_path(d) for d in (spec_dirs or [user_dir()])])

    os.umask(0o022)
    if not _host_python_ok():
        print("The proxy service needs python3-evdev and python3-pyudev for {}.".format(HOST_PYTHON), file=sys.stderr)
        return 4
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _copy_daemon(package_dir)

    dirs = [BUILTIN_DIR] + list(spec_dirs or [])
    specs, errors = load_specs(dirs)
    for path, err in errors:
        print("skipping {}: {}".format(path, err), file=sys.stderr)

    os.makedirs(SYSTEM_DIR, mode=0o755, exist_ok=True)
    os.chmod(os.path.dirname(SYSTEM_DIR), 0o755)
    os.chmod(SYSTEM_DIR, 0o755)
    # Only replace what we installed before; leave admin-placed specs alone.
    manifest = os.path.join(SYSTEM_DIR, '.installed')
    try:
        with open(manifest) as f:
            previous = [line.strip() for line in f if line.strip()]
    except OSError:
        previous = []
    for old_id in previous:
        old = os.path.join(SYSTEM_DIR, old_id + '.json')
        if os.path.exists(old):
            os.remove(old)
    for spec in specs.values():
        path = os.path.join(SYSTEM_DIR, spec.id + '.json')
        spec.save(path)
        os.chmod(path, 0o644)
    with open(manifest, 'w') as f:
        f.write("\n".join(sorted(specs)) + "\n")
    os.chmod(manifest, 0o644)

    rules = _rules_for(specs)
    for path, text in ((HIDE_RULES_FILE, rules), (HIDE_TAG_RULES_FILE, hide_rules(specs, 'TAG-="uaccess"') if rules else None)):
        if text:
            with open(path, 'w') as f:
                f.write(text)
            os.chmod(path, 0o644)
        elif os.path.exists(path):
            os.remove(path)
    for stale in ('/etc/udev/rules.d/90-oversteer-proxy-hide.rules',):
        if os.path.exists(stale):
            os.remove(stale)

    with open(UNIT_FILE, 'w') as f:
        f.write(UNIT_TEMPLATE.format(daemon_dir=DAEMON_DIR, python=HOST_PYTHON))
    os.chmod(UNIT_FILE, 0o644)
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
    """Remove the service, the hide rules, the system specs and the daemon copy."""
    if not _root():
        return _elevated(['remove'])
    os.umask(0o022)
    _run(['systemctl', 'disable', '--now', 'oversteer-proxy.service'])
    for path in (UNIT_FILE, HIDE_RULES_FILE, HIDE_TAG_RULES_FILE, '/etc/udev/rules.d/90-oversteer-proxy-hide.rules'):
        if os.path.exists(path):
            os.remove(path)
    shutil.rmtree(SYSTEM_DIR, ignore_errors=True)
    shutil.rmtree(DAEMON_DIR, ignore_errors=True)
    _run(['systemctl', 'daemon-reload'])
    _run(['udevadm', 'control', '--reload-rules'])
    _run(['udevadm', 'trigger', '--subsystem-match=input', '--subsystem-match=hidraw', '--action=change'])
    print("proxy service removed; replug hidden devices to restore their permissions")
    return 0


if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == 'install' and sys.argv[2] == '--specs':
        sys.exit(install(sys.argv[3:]))
    if len(sys.argv) >= 2 and sys.argv[1] == 'remove':
        sys.exit(remove())
    print("usage: python3 -m oversteer.proxy.install install --specs <dir...> | remove", file=sys.stderr)
    sys.exit(2)
