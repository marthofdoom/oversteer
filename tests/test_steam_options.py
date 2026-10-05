from oversteer import steam_options


def test_wheel_options_are_text_only():
    assert steam_options.WHEEL_OPTIONS == 'SDL_JOYSTICK_HIDAPI=0 %command%'
    assert 'PROTON_LOG' not in steam_options.WHEEL_OPTIONS
    assert 'PROTON_LOG=1' in steam_options.WHEEL_TOOLTIP


def test_shared_memory_combination_reuses_the_run_path():
    shared = '/opt/oversteer/oversteer-run %command%'
    assert steam_options.shared_memory_combined(shared) == \
        'SDL_JOYSTICK_HIDAPI=0 /opt/oversteer/oversteer-run %command%'
    assert 'SDL_JOYSTICK_HIDAPI=0 /opt/oversteer/oversteer-run %command%' in steam_options.guidance(shared)
    assert 'Steam Input' in steam_options.guidance(shared)


def test_no_combination_without_a_path():
    missing = 'oversteer-run was not found: install Oversteer'
    assert steam_options.shared_memory_combined(missing) is None
    assert 'shared-memory' not in steam_options.guidance(missing)
