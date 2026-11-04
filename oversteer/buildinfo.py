"""Which build this is: the git commit (short hash, "-dirty" with local changes) stamped in at meson setup time
(meson.build -> bin/oversteer), "unknown" where nothing could say. Logged at start-up and written to the headers
of the telemetry captures and the FFB sidecar."""

UNKNOWN = 'unknown'
BUILD = UNKNOWN


def clean(value):
    """A commit stamp from the build, "unknown" when it is empty or still the template's placeholder."""
    value = (value or '').strip()
    return UNKNOWN if not value or value.startswith('@') else value


def set_build(value):
    global BUILD
    BUILD = clean(value)
    return BUILD
