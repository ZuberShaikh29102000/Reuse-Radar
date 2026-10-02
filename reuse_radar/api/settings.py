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


def _csv(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


INSTALLED_APPS = [
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "reuse_radar.api.apps.RadarConfig",
]
# A public, read-only JSON API: no sessions, cookies, templates or admin, so none of that
# middleware is installed. CORS lets the Cloudflare Pages frontend (Phase 6) call it.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
]
ROOT_URLCONF = "reuse_radar.api.urls"
WSGI_APPLICATION = "reuse_radar.api.wsgi.application"
DATABASES = {"default": database_from_url(_required("DATABASE_URL"))}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"

CORS_ALLOWED_ORIGINS = _csv("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_METHODS = ["GET", "POST", "OPTIONS"]  # POST only for curator reviews

# Render terminates TLS at its proxy.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = not DEBUG and os.environ.get("DJANGO_SECURE_SSL_REDIRECT", "1") == "1"
SECURE_CONTENT_TYPE_NOSNIFF = True

REST_FRAMEWORK = {
    # Anonymous, read-only: no authentication backends, so django.contrib.auth is not needed.
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PAGINATION_CLASS": "reuse_radar.api.pagination.Pagination",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # Protects the free-tier instance and database from a single noisy client.
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.environ.get("API_RATE_LIMIT", "120/min"),
        "reviews": os.environ.get("REVIEW_RATE_LIMIT", "60/min"),
    },
}
SPECTACULAR_SETTINGS = {
    "TITLE": "Reuse Radar API",
    "DESCRIPTION": (
        "Reusable data products declared in high-energy-physics papers, and which of them are "
        "missing from HEPData. Read-only; every value is precomputed by the offline pipeline."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "reuse_radar.log.JsonFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}

# Only the OpenAPI docs page (Swagger UI from drf-spectacular) renders a template.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]
