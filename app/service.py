import hashlib
import json
from datetime import date, datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import Conflict, Gone, NotFound, PayloadTooLarge
from app.models import Certificate, CertStatus, ErrorCode, Job, JobStatus
from app.schemas import JobCounts, JobCreate, JobOut
from app.validation import RowError, validate_row


def _hash_payload(payload: JobCreate) -> str:
    # Hash the client's payload *before* defaults (issue_date=today) are applied,
    # so a retry after midnight still matches.
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _find_replay(db: Session, key: str, request_hash: str) -> Job | None:
    existing = db.scalar(select(Job).where(Job.idempotency_key == key))
    if existing and existing.request_hash != request_hash:
        raise Conflict("Idempotency-Key was already used with a different request body")
    return existing


def create_job(db: Session, payload: JobCreate, settings: Settings, idempotency_key: str | None) -> tuple[Job, bool]:
    """Validate every row, persist the job + one certificate row per recipient.

    Returns (job, created). created=False means an idempotent replay.
    Invalid / duplicate rows are stored as FAILED rows (never silently dropped),
    so the client can see exactly which input rows were rejected and why.
    """
    if len(payload.recipients) > settings.max_recipients:
        raise PayloadTooLarge(
            f"too many recipients ({len(payload.recipients)}); the maximum per job is {settings.max_recipients}")
    request_hash = _hash_payload(payload)

    if idempotency_key:
        existing = _find_replay(db, idempotency_key, request_hash)
        if existing:
            return existing, False

    job = Job(
        course_name=payload.course_name,
        issuer=payload.issuer,
        issue_date=payload.issue_date or date.today(),
        total=len(payload.recipients),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    seen_emails: set[str] = set()
    certs: list[Certificate] = []
    for index, row in enumerate(payload.recipients):
        cert = Certificate(job_id=job.id, row_index=index, raw_input=_safe_json(row), status=CertStatus.PENDING)
        try:
            clean = validate_row(row, settings.max_name_length)
        except RowError as e:
            cert.status, cert.error_code, cert.error_message = CertStatus.FAILED, ErrorCode.VALIDATION, str(e)
            if isinstance(row, dict):
                cert.name = _short(row.get("name"), 200)
                cert.email = _short(row.get("email"), 320)
        else:
            cert.name, cert.email = clean.name, clean.email
            if clean.email in seen_emails:
                cert.status = CertStatus.FAILED
                cert.error_code = ErrorCode.DUPLICATE
                cert.error_message = f"duplicate email '{clean.email}' in this job (first occurrence wins)"
            else:
                seen_emails.add(clean.email)
        if cert.status == CertStatus.FAILED:
            cert.completed_at = datetime.now(timezone.utc)
        certs.append(cert)

    if all(c.status == CertStatus.FAILED for c in certs):
        job.status = JobStatus.FAILED  # nothing to generate; no point queueing
        job.finished_at = datetime.now(timezone.utc)

    job.certificates = certs
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        # Two concurrent requests with the same Idempotency-Key: the unique constraint picked a winner.
        db.rollback()
        if idempotency_key:
            existing = _find_replay(db, idempotency_key, request_hash)
            if existing:
                return existing, False
        raise
    return job, True


def _safe_json(row) -> str:
    try:
        text = json.dumps(row, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = json.dumps(str(row))
    return text[:2000]  # don't let a giant row bloat the DB


def _short(value, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def get_counts(db: Session, job_id: str) -> JobCounts:
    rows = db.execute(
        select(Certificate.status, func.count()).where(Certificate.job_id == job_id).group_by(Certificate.status)
    ).all()
    return JobCounts(**{status: n for status, n in rows})


def final_status(counts: JobCounts) -> str:
    if counts.completed == 0:
        return JobStatus.FAILED
    return JobStatus.COMPLETED if counts.failed == 0 else JobStatus.COMPLETED_WITH_ERRORS


def to_job_out(db: Session, job: Job) -> JobOut:
    counts = get_counts(db, job.id)
    done = counts.completed + counts.failed
    return JobOut(
        id=job.id,
        status=job.status,
        course_name=job.course_name,
        issuer=job.issuer,
        issue_date=job.issue_date,
        total=job.total,
        counts=counts,
        progress_percent=round(100 * done / job.total, 2) if job.total else 100.0,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        links={
            "self": f"/api/v1/jobs/{job.id}",
            "certificates": f"/api/v1/jobs/{job.id}/certificates",
            "download_all": f"/api/v1/jobs/{job.id}/download",
        },
    )


# ----------------------------------------------------------------------------- queries


def get_job(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if not job:
        raise NotFound("job not found")
    return job


def list_certificates(db: Session, job_id: str, status: str | None, limit: int, offset: int):
    get_job(db, job_id)
    query = select(Certificate).where(Certificate.job_id == job_id)
    if status:
        query = query.where(Certificate.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(Certificate.row_index).limit(limit).offset(offset)).all()
    return total, rows


def certificate_file(db: Session, storage, certificate_id: str):
    """Return (certificate, path) for a downloadable certificate, or raise the right domain error."""
    cert = db.get(Certificate, certificate_id)
    if not cert:
        raise NotFound("certificate not found")
    if cert.status != CertStatus.COMPLETED:
        detail = cert.error_message if cert.status == CertStatus.FAILED else f"certificate is {cert.status}"
        raise Conflict(f"certificate is not available: {detail}")
    if not storage.is_safe_key(cert.file_path):
        raise NotFound("certificate file not found")  # a stored path escaping the storage root is never served
    path = storage.existing_file(cert.file_path)
    if path is None:
        raise Gone("certificate file is no longer available on disk")
    return cert, path


def build_zip(db: Session, storage, job_id: str):
    """ZIP of the successfully generated certificates of a finished job, spooled to disk. Caller closes it."""
    import re
    import tempfile
    import zipfile

    job = get_job(db, job_id)
    if job.status in (JobStatus.QUEUED, JobStatus.PROCESSING):
        raise Conflict(f"job is still {job.status}; poll the job status and retry when it has finished")
    certs = db.scalars(
        select(Certificate).where(Certificate.job_id == job_id, Certificate.status == CertStatus.COMPLETED)
        .order_by(Certificate.row_index)
    ).all()
    if not certs:
        raise Conflict("no certificates were generated for this job")

    tmp = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)  # disk beyond 8 MB, not RAM
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for c in certs:
            path = storage.existing_file(c.file_path)
            if path is None:
                continue  # file lost on disk: skip rather than fail the whole archive
            slug = re.sub(r"[^A-Za-z0-9]+", "_", c.name or "").strip("_")[:50] or "recipient"
            zf.write(path, f"{c.row_index + 1:05d}_{slug}.pdf")  # row number keeps entry names unique
    tmp.seek(0)
    return tmp


def retry_failed(db: Session, job_id: str) -> Job:
    """Re-queue the certificates that failed during *generation*.

    Validation / duplicate failures are properties of the input and can never succeed on retry, so they stay failed.
    """
    job = get_job(db, job_id)
    if job.status in (JobStatus.QUEUED, JobStatus.PROCESSING):
        raise Conflict(f"job is still {job.status}; retry is only possible once it has finished")
    reset = db.execute(
        update(Certificate)
        .where(Certificate.job_id == job_id, Certificate.status == CertStatus.FAILED,
               Certificate.error_code == ErrorCode.GENERATION)
        .values(status=CertStatus.PENDING, error_code=None, error_message=None, completed_at=None)
    ).rowcount
    if not reset:
        db.rollback()
        raise Conflict("no retryable certificates (only generation failures can be retried)")
    # Conditional on the status we read, so two concurrent retry calls cannot both re-queue the job.
    won = db.execute(
        update(Job).where(Job.id == job_id, Job.status == job.status)
        .values(status=JobStatus.QUEUED, finished_at=None, locked_by=None, lease_expires_at=None)
    ).rowcount
    if not won:
        db.rollback()
        raise Conflict("job state changed concurrently; retry the request")
    db.commit()
    db.refresh(job)
    return job
