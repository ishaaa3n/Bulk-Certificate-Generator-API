import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Date, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base, UTCDateTime


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus:
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"                      # every row succeeded
    COMPLETED_WITH_ERRORS = "completed_with_errors"  # mixed
    FAILED = "failed"                            # every row failed


class CertStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ErrorCode:
    VALIDATION = "validation_error"
    DUPLICATE = "duplicate_recipient"
    GENERATION = "generation_error"


class Job(Base):
    __tablename__ = "jobs"
    # Workers poll "queued, or processing with an expired lease" ordered by age.
    __table_args__ = (Index("ix_jobs_claim", "status", "lease_expires_at", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.QUEUED)
    course_name: Mapped[str] = mapped_column(String(200))
    issuer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    issue_date: Mapped[date] = mapped_column(Date)
    total: Mapped[int] = mapped_column(Integer)
    # Idempotency: same key + same payload returns the same job instead of a duplicate batch.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Lease (see worker.py): which worker owns the job and until when. NULL/expired => claimable.
    locked_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    certificates: Mapped[list["Certificate"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class Certificate(Base):
    __tablename__ = "certificates"
    __table_args__ = (
        UniqueConstraint("job_id", "row_index"),
        Index("ix_cert_job_status", "job_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    row_index: Mapped[int] = mapped_column(Integer)  # position in the client's request, 0-based
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    raw_input: Mapped[str] = mapped_column(Text)  # original row as JSON, so failures are traceable
    status: Mapped[str] = mapped_column(String(16), default=CertStatus.PENDING)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)  # storage key, relative to storage root
    attempts: Mapped[int] = mapped_column(Integer, default=0)  # generation attempts (grows with each retry)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    job: Mapped[Job] = relationship(back_populates="certificates")
