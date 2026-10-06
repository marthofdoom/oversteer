"""Steam launch options for wheels and the combined device under Proton,
as strings (text only: nothing here knows a game)."""

from locale import gettext as _

WHEEL_OPTIONS = 'SDL_JOYSTICK_HIDAPI=0 %command%'
WHEEL_TOOLTIP = _("Keeps SDL (in Proton) from driving a Logitech wheel itself through HIDAPI, which bypasses "
                  "the kernel driver and Oversteer's settings. Also set this game's Steam Input to Disabled "
                  "(Properties > Controller). To see what the game does with the devices, add PROTON_LOG=1 "
                  "(not in the copied text): Proton then writes steam-<appid>.log in your home folder.")


def shared_memory_combined(shared):
    """`shared` is gui.launch_options(): '<oversteer-run> %command%', or a
    sentence when oversteer-run was not found. Returns the combined
    options, or None when there is no path to combine."""
    if shared.endswith(' %command%'):
        return 'SDL_JOYSTICK_HIDAPI=0 ' + shared
    return None


def guidance(shared):
    """The dim line under the row."""
    text = _("Also set this game's Steam Input to Disabled (Properties > Controller).")
    combined = shared_memory_combined(shared)
    if combined:
        text += ' ' + _("If the game also uses the shared-memory bridge (Assetto Corsa family), use both: {}").format(combined)
    return text
