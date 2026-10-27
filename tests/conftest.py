import pytest

from oversteer import rbr_install, stage_tables


@pytest.fixture(autouse=True)
def _no_learnt_finishes():
    """The finish lines learnt from a database are process-wide (stage_tables.set_learnt): a test's own do not
    leak into the next one."""
    stage_tables.set_learnt({})
    yield
    stage_tables.set_learnt({})


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
