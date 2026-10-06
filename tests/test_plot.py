"""The Telemetry tab's cairo drawing, rendered to an image and read back at known points."""
import cairo

from oversteer import telemetry_plot as plot


def pixel(surface, x, y):
    surface.flush()
    data = surface.get_data()
    i = y * surface.get_stride() + 4 * x
    return data[i + 2], data[i + 1], data[i]                    # r, g, b of BGRA


def colour(hex_):
    return int(hex_[1:3], 16), int(hex_[3:5], 16), int(hex_[5:7], 16)


def test_the_ribbon_has_a_cell_per_split_and_wider_sector_cells():
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 300, 10)
    plot.ribbon(cairo.Context(surface), 300, 10, ['gold', 'behind-lose', 'none'], ['ahead', 'behind'])
    assert pixel(surface, 5, 5) == colour(plot.TONES['gold'])
    assert pixel(surface, 100, 5) == colour(plot.TONES['behind-lose'])
    assert pixel(surface, 295, 5) == colour(plot.TONES['behind'])           # the last sector at the right edge
    assert pixel(surface, 295 - 23, 5) == colour(plot.TONES['ahead'])
    empty = cairo.ImageSurface(cairo.FORMAT_ARGB32, 100, 10)
    plot.ribbon(cairo.Context(empty), 100, 10, [])                          # nothing to draw is not an error


def test_the_shift_lights_follow_the_pages_rule():
    assert plot.lights_on(0, 7000) == (0, False) and plot.lights_on(6000, 0) == (0, False)
    assert plot.lights_on(4200, 7000) == (0, False)                         # the first at 60 % of the change-up point
    assert plot.lights_on(7000, 7000) == (15, True)                         # all lit and flashing at it
    assert plot.lights_on(5600, 7000)[0] == 8


def test_the_dash_draws_lights_and_survives_no_data():
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 420, 240)
    cr = cairo.Context(surface)
    plot.dash(cr, 420, 240, {'gear': 3, 'speed': 130.0, 'rpm': 6420.0, 'shift_rpm': 7000.0, 'learnt': True,
                             'progress': 0.5, 'km': '2.5 / 5.0 km', 'title': 'Stage'})
    assert pixel(surface, 30, 22) == colour(plot.LED_G)                      # the first light is on
    assert pixel(surface, 400, 22) == colour(plot.LED_OFF)                   # the last one is not yet
    for data in (None, {'gear': None, 'speed': None, 'rpm': 0.0, 'dim': True, 'title': 'No telemetry arriving'},
                 {'gear': -1, 'speed': 0.0, 'rpm': 900.0, 'progress': None}):
        plot.dash(cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 200, 120)), 200, 120, data)
