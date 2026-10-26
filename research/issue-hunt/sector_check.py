"""Issue hunt 2026-10: every finished ACR run's game sectors, timed as coach._sectors times them.

    python3 research/issue-hunt/sector_check.py HUNT.db

Per run: the sectors timed (S1..), their sum against the result, sectors missing (a line not crossed), and where the
run's trace began against the start line.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from oversteer import coach_context, stage_tables  # noqa: E402
from oversteer.telemetry_store import open_reader  # noqa: E402


def main(db):
    r = open_reader(db)
    rows = r.db.execute("select id, stage, result_time, course, finished, run_class from runs "
                        "where stage like 'acr:%' and finished = 1 order by id").fetchall()
    for rid, stage, result, course, finished, klass in rows:
        entry = stage_tables.entry(stage)
        placed = stage_tables.sector_bounds(entry)
        trace = r.trace(rid)
        if placed is None or not trace:
            print(rid, stage, 'no sectors' if placed is None else 'no trace')
            continue
        bounds = [(a - placed['start_m'], b - placed['start_m']) for a, b in placed['bounds']]
        origin = r.run_start(rid)
        if origin is None:
            origin = stage_tables.run_origin(entry)
        shift = 0.0 if origin is None else origin - placed['start_m']
        rows_ = coach_context.stage_rows(trace, course, True, result)
        clock = r.run_clock(rid) == 'game'
        t = coach_context.sector_times(rows_, bounds, shift, clock, result)
        s = sum(t.values()) if len(t) == len(bounds) else None
        print(rid, stage.split(':')[-1][:22].ljust(22), klass.ljust(8), 'res %.2f' % result, 'clock', 'game' if clock else 'own ',
              'sectors', ' '.join('%.2f' % t[i] if i in t else '----' for i in range(len(bounds))),
              'sum-res %s' % ('%.2f' % (s - result) if s is not None else 'MISSING'), 'start shift %.1f' % shift)


if __name__ == '__main__':
    main(os.path.expanduser(sys.argv[1]))
