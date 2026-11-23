"""Offline integration and hostile-network fixtures for attached ICS sources."""

import gzip
import io
import json
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from icalendar import Calendar

from server.app import create_app
from server.ics_sources import (MAX_ICS_BYTES, PinnedHTTPSConnection, SourceError, download_ics,
                                merge_calendars, normalize_ics, public_ip, validate_url)
from server.settings import Settings
from server.worker import run_source_once
from tests.test_capture import snapshot
from tests.test_service import FORM, ORIGIN, calendar_text


def events(content):
    return Calendar.from_ical(content).walk("VEVENT")


def recurring(title="Study", zone="Custom/Zone", offset="+0800"):
    return (f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//EN\r\n"
            f"BEGIN:VTIMEZONE\r\nTZID:{zone}\r\nBEGIN:STANDARD\r\n"
            f"DTSTART:19700101T000000\r\nTZOFFSETFROM:{offset}\r\nTZOFFSETTO:{offset}\r\n"
            "END:STANDARD\r\nEND:VTIMEZONE\r\nBEGIN:VEVENT\r\nUID:weekly\r\n"
            f"DTSTART;TZID={zone}:20261123T080000\r\nDTEND;TZID={zone}:20261123T090000\r\n"
            f"RRULE:FREQ=WEEKLY;COUNT=3\r\nEXDATE;TZID={zone}:20261130T080000\r\n"
            f"SUMMARY:{title}\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:weekly\r\n"
            f"RECURRENCE-ID;TZID={zone}:20261207T080000\r\n"
            f"DTSTART;TZID={zone}:20261207T100000\r\nSUMMARY:Exception\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n")


class SourceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(Path(self.temp.name), ORIGIN, Fernet.generate_key())
        self.app = create_app(self.settings)
        self.store = self.app.extensions["store"]
        self.client = self.app.test_client()
        self.fixtures = {}
        self.csrf = self.client.get("/api/session", base_url=ORIGIN).json["csrf"]
        created = self.post("subscriptions", FORM).json
        self.owner = self.store.owner_for_key(created["management_key"])
        self.url = created["subscription"]["url"]
        self.store.finish(self.store.claim(), calendar=calendar_text())

    def post(self, path, data=None):
        response = self.client.post("/api/" + path, base_url=ORIGIN, json=data or {},
                                    headers={"Origin": ORIGIN, "X-CSRF-Token": self.csrf})
        if response.is_json and response.json.get("csrf"):
            self.csrf = response.json["csrf"]
        return response

    def attach(self, title="Custom", **extra):
        content = self.fixtures.setdefault(title, calendar_text(title))
        response = self.post("sources", dict(kind="upload", name="Private source", content=content, **extra))
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["subscription"]["url"], self.url)
        return response.json["subscription"]["sources"][-1]

    def feed(self):
        return self.client.get(self.url, base_url=ORIGIN)

    def test_upload_replace_deduplicate_delete_keep_original_url_and_school_cache(self):
        original = self.store.get(self.owner)["calendar"]
        source = self.attach()
        first = self.feed()
        self.assertEqual(len(events(first.data)), 2)
        custom = [e for e in events(first.data) if str(e["SUMMARY"]) == "Custom"][0]
        duplicate = self.attach()
        self.assertEqual(duplicate["id"], source["id"])
        self.assertEqual(self.feed().headers["ETag"], first.headers["ETag"])
        self.attach("Changed", id=source["id"])
        second = self.feed()
        changed = [e for e in events(second.data) if str(e["SUMMARY"]) == "Changed"][0]
        self.assertEqual(custom["UID"], changed["UID"])
        self.assertGreater(changed["SEQUENCE"], custom["SEQUENCE"])
        self.assertNotEqual(first.headers["ETag"], second.headers["ETag"])
        self.assertEqual(self.client.get(self.url, base_url=ORIGIN, headers={"If-None-Match": second.headers["ETag"]}).status_code, 304)
        self.assertEqual(self.client.head(self.url, base_url=ORIGIN).data, b"")
        self.assertEqual(self.store.get(self.owner)["calendar"], original)
        self.assertEqual(self.post(f"sources/{source['id']}/delete").status_code, 200)
        self.assertEqual(len(events(self.feed().data)), 1)
        self.assertEqual(self.post(f"sources/{source['id']}/delete").status_code, 404)

    def test_school_worker_and_new_snapshot_keep_custom_events(self):
        source = self.attach()
        with self.store.connection() as db:
            db.execute("UPDATE subscriptions SET due=0 WHERE id=?", (self.owner,))
        self.store.finish(self.store.claim(), calendar=calendar_text("School changed"))
        self.assertEqual({str(e["SUMMARY"]) for e in events(self.feed().data)}, {"School changed", "Custom"})
        response = self.post("local-capture", {"capture": snapshot(), "export": "all", "consent": True})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["subscription"]["url"], self.url)
        self.assertEqual(response.json["subscription"]["sources"][0]["id"], source["id"])
        self.assertIn("Custom", {str(e["SUMMARY"]) for e in events(self.feed().data)})

    def test_source_can_be_ready_before_first_school_sync(self):
        with self.store.connection() as db:
            db.execute("UPDATE subscriptions SET calendar=NULL WHERE id=?", (self.owner,))
        self.assertEqual(self.feed().status_code, 503)
        self.attach()
        self.assertEqual(self.feed().status_code, 200)
        self.assertEqual(len(events(self.feed().data)), 1)

    def test_bad_upload_preserves_cache_and_owner_and_csrf_are_required(self):
        source = self.attach()
        first = self.feed().data
        bad = self.post("sources", {"kind": "upload", "name": "bad", "id": source["id"], "content": "not a calendar"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.feed().data, first)
        self.assertEqual(self.client.post("/api/sources", base_url=ORIGIN, json={}).status_code, 403)
        other = self.app.test_client()
        csrf = other.get("/api/session", base_url=ORIGIN).json["csrf"]
        self.assertEqual(other.post("/api/sources", base_url=ORIGIN, json={}, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}).status_code, 401)
        owner, _ = self.store.create({"username": "3000000001", "kind": "ugrs", "mode": "snapshot"}, {}, manual=True)
        with self.assertRaises(LookupError):
            self.store.save_source(owner, source_id=source["id"], name="other", kind="upload", content=calendar_text())
        self.assertFalse(self.store.delete_source(owner, source["id"]))
        with self.assertRaises(LookupError):
            self.store.refresh_source(owner, source["id"])

    def test_remote_worker_success_unchanged_failure_replacement_delete_and_cascade(self):
        url = "https://public.example.test/feed.ics?secret=must-stay-private"
        response = self.post("sources", {"kind": "url", "name": "Private remote", "url": url})
        self.assertEqual(response.status_code, 200, response.json)
        source = response.json["subscription"]["sources"][0]
        self.assertNotIn("must-stay-private", json.dumps(response.json))
        self.assertTrue(source["queued"])
        self.assertEqual(self.post(f"sources/{source['id']}/refresh").status_code, 429)
        job = self.store.claim_source()
        self.assertIsNone(self.store.claim_source())
        remote_content = normalize_ics(calendar_text("Remote"))
        self.store.finish_source(job, calendar=remote_content)
        first = self.feed()
        with self.store.connection() as db:
            db.execute("UPDATE calendar_sources SET due=0,requested=0 WHERE id=?", (source["id"],))
        same = self.store.claim_source()
        self.store.finish_source(same, calendar=remote_content)
        self.assertEqual(self.feed().headers["ETag"], first.headers["ETag"])
        with self.store.connection() as db:
            db.execute("UPDATE calendar_sources SET due=0 WHERE id=?", (source["id"],))
        self.store.finish_source(self.store.claim_source(), calendar=remote_content.replace("Remote", "Remote changed"))
        changed = self.feed()
        self.assertEqual({str(e["UID"]) for e in events(first.data)}, {str(e["UID"]) for e in events(changed.data)})
        self.assertNotEqual(first.headers["ETag"], changed.headers["ETag"])
        first = changed
        self.assertEqual(self.post(f"sources/{source['id']}/refresh").status_code, 200)
        failed = self.store.claim_source()
        self.store.finish_source(failed, error="source_unavailable")
        self.assertEqual(self.feed().data, first.data)
        with self.store.connection() as db:
            db.execute("UPDATE calendar_sources SET due=0 WHERE id=?", (source["id"],))
        stale = self.store.claim_source()
        self.post("sources", {"id": source["id"], "kind": "url", "name": "Private remote", "url": "https://other.example.test/new.ics"})
        self.store.finish_source(stale, calendar=normalize_ics(calendar_text("Stale result")))
        self.assertEqual(self.feed().data, first.data)
        current = self.store.claim_source()
        self.store.delete_source(self.owner, source["id"])
        self.store.finish_source(current, calendar=normalize_ics(calendar_text("Deleted result")))
        self.assertFalse(self.store.sources(self.owner))
        self.attach()
        self.post("delete")
        self.assertFalse(self.store.sources(self.owner))
        self.assertEqual(self.feed().status_code, 404)

    def test_encryption_restart_limit_and_old_database_upgrade(self):
        self.attach()
        response = self.post("sources", {"kind": "url", "name": "Private remote", "url": "https://public.example.test/feed?hidden-read-key"})
        self.assertEqual(response.status_code, 200)
        with self.store.connection() as db:
            rows = db.execute("SELECT name,url,calendar FROM calendar_sources").fetchall()
        for row in rows:
            for value in row:
                if value:
                    for private in (b"Private", b"Custom", b"hidden-read-key", b"BEGIN:VCALENDAR"):
                        self.assertNotIn(private, value)
        app = create_app(self.settings)
        self.assertEqual(app.test_client().get(self.url, base_url=ORIGIN).data, self.feed().data)
        for index in range(8):
            self.attach(f"Custom {index}")
        self.assertEqual(self.post("sources", {"kind": "upload", "name": "over limit", "content": calendar_text("eleventh")}).status_code, 400)
        self.assertEqual(len(self.store.sources(self.owner)), 10)
        self.store.delete(self.owner)
        with self.store.connection() as db:
            db.execute("DROP TABLE calendar_sources")
        # Existing subscription schema needs no changed columns or token regeneration.
        app = create_app(self.settings)
        self.assertTrue(app.extensions["store"])

    def test_worker_subprocess_success_timeout_and_redacted_errors(self):
        self.post("sources", {"kind": "url", "name": "Remote", "url": "https://public.example.test/feed"})
        def completed(*args, **kwargs):
            self.assertNotIn("https://", str(args))
            return SimpleNamespace(stdout=json.dumps({"calendar": calendar_text("Remote")}))
        with patch("server.worker.subprocess.run", side_effect=completed):
            self.assertTrue(run_source_once(self.store))
        self.assertEqual(len(events(self.feed().data)), 2)
        with self.store.connection() as db:
            db.execute("UPDATE calendar_sources SET due=0")
        with patch("server.worker.subprocess.run", side_effect=subprocess.TimeoutExpired("test", 40)):
            self.assertTrue(run_source_once(self.store))
        self.assertEqual(self.store.sources(self.owner)[0]["error"], "timeout")
        self.assertEqual(len(events(self.feed().data)), 2)
        self.assertFalse(run_source_once(self.store))


