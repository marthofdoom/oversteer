"""oversteer-shm-bridge's reading of the graphics page's clock (version 4):
the C parser, compiled natively where a C compiler is installed, against a
Python mirror of it and against strings from marth's raw ACR dump; and the
whole packet as the bridge builds it from the pages (version 5), compiled
the same way, against the decoder."""
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
    from oversteer.telemetry_formats import OVST3_SIZE, OVST4_SIZE, OVST5_SIZE
    source = open(BRIDGE).read()
    assert '#define OVST_VERSION 5' in source
    assert OVST3_SIZE == 324 and OVST4_SIZE == 328 and OVST5_SIZE == 456
    # the bridge's own compile-time checks
    assert 'offsetof(struct ovst_packet, packet_id) == {}'.format(OVST4_SIZE) in source
    assert 'sizeof(struct ovst_packet) == {}'.format(OVST5_SIZE) in source
    assert re.search(r'float world_pos\[3\];\s*/\*[^*]*(\*[^/][^*]*)*\*/\s*float stage_clock;', source)
    # the physics page is mapped to the end of the last field read (abs vibrations, 796 + 4)
    assert 'static const size_t phys_sizes[] = { 800, 580, 64, 0 };' in source
    assert '#define PHYS_VIBRATIONS  784' in source


# -- the packet as the bridge builds it, compiled natively, against the decoder --

PHYS, STATIC, GRAPH = 800, 820, 1588


def _packet_builder(tmp_path):
    cc = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')
    if cc is None:
        pytest.skip('no C compiler to build the packet natively')
    source = open(BRIDGE).read()
    parts = [chunk.split('/* native: end */')[0] for chunk in source.split('/* native: begin')[1:]]
    parts = [part.split('*/', 1)[1] for part in parts]
    assert len(parts) == 2 and 'fill_packet' in parts[1] and 'struct ovst_packet {' in parts[0]
    harness = tmp_path / 'packet.c'
    harness.write_text('#include <stdio.h>\n#include <stdlib.h>\n#include <stdint.h>\n#include <stddef.h>\n'
                       '#include <string.h>\n' + ''.join(parts) + r'''
/* argv: game, packet id, physics size, static size, graphics size; stdin: the three pages at their full size */
int main(int argc, char **argv)
{
    static char phys[%d], stat[%d], graph[%d];
    struct ovst_packet pkt;
    if (argc != 6 || fread(phys, 1, sizeof(phys), stdin) != sizeof(phys) || fread(stat, 1, sizeof(stat), stdin)
        != sizeof(stat) || fread(graph, 1, sizeof(graph), stdin) != sizeof(graph))
        return 2;
    fill_packet(&pkt, (uint8_t)atoi(argv[1]), atoi(argv[2]), phys, (size_t)atoi(argv[3]),
                atoi(argv[4]) ? stat : NULL, (size_t)atoi(argv[4]), atoi(argv[5]) ? graph : NULL, (size_t)atoi(argv[5]));
    fwrite(&pkt, sizeof(pkt), 1, stdout);
    return 0;
}
''' % (PHYS, STATIC, GRAPH))
    exe = tmp_path / 'packet'
    subprocess.run([cc, '-O2', '-Wall', '-Werror', '-Wno-unused-function', '-Wno-unused-const-variable',
                    '-Wno-unknown-warning-option', '-o', str(exe), str(harness)], check=True)
    return exe


def _pages():
    """A physics, static and graphics page with known values where the bridge reads (SPageFilePhysics,
    SPageFileStatic, SPageFileGraphic offsets, as ACR fills them)."""
    phys, stat, graph = bytearray(PHYS), bytearray(STATIC), bytearray(GRAPH)
    struct.pack_into('<ifffii', phys, 0, 98765, 0.8, 0.1, 30.0, 4, 6100)     # packetId, gas, brake, fuel, gear 3, rpms
    struct.pack_into('<ff', phys, 24, -0.25, 90.0)                           # steerAngle, speedKmh
    struct.pack_into('<4f', phys, 72, 3000.0, 3100.0, 2500.0, 2600.0)        # wheelLoad
    struct.pack_into('<f', phys, 308, 0.37)                                  # finalFF
    struct.pack_into('<12f', phys, 468, *[0.01 * i for i in range(12)])      # tyreContactNormal[4][3]
    struct.pack_into('<4f', phys, 592, -31.0, -42.0, -3.0, -4.0)             # mz
    struct.pack_into('<4f', phys, 608, 100.0, 110.0, 120.0, 130.0)           # fx
    struct.pack_into('<4f', phys, 624, 200.0, 210.0, 220.0, 230.0)           # fy
    struct.pack_into('<4f', phys, 640, 0.01, 0.02, 0.03, 0.04)               # slipRatio
    struct.pack_into('<4f', phys, 656, 0.05, 0.06, 0.07, 0.08)               # slipAngle
    struct.pack_into('<ii', phys, 672, 1, 0)                                 # tcinAction, absInAction
    struct.pack_into('<4f', phys, 784, 0.0, 0.5, 0.25, 0.0)                  # kerb, slip, g, abs vibrations
    struct.pack_into('<i', stat, 412, 7800)                                  # maxRpm
    stat[68:68 + 18] = 'rally_car'.encode('utf-16-le')
    struct.pack_into('<f', stat, 520, 8123.0)                                # trackSPlineLength
    graph[12:12 + 18] = '00:58.094'.encode('utf-16-le')                      # currentTime
    struct.pack_into('<f', graph, 156, 2030.0)                               # distanceTraveled
    return bytes(phys + stat + graph)


