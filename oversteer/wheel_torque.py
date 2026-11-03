"""The peak torque (Nm at the rim) of the wheels Countersteer FFB may play on, keyed by USB id (wheel_ids), for the
physical target in ffb_engine.RackForce: the driver's torque at the rim of a real car, played 1:1 in Nm up to the
KNEE share of the wheel's peak and compressed above it (rim_share), so the car's top torque lands at STEER_BUDGET.

Only a wheel on the new-lg4ff driver takes ext_force, so only those are `usable`; the rest are listed for later.
A wheel not here (or not usable) gets None, and the target then assumes the 2.1 Nm class (DEFAULT_PEAK_NM: the
G29 and its kin, the wheels Countersteer FFB is built and tested on). The figures are the makers' peak torque, rounded; the Logitech belt and gear
wheels peak lower than their sheets for long (the thermal limit is the driver's own)."""
import math

from . import wheel_ids as wid

DEFAULT_PEAK_NM = 2.1                    # an unknown wheel is taken to peak like a G29
STEER_BUDGET = 0.75                      # share of full scale the steering torque may reach; the other 25 % stay free
                                         # for texture and impacts (ffb_texture fits in FULL - |steering force|)
P99_TARGET = 0.70                        # share of full scale the car's p99 driver torque was scaled to land at (impedance_scale)
KNEE = 0.50                              # share of the wheel's peak played 1:1 in Nm ("1:1 up to"; valid KNEE_RANGE)
KNEE_RANGE = (0.30, 0.60)
TOP = STEER_BUDGET                       # share the compression lands the car's top torque at
C_U0 = 0.25                              # the compression's early-bend width in the log domain, as a share of ln(TOP / knee)
HYD_TOP_NM = 3.7                         # Nm: the top of an assisted car's driver torque (ffb_carprops.HYD_K + HYD_R)

# usb id: (peak torque Nm, usable)
PEAK_NM = {
    wid.LG_G29: (2.1, True),
    wid.LG_G920: (2.2, True),
    wid.LG_G923P: (2.2, True),
    wid.LG_G923X: (2.2, True),
    wid.LG_G27: (2.5, True),
    wid.LG_G25: (2.5, True),
    wid.LG_DFGT: (2.0, True),
    wid.LG_DFP: (2.0, True),
    wid.FT_CSL_DD: (5.0, False),         # 8 Nm with the boost kit
    wid.CM_C5: (5.0, False),
}

# usb id: the wheel's rim diameter in mm. The hand feels force = torque / rim radius, so a car's torque at its own steering
# wheel is scaled by rho = rim / car wheel diameter (RackForce.rho) before rim_share: "1:1" is in hand force. None: unknown
# (the G29's is taken). Logitech's published figures: G29/G920/G923 11 in (279 mm), G25/G27/DFGT about 270 mm.
RIM_MM = {
    wid.LG_G29: 279.0,
    wid.LG_G920: 279.0,
    wid.LG_G923P: 279.0,
    wid.LG_G923X: 279.0,
    wid.LG_G27: 270.0,
    wid.LG_G25: 270.0,
    wid.LG_DFGT: 270.0,
    wid.LG_DFP: None,
    wid.FT_CSL_DD: None,
    wid.CM_C5: None,
}
DEFAULT_RIM_MM = 279.0                   # an unknown wheel is taken to have a G29's rim
CAR_WHEEL_MM = 350.0                     # a rally car's steering wheel (profile key countersteer_car_wheel_mm)
CAR_WHEEL_RANGE = (280, 420)
RIM_RANGE = (200, 400)                   # mm: the rim diameter setting

# Direct drives by name, for later: their USB ids are not in wheel_ids and are NOT FOUND in the repo (not guessed)
DIRECT_DRIVE_NM = {'moza r5': 5.5, 'moza r9': 9.0, 'moza r12': 12.0, 'moza r16': 16.0, 'moza r21': 21.0,
                   'fanatec csl dd 8': 8.0, 'simucube 2 sport': 17.0}


