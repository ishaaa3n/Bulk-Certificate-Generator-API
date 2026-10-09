# Bulk Certificate Generator

Backend API that accepts a list of recipients in **one request**, generates a PDF certificate for each valid recipient
from a single predefined template, tracks per-certificate status, and serves the results individually or as a ZIP.

**Stack:** Python 3.11+, FastAPI, SQLAlchemy 2 (SQLite by default, PostgreSQL supported), ReportLab, pytest.

## Architecture

```
                    ┌────────────────────────────  SQL database  ────────────────────────────┐
                    │  jobs (status, lease)   certificates (status, attempts, error, key)     │
                    └───▲───────────────▲────────────────────────────────▲───────────────────┘
        (1) validate +  │               │ (4) compare-and-set claim,     │ (6) status / progress
        persist, 202    │               │     lease renewed per cert     │     (GROUP BY, never stale)
 Client ─────────────▶ API (stateless) ─┘                                │
   ▲  POST /jobs        │ (2) wake-up hint                       Worker(s) ┘  × N, threads or processes or hosts
   │                    ▼                                           │ (5) render PDF → atomic rename → commit
   │               [optional]                                       ▼
   └── GET status / download ◀──────────────────────────────── Storage (local disk; S3-ready seam)
```

Layers (each file has one job; dependencies point downward):

| Layer | File | Responsibility |
|---|---|---|
| Transport | `api.py` | HTTP only: parse, call service, shape response |
| Application | `service.py`, `validation.py`, `errors.py` | Business rules, idempotency, retry; raises *domain* errors (no HTTP types) |
| Execution | `worker.py`, `worker_main.py` | Lease-based job queue on the DB, per-certificate isolation |
| Infrastructure | `storage.py`, `generator.py`, `database.py`, `models.py` | Files, PDF template, DB engine/types |
| Wiring | `main.py`, `config.py` | App factory, env config, request-id/logging middleware |

## Setup & run

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload     # dev: API + embedded worker threads, SQLite. Docs: http://localhost:8000/docs
pytest                            # 50 tests (SQLite; same suite runs on PostgreSQL, see below)
```

**Production-shaped (separate API and workers, PostgreSQL):**

```bash
docker compose up --build --scale worker=3
```

or by hand: API with `CERTGEN_WORKER_MODE=external uvicorn app.main:app`, plus any number of
`python -m app.worker_main` processes pointing at the same database and storage.

Config (env vars, prefix `CERTGEN_`): `DATABASE_URL`, `STORAGE_DIR`, `MAX_RECIPIENTS` (5000), `MAX_NAME_LENGTH` (100),
`WORKER_MODE` (`embedded`|`external`), `WORKER_THREADS` (2), `LEASE_SECONDS` (60), `POLL_INTERVAL` (0.5).

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/jobs` | Submit a bulk request → `202` + job. Optional `Idempotency-Key` header |
| GET | `/api/v1/jobs/{id}` | Status, counts, progress % |
| GET | `/api/v1/jobs/{id}/certificates?status=&limit=&offset=` | Per-recipient result with error details |
| POST | `/api/v1/jobs/{id}/retry` | Re-run certificates that failed in *generation* → `202` |
| GET | `/api/v1/certificates/{id}/download` | One PDF |
| GET | `/api/v1/jobs/{id}/download` | ZIP of all successful PDFs (job must have finished) |
| GET | `/health`, `/ready` | Liveness; readiness (DB + storage), `503` if not ready |

```bash
curl -X POST localhost:8000/api/v1/jobs -H "Content-Type: application/json" -H "Idempotency-Key: batch-1" -d '{
  "course_name": "Python Fundamentals", "issuer": "Acme Academy", "issue_date": "2024-06-01",
  "recipients": [ {"name": "Alice Smith", "email": "alice@example.com"},
                  {"name": "", "email": "broken"} ] }'

curl localhost:8000/api/v1/jobs/<id>                              # poll until completed / completed_with_errors / failed
curl "localhost:8000/api/v1/jobs/<id>/certificates?status=failed" # what failed and why
curl -OJ localhost:8000/api/v1/certificates/<cert_id>/download    # one PDF
curl -OJ localhost:8000/api/v1/jobs/<id>/download                 # ZIP
curl -X POST localhost:8000/api/v1/jobs/<id>/retry                # retry generation failures
```

Job status: `queued → processing → completed | completed_with_errors | failed`.
Certificate status: `pending → processing → completed | failed`, with `error_code`
(`validation_error`, `duplicate_recipient`, `generation_error`), `error_message` and `attempts`.

