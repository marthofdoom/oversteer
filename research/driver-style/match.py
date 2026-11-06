"""Nearest rally-driver archetype to marth's profile (for fun).

Axes are 0..1. marth's values are mapped from the measured numbers (profile.json) by the anchors below; the
drivers' values are a reading of public descriptions, NOT their telemetry. Each driver trait carries a tag:
E = well established (repeated across interviews/biographies/commentary), R = my reading/extrapolation.
"""

import math

AXES = ['lfb', 'trail', 'sideways', 'flick', 'throttle', 'smooth', 'commit', 'consistent', 'risk', 'late_brake']

# marth, from profile.json (anchors in comments; weight = how far the measure can be trusted)
ME = {
    'lfb': 0.80,         # 45 % of braking time with throttle > 10 %, 16 % sustained both > 20 %; throttle already on at 99 % of apexes
    'trail': 0.70,       # brake still on at turn-in in 65 % of braked corners; 60 % of turn-in -> slowest point under brake
    'sideways': 0.65,    # gravel |slip angle| in turns median 5.9 deg, p90 18.8 deg; tarmac 1.6 / 4.6 deg
    'flick': 0.25,       # strict pendulum before isolated corners 23 % vs 15 % on random straights: barely above noise
    'throttle': 0.70,    # on at/before the slowest point 99 %, full 0.5 s after it, 41 % of moving time flat
    'smooth': 0.50,      # steering rate p50 0.22, p90 1.04 lock/s (60 Hz): no outside benchmark, so the middle
    'commit': 0.50,      # grip used at the apex median 0.69 (>= 0.92 in 6 %), 87 % of the grip layer's pace
    'consistent': 0.75,  # same sitting, same car: stage-time range 0.7-4.0 s (SD 0.4-2.0 s) on 3-4 min stages
    'risk': 0.55,        # 1.0 off per 10 km over finished runs, 14 of 37 finished runs had an off, 107 restarts
    'late_brake': 0.40,  # braking starts a median 8 m before the grip layer's point; 45 % > 10 m early, 24 % late
}
WEIGHT = {'flick': 0.5, 'smooth': 0.5}

# name: {axis: (value, tag)}, source type
D = {
    'Colin McRae': ({'lfb': (.6, 'R'), 'trail': (.6, 'R'), 'sideways': (.9, 'E'), 'flick': (.7, 'R'),
                     'throttle': (.95, 'E'), 'smooth': (.25, 'R'), 'commit': (.95, 'E'), 'consistent': (.3, 'E'),
                     'risk': (.95, 'E'), 'late_brake': (.8, 'R')}, 'biographies, contemporary commentary'),
    'Sébastien Loeb': ({'lfb': (.8, 'R'), 'trail': (.7, 'R'), 'sideways': (.35, 'E'), 'flick': (.3, 'R'),
                        'throttle': (.7, 'R'), 'smooth': (.95, 'E'), 'commit': (.8, 'R'), 'consistent': (.95, 'E'),
                        'risk': (.15, 'E'), 'late_brake': (.6, 'R')}, 'commentary, interviews, results record'),
    'Sébastien Ogier': ({'lfb': (.8, 'R'), 'trail': (.7, 'R'), 'sideways': (.35, 'R'), 'flick': (.35, 'R'),
                         'throttle': (.7, 'R'), 'smooth': (.85, 'E'), 'commit': (.75, 'E'), 'consistent': (.95, 'E'),
                         'risk': (.2, 'E'), 'late_brake': (.6, 'R')}, 'interviews, commentary'),
    'Tommi Mäkinen': ({'lfb': (.9, 'E'), 'trail': (.8, 'R'), 'sideways': (.5, 'R'), 'flick': (.6, 'R'),
                       'throttle': (.85, 'R'), 'smooth': (.75, 'R'), 'commit': (.9, 'E'), 'consistent': (.85, 'E'),
                       'risk': (.4, 'R'), 'late_brake': (.75, 'R')}, 'commentary, technical features'),
    'Kalle Rovanperä': ({'lfb': (.9, 'E'), 'trail': (.8, 'R'), 'sideways': (.55, 'R'), 'flick': (.7, 'R'),
                         'throttle': (.9, 'R'), 'smooth': (.8, 'R'), 'commit': (.95, 'E'), 'consistent': (.85, 'E'),
                         'risk': (.35, 'R'), 'late_brake': (.8, 'R')}, 'onboard commentary, interviews'),
    'Petter Solberg': ({'lfb': (.6, 'R'), 'trail': (.6, 'R'), 'sideways': (.95, 'E'), 'flick': (.75, 'R'),
                        'throttle': (.9, 'R'), 'smooth': (.3, 'R'), 'commit': (.9, 'E'), 'consistent': (.45, 'R'),
                        'risk': (.8, 'E'), 'late_brake': (.7, 'R')}, 'commentary, interviews'),
    'Walter Röhrl': ({'lfb': (.3, 'R'), 'trail': (.6, 'R'), 'sideways': (.3, 'E'), 'flick': (.3, 'R'),
                      'throttle': (.6, 'R'), 'smooth': (.95, 'E'), 'commit': (.7, 'R'), 'consistent': (.9, 'E'),
                      'risk': (.15, 'E'), 'late_brake': (.5, 'R')}, 'interviews, biography'),
    'Marcus Grönholm': ({'lfb': (.8, 'R'), 'trail': (.7, 'R'), 'sideways': (.6, 'R'), 'flick': (.6, 'R'),
                         'throttle': (.9, 'R'), 'smooth': (.6, 'R'), 'commit': (.95, 'E'), 'consistent': (.7, 'R'),
                         'risk': (.55, 'R'), 'late_brake': (.8, 'R')}, 'commentary'),
    'Ari Vatanen': ({'lfb': (.6, 'R'), 'trail': (.6, 'R'), 'sideways': (.85, 'E'), 'flick': (.75, 'R'),
                     'throttle': (.95, 'E'), 'smooth': (.35, 'R'), 'commit': (.95, 'E'), 'consistent': (.4, 'E'),
                     'risk': (.85, 'E'), 'late_brake': (.8, 'R')}, 'biography, commentary'),
    'Michèle Mouton': ({'lfb': (.6, 'R'), 'trail': (.6, 'R'), 'sideways': (.6, 'R'), 'flick': (.5, 'R'),
                        'throttle': (.9, 'R'), 'smooth': (.5, 'R'), 'commit': (.9, 'E'), 'consistent': (.65, 'R'),
                        'risk': (.6, 'R'), 'late_brake': (.7, 'R')}, 'interviews, documentaries'),
}


def dist(v):
    return math.sqrt(sum(WEIGHT.get(a, 1.0) * (ME[a] - v[a][0]) ** 2 for a in AXES) / sum(WEIGHT.get(a, 1.0) for a in AXES))


ranked = sorted(D, key=lambda n: dist(D[n][0]))
for n in ranked:
    v = D[n][0]
    diffs = sorted(AXES, key=lambda a: abs(ME[a] - v[a][0]))
    print('{:18s} d={:.3f} shared: {}  biggest diff: {} (me {:.2f} vs {:.2f})'.format(
        n, dist(v), ', '.join(diffs[:3]), diffs[-1], ME[diffs[-1]], v[diffs[-1]][0]))
