import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Tests must not pick up the real passphrase from GitHub Actions."""
    monkeypatch.delenv("PAGE_PASSPHRASE", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)   # tests must never make paid API calls
    monkeypatch.delenv("PAGE_SALT", raising=False)
