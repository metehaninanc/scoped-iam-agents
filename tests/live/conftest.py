"""Live tests touch the tenant. They carry the `live` marker and only run with `-m live`."""

import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/live" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.live)
