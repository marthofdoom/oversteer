#!/usr/bin/env python3
"""One recording of a game, any car: its routes, its own constants, and every stage of today's force against the tyre laws.

    scripts/ffb-new-game.py CAPTURE [--out FILE.json] [--no-rack] [--peak NM] [--set NAME=VALUE ...] [--laws FILE]
    scripts/ffb-new-game.py --shape CAPTURE [CAPTURE ...]     (the grip in use against the slip, one game: ffb_forza.GRIP_USE)
    scripts/ffb-new-game.py --refit ACR_CAPTURE [ACR_CAPTURE ...] [--laws FILE]
    scripts/ffb-new-game.py --refit-rack ACR_CAPTURE [ACR_CAPTURE ...]

CAPTURE: a name in the captures folder or a full path. It (a) lists the channels and the route each force stage can take with
them, (b) fits the game's own constants from this drive (grip and peak slip per 60 s stretch, the steering lock from the motion
corrected for understeer, the ratio it gives, the mass where the tyres send forces), (c) replays today's rack for the game over
the drive and sets each stage against the axle law fitted from ACR's tyres (scripts/ffb_laws.json 'axle') at the same physical cause: the
grip level, the grip in use and the slip over the peak slip, not another recording, and flags what departs, (d) writes the
fitted constants to FILE (default ~/.cache/ffb-new-game/<capture>.json). A game without a rack gets (a), (b) and the list of
what the engine still needs. --no-rack treats a known game that way. --refit rebuilds the laws from ACR drives.
Log: ~/.cache/ffb-new-game/latest.log. Reads only: no wheel, proxy, game or telemetry database is touched.
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ffb_laws as laws_mod  # noqa: E402
import ffb_tools as tools  # noqa: E402

CACHE = os.path.expanduser('~/.cache/ffb-new-game')
LOG = os.path.join(CACHE, 'latest.log')


def _load(path):
    import ffb_benchmark as fb
    return list(fb._walk(path)[1])


def _simulate(path, game, peak=None):
    """(cols, causes, info, in_force lines) of today's rack over the capture; inside the sandbox."""
    sim = tools.simulate_stages(_load(path), game, tools.profile_power_class(game), peak, probe=(laws_mod.PROBE_KEYS, laws_mod.probe))
    bins = tools.TYRE_BINS['norm' if game == 'forza-fh' else 'rad']       # (Forza's slip is its normalised slip angle)
    return sim['cols'], laws_mod.causes(sim['cols'], sim['info']['dt'], bins), sim['info'], sim['in_force']


def shape(names):
    """The grip in use against the slip of drives, one game's: Forza by Forza's normalised slip (ffb_forza.GRIP_USE is its
    grip_use_shape), ACR by slip over peak slip (the cross-check)."""
    import ffb_parity as fp
    import ffb_score as fs
    drives, game, out = [], None, []
    with fs._sandbox():
        for name in names:
            path = fp.resolve(name)
            g = fp.game_of(path)
            if game not in (None, g):
                out.append('%s: %s, not %s, left out' % (name, g, game))
                continue
            game = g
            cols, c, _info, _f = _simulate(path, game)
            drives.append((cols, c))
    if game == 'acr':
        out.append('ACR, grip in use u = r / mu against slip / peak slip (p80 per stretch, median over stretches):')
        out += ['  x %.2f  u %.2f  (%d stretches)' % r for r in laws_mod.shape_table(drives, laws_mod.SHAPE_X, 'x')]
    else:
        out.append('%s, grip in use u = r / mu against the normalised slip:' % game)
        out += ['  s %.2f  u %.2f  (%d stretches)' % r for r in laws_mod.shape_table(drives)]
        out.append('ffb_forza.GRIP_USE = %r' % (laws_mod.grip_use_shape(drives),))
    return out


def _car_per_sample(items, times, fallback):
    """The car live at each of `times` (the last car named at or before it; the first named one before any): a capture can hold two cars."""
    ts, cs, car = [], [], None
    for t, s in items:
        car = s.car or car
        ts.append(t)
        cs.append(car)
    first = next((c for c in cs if c), fallback)
    cs = np.array([c or first for c in cs], object)
    i = np.clip(np.searchsorted(ts, np.asarray(times, float), side='right') - 1, 0, max(len(ts) - 1, 0))
    return cs[i] if len(ts) else np.full(len(times), fallback, object)


