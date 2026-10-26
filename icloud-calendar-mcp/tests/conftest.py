import pytest


@pytest.fixture(autouse=True)
def _no_real_creds(monkeypatch):
    # Tests never use the real iCloud account.
    for k in ("ICLOUD_APPLE_ID", "ICLOUD_APP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
