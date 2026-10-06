"""Summarise validate.py's output for docs/coach-diagnosis.md: the distribution of diagnoses, the old advice against
the new on chosen examples, and the sanity checks.

    python3 research/diagnosis/report.py OUT_PREFIX      (reads OUT_PREFIX-coach.json, -pairs.json, -pb.json)
"""

import json
import sys
from collections import Counter

KMH = 3.6


def union(prefix):
    seen, out = set(), []
    for mode in ('coach', 'pairs', 'pb'):
        for x in json.load(open('{}-{}.json'.format(prefix, mode))):
            k = (x['run'], x['ref_run'], x['grid'])
            if k not in seen:
                seen.add(k)
                out.append(x)
    return out


def main(prefix):
    xs = union(prefix)
    lost = [x for x in xs if x['loss'] >= 0.1]
    print('## sections compared: {} ({} losing >= 0.1 s, {} gaining, {} within 0.1 s)'.format(
        len(xs), len(lost), sum(1 for x in xs if x['loss'] <= -0.1), sum(1 for x in xs if abs(x['loss']) < 0.1)))
    for stage in sorted({x['stage'] for x in xs}):
        c = Counter(x['diag'] for x in xs if x['stage'] == stage)
        print(stage, dict(c.most_common()))
    print('all', dict(Counter(x['diag'] for x in xs).most_common()))
    print('\n## losing sections: old pattern -> new diagnosis')
    tab = Counter((x['old_pattern'] or 'none', x['diag']) for x in lost)
    for (o, n), k in sorted(tab.items(), key=lambda kv: (kv[0][0], -kv[1])):
        print('  {:16s} -> {:13s} {}'.format(o, n, k))
    print('\n## confidence of the new diagnoses (losing sections)')
    print(dict(Counter((x['diag'], x['confidence']) for x in lost)))
    # the old rules' contradictions
    od = [x for x in lost if x['old_pattern'] == 'overdriven']
    od_low = [x for x in od if x['m']['min_dv'] <= -3]
    od_left = [x for x in od_low if x['m']['grip_x'] is not None and x['m']['grip_x'] < 0.85]
    print('\n## old "overdriven" (brake earlier / enter slower): {}; of them with a lower minimum: {}; with grip left '
          '(< 0.85) at that lower minimum: {}'.format(len(od), len(od_low), len(od_left)))
    flips = [x for x in xs if x['old_brake'] is not None and x['m']['onset_dd'] is not None]
    sign = [x for x in flips if abs(x['old_brake']) >= 10 and abs(x['m']['onset_dd']) >= 10
            and (x['old_brake'] > 0) == (x['m']['onset_dd'] > 0)]          # old: + earlier; new: + later
    far = [x for x in flips if abs(-x['old_brake'] - x['m']['onset_dd']) > 20]
    print('## braking point: sections with both measures {}; old and new disagree on the side (earlier/later) {}; '
          'differ by more than 20 m {}'.format(len(flips), len(sign), len(far)))
    key_mismatch = [x for x in lost if len(x['parts']) > 1 and x['old_speed'] is not None
                    and abs(x['old_speed'] * KMH - x['m']['min_dv']) > 5]
    print('## complexes where the old "slowest point" speed differs from the diagnosed corner\'s by > 5 km/h: {} of {}'
          .format(len(key_mismatch), sum(1 for x in lost if len(x['parts']) > 1)))
    pot = [x for x in lost if x['pot_causes']]
    pe = [x for x in pot if 'entry' in x['pot_causes']]
    pe_bad = [x for x in pe if x['diag'] in ('OVER-SLOWED', 'OVERSHOT', 'SLOW-ARRIVAL', 'SLIDE', 'EXIT-BRAKE')]
    print('## potential.call "Brake later" (cause entry) on losing sections: {}; where the diagnosis says the fix is '
          'not a later braking point: {} ({})'.format(len(pe), len(pe_bad), dict(Counter(x['diag'] for x in pe_bad))))
    # follow-through
    print('\n## follow-through: the next later run quicker through the section by >= 0.1 s')
    have = [x for x in lost if x.get('next')]
    print('diagnosed losses with a later quicker pass: {}'.format(len(have)))
    for name, key in (('old', 'followed_old'), ('new', 'followed_new')):
        judged = [x for x in have if x.get(key) is not None]
        print('  {}: a fix to check in {}; the quicker pass did it in {} ({:.0f} %)'.format(
            name, len(judged), sum(1 for x in judged if x[key]),
            100.0 * sum(1 for x in judged if x[key]) / len(judged) if judged else 0))
    by = Counter()
    for x in have:
        if x.get('followed_new') is not None:
            by[(x['diag'], bool(x['followed_new']))] += 1
    print('  new by diagnosis (followed, not):', {d: (by[(d, True)], by[(d, False)]) for d in sorted({k[0] for k in by})})
    by = Counter()
    for x in have:
        if x.get('followed_old') is not None:
            by[(x['old_pattern'], bool(x['followed_old']))] += 1
    print('  old by pattern (followed, not):', {d: (by[(d, True)], by[(d, False)]) for d in sorted({k[0] for k in by})})
    # the base rate: how often any quicker pass did each fix, whatever the diagnosis said (a fix a quicker pass does
    # anyway, carrying more speed, says little; one it rarely does unprompted, braking earlier, says more)
    sys.path.insert(0, __file__.rsplit('/', 1)[0])
    import validate
    base = [x for x in have if x.get('next_m')]
    rates = {}
    for fix in sorted(set(validate.NEW_FIX.values()) | set(validate.OLD_FIX.values())):
        done = [validate.followed(fix, x['next_m']) for x in base]
        done = [d for d in done if d is not None]
        rates[fix] = '{:.0f} %'.format(100.0 * sum(done) / len(done)) if done else '-'
    print('  base rate over all {} quicker passes: {}'.format(len(base), rates))
    both = [x for x in have if x.get('followed_old') is not None and x.get('followed_new') is not None]
    print('  where both give a checkable fix ({}): old followed {}, new followed {}'.format(
        len(both), sum(1 for x in both if x['followed_old']), sum(1 for x in both if x['followed_new'])))


if __name__ == '__main__':
    main(sys.argv[1])
