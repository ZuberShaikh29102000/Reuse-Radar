"""Django settings. Everything environment-specific comes from environment variables (or `.env`
for local development); nothing secret is committed. Missing required settings fail loudly."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"{name} must be set (see .env.example)")
    return value


def database_from_url(url: str) -> dict[str, Any]:
    """postgres://user:password@host:port/name?sslmode=require -> Django DATABASES entry."""
    parsed = urlparse(url)
    if parsed.scheme not in ("postgres", "postgresql"):
        raise ImproperlyConfigured(f"DATABASE_URL must be a postgres:// URL, got {parsed.scheme!r}")
    if not parsed.path.strip("/"):
        raise ImproperlyConfigured("DATABASE_URL has no database name")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or ""),
        "OPTIONS": dict(parse_qsl(parsed.query)),
        "CONN_MAX_AGE": 60,
    }


SECRET_KEY = _required("DJANGO_SECRET_KEY")
DEBUG = os.environ.get("DJANGO_DEBUG", "") == "1"
ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if h.strip()
]

INSTALLED_APPS = ["reuse_radar.api.apps.RadarConfig"]
MIDDLEWARE: list[str] = []
ROOT_URLCONF = "reuse_radar.api.urls"
DATABASES = {"default": database_from_url(_required("DATABASE_URL"))}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
