"""The peak torque (Nm at the rim) of the wheels Countersteer FFB may play on, keyed by USB id (wheel_ids), for the
physical target in ffb_engine.RackForce: the car's real rim torque is mapped to a share of the wheel's full scale.

Only a wheel on the new-lg4ff driver takes ext_force, so only those are `usable`; the rest are listed for later.
A wheel not here (or not usable) gets None, and the target then falls back to the relative rule (the car's p99
torque at TARGET_P99 of full scale). The figures are the makers' peak torque, rounded; the Logitech belt and gear
wheels peak lower than their sheets for long (the thermal limit is the driver's own)."""
from . import wheel_ids as wid

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
