"""Application settings, read once from the environment.

Everything the application needs to run is supplied by compose.yml, so a reviewer never
has to create a .env file or export anything. The defaults below match those values, which
means the app also starts under `uvicorn app.main:app` outside Docker if a database is
reachable at the default URL.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    database_url: str = "postgresql://crm:local-development-only@db:5432/crm"

    # Where the supplied archive is mounted, read-only. Never read from anywhere else:
    # the reviewer replaces data/ before review, and the bind mount is the only path that
    # sees the replacement.
    crm_data_dir: Path = Path("/srv/data")

    app_version: str = "0.1.0"

    # Cache-busts static assets and identifies the build in the import ledger.
    page_size: int = 25

    # Bumping this triggers a readiness recompute at boot; see app/handoff/policy.py.
    handoff_policy_version: str = "4state-v1"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