def _build(exe, game, phys_size, packet_id=98765, stat_size=STATIC, graph_size=GRAPH):
    args = [str(exe), str(game), str(packet_id), str(phys_size), str(stat_size), str(graph_size)]
    return subprocess.run(args, input=_pages(), capture_output=True, check=True).stdout


def test_the_bridges_v5_packet_decodes(tmp_path):
    from oversteer.telemetry_formats import OVST5_SIZE, decode_sample
    exe = _packet_builder(tmp_path)
    data = _build(exe, 3, PHYS)
    assert len(data) == OVST5_SIZE and data[:5] == b'OVST\x05'
    s = decode_sample(data)
    assert s.game == 'acr' and s.car == 'acr/rally_car' and s.gear == 3 and s.rpm == 6100.0 and s.max_rpm == 7800.0
    assert abs(s.stage_time - 58.094) < 1e-4 and s.lap_distance == 2030.0 and s.stage_length == 8123.0
    assert s.tyre_fx == (100.0, 110.0, 120.0, 130.0) and s.tyre_load == (3000.0, 3100.0, 2500.0, 2600.0)
    assert s.packet_id == 98765 and abs(s.game_ffb - 0.37) < 1e-6
    assert s.tyre_mz == (-31.0, -42.0, -3.0, -4.0)
    assert [round(x, 6) for x in s.tyre_slip_ratio] == [0.01, 0.02, 0.03, 0.04]
    assert [round(x, 6) for x in s.tyre_slip_angle] == [0.05, 0.06, 0.07, 0.08]
    assert [round(x, 6) for n in s.contact_normal for x in n] == [round(0.01 * i, 6) for i in range(12)]
    assert s.vibrations == (0.0, 0.5, 0.25, 0.0)
    assert s.tc_active is True and s.abs_active is False


def test_the_bridge_sends_nan_past_the_mapped_page(tmp_path):
    from oversteer.telemetry_formats import decode_sample
    exe = _packet_builder(tmp_path)
    # AC's 580-byte page: finalFF and the contact normals are in it, the rest is not (nor read for AC)
    ac = decode_sample(_build(exe, 1, 580))
    assert ac.game == 'ac' and abs(ac.game_ffb - 0.37) < 1e-6 and ac.contact_normal is not None
    assert ac.tyre_mz is ac.tyre_slip_ratio is ac.tyre_slip_angle is ac.vibrations is None
    assert ac.tc_active is None and ac.abs_active is None
    # ACR with only 580 bytes mapped: nothing read past them
    short = decode_sample(_build(exe, 3, 580))
    assert short.tyre_mz is short.vibrations is short.tyre_fx is short.tc_active is None
    assert abs(short.game_ffb - 0.37) < 1e-6
    # the page cut inside the vibrations: the four come as one, so none
    cut = decode_sample(_build(exe, 3, 792))
    assert cut.vibrations is None and cut.tyre_mz == (-31.0, -42.0, -3.0, -4.0) and cut.tc_active is True
    # an unnamed game: only what every AC game has
    unknown = decode_sample(_build(exe, 0, PHYS))
    assert unknown.game == 'acpmf' and unknown.tyre_mz is None and abs(unknown.game_ffb - 0.37) < 1e-6
    # the smallest mapping (64 bytes): the v5 tail is NaN but for the step
    tiny = decode_sample(_build(exe, 3, 64, stat_size=0, graph_size=0))
    assert tiny.packet_id == 98765 and tiny.game_ffb is None and tiny.contact_normal is None
