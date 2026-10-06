#!/usr/bin/env python3
"""The game's stage clock (bridge v4, ACR) as the timing truth, and the
exact start and finish along the spline from where it starts and stops.

For every run of a v4 capture: the spline distance where the clock starts
running (the start line) and where it stops with the car still moving
(the flying finish), the run's time on the game clock against the
receive clock and against the distance-based timing the research and the
coach use (start line to the finish note), and the finish against the
'Finish' pace note and the stage table's finish_m / pacenote_last_m.

    python3 research/extrapolation/clock_finish.py
"""
import json
import os

import numpy as np

import common
import corners as corner_mod


def table(track):
    with open(os.path.join(common.ROOT, 'data/telemetry/stages/acr.json')) as f:
        for s in json.load(f)['stages']:
            if s['track'][:31] == track:
                return s
    return {}


def main():
    out = []
    for p in common.capture_paths():
        cap = common.load_cached(p)
        st = cap['stage_time']
        if not np.isfinite(st).any():
            continue
        for idx in common.runs(cap):
            t, d, v, c = cap['t'][idx], cap['lap_distance'][idx], cap['speed'][idx], st[idx]
            track = cap['track'][idx[len(idx) // 2]]
            run_ = np.flatnonzero(np.r_[False, np.diff(c) > 0])
            if not len(run_):
                continue
            i0, i1 = run_[0] - 1, run_[-1]
            stopped_moving = v[min(len(v) - 1, i1 + 5)] > 5
            row = {'capture': os.path.basename(p), 'track': track, 'car': cap['car'][idx[0]],
                   'start_d': round(float(d[max(i0, 0)]), 1), 'clock_stop_d': round(float(d[i1]), 1),
                   'clock_stopped_while_moving': bool(stopped_moving and i1 < len(idx) - 10),
                   'game_time_s': round(float(c[i1]), 2),
                   'receive_clock_s': round(float(t[i1] - t[max(i0, 0)]), 2)}
            row['receive_minus_game_s'] = round(row['receive_clock_s'] - row['game_time_s'], 2)
            stage = corner_mod.STAGES.get(track)
            notes = corner_mod.read_notes(stage) if stage else None
            fin = next((dd for dd, toks in notes or [] if 'Finish' in toks), None)
            tab = table(track)
            row['finish_note_d'] = fin
            row['table_finish_m'] = tab.get('finish_m')
            row['table_pacenote_last_m'] = tab.get('pacenote_last_m')
            if row['clock_stopped_while_moving'] and fin:
                row['clock_stop_minus_finish_note_m'] = round(row['clock_stop_d'] - fin, 1)
                # the distance-based timing from the start line to the finish note
                keep = np.r_[True, np.diff(d) > 0.01]
                row['distance_timing_s'] = round(float(np.interp(fin, d[keep], t[keep]) -
                                                       np.interp(row['start_d'], d[keep], t[keep])), 2)
            # pauses: receive-clock gaps
            gaps = np.diff(t[i0:i1 + 1])
            row['receive_gaps_over_0.5s'] = int((gaps > 0.5).sum())
            out.append(row)
    with open(os.path.join(common.OUT, 'clock_finish.json'), 'w') as f:
        json.dump(out, f, indent=1)
    for r in out:
        print(r)


if __name__ == '__main__':
    main()
