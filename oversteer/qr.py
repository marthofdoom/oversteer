"""A small QR code encoder, for the pairing link in the app (a phone camera
reads it off the screen): byte mode, error correction level L, versions 1
to 10 (up to 271 bytes). `encode(text)` returns the modules as rows of
booleans (True is dark), without the quiet zone."""

# version -> (error correction codewords per block, ((blocks, data codewords per block), ...))
BLOCKS = {
    1: (7, ((1, 19),)), 2: (10, ((1, 34),)), 3: (15, ((1, 55),)), 4: (20, ((1, 80),)), 5: (26, ((1, 108),)),
    6: (18, ((2, 68),)), 7: (20, ((2, 78),)), 8: (24, ((2, 97),)), 9: (30, ((2, 116),)),
    10: (18, ((2, 68), (2, 69))),
}
ALIGNMENT = {1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30), 6: (6, 34), 7: (6, 22, 38), 8: (6, 24, 42),
             9: (6, 26, 46), 10: (6, 28, 50)}
LEVEL_L = 1                                     # the two format bits of level L

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _multiply(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _generator(degree):
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, c in enumerate(poly):
            nxt[j] ^= c
            nxt[j + 1] ^= _multiply(c, _EXP[i])
        poly = nxt
    return poly[1:]


def _remainder(data, degree):
    generator = _generator(degree)
    result = [0] * degree
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, c in enumerate(generator):
            result[i] ^= _multiply(c, factor)
    return result


def _capacity(version):
    ec, groups = BLOCKS[version]
    return sum(n * k for n, k in groups)


def _codewords(data, version):
    """The data codewords with their error correction, interleaved."""
    ec, groups = BLOCKS[version]
    blocks, at = [], 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(data[at:at + size])
            at += size
    checks = [_remainder(block, ec) for block in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):
        out.extend(b[i] for b in blocks if i < len(b))
    for i in range(ec):
        out.extend(c[i] for c in checks)
    return out


class _Matrix:
    def __init__(self, version):
        self.version = version
        self.size = version * 4 + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]
        self._draw_function_patterns()

    def _set(self, x, y, dark):
        self.modules[y][x] = dark
        self.function[y][x] = True

    def _draw_function_patterns(self):
        size = self.size
        for i in range(size):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for x, y in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    if 0 <= x + dx < size and 0 <= y + dy < size:
                        self._set(x + dx, y + dy, max(abs(dx), abs(dy)) not in (2, 4))
        positions = ALIGNMENT[self.version]
        for i, a in enumerate(positions):
            for j, b in enumerate(positions):
                if (i == 0 and j == 0) or (i == 0 and j == len(positions) - 1) or (i == len(positions) - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(a + dx, b + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format(0)
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.version << 12 | rem
            for i in range(18):
                bit = bool(bits >> i & 1)
                a, b = size - 11 + i % 3, i // 3
                self._set(a, b, bit)
                self._set(b, a, bit)

    def _draw_format(self, mask):
        size = self.size
        data = LEVEL_L << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: bool(bits >> i & 1)
        for i in range(6):
            self._set(8, i, bit(i))
        self._set(8, 7, bit(6))
        self._set(8, 8, bit(7))
        self._set(7, 8, bit(8))
        for i in range(9, 15):
            self._set(14 - i, 8, bit(i))
        for i in range(8):
            self._set(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._set(8, size - 15 + i, bit(i))
        self._set(8, size - 8, True)

    def place(self, codewords):
        size, i = self.size, 0
        total = len(codewords) * 8
        for right in range(size - 1, 0, -2):
            if right <= 6:
                right -= 1
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    y = (size - 1 - vert) if ((right + 1) & 2) == 0 else vert
                    if not self.function[y][x] and i < total:
                        self.modules[y][x] = bool(codewords[i >> 3] >> (7 - (i & 7)) & 1)
                        i += 1

    MASKS = (
        lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
        lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
        lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
        lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
    )

    def mask(self, number):
        rule = self.MASKS[number]
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and rule(x, y):
                    self.modules[y][x] = not self.modules[y][x]

    def penalty(self):
        size, m = self.size, self.modules
        score = 0
        lines = [row for row in m] + [[m[y][x] for y in range(size)] for x in range(size)]
        for line in lines:
            run = 1
            for i in range(1, size):
                if line[i] == line[i - 1]:
                    run += 1
                    continue
                if run >= 5:
                    score += 3 + run - 5
                run = 1
            if run >= 5:
                score += 3 + run - 5
            text = ''.join('1' if c else '0' for c in line)
            for pattern in ('00001011101', '10111010000'):       # a finder-like 1:1:3:1:1 run with four light beside it
                at = text.find(pattern)
                while at >= 0:
                    score += 40
                    at = text.find(pattern, at + 1)
        for y in range(size - 1):
            for x in range(size - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        total = size * size
        dark = sum(c for row in m for c in row)
        score += ((abs(dark * 20 - total * 10) + total - 1) // total - 1) * 10
        return score


def encode(text):
    """The QR code of `text` as rows of booleans; ValueError when it is too long."""
    raw = text.encode('utf-8')
    for version in range(1, 11):
        bits_for_count = 8 if version < 10 else 16
        if 4 + bits_for_count + 8 * len(raw) <= 8 * _capacity(version):
            break
    else:
        raise ValueError("too long for a QR code")
    bits = '0100' + format(len(raw), '0{}b'.format(bits_for_count)) + ''.join(format(b, '08b') for b in raw)
    room = 8 * _capacity(version)
    bits += '0' * min(4, room - len(bits))
    bits += '0' * (-len(bits) % 8)
    data = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    for i in range(_capacity(version) - len(data)):
        data.append(0xEC if i % 2 == 0 else 0x11)
    matrix = _Matrix(version)
    matrix.place(_codewords(data, version))
    best = None
    for number in range(8):
        matrix.mask(number)
        matrix._draw_format(number)
        score = matrix.penalty()
        if best is None or score < best[0]:
            best = (score, number)
        matrix.mask(number)
    matrix.mask(best[1])
    matrix._draw_format(best[1])
    return matrix.modules
