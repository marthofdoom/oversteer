#!/usr/bin/env python3
"""Build data/telemetry/cars/dirt.json (the car names) from dr2_logger's car table.

    python3 research/dr2/car_table.py [car_data_dr2.py] > data/telemetry/cars/dirt.json

dr2_logger (ErlerPhilipp/dr2_logger, MIT, source/dirt_rally/car_data_dr2.py, commit 5e5fcbe) lists
[max rpm, idle rpm, gears, name] per car in rad/s; the decoder's car key is
dirt/<max rpm>-<idle rpm>-<gears> with the rpm rounded to 10 (telemetry_formats._codemasters). Cars that
share a key are listed together, in the table's order. The section comments of the source are the class."""
import json
import math
import re
import sys
import urllib.request

URL = 'https://raw.githubusercontent.com/ErlerPhilipp/dr2_logger/5e5fcbe3a2e2df5c366e11ec5aec942712e946ac/source/dirt_rally/car_data_dr2.py'
RAD_S = 30.0 / math.pi
LICENSE = ('MIT License\n\nCopyright (c) 2019-2020 ErlerPhilipp\n\nPermission is hereby granted, free of charge, to any person '
           'obtaining a copy\nof this software and associated documentation files (the "Software"), to deal\nin the Software '
           'without restriction, including without limitation the rights\nto use, copy, modify, merge, publish, distribute, '
           'sublicense, and/or sell\ncopies of the Software, and to permit persons to whom the Software is\nfurnished to do so, '
           'subject to the following conditions:\n\nThe above copyright notice and this permission notice shall be included in '
           'all\ncopies or substantial portions of the Software.\n\nTHE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY '
           'KIND, EXPRESS OR\nIMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\nFITNESS FOR A PARTICULAR '
           'PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\nAUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR '
           'OTHER\nLIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\nOUT OF OR IN CONNECTION WITH THE '
           'SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\nSOFTWARE.\n')


def main():
    text = open(sys.argv[1]).read() if len(sys.argv) > 1 else urllib.request.urlopen(URL).read().decode()
    names, group = {}, None
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r'^# (.+)$', line)
        if m and not line.startswith('# see') and not line.startswith('# max_rpm'):
            group = m.group(1)
            continue
        m = re.match(r"^\[([\d.]+), ([\d.]+), ([\d.]+), '([^']+)'\],", line)
        if not m:
            continue
        top, idle, gears = float(m.group(1)) * RAD_S, float(m.group(2)) * RAD_S, float(m.group(3))
        key = 'dirt/{:.0f}-{:.0f}-{:.0f}'.format(round(top, -1), round(idle, -1), gears)
        cand = {'name': m.group(4), 'class': group}
        if cand not in names.setdefault(key, []) and m.group(4) not in [c['name'] for c in names[key]]:
            names[key].append(cand)
    out = {
        'game': 'dirt',
        'title': 'DiRT Rally 2.0 car names',
        'keys': "The car key the decoder makes (telemetry_formats._codemasters): dirt/<max rpm>-<idle rpm>-<gears>, the rpm rounded to 10. "
                "The game sends no car name, so a key lists every car with that engine and gearbox, in dr2_logger's order; "
                "two or more are shown as 'A / B' until the learnt drivetrain or gearing tells them apart. class is the "
                "section dr2_logger lists the car under.",
        'notes': "Names, max rpm, idle rpm and gear count from dr2_logger (ErlerPhilipp/dr2_logger, "
                 "source/dirt_rally/car_data_dr2.py, 5e5fcbe); built by research/dr2/car_table.py. No torque curves or gearing, "
                 "so car_data.load() takes no cars from this file ('cars' is empty).",
        'licenses': [{'source': 'https://github.com/ErlerPhilipp/dr2_logger', 'text': LICENSE}],
        'cars': [],
        'names': names,
    }
    json.dump(out, sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
