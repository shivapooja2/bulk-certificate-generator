"""Business logic: persisting batches and generating certificates in the background.

Design notes
- The DB is the queue's source of truth. Each certificate row is a job with a
  status; workers claim a job with an atomic conditional UPDATE, so a job can
  never be processed twice even if it is enqueued twice.
- Jobs run on a thread pool (ThreadPoolExecutor). On startup, `recover()`
  re-queues anything left 'pending'/'processing' by a crash/restart.
- Failures are retried up to `max_attempts`, then marked 'failed' with the error.
- Batch status is *derived* from the per-certificate counts rather than stored,
  so it can never drift out of sync.
"""
import logging
import re
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .pdf import render_certificate

log = logging.getLogger(__name__)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def derive_status(counts: dict, total: int) -> str:
    pending, processing = counts.get("pending", 0), counts.get("processing", 0)
    completed = counts.get("completed", 0)
    if pending + processing:
        return "queued" if pending == total else "processing"
    if completed == total:
        return "completed"
    if completed == 0:
        return "failed"
    return "completed_with_errors"


def slugify(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_") or "certificate"


class GenerationService:
    def __init__(self, db, storage_dir, workers=4, max_attempts=3, renderer=render_certificate):
        self.db = db
        self.storage = Path(storage_dir)
        self.storage.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        self.renderer = renderer
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="certgen")

    # ---------- submission ----------
    def create_batch(self, params, rows, idempotency_key=None):
        """Persist the batch and all its rows in ONE transaction, then enqueue.
        Returns (batch_id, created). If idempotency_key was already used the
        existing batch is returned with created=False."""
        if idempotency_key:
            existing = self._batch_by_key(idempotency_key)
            if existing:
                return existing, False

        batch_id, ts = str(uuid.uuid4()), now()
        prefix = batch_id[:8].upper()
        pending_ids = []
        try:
            with self.db.tx() as c:
                c.execute(
                    "INSERT INTO batches (id,title,event_name,issuer,issue_date,idempotency_key,total,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (batch_id, params["title"], params["event_name"], params["issuer"],
                     params["issue_date"], idempotency_key, len(rows), ts))
                records = []
                for r in rows:
                    cert_id = str(uuid.uuid4())
                    if r["valid"]:
                        pending_ids.append(cert_id)
                    records.append((
                        cert_id, batch_id, r["index"], r["name"], r["email"], r["achievement"],
                        "pending" if r["valid"] else "invalid",
                        None if r["valid"] else "; ".join(r["errors"]),
                        f"CERT-{prefix}-{r['index'] + 1:05d}" if r["valid"] else None, ts))
                c.executemany(
                    "INSERT INTO certificates (id,batch_id,row_index,recipient_name,recipient_email,"
                    "achievement,status,error,code,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", records)
        except sqlite3.IntegrityError:
            # Lost a race with a concurrent request using the same idempotency key.
            existing = self._batch_by_key(idempotency_key) if idempotency_key else None
            if existing:
                return existing, False
            raise

        self.enqueue(pending_ids)  # only after commit, so workers always see the rows
        return batch_id, True

    def _batch_by_key(self, key):
        with self.db.tx() as c:
            row = c.execute("SELECT id FROM batches WHERE idempotency_key=?", (key,)).fetchone()
        return row["id"] if row else None

    # ---------- queue ----------
    def enqueue(self, cert_ids):
        for cid in cert_ids:
            self.executor.submit(self._process_safe, cid)

    def recover(self):
        """Re-queue work interrupted by a crash or restart."""
        with self.db.tx() as c:
            c.execute("UPDATE certificates SET status='pending' WHERE status='processing'")
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM certificates WHERE status='pending' ORDER BY created_at, row_index")]
        if ids:
            log.info("Recovering %d unfinished certificates", len(ids))
            self.enqueue(ids)

    def _process_safe(self, cert_id):
        try:
            self._process(cert_id)
        except Exception:  # never let a worker thread die silently
            log.exception("Unexpected error processing %s", cert_id)

    def _process(self, cert_id):
        with self.db.tx() as c:
            claimed = c.execute(
                "UPDATE certificates SET status='processing', attempts=attempts+1 "
                "WHERE id=? AND status='pending'", (cert_id,)).rowcount
            if not claimed:
                return  # someone else has it / already done
            row = c.execute(
                "SELECT c.*, b.title, b.event_name, b.issuer, b.issue_date FROM certificates c "
                "JOIN batches b ON b.id=c.batch_id WHERE c.id=?", (cert_id,)).fetchone()

        try:
            pdf = self.renderer({
                "title": row["title"], "event_name": row["event_name"], "issuer": row["issuer"],
                "issue_date": row["issue_date"], "recipient_name": row["recipient_name"],
                "achievement": row["achievement"], "code": row["code"]})
            rel = Path(row["batch_id"]) / f"{cert_id}.pdf"
            self._write_atomic(self.storage / rel, pdf)
        except Exception as exc:
            log.warning("Generation failed for %s (attempt %d): %s", cert_id, row["attempts"], exc)
            retry = row["attempts"] < self.max_attempts
            with self.db.tx() as c:
                c.execute("UPDATE certificates SET status=?, error=? WHERE id=?",
                          ("pending" if retry else "failed", f"{type(exc).__name__}: {exc}"[:500], cert_id))
            if retry:
                self.enqueue([cert_id])
            return

        with self.db.tx() as c:
            c.execute("UPDATE certificates SET status='completed', file_path=?, error=NULL, completed_at=? "
                      "WHERE id=?", (str(rel), now(), cert_id))

    @staticmethod
    def _write_atomic(path: Path, data: bytes):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)  # readers never see a half-written file

    # ---------- queries ----------
    def batch_summary(self, batch_id):
        with self.db.tx() as c:
            b = c.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not b:
                return None
            counts = {r["status"]: r["n"] for r in c.execute(
                "SELECT status, COUNT(*) n FROM certificates WHERE batch_id=? GROUP BY status", (batch_id,))}
        total = b["total"]
        finished = sum(counts.get(s, 0) for s in ("completed", "failed", "invalid"))
        return {
            "id": b["id"], "title": b["title"], "event_name": b["event_name"],
            "issuer": b["issuer"], "issue_date": b["issue_date"], "created_at": b["created_at"],
            "status": derive_status(counts, total),
            "progress": {
                "total": total,
                **{s: counts.get(s, 0) for s in ("pending", "processing", "completed", "failed", "invalid")},
                "percent": round(100 * finished / total, 1) if total else 100.0,
            },
        }
