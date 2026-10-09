import io
import zipfile

from pypdf import PdfReader

from app.generator import generate_certificate
from tests.conftest import db_url, payload, person


def pdf_text(content: bytes) -> str:
    return "".join(p.extract_text() for p in PdfReader(io.BytesIO(content)).pages)


# ---------- creating a job ----------

def test_create_job_returns_202_and_location(client):
    r = client.post("/api/v1/jobs", json=payload(person(), person("Bob", "bob@example.com")))
    assert r.status_code == 202
    body = r.json()
    assert body["total"] == 2
    assert r.headers["location"] == f"/api/v1/jobs/{body['id']}"


def test_many_recipients_in_a_single_request(client):
    people = [person(f"User {chr(65 + i % 26)}{'x' * (i // 26)}", f"u{i}@example.com") for i in range(150)]
    body = client.post("/api/v1/jobs", json=payload(*people)).json()
    assert body["status"] == "completed"
    assert body["counts"]["completed"] == 150


def test_idempotency_key_replays_same_job(client):
    h = {"Idempotency-Key": "abc-123"}
    first = client.post("/api/v1/jobs", json=payload(person()), headers=h)
    again = client.post("/api/v1/jobs", json=payload(person()), headers=h)
    assert first.status_code == 202 and again.status_code == 200
    assert first.json()["id"] == again.json()["id"]


def test_idempotency_key_with_different_body_is_conflict(client):
    h = {"Idempotency-Key": "abc-123"}
    client.post("/api/v1/jobs", json=payload(person()), headers=h)
    r = client.post("/api/v1/jobs", json=payload(person("Someone Else", "x@example.com")), headers=h)
    assert r.status_code == 409


# ---------- validation ----------

def test_request_level_validation(client):
    assert client.post("/api/v1/jobs", json=payload()).status_code == 422                       # empty list
    assert client.post("/api/v1/jobs", json={"recipients": [person()]}).status_code == 422      # no course
    assert client.post("/api/v1/jobs", json=payload(person(), course="  ")).status_code == 422  # blank course
    assert client.post("/api/v1/jobs", json={"course_name": "x", "recipients": "nope"}).status_code == 422
    assert client.post("/api/v1/jobs", json=payload(person(), bogus=1)).status_code == 422      # unknown field
    assert client.post("/api/v1/jobs", content=b"{not json", headers={"content-type": "application/json"}).status_code == 422


def test_too_many_recipients_rejected(make_client):
    c = make_client(max_recipients=3)
    people = [person(f"P{i}", f"p{i}@example.com") for i in range(4)]
    assert c.post("/api/v1/jobs", json=payload(*people)).status_code == 413


def test_bad_rows_fail_individually_and_good_rows_succeed(client):
    rows = [
        person(),                                           # 0 ok
        {"email": "noname@example.com"},                    # 1 missing name
        person("   ", "blank@example.com"),                 # 2 blank name
        person("Bad Email", "not-an-email"),                # 3 invalid email
        person("Carol", "carol@example.com"),               # 4 ok
        "just a string",                                    # 5 not an object
        {"name": 123, "email": "num@example.com"},          # 6 wrong type
        person("Line\nBreak", "nl@example.com"),            # 7 control character
        person("A" * 101, "long@example.com"),              # 8 too long
        person("12345", "digits@example.com"),              # 9 no letters
        person("Zoë 李", "cjk@example.com"),                 # 10 unrenderable glyphs
        person("Alice Again", "ALICE@example.com"),         # 11 duplicate email (case-insensitive)
        None,                                               # 12 null row
    ]
    job = client.post("/api/v1/jobs", json=payload(*rows)).json()
    assert job["total"] == 13
    assert job["counts"] == {"pending": 0, "processing": 0, "completed": 2, "failed": 11}
    assert job["status"] == "completed_with_errors"

    items = client.get(f"/api/v1/jobs/{job['id']}/certificates?limit=500").json()["items"]
    by_row = {i["row_index"]: i for i in items}
    assert [r for r, i in by_row.items() if i["status"] == "completed"] == [0, 4]
    assert by_row[11]["error_code"] == "duplicate_recipient"
    assert all(by_row[r]["error_code"] == "validation_error" for r in (1, 2, 3, 5, 6, 7, 8, 9, 10, 12))
    assert all(by_row[r]["error_message"] for r in by_row if by_row[r]["status"] == "failed")


def test_name_whitespace_and_email_case_are_normalised(client):
    job = client.post("/api/v1/jobs", json=payload(person("  Ada    Lovelace ", "Ada@Example.COM"))).json()
    item = client.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"][0]
    assert item["name"] == "Ada Lovelace" and item["email"] == "ada@example.com"


