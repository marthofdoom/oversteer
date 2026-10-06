"""One way to write an absolute time (a stage, run, PB, split, sector or predicted time): m:ss.s, 28.7 under a
minute, h:mm:ss.s from an hour. Differences are not times: they stay signed seconds (telemetry_view.signed)."""


def clock(seconds):
    """'3:08.7', '1:25.7', '28.7', '1:02:03.4'; '–' for none. Rounded to the tenth first: 59.96 is '1:00.0'."""
    if seconds is None:
        return '–'
    tenths = int(seconds * 10 + 0.5) if seconds >= 0 else -int(-seconds * 10 + 0.5)
    sign, tenths = ('−', -tenths) if tenths < 0 else ('', tenths)
    hours, rest = divmod(tenths, 36000)
    minutes, tenths = divmod(rest, 600)
    if hours:
        return '{}{}:{:02d}:{:04.1f}'.format(sign, hours, minutes, tenths / 10.0)
    if minutes:
        return '{}{}:{:04.1f}'.format(sign, minutes, tenths / 10.0)
    return '{}{:.1f}'.format(sign, tenths / 10.0)


def span(seconds):
    """A time inside a sentence: '3:08.7', or '28.7 s' under a minute."""
    text = clock(seconds)
    return text if seconds is None or seconds >= 59.95 else text + ' s'
