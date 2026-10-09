import os

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.generator import generate_certificate
from app.main import create_app
from app.worker import InlineRunner


def db_url(tmp_path, name="test.db") -> str:
    """Per-test database. Defaults to a throwaway SQLite file; set CERTGEN_TEST_DATABASE_URL to run the whole
    suite against another database (e.g. PostgreSQL). The target schema is dropped first so tests are isolated."""
    url = os.environ.get("CERTGEN_TEST_DATABASE_URL")
    if not url:
        return f"sqlite:///{tmp_path / name}"
    from app.database import Base, make_engine
    import app.models  # noqa: F401  (register tables)
    engine = make_engine(url)
    Base.metadata.drop_all(engine)
    engine.dispose()
    return url


class Harness:
    """Builds an app on a throwaway DB/storage dir. Jobs run inline (synchronously) for determinism."""

    def __init__(self, tmp_path, generator=None, **overrides):
        self.settings = Settings(
            database_url=db_url(tmp_path),
            storage_dir=tmp_path / "certs",
            **overrides,
        )
        self.app = create_app(self.settings, runner_factory=InlineRunner, generator=generator or generate_certificate)

    def __enter__(self):
        self.client = TestClient(self.app).__enter__()
        return self.client

    def __exit__(self, *exc):
        self.client.__exit__(*exc)


@pytest.fixture
def make_client(tmp_path):
    opened = []

    def _make(generator=None, **overrides):
        h = Harness(tmp_path, generator, **overrides)
        opened.append(h)
        return h.__enter__()

    yield _make
    for h in opened:
        h.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


def payload(*recipients, course="Python 101", **extra):
    return {"course_name": course, "recipients": list(recipients), **extra}


def person(name="Alice Smith", email="alice@example.com"):
    return {"name": name, "email": email}
