import copy
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from icalendar import Calendar

from server.app import create_app
from server.capture import build, validate
from server.capture_script import capture_script
from server.settings import Settings
from tests.test_regressions import raw_course, raw_exam

ORIGIN = "https://calendar.example.test"


def snapshot():
    return {"format": "zjuical-local-capture-v1", "username": "3000000000", "config": "config.json",
            "export": "all", "courses": [{"year": "2026-2027", "term": term, "rows": [raw_course()]} for term in ["秋", "冬"]],
            "exams": [raw_exam()]}


class LocalCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Settings(Path(self.temp.name), ORIGIN, Fernet.generate_key()))
        self.client = self.app.test_client()
        self.store = self.app.extensions["store"]
        self.csrf = self.client.get("/api/session", base_url=ORIGIN).json["csrf"]

    def post(self, path="local-capture", capture=None, **options):
        result = self.client.post("/api/" + path, base_url=ORIGIN,
                                  json={"capture": capture or snapshot(), "export": "all", "consent": True, **options},
                                  headers={"Origin": ORIGIN, "X-CSRF-Token": self.csrf})
        if result.is_json and result.json.get("csrf"):
            self.csrf = result.json["csrf"]
        return result

    def test_snapshot_is_immediately_ready_encrypted_and_never_scheduled(self):
        created = self.post()
        self.assertEqual(created.status_code, 201, created.json)
        self.assertTrue(created.json["subscription"]["ready"])
        self.assertEqual(created.json["subscription"]["mode"], "snapshot")
        owner = self.store.owner_for_key(created.json["management_key"])
        row = self.store.get(owner)
        self.assertEqual(self.store.decrypt(row["credentials"]), {"username": "3000000000", "kind": "ugrs", "mode": "snapshot"})
        self.assertIsNone(self.store.claim())
        self.assertNotIn("测试课程".encode(), self.store.path.read_bytes())
        url = created.json["subscription"]["url"]
        first = self.client.get(url)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(Calendar.from_ical(first.data).walk("VEVENT"))
        response = self.post("refresh")
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.store.claim())

    def test_updates_preserve_link_and_uids_and_apply_removals(self):
        created = self.post().json
        url = created["subscription"]["url"]
        before = Calendar.from_ical(self.client.get(url).data).walk("VEVENT")
        changed = snapshot()
        for term in changed["courses"]:
            term["rows"][0]["kcb"] = "改名课程<br>1-8周<br>新教师<br>新教室"
        response = self.post(capture=changed)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["subscription"]["url"], url)
        after = Calendar.from_ical(self.client.get(url).data).walk("VEVENT")
        self.assertEqual({str(e["UID"]) for e in before}, {str(e["UID"]) for e in after})
        for term in changed["courses"]:
            term["rows"] = []
        changed["exams"] = []
        self.assertEqual(self.post(capture=changed).status_code, 200)
        self.assertFalse(Calendar.from_ical(self.client.get(url).data).walk("VEVENT"))

    def test_bad_or_partial_capture_preserves_existing_cache_and_identity(self):
        created = self.post().json
        url = created["subscription"]["url"]
        before = self.client.get(url).data
        for mutation in [lambda c: c["courses"].pop(),
                         lambda c: c.update(password="do-not-accept"),
                         lambda c: c["courses"][0]["rows"][0].update(kcb="title\r\nATTENDEE:injected"),
                         lambda c: c.update(username="3000000001")]:
            value = snapshot()
            mutation(value)
            self.assertEqual(self.post(capture=value).status_code, 400)
            self.assertEqual(self.client.get(url).data, before)

    def test_request_bounds_csrf_consent_and_legacy_credential_limits(self):
        self.assertEqual(self.post(consent=False).status_code, 400)
        response = self.client.post("/api/local-capture", base_url=ORIGIN, json={"capture": snapshot()})
        self.assertEqual(response.status_code, 403)
        large = snapshot()
        for index in range(30):
            large["courses"][0]["rows"].append(raw_course(xkkh=f"(2026-2027-1)-{index:09d}-1", kcb="课" * 500 + "<br>1-8周"))
        self.assertGreater(len(json.dumps(large).encode()), 16384)
        self.assertEqual(self.post(capture=large).status_code, 201)
        response = self.client.post("/api/settings", base_url=ORIGIN, data=b" " * 20000,
                                    content_type="application/json", headers={"Origin": ORIGIN, "X-CSRF-Token": self.csrf})
        self.assertEqual(response.status_code, 413)

    def test_conversion_is_offline_and_rejects_unsupported_fields(self):
        now = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        payload = {"capture": snapshot(), "export": "all", "id": "offline-owner", "sequence": 1, "created": now, "modified": now}
        with patch("requests.sessions.Session.request", side_effect=AssertionError("No school network")):
            self.assertIn("BEGIN:VCALENDAR", build(payload)["calendar"])
        value = copy.deepcopy(payload["capture"])
        value["exams"][0]["JSESSIONID"] = "not-a-course-field"
        with self.assertRaises(ValueError):
            validate(value, "all")

    def test_converting_authorization_to_snapshot_invalidates_inflight_worker(self):
        owner, key = self.store.create({"username": "3000000000", "kind": "ugrs", "mode": "token", "secret": "old-teaching-cookie"},
                                       {"config": "config.json", "export": "all", "include_todos": False})
        self.assertEqual(self.post("login", key=key).status_code, 200)
        job = self.store.claim()
        self.assertIsNotNone(job)
        response = self.post()
        self.assertEqual(response.status_code, 200, response.json)
        before = self.store.get(owner)["calendar"]
        self.store.finish(job, calendar="stale worker result")
        self.assertEqual(self.store.get(owner)["calendar"], before)
        self.assertIsNone(self.store.claim())

    def test_generated_script_is_school_only_and_never_reads_cookie_or_storage(self):
        script = capture_script(ORIGIN)
        self.assertIn("@match        https://zdbk.zju.edu.cn/jwglxt/*", script)
        self.assertNotIn("document.cookie", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("/*__CAPTURE_", script)
        self.assertIn('credentials: "same-origin"', script)
        self.assertEqual(self.client.get("/downloads/zjuical-capture.user.js", base_url=ORIGIN).status_code, 200)
