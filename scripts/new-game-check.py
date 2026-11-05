#!/usr/bin/env python3
"""The first look at a recording of a new game: what it decoded as, which channels it carries, then today's force against ACR's.

    scripts/new-game-check.py CAPTURE [--vs REFERENCE] [--peak NM]

CAPTURE: a name in the captures folder or a full path. Prints the game it decoded as and which of the Sample channels the force
code reads are present and which are missing (that is the input to deriving the math). Where the game has a rack, runs
ffb-sim-match against REFERENCE (default the newest ACR capture) and then ffb-parity; where it has none, says so and stops after
the channels. One log: ~/.cache/new-game-check/latest.log. Reads only: no wheel, proxy, game or telemetry database is touched.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffb_situations as sit  # noqa: E402
import ffb_tools as tools  # noqa: E402

LOG = os.path.expanduser('~/.cache/new-game-check/latest.log')


def newest_of_game(game, exclude):
    """The newest capture of `game` other than `exclude`, or None."""
    import ffb_parity as fp
    seen = {}
    for folder in fp.captures_folders():
        for f in os.listdir(folder):
            if f.endswith('.ovcap.gz'):
                seen[f] = os.path.join(folder, f)
    for _name, path in sorted(seen.items(), reverse=True):
        if os.path.abspath(path) != os.path.abspath(exclude) and fp.game_of(path) == game:
            return path
    return None


def channel_lines(path, limit=4000):
    import ffb_benchmark as fb
    samples = [s for _t, s in fb._walk(path)[1]]
    if not samples:
        return ['no decodable samples'], 0
    step = max(1, len(samples) // limit)
    share, present, missing = tools.channel_report(samples[::step])
    lines = ['%d samples decoded' % len(samples),
             'present: ' + ', '.join('%s %.0f%%' % (c, share[c] * 100) for c in present),
             'MISSING: ' + (', '.join(missing) if missing else 'none')]
    return lines, len(samples)


def parity_lines(drive, peak):
    import ffb_parity as fp
    if fp.MISSING:
        return [fp.MISSING]
    ref = None
    game = fp.game_of(drive)
    for _name, path in reversed(fp.list_tuning()):
        if path != drive and fp.has_force(path) and fp.game_of(path) != game:
            ref = path
            break
    if not fp.has_force(drive):
        return ['no force recorded for this capture (no .ffb.gz sidecar with ext_force): ffb-parity needs one']
    return fp.run(drive, ref, peak)[0].split('\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('capture')
    parser.add_argument('--vs', metavar='REFERENCE', help='the capture to set the simulated force against (default: the newest ACR one)')
    parser.add_argument('--peak', type=float, metavar='NM', help="the wheel's peak torque (default 3.08)")
    args = parser.parse_args(argv)
    import ffb_parity as fp
    try:
        path = fp.resolve(args.capture)
        game = fp.game_of(path)
    except (FileNotFoundError, OSError, ValueError) as e:
        print('new-game-check: %s' % e, file=sys.stderr)
        return 2
    out = ['== %s' % os.path.basename(path), 'decoded as game: %s' % (game or 'unknown (no decodable packets)')]
    chan, _n = channel_lines(path)
    out += ['', '== channels the force code reads'] + chan
    try:
        tools.rack_for(game, os.path.basename(path)[:15])
        have_rack = True
    except tools.NoRack as e:
        out += ['', '== force', 'NO RACK: %s' % e, 'Only the channels above are reported; derive the math from them first.']
        have_rack = False
    if have_rack:
        ref = fp.resolve(args.vs) if args.vs else newest_of_game('acr', path)
        out.append('')
        if ref is None:
            out += ['== ffb-sim-match', 'no ACR capture to set it against (give one with --vs)']
        else:
            out.append('== ffb-sim-match: this capture (A) against %s (B)' % os.path.basename(ref)[:15])
            try:
                lines, rows, summary = tools.sim_match([path], [ref], peak=args.peak)
                head, body = sit.table(rows)
                out += lines + [summary, head] + body[:12]
            except (tools.NoRack, OSError, ValueError) as e:
                out.append('ffb-sim-match failed: %s' % e)
        out += ['', '== ffb-parity']
        try:
            out += parity_lines(path, args.peak)
        except (OSError, RuntimeError, ValueError) as e:
            out.append('ffb-parity failed: %s' % e)
    text = '\n'.join(out)
    print(text)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'w') as f:
        f.write(text + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
