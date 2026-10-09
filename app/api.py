"""HTTP layer only: parse/validate the request, call the service, shape the response. No business logic here."""
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import service
from app.models import Certificate, CertStatus
from app.schemas import CertificateOut, CertificatePage, JobCreate, JobOut

router = APIRouter()


def get_db(request: Request):
    with request.app.state.session_factory() as db:
        yield db


def _cert_out(cert: Certificate) -> CertificateOut:
    url = f"/api/v1/certificates/{cert.id}/download" if cert.status == CertStatus.COMPLETED else None
    return CertificateOut(
        id=cert.id, row_index=cert.row_index, name=cert.name, email=cert.email, status=cert.status,
        attempts=cert.attempts, error_code=cert.error_code, error_message=cert.error_message,
        download_url=url, completed_at=cert.completed_at,
    )


# ------------------------------------------------------------------ ops

@router.get("/health", tags=["ops"])
def health():
    """Liveness: the process is up."""
    return {"status": "ok"}


@router.get("/ready", tags=["ops"])
def ready(request: Request, response: Response):
    """Readiness: dependencies (database, storage) are usable. Use this for load-balancer checks."""
    checks = {}
    try:
        with request.app.state.session_factory() as db:
            db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"
    checks["storage"] = "ok" if request.app.state.storage.check_writable() else "unavailable"
    if "unavailable" in checks.values():
        response.status_code = 503
    return {"status": "ready" if response.status_code != 503 else "not_ready", **checks}


# ------------------------------------------------------------------ jobs

@router.post("/api/v1/jobs", response_model=JobOut, status_code=202, tags=["jobs"])
def submit_job(
    payload: JobCreate,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, max_length=128),
):
    """Accept a bulk request. Returns immediately; generation happens in the background."""
    job, created = service.create_job(db, payload, request.app.state.settings, idempotency_key)
    if created:
        request.app.state.runner.submit(job.id)  # a wake-up hint; the job is already durable in the DB
    else:
        response.status_code = 200  # replay of an earlier identical request
    db.expire_all()  # re-read: an inline runner may already have finished the job
    response.headers["Location"] = f"/api/v1/jobs/{job.id}"
    return service.to_job_out(db, job)


@router.get("/api/v1/jobs/{job_id}", response_model=JobOut, tags=["jobs"])
def job_status(job_id: str, db: Session = Depends(get_db)):
    return service.to_job_out(db, service.get_job(db, job_id))


@router.post("/api/v1/jobs/{job_id}/retry", response_model=JobOut, status_code=202, tags=["jobs"])
def retry_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    """Re-run the certificates that failed during generation (not input-validation failures)."""
    job = service.retry_failed(db, job_id)
    request.app.state.runner.submit(job.id)
    db.expire_all()
    return service.to_job_out(db, job)


@router.get("/api/v1/jobs/{job_id}/certificates", response_model=CertificatePage, tags=["certificates"])
def list_certificates(
    job_id: str,
    status: str | None = Query(default=None, pattern="^(pending|processing|completed|failed)$"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    total, rows = service.list_certificates(db, job_id, status, limit, offset)
    return CertificatePage(total=total, limit=limit, offset=offset, items=[_cert_out(c) for c in rows])


@router.get("/api/v1/certificates/{certificate_id}/download", tags=["certificates"])
def download_certificate(certificate_id: str, request: Request, db: Session = Depends(get_db)):
    cert, path = service.certificate_file(db, request.app.state.storage, certificate_id)
    return FileResponse(path, media_type="application/pdf", filename=f"certificate_{cert.row_index + 1:05d}.pdf")


@router.get("/api/v1/jobs/{job_id}/download", tags=["certificates"])
def download_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    """All successfully generated certificates of a finished job as one ZIP."""
    tmp = service.build_zip(db, request.app.state.storage, job_id)

    def stream():
        try:
            while chunk := tmp.read(64 * 1024):
                yield chunk
        finally:
            tmp.close()

    return StreamingResponse(stream(), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="certificates_{job_id}.zip"'})
