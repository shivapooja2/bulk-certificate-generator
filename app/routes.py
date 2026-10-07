import io
import tempfile
import zipfile

from flask import Blueprint, current_app, jsonify, request, send_file, url_for

from .service import slugify
from .validation import BatchValidationError, validate_batch

bp = Blueprint("api", __name__, url_prefix="/api/v1")


def svc():
    return current_app.extensions["generator"]


def error(http_status, message, **extra):
    return jsonify({"error": message, **extra}), http_status


def _links(batch_id):
    return {
        "status": url_for("api.get_batch", batch_id=batch_id),
        "certificates": url_for("api.list_certificates", batch_id=batch_id),
        "download_all": url_for("api.download_batch", batch_id=batch_id),
    }


def _page_args():
    try:
        page = max(int(request.args.get("page", 1)), 1)
        size = min(max(int(request.args.get("page_size", 50)), 1), 500)
    except ValueError:
        return None, None
    return page, size


@bp.get("/health")
def health():
    return jsonify({"status": "ok"})


@bp.post("/batches")
def create_batch():
    data = request.get_json(silent=True)
    if data is None:
        return error(400, "Request body must be valid JSON (Content-Type: application/json)")
    try:
        params, rows = validate_batch(data, current_app.config["MAX_RECIPIENTS"])
    except BatchValidationError as e:
        return error(400, "Invalid request", details=e.errors)

    rejected = [{"index": r["index"], "errors": r["errors"]} for r in rows if not r["valid"]]
    if len(rejected) == len(rows):
        return error(422, "No valid recipients in request", rejected_recipients=rejected)

    key = (request.headers.get("Idempotency-Key") or "").strip()[:128] or None
    batch_id, created = svc().create_batch(params, rows, key)
    summary = svc().batch_summary(batch_id)

    body = {
        "batch_id": batch_id,
        "status": summary["status"],
        "accepted": len(rows) - len(rejected),
        "rejected": len(rejected),
        "rejected_recipients": rejected,
        "links": _links(batch_id),
    }
    if not created:
        body.update(accepted=summary["progress"]["total"] - summary["progress"]["invalid"],
                    rejected=summary["progress"]["invalid"], rejected_recipients=[],
                    note="Idempotency-Key already used; returning the existing batch")
    resp = jsonify(body)
    resp.status_code = 202 if created else 200  # 202 = accepted for async processing
    resp.headers["Location"] = url_for("api.get_batch", batch_id=batch_id)
    return resp


@bp.get("/batches/<batch_id>")
def get_batch(batch_id):
    summary = svc().batch_summary(batch_id)
    if not summary:
        return error(404, "Batch not found")
    return jsonify({**summary, "links": _links(batch_id)})


@bp.get("/batches/<batch_id>/certificates")
def list_certificates(batch_id):
    if not svc().batch_summary(batch_id):
        return error(404, "Batch not found")
    status = request.args.get("status")
    if status and status not in ("pending", "processing", "completed", "failed", "invalid"):
        return error(400, "Invalid status filter")
    page, size = _page_args()
    if page is None:
        return error(400, "page and page_size must be integers")

    where, args = "batch_id=?", [batch_id]
    if status:
        where += " AND status=?"
        args.append(status)
    with svc().db.tx() as c:
        total = c.execute(f"SELECT COUNT(*) n FROM certificates WHERE {where}", args).fetchone()["n"]
        rows = c.execute(
            f"SELECT * FROM certificates WHERE {where} ORDER BY row_index LIMIT ? OFFSET ?",
            args + [size, (page - 1) * size]).fetchall()
    return jsonify({
        "page": page, "page_size": size, "total": total,
        "results": [{
            "id": r["id"], "index": r["row_index"], "code": r["code"],
            "name": r["recipient_name"], "email": r["recipient_email"],
            "status": r["status"], "error": r["error"], "completed_at": r["completed_at"],
            "download_url": url_for("api.download_certificate", cert_id=r["id"])
            if r["status"] == "completed" else None,
        } for r in rows],
    })


@bp.get("/certificates/<cert_id>/download")
def download_certificate(cert_id):
    with svc().db.tx() as c:
        r = c.execute("SELECT * FROM certificates WHERE id=?", (cert_id,)).fetchone()
    if not r:
        return error(404, "Certificate not found")
    if r["status"] != "completed":
        return error(409, f"Certificate is not ready (status: {r['status']})", status=r["status"])
    path = svc().storage / r["file_path"]
    if not path.exists():
        return error(410, "Certificate file is no longer available")
    return send_file(path, mimetype="application/pdf",
                     download_name=f"{r['code']}_{slugify(r['recipient_name'])}.pdf")


@bp.get("/batches/<batch_id>/download")
def download_batch(batch_id):
    summary = svc().batch_summary(batch_id)
    if not summary:
        return error(404, "Batch not found")
    with svc().db.tx() as c:
        rows = c.execute("SELECT * FROM certificates WHERE batch_id=? AND status='completed' "
                         "ORDER BY row_index", (batch_id,)).fetchall()
    if not rows:
        return error(409, "No certificates are ready yet", status=summary["status"])

    spool = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)  # spills to disk if big
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_STORED) as zf:  # PDFs are already compressed
        for r in rows:
            zf.write(svc().storage / r["file_path"], f"{r['code']}_{slugify(r['recipient_name'])}.pdf")
    spool.seek(0)
    resp = send_file(spool, mimetype="application/zip", download_name=f"certificates_{batch_id[:8]}.zip")
    resp.headers["X-Certificates-Included"] = str(len(rows))
    resp.headers["X-Batch-Status"] = summary["status"]  # zip may be partial while still processing
    return resp


@bp.get("/certificates/verify/<code>")
def verify_certificate(code):
    """Public check that a certificate number is genuine."""
    with svc().db.tx() as c:
        r = c.execute(
            "SELECT c.code, c.recipient_name, c.completed_at, b.title, b.event_name, b.issuer, b.issue_date "
            "FROM certificates c JOIN batches b ON b.id=c.batch_id "
            "WHERE c.code=? AND c.status='completed'", (code,)).fetchone()
    if not r:
        return jsonify({"valid": False}), 404
    return jsonify({"valid": True, **dict(r)})
