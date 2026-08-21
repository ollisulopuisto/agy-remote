import pytest

import agy_remote.config as config_mod


@pytest.fixture(autouse=True)
def reset_config_singleton(monkeypatch):
    """Ensure every test starts and finishes with a clean config singleton."""
    config_mod.config_instance = None
    yield
    config_mod.config_instance = None
