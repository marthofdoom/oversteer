"""Issue hunt 2026-10: the automated anomaly sweep over a replay_hunt.py output folder.

    python3 research/issue-hunt/sweep.py OUT_DIR

Reads OUT_DIR/runs.jsonl (and OUT_DIR/hunt.db through its stored rows only via the JSON) and prints every check
with the runs that fail it:
  timing    finish detection against the game clock's last step, run end after the finish (coach latency),
            result against the clock's stop (a progress finish before the line, a clock that stood mid-stage)
  phantom   short runs started at the end of a stage just finished (a new stage key at the finish's z)
  splits    sector sums against the result, PB / potential ordering (user layer slower than the run itself)
  tips      tips about another stage than the run just driven, the same tip repeated verbatim, raw stage keys,
            arithmetic in "carry N km/h more (the grip allows A, you took B)", "from X.Y N days ago", "last 1 session",
            implausible top-3 seconds, spins at walking pace, a slowest point faster than the entry
"""
import collections
import json
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from oversteer import stage_tables  # noqa: E402


def load(out_dir):
    return [json.loads(line) for line in open(os.path.join(out_dir, 'runs.jsonl'))]


def timing(runs):
    print('== timing')
    ends = []
    for r in runs:
        if r.get('dropped') or not r.get('finish_detect_t'):
            continue
        det, clk = r['finish_detect_t'], r.get('clock_last_t')
        end_after = r['end_t'] - det
        if r['game'] == 'acr' and r['reason'] == 'finish':
            ends.append(end_after)
        res, stop = r.get('result_time'), r.get('clock_last_st')
        short = (stop - res) if res is not None and stop is not None and r.get('result_clock') != 'line' else None
        flags = []
        if short is not None and short > 0.05:
            flags.append('result %.2f s short of the clock stop' % short)
        if clk is not None and det < clk - 0.5:
            flags.append('finish declared %.1f s before the clock last moved' % (clk - det))
        if r.get('clock_stops'):
            flags.append('clock stood >= 0.5 s while moving %d times' % len(r['clock_stops']))
        if flags:
            print('  run %s %s %s: %s' % (r['run_id'], r['capture'][:15], r.get('stage'), '; '.join(flags)))
    if ends:
        print('  ACR finished runs: run end %.1f s (median) after the finish, %d of %d ended by FINISH_GRACE' % (
            statistics.median(ends), sum(1 for e in ends if e >= 14.9), len(ends)))


def phantom(runs):
    print('== phantom runs after a finish')
    last_finished = {}
    for r in runs:
        if r.get('dropped'):
            continue
        prev = last_finished.get(r['capture'])
        if prev is not None and r.get('finished') != 1 and (r.get('distance') or 0) < 200 \
                and r.get('start_ld') and prev.get('max_ld') and r['start_ld'] >= 0.98 * prev['max_ld']:
            print('  run %s starts at %.0f m (the end of run %s): stage %s' % (r['run_id'], r['start_ld'],
                                                                         prev['run_id'], r.get('stage')))
        if r.get('finished') == 1:
            last_finished[r['capture']] = r


def splits(runs):
    print('== splits')
    for r in runs:
        sp = r.get('splits')
        if r.get('finished') != 1 or not sp:
            continue
        rows = sp['splits']
        if all(x['last'] is not None for x in rows) and sp['unit'] == 'sectors':
            s = sum(x['last'] for x in rows)
            if abs(s - r['result_time']) > 0.05:
                print('  run %s: sectors sum %.2f against result %.2f' % (r['run_id'], s, r['result_time']))
        pot = sp.get('potential') or {}
        if pot.get('user') and r['result_time'] < pot['user'] - 0.5:
            print('  run %s %s: potential user layer %.1f slower than this run %.2f (class %s; PB %s)' % (
                r['run_id'], sp['name'], pot['user'], r['result_time'], r['run_class'], sp.get('best')))
        if pot.get('grip') and pot.get('user') and pot['grip'] < 0.75 * pot['user']:
            print('  run %s %s: grip layer %.1f is %.0f %% under the user layer %.1f' % (
                r['run_id'], sp['name'], pot['grip'], 100 * (1 - pot['grip'] / pot['user']), pot['user']))


STAGE_RE = re.compile(r'^(?:Still: )?On ([^,]+),')


def tips(runs):
    print('== tips')
    names = {}
    for r in runs:
        sp = r.get('splits')
        if sp and r.get('stage'):
            names[r['stage']] = sp['name']
        elif r.get('stage') and r['stage'] not in names:
            names[r['stage']] = (stage_tables.entry(r['stage']) or {}).get('stage')
    count = collections.Counter()
    other = collections.Counter()
    seen = set()
    for r in runs:
        here = names.get(r.get('stage'))
        for t in r.get('tips', []):
            text = t['text']
            count[text] += 1
            m = STAGE_RE.match(text)
            if m and here and m.group(1) != here and t['kind'] != 'still':
                other[r['run_id']] += 1
            if text in seen:
                continue
            seen.add(text)
            for g in re.finditer(r'carry (\d+) km/h more to the apex \(the grip allows (\d+), you took (\d+)\)', text):
                z, a, b = map(int, g.groups())
                if z != a - b:
                    print('  arithmetic, run %s: %s' % (r['run_id'], g.group(0)))
            if re.search(r'stage [a-z]+:', text):
                print('  raw stage key, run %s: %s' % (r['run_id'], text[:90]))
            if re.search(r'from \d+\.\d \d', text) or 'last 1 session' in text:
                print('  wording, run %s: %s' % (r['run_id'], text[:110]))
            g = re.search(r'about (\d+\.\d) s is there', text)
            if g and float(g.group(1)) >= 5.0:
                print('  top-3 seconds, run %s: %s' % (r['run_id'], text[:110]))
            g = re.search(r'came in at (\d+) km/h and were down to (\d+) km/h', text)
            if g and (int(g.group(1)) < 20 or int(g.group(2)) > int(g.group(1))):
                print('  spin at walking pace / slower entry than apex, run %s: %s' % (r['run_id'], text[:120]))
            g = re.search(r'car turned (\d+) degrees', text)
            if g and int(g.group(1)) > 360:
                print('  spin over a full turn, run %s: %s' % (r['run_id'], text[:100]))
    print('  runs whose tips (not still) are about another stage: %d (e.g. %s)' % (
        len(other), ', '.join('%s:%d' % kv for kv in list(other.items())[:12])))
    for text, n in count.most_common(5):
        print('  shown after %d runs: %s' % (n, text[:110]))


def main(out_dir):
    runs = load(out_dir)
    timing(runs)
    phantom(runs)
    splits(runs)
    tips(runs)


if __name__ == '__main__':
    main(os.path.expanduser(sys.argv[1]))
