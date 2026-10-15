import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Tests must not pick up the real passphrase from GitHub Actions."""
    monkeypatch.delenv("PAGE_PASSPHRASE", raising=False)
    monkeypatch.delenv("PAGE_SALT", raising=False)