def refit(names, laws_path):
    import ffb_parity as fp
    import ffb_score as fs
    drives, used, cars, out = [], [], [], []
    with fs._sandbox():
        for name in names:
            path = fp.resolve(name)
            if fp.game_of(path) != 'acr':
                out.append('%s: not an ACR capture, left out' % name)
                continue
            cols, c, info, _f = _simulate(path, 'acr')
            cols['gsx'] = laws_mod.gripscale_at(_load(path), cols['t'])
            drives.append((cols, c))
            used.append(os.path.basename(path)[:15])
            cars.append(_car_per_sample(_load(path), cols['t'], info['car']))
            out.append('%s: %d samples scored, %d stretches with a grip top' % (used[-1], info['scored'], len(laws_mod.stretch_points(c))))
    laws = laws_mod.fit_laws(drives, used, cars)
    with open(laws_path, 'w') as f:
        json.dump(laws, f, indent=1, sort_keys=True)
        f.write('\n')
    a, b = laws['tp0']
    out += ['laws written to %s' % laws_path,
            'no-slip pneumatic trail: %.1f mm + %.1f mm x mu  (%d stretches, mu %.2f-%.2f)' % (a * 1000, b * 1000, laws['stretches'], *laws['mu_range']),
            ('no-slip pneumatic trail against GripScale mu (for a game without tyre forces; TP0_FIT is against the tyres own top): %.1f mm + %.1f mm x mu  (r %.2f, %d stretches, mu %.2f-%.2f)'
             % (laws['tp0_gs'][0] * 1000, laws['tp0_gs'][1] * 1000, laws['gs_r'], laws['gs_stretches'], *laws['gs_range'])) if 'tp0_gs' in laws
            else 'no line of no-slip trail against GripScale mu',
            'trail over the no-slip trail, by cause: ' + ', '.join('%s %.2f' % kv for kv in laws['cells'].items()),
            'peak slip (rad) by grip band: ' + ', '.join('%s %.3f' % kv for kv in sorted(laws['peak_slip_rad'].items()))]
    ax = laws['axle']
    out += ['the axle law (what the racks are checked against; a cell = the median over its stretches):',
            '  trail over the no-slip trail: ' + ', '.join('%s %.2f (%d)' % (k, v, ax['cell_stretches'][k]) for k, v in ax['cells'].items()),
            '  (the table above, pooled over the samples, is kept as the reference)']
    form = ax.get('form')
    if form:
        out += ['the form the racks play (every car weighing the same; ffb_forza.AXLE_TP_USE / AXLE_X_BLEND / AXLE_TP_PAST / AXLE_U_OF_X):',
                '  AXLE_TP_USE = %s' % ([(round(a, 3), round(b, 3)) for a, b in form['use']],),
                '  AXLE_X_BLEND = %s   (rms against the (u, x) cells %.3f)' % (tuple(form['blend']), form['rms']),
                '  AXLE_TP_PAST = %s' % ([(round(a, 3), round(b, 3)) for a, b in form['past']],),
                '  AXLE_U_OF_X = %s' % ([(round(a, 3), round(b, 3)) for a, b in form['u_of_x']],)]
        out += ['  (u, x) cells: ' + ', '.join('%s %.2f' % kv for kv in form['ux'].items())]
    return out


