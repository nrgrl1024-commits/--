import sys
from pathlib import Path

import pytest

pytest.importorskip("flask")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
