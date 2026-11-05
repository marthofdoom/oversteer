#!/usr/bin/env python3
"""What saved wheel-calibration runs say: outcome and log, pulses, run-ups, odd pushes, a refit with today's fit, accel against speed.

    scripts/calib-check.py [RUN ...] [--dir PATH]

RUN: a file in the runs folder (name, with or without .json.gz) or a full path; default the newest. The folder is the app's
calibration-runs in its config folder (Flatpak first); --dir overrides. A failed or short run is reported, not a crash.
Reads only. Full log: ~/.cache/calib-check/latest.log
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffb_tools as tools  # noqa: E402

LOG = os.path.expanduser('~/.cache/calib-check/latest.log')


def resolve(name, directory):
    if os.path.isabs(name) or os.path.exists(name):
        return name
    path = os.path.join(directory, name)
    return path if os.path.exists(path) or name.endswith('.json.gz') else path + '.json.gz'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('runs', nargs='*')
    parser.add_argument('--dir', help="the calibration-runs folder")
    args = parser.parse_args(argv)
    directory = args.dir or tools.runs_dir()
    paths = [resolve(r, directory) for r in args.runs] or [tools.newest_run(directory)]
    if paths == [None]:
        print('calib-check: no calibration runs in %s' % directory, file=sys.stderr)
        return 2
    lines, datas = [], []
    for path in paths:
        try:
            data = tools.load_run_raw(path)
        except (OSError, ValueError, EOFError) as e:
            lines.append('== %s\ncannot read: %s' % (os.path.basename(path), e))
            continue
        datas.append(data)
        lines += tools.run_report(path, data) + ['']
    lines += ['== accel against speed, all runs above'] + tools.accel_lines(tools.accel_pool(datas))
    text = '\n'.join(lines)
    print(text)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'w') as f:
        f.write(text + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
