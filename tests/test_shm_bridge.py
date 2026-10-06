"""oversteer-shm-bridge's reading of the graphics page's clock (version 4):
the C parser, compiled natively where a C compiler is installed, against a
Python mirror of it and against strings from marth's raw ACR dump."""
import math
import os
import re
import shutil
import struct
import subprocess

import pytest

BRIDGE = os.path.join(os.path.dirname(__file__), '..', 'data', 'telemetry', 'oversteer-shm-bridge.c')


def parse_clock(text):
    """The Python mirror of the bridge's parse_clock(): [[h:]m:]s[.fff], or
    m:ss:mmm; only the leading field may pass 59; NaN for anything else."""
    m = re.fullmatch(r'(\d{1,6})(?::(\d{1,6}))?(?::(\d{1,6}))?(?:\.(\d+))?', text)
    if not m:
        return math.nan
    fields = [f for f in m.groups()[:3] if f is not None]
    frac = m.group(4)
    if frac is None and len(fields) == 3 and len(fields[2]) == 3:
        if int(fields[1]) > 59:
            return math.nan
        return int(fields[0]) * 60 + int(fields[1]) + int(fields[2]) / 1000.0
    if any(int(f) > 59 for f in fields[1:]):
        return math.nan
    value = 0
    for f in fields:
        value = value * 60 + int(f)
    return value + (int(frac[:6]) / 10 ** len(frac[:6]) if frac else 0.0)


def wide(text, chars=15):
    """`text` as the page holds it: wchar_t[15], NUL-padded (a full one has no NUL)."""
    units = [ord(c) for c in text][:chars]
    return units + [0] * (chars - len(units))


# (as the page spells it, seconds; None: NaN)
CASES = [
    ('00:58.094', 58.094),               # ACR, from marth's dump
    ('00:00.000', 0.0),                  # before the start, and after a restart
    ('', None),                          # loading: nothing written
    ('04:59.886', 299.886),
    ('75:12.345', 4512.345),             # the leading field may pass 59
    ('1:02:03.456', 3723.456),           # past the hour
    ('1:23:456', 83.456),                # m:ss:mmm, the milliseconds after a colon
    ('58.5', 58.5),
    ('-:--:---', None),                  # a placeholder
    ('00:60.000', None),
    ('1:60:00', None),
    ('00:58.', None),
    (':58.094', None),
    ('00:58.094x', None),
    ('1:2:3:4', None),
    ('123456789:00', None),              # more digits than a clock has
]


@pytest.mark.parametrize('text, seconds', CASES)
def test_the_python_mirror(text, seconds):
    got = parse_clock(text)
    assert (math.isnan(got) if seconds is None else abs(got - seconds) < 1e-9)


def _compiled(tmp_path):
    cc = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
    if cc is None:
        pytest.skip('no C compiler to build the parser natively')
    source = open(BRIDGE).read()
    part = source.split('/* clock-parse: begin')[1].split('/* clock-parse: end */')[0].split('*/', 1)[1]
    harness = tmp_path / 'clock.c'
    harness.write_text('#include <stdio.h>\n#include <stdint.h>\n' + part + r'''
int main(void)
{
    unsigned v[15];
    while (scanf("%x %x %x %x %x %x %x %x %x %x %x %x %x %x %x", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5], &v[6],
                 &v[7], &v[8], &v[9], &v[10], &v[11], &v[12], &v[13], &v[14]) == 15) {
        uint16_t w[15];
        int i;
        double r;
        for (i = 0; i < 15; i++)
            w[i] = (uint16_t)v[i];
        r = parse_clock(w, 15);
        if (r != r)
            printf("nan\n");
        else
            printf("%.6f\n", r);
    }
    return 0;
}
''')
    exe = tmp_path / 'clock'
    subprocess.run([cc, '-O2', '-Wall', '-Werror', '-o', str(exe), str(harness)], check=True)
    return exe


def _run(exe, strings):
    lines = '\n'.join(' '.join('%x' % u for u in units) for units in strings) + '\n'
    out = subprocess.run([str(exe)], input=lines, capture_output=True, text=True, check=True).stdout.split()
    return [float(x) for x in out]


def test_the_bridges_parser_matches_the_mirror(tmp_path):
    exe = _compiled(tmp_path)
    got = _run(exe, [wide(text) for text, _ in CASES])
    for (text, seconds), value in zip(CASES, got):
        assert (math.isnan(value) if seconds is None else abs(value - seconds) < 1e-6), text
    # A full field (15 characters, no NUL) is read to its end and no further
    assert _run(exe, [wide('000000:00.00000')]) == [0.0]


# The 30 bytes at offset 12 of the graphics page, as marth's ACR dump
# (oversteer-shm-bridge.dump, 2026-10-03) has them: running, at the start,
# and loading
DUMP_FIELDS = [
    ('300030003a00350038002e00300039003400000000000000000000000000', 58.094),
    ('300030003a00330034002e00310030003700000000000000000000000000', 34.107),
    ('300030003a00300030002e00300030003000000000000000000000000000', 0.0),
    ('000000000000000000000000000000000000000000000000000000000000', None),
]


def test_the_dumps_clock_fields(tmp_path):
    strings = [list(struct.unpack('<15H', bytes.fromhex(h))) for h, _ in DUMP_FIELDS]
    for units, (_, seconds) in zip(strings, DUMP_FIELDS):
        text = ''.join(chr(u) for u in units).split('\0', 1)[0]
        value = parse_clock(text)
        assert (math.isnan(value) if seconds is None else abs(value - seconds) < 1e-9)
    exe = _compiled(tmp_path)
    for value, (_, seconds) in zip(_run(exe, strings), DUMP_FIELDS):
        assert (math.isnan(value) if seconds is None else abs(value - seconds) < 1e-6)


def test_the_packet_is_the_size_the_decoder_takes():
    from oversteer.telemetry_formats import OVST3_SIZE, OVST4_SIZE
    source = open(BRIDGE).read()
    assert '#define OVST_VERSION 4' in source
    assert OVST3_SIZE == 324 and OVST4_SIZE == 328
    assert 'sizeof(struct ovst_packet) == {}'.format(OVST4_SIZE) in source      # the bridge's own compile-time check
    assert re.search(r'float world_pos\[3\];\s*/\*[^*]*(\*[^/][^*]*)*\*/\s*float stage_clock;\s*};', source)
