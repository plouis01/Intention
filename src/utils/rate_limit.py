"""Shared slowapi Limiter instance.

Defined here (not in src.api.main) so route modules can import it without
creating a circular dependency with the app factory.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