def refit_rack(names):
    """RbrRack's pneumatic-trail constants in its own grip estimate, refitted over ACR drives (ffb_laws.fit_rack_form)."""
    import ffb_parity as fp
    import ffb_score as fs
    drives, out = [], []
    with fs._sandbox():
        for name in names:
            path = fp.resolve(name)
            if fp.game_of(path) != 'acr':
                out.append('%s: not an ACR capture, left out' % name)
                continue
            cols, c, _info, _f = _simulate(path, 'acr')
            drives.append((cols, c))
    fit = laws_mod.fit_rack_form(drives)
    if fit is None:
        return out + ['no drive gives a true mass and three stretches with a no-slip trail']
    out.append('no-slip trail by the rack\'s grip (mu: rack, drive; rack/drive median %.2f; learnt peak / drive peak %.2f)' % (fit['mu_ratio'], fit['peak_ratio']))
    out += ['  rack mu %.3f  drive mu %.3f  trail %.1f mm  peak learnt %.3f drive %.3f' % (r[0], r[2], r[1] * 1000, r[3], r[4]) for r in fit['stretches']]
    out.append('power  B %.4f m  N %.2f  (relative rms %.3f);  line %.1f mm + %.1f mm x mu (relative rms %.3f)'
               % (fit['B'], fit['N'], fit['rel_pow'], fit['line'][0] * 1000, fit['line'][1] * 1000, fit['rel_line']))
    out.append('trail over the fit by the rack\'s grip in use u: ' + ', '.join('%g-%g %.2f (%d)' % (k + v) for k, v in sorted(fit['use'].items())))
    out.append('and by x past the peak: ' + ', '.join('%g-%g %.2f (%d)' % (k + v) for k, v in sorted(fit['past'].items())))
    return out


def _fit_lines(c, dt, unit, lock, wheel_range, wheelbase, const):
    """(b): the drive's own constants."""
    pts = laws_mod.stretch_points(c, min_n=10 ** 9)
    n_all = len(np.unique(c['seg']))
    out = ['stretches of 60 s: %d, %d with a grip top (the rest never reached the limit: their mu is a floor, left out)' % (n_all, len(pts))]
    mu = laws_mod.band_medians(c, c['mu'], dt)
    pk = laws_mod.band_medians(c, c['peak'], dt)
    for name in tools.MU_BAND_NAMES:
        if name in mu:
            out.append('  grip band %-6s mu %.2f, peak slip %.3f %s  (%.0f s)' % (name, mu[name][0], pk[name][0], unit, mu[name][1]))
    const['grip'] = {k: round(v[0], 3) for k, v in mu.items()}
    const['peak_slip'] = {'unit': unit, **{k: round(v[0], 4) for k, v in pk.items()}}
    if lock:
        ratio = np.radians(wheel_range) / 2 / lock['lock']
        out.append('steering lock %.3f rad per unit of steer (uncorrected for understeer %.3f; understeer %.4f rad per m/s2; %d samples; '
                   'scales with the assumed wheelbase %.2f m)' % (lock['lock'], lock['naive'], lock['K'], lock['n'], wheelbase))
        out.append('  ratio %.1f if full steer is half the profile\'s wheel range of %.0f deg (Forza; not where the game maps its own range)' % (ratio, wheel_range))
        const['lock_rad'], const['ratio_at_range'], const['understeer_rad_per_ms2'] = round(lock['lock'], 4), round(ratio, 2), round(lock['K'], 5)
    else:
        out.append('steering lock: not enough steady driving with steer, velocity and yaw rate')
    return out


