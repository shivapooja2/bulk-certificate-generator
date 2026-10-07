# Bulk Certificate Generator

Backend API that accepts a list of recipients in **one request**, validates them, generates a PDF
certificate per valid recipient from a single predefined template in the background, and lets the
client track progress and download the results.

**Stack:** Python 3.10+ · Flask · SQLite (relational) · ReportLab (PDF) · `ThreadPoolExecutor` workers

## Run

```bash
pip install -r requirements.txt
python run.py                      # http://localhost:8000
python -m unittest discover -v     # 10 tests
```
Env vars (optional): `DATABASE`, `STORAGE_DIR`, `WORKERS` (default 4), `MAX_RECIPIENTS` (default 5000).

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/batches` | Submit a bulk request → `202 Accepted` + batch id |
| GET | `/api/v1/batches/{id}` | Status + progress counts |
| GET | `/api/v1/batches/{id}/certificates?status=&page=&page_size=` | Per-recipient results (filter by status, paginated) |
| GET | `/api/v1/certificates/{id}/download` | One PDF |
| GET | `/api/v1/batches/{id}/download` | ZIP of all completed PDFs |
| GET | `/api/v1/certificates/verify/{code}` | Verify a certificate number |

### Submit
```bash
curl -X POST localhost:8000/api/v1/batches \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: bootcamp-2026-run-1' \
  -d '{
    "title": "Certificate of Completion",
    "event_name": "Advanced Python Bootcamp 2026",
    "issuer": "Acme Academy",
    "issue_date": "2026-10-01",
    "recipients": [
      {"name": "Asha Verma",  "email": "asha@example.com", "achievement": "with distinction"},
      {"name": "Rohan Mehta", "email": "rohan@example.com"},
      {"name": "",            "email": "bad"}
    ]}'
```
```json
{ "batch_id": "…", "status": "queued", "accepted": 2, "rejected": 1,
  "rejected_recipients": [{"index": 2, "errors": ["'name' is required", "'email' is not a valid email address"]}],
  "links": {"status": "/api/v1/batches/…", "certificates": "…", "download_all": "…"} }
```

### Progress
```json
{ "id": "…", "status": "processing",
  "progress": {"total": 2000, "pending": 900, "processing": 4, "completed": 1090,
               "failed": 0, "invalid": 6, "percent": 54.5} }
```
Batch `status`: `queued` → `processing` → `completed` | `completed_with_errors` | `failed`.
Certificate `status`: `pending`, `processing`, `completed`, `failed`, `invalid`.

Errors are JSON: `400` bad request-level input, `404` unknown id, `409` file not ready,
`413` body > 5 MB, `422` no valid recipients.

## Design decisions

- **Async, 202 + polling.** Generation is CPU work; the request returns as soon as rows are persisted
  (2,000 recipients ≈ 0.15 s), and the client polls the batch endpoint.
- **Validation is two-level.** Bad batch fields (title, issuer, date, empty/oversized list) reject the whole
  request with `400`. Bad *recipients* (missing name, bad email, duplicate email) are recorded as `invalid`
  with a reason and returned in the response, while valid ones proceed. One typo doesn't block 4,999 others.
- **The database is the queue.** Each certificate row is a job. Workers claim it with an atomic
  `UPDATE … WHERE status='pending'`, so a job can't run twice. Batch + all rows are inserted in a single
  transaction, and jobs are enqueued only *after* commit.
- **Failure handling.** A failed render retries up to 3 times, then becomes `failed` with the error stored;
  other certificates are unaffected. PDFs are written to a temp file then renamed, so downloads never see a
  partial file.
- **Crash recovery.** On startup, anything left `pending`/`processing` is re-queued (tested).
- **Derived batch status.** Computed from per-certificate counts instead of stored, so it can't go stale.
- **Idempotency-Key header.** Retrying a timed-out POST returns the same batch instead of creating duplicates.
- **Certificate numbers** (`CERT-<batch>-<seq>`) are unique and printed on the PDF; `/verify/{code}` checks them.
- **Safety:** input is whitespace/control-char normalised, lengths capped, long names/events auto-shrink or
  wrap on the template, body size and recipient count are limited.

Measured locally: 2,000 certificates generated in ~7.5 s with 4 workers (4.8 MB ZIP).

## Known limitations / what I'd do next for production

- **Workers are in-process threads.** Fine for one instance. For horizontal scale I'd move jobs to
  Celery/RQ + Redis (the `_process` function is already idempotent and claim-based) and the DB to PostgreSQL
  (use `SELECT … FOR UPDATE SKIP LOCKED` for claiming).
- **Python threads + the GIL:** PDF drawing is CPU-bound, so a `ProcessPoolExecutor` would scale better on
  multi-core if staying in-process.
- PDFs live on local disk; production would use S3/GCS with signed URLs.
- Built-in Helvetica/Times fonts cover Latin text only; non-Latin names need an embedded TTF font.
- No authentication/rate limiting (would add API keys or OAuth and per-client quotas).
- Zip download builds from files on disk via a spooled temp file; for huge batches I'd generate the ZIP
  asynchronously or stream it.
- Possible extras: webhook/email on completion, batch cancellation, retry-failed endpoint, DB migrations (Alembic).

## Layout
```
app/__init__.py    app factory, error handlers
app/routes.py      HTTP endpoints
app/service.py     batch creation, worker pool, retry, recovery, status derivation
app/validation.py  request/recipient validation
app/pdf.py         certificate template (ReportLab)
app/db.py          schema + connection helper
tests/test_api.py  end-to-end tests
```
