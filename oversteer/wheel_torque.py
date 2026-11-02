"""The peak torque (Nm at the rim) of the wheels Countersteer FFB may play on, keyed by USB id (wheel_ids), for the
physical target in ffb_engine.RackForce: the driver's torque at the rim of a real car, scaled by one uniform impedance
factor s per car (s = 1 where the wheel is strong enough to play it 1:1 in Nm), so the car's p99 lands at P99_TARGET of full scale.

Only a wheel on the new-lg4ff driver takes ext_force, so only those are `usable`; the rest are listed for later.
A wheel not here (or not usable) gets None, and the target then assumes the 2.1 Nm class (DEFAULT_PEAK_NM: the
G29 and its kin, the wheels Countersteer FFB is built and tested on). The figures are the makers' peak torque, rounded; the Logitech belt and gear
wheels peak lower than their sheets for long (the thermal limit is the driver's own)."""
from . import wheel_ids as wid

DEFAULT_PEAK_NM = 2.1                    # an unknown wheel is taken to peak like a G29
STEER_BUDGET = 0.75                      # share of full scale the steering torque may reach; the other 25 % stay free
                                         # for texture and impacts (ffb_texture fits in FULL - |steering force|)
P99_TARGET = 0.70                        # share of full scale the car's p99 driver torque is scaled to land at

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

# Direct drives by name, for later: their USB ids are not in wheel_ids and are NOT FOUND in the repo (not guessed)
DIRECT_DRIVE_NM = {'moza r5': 5.5, 'moza r9': 9.0, 'moza r12': 12.0, 'moza r16': 16.0, 'moza r21': 21.0,
                   'fanatec csl dd 8': 8.0, 'simucube 2 sport': 17.0}


def peak_nm(usb_id):
    """The wheel's peak rim torque in Nm if it is known and usable, else None."""
    entry = PEAK_NM.get(str(usb_id).lower()) if usb_id else None
    return entry[0] if entry is not None and entry[1] else None


def impedance_scale(p99_nm, peak_nm=None):
    """The uniform impedance scale s of a car on a wheel: the factor that brings the car's p99 driver torque (Nm at
    the rim) to P99_TARGET of the wheel's full scale, at most 1 (a strong wheel plays the car 1:1). The same s
    scales the column's damping, friction and stiffness that the driver renders, so the whole steering is the
    real car's, shrunk to what the wheel can do (verification report item 7: s about 0.35-0.5 on a G29)."""
    if not p99_nm or not p99_nm > 0.0:
        return 1.0
    return min(1.0, P99_TARGET * (peak_nm or DEFAULT_PEAK_NM) / p99_nm)


def rim_share(torque_nm, peak_nm=None, scale=1.0):
    """The driver's rim torque (Nm, signed) as a share of the wheel's full scale (signed): scale * torque against
    the wheel's peak, in a straight line up to STEER_BUDGET and hard-clamped there (no knee: the p90-p99 range keeps
    its shape; the scale puts a car's p99 under the budget), so the reserve for texture and impacts stays free. An
    unknown wheel (None) is taken as DEFAULT_PEAK_NM."""
    share = scale * torque_nm / (peak_nm or DEFAULT_PEAK_NM)
    return max(-STEER_BUDGET, min(STEER_BUDGET, share))
