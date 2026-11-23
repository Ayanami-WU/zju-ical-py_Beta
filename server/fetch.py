"""Isolated upstream job. Secrets enter via stdin, only structured results leave stdout."""

import contextlib
import io
import json
import sys
import time
from urllib.parse import urlsplit

import requests
from icalendar import Calendar
from loguru import logger

from main.integration import getCalender
from server.settings import ROOT
from utils.config import config
from zjuam.grs import GrsZjuam
from zjuam.ugrs import UgrsZjuam


class UpstreamAuthError(Exception):
    pass


class BoundedSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.max_redirects = 8

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "zdbk.zju.edu.cn",
            "yjsy.zju.edu.cn",
        }:
            raise ValueError("服务仅允许访问固定教务接口")
        kwargs.setdefault("timeout", (10, 25))
        if parsed.hostname == "zdbk.zju.edu.cn" and parsed.path.startswith("/jwglxt/xskscx/"):
            headers = dict(kwargs.get("headers") or {})
            headers["Referer"] = (
                "https://zdbk.zju.edu.cn/jwglxt/xskscx/kscx_cxXsgrksIndex.html?gnmkdm=N509070&layout=default"
            )
            kwargs["headers"] = headers
        # An expired session must not forward a custom token header to a CAS redirect.
        kwargs["allow_redirects"] = False
        response = super().request(method, url, **kwargs)
        if response.status_code in {301, 302, 303, 307, 308, 401, 403, 901}:
            raise UpstreamAuthError
        return response


def _fetch_once(payload, *, session_updates=None):
    credentials, options = payload["credentials"], payload["options"]
    username, secret = credentials["username"], credentials["secret"]
    kind, mode = credentials["kind"], credentials["mode"]
    if mode != "token":
        raise UpstreamAuthError
    client = (UgrsZjuam if kind == "ugrs" else GrsZjuam)(username, "", 1.5)
    headers = dict(client.r.headers)
    client.r.close()
    client.r = BoundedSession()
    client.r.headers.update(headers)
    try:
        if kind == "ugrs":
            # Receive only the undergraduate-system session, never the global CAS cookie.
            client.r.cookies.set("JSESSIONID", secret, domain="zdbk.zju.edu.cn", path="/jwglxt")
            client.r.headers.update(
                {
                    "Origin": "https://zdbk.zju.edu.cn",
                    "Referer": "https://zdbk.zju.edu.cn/jwglxt/kbcx/xskbcx_cxXskbcxIndex.html?gnmkdm=N2151&layout=default",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                }
            )
            if credentials.get("teaching_token"):
                client.r.cookies.set(
                    "JSESSIONTOKEN",
                    credentials["teaching_token"],
                    domain="zdbk.zju.edu.cn",
                    path="/",
                )
            # Preserve both copied cookie paths in a deterministic order.
            if credentials.get("root_session"):
                client.r.cookies.set(
                    "JSESSIONID", credentials["root_session"], domain="zdbk.zju.edu.cn", path="/"
                )
            if credentials.get("route"):
                client.r.cookies.set(
                    "route", credentials["route"], domain="zdbk.zju.edu.cn", path="/"
                )
        else:
            # Graduate API explicitly expects its own X-Access-Token.
            client._token = secret
        config.load(str(ROOT / "configs" / options["config"]))
        content, _ = getCalender(
            username,
            "",
            exams_only=options["export"] == "exams",
            courses_only=options["export"] == "courses",
            skip_verification_and_use=kind,
            include_todos=options["include_todos"],
            authenticated_client=client,
            uid_namespace=payload["id"],
        )
        if kind == "ugrs" and client.student_info.get("xh") != username:
            raise UpstreamAuthError
        # Graduate responses do not currently expose a verified student identifier;
        # ownership is the independent management key, never the submitted student number.
        # The existing todo API needs its own courses.zju session, not this token.
        parsed = Calendar.from_ical(content)
        for event in parsed.walk("VEVENT"):
            event["SEQUENCE"] = payload["sequence"]
            event["CREATED"] = payload["created"]
            event["LAST-MODIFIED"] = payload["modified"]
            event["DTSTAMP"] = payload["modified"]
        if kind == "ugrs" and session_updates is not None:
            for cookie in client.r.cookies:
                if cookie.name == "route" and cookie.domain.lstrip(".") == "zdbk.zju.edu.cn":
                    session_updates["route"] = cookie.value
        return parsed.to_ical().decode()
    finally:
        client.r.close()


def fetch(payload, *, session_updates=None):
    credentials = payload["credentials"]
    if credentials["mode"] != "token":
        raise UpstreamAuthError
    # Without affinity, a copied session may land on another portal node. Each
    # attempt starts from the supplied cookies: a failed node issues anonymous
    # JSESSIONID cookies which must not replace the user's authenticated session.
    attempts = 3 if credentials["kind"] == "ugrs" and not credentials.get("route") else 1
    for attempt in range(attempts):
        try:
            return _fetch_once(payload, session_updates=session_updates)
        except UpstreamAuthError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5)


def main():
    logger.remove()
    try:
        payload = json.loads(sys.stdin.read(32769))
        # Legacy parsers can log response bodies or student info. Suppress them entirely.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            updates = {}
            calendar = fetch(payload, session_updates=updates)
        result = {"calendar": calendar, "session_updates": updates}
    except UpstreamAuthError:
        result = {"error": "authentication_failed"}
    except requests.exceptions.JSONDecodeError:
        result = {"error": "refresh_failed"}
    except requests.RequestException:
        result = {"error": "upstream_unavailable"}
    except BaseException:
        result = {"error": "refresh_failed"}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
