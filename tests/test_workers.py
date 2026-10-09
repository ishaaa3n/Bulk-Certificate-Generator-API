"""Distributed-worker behaviour: leases, takeover, graceful shutdown, scale-out, retry."""
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.generator import generate_certificate
from app.main import create_app
from app.models import Certificate, CertStatus, Job, JobStatus
from app.worker import ExternalRunner, JobWorker
from tests.conftest import db_url, payload, person


def people(n):
    return [person(f"User {chr(65 + i)}", f"u{i}@example.com") for i in range(n)]


def external_client(tmp_path, generator=None):
    """API that only enqueues; the test drives workers by hand."""
    s = Settings(database_url=db_url(tmp_path, 'w.db'), storage_dir=tmp_path / "certs", worker_mode="external")
    return TestClient(create_app(s, generator=generator))


def new_worker(c, generator=generate_certificate, stop=None, lease=60):
    app = c.app.state
    return JobWorker(app.session_factory, app.storage, generator, lease_seconds=lease, stop_event=stop)


def expire_lease(c, job_id):
    with c.app.state.session_factory() as db:
        db.get(Job, job_id).lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()


# ---------------------------------------------------------------- API / worker separation

def test_external_mode_api_only_enqueues(tmp_path):
    with external_client(tmp_path) as c:
        job = c.post("/api/v1/jobs", json=payload(*people(3))).json()
        assert job["status"] == "queued" and job["counts"]["pending"] == 3
        assert c.get(job["links"]["download_all"]).status_code == 409   # nothing generated yet

        assert new_worker(c).run_once() is True                          # a separate worker picks it up
        assert c.get(f"/api/v1/jobs/{job['id']}").json()["status"] == "completed"
        assert new_worker(c).run_once() is False                         # queue is empty


# ---------------------------------------------------------------- leases

def test_only_one_of_many_racing_workers_claims_a_job(tmp_path):
    with external_client(tmp_path) as c:
        job = c.post("/api/v1/jobs", json=payload(*people(2))).json()
        workers = [new_worker(c) for _ in range(8)]
        barrier, claimed = threading.Barrier(8), []

        def race(w):
            with c.app.state.session_factory() as db:
                barrier.wait()
                claimed.append(w.claim(db))

        threads = [threading.Thread(target=race, args=(w,)) for w in workers]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert claimed.count(job["id"]) == 1 and claimed.count(None) == 7


def test_live_lease_is_not_stolen_but_expired_lease_is_taken_over(tmp_path):
    """The flaw of 'reset all processing jobs on startup': it steals work from healthy workers."""
    with external_client(tmp_path) as c:
        job = c.post("/api/v1/jobs", json=payload(*people(2))).json()
        a, b = new_worker(c), new_worker(c)
        with c.app.state.session_factory() as db:
            assert a.claim(db, job["id"]) == job["id"]             # A owns it, lease valid for 60s
            assert b.claim(db, job["id"]) is None                  # B must not take it
            expire_lease(c, job["id"])                             # A "dies": lease runs out
            assert b.claim(db, job["id"]) == job["id"]             # now B may take over


def test_stale_worker_that_lost_its_lease_stops_and_does_not_corrupt_the_job(tmp_path):
    """Worker A stalls past its lease while rendering; B takes over and finishes; A must back off."""
    calls = Counter()
    holder = {}

    def counting_gen(data, dest):
        calls[data.name] += 1
        generate_certificate(data, dest)

    def stalling_gen(data, dest):
        calls[data.name] += 1
        if data.name == "User A" and calls["User A"] == 1:      # A is mid-render when its lease expires
            expire_lease(holder["c"], holder["job"])
            assert holder["b"].run_once() is True               # B takes over and completes the whole job
        generate_certificate(data, dest)

    with external_client(tmp_path) as c:
        job = c.post("/api/v1/jobs", json=payload(*people(3))).json()
        holder.update(c=c, job=job["id"], b=new_worker(c, counting_gen))
        assert new_worker(c, stalling_gen).run_once() is True   # A

        got = c.get(f"/api/v1/jobs/{job['id']}").json()
        assert got["status"] == "completed" and got["counts"]["completed"] == 3
        assert calls["User B"] == 1 and calls["User C"] == 1    # A stopped; it did not redo B's work
        assert len(list((tmp_path / "certs").rglob("*.pdf"))) == 3


# ---------------------------------------------------------------- graceful shutdown

def test_graceful_shutdown_releases_job_and_next_worker_resumes_only_the_rest(tmp_path):
    calls, stop = Counter(), threading.Event()

    def gen(data, dest):
        calls[data.name] += 1
        generate_certificate(data, dest)
        stop.set()                                              # SIGTERM arrives after the first certificate

    with external_client(tmp_path) as c:
        job = c.post("/api/v1/jobs", json=payload(*people(3))).json()
        new_worker(c, gen, stop).run_once()

        mid = c.get(f"/api/v1/jobs/{job['id']}").json()
        assert mid["status"] == "queued" and mid["counts"]["completed"] == 1 and mid["counts"]["pending"] == 2

        new_worker(c, gen).run_once()                           # another worker takes it, immediately (no lease wait)
        done = c.get(f"/api/v1/jobs/{job['id']}").json()
        assert done["status"] == "completed" and sum(calls.values()) == 3   # nothing generated twice


