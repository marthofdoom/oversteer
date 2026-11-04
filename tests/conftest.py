import pytest

from oversteer import ffb_engine, ffb_carprops, rbr_fingerprint, rbr_install, stage_tables


@pytest.fixture(autouse=True)
def _no_learnt_finishes():
    """The finish lines learnt from a database are process-wide (stage_tables.set_learnt): a test's own do not
    leak into the next one."""
    stage_tables.set_learnt({})
    yield
    stage_tables.set_learnt({})


@pytest.fixture(autouse=True)
def _no_stall_dump_file(monkeypatch):
    """The engine thread writes no engine-stalls.log in the real log folder."""
    monkeypatch.setattr(ffb_engine.FfbEngine, 'STALL_DUMPS', False)


@pytest.fixture(autouse=True)
def _no_real_rbr_install(monkeypatch):
    """No test reads (or builds a cache from) a Richard Burns Rally install of the machine it runs on; the ones
    about it point OVERSTEER_RBR_DIR at a folder of their own."""
    monkeypatch.delenv(rbr_install.ENV, raising=False)
    monkeypatch.setattr(rbr_install, 'CANDIDATES', ())
    monkeypatch.setattr(rbr_install, 'STEAM_SHORTCUTS', ())
    monkeypatch.setattr(rbr_install, 'LUTRIS_GAMES', ())
    monkeypatch.setattr(rbr_install, 'ASYNC', False)          # (a search on a thread of its own is tested as such)
    rbr_install.forget()
    yield
    rbr_install.forget()


@pytest.fixture(autouse=True)
def _own_rbr_car_library(tmp_path):
    """The RBR cars told from their gearing (rbr_fingerprint) are read from a library of the test's own, never the
    user's."""
    rbr_fingerprint.set_library_path(str(tmp_path / 'rbr-cars.json'))
    yield
    rbr_fingerprint.set_library_path(None)


@pytest.fixture
def without_real_lines():
    """The stage tables as they were before the game's real sector lines were shipped: no `sector_lines_*`, so a
    stage's start, finish and sectors come from the learnt or shipped finish, the pace notes and the sector
    lengths (the fallbacks those tests are about)."""
    tables = stage_tables.load()
    for entries in tables.values():
        for entry in entries.values():
            for name in [n for n in entry if n.startswith('sector_lines_')]:
                del entry[name]
    stage_tables.set_tables(tables)
    yield
    stage_tables.set_tables(None)


@pytest.fixture(autouse=True)
def _own_ffb_car_props(tmp_path):
    """The per-car trail and scrub the FFB learns are kept in a file of the test's own, never the user's."""
    ffb_carprops.STORE.use(str(tmp_path / 'ffb_car_props.json'))
    ffb_carprops.ASYNC = False
    yield
    ffb_carprops.STORE.use(None)
    ffb_carprops.ASYNC = True


@pytest.fixture(autouse=True)
def _rally_power_class():
    """The assist class is process-wide (ffb_carprops.set_power_class): every test starts on the default, rally."""
    ffb_carprops.set_power_class(None)
    yield
    ffb_carprops.set_power_class(None)


@pytest.fixture
def street():
    """The street class (top end 3.7 Nm): for the tests whose numbers are the commuter car's."""
    ffb_carprops.set_power_class('street')


@pytest.fixture(autouse=True)
def _own_rbr_strength(tmp_path, monkeypatch):
    """The RBR STRENGTH kept between sessions is a file of the test's own, never the user's."""
    from oversteer import ffb_rbr
    monkeypatch.setattr(ffb_rbr, 'STRENGTH_PATH', str(tmp_path / 'rbr_strength.json'))
