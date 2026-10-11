import importlib

import pytest

PACKAGES = [
    "iamagents",
    "iamagents.gateway",
    "iamagents.services",
    "iamagents.hooks",
    "eval",
]


@pytest.mark.parametrize("name", PACKAGES)
def test_package_imports(name):
    assert importlib.import_module(name)
