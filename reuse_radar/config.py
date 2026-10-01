"""Runtime settings read from environment variables. No secrets live in code."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """A required setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    inspire_contact_email: str
    cache_dir: Path
    data_dir: Path

    @classmethod
    def from_env(cls, *, dotenv: bool = True) -> Settings:
        """Read settings from the environment, after loading `.env` from the working directory
        if present. Variables already set in the real environment take precedence."""
        if dotenv:
            # Explicit path: bare load_dotenv() searches from this module's directory, not the cwd.
            load_dotenv(Path.cwd() / ".env")
        email = os.environ.get("INSPIRE_CONTACT_EMAIL", "").strip()
        if "@" not in email:
            raise ConfigError(
                "INSPIRE_CONTACT_EMAIL must be set to a real contact address "
                "(INSPIRE requires an identifiable User-Agent). See .env.example."
            )
        return cls(
            inspire_contact_email=email,
            cache_dir=Path(os.environ.get("REUSE_RADAR_CACHE_DIR", ".cache")),
            data_dir=Path(os.environ.get("REUSE_RADAR_DATA_DIR", "data")),
        )