def test_all_rows_invalid_marks_job_failed_immediately(client):
    job = client.post("/api/v1/jobs", json=payload({"name": ""}, {"email": "x"})).json()
    assert job["status"] == "failed"
    assert job["counts"]["failed"] == 2
    assert client.get(f"/api/v1/jobs/{job['id']}/download").status_code == 409


# ---------- generation ----------

def test_certificate_pdf_contains_recipient_data(client):
    job = client.post("/api/v1/jobs", json=payload(person("Grace Hopper", "grace@example.com"),
                                                   course="Compilers", issuer="ACM", issue_date="2024-03-05")).json()
    cert = client.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"][0]
    r = client.get(cert["download_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    text = pdf_text(r.content)
    for expected in ("Grace Hopper", "Compilers", "05 March 2024", "ACM", cert["id"]):
        assert expected in text


def test_very_long_name_still_renders(client):
    name = "Wolfeschlegelsteinhausenbergerdorff " * 2
    job = client.post("/api/v1/jobs", json=payload(person(name.strip(), "w@example.com"))).json()
    assert job["status"] == "completed"


def test_special_characters_in_names(client):
    job = client.post("/api/v1/jobs", json=payload(person("José O'Brien-Müller", "j@example.com"),
                                                   person("<script>alert(1)</script>", "x@example.com"))).json()
    assert job["counts"]["completed"] == 2


# ---------- individual failure isolation ----------

def failing_on(bad_name):
    def gen(data, dest):
        if data.name == bad_name:
            raise RuntimeError("renderer exploded")
        generate_certificate(data, dest)
    return gen


def test_one_generation_failure_does_not_stop_others(make_client):
    c = make_client(generator=failing_on("Bob"))
    people = [person("Alice", "a@example.com"), person("Bob", "b@example.com"), person("Carol", "c@example.com")]
    job = c.post("/api/v1/jobs", json=payload(*people)).json()
    assert job["status"] == "completed_with_errors"
    assert job["counts"]["completed"] == 2 and job["counts"]["failed"] == 1

    items = {i["name"]: i for i in c.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"]}
    assert items["Bob"]["status"] == "failed"
    assert items["Bob"]["error_code"] == "generation_error"
    assert "renderer exploded" in items["Bob"]["error_message"]
    assert items["Bob"]["download_url"] is None
    assert c.get(items["Alice"]["download_url"]).status_code == 200
    assert c.get(items["Carol"]["download_url"]).status_code == 200


def test_failed_generation_leaves_no_partial_file(make_client, tmp_path):
    def gen(data, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        raise OSError("disk full")
    c = make_client(generator=gen)
    job = c.post("/api/v1/jobs", json=payload(person())).json()
    assert job["status"] == "failed"
    assert list((tmp_path / "certs").rglob("*.pdf")) == []


def test_downloading_a_failed_certificate_returns_409(make_client):
    c = make_client(generator=failing_on("Bob"))
    job = c.post("/api/v1/jobs", json=payload(person("Bob", "b@example.com"))).json()
    item = c.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"][0]
    r = c.get(f"/api/v1/certificates/{item['id']}/download")
    assert r.status_code == 409 and "renderer exploded" in r.json()["detail"]


# ---------- status / progress ----------

def test_job_status_reports_progress(client):
    job = client.post("/api/v1/jobs", json=payload(person(), person("Bob", "bad"))).json()
    got = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert got["status"] == "completed_with_errors"
    assert got["progress_percent"] == 100.0
    assert got["started_at"] and got["finished_at"]
    assert set(got["links"]) == {"self", "certificates", "download_all"}


def test_progress_visible_mid_run(make_client):
    """While generating the 2nd certificate, the status endpoint already shows the 1st as done."""
    snapshots, job_ids = [], []

    def gen(data, dest):
        if data.name == "Second":
            snapshots.append(c.get(f"/api/v1/jobs/{job_ids[0]}").json())
        generate_certificate(data, dest)

    c = make_client(generator=gen)
    original_submit = c.app.state.runner.submit  # inline runner: capture the id before the job runs
    c.app.state.runner.submit = lambda job_id: (job_ids.append(job_id), original_submit(job_id))
    people = [person("First", "1@example.com"), person("Second", "2@example.com"), person("Third", "3@example.com")]
    c.post("/api/v1/jobs", json=payload(*people))
    snap = snapshots[0]
    assert snap["status"] == "processing"
    assert snap["counts"]["completed"] == 1 and snap["counts"]["processing"] == 1 and snap["counts"]["pending"] == 1
    assert 0 < snap["progress_percent"] < 100


def test_unknown_job_is_404(client):
    assert client.get("/api/v1/jobs/does-not-exist").status_code == 404
    assert client.get("/api/v1/jobs/does-not-exist/certificates").status_code == 404
    assert client.get("/api/v1/jobs/does-not-exist/download").status_code == 404


# ---------- retrieval ----------

def test_list_certificates_pagination_and_filter(client):
    rows = [person(f"User {i}", f"u{i}@example.com") for i in range(5)] + [person("Bad", "bad")]
    job = client.post("/api/v1/jobs", json=payload(*rows)).json()
    page = client.get(f"/api/v1/jobs/{job['id']}/certificates?limit=2&offset=2").json()
    assert page["total"] == 6 and [i["row_index"] for i in page["items"]] == [2, 3]
    failed = client.get(f"/api/v1/jobs/{job['id']}/certificates?status=failed").json()
    assert failed["total"] == 1 and failed["items"][0]["name"] == "Bad"
    assert client.get(f"/api/v1/jobs/{job['id']}/certificates?status=bogus").status_code == 422
    assert client.get(f"/api/v1/jobs/{job['id']}/certificates?limit=0").status_code == 422


def test_download_zip_contains_only_successful_certificates(make_client):
    c = make_client(generator=failing_on("Bob"))
    people = [person("Alice", "a@example.com"), person("Bob", "b@example.com"), person("Carol", "c@example.com")]
    job = c.post("/api/v1/jobs", json=payload(*people)).json()
    r = c.get(job["links"]["download_all"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(zf.namelist()) == ["00001_Alice.pdf", "00003_Carol.pdf"]
    assert "Alice" in pdf_text(zf.read("00001_Alice.pdf"))


def test_zip_names_unique_for_same_name(client):
    job = client.post("/api/v1/jobs", json=payload(person("Sam Lee", "s1@example.com"), person("Sam Lee", "s2@example.com"))).json()
    names = zipfile.ZipFile(io.BytesIO(client.get(job["links"]["download_all"]).content)).namelist()
    assert len(set(names)) == 2


def test_download_unknown_certificate_404(client):
    assert client.get("/api/v1/certificates/nope/download").status_code == 404


def test_missing_file_on_disk_returns_410(client, tmp_path):
    job = client.post("/api/v1/jobs", json=payload(person())).json()
    item = client.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"][0]
    for f in (tmp_path / "certs").rglob("*.pdf"):
        f.unlink()
    assert client.get(item["download_url"]).status_code == 410


def test_path_traversal_in_db_path_is_refused(client, tmp_path):
    from app.models import Certificate
    job = client.post("/api/v1/jobs", json=payload(person())).json()
    item = client.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"][0]
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    with client.app.state.session_factory() as db:
        cert = db.get(Certificate, item["id"])
        cert.file_path = "../secret.txt"
        db.commit()
    assert client.get(item["download_url"]).status_code == 404


def test_job_download_not_ready_while_processing(make_client):
    results = []

    def gen(data, dest):
        results.append(c.get(f"/api/v1/jobs/{holder[0]}/download").status_code)
        generate_certificate(data, dest)

    holder = []
    c = make_client(generator=gen)
    orig = c.app.state.runner.submit
    c.app.state.runner.submit = lambda jid: (holder.append(jid), orig(jid))
    c.post("/api/v1/jobs", json=payload(person()))
    assert results == [409]


# ---------- crash recovery ----------

def test_interrupted_job_is_resumed_on_startup(tmp_path):
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.config import Settings
    from app.main import create_app
    from app.models import Certificate, CertStatus, Job, JobStatus
    from app.worker import InlineRunner

    settings = Settings(database_url=db_url(tmp_path, 'r.db'), storage_dir=tmp_path / "certs")
    calls = []

    def dying(data, dest):
        calls.append(data.name)
        generate_certificate(data, dest)

    # First run: simulate a crash by leaving a job half-done in the DB.
    with TestClient(create_app(settings, runner_factory=InlineRunner)) as c:
        job = c.post("/api/v1/jobs", json=payload(person("Done", "d@example.com"), person("Todo", "t@example.com"))).json()
        with c.app.state.session_factory() as db:
            todo = db.scalar(select(Certificate).where(Certificate.name == "Todo"))
            todo.status, todo.file_path, todo.completed_at = CertStatus.PROCESSING, None, None
            db.get(Job, job["id"]).status = JobStatus.PROCESSING
            db.commit()

    # Restart: recovery re-queues; only the unfinished certificate is regenerated.
    with TestClient(create_app(settings, runner_factory=InlineRunner, generator=dying)) as c:
        got = c.get(f"/api/v1/jobs/{job['id']}").json()
        assert got["status"] == "completed" and got["counts"]["completed"] == 2
        assert calls == ["Todo"]


def test_job_is_not_processed_twice(client):
    """Claiming is atomic: re-submitting a finished job is a no-op."""
    calls = []
    job = client.post("/api/v1/jobs", json=payload(person())).json()
    proc = client.app.state.runner.processor
    proc.generator = lambda d, p: calls.append(1)
    proc.process(job["id"])
    assert calls == []


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
