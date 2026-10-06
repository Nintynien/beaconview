import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from beaconview import config  # noqa: E402
from beaconview.store import Store  # noqa: E402


def make_cfg(tmp_path, **over):
    env = {
        "ACCESS_LOG": str(tmp_path / "access.log"),
        "INSTANCE_NAME": "edge",
        "DATA_DIR": str(tmp_path / "data"),
        "BATCH_SECONDS": "0.1",
    }
    env.update({k: str(v) for k, v in over.items()})
    cfg = config.load(env)
    # quicker loops for tests
    return config.Config(**{**cfg.__dict__, "poll_seconds": 0.02, "rotate_grace_seconds": 0.3,
                            "rollup_seconds": 0.0})


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "data" / "beaconview.duckdb"), "256MB", 2)
    yield s
    s.close()
