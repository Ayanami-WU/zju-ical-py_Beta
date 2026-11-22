"""Offline conversion of a client-collected undergraduate timetable snapshot."""

import contextlib
import io
import json
import math
import re
import sys

from icalendar import Calendar
from loguru import logger

from course.ugrs_course import UGRSCourseTable
from exam.exam import ExamTable
from main.integration import getCalender
from server.settings import ROOT
from utils.config import config

COURSE_FIELDS = {"xkkh", "xqj", "dsz", "xxq", "kcb", "djj", "skcd"}
EXAM_FIELDS = {"xkkh", "kcmc", "xf", "kssj", "jsmc", "zwxh", "qzkssj", "qzjsmc", "qzzwxh"}


def records(value, fields, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("invalid_capture")
    for row in value:
        if not isinstance(row, dict) or set(row) - fields:
            raise ValueError("invalid_capture")
        for text in row.values():
            if not isinstance(text, str) or len(text) > 4096 or any(ord(c) < 32 for c in text):
                raise ValueError("invalid_capture")
        if not row.get("xkkh") or len(row["xkkh"]) > 120:
            raise ValueError("invalid_capture")
    return value


def validate(capture, export):
    if (not isinstance(capture, dict)
            or set(capture) != {"format", "username", "config", "export", "courses", "exams"}
            or capture["format"] != "zjuical-local-capture-v1"
            or not isinstance(capture["username"], str)
            or not re.fullmatch(r"[0-9]{5,20}", capture["username"])
            or capture["export"] not in {"all", "courses"}
            or export not in {"all", "courses", "exams"}):
        raise ValueError("invalid_capture")
    name = capture["config"]
    if not isinstance(name, str) or name not in {p.name for p in (ROOT / "configs").glob("config*.json")}:
        raise ValueError("invalid_capture")
    expected = json.loads((ROOT / "configs" / name).read_text())["classTerms"]
    courses = capture["courses"]
    if not isinstance(courses, list) or len(courses) != len(expected):
        raise ValueError("invalid_capture")
    seen = set()
    for term in courses:
        if not isinstance(term, dict) or set(term) != {"year", "term", "rows"}:
            raise ValueError("invalid_capture")
        key = f"{term['year']}:{term['term']}"
        if key not in expected or key in seen:
            raise ValueError("invalid_capture")
        seen.add(key)
        for row in records(term["rows"], COURSE_FIELDS, 200):
            if (set(row) != COURSE_FIELDS or not 1 <= int(row["xqj"]) <= 7
                    or not 1 <= int(row["djj"]) <= 14 or not 1 <= int(row["skcd"]) <= 14
                    or int(row["djj"]) + int(row["skcd"]) > 15
                    or "<br>" not in row["kcb"]):
                raise ValueError("invalid_capture")
    if capture["export"] == "courses":
        if capture["exams"] is not None or export != "courses":
            raise ValueError("invalid_capture")
    else:
        for row in records(capture["exams"], EXAM_FIELDS, 500):
            credit = float(row.get("xf", "nan"))
            if not row.get("kcmc") or not math.isfinite(credit) or not 0 <= credit <= 100:
                raise ValueError("invalid_capture")
    return capture


class CapturedClient:
    def __init__(self, capture):
        self.capture = capture
        self.student_info = {"xh": capture["username"]}

    def getCourses(self, year, term, exams):
        rows = next(item["rows"] for item in self.capture["courses"]
                    if item["year"] == year and item["term"] == term.value)
        result = UGRSCourseTable()
        result.fromRes(rows)
        result.merge()
        result.communicate(exams)
        return result

    def getExams(self):
        result = ExamTable()
        result.fromZdbk(self.capture["exams"])
        return result


def build(payload):
    capture = validate(payload["capture"], payload["export"])
    config.load(str(ROOT / "configs" / capture["config"]))
    content, _ = getCalender(
        capture["username"], "", skip_verification_and_use="ugrs",
        courses_only=payload["export"] == "courses", exams_only=payload["export"] == "exams",
        authenticated_client=CapturedClient(capture), uid_namespace=payload["id"],
    )
    parsed = Calendar.from_ical(content)
    for event in parsed.walk("VEVENT"):
        event["SEQUENCE"] = payload["sequence"]
        event["CREATED"] = payload["created"]
        event["LAST-MODIFIED"] = payload["modified"]
        event["DTSTAMP"] = payload["modified"]
    return {"calendar": parsed.to_ical().decode(), "username": capture["username"],
            "config": capture["config"]}


def main():
    logger.remove()
    try:
        payload = json.loads(sys.stdin.read(1048577))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = build(payload)
    except BaseException:
        result = {"error": "invalid_capture"}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