class CalendarMergeTests(unittest.TestCase):
    def test_timezones_recurrence_exceptions_and_all_day_survive_without_uid_collisions(self):
        first = normalize_ics(recurring())
        second = normalize_ics(recurring("Other", offset="+0900"))
        sources = [dict(id=identity, calendar=content, updated=1795393600, revision=1)
                   for identity, content in [("first", first), ("second", second)]]
        result = Calendar.from_ical(merge_calendars(calendar_text(), sources))
        rows = result.walk("VEVENT")
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[1]["UID"], rows[2]["UID"])
        self.assertNotEqual(rows[1]["UID"], rows[3]["UID"])
        zones = {str(z["TZID"]) for z in result.walk("VTIMEZONE") if str(z["TZID"]).startswith("zjuical-source-")}
        self.assertEqual(len(zones), 2)
        for row in rows[1:]:
            self.assertIn(str(row["DTSTART"].params["TZID"]), zones)
        self.assertEqual(rows[1]["DTSTART"].dt.utcoffset().total_seconds(), 8 * 3600)
        self.assertEqual(rows[3]["DTSTART"].dt.utcoffset().total_seconds(), 9 * 3600)
        self.assertEqual(rows[1]["RRULE"]["COUNT"], [3])
        self.assertIn("EXDATE", rows[1])
        day = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:day\r\nDTSTART;VALUE=DATE:20261123\r\nSUMMARY:Day\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        source = dict(id="day", calendar=day, updated=1795393600, revision=1)
        self.assertEqual(str(events(merge_calendars(None, [source]))[0]["DTSTART"].params["VALUE"]), "DATE")

    def test_rejects_missing_ids_partial_multiple_and_invalid_components(self):
        valid = recurring()
        for content in ["bad", valid.replace("UID:weekly\r\n", ""), valid + valid,
                        valid[:-20], valid.replace("20261123T080000", "broken"),
                        valid.replace("VERSION:2.0", "VERSION:1.0"), "x" * (MAX_ICS_BYTES + 1)]:
            with self.subTest(kind=content[:35]), self.assertRaises(SourceError):
                normalize_ics(content)


