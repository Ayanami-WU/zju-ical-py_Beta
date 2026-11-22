"""Offline service integration tests; no school credentials or network required."""

import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from cryptography.fernet import Fernet
from icalendar import Calendar

from course.ugrs_course import UGRSCourseTable
from exam.exam import ExamTable
from ical.ical import Calender
from server.app import create_app
from server.fetch import BoundedSession, UpstreamAuthError, fetch
from server.settings import Settings
from server.store import Store
from server.worker import run_once
from tests.test_regressions import raw_course, raw_exam
from utils.config import config
from utils.const import Term

ORIGIN = "https://calendar.example.test"
FORM = {
    "username": "3000000000",
    "kind": "ugrs",
    "mode": "token",
    "secret": "offline-teaching-session",
    "consent": True,
    "export": "courses",
}


def calendar_text(title="测试课程", location="位置待补", identity="course:one"):
    calendar = Calender()
    calendar.add(
        summary=title,
        location=location,
        description="教师: ",
        start=datetime(2026, 11, 23, 8),
        end=datetime(2026, 11, 23, 9, 35),
        identity=identity,
    )
    return calendar.getICS("离线测试")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name), ORIGIN, Fernet.generate_key())
        self.app = create_app(self.settings)
        self.client = self.app.test_client()
        self.store = self.app.extensions["store"]
        self.csrf = self.client.get("/api/session", base_url=ORIGIN).json["csrf"]

    def post(self, path, data=None, client=None, csrf=None):
        result = (client or self.client).post(
            f"/api/{path}",
            json=data if data is not None else {},
            base_url=ORIGIN,
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf or self.csrf},
        )
        if client is None and result.is_json and result.json.get("csrf"):
            self.csrf = result.json["csrf"]
        return result

    def create(self):
        response = self.post("subscriptions", FORM)
        self.assertEqual(response.status_code, 201, response.json)
        self.created = response.json
        self.owner = self.store.owner_for_key(self.created["management_key"])
        self.feed_path = "/calendar/" + self.created["subscription"]["url"].rsplit("/", 1)[1]
        return response.json

    def seed(self, content=None):
        job = self.store.claim()
        self.store.finish(job, calendar=content or calendar_text())

    def test_page_security_cookies_and_csrf(self):
        response = self.client.get("/", base_url=ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        client = self.app.test_client()
        cookie = client.get("/api/session", base_url=ORIGIN).headers["Set-Cookie"]
        for flag in ["__Host-zjuical=", "Secure", "HttpOnly", "SameSite=Strict", "Path=/"]:
            self.assertIn(flag, cookie)
        self.assertEqual(
            self.client.post("/api/subscriptions", json=FORM, base_url=ORIGIN).status_code, 403
        )
        response = self.client.post(
            "/api/subscriptions",
            json=FORM,
            base_url=ORIGIN,
            headers={"Origin": "https://evil.test", "X-CSRF-Token": self.csrf},
        )
        self.assertEqual(response.status_code, 403)

    def test_public_local_login_download_contains_no_installation_secrets(self):
        import io
        import zipfile

        response = self.client.get("/downloads/local-login.zip", base_url=ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.data)) as bundle:
            self.assertIn("server/school_login.py", bundle.namelist())
            self.assertFalse(any(name.startswith(".secrets/") for name in bundle.namelist()))

    def test_credentials_cache_and_secret_tokens_are_not_plaintext(self):
        created = self.create()
        content = calendar_text()
        self.seed(content)
        row = self.store.get(self.owner)
        self.assertEqual(self.store.decrypt(row["credentials"])["secret"], FORM["secret"])
        self.assertEqual(self.store.decrypt(row["calendar"]), content)
        database = self.store.path.read_bytes()
        for secret in [
            FORM["secret"],
            created["management_key"],
            created["subscription"]["url"].rsplit("/", 1)[1][:-4],
            "测试课程",
        ]:
            self.assertNotIn(secret.encode(), database)
        state = self.client.get("/api/session", base_url=ORIGIN).json
        self.assertNotIn("management_key", state)
        self.assertNotIn(FORM["secret"], json.dumps(state))

    def test_feed_returns_cache_etag_and_supports_head_without_upstream(self):
        self.create()
        self.assertEqual(self.client.get(self.feed_path, base_url=ORIGIN).status_code, 503)
        self.seed()
        response = self.client.get(self.feed_path, base_url=ORIGIN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(Calendar.from_ical(response.data).walk("VEVENT")), 1)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        conditional = self.client.get(
            self.feed_path, base_url=ORIGIN, headers={"If-None-Match": response.headers["ETag"]}
        )
        self.assertEqual(conditional.status_code, 304)
        self.assertEqual(self.client.head(self.feed_path, base_url=ORIGIN).data, b"")

    def test_other_users_and_read_only_token_cannot_manage_subscription(self):
        created = self.create()
        other = self.app.test_client()
        csrf = other.get("/api/session", base_url=ORIGIN).json["csrf"]
        self.assertEqual(self.post("delete", client=other, csrf=csrf).status_code, 401)
        token = created["subscription"]["url"].rsplit("/", 1)[1][:-4]
        self.assertEqual(
            self.post("login", {"key": token}, client=other, csrf=csrf).status_code, 401
        )
        response = self.post("login", {"key": created["management_key"]}, client=other, csrf=csrf)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["subscription"]["url"], created["subscription"]["url"])

    def test_rotating_feed_revokes_old_link_without_losing_cache(self):
        self.create()
        self.seed()
        old = self.feed_path
        response = self.post("rotate")
        new = "/calendar/" + response.json["subscription"]["url"].rsplit("/", 1)[1]
        self.assertNotEqual(new, old)
        self.assertEqual(self.client.get(old, base_url=ORIGIN).status_code, 404)
        self.assertEqual(self.client.get(new, base_url=ORIGIN).status_code, 200)

    def test_rotating_management_revokes_existing_other_sessions(self):
        created = self.create()
        other = self.app.test_client()
        csrf = other.get("/api/session", base_url=ORIGIN).json["csrf"]
        login = self.post("login", {"key": created["management_key"]}, client=other, csrf=csrf)
        other_csrf = login.json["csrf"]
        new = self.post("management-key").json["management_key"]
        self.assertIsNone(self.store.owner_for_key(created["management_key"]))
        self.assertEqual(self.store.owner_for_key(new), self.owner)
        self.assertEqual(self.post("rotate", client=other, csrf=other_csrf).status_code, 403)

    def test_failed_refresh_keeps_cache_and_pauses_after_three_attempts(self):
        self.create()
        self.seed()
        before = self.store.decrypt(self.store.get(self.owner)["calendar"])
        for _ in range(3):
            with self.store.connection() as db:
                db.execute("UPDATE subscriptions SET due=0 WHERE id=?", (self.owner,))
            job = self.store.claim()
            self.store.finish(job, error="authentication_failed")
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.decrypt(self.store.get(self.owner)["calendar"]), before)
        self.assertTrue(
            self.client.get("/api/session", base_url=ORIGIN).json["subscription"]["paused"]
        )

    def test_successful_node_affinity_is_encrypted_and_survives_next_job(self):
        self.create()
        job = self.store.claim()
        self.store.finish(
            job,
            calendar=calendar_text(),
            credential_updates={"route": "learned-node", "secret": "must-not-replace"},
        )
        credentials = self.store.decrypt(self.store.get(self.owner)["credentials"])
        self.assertEqual(credentials["route"], "learned-node")
        self.assertEqual(credentials["secret"], FORM["secret"])
        self.assertNotIn(
            "learned-node", json.dumps(self.client.get("/api/session", base_url=ORIGIN).json)
        )

    def test_lease_excludes_concurrent_workers_and_stale_job_cannot_overwrite_edit(self):
        self.create()
        job = self.store.claim()
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.post("settings", {**FORM, "secret": "new-password"}).status_code, 200)
        self.store.finish(job, calendar=calendar_text("旧任务"))
        row = self.store.get(self.owner)
        self.assertIsNone(row["calendar"])
        self.assertIsNone(row["lease"])
        self.assertEqual(self.store.decrypt(row["credentials"])["secret"], "new-password")
        self.assertIsNotNone(self.store.claim())

    def test_deleted_subscription_cannot_be_resurrected_by_running_job(self):
        self.create()
        job = self.store.claim()
        self.assertEqual(self.post("delete").status_code, 200)
        self.store.finish(job, calendar=calendar_text())
        self.assertIsNone(self.store.get(self.owner))
        self.assertEqual(self.client.get(self.feed_path, base_url=ORIGIN).status_code, 404)

    def test_input_consent_traversal_and_unsupported_modes_rejected(self):
        for overrides in [
            {"consent": False},
            {"config": "../../.env"},
            {"kind": "admin"},
            {"secret": ""},
            {"include_todos": True},
            {"config": []},
            {"mode": "token", "secret": "Cookie: bad value"},
        ]:
            with self.subTest(overrides=overrides):
                response = self.post("subscriptions", {**FORM, **overrides})
                # The five-attempt creation quota also limits invalid credentials.
                self.assertIn(response.status_code, {400, 429})

    def test_persistent_rate_limits_and_manual_refresh_cooldown(self):
        self.create()
        self.assertEqual(self.post("refresh").status_code, 429)
        with self.store.connection() as db:
            db.execute("UPDATE subscriptions SET requested=0")
        self.assertEqual(self.post("refresh").status_code, 200)
        self.assertTrue(self.store.limit("test", "ip", 1, 60))
        self.assertFalse(self.store.limit("test", "ip", 1, 60))

    def test_worker_hard_timeout_preserves_previous_calendar(self):
        self.create()
        self.seed()
        with self.store.connection() as db:
            db.execute("UPDATE subscriptions SET due=0")
        with patch(
            "server.worker.subprocess.run", side_effect=subprocess.TimeoutExpired("fetch", 180)
        ) as run:
            self.assertTrue(run_once(self.store))
            self.assertEqual(run.call_args.kwargs["timeout"], 180)
            self.assertNotIn(FORM["secret"], " ".join(run.call_args.args[0]))
        self.assertIsNotNone(self.store.get(self.owner)["calendar"])
        self.assertEqual(self.store.get(self.owner)["error"], "timeout")

    def test_worker_only_accepts_known_redacted_error_codes(self):
        self.create()
        with patch(
            "server.worker.subprocess.run",
            return_value=SimpleNamespace(stdout=json.dumps({"error": "SECRET response body"})),
        ):
            self.assertTrue(run_once(self.store))
        self.assertEqual(self.store.get(self.owner)["error"], "refresh_failed")

    def test_insecure_public_configuration_fails_closed(self):
        with self.assertRaises(ValueError):
            Settings(
                Path(self.temp.name),
                "http://public.example",
                Fernet.generate_key(),
                cookie_secure=False,
            )

    def test_password_mode_is_rejected_even_with_valid_csrf_and_consent(self):
        result = self.post("subscriptions", {**FORM, "mode": "password"})
        self.assertEqual(result.status_code, 400)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM subscriptions").fetchone()[0], 0)

    def test_undergraduate_route_is_encrypted_and_not_returned_in_state(self):
        route = "offline-route-cookie"
        extras = {
            "route": route,
            "root_session": "offline-root-session",
            "teaching_token": "offline-teaching-token",
        }
        result = self.post("subscriptions", {**FORM, **extras})
        self.assertEqual(result.status_code, 201)
        owner = self.store.owner_for_key(result.json["management_key"])
        for key, value in extras.items():
            self.assertEqual(self.store.decrypt(self.store.get(owner)["credentials"])[key], value)
            self.assertNotIn(value, json.dumps(result.json))

    def test_complete_cookie_headers_and_route_injection_are_rejected(self):
        for overrides in [
            {"secret": "value;route=other"},
            {"route": "value;other=bad"},
            {"route": "two values"},
            {"kind": "grs", "route": "route-value"},
        ]:
            self.assertEqual(self.post("subscriptions", {**FORM, **overrides}).status_code, 400)

    def test_wrong_encryption_key_fails_without_touching_subscription_data(self):
        self.create()
        with self.assertRaises(ValueError):
            Store(Settings(self.settings.data_dir, ORIGIN, Fernet.generate_key()))
        self.assertIsNotNone(self.store.get(self.owner))