| HTTP | Meaning |
|---|---|
| 202 | Job accepted / retry accepted |
| 200 | Idempotent replay of an earlier identical request; successful reads |
| 404 | Unknown job / certificate |
| 409 | Idempotency-Key reused with a different body; certificate failed/not ready; ZIP or retry requested while the job is still running; nothing to retry/download |
| 410 | Recorded as completed, but the file is gone from disk |
| 413 | More than `MAX_RECIPIENTS` recipients |
| 422 | Malformed request (bad JSON, blank course, empty/non-list recipients, unknown fields, bad query params) |
| 503 | `/ready` only: a dependency is down |

## Design decisions

### 1. Asynchronous processing with the database as the queue
A synchronous endpoint holds a connection open for N × render-time and times out on large batches. `POST` validates and
persists everything, returns `202`, and workers generate in the background. The job row *is* the queue entry, so there is
no broker to operate and no "lost message" window between "job saved" and "job enqueued": if the row exists, it will be
processed. Trade-off: polling latency (`POLL_INTERVAL`, shortened by an in-process wake-up) and DB-bound throughput,
which is far above what PDF rendering needs. The execution mechanism is behind one seam (`worker.py` runners), so
moving to Celery/SQS means a task calling `JobWorker.run_once(job_id)`; nothing else changes.

### 2. Leases instead of "recover on startup"
Workers claim a job with one compare-and-set `UPDATE … WHERE status='queued' OR (status='processing' AND lease expired)`;
exactly one racing worker gets `rowcount == 1`. The lease is renewed after every certificate. If a worker dies, its lease
expires and any worker takes the job over and redoes only unfinished certificates. A naive "reset all processing jobs on
startup" is wrong the moment there are two processes (it steals healthy workers' jobs); leases make the system correct
for threads, processes and machines alike. A worker that discovers it lost its lease (stalled past expiry) stops
touching the job. Graceful shutdown (SIGTERM) finishes
the current certificate and *releases* the job so another worker continues immediately without waiting for expiry.

**Fencing (stale workers).** Expiry alone is not enough: worker A can stall past its lease, B takes over, and A then wakes
up. So every protected write is *fenced*: each claim stores a fresh token in `jobs.locked_by`, and every certificate/job
update is a single statement conditioned on `EXISTS (job still leased to my token)`. There is no check-then-act gap; the
database decides atomically. A stale worker's late success *or* late failure is rejected (`LeaseLost`) and it backs off
without touching anything. Tests prove this, and removing the fence makes them fail. The one thing fencing cannot stop is
the stale worker's atomic file rename, which is harmless because both workers produce byte-identical output for the same
certificate. If a single render can take longer than `LEASE_SECONDS`, the lease expires mid-render and a second worker
will redo it; that is safe (idempotent, fenced) but wasteful, so keep the lease comfortably above the slowest render.

**Embedded + external workers together** is safe: they are just more workers contending for the same leases. Nothing
special is needed to avoid duplicate processing; in practice use `external` in production so the API stays stateless.

### 3. Failure isolation and visibility
Request-level problems (missing course, empty list, malformed JSON, too many recipients) reject the request. *Row*-level
problems never do: recipients are accepted as raw objects and validated one by one, and every bad row is stored as a
`failed` certificate with a reason, so `total == completed + failed` always holds and nothing is dropped silently. In the
worker each certificate has its own `try/except` and its own commit: one render failure cannot stop the rest, and progress is
visible live. Job counts are computed with `GROUP BY` rather than stored counters, so they cannot drift.
`POST …/retry` re-queues only *generation* failures; validation/duplicate failures are properties of the input and can never
succeed on retry. The certificate reset and the job's return to `queued` happen in one transaction (a crash cannot leave a
finished job holding pending rows), and the state change is conditional, so concurrent retries schedule the work once
(one `202`, the rest `409`). Completed certificates are never touched; counts and final status are recomputed from rows.

### 4. Delivery guarantee: at-least-once execution, idempotent results
I deliberately do **not** claim exactly-once execution. Writing a file and committing a DB row are not one atomic
transaction, so if a worker writes a PDF and dies before the commit, the replacement worker must render that certificate
again: the work happens twice, but the *result* is the same single file. What the design guarantees instead:
- **Work is safe to retry.** The output key is a pure function of ids (`<job_id>/<certificate_id>.pdf`) and the same inputs
  give the same bytes, so re-running overwrites the same path (no `_1` duplicates).
- **No partial final files.** Rendering goes to a uniquely named temp file, then an atomic rename.
- **Completed results are preserved.** Recovery only redoes `pending`/`processing` certificates.
- **DB state is consistent with the protocol** (see fencing below): a stale worker cannot overwrite a newer owner's state.
Tested with a real `os._exit` process kill, and with a crash between file-write and DB-commit. (One known residue: a hard
kill mid-render can leave an orphaned `*.tmp` file; it is never served and a periodic sweep would remove it.)

