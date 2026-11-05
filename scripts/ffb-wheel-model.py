#!/usr/bin/env python3
"""What the wheel delivers for a torque asked of it, from the stored calibration (read only).

    scripts/ffb-wheel-model.py [--old RUN.json.gz] [--peak NM] [--old-peak NM] [--store PATH] [--key KEY]
                               [--asked NM ...] [--weight KG SHARE ...] [--radius M]

The stored wheel_calibration.json entry (the app's config folder, Flatpak first; --store overrides; the newest entry unless --key)
gives, for each asked torque in Nm, what comes out through the shaping table and the measured curve, in Nm and percent of the
ask. --old adds a saved run's shaping table and dead zone, on the stored curve (the wheel as measured now), side by side; that
build asked against a peak of --old-peak (default 3.5) and the wheel's real peak is --peak for both. --weight KG SHARE (repeatable)
is the hanging-weight cross-check: what the curve says the motor makes at that share of full scale, net of the fit's friction,
against KG at --radius (default 0.1395 m). Full log: ~/.cache/ffb-wheel-model/latest.log
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffb_tools as tools  # noqa: E402

LOG = os.path.expanduser('~/.cache/ffb-wheel-model/latest.log')
ASKED = (.15, .25, .5, .75, 1, 1.25, 1.5, 1.75, 2, 2.5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--old', metavar='RUN.json.gz', help="a saved calibration run to set beside the stored result")
    parser.add_argument('--peak', type=float, default=tools.DEFAULT_PEAK, metavar='NM', help="the wheel's real peak torque (default 3.08)")
    parser.add_argument('--old-peak', type=float, default=3.5, metavar='NM', help="the peak the old build assumed when asking (default 3.5)")
    parser.add_argument('--store', metavar='PATH', help="the calibration store (default: wheel_calibration.json in the config folder)")
    parser.add_argument('--key', help="the store entry (default: the newest)")
    parser.add_argument('--asked', type=float, nargs='+', default=ASKED, metavar='NM')
    parser.add_argument('--weight', type=float, nargs=2, action='append', default=[], metavar=('KG', 'SHARE'))
    parser.add_argument('--radius', type=float, default=tools.DEFAULT_RADIUS, metavar='M')
    args = parser.parse_args(argv)
    try:
        key, new = tools.stored_result(args.store, args.key)
        old = tools.load_old_result(args.old) if args.old else None
    except (OSError, ValueError) as e:
        print('ffb-wheel-model: %s' % e, file=sys.stderr)
        return 2
    lines = ['store entry %s (result dated %s), peak %.2f Nm' % (key, new.get('date'), args.peak)]
    lines += tools.delivered_table(args.asked, new, args.peak, old, args.old_peak)
    for kg, share in args.weight:
        motor, load = tools.weight_check(kg, share, new, args.peak, args.radius)
        lines.append('%.2f kg at %.4f m (%.2f Nm): the curve at share %.2f, less friction, makes %.2f Nm' % (kg, args.radius, load, share, motor))
    text = '\n'.join(lines)
    print(text)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'w') as f:
        f.write(text + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
