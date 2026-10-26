#!/usr/bin/env python3
"""Build a Forza Horizon 6 car table: CarOrdinal -> year, make, model, plus the
game's media name (media/Cars/<MAKE>_<Model>_<YY>.zip) worked out by matching.

Inputs (all read-only):
  ordinals.json   {"1969 Toyota 2000 GT": "247", ...}  (HDR's gist, or LapScope's
                  MIT copy app/car_ordinals.json)
  cars dir        <FH6>/media/Cars  (only the zip file names are read)
Optional:
  check.ts        a third-party ordinal->asset crosswalk, used ONLY to report
                  how often the heuristic agrees (never copied into the output)

Usage: fh6_cars.py ordinals.json <FH6>/media/Cars [check.ts] > fh6_cars.json

The media name match: same two-digit year, then the best token overlap between
the model words and the media name's model part. Ties and weak matches are left
empty (media: null) rather than guessed.
"""
import difflib
import json
import os
import re
import sys


def norm(s):
    return re.sub(r'[^a-z0-9]', '', s.lower())


def split_name(name):
    m = re.match(r'(\d{4}) (.*)$', name)
    if not m:
        return None, name
    return int(m.group(1)), m.group(2)


def main():
    ordinals = json.load(open(sys.argv[1]))
    media = sorted(n[:-4] for n in os.listdir(sys.argv[2]) if n.endswith('.zip'))
    by_year = {}
    for m in media:
        parts = m.split('_')
        if len(parts) >= 3 and parts[-1].isdigit():
            by_year.setdefault(int(parts[-1]), []).append(m)
    rows = []
    for name, ordinal in ordinals.items():
        year, rest = split_name(name)
        best, best_score, second = None, 0.0, 0.0
        for m in by_year.get(year % 100 if year else -1, []):
            model = norm(''.join(m.split('_')[1:-1]))
            score = difflib.SequenceMatcher(None, norm(rest), model).ratio()
            # Make code: the media prefix is usually the first letters of the make
            if norm(rest).startswith(norm(m.split('_')[0])[:3]):
                score += 0.3
            if score > best_score:
                best, best_score, second = m, score, best_score
            elif score > second:
                second = score
        media_name = best if best and best_score >= 0.45 and best_score - second >= 0.05 else None
        rows.append({'ordinal': int(ordinal), 'name': name, 'year': year, 'media': media_name})
    rows.sort(key=lambda r: r['ordinal'])
    if len(sys.argv) > 3:
        truth = dict((int(o), a) for o, a in re.findall(r'carOrdinal: (\d+),.*?asset: "([^"]*)"', open(sys.argv[3]).read()))
        known = [r for r in rows if r['ordinal'] in truth]
        right = sum(1 for r in known if r['media'] and r['media'].lower() == truth[r['ordinal']].lower())
        wrong = sum(1 for r in known if r['media'] and r['media'].lower() != truth[r['ordinal']].lower())
        empty = sum(1 for r in known if not r['media'])
        print(f'check: {len(known)} comparable, {right} agree, {wrong} disagree, {empty} unmatched', file=sys.stderr)
    print(f'{len(rows)} ordinals, {sum(1 for r in rows if r["media"])} with a media name, '
          f'{len(media)} media cars', file=sys.stderr)
    json.dump(rows, sys.stdout, indent=1)


if __name__ == '__main__':
    main()
