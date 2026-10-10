"""Deployment environments shared by every service's configuration (doc 29.1)."""

from typing import Literal

Environment = Literal["development", "test", "staging", "production"]
