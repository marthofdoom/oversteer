#!/usr/bin/env python3
"""Show the raw events a device emits, by name or USB id.

    sudo scripts/probe-device.py 044f:b660        # by USB id
    sudo scripts/probe-device.py shifter          # by name fragment
    sudo scripts/probe-device.py --list

Needed when a device is hidden from normal users (a proxy is presenting it)
or grabbed by the proxy daemon: it names every key and axis event so a new
control (a sequential plate, an extra pedal, a wheel's dial) can be mapped.
Pass --stop-proxy to stop oversteer-proxy.service for the duration and
start it again on exit.
"""
import argparse
import os
import select
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from evdev import InputDevice, categorize, ecodes, list_devices  # noqa: E402

KEY_NAMES = {v: (k[0] if isinstance(k, list) else k) for v, k in ecodes.keys.items()}


def describe(dev):
    usb = ''
    try:
        with open('/sys/class/input/{}/device/id/vendor'.format(os.path.basename(dev.path))) as f:
            vendor = f.read().strip()
        with open('/sys/class/input/{}/device/id/product'.format(os.path.basename(dev.path))) as f:
            usb = '{}:{}'.format(vendor, f.read().strip())
    except OSError:
        pass
    return '{:<20} {:<40} {}'.format(dev.path, dev.name[:40], usb)


def find(devices, needle):
    needle = needle.lower()
    for dev in devices:
        if needle in dev.name.lower() or needle in describe(dev).lower():
            return dev
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('device', nargs='?', help='USB id (vvvv:pppp) or part of the device name')
    parser.add_argument('--list', action='store_true', help='list the devices this user can open')
    parser.add_argument('--stop-proxy', action='store_true', help='stop oversteer-proxy.service while probing')
    parser.add_argument('--seconds', type=float, default=0, help='stop after this many seconds')
    args = parser.parse_args()

    devices = []
    for path in sorted(list_devices(), key=lambda p: int(p.rsplit('event', 1)[-1])):
        try:
            devices.append(InputDevice(path))
        except OSError:
            pass
    if args.list or not args.device:
        for dev in devices:
            print(describe(dev))
        if not args.device:
            print("\nPick one: {} <usb id or name fragment>".format(sys.argv[0]), file=sys.stderr)
        return 0

    dev = find(devices, args.device)
    if dev is None:
        print("no device matches {!r} (run --list; hidden devices need root)".format(args.device), file=sys.stderr)
        return 1

    stopped = False
    if args.stop_proxy:
        subprocess.run(['systemctl', 'stop', 'oversteer-proxy.service'], check=False)
        stopped = True
        time.sleep(0.5)
        dev = find([InputDevice(p) for p in list_devices()], args.device) or dev

    print("watching {}".format(describe(dev)))
    print("buttons: {}".format(sorted(dev.capabilities().get(ecodes.EV_KEY, []))))
    print("press/move the control; Ctrl-C to stop\n")
    seen_keys, seen_abs = {}, {}
    deadline = time.monotonic() + args.seconds if args.seconds else None
    axes = (ecodes.ABS_X, ecodes.ABS_Y, ecodes.ABS_Z, ecodes.ABS_RX, ecodes.ABS_RY, ecodes.ABS_RZ,
            ecodes.ABS_THROTTLE, ecodes.ABS_RUDDER, ecodes.ABS_HAT0X, ecodes.ABS_HAT0Y)
    try:
        while deadline is None or time.monotonic() < deadline:
            # select, not read_loop: a quiet device must not block past the deadline
            timeout = 0.5 if deadline is None else max(0.0, min(0.5, deadline - time.monotonic()))
            if not select.select([dev.fd], [], [], timeout)[0]:
                continue
            for event in dev.read():
                if event.type == ecodes.EV_KEY:
                    name = KEY_NAMES.get(event.code, '?')
                    seen_keys[event.code] = name
                    print("KEY  code {:<5} {:<24} {}".format(event.code, name, 'down' if event.value else 'up'), flush=True)
                elif event.type == ecodes.EV_ABS and event.code in axes:
                    name = ecodes.ABS[event.code]
                    if seen_abs.get(event.code) != event.value:
                        seen_abs[event.code] = event.value
                        print("ABS  code {:<5} {:<24} {}".format(event.code, name, event.value), flush=True)
    except KeyboardInterrupt:
        pass
    except OSError as e:
        print("read failed: {}".format(e), file=sys.stderr)
    finally:
        if stopped:
            subprocess.run(['systemctl', 'start', 'oversteer-proxy.service'], check=False)
            print("\noversteer-proxy.service started again")
    if seen_keys:
        print("\nbuttons seen: " + ", ".join("{} ({})".format(c, n) for c, n in sorted(seen_keys.items())))
    else:
        print("\nno events seen — if a proxy holds this device, re-run with --stop-proxy")
    return 0


if __name__ == '__main__':
    sys.exit(main())
