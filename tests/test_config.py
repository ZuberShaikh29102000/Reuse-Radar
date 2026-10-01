from __future__ import annotations

from pathlib import Path

import pytest

from reuse_radar.config import ConfigError, Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in ("INSPIRE_CONTACT_EMAIL", "REUSE_RADAR_CACHE_DIR", "REUSE_RADAR_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


def test_reads_dotenv_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "INSPIRE_CONTACT_EMAIL=a@example.org\nREUSE_RADAR_DATA_DIR=out\n", encoding="utf-8"
    )
    settings = Settings.from_env()
    assert settings.inspire_contact_email == "a@example.org"
    assert settings.data_dir == Path("out")
    assert settings.cache_dir == Path(".cache")


def test_real_environment_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("INSPIRE_CONTACT_EMAIL=file@example.org\n", encoding="utf-8")
    monkeypatch.setenv("INSPIRE_CONTACT_EMAIL", "env@example.org")
    assert Settings.from_env().inspire_contact_email == "env@example.org"


def test_missing_email_fails_loudly() -> None:
    with pytest.raises(ConfigError, match="INSPIRE_CONTACT_EMAIL"):
        Settings.from_env()
