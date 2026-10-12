import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("HOMEMESH_MOCK_DELAY", "0.005")


@pytest.fixture
def home(tmp_path):
    from harness import paths_for

    p = paths_for(tmp_path / "home")
    p.ensure()
    return p