def _check_lines(laws, cols, c, dt, lock, const):
    """(c): each stage against the laws. Returns (lines, number flagged)."""
    out, flags = [], 0
    out.append('-- pneumatic trail by cause (rack / law per sample, median; stretches with a grip top)')
    out.append('   grip    cause         s   rack mm  law mm  rack/law  (tolerance)')
    rows = laws_mod.trail_check(laws, c, dt)
    for band, cell, secs, rack, law, ratio, flag, tol in rows:
        out.append('   %-6s  %-9s %5.0f   %6.1f  %6.1f   %5.2f  (+-%.2f)%s' % (band, cell, secs, rack, law, ratio, tol, '  FLAG' if flag else ''))
        flags += flag
    if not rows:
        out.append('   no cell with %g s: the drive never reached the grip limit long enough' % laws_mod.MIN_S)
    const['trail_cells'] = [{'grip': r[0], 'cause': r[1], 's': round(r[2], 1), 'rack_mm': round(r[3], 1), 'law_mm': round(r[4], 1),
                             'ratio': round(r[5], 3), 'flag': bool(r[6])} for r in rows]
    resp = laws_mod.grip_response(laws, c)
    out.append('-- no-slip trail against the grip: ' + resp['verdict'])
    for mu, (rack, law) in resp['at'].items():
        out.append('   at mu %.2f: rack %.1f mm, law %.1f mm' % (mu, rack, law))
    flags += 'FLAG' in resp['verdict']
    const['no_slip_trail'] = {'a_mm': round(resp['a'] * 1000, 2) if resp['n'] else None, 'b_mm_per_mu': round(resp['b'] * 1000, 2),
                              'stretches': resp['n'], 'verdict': resp['verdict']}
    out.append('-- peak slip the rack divides by, against the drive\'s own')
    rp, fp_ = laws_mod.band_medians(c, cols['rack_peak'], dt), laws_mod.band_medians(c, c['peak'], dt)
    for name in fp_:
        if name in rp:
            ratio = rp[name][0] / fp_[name][0]
            flag = abs(ratio - 1) > laws_mod.PEAK_FLAG
            flags += flag
            out.append('   grip %-6s rack %.3f, drive %.3f, rack/drive %.2f%s' % (name, rp[name][0], fp_[name][0], ratio, '  FLAG' if flag else ''))
    if not rp:
        out.append('   the rack learns no peak slip of its own (see the x cells above for where its trail fades)')
    out.append('-- grip the rack believes, against the drive\'s own (Fy_f / Fz top)')
    for key, text, same in (('rack_mu', 'grip_mu (Fy_f / Fz)', True), ('gs_mu', 'GripScale (p95 of |a_y| / g: the whole car, reads higher)', False),
                           ('lg_mu', 'LimitGrip (Newton Fy_f over the static front load at the slip limit)', True)):
        got = laws_mod.band_medians(c, cols[key], dt)
        own = laws_mod.band_medians(c, c['mu'], dt)
        for name in got:
            ratio = got[name][0] / own[name][0]
            flag = same and abs(ratio - 1) > FLAG_GRIP
            flags += flag
            out.append('   grip %-6s %s %.2f, drive %.2f, ratio %.2f%s' % (name, text, got[name][0], own[name][0], ratio, '  FLAG' if flag else ''))
    out.append('-- steering lock the rack uses, against the motion\'s')
    rack_lock = cols['lock'][np.isfinite(cols['lock'])]
    if lock and len(rack_lock):
        ratio = float(np.median(rack_lock)) / lock['lock']
        flag = abs(ratio - 1) > laws_mod.LOCK_FLAG
        flags += flag
        out.append('   rack %.3f rad, motion %.3f rad, rack/motion %.2f%s' % (np.median(rack_lock), lock['lock'], ratio, '  FLAG' if flag else ''))
        const['rack_lock_rad'] = round(float(np.median(rack_lock)), 4)
    else:
        out.append('   no lock in the rack or no fit')
    out.append('-- column, boost and rim: ffb_carprops.driver_from_column, the same code for every game (median boost gain %.2f, rho %.3f)'
               % (np.nanmedian(cols['gain']), np.nanmedian(cols['rho'])))
    return out, flags


FLAG_GRIP = 0.20


