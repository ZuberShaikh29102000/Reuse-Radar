"""WSGI entry point for gunicorn (Render)."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "reuse_radar.api.settings")
application = get_wsgi_application()