# ---------------------------------------------------------------- scale-out

def test_many_workers_process_many_jobs_without_duplicate_work(tmp_path):
    calls, lock = Counter(), threading.Lock()

    def gen(data, dest):
        with lock:
            calls[data.certificate_id] += 1
        generate_certificate(data, dest)

    with external_client(tmp_path) as c:
        ids = [c.post("/api/v1/jobs", json=payload(*people(3), course=f"Course {i}")).json()["id"] for i in range(6)]

        def drain():
            w = new_worker(c, gen)
            while w.run_once():
                pass

        threads = [threading.Thread(target=drain) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]

        assert all(c.get(f"/api/v1/jobs/{i}").json()["status"] == "completed" for i in ids)
        assert len(calls) == 18 and set(calls.values()) == {1}   # no worker failed, so each certificate was rendered exactly once


# ---------------------------------------------------------------- retry

def flaky(fail_names):
    def gen(data, dest):
        if data.name in fail_names:
            raise RuntimeError("transient render error")
        generate_certificate(data, dest)
    return gen


def test_retry_regenerates_only_generation_failures(make_client):
    fail = {"User B"}
    c = make_client(generator=lambda d, p: flaky(fail)(d, p))
    rows = people(3) + [person("Bad Row", "not-an-email")]
    job = c.post("/api/v1/jobs", json=payload(*rows)).json()
    assert job["status"] == "completed_with_errors" and job["counts"]["failed"] == 2

    fail.clear()                                                  # the transient problem is fixed
    r = c.post(f"/api/v1/jobs/{job['id']}/retry")
    assert r.status_code == 202

    got = c.get(f"/api/v1/jobs/{job['id']}").json()
    assert got["status"] == "completed_with_errors"               # the invalid row can never succeed
    assert got["counts"]["completed"] == 3 and got["counts"]["failed"] == 1
    items = {i["name"]: i for i in c.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"]}
    assert items["User B"]["status"] == "completed" and items["User B"]["attempts"] == 2
    assert items["User A"]["attempts"] == 1                       # untouched certificates are not regenerated
    assert items["Bad Row"]["error_code"] == "validation_error"
    assert c.get(job["links"]["download_all"]).status_code == 200


def test_retry_conflicts(make_client):
    c = make_client()
    ok = c.post("/api/v1/jobs", json=payload(*people(2))).json()
    assert c.post(f"/api/v1/jobs/{ok['id']}/retry").status_code == 409        # nothing failed
    bad = c.post("/api/v1/jobs", json=payload(person("X", "bad"))).json()
    assert c.post(f"/api/v1/jobs/{bad['id']}/retry").status_code == 409       # only validation failures
    assert c.post("/api/v1/jobs/nope/retry").status_code == 404


def test_retry_rejected_while_job_is_running(tmp_path):
    with external_client(tmp_path) as c:
        job = c.post("/api/v1/jobs", json=payload(*people(2))).json()         # queued, not finished
        assert c.post(f"/api/v1/jobs/{job['id']}/retry").status_code == 409


# ---------------------------------------------------------------- ops / API contract

def test_ready_endpoint(client):
    r = client.get("/ready")
    assert r.status_code == 200 and r.json() == {"status": "ready", "database": "ok", "storage": "ok"}


def test_request_id_header_is_set_and_echoed(client):
    assert client.get("/health").headers["x-request-id"]
    assert client.get("/health", headers={"X-Request-ID": "abc"}).headers["x-request-id"] == "abc"


def test_timestamps_are_timezone_aware_utc(client):
    job = client.post("/api/v1/jobs", json=payload(person())).json()
    for field in ("created_at", "started_at", "finished_at"):
        assert datetime.fromisoformat(job[field]).utcoffset() == timedelta(0), field


def test_unexpected_worker_crash_marks_unfinished_certificates_failed(tmp_path, monkeypatch):
    """Infrastructure error (not a render error) must not leave a job stuck in 'processing'."""
    with external_client(tmp_path) as c:
        job = c.post("/api/v1/jobs", json=payload(*people(2))).json()
        w = new_worker(c)
        monkeypatch.setattr(JobWorker, "_heartbeat", lambda self, db, jid: (_ for _ in ()).throw(OSError("db gone")))
        w.run_once()
        got = c.get(f"/api/v1/jobs/{job['id']}").json()
        assert got["status"] in ("completed_with_errors", "failed")
        assert got["counts"]["pending"] == 0 and got["counts"]["processing"] == 0


# ---------------------------------------------------------------- fencing (stale workers cannot corrupt state)

def _run_stale_worker_scenario(tmp_path, stale_outcome):
    """A is mid-render when its lease expires; B takes over and completes the job; then A's render finishes
    (stale_outcome='ok') or blows up (stale_outcome='error'). Returns (client, job_id)."""
    holder = {}

    def stale_gen(data, dest):
        if data.name == "User A" and not holder.get("done"):
            holder["done"] = True
            expire_lease(holder["c"], holder["job"])
            assert holder["b"].run_once() is True
            if stale_outcome == "error":
                raise RuntimeError("late failure from the stale worker")
        generate_certificate(data, dest)

    c = external_client(tmp_path).__enter__()
    job = c.post("/api/v1/jobs", json=payload(*people(3))).json()
    holder.update(c=c, job=job["id"], b=new_worker(c))
    assert new_worker(c, stale_gen).run_once() is True
    return c, job["id"]


def test_stale_worker_failing_late_cannot_overwrite_new_owners_result(tmp_path):
    c, job_id = _run_stale_worker_scenario(tmp_path, "error")
    got = c.get(f"/api/v1/jobs/{job_id}").json()
    assert got["status"] == "completed" and got["counts"] == {"pending": 0, "processing": 0, "completed": 3, "failed": 0}
    items = c.get(f"/api/v1/jobs/{job_id}/certificates").json()["items"]
    assert all(i["status"] == "completed" and i["error_code"] is None for i in items)
    assert c.get(got["links"]["download_all"]).status_code == 200


def test_stale_worker_succeeding_late_leaves_consistent_state_and_one_file_each(tmp_path):
    c, job_id = _run_stale_worker_scenario(tmp_path, "ok")
    got = c.get(f"/api/v1/jobs/{job_id}").json()
    assert got["status"] == "completed" and got["counts"]["completed"] == 3
    files = list((tmp_path / "certs").rglob("*"))
    assert len([f for f in files if f.suffix == ".pdf"]) == 3        # deterministic keys: no duplicates
    assert not [f for f in files if f.suffix == ".tmp"]              # no orphaned temp files
    with c.app.state.session_factory() as db:
        job = db.get(Job, job_id)
        assert job.locked_by is None and job.lease_expires_at is None  # lease cleanly released by the real owner


def test_new_claim_gets_a_new_fencing_token_even_for_the_same_worker(tmp_path):
    with external_client(tmp_path) as c:
        job = c.post("/api/v1/jobs", json=payload(*people(1))).json()
        w = new_worker(c)
        with c.app.state.session_factory() as db:
            w.claim(db, job["id"])
            first = w._token
            expire_lease(c, job["id"])
            w.claim(db, job["id"])
            assert w._token != first
            with c.app.state.session_factory() as db2:
                assert db2.get(Job, job["id"]).locked_by == w._token


# ---------------------------------------------------------------- retry correctness

def test_concurrent_retries_schedule_the_work_once(tmp_path):
    calls = Counter()
    lock = threading.Lock()
    fail = {"User B"}

    def gen(data, dest):
        with lock:
            calls[data.name] += 1
        if data.name in fail:
            raise RuntimeError("boom")
        generate_certificate(data, dest)

    with external_client(tmp_path) as c:
        job = c.post("/api/v1/jobs", json=payload(*people(3))).json()
        new_worker(c, gen).run_once()
        assert c.get(f"/api/v1/jobs/{job['id']}").json()["status"] == "completed_with_errors"

        fail.clear()
        barrier, codes = threading.Barrier(5), []

        def hit():
            barrier.wait()
            codes.append(c.post(f"/api/v1/jobs/{job['id']}/retry").status_code)

        threads = [threading.Thread(target=hit) for _ in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert sorted(codes) == [202, 409, 409, 409, 409]            # exactly one retry accepted

        while new_worker(c, gen).run_once():
            pass
        assert calls["User B"] == 2 and calls["User A"] == 1         # B regenerated once; A untouched
        got = c.get(f"/api/v1/jobs/{job['id']}").json()
        assert got["status"] == "completed" and got["counts"]["completed"] == 3


def test_retry_is_atomic_so_a_crash_cannot_leave_certificates_pending_on_a_finished_job(tmp_path, monkeypatch):
    """If the job-state update inside retry fails, the certificate reset must roll back with it."""
    from sqlalchemy.orm import Session
    from app import service

    with external_client(tmp_path) as c:
        job = c.post("/api/v1/jobs", json=payload(*people(2))).json()
        w = new_worker(c, lambda d, p: (_ for _ in ()).throw(RuntimeError("x")))
        w.run_once()
        before = c.get(f"/api/v1/jobs/{job['id']}").json()
        assert before["status"] == "failed"

        real_commit = Session.commit
        def exploding_commit(self):
            raise OSError("crash during retry commit")
        monkeypatch.setattr(Session, "commit", exploding_commit)
        with c.app.state.session_factory() as db:
            try:
                service.retry_failed(db, job["id"])
            except OSError:
                pass
        monkeypatch.setattr(Session, "commit", real_commit)

        after = c.get(f"/api/v1/jobs/{job['id']}").json()
        assert after["status"] == "failed" and after["counts"] == before["counts"]   # nothing half-applied
        assert c.post(f"/api/v1/jobs/{job['id']}/retry").status_code == 202             # and retry still works
