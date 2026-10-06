"""pytest 共享夹具。"""
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.parser import ParseEngine, to_plain          # noqa: E402
from app.storage import Storage                        # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_MD = FIXTURES / "sample_deck.md"
SAMPLE_JSON = FIXTURES / "sample_deck.expected.json"


@pytest.fixture
def sample_text() -> str:
    return SAMPLE_MD.read_text(encoding="utf-8")


@pytest.fixture
def sample_expected():
    import json
    return json.loads(SAMPLE_JSON.read_text(encoding="utf-8"))


@pytest.fixture
def engine():
    return ParseEngine()


@pytest.fixture
def store():
    s = Storage(":memory:")
    yield s
    s.close()
