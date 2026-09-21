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
import stat
import subprocess
import sys

from .manager import HIDE_RULES_FILE, HIDE_TAG_RULES_FILE, SYSTEM_DIR, BUILTIN_DIR, load_specs, hide_rules, user_dir

UNIT_FILE = '/etc/systemd/system/oversteer-proxy.service'
UNIT_TEMPLATE = """[Unit]
Description=Oversteer proxy devices (virtual wheels, shifters, handbrakes)
After=systemd-udev-settle.service
Wants=systemd-udev-settle.service

[Service]
Type=simple
{environment}ExecStart={exec_start}
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


def _systemd_quote(arg):
    """Quote one argument for ExecStart= / Environment= (systemd.syntax)."""
    return '"' + arg.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def _unsafe_path(path):
    """Why root must not execute `path`: a component that isn't owned by
    root or that the group/others can write. None when the path is safe."""
    path = os.path.realpath(path)
    parts = path.split(os.sep)
    for i in range(1, len(parts) + 1):
        component = os.sep.join(parts[:i]) or os.sep
        try:
            st = os.stat(component)
        except OSError as e:
            return "{}: {}".format(component, e)
        if st.st_uid != 0:
            return "{} is owned by uid {}, not root".format(component, st.st_uid)
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not (st.st_mode & stat.S_ISVTX and stat.S_ISDIR(st.st_mode)):
            return "{} is writable by group or others".format(component)
    return None


def _check_root_code(exec_argv, package_dir):
    """Refuse to run user-writable code as root: the interpreter, the
    script and the Python package the service imports must all be root
    owned and not group/world writable."""
    problems = []
    for path in [exec_argv[0], exec_argv[1] if len(exec_argv) > 1 else None, package_dir]:
        if path:
            why = _unsafe_path(path)
            if why:
                problems.append(why)
    return problems


def _run(cmd):
    subprocess.run(cmd, check=False)


def _rules_for(specs):
    text = hide_rules(specs)
    return text if any(l.startswith('SUBSYSTEM') for l in text.splitlines()) else None


def _reapply_permissions(specs):
    """Apply the new rules to devices that are already plugged in."""
    _run(['udevadm', 'control', '--reload-rules'])
    _run(['udevadm', 'trigger', '--subsystem-match=input', '--action=change'])
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


def _environment_lines():
    """Environment the daemon needs when Oversteer runs from a meson build
    directory instead of an installed copy."""
    lines = []
    for var in ('MESON_BUILD_ROOT', 'MESON_SOURCE_ROOT'):
        if os.environ.get(var):
            lines.append('Environment={}\n'.format(_systemd_quote('{}={}'.format(var, os.environ[var]))))
    return ''.join(lines)


def install(exec_argv, spec_dirs=None, allow_unsafe=False):
    """Install the hide rules, the system specs and the service.
    `exec_argv` is the daemon command as a list (interpreter, script,
    args). Runs via pkexec when not root. Returns the process return code."""
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not _root():
        problems = _check_root_code(exec_argv, package_dir)
        if problems and not allow_unsafe:
            print("Refusing to run Oversteer as a system service from a location root can't trust:", file=sys.stderr)
            for why in problems:
                print("  " + why, file=sys.stderr)
            print("Install Oversteer system-wide (ninja -C build install as root, prefix /usr or /usr/local)\n"
                  "or, for development only, pass --unsafe-dev-tree.", file=sys.stderr)
            return 3
        cmd = ['pkexec', 'env', 'PYTHONPATH=' + os.pathsep.join(p for p in [os.path.dirname(package_dir), os.environ.get('PYTHONPATH', '')] if p)]
        for var in ('MESON_BUILD_ROOT', 'MESON_SOURCE_ROOT'):
            if os.environ.get(var):
                cmd.append('{}={}'.format(var, os.environ[var]))
        cmd += [sys.executable, '-m', 'oversteer.proxy.install', 'install']
        if allow_unsafe:
            cmd.append('--unsafe-dev-tree')
        cmd += ['--exec'] + list(exec_argv) + ['--specs'] + list(spec_dirs or [user_dir()])
        try:
            return subprocess.call(cmd)
        except FileNotFoundError:
            print("pkexec is not available; run as root: {}".format(' '.join(cmd[1:])), file=sys.stderr)
            return 127

    os.umask(0o022)
    problems = _check_root_code(exec_argv, package_dir)
    if problems and not allow_unsafe:
        for why in problems:
            print("refusing: " + why, file=sys.stderr)
        return 3

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

    exec_start = ' '.join(_systemd_quote(a) for a in exec_argv)
    with open(UNIT_FILE, 'w') as f:
        f.write(UNIT_TEMPLATE.format(exec_start=exec_start, environment=_environment_lines()))
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
    """Remove the service, the hide rules and the system specs."""
    if not _root():
        pythonpath = os.pathsep.join(p for p in [os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), os.environ.get('PYTHONPATH', '')] if p)
        try:
            return subprocess.call(['pkexec', 'env', 'PYTHONPATH=' + pythonpath, sys.executable, '-m', 'oversteer.proxy.install', 'remove'])
        except FileNotFoundError:
            print("pkexec is not available", file=sys.stderr)
            return 127
    os.umask(0o022)
    _run(['systemctl', 'disable', '--now', 'oversteer-proxy.service'])
    for path in (UNIT_FILE, HIDE_RULES_FILE, HIDE_TAG_RULES_FILE, '/etc/udev/rules.d/90-oversteer-proxy-hide.rules'):
        if os.path.exists(path):
            os.remove(path)
    shutil.rmtree(SYSTEM_DIR, ignore_errors=True)
    _run(['systemctl', 'daemon-reload'])
    _run(['udevadm', 'control', '--reload-rules'])
    _run(['udevadm', 'trigger', '--subsystem-match=input', '--action=change'])
    print("proxy service removed; replug hidden devices to restore their permissions")
    return 0


def _parse_install(argv):
    """install [--unsafe-dev-tree] --exec <argv...> --specs <dir...>"""
    allow_unsafe = '--unsafe-dev-tree' in argv
    argv = [a for a in argv if a != '--unsafe-dev-tree']
    try:
        exec_at = argv.index('--exec')
        specs_at = argv.index('--specs')
    except ValueError:
        return None
    return argv[exec_at + 1:specs_at], argv[specs_at + 1:], allow_unsafe


if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == 'install':
        parsed = _parse_install(sys.argv[2:])
        if parsed:
            exec_argv, spec_dirs, allow_unsafe = parsed
            sys.exit(install(exec_argv, spec_dirs, allow_unsafe))
    if len(sys.argv) >= 2 and sys.argv[1] == 'remove':
        sys.exit(remove())
    print("usage: python3 -m oversteer.proxy.install install [--unsafe-dev-tree] --exec <cmd...> --specs <dir...> | remove", file=sys.stderr)
    sys.exit(2)