def peak_nm(usb_id):
    """The wheel's peak rim torque in Nm if it is known and usable, else None."""
    entry = PEAK_NM.get(str(usb_id).lower()) if usb_id else None
    return entry[0] if entry is not None and entry[1] else None


def rim_mm(usb_id):
    """The wheel's rim diameter in mm, None if unknown."""
    return RIM_MM.get(str(usb_id).lower()) if usb_id else None


def impedance_scale(p99_nm, peak_nm=None):
    """The uniform impedance scale s of a car on a wheel: the factor that brings the car's p99 driver torque (Nm at
    the rim) to P99_TARGET of the wheel's full scale, at most 1 (a strong wheel plays the car 1:1). The torque no
    longer uses it (rim_share plays 1:1 up to the knee); it is kept as a diagnostic."""
    if not p99_nm or not p99_nm > 0.0:
        return 1.0
    return min(1.0, P99_TARGET * (peak_nm or DEFAULT_PEAK_NM) / p99_nm)


_GAMMA = {}


def _v(u, gamma, u0):
    return gamma * u + (1.0 - gamma) * u0 * (1.0 - math.exp(-u / u0))


def _gamma(knee, x_top, peak):
    """The compression's share gamma (1: none) that brings a driver torque x_top (Nm) to TOP: v(u_top) = ln(TOP / knee),
    by bisection; 1 when x_top is within TOP * peak (plain 1:1 clamped at TOP). Cached per (knee, x_top, peak)."""
    if x_top is None or not x_top > TOP * peak:
        return 1.0
    key = (knee, x_top, peak)
    gamma = _GAMMA.get(key)
    if gamma is None:
        u0 = C_U0 * math.log(TOP / knee)
        u_top = math.log(x_top / (knee * peak))
        goal = math.log(TOP / knee)
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            if _v(u_top, mid, u0) < goal:
                lo = mid
            else:
                hi = mid
        gamma = 0.5 * (lo + hi)
        if len(_GAMMA) > 64:
            _GAMMA.clear()
        _GAMMA[key] = gamma
    return gamma


def rim_share(torque_nm, peak_nm=None, strength=1.0, x_top=None, knee=KNEE):
    """The driver's rim torque (Nm, signed) as a share of the wheel's full scale (signed). With a = strength * |T| /
    peak: a straight line (1:1 in Nm) up to the knee share, above it k e^v with v = gamma u + (1 - gamma) u0 (1 -
    e^(-u/u0)), u = ln(a / k): the log-domain compression that lands a driver torque of `x_top` Nm (the car's top:
    HYD_TOP_NM assisted, else its p99) at TOP, the slope continuous at the knee. Never past TOP, so the reserve for
    texture and impacts stays free. An unknown wheel (None) is taken as DEFAULT_PEAK_NM."""
    peak = peak_nm or DEFAULT_PEAK_NM
    a = strength * abs(torque_nm) / peak
    if a <= knee:
        share = a
    else:
        u0 = C_U0 * math.log(TOP / knee)
        u = math.log(a / knee)
        share = min(knee * math.exp(_v(u, _gamma(knee, x_top, peak), u0)), TOP)
    return math.copysign(share, torque_nm)


def rim_share_slope(torque_nm, peak_nm=None, strength=1.0, x_top=None, knee=KNEE):
    """d share / d a at this torque (a = strength * |T| / peak, as in rim_share): 1 up to the knee, v'(u) share / a
    above it, 0 once clamped at TOP."""
    peak = peak_nm or DEFAULT_PEAK_NM
    a = strength * abs(torque_nm) / peak
    if a <= knee:
        return 1.0
    u0 = C_U0 * math.log(TOP / knee)
    u = math.log(a / knee)
    gamma = _gamma(knee, x_top, peak)
    share = knee * math.exp(_v(u, gamma, u0))
    if share >= TOP:
        return 0.0
    return (gamma + (1.0 - gamma) * math.exp(-u / u0)) * share / a
