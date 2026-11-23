"""Bounded ICS parsing/merging and an isolated, public-HTTPS-only downloader."""

import gzip
import hashlib
import http.client
import io
import ipaddress
import json
import socket
import ssl
import sys
from datetime import UTC, datetime
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from icalendar import Calendar

MAX_ICS_BYTES = 524288
MAX_COMPONENTS = 2000
MAX_SOURCES = 10
SOURCE_ERRORS = {"unsafe_url", "invalid_ics", "source_unavailable", "timeout"}


class SourceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def parse_ics(content):
    """Reject partial/ambiguous input without exposing calendar text in errors."""
    try:
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_ICS_BYTES or "\x00" in content:
            raise ValueError
        text = content.lstrip("\ufeff").strip()
        if not text.startswith("BEGIN:VCALENDAR") or not text.endswith("END:VCALENDAR"):
            raise ValueError
        calendars = Calendar.from_ical(text, multiple=True)
        if len(calendars) != 1:
            raise ValueError
        result = calendars[0]
        if str(result.get("VERSION")) != "2.0":
            raise ValueError
        components = result.walk()
        if len(components) > MAX_COMPONENTS or any(item.errors for item in components):
            raise ValueError
        for item in result.subcomponents:
            allowed_children = {"STANDARD", "DAYLIGHT"} if item.name == "VTIMEZONE" else {"VALARM"}
            if any(child.name not in allowed_children or child.subcomponents for child in item.subcomponents):
                raise ValueError
        seen, zones = {}, set()
        for item in result.subcomponents:
            if item.name == "VTIMEZONE":
                zone = item.get("TZID")
                if not zone or isinstance(zone, list) or str(zone) in zones:
                    raise ValueError
                zones.add(str(zone))
            elif item.name in {"VEVENT", "VTODO", "VJOURNAL"}:
                uid = item.get("UID")
                if not uid or isinstance(uid, list) or len(str(uid)) > 1024:
                    raise ValueError
                for field in ("DTSTART", "DTEND", "DUE", "DTSTAMP", "RECURRENCE-ID"):
                    value = item.get(field)
                    if value is not None and (isinstance(value, list) or not hasattr(value, "dt")):
                        raise ValueError
                if item.name == "VEVENT" and "DTSTART" not in item and not (
                    str(item.get("STATUS")) == "CANCELLED" and "RECURRENCE-ID" in item
                ):
                    raise ValueError
                recurrence = item.get("RECURRENCE-ID")
                key = (str(uid), recurrence.to_ical() if recurrence is not None else b"")
                if key in seen:
                    raise ValueError
                seen[key] = True
            else:
                raise ValueError
        return result
    except Exception:
        raise SourceError("invalid_ics") from None


def normalize_ics(content):
    try:
        encoded = parse_ics(content).to_ical()
        if len(encoded) > MAX_ICS_BYTES:
            raise ValueError
        return encoded.decode("utf-8")
    except Exception:
        raise SourceError("invalid_ics") from None


def merge_calendars(base, sources):
    """Only cached input; source-scoped UIDs/TZIDs preserve recurrence groups."""
    if not sources:
        return base
    if base:
        result = Calendar.from_ical(base)
    else:
        result = Calendar()
        result.add("PRODID", "-//ZJU Calendar//Merged subscription//ZH")
        result.add("VERSION", "2.0")
        result.add("X-WR-CALNAME", "课程与自定义日历")
    for source in sources:
        parsed = parse_ics(source["calendar"])
        prefix = "zjuical-source-" + source["id"]
        uid_for = lambda uid: prefix + "-" + hashlib.sha256(str(uid).encode()).hexdigest() + "@zjuical"
        zones = {str(item["TZID"]): prefix + "/" + str(item["TZID"])
                 for item in parsed.subcomponents if item.name == "VTIMEZONE"}
        modified = datetime.fromtimestamp(source["updated"], UTC)
        for component in parsed.subcomponents:
            if component.name == "VTIMEZONE":
                component["TZID"] = zones[str(component["TZID"])]
            else:
                component["UID"] = uid_for(component["UID"])
                component["SEQUENCE"] = source["revision"]
                for field in ("DTSTAMP", "LAST-MODIFIED"):
                    component.pop(field, None)
                    component.add(field, modified)
                relations = component.get("RELATED-TO")
                if relations is not None:
                    values = relations if isinstance(relations, list) else [relations]
                    del component["RELATED-TO"]
                    for value in values:
                        component.add("RELATED-TO", uid_for(value), parameters=value.params)
            for part in component.walk():
                for value in part.values():
                    for prop in value if isinstance(value, list) else [value]:
                        params = getattr(prop, "params", {})
                        if str(params.get("TZID", "")) in zones:
                            params["TZID"] = zones[str(params["TZID"])]
            result.add_component(component)
    # A subscription is a snapshot, not an invitation/cancellation transaction.
    result.pop("METHOD", None)
    return result.to_ical().decode("utf-8")


