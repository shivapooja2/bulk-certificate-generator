"""Request validation. Batch-level problems reject the whole request (400);
recipient-level problems only reject that row, so one typo in 5,000 rows
doesn't block the other 4,999."""
import re
from datetime import date

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

MAX_NAME = 100
MAX_TEXT = 150


class BatchValidationError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


def _clean(value) -> str:
    return re.sub(r"\s+", " ", CTRL_RE.sub(" ", value)).strip()


def _required_text(data, field, errors, max_len=MAX_TEXT):
    v = data.get(field)
    if not isinstance(v, str) or not _clean(v):
        errors.append(f"'{field}' is required and must be a non-empty string")
        return None
    v = _clean(v)
    if len(v) > max_len:
        errors.append(f"'{field}' must be at most {max_len} characters")
        return None
    return v


def validate_batch(data, max_recipients):
    """Returns (params, rows). rows is a list of dicts, one per submitted
    recipient, in order: {index, valid, name, email, achievement, errors}."""
    if not isinstance(data, dict):
        raise BatchValidationError(["Request body must be a JSON object"])

    errors = []
    title = _required_text(data, "title", errors)
    event_name = _required_text(data, "event_name", errors)
    issuer = _required_text(data, "issuer", errors)

    issue_date = date.today()
    if data.get("issue_date") is not None:
        try:
            issue_date = date.fromisoformat(str(data["issue_date"]))
        except ValueError:
            errors.append("'issue_date' must be an ISO date (YYYY-MM-DD)")

    recipients = data.get("recipients")
    if not isinstance(recipients, list) or not recipients:
        errors.append("'recipients' must be a non-empty list")
    elif len(recipients) > max_recipients:
        errors.append(f"'recipients' may contain at most {max_recipients} entries")

    if errors:
        raise BatchValidationError(errors)

    rows, seen_emails = [], set()
    for i, r in enumerate(recipients):
        row_errors, name, email, achievement = [], None, None, None
        if not isinstance(r, dict):
            row_errors.append("recipient must be an object")
        else:
            n = r.get("name")
            if not isinstance(n, str) or not _clean(n):
                row_errors.append("'name' is required")
            elif len(_clean(n)) > MAX_NAME:
                row_errors.append(f"'name' must be at most {MAX_NAME} characters")
            else:
                name = _clean(n)

            e = r.get("email")
            if not isinstance(e, str) or not e.strip():
                row_errors.append("'email' is required")
            elif len(e.strip()) > 254 or not EMAIL_RE.match(e.strip()):
                row_errors.append("'email' is not a valid email address")
            else:
                email = e.strip().lower()
                if email in seen_emails:
                    row_errors.append("duplicate email in this request")
                    email = None
                else:
                    seen_emails.add(email)

            a = r.get("achievement")
            if a is not None:
                if not isinstance(a, str) or len(_clean(a)) > MAX_TEXT:
                    row_errors.append(f"'achievement' must be a string of at most {MAX_TEXT} characters")
                elif _clean(a):
                    achievement = _clean(a)

        rows.append({"index": i, "valid": not row_errors, "name": name, "email": email,
                     "achievement": achievement, "errors": row_errors})

    params = {"title": title, "event_name": event_name, "issuer": issuer,
              "issue_date": issue_date.isoformat()}
    return params, rows
