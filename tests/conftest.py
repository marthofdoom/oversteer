import pytest

from oversteer import stage_tables


@pytest.fixture(autouse=True)
def _no_learnt_finishes():
    """The finish lines learnt from a database are process-wide (stage_tables.set_learnt): a test's own do not
    leak into the next one."""
    stage_tables.set_learnt({})
    yield
    stage_tables.set_learnt({})
