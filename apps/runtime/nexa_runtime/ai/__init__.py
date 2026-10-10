"""Runtime's side of the AI Gateway: catalog, quotas, usage ledger and the `/ai` routes.

The gateway itself is the framework-free `nexa_ai` package; this module connects it to
PostgreSQL, Redis and FastAPI.
"""
