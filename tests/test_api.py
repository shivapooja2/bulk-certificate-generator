import io
import tempfile
import time
import unittest
import zipfile

from app import create_app

PAYLOAD = {
    "title": "Certificate of Completion",
    "event_name": "Advanced Python Bootcamp 2026",
    "issuer": "Acme Academy",
    "issue_date": "2026-10-01",
    "recipients": [
        {"name": "Asha Verma", "email": "asha@example.com", "achievement": "with distinction"},
        {"name": "Rohan Mehta", "email": "rohan@example.com"},
        {"name": "Priya Nair-Subramanian", "email": "priya@example.com"},
    ],
}


def payload(**over):
    p = {**PAYLOAD, "recipients": [dict(r) for r in PAYLOAD["recipients"]]}
    p.update(over)
    return p


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({"DATABASE": f"{self.tmp.name}/t.db", "STORAGE_DIR": f"{self.tmp.name}/certs",
                               "TESTING": True, "MAX_RECIPIENTS": 50})
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions["generator"].executor.shutdown(wait=True)
        self.tmp.cleanup()

    def wait_done(self, batch_id, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            b = self.client.get(f"/api/v1/batches/{batch_id}").get_json()
            if b["status"] not in ("queued", "processing"):
                return b
            time.sleep(0.05)
        self.fail("batch did not finish")

    def test_happy_path(self):
        r = self.client.post("/api/v1/batches", json=payload())
        self.assertEqual(r.status_code, 202)
        body = r.get_json()
        self.assertEqual((body["accepted"], body["rejected"]), (3, 0))
        self.assertIn("Location", r.headers)

        b = self.wait_done(body["batch_id"])
        self.assertEqual(b["status"], "completed")
        self.assertEqual(b["progress"]["completed"], 3)
        self.assertEqual(b["progress"]["percent"], 100.0)

        certs = self.client.get(f"/api/v1/batches/{body['batch_id']}/certificates").get_json()
        self.assertEqual(certs["total"], 3)
        pdf = self.client.get(certs["results"][0]["download_url"])
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF"))

        z = self.client.get(f"/api/v1/batches/{body['batch_id']}/download")
        self.assertEqual(len(zipfile.ZipFile(io.BytesIO(z.data)).namelist()), 3)

        code = certs["results"][0]["code"]
        v = self.client.get(f"/api/v1/certificates/verify/{code}")
        self.assertTrue(v.get_json()["valid"])
        self.assertEqual(self.client.get("/api/v1/certificates/verify/NOPE").status_code, 404)

    def test_partial_validation(self):
        p = payload()
        p["recipients"] += [
            {"name": "", "email": "x@example.com"},
            {"name": "Bad Email", "email": "not-an-email"},
            {"name": "Dup", "email": "ASHA@example.com"},  # duplicate (case-insensitive)
        ]
        r = self.client.post("/api/v1/batches", json=p)
        body = r.get_json()
        self.assertEqual(r.status_code, 202)
        self.assertEqual((body["accepted"], body["rejected"]), (3, 3))
        self.assertEqual([x["index"] for x in body["rejected_recipients"]], [3, 4, 5])

        b = self.wait_done(body["batch_id"])
        self.assertEqual(b["status"], "completed_with_errors")
        self.assertEqual(b["progress"]["invalid"], 3)
        invalid = self.client.get(f"/api/v1/batches/{body['batch_id']}/certificates?status=invalid").get_json()
        self.assertEqual(invalid["total"], 3)
        self.assertIsNone(invalid["results"][0]["download_url"])

    def test_batch_level_validation(self):
        for bad in [payload(title=""), payload(recipients=[]), payload(issue_date="01/10/2026"),
                    payload(recipients=[{"name": "A", "email": "a@b.co"}] * 51)]:
            r = self.client.post("/api/v1/batches", json=bad)
            self.assertEqual(r.status_code, 400, bad.get("title"))
        self.assertEqual(self.client.post("/api/v1/batches", data="nope").status_code, 400)

    def test_all_invalid_creates_nothing(self):
        r = self.client.post("/api/v1/batches", json=payload(recipients=[{"name": "x"}]))
        self.assertEqual(r.status_code, 422)

    def test_idempotency_key(self):
        h = {"Idempotency-Key": "abc-123"}
        a = self.client.post("/api/v1/batches", json=payload(), headers=h)
        b = self.client.post("/api/v1/batches", json=payload(), headers=h)
        self.assertEqual((a.status_code, b.status_code), (202, 200))
        self.assertEqual(a.get_json()["batch_id"], b.get_json()["batch_id"])
        self.wait_done(a.get_json()["batch_id"])

    def test_not_found_and_not_ready(self):
        self.assertEqual(self.client.get("/api/v1/batches/nope").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/certificates/nope/download").status_code, 404)

    def test_failure_retries_then_marks_failed(self):
        gen = self.app.extensions["generator"]
        real, calls = gen.renderer, {"boom": 0}

        def flaky(d):
            if d["recipient_name"] == "Rohan Mehta":
                calls["boom"] += 1
                raise RuntimeError("render exploded")
            return real(d)

        gen.renderer = flaky
        body = self.client.post("/api/v1/batches", json=payload()).get_json()
        b = self.wait_done(body["batch_id"])
        self.assertEqual(b["status"], "completed_with_errors")
        self.assertEqual((b["progress"]["completed"], b["progress"]["failed"]), (2, 1))
        self.assertEqual(calls["boom"], 3)  # MAX_ATTEMPTS
        failed = self.client.get(f"/api/v1/batches/{body['batch_id']}/certificates?status=failed").get_json()
        self.assertIn("render exploded", failed["results"][0]["error"])
        r = self.client.get(f"/api/v1/certificates/{failed['results'][0]['id']}/download")
        self.assertEqual(r.status_code, 409)

    def test_pagination(self):
        p = payload(recipients=[{"name": f"P{i}", "email": f"p{i}@example.com"} for i in range(7)])
        bid = self.client.post("/api/v1/batches", json=p).get_json()["batch_id"]
        self.wait_done(bid)
        r = self.client.get(f"/api/v1/batches/{bid}/certificates?page=2&page_size=3").get_json()
        self.assertEqual([x["index"] for x in r["results"]], [3, 4, 5])
        self.assertEqual(r["total"], 7)

    def test_recover_after_restart(self):
        gen = self.app.extensions["generator"]
        bid = self.client.post("/api/v1/batches", json=payload()).get_json()["batch_id"]
        self.wait_done(bid)
        # simulate a crash that left rows mid-flight
        with gen.db.tx() as c:
            c.execute("UPDATE certificates SET status='processing', file_path=NULL WHERE batch_id=?", (bid,))
        gen.recover()
        self.assertEqual(self.wait_done(bid)["status"], "completed")

    def test_long_names_render(self):
        from app.pdf import render_certificate
        pdf = render_certificate({"title": "T" * 100, "event_name": "E " * 70, "issuer": "I" * 60,
                                  "issue_date": "2026-01-01", "recipient_name": "W" * 100,
                                  "achievement": "A " * 70, "code": "CERT-X-00001"})
        self.assertTrue(pdf.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
