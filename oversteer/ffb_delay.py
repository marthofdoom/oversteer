"""How late a big force reversal reaches the wheel, from the game's force to the rim, as an estimate from the settings:
the game's own source delay and reconstruction, RBR's force cut, the gear crossing's limit and the wheel's latency.

DELAY_SOFT_MS is a soft cutoff: past it the Countersteer page shows the figure in red. It warns only, nothing is blocked
or changed. The source delays are rough figures per game; the filters are group delays of a second-order Butterworth,
0.225 / corner in Hz.
"""
from . import ffb_forza
from . import ffb_gear

DELAY_SOFT_MS = 50.0
SOURCE_MS = {'rbr': 2.0, 'acr': 3.0, 'fh6': 8.0, 'beamng': 3.0}
BUTTERWORTH_S_HZ = 0.225
RBR_CUT_OFF = 120        # Hz: the top of the RBR force cut's range is no filter (ffb_engine.RBR_CUT_OFF)
WHEEL_LATENCY_MS = 5.0   # when the calibration's fit has none
FH_RATE = 60.0           # packets/s when the rate isn't known


def game_key(game):
    """The engine's mode or a decoded format's game as a SOURCE_MS key; a game we don't know counts as ACR."""
    name = str(game or '').lower()
    if name.startswith('rbr'):
        return 'rbr'
    if name.startswith(('fh', 'forza')):
        return 'fh6'
    if name.startswith('beamng'):
        return 'beamng'
    return 'acr'


def group_delay_ms(corner_hz):
    return BUTTERWORTH_S_HZ / corner_hz * 1e3


def fh_corner(rate):
    """The corner of ffb_forza.OutputFilter at this packet rate."""
    f = ffb_forza.OutputFilter
    return min(max((rate or FH_RATE) / f.FC_RATIO, f.FC_MIN), f.FC_MAX)


def source_ms(game, settings):
    """The game's part: its source delay, and what follows it in our chain for that game alone."""
    key = game_key(game)
    ms = SOURCE_MS[key]
    if key == 'fh6':
        ms += group_delay_ms(fh_corner(settings.get('fh_rate')))
    elif key == 'rbr':
        cut = settings.get('rbr_cut_hz')
        if cut and cut < RBR_CUT_OFF:
            ms += group_delay_ms(cut)
    return ms


def gear_ms(settings, wheel, level=None):
    """The gear crossing's part; 0 for a belt or direct drive, the guard off or level 0."""
    if not settings.get('gear_guard') or (wheel or {}).get('drive', 'gear') != 'gear':
        return 0.0
    return ffb_gear.reversal_late_ms(settings.get('gear_level') if level is None else level,
                                     bool(settings.get('friction_removal')),
                                     settings.get('gear_flip', ffb_gear.OVERALL_GEAR_FLIP))


def wheel_ms(wheel):
    latency = (wheel or {}).get('fit_latency_ms')
    return float(latency) if latency else WHEEL_LATENCY_MS


def estimated_total_ms(game, settings, wheel):
    """The estimate, ms. `settings`: gear_guard, gear_level (%), gear_flip (% of full scale), friction_removal, rbr_cut_hz, fh_rate (packets/s, optional);
    `wheel`: drive ('gear', 'belt', 'direct'), fit_latency_ms (None: not calibrated)."""
    return source_ms(game, settings) + gear_ms(settings, wheel) + wheel_ms(wheel)


def over_soft(ms):
    return ms > DELAY_SOFT_MS


def soft_level(game, settings, wheel):
    """The gear guard level (%, continuous) where the estimate reaches DELAY_SOFT_MS for this game and these settings; None
    when no level on the slider does (the guard adds nothing here, or stays under it all the way to 100 %). The estimate
    rises with the level, so bisection does. A guard switched off still has its mark: it says where it would land."""
    on = dict(settings, gear_guard=True)
    if (wheel or {}).get('drive', 'gear') != 'gear':
        return None
    if estimated_total_ms(game, dict(on, gear_level=100.0), wheel) <= DELAY_SOFT_MS:
        return None
    low, high = 0.0, 100.0
    for _ in range(40):
        mid = (low + high) / 2.0
        if estimated_total_ms(game, dict(on, gear_level=mid), wheel) > DELAY_SOFT_MS:
            high = mid
        else:
            low = mid
    return low


def text(ms):
    return 'about %.0f ms total' % ms
