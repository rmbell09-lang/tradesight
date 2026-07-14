"""Suite-wide resource hygiene for CPython versions with less eager GC."""

import gc

import pytest


@pytest.fixture(autouse=True)
def collect_test_resources():
    """Collect test-owned SQLite/context objects after every isolated test."""
    yield
    gc.collect()
