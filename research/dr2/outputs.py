"""What the coach, the splits, the potential and the Run view make of DiRT Rally 2.0 runs in a scratch database
(research/dr2/replay.py's), read-only.

    python3 research/dr2/outputs.py DB [PROFILE]
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from oversteer import coach, run_analysis  # noqa: E402
from oversteer.telemetry_store import open_reader  # noqa: E402
from oversteer.telemetry_view import splits_lines  # noqa: E402


def main(db, profile='DR2'):
    reader = open_reader(db)
    cars = reader.db.execute('select id, key, name, drivetrain from cars').fetchall()
    print('cars', [tuple(c) for c in cars])
    runs = [r for r in reader.db.execute('select r.id, s.car, r.stage, r.finished, r.result_time, r.run_class from runs r '
                                         'join sessions s on s.id = r.session where r.finished = 1').fetchall()]
    for car_id in sorted({r[1] for r in runs}):
        print('\n## car', car_id)
        for t in coach.Coach(reader).tips(profile, car_id):
            print('  tip:', t.to_dict().get('text') if hasattr(t, 'to_dict') else t)
        try:
            found = coach.splits(reader, profile, car_id)
            for line in splits_lines(found) or []:
                print('  splits:', line)
        except Exception as e:                     # noqa: BLE001 - a research probe reports, it does not stop
            print('  splits failed:', repr(e))
    for run_id, car, stage, finished, result, klass in runs:
        h = run_analysis.head(reader, run_id)
        a = run_analysis.analysis(reader, run_id)
        pot = (h or {}).get('potential') or {}
        print('\n## run %d %s %s time %.2f class %s' % (run_id, stage, h and h['stage_name'], result, klass))
        print('  potential user/grip/car:', pot.get('user'), pot.get('grip'), pot.get('car'),
              'sections', len(pot.get('sections') or []))
        if a:
            print('  analysis length %.0f, sections %d, sectors %s, x %s' % (
                a['length'], len(a['sections'] or []), json.dumps(a['sectors'])[:200],
                'yes' if a['x'] and any(v is not None for v in a['x']) else 'no'))
            for s in (a['sections'] or [])[:3]:
                print('   section', {k: s.get(k) for k in ('d0', 'd1', 'loss', 'text', 'call', 'short')})


if __name__ == '__main__':
    main(*sys.argv[1:])