class DownloaderTests(unittest.TestCase):
    def dns(self, addresses):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]

    def connection(self, body=b"", status=200, headers=None):
        response = Mock(status=status)
        response.read.side_effect = io.BytesIO(body).read
        response.getheader.side_effect = lambda key, default=None: (headers or {}).get(key, default)
        connection = Mock()
        connection.getresponse.return_value = response
        return connection

    def test_rejects_private_local_multicast_mapped_and_transition_addresses(self):
        for address in ["127.0.0.1", "10.70.0.130", "172.26.20.3", "192.168.50.23", "169.254.169.254",
                        "100.64.0.1", "::1", "fd00::1", "fe80::1", "224.0.0.1", "ff02::1",
                        "::ffff:8.8.8.8", "2002:0808:0808::1", "64:ff9b::a00:1"]:
            with self.subTest(address=address):
                self.assertFalse(public_ip(address))
        self.assertTrue(public_ip("8.8.8.8"))
        self.assertTrue(public_ip("2606:4700:4700::1111"))
        for url in ["http://example.com/a", "file:///etc/passwd", "https://user:pass@example.com/a",
                    "https://127.0.0.1/a", "https://[::1]/a", "https://example.com:8443/a",
                    "https://example.com/a#fragment", "https://example.com/\r\nx", "https://[fe80::1%eth0]/"]:
            with self.subTest(url=url), self.assertRaises(SourceError):
                validate_url(url)

    def test_dns_mixed_or_rebound_redirect_never_connects_to_private_address(self):
        with patch("server.ics_sources.socket.getaddrinfo", return_value=self.dns(["8.8.8.8", "10.0.0.1"])), patch("server.ics_sources.PinnedHTTPSConnection") as pool:
            with self.assertRaises(SourceError):
                download_ics("https://public.example.test/feed")
            pool.assert_not_called()
        redirect = self.connection(status=302, headers={"Location": "https://rebound.example.test/feed"})
        with patch("server.ics_sources.socket.getaddrinfo", side_effect=[self.dns(["8.8.8.8"]), self.dns(["127.0.0.1"])]), patch("server.ics_sources.PinnedHTTPSConnection", return_value=redirect) as pool:
            with self.assertRaises(SourceError):
                download_ics("https://public.example.test/feed")
            self.assertEqual(pool.call_count, 1)
            redirect.close.assert_called_once()

    def test_download_is_pinned_preserves_query_and_never_sends_credentials(self):
        body = calendar_text("Remote").encode()
        connection = self.connection(body)
        with patch("server.ics_sources.socket.getaddrinfo", return_value=self.dns(["8.8.8.8"])), patch("server.ics_sources.PinnedHTTPSConnection", return_value=connection) as pool:
            self.assertIn("Remote", download_ics("https://public.example.test/feed?read-key=private"))
            pool.assert_called_once_with("public.example.test", "8.8.8.8")
            args, kwargs = connection.request.call_args
            self.assertEqual(args, ("GET", "/feed?read-key=private"))
            self.assertNotIn("Cookie", kwargs["headers"])
            self.assertNotIn("Authorization", kwargs["headers"])
        sock, context = Mock(), Mock()
        with patch("server.ics_sources.ssl.create_default_context", return_value=context), patch("server.ics_sources.socket.create_connection", return_value=sock) as connect:
            pinned = PinnedHTTPSConnection("public.example.test", "8.8.8.8")
            pinned.connect()
            connect.assert_called_once_with(("8.8.8.8", 443), 10)
            context.wrap_socket.assert_called_once_with(sock, server_hostname="public.example.test")

    def test_size_encoding_gzip_bomb_invalid_content_and_redirect_bounds(self):
        for connection in [self.connection(b"x" * (MAX_ICS_BYTES + 1)), self.connection(b"<html>Login</html>"),
                           self.connection(b"\xff"), self.connection(status=401),
                           self.connection(gzip.compress(b"x" * (MAX_ICS_BYTES + 1)), headers={"Content-Encoding": "gzip"}),
                           self.connection(status=302, headers={"Location": "/loop"})]:
            with patch("server.ics_sources.socket.getaddrinfo", return_value=self.dns(["8.8.8.8"])), patch("server.ics_sources.PinnedHTTPSConnection", return_value=connection):
                with self.assertRaises(SourceError):
                    download_ics("https://public.example.test/feed")
        good = self.connection(gzip.compress(calendar_text("Gzip").encode()), headers={"Content-Encoding": "gzip"})
        with patch("server.ics_sources.socket.getaddrinfo", return_value=self.dns(["8.8.8.8"])), patch("server.ics_sources.PinnedHTTPSConnection", return_value=good):
            self.assertIn("Gzip", download_ics("https://public.example.test/feed"))


if __name__ == "__main__":
    unittest.main()
