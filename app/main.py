import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import router
from app.config import Settings
from app.database import Base, make_engine, make_session_factory
from app.errors import DomainError
from app.generator import generate_certificate
from app.storage import LocalStorage
from app.worker import EmbeddedRunner, ExternalRunner, JobWorker

log = logging.getLogger("certgen.api")


def build_runtime(settings: Settings, generator=None):
    """Shared by the API and the standalone worker so both are wired identically."""
    if settings.database_url.startswith("sqlite:///"):
        Path(settings.database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)  # NOTE: a real deployment would use Alembic migrations instead
    session_factory = make_session_factory(engine)
    storage = LocalStorage(settings.storage_dir)

    def make_worker(stop_event):
        return JobWorker(session_factory, storage, generator or generate_certificate,
                         lease_seconds=settings.lease_seconds, poll_interval=settings.poll_interval,
                         stop_event=stop_event)

    return engine, session_factory, storage, make_worker


def create_app(settings: Settings | None = None, runner_factory=None, generator=None) -> FastAPI:
    """App factory. `runner_factory` / `generator` let tests inject deterministic doubles."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine, session_factory, storage, make_worker = build_runtime(settings, generator)
        factory = runner_factory or (EmbeddedRunner if settings.worker_mode == "embedded" else ExternalRunner)
        runner = factory(make_worker, settings.worker_threads)

        app.state.settings = settings
        app.state.session_factory = session_factory
        app.state.storage = storage
        app.state.runner = runner
        runner.submit()  # pick up anything left queued / with an expired lease from before a restart
        yield
        runner.shutdown()  # workers finish the current certificate, release their job, and exit
        engine.dispose()

    app = FastAPI(title="Bulk Certificate Generator", version="2.0.0", lifespan=lifespan)

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        log.info("%s %s -> %s %.0fms rid=%s", request.method, request.url.path, response.status_code,
                 (time.perf_counter() - started) * 1000, request_id)
        return response

    app.include_router(router)
    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app()  # `uvicorn app.main:app`
