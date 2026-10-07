# Bulk Certificate Generator

Backend API that accepts a list of recipients in **one request**, generates a PDF certificate for each valid
recipient from a single predefined template, tracks per-certificate status, and lets the client download the results
individually or as a ZIP.

**Stack:** Python 3.11+, FastAPI, SQLAlchemy 2 + SQLite (relational), ReportLab (PDF), pytest.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows   (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
```

## Run

```bash
uvicorn app.main:app --reload
```

Interactive docs: http://localhost:8000/docs. Data goes to `./data/` (SQLite DB + PDFs).
Config via env vars (prefix `CERTGEN_`): `DATABASE_URL`, `STORAGE_DIR`, `MAX_RECIPIENTS` (default 5000),
`MAX_NAME_LENGTH` (100), `WORKER_THREADS` (2).

> Run a single uvicorn process (no `--workers N`): the worker threads live inside the API process (see decisions).

## Test

```bash
pytest
```

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/jobs` | Submit a bulk request → `202` + job (generation runs in background) |
| GET | `/api/v1/jobs/{id}` | Status, counts, progress % |
| GET | `/api/v1/jobs/{id}/certificates?status=&limit=&offset=` | Per-recipient result, with error details |
| GET | `/api/v1/certificates/{id}/download` | One PDF |
| GET | `/api/v1/jobs/{id}/download` | ZIP of all successful PDFs (job must be finished) |

### Submit

```bash
curl -X POST localhost:8000/api/v1/jobs \
  -H "Content-Type: application/json" -H "Idempotency-Key: event-2024-batch-1" \
  -d '{
    "course_name": "Python Fundamentals",
    "issuer": "Acme Academy",
    "issue_date": "2024-06-01",
    "recipients": [
      {"name": "Alice Smith", "email": "alice@example.com"},
      {"name": "Bob Jones",   "email": "bob@example.com"},
      {"name": "",            "email": "broken"}
    ]
  }'
```

`issuer` and `issue_date` are optional (date defaults to today). Response (abridged):

```json
{ "id": "b27f…", "status": "queued", "total": 3,
  "counts": {"pending": 2, "processing": 0, "completed": 0, "failed": 1},
  "progress_percent": 33.33, "links": {"self": "…", "certificates": "…", "download_all": "…"} }
```

### Track and retrieve

```bash
curl localhost:8000/api/v1/jobs/<id>                        # poll until status is completed / completed_with_errors / failed
curl localhost:8000/api/v1/jobs/<id>/certificates?status=failed   # which rows failed and why
curl -OJ localhost:8000/api/v1/certificates/<cert_id>/download    # single PDF
curl -OJ localhost:8000/api/v1/jobs/<id>/download                 # ZIP of everything that succeeded
```

Job status: `queued → processing → completed | completed_with_errors | failed`.
Certificate status: `pending → processing → completed | failed`, with `error_code`
(`validation_error`, `duplicate_recipient`, `generation_error`) and a human-readable `error_message`.

## Design decisions

**Background processing (not synchronous).** A synchronous request would hold an HTTP connection open for
N × render-time and time out on large batches. `POST` validates and stores everything, returns `202` immediately, and a
small thread pool generates certificates. The client polls the status endpoint. A thread pool in the API process is
the simplest correct choice for this scope; the cost is that it is single-node. Because the job state lives in the
database (not in memory), swapping `ThreadRunner` for Celery/RQ is a change confined to `worker.py`.

**Row-level failure isolation.** Request-level problems (missing course, empty list, malformed JSON, too many
recipients) reject the request. *Row*-level problems never do: recipients are intentionally accepted as untyped
objects and validated one by one, and each bad row is stored as a `failed` certificate with a reason. Nothing is
silently dropped, and `total == completed + failed` always holds. In the worker, every certificate has its own
`try/except` and its own commit, so one render failure cannot stop the rest, and progress is visible live.

**Edge cases handled**

- *Bad data:* missing/blank/non-string name or email, invalid email, name too long, name with no letters, control or
  invisible characters (newlines, NUL, bidi overrides), `null` or non-object rows.
- *Normalisation:* names trimmed and whitespace-collapsed; emails lower-cased.
- *Duplicates:* same email twice in a job (case-insensitive) → first wins, later ones fail as `duplicate_recipient`.
- *Fonts:* the built-in PDF font is Latin-1 only; names with unsupported characters (e.g. CJK) fail with a clear message
  instead of producing garbage glyphs. Very long names are auto-shrunk to fit the page.
- *Retries / double-submit:* optional `Idempotency-Key` header. Same key + same body → same job (`200`); same key +
  different body → `409`. Enforced by a unique DB constraint so concurrent duplicates are safe.
- *Crash safety:* PDFs are written to a temp file then atomically renamed (no truncated files). On startup, jobs left
  `processing`/`queued` are resumed and only unfinished certificates are redone. A job is claimed with an atomic
  `UPDATE … WHERE status='queued'`, so it can never be processed twice. If the job aborts for infrastructure reasons,
  leftover rows are marked failed rather than stuck.
- *Security:* file names are server-generated UUIDs (user input never touches a path); download resolves the path and
  refuses anything outside the storage dir; ZIP entry names are sanitised and made unique with the row number.
- *Downloads:* unknown id → `404`; certificate failed/not ready, or ZIP requested while job still running → `409`;
  file deleted from disk → `410`. ZIP is spooled to disk, not built in RAM.
- *Limits:* `MAX_RECIPIENTS` per request (`413`), pagination capped at 500.

**Why counts are computed, not stored.** Job counters come from a `GROUP BY` over the certificate rows, so they can
never drift from the real state.

**SQLite.** Chosen for zero-setup; WAL mode lets status reads proceed while workers write. Everything goes through
SQLAlchemy, so pointing `CERTGEN_DATABASE_URL` at PostgreSQL requires no code change.

## Known limitations / what I would do next

- Jobs run inside the API process: use one uvicorn worker, or move to a real queue (Celery/RQ + Postgres) to scale out.
- Local-disk storage; production would use S3-compatible storage with signed URLs.
- No authentication or rate limiting.
- Latin-1 names only; supporting other scripts means bundling a Unicode TTF (e.g. Noto) with ReportLab.
- No endpoint to retry failed certificates; easy to add since failed rows keep their input (`raw_input`).

## Layout

```
app/
  main.py        routes + app factory (wiring, startup recovery)
  service.py     create_job: validation, dedupe, idempotency; status aggregation
  validation.py  per-row validation rules
  worker.py      JobProcessor (per-certificate isolation), ThreadRunner / InlineRunner
  generator.py   the single PDF template
  models.py      Job, Certificate     schemas.py  API models     config.py / database.py
tests/           28 tests: creation, validation, generation, progress, failure isolation, retrieval, recovery
```