def report(path, game, laws, peak=None, no_rack=False, out_path=None):
    """(lines, constants dict) for one capture; inside the sandbox when the game has a rack."""
    from oversteer import ffb_forza
    short = os.path.basename(path)[:15]
    items = _load(path)
    const = {'capture': short, 'game': game, 'laws': {'tp0': laws['tp0'], 'sources': laws.get('sources')}}
    out = ['== %s, decoded as: %s, %d samples' % (short, game or 'unknown', len(items))]
    samples = [s for _t, s in items]
    share, present, missing = tools.channel_report(samples[::max(1, len(samples) // 4000)], tools.CHANNELS + laws_mod.EXTRA_CHANNELS)
    out += ['', '== (a) channels and routes', 'present: ' + ', '.join('%s %.0f%%' % (ch, share[ch] * 100) for ch in present),
            'missing: ' + (', '.join(missing) or 'none')]
    const['routes'] = {}
    for stage, route, needs in laws_mod.routes(share):
        out.append('  %-17s %s%s' % (stage + ':', route, '   [needs %s]' % needs if needs else ''))
        const['routes'][stage] = route
    wheelbase = ffb_forza.WHEELBASE
    have_rack = game in tools.RACKS and not no_rack
    wheel_range = tools.profile_range(game) if game in tools.RACKS else 900.0
    if have_rack:
        cols, c, info, in_force = _simulate(path, game, peak)
        dt, unit = info['dt'], 'norm' if game == 'forza-fh' else 'rad'
        lock = laws_mod.lock_fit(cols, wheelbase)
        const['car'], const['rack'] = info['car'], {k: v for k, v in info['learnt'].items() if v is not None}
        out += ['', '== today\'s rack: %s, car %s' % (tools.RACKS[game][0], info['car'])] + in_force
    else:
        cols, unit, lock = laws_mod.raw_cols(items, wheelbase, lambda s: ffb_forza.front_share(getattr(s, 'drivetrain', None)),
                                             1.0)
        dt = tools.sample_spacing([t for t, _s in items])
        c = laws_mod.causes(cols, dt, tools.TYRE_BINS['norm' if unit == 'norm' else 'rad'])
    out += ['', '== (b) this drive\'s own constants'] + _fit_lines(c, dt, unit, lock, wheel_range, wheelbase, const)
    if have_rack and game == 'acr':
        m = tools.acr_true_mass(cols)
        if m:
            loads = float(np.nanmedian(cols['fzf'])) / (m[1] * tools.G)
            out.append('moving mass %.0f kg (all four tyres\' Fy over a_y), front share %.3f; front loads read %.2f of the front moving mass'
                       % (m[0], m[2], loads))
            const['mass_kg'], const['front_share'] = round(m[0]), round(m[2], 3)
    if have_rack:
        lines, flags = _check_lines(laws, cols, c, dt, lock, const)
        out += ['', '== (c) today\'s force against the laws (%s)' % ', '.join(laws.get('sources') or ['built in'])] + lines
        out += ['', '== %d flagged' % flags]
        const['flags'] = flags
    else:
        need = ['a rack class in oversteer/ffb_engine.py and its line in RACKS (scripts/ffb_tools.py): none yet']
        need += ['%s: %s' % (stage, needs) for stage, route, needs in laws_mod.routes(share) if needs]
        if unit == 'norm':
            need.append('the slip is normalised: the rack divides by the peak slip above, per grip band')
        out += ['', '== (c) what the engine still needs for this game'] + ['  ' + n for n in need]
        const['needs'] = need
    out_path = out_path or os.path.join(CACHE, short + '.json')
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(const, f, indent=1, default=str)
        f.write('\n')
    out += ['', 'constants written to %s' % out_path]
    return out, const


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('capture', nargs='?')
    parser.add_argument('--shape', nargs='+', metavar='CAPTURE', help='the grip in use against the slip of these drives (one game)')
    parser.add_argument('--refit', nargs='+', metavar='ACR_CAPTURE', help='rebuild the laws from these ACR drives')
    parser.add_argument('--refit-rack', nargs='+', metavar='ACR_CAPTURE', help="RbrRack's trail constants in its own grip estimate, from ACR drives")
    parser.add_argument('--laws', metavar='FILE', default=laws_mod.LAWS_PATH)
    parser.add_argument('--out', metavar='FILE.json')
    parser.add_argument('--no-rack', action='store_true', help='treat the game as one without a rack: routes, constants, what is missing')
    parser.add_argument('--peak', type=float, metavar='NM')
    parser.add_argument('--set', dest='sets', action='append', default=[], metavar='NAME=VALUE', help='a constant for the replay only')
    tools.add_store_args(parser)
    args = parser.parse_args(argv)
    if not args.capture and not args.refit and not args.refit_rack and not args.shape:
        parser.error('give a capture, --shape, --refit or --refit-rack')
    import ffb_parity as fp
    import ffb_score as fs
    try:
        tools.apply_store_args(args)
        if args.refit_rack:
            out = refit_rack(args.refit_rack)
        elif args.shape:
            out = shape(args.shape)
        elif args.refit:
            out = refit(args.refit, args.laws)
        else:
            path = fp.resolve(args.capture)
            game = fp.game_of(path)
            laws = laws_mod.load_laws(args.laws)
            with fs._sandbox(), tools.overrides(args.sets) as set_lines:
                out = list(set_lines) + report(path, game, laws, args.peak, args.no_rack, args.out)[0]
    except (FileNotFoundError, OSError, ValueError, tools.NoRack) as e:
        print('ffb-new-game: %s' % e, file=sys.stderr)
        return 2
    text = '\n'.join(out)
    print(text)
    os.makedirs(CACHE, exist_ok=True)
    with open(LOG, 'w') as f:
        f.write(text + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
