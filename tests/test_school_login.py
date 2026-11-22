import io
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from server.local_login import write_session
from server.login_bundle import local_login_bundle
from server.school_login import LoginError, SchoolLoginSession, exchange, main, teaching_credentials


class SchoolLoginTests(unittest.TestCase):
    def test_only_path_specific_teaching_credentials_leave_login_process(self):
        session = requests.Session()
        for name, value, domain, path in [
            ("JSESSIONID", "teaching", "zdbk.zju.edu.cn", "/jwglxt"),
            ("JSESSIONID", "root", "zdbk.zju.edu.cn", "/"),
            ("JSESSIONTOKEN", "teaching-token", "zdbk.zju.edu.cn", "/"),
            ("route", "node-one", "zdbk.zju.edu.cn", "/"),
            ("JSESSIONID", "wrong-path", "zdbk.zju.edu.cn", "/other"),
            ("JSESSIONID", "wrong-host", "zjuam.zju.edu.cn", "/jwglxt"),
            ("iPlanetDirectoryPro", "global-sso", "zjuam.zju.edu.cn", "/"),
            ("JWTUser", "global-user", ".zju.edu.cn", "/"),
            ("JSESSIONPREJSDM", "not-needed", "zdbk.zju.edu.cn", "/jwglxt/xtgl"),
        ]:
            session.cookies.set(name, value, domain=domain, path=path)
        result = teaching_credentials(SimpleNamespace(r=session), "3000000000", "ugrs")
        self.assertEqual(result, {"username": "3000000000", "kind": "ugrs", "mode": "token",
                                  "secret": "teaching", "root_session": "root",
                                  "teaching_token": "teaching-token", "route": "node-one"})

    def test_login_and_redirects_cannot_send_credentials_outside_official_https_hosts(self):
        for url in ["http://zjuam.zju.edu.cn/cas/login", "https://evil.test/",
                    "https://zjuam.zju.edu.cn.evil.test/", "https://zjuam.zju.edu.cn:444/",
                    "https://user@zjuam.zju.edu.cn/", "https://zdbk.zju.edu.cn/"]:
            request = requests.Request("POST", url, data="password=encrypted").prepare()
            with self.subTest(url=url), patch("requests.Session.send") as send:
                with self.assertRaises(LoginError):
                    SchoolLoginSession().send(request)
                send.assert_not_called()

    def test_captcha_or_incomplete_login_is_not_treated_as_success(self):
        with self.assertRaisesRegex(LoginError, "interaction_required"):
            teaching_credentials(SimpleNamespace(r=requests.Session()), "3000000000", "ugrs")

    def test_login_closes_and_clears_global_authentication_state(self):
        client = SimpleNamespace(r=requests.Session(), password="local-only-password", sso_cookie={"global": "sso"})

        def login():
            client.r.cookies.set("JSESSIONID", "teaching-only", domain="zdbk.zju.edu.cn", path="/jwglxt")
            client.r.cookies.set("iPlanetDirectoryPro", "global", domain="zjuam.zju.edu.cn", path="/")

        client.login = login
        with patch("server.school_login.UgrsZjuam", return_value=client):
            credentials = exchange("3000000000", "local-only-password", "ugrs")
        self.assertEqual(credentials["secret"], "teaching-only")
        self.assertEqual(client.password, "")
        self.assertEqual(client.sso_cookie, {})
        self.assertEqual(len(client.r.cookies), 0)

    def test_login_errors_are_redacted(self):
        sensitive = "password-and-sensitive-response"
        output = io.StringIO()
        with (patch("server.school_login.sys.stdin", io.StringIO('{}')),
              patch("server.school_login.sys.stdout", output),
              patch("server.school_login.exchange", side_effect=ValueError(sensitive))):
            main()
        self.assertEqual(json.loads(output.getvalue()), {"error": "login_failed"})
        self.assertNotIn(sensitive, output.getvalue())

    def test_session_file_has_private_permissions_and_never_overwrites(self):
        credentials = {"username": "3000000000", "kind": "ugrs", "mode": "token", "secret": "test-session"}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "private" / "session.json"
            write_session(path, credentials)
            original = path.read_bytes()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                write_session(path, credentials)
            self.assertEqual(path.read_bytes(), original)
            with self.assertRaises(ValueError):
                write_session(Path(folder) / "bad.json", {**credentials, "password": "not-exportable"})

    def test_helper_package_contains_only_public_sources_and_can_run(self):
        with zipfile.ZipFile(io.BytesIO(local_login_bundle())) as bundle:
            names = bundle.namelist()
            self.assertIn("server/local_login.py", names)
            self.assertIn("LICENSE", names)
            self.assertNotIn("server/app.py", names)
            self.assertFalse(any(".secrets" in name or name.endswith(".sqlite3") for name in names))
            with tempfile.TemporaryDirectory() as folder:
                bundle.extractall(folder)
                result = subprocess.run([os.sys.executable, "-m", "server.local_login", "--help"],
                                        cwd=folder, capture_output=True, text=True, check=True)
                self.assertNotIn("--password", result.stdout)


if __name__ == "__main__":
    unittest.main()
