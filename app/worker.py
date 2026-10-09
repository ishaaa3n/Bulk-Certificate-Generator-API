"""Background processing.

The database is the queue. A job row is claimed with a *lease*:

    claimable = status == queued
                OR (status == processing AND lease expired)

A worker claims a job with one conditional UPDATE (compare-and-set), renews the lease after each
certificate, and finishes it. If a worker dies, its lease expires and any other worker takes the job over,
redoing only the unfinished certificates. No in-memory state is needed, so workers can be threads in the API
process, separate processes, or separate machines, with no coordination beyond the database.
"""
import logging
import os
import socket
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.generator import CertificateData, generate_certificate
from app.models import Certificate, CertStatus, ErrorCode, Job, JobStatus
from app.service import final_status, get_counts
from app.storage import LocalStorage

log = logging.getLogger("certgen.worker")

Generator = Callable[[CertificateData, Path], None]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LeaseLost(Exception):
    """This worker no longer owns the job (its lease expired and another worker claimed it)."""


class JobWorker:
    def __init__(
        self,
        session_factory: sessionmaker,
        storage: LocalStorage,
        generator: Generator = generate_certificate,
        lease_seconds: float = 60,
        poll_interval: float = 0.5,
        stop_event: threading.Event | None = None,
    ):
        self.session_factory = session_factory
        self.storage = storage
        self.generator = generator
        self.lease = timedelta(seconds=lease_seconds)
        self.poll_interval = poll_interval
        self.stop_event = stop_event or threading.Event()
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._token: str | None = None  # set by claim(); every protected write is fenced on it

    # ------------------------------------------------------------------ claiming

    def claim(self, db: Session, job_id: str | None = None) -> str | None:
        """Atomically take ownership of the oldest claimable job (or a specific one). Returns its id."""
        now = _now()
        token = f"{self.worker_id}:{uuid.uuid4().hex[:8]}"  # fencing token: unique per *claim*, not per worker
        claimable = or_(
            Job.status == JobStatus.QUEUED,
            and_(Job.status == JobStatus.PROCESSING, or_(Job.lease_expires_at.is_(None), Job.lease_expires_at < now)),
        )
        query = select(Job.id).where(claimable)
        if job_id:
            query = query.where(Job.id == job_id)
        for candidate in db.scalars(query.order_by(Job.created_at).limit(5)).all():
            # Compare-and-set: the WHERE re-checks claimability, so of N racing workers exactly one gets rowcount 1.
            won = db.execute(
                update(Job).where(Job.id == candidate, claimable).values(
                    status=JobStatus.PROCESSING, locked_by=token,
                    lease_expires_at=now + self.lease, started_at=func.coalesce(Job.started_at, now),
                )
            ).rowcount
            db.commit()
            if won:
                self._token = token
                # Taking over from a dead worker: anything it left half-done goes back to pending.
                db.execute(update(Certificate).where(Certificate.job_id == candidate,
                                                     Certificate.status == CertStatus.PROCESSING)
                           .values(status=CertStatus.PENDING))
                db.commit()
                return candidate
        return None

    def _owns_job(self, job_id: str):
        """SQL predicate: 'the job is still leased to *my current claim*'. Used to fence every protected write."""
        return exists().where(Job.id == job_id, Job.locked_by == self._token, Job.status == JobStatus.PROCESSING)

    def _heartbeat(self, db: Session, job_id: str) -> None:
        """Renew the lease. Raises LeaseLost if someone else owns the job now."""
        renewed = db.execute(
            update(Job).where(Job.id == job_id, Job.locked_by == self._token, Job.status == JobStatus.PROCESSING)
            .values(lease_expires_at=_now() + self.lease)
        ).rowcount
        db.commit()
        if not renewed:
            raise LeaseLost(job_id)

    def _write_cert(self, db: Session, job_id: str, cert_id: str, **values) -> None:
        """Update a certificate ONLY IF we still own the job, in the same statement (no check-then-act gap).

        A stale worker (lease expired mid-render) therefore cannot overwrite what the new owner recorded.
        """
        done = db.execute(
            update(Certificate).where(Certificate.id == cert_id, self._owns_job(job_id)).values(**values)
        ).rowcount
        db.commit()
        if not done:
            raise LeaseLost(job_id)

    # ----------------------------------------------------------------- processing

    def run_once(self, job_id: str | None = None) -> bool:
        """Claim and fully process one job. Returns False if there was nothing to do."""
        with self.session_factory() as db:
            claimed = self.claim(db, job_id)
            if not claimed:
                return False
            try:
                self._process(db, claimed)
            except LeaseLost:
                db.rollback()  # another worker owns the job now; touch nothing
                log.warning("job %s: lease lost, worker %s backing off", claimed, self.worker_id)
            except Exception:  # infrastructure failure (DB error, disk full...): never leave a job stuck
                log.exception("job %s aborted (worker %s)", claimed, self.worker_id)
                db.rollback()
                self._finish(db, claimed, aborted=True)
            return True

    process = run_once  # run_once(job_id) targets a specific job; used by tests and tooling

    def _process(self, db: Session, job_id: str) -> None:
        """One certificate = one try/except = one commit: failures are isolated and progress is durable/visible."""
        job = db.get(Job, job_id)
        pending_ids = db.scalars(
            select(Certificate.id)
            .where(Certificate.job_id == job_id, Certificate.status == CertStatus.PENDING)
            .order_by(Certificate.row_index)
        ).all()  # ids only: bounded memory even for huge jobs
        log.info("job %s: %d certificates to generate (worker %s)", job_id, len(pending_ids), self.worker_id)

        for cert_id in pending_ids:
            if self.stop_event.is_set():  # graceful shutdown: hand the job back instead of abandoning a lease
                self._release(db, job_id)
                return
            name = db.scalar(select(Certificate.name).where(Certificate.id == cert_id))
            self._write_cert(db, job_id, cert_id, status=CertStatus.PROCESSING, attempts=Certificate.attempts + 1)

            key = LocalStorage.key_for(job_id, cert_id)
            try:
                self.generator(
                    CertificateData(certificate_id=cert_id, name=name, course_name=job.course_name,
                                    issue_date=job.issue_date, issuer=job.issuer),
                    self.storage.write_path(key),
                )
                outcome = dict(status=CertStatus.COMPLETED, file_path=key, error_code=None, error_message=None)
            except Exception as e:
                log.warning("job %s certificate %s failed: %s", job_id, cert_id, e)
                outcome = dict(status=CertStatus.FAILED, error_code=ErrorCode.GENERATION,
                               error_message=f"{type(e).__name__}: {e}"[:500])
            self._write_cert(db, job_id, cert_id, completed_at=_now(), **outcome)
            self._heartbeat(db, job_id)
        self._finish(db, job_id)

    def _finish(self, db: Session, job_id: str, aborted: bool = False) -> None:
        if aborted:  # leftover rows would otherwise sit in pending/processing forever
            db.execute(
                update(Certificate)
                .where(Certificate.job_id == job_id, self._owns_job(job_id),
                       Certificate.status.in_([CertStatus.PENDING, CertStatus.PROCESSING]))
                .values(status=CertStatus.FAILED, error_code=ErrorCode.GENERATION,
                        error_message="job aborted before this certificate was generated", completed_at=_now())
            )
        counts = get_counts(db, job_id)
        finished = db.execute(  # only the current lease holder may finish the job
            update(Job).where(Job.id == job_id, Job.locked_by == self._token, Job.status == JobStatus.PROCESSING)
            .values(status=final_status(counts), finished_at=_now(), locked_by=None, lease_expires_at=None)
        ).rowcount
        db.commit()
        if finished:
            log.info("job %s finished: %s", job_id, counts.model_dump())

    def _release(self, db: Session, job_id: str) -> None:
        db.execute(update(Certificate).where(Certificate.job_id == job_id, self._owns_job(job_id),
                                             Certificate.status == CertStatus.PROCESSING)
                   .values(status=CertStatus.PENDING))
        db.execute(update(Job).where(Job.id == job_id, Job.locked_by == self._token)
                   .values(status=JobStatus.QUEUED, locked_by=None, lease_expires_at=None))
        db.commit()
        log.info("job %s released back to the queue (shutdown)", job_id)

    # ------------------------------------------------------------------ main loop

    def run_forever(self, wake: threading.Event | None = None) -> None:
        wake = wake or threading.Event()
        log.info("worker %s started", self.worker_id)
        while not self.stop_event.is_set():
            try:
                worked = self.run_once()
            except Exception:
                log.exception("worker loop error")
                worked = False
            if not worked:
                wake.wait(self.poll_interval)  # woken early by the API for low latency, else poll
                wake.clear()
        log.info("worker %s stopped", self.worker_id)


