"""WSGI entrypoint for NEXA Core (Gunicorn in deployed environments)."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nexa_core.settings")

application = get_wsgi_application()