class CalendarIdentityTests(unittest.TestCase):
    def test_course_metadata_changes_keep_uid(self):
        root = Path(__file__).resolve().parents[1]
        config.load(str(root / "configs/config.json"))
        term = next(t for t in config.termConfigs if t.Term == Term.Winter)
        table = UGRSCourseTable()
        table.fromRes([raw_course()])
        before = table.toEvents(term)
        table.courses[0].teacher = "新教师"
        table.courses[0].location = "新教室"
        table.courses[0].credit = 3
        after = table.toEvents(term)
        self.assertEqual([e.uid for e in before], [e.uid for e in after])
        self.assertEqual(len({e.uid for e in after}), len(after))

    def test_exam_time_and_location_updates_keep_uid(self):
        table = UGRSCourseTable()
        table.fromRes([raw_course()])
        exams = ExamTable()
        exams.fromZdbk([raw_exam()])
        before = exams.toEvents(table)[0]
        exams.exams[0].isEventGenerated = False
        exams.exams[0].start = datetime(2027, 1, 6, 9)
        exams.exams[0].location = "新教室"
        after = exams.toEvents(table)[0]
        self.assertEqual(before.uid, after.uid)
        self.assertNotEqual(before.start, after.start)

    def test_fetch_uses_verified_ugrs_identity_and_normalizes_ics(self):
        client = Mock()
        client.r.headers = {}
        client.student_info = {"xh": FORM["username"]}
        payload = {
            "id": "separate-owner",
            "credentials": {
                **FORM,
                "root_session": "offline-root-session",
                "teaching_token": "offline-teaching-token",
            },
            "options": {"config": "config.json", "export": "courses", "include_todos": False},
            "sequence": 7,
            "created": "20261117T010000Z",
            "modified": "20261117T020000Z",
        }
        with (
            patch("server.fetch.UgrsZjuam", return_value=client),
            patch("server.fetch.getCalender", return_value=(calendar_text(), "test.ics")) as get,
        ):
            result = fetch(payload)
            self.assertEqual(get.call_args.kwargs["uid_namespace"], "separate-owner")
            cookies = get.call_args.kwargs["authenticated_client"].r.cookies
            self.assertEqual(
                cookies.get("JSESSIONID", domain="zdbk.zju.edu.cn", path="/jwglxt"), FORM["secret"]
            )
            self.assertEqual(
                cookies.get("JSESSIONID", domain="zdbk.zju.edu.cn", path="/"),
                "offline-root-session",
            )
            self.assertEqual(
                cookies.get("JSESSIONTOKEN", domain="zdbk.zju.edu.cn", path="/"),
                "offline-teaching-token",
            )
            self.assertNotIn("iPlanetDirectoryPro", cookies)
            prepared = get.call_args.kwargs["authenticated_client"].r.prepare_request(
                requests.Request("POST", "https://zdbk.zju.edu.cn/jwglxt/kbcx/xskbcx_cxXsKb.html")
            )
            self.assertEqual(
                prepared.headers["Cookie"],
                f"JSESSIONID={FORM['secret']}; JSESSIONTOKEN=offline-teaching-token; JSESSIONID=offline-root-session",
            )
            self.assertEqual(
                get.call_args.kwargs["authenticated_client"].r.headers["X-Requested-With"],
                "XMLHttpRequest",
            )
        event = Calendar.from_ical(result).walk("VEVENT")[0]
        self.assertEqual(int(event["SEQUENCE"]), 7)
        self.assertEqual(event.decoded("DTSTAMP").hour, 2)
        client.student_info = {"xh": "3000000001"}
        with (
            patch("server.fetch.UgrsZjuam", return_value=client),
            patch("server.fetch.getCalender", return_value=(calendar_text(), "test.ics")),
            self.assertRaises(UpstreamAuthError),
        ):
            fetch(payload)

    def test_all_upstream_requests_have_timeouts(self):
        session = BoundedSession()
        with patch("requests.Session.request") as request:
            session.get("https://zdbk.zju.edu.cn/jwglxt/")
        self.assertEqual(request.call_args.kwargs["timeout"], (10, 25))
        self.assertFalse(request.call_args.kwargs["allow_redirects"])
        with self.assertRaises(ValueError):
            session.get("https://zjuam.zju.edu.cn/")
        with self.assertRaises(ValueError):
            session.get("https://attacker.invalid/")

    def test_fetch_never_accepts_password_mode(self):
        with self.assertRaises(UpstreamAuthError):
            fetch({"credentials": {**FORM, "mode": "password"}, "options": {}})

    def test_login_redirect_is_authentication_failure_not_network_failure(self):
        session = BoundedSession()
        for status in [302, 901]:
            with (
                patch("requests.Session.request", return_value=SimpleNamespace(status_code=status)),
                self.assertRaises(UpstreamAuthError),
            ):
                session.post("https://zdbk.zju.edu.cn/jwglxt/kbcx/xskbcx_cxXsKb.html")

    def test_exam_request_uses_exam_referer(self):
        with patch(
            "requests.Session.request", return_value=SimpleNamespace(status_code=200)
        ) as request:
            BoundedSession().post("https://zdbk.zju.edu.cn/jwglxt/xskscx/kscx_cxXsgrksIndex.html")
        self.assertIn("/xskscx/", request.call_args.kwargs["headers"]["Referer"])

    def test_initial_node_selection_retries_boundedly_but_known_route_does_not(self):
        payload = {"credentials": {**FORM}}
        with (
            patch(
                "server.fetch._fetch_once", side_effect=[UpstreamAuthError(), "calendar"]
            ) as attempt,
            patch("server.fetch.time.sleep"),
        ):
            self.assertEqual(fetch(payload), "calendar")
            self.assertEqual(attempt.call_count, 2)
        with (
            patch("server.fetch._fetch_once", side_effect=UpstreamAuthError()) as attempt,
            patch("server.fetch.time.sleep"),
            self.assertRaises(UpstreamAuthError),
        ):
            fetch(payload)
        self.assertEqual(attempt.call_count, 3)
        payload["credentials"]["route"] = "known-node"
        with (
            patch("server.fetch._fetch_once", side_effect=UpstreamAuthError()) as attempt,
            self.assertRaises(UpstreamAuthError),
        ):
            fetch(payload)
        self.assertEqual(attempt.call_count, 1)


if __name__ == "__main__":
    unittest.main()
