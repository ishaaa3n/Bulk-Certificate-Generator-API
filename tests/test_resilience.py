"""Crash / concurrency scenarios: the things that are easy to claim and hard to get right."""
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.main import create_app
from app.models import Certificate, CertStatus, Job, JobStatus
from app.worker import InlineRunner
from tests.conftest import db_url, payload, person

# Runs in a child process that is killed (os._exit, no cleanup, no finally blocks) while rendering "Crash".
CRASHING_SERVER = textwrap.dedent("""
    import os, sys
    from pathlib import Path
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.generator import generate_certificate
    from app.main import create_app
    from app.worker import InlineRunner

    def gen(data, dest):
        if data.name == "Crash":
            os._exit(99)  # simulates kill -9 mid-job
        generate_certificate(data, dest)

    base = Path(sys.argv[1])
    s = Settings(database_url=sys.argv[2], storage_dir=base / "certs", lease_seconds=1)
    with TestClient(create_app(s, runner_factory=InlineRunner, generator=gen)) as c:
        people = [
            {"name": "Done One", "email": "1@example.com"},
            {"name": "Bad Email", "email": "nope"},
            {"name": "Done Two", "email": "2@example.com"},
            {"name": "Crash", "email": "3@example.com"},
            {"name": "Never Started", "email": "4@example.com"},
        ]
        c.post("/api/v1/jobs", json={"course_name": "C", "recipients": people})
""")


def test_process_is_killed_mid_job_then_restarted(tmp_path):
    url = db_url(tmp_path, "r.db")
    proc = subprocess.run([sys.executable, "-c", CRASHING_SERVER, str(tmp_path), url],
                          capture_output=True, text=True,
                          env=_env())
    assert proc.returncode == 99, proc.stderr  # really died, did not exit cleanly

    time.sleep(1.2)  # the dead worker's 1s lease must expire before anyone may take the job over
    settings = Settings(database_url=url, storage_dir=tmp_path / "certs")
    with TestClient(create_app(settings, runner_factory=InlineRunner)) as c:  # "restart"
        with c.app.state.session_factory() as db:
            job = db.scalar(select(Job))
        got = c.get(f"/api/v1/jobs/{job.id}").json()
        assert got["status"] == "completed_with_errors"
        assert got["counts"] == {"pending": 0, "processing": 0, "completed": 4, "failed": 1}

        items = {i["name"]: i for i in c.get(f"/api/v1/jobs/{job.id}/certificates").json()["items"]}
        assert items["Bad Email"]["status"] == "failed"            # failed rows stay failed
        assert items["Bad Email"]["error_code"] == "validation_error"
        assert all(items[n]["status"] == "completed" for n in ("Done One", "Done Two", "Crash", "Never Started"))

    pdfs = list((tmp_path / "certs").rglob("*.pdf"))
    assert len(pdfs) == 4 and len({p.name for p in pdfs}) == 4      # no duplicates, no leftovers
    assert not list((tmp_path / "certs").rglob("*.tmp"))


def _env():
    import os
    from pathlib import Path
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    return env


def test_crash_after_pdf_written_but_before_db_update(make_client, tmp_path):
    """DB says PENDING, PDF already on disk. Recovery must reuse the same path, never create a second file."""
    c = make_client()
    job = c.post("/api/v1/jobs", json=payload(person(), person("Bob", "b@example.com"))).json()
    before = sorted(p.name for p in (tmp_path / "certs").rglob("*.pdf"))

    with c.app.state.session_factory() as db:                       # rewind the DB as if the commit never happened
        cert = db.scalar(select(Certificate).where(Certificate.name == "Alice Smith"))
        cert.status, cert.file_path, cert.completed_at = CertStatus.PENDING, None, None
        db.get(Job, job["id"]).status = JobStatus.PROCESSING
        db.commit()

    with c.app.state.session_factory() as db:                       # the old worker's lease has expired
        db.get(Job, job["id"]).lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert c.app.state.runner.processor.run_once(job["id"])

    after = sorted(p.name for p in (tmp_path / "certs").rglob("*.pdf"))
    assert after == before                                          # same file names, overwritten in place
    assert c.get(f"/api/v1/jobs/{job['id']}").json()["status"] == "completed"


def test_concurrent_requests_with_same_idempotency_key_create_one_job(make_client):
    c = make_client()
    body = payload(person(), person("Bob", "b@example.com"))
    for attempt in range(5):
        key = f"race-{attempt}"
        barrier, results = threading.Barrier(4), []

        def fire():
            barrier.wait()
            r = c.post("/api/v1/jobs", json=body, headers={"Idempotency-Key": key})
            results.append((r.status_code, r.json().get("id")))

        threads = [threading.Thread(target=fire) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]

        assert len({job_id for _, job_id in results}) == 1, results   # everyone got the same job
        assert sorted(code for code, _ in results).count(202) == 1    # exactly one creator, the rest are replays
        with c.app.state.session_factory() as db:
            assert db.scalar(select(func.count()).select_from(Job).where(Job.idempotency_key == key)) == 1


def test_foreign_key_and_unique_constraints_are_enforced_by_the_database(client):
    import pytest
    from sqlalchemy.exc import IntegrityError
    with client.app.state.session_factory() as db:
        db.add(Certificate(job_id="no-such-job", row_index=0, raw_input="{}"))
        with pytest.raises(IntegrityError):
            db.commit()
