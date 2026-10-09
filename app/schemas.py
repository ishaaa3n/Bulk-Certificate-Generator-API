from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course_name: str = Field(min_length=1, max_length=200)
    issuer: str | None = Field(default=None, max_length=200)
    issue_date: date | None = None  # defaults to today (server date)
    # Rows are intentionally untyped: they're validated one-by-one so a bad row can't sink the batch.
    # The max size is enforced against settings in the route (it is configurable).
    recipients: list[Any] = Field(min_length=1)

    @field_validator("course_name")
    @classmethod
    def _clean_course(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("course_name must not be blank")
        return v

    @field_validator("issuer")
    @classmethod
    def _clean_issuer(cls, v: str | None) -> str | None:
        return " ".join(v.split()) or None if v else None


class JobCounts(BaseModel):
    pending: int = 0
    processing: int = 0
    completed: int = 0
    failed: int = 0


class JobOut(BaseModel):
    id: str
    status: str
    course_name: str
    issuer: str | None
    issue_date: date
    total: int
    counts: JobCounts
    progress_percent: float
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    links: dict[str, str]


class CertificateOut(BaseModel):
    id: str
    row_index: int
    name: str | None
    email: str | None
    status: str
    attempts: int
    error_code: str | None
    error_message: str | None
    download_url: str | None
    completed_at: datetime | None


class CertificatePage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CertificateOut]
