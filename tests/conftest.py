import pytest

from oversteer import stage_tables


@pytest.fixture(autouse=True)
def _no_learnt_finishes():
    """The finish lines learnt from a database are process-wide (stage_tables.set_learnt): a test's own do not
    leak into the next one."""
    stage_tables.set_learnt({})
    yield
    stage_tables.set_learnt({})


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
