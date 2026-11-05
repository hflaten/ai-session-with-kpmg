from dataclasses import replace

import pytest

from app import auth

TEST_API_KEY = "test-key"


@pytest.fixture(autouse=True)
def fixed_api_key(monkeypatch):
    """Every test uses a known token instead of the real one from .env."""
    monkeypatch.setattr(auth, "settings", replace(auth.settings, app_api_key=TEST_API_KEY))