### 5. Idempotent submission
`Idempotency-Key` + a `UNIQUE` DB constraint + a hash of the body: same key/same body → the same job (`200`); same
key/different body → `409`. Concurrent duplicates race on the insert and the loser reads back the winner (tested with
simultaneous requests). The hash ignores server-side defaults, so a retry after midnight still matches.

### 6. Input hardening
Names are trimmed/whitespace-collapsed (NFC); control and invisible characters (newline, NUL, bidi overrides) are rejected;
emails are lower-cased and de-duplicated per job (first wins). The built-in PDF font is Latin-1, so names it cannot
render (e.g. CJK) fail with a clear message instead of garbage glyphs; very long names shrink to fit. File names are
server-generated UUIDs, stored keys are re-validated to stay inside the storage root on every download, ZIP entry names are
sanitised and made unique by row number. `MAX_RECIPIENTS` caps the batch; pagination is capped at 500.

### 7. Data integrity at the database level
Foreign key (`certificates.job_id → jobs.id`, enforced, including on SQLite), `UNIQUE(job_id, row_index)`,
`UNIQUE(idempotency_key)`, indexes for the claim query and per-job status counts. Timestamps go through a `UTCDateTime`
type so they are timezone-aware UTC on every database (SQLite would otherwise drop the offset).

### 8. Observability
Structured log lines for job lifecycle (claimed / finished / lease lost / released) with job and worker ids; per-request
log with an `X-Request-ID` (echoed or generated); `/health` vs `/ready` split for orchestrators.

### Why SQLite by default, and PostgreSQL
SQLite gives zero-setup for reviewers; WAL mode lets status reads proceed during writes. All concurrency logic is plain SQL
(compare-and-set `UPDATE … WHERE`, `UNIQUE` constraints, `EXISTS` fencing), not SQLite-specific, and the design is meant for
PostgreSQL (`READ COMMITTED`: a conditional UPDATE blocks on the row lock, then re-evaluates its WHERE, so only one racing
claim wins). Verified, not assumed:
- The **entire test suite passes on PostgreSQL 16** as well as SQLite (racing claims, 4 concurrent workers, concurrent
  idempotency/retry, process kill, fencing):
  `docker run -d --rm -p 54329:5432 -e POSTGRES_USER=certgen -e POSTGRES_PASSWORD=certgen -e POSTGRES_DB=certgen postgres:16-alpine`
  then `CERTGEN_TEST_DATABASE_URL=postgresql+psycopg2://certgen:certgen@localhost:54329/certgen pytest`
  (needs `pip install psycopg2-binary`).
- The **real `docker compose up --build --scale worker=3` stack** was run end to end: 4 jobs × 150 recipients, one worker
  container SIGKILLed mid-flight, all 600 certificates still completed (the killed worker's jobs were taken over after
  lease expiry) and the ZIP downloaded from the API (shared volume) was correct.
Not verified: multi-*host* deployment (the compose volume is a single-host shared disk) and PostgreSQL versions other than 16.

## Security / access model
No authentication (out of scope for the brief). Job and certificate ids are random UUIDv4 (unguessable) but are the *only*
access control: anyone holding an id can read that job or download its certificates. Production would add authN plus a
per-tenant ownership check on every job lookup, or short-lived signed URLs. There is no request body size limit beyond the
recipient cap; put a reverse-proxy limit in front.

## Known limitations / next steps
- **No migrations:** tables are created with `create_all` at startup; a real deployment would use Alembic. Schema changes
  therefore need a manual migration or a fresh database.
- Local-disk storage is shared between API and workers via a volume; S3-compatible storage (with signed URLs) is the next
  step. `storage.py` is the only module that knows the filesystem layout.
- A job is processed by one worker at a time; very large single jobs could be sharded by certificate ranges.
- No auth, rate limiting or metrics endpoint (Prometheus) yet.
- Latin-1 names only; other scripts need a bundled Unicode TTF (e.g. Noto).
- A "poison" job that crashes workers repeatedly is marked failed on the first infrastructure exception rather than retried
  with backoff.

## Tests (50)
`tests/test_api.py` creation, validation, generation, progress, failure isolation, retrieval, ZIP, traversal.
`tests/test_resilience.py` real process kill + restart, crash between file-write and DB-commit, concurrent idempotency, DB constraints.
`tests/test_workers.py` racing claims, live-lease-not-stolen, expired-lease takeover, fencing of stale workers (late success and late
failure), concurrent retries, retry atomicity, graceful
shutdown, 4 workers × 6 jobs with no duplicate work, API/worker separation, retry, `/ready`, request ids, UTC timestamps.
