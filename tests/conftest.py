import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from helpers import make_cfg  # noqa: E402,F401  (tests import it from here too)
from beaconview.store import Store  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "data" / "beaconview.duckdb"), "256MB", 2)
    yield s
    s.close()