# ---------------------------------------------------------------------- runners
# A "runner" is how the API process relates to workers. All implement submit() and shutdown().

MakeWorker = Callable[[threading.Event], JobWorker]


class EmbeddedRunner:
    """Worker threads inside the API process. submit() just wakes them; the job is already in the DB."""

    def __init__(self, make_worker: MakeWorker, threads: int):
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.workers = [make_worker(self.stop) for _ in range(threads)]
        self.threads = [threading.Thread(target=w.run_forever, args=(self.wake,), name=f"certgen-{i}", daemon=True)
                        for i, w in enumerate(self.workers)]
        for t in self.threads:
            t.start()

    def submit(self, job_id: str | None = None) -> None:
        self.wake.set()

    def shutdown(self) -> None:
        self.stop.set()
        self.wake.set()
        for t in self.threads:
            t.join(timeout=30)


class ExternalRunner:
    """API only enqueues; separate `python -m app.worker_main` processes do the work."""

    def __init__(self, make_worker: MakeWorker, threads: int = 0):
        pass

    def submit(self, job_id: str | None = None) -> None:
        pass

    def shutdown(self) -> None:
        pass


class InlineRunner:
    """Runs jobs synchronously inside submit(). Used by tests for determinism."""

    def __init__(self, make_worker: MakeWorker, threads: int = 0):
        self.processor = make_worker(threading.Event())

    def submit(self, job_id: str | None = None) -> None:
        while self.processor.run_once():
            pass

    def shutdown(self) -> None:
        pass
