"""Render a self-contained, school-origin-only capture userscript."""

import json
import re

from course.convert import ugrsClassTermToQueryString
from server.settings import ROOT
from utils.const import Term


def capture_script(public_url):
    configurations = []
    for path in sorted((ROOT / "configs").glob("config*.json")):
        terms = json.loads(path.read_text())["classTerms"]
        values = []
        for value in terms:
            year, term = value.split(":")
            values.append({"year": year, "term": term, "query": ugrsClassTermToQueryString(Term(term))})
        configurations.append({"file": path.name, "label": ", ".join(terms), "terms": values})
    template = (ROOT / "server/capture.user.js").read_text()
    core = re.sub(r"(?m)^export (const|function) ", r"\1 ", (ROOT / "server/static/capture-core.mjs").read_text())
    return (template.replace("/*__CAPTURE_CORE__*/", core)
            .replace("/*__CAPTURE_CONFIGS__*/", json.dumps(configurations, ensure_ascii=False))
            .replace("/*__CAPTURE_IMPORT_URL__*/", json.dumps(public_url)))