def public_ip(address):
    ip = ipaddress.ip_address(address)
    return (ip.is_global and not ip.is_multicast and not ip.is_reserved
            and not getattr(ip, "ipv4_mapped", None) and not getattr(ip, "sixtofour", None)
            and not getattr(ip, "teredo", None)
            and not (ip.version == 6 and ip in ipaddress.ip_network("64:ff9b::/96")))


def validate_url(value):
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError
        parts = urlsplit(value)
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.fragment or parts.port not in {None, 443}):
            raise ValueError
        host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if not host or any(c in host for c in "%\\/#?@"):
            raise ValueError
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            if not all(label and len(label) <= 63 and all(c.isalnum() or c == "-" for c in label)
                       for label in host.split(".")):
                raise ValueError
        else:
            if not public_ip(str(ip)):
                raise ValueError
        authority = f"[{host}]" if ":" in host else host
        return urlunsplit(("https", authority, quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                           quote(parts.query, safe="%/?@:!$&'()*+,;=-._~"), ""))
    except (ValueError, UnicodeError):
        raise SourceError("unsafe_url") from None


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Keep TLS SNI/certificate verification on the hostname, connect to vetted IP."""
    def __init__(self, host, address):
        super().__init__(host, 443, timeout=10, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def download_ics(url):
    """No proxy environment, cookies, credentials, internal addresses or DNS rebind."""
    for _ in range(4):
        url = validate_url(url)
        parts = urlsplit(url)
        addresses = socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not public_ip(item[4][0]) for item in addresses):
            raise SourceError("unsafe_url")
        # The subprocess deadline bounds DNS, all redirects and slow responses.
        connection = PinnedHTTPSConnection(parts.hostname, addresses[0][4][0])
        try:
            target = parts.path + ("?" + parts.query if parts.query else "")
            connection.request("GET", target, headers={"Accept": "text/calendar, */*;q=0.1",
                               "Accept-Encoding": "identity", "User-Agent": "ZJUCalendar/1.0"})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise SourceError("source_unavailable")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise SourceError("source_unavailable")
            content = response.read(MAX_ICS_BYTES + 1)
            if len(content) > MAX_ICS_BYTES:
                raise SourceError("invalid_ics")
            encoding = response.getheader("Content-Encoding", "identity").lower()
            if encoding == "gzip":
                with gzip.GzipFile(fileobj=io.BytesIO(content)) as archive:
                    content = archive.read(MAX_ICS_BYTES + 1)
            elif encoding not in {"", "identity"}:
                raise SourceError("invalid_ics")
            if len(content) > MAX_ICS_BYTES:
                raise SourceError("invalid_ics")
            try:
                return normalize_ics(content.decode("utf-8-sig"))
            except UnicodeError:
                raise SourceError("invalid_ics") from None
        finally:
            connection.close()
    raise SourceError("source_unavailable")


def main():
    try:
        payload = json.loads(sys.stdin.read(8193))
        result = {"calendar": download_ics(payload["url"])}
    except SourceError as error:
        result = {"error": error.code}
    except BaseException:
        result = {"error": "source_unavailable"}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
