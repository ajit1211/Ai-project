"""NEXA Core: the Django control plane."""

# Load the Celery app with Django so that @shared_task binds to it.
from nexa_core.celery import app as celery_app

__all__ = ("celery_app",)
