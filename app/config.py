from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CERTGEN_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/certgen.db"
    storage_dir: Path = Path("./data/certificates")

    # Request limits: protect memory and request time.
    max_recipients: int = 5000
    max_name_length: int = 100

    # Workers.
    # embedded: API process also runs worker threads (zero-setup dev).
    # external: API only enqueues; run `python -m app.worker_main` separately (scale-out / production).
    worker_mode: Literal["embedded", "external"] = "embedded"
    worker_threads: int = 2
    # A claimed job is leased for this long and the lease is renewed after every certificate.
    # If a worker dies, another one takes the job over once the lease expires.
    # Must comfortably exceed the time to render ONE certificate.
    lease_seconds: float = 60
    poll_interval: float = 0.5
