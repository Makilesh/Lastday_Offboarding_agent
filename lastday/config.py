"""Settings read from the environment, loaded from .env when present."""

import os

from dotenv import load_dotenv

# Only repositories carrying this topic are ever read or changed.
SCOPE_TOPIC = "lastday-demo"


class ConfigError(RuntimeError):
    pass


def require_env(name: str) -> str:
    """Return a required setting, or fail with a message that says how to fix it."""
    load_dotenv()
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value
