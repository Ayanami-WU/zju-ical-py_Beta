"""One-shot school login: stdin in, teaching-session allowlist out.

This module is not an HTTP endpoint. Callers must run it in a short-lived
process, bound its total lifetime, and never persist its password input.
"""

import contextlib
import io
import json
import re
import sys
from urllib.parse import urlsplit

import requests
from loguru import logger

from zjuam.grs import GrsZjuam
from zjuam.ugrs import UgrsZjuam


class LoginError(Exception):
    pass


class SchoolLoginSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.max_redirects = 6

    @staticmethod
    def check_url(url):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in {None, 443}
                or parsed.hostname not in {"zjuam.zju.edu.cn", "zdbk.zju.edu.cn", "yjsy.zju.edu.cn"}):
            raise LoginError("login_failed")

    def request(self, method, url, **kwargs):
        self.check_url(url)
        kwargs["timeout"] = (10, 20)
        kwargs["verify"] = True
        return super().request(method, url, **kwargs)

    def send(self, request, **kwargs):
        # Requests follows redirects through send(), bypassing request().
        self.check_url(request.url)
        if request.body and urlsplit(request.url).hostname != "zjuam.zju.edu.cn":
            raise LoginError("login_failed")
        kwargs["verify"] = True
        kwargs["timeout"] = (10, 20)
        return super().send(request, **kwargs)


def teaching_credentials(client, username, kind):
    result = {"username": username, "kind": kind, "mode": "token"}
    if kind == "grs":
        token = getattr(client, "_token", "")
        if not isinstance(token, str) or not token:
            raise LoginError("interaction_required")
        result["secret"] = token
    else:
        candidates = {}
        names = {("JSESSIONID", "/jwglxt"): "secret", ("JSESSIONID", ""): "root_session",
                 ("JSESSIONTOKEN", ""): "teaching_token", ("route", ""): "route",
                 ("route", "/jwglxt"): "route"}
        for cookie in client.r.cookies:
            if cookie.domain.lstrip(".") != "zdbk.zju.edu.cn":
                continue
            key = names.get((cookie.name, cookie.path.rstrip("/")))
            if key:
                candidates.setdefault(key, set()).add(cookie.value)
        if any(len(values) != 1 for values in candidates.values()):
            raise LoginError("login_failed")
        result.update({key: next(iter(values)) for key, values in candidates.items()})
        if not result.get("secret"):
            raise LoginError("interaction_required")
    for key in ["secret", "root_session", "teaching_token", "route"]:
        value = result.get(key, "")
        if (not isinstance(value, str) or len(value) > (512 if key == "route" else 4096) or not value.isascii()
                or any(c.isspace() or c in '\r\n;,"\\' for c in value)):
            raise LoginError("login_failed")
    return result


def exchange(username, password, kind):
    if (not isinstance(username, str) or not re.fullmatch(r"[0-9]{5,20}", username)
            or not isinstance(password, str) or not 1 <= len(password) <= 1024
            or kind not in {"ugrs", "grs"}):
        raise LoginError("login_failed")
    client = (UgrsZjuam if kind == "ugrs" else GrsZjuam)(username, password, 1.5)
    headers = dict(client.r.headers)
    client.r.close()
    client.r = SchoolLoginSession()
    client.r.headers.update(headers)
    try:
        client.login()
        return teaching_credentials(client, username, kind)
    finally:
        client.password = ""
        if hasattr(client, "sso_cookie"):
            client.sso_cookie = {}
        client.r.cookies.clear()
        client.r.close()


def main():
    logger.remove()
    try:
        payload = json.loads(sys.stdin.read(16385))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            credentials = exchange(payload.get("username"), payload.pop("password", None), payload.get("kind"))
        result = {"credentials": credentials}
    except LoginError as error:
        result = {"error": str(error)}
    except requests.RequestException:
        result = {"error": "upstream_unavailable"}
    except BaseException:
        result = {"error": "login_failed"}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
