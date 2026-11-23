import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime

from server.ics_sources import SOURCE_ERRORS, SourceError, normalize_ics
from server.settings import ROOT, Settings
from server.store import Store


def run_once(store):
    job = store.claim()
    if job is None:
        return False
    now = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "id": job["id"],
        "credentials": store.decrypt(job["credentials"]),
        "options": json.loads(job["options"]),
        "sequence": int(time.time()),
        "created": datetime.fromtimestamp(job["created"], UTC).strftime("%Y%m%dT%H%M%SZ"),
        "modified": now,
    }
    try:
        result = subprocess.run(
            [sys.executable, "-m", "server.fetch"],
            input=json.dumps(payload),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=store.settings.job_timeout,
            cwd=ROOT,
            check=True,
        )
        response = json.loads(result.stdout)
        calendar = response.get("calendar")
        if not isinstance(calendar, str) or not calendar.startswith("BEGIN:VCALENDAR"):
            error = response.get("error")
            if error not in {"authentication_failed", "upstream_unavailable", "refresh_failed"}:
                error = "refresh_failed"
            store.finish(job, error=error)
        else:
            store.finish(job, calendar=calendar, credential_updates=response.get("session_updates"))
    except subprocess.TimeoutExpired:
        store.finish(job, error="timeout")
    except Exception:
        store.finish(job, error="refresh_failed")
    return True


def main():
    os.umask(0o077)
    store = Store(Settings.from_env())
    while True:
        try:
            source_work = run_source_once(store)
            school_work = run_once(store)
            if not source_work and not school_work:
                time.sleep(3)
        except Exception as error:
            print(f"Worker error: {type(error).__name__}", flush=True)
            time.sleep(5)


def run_source_once(store):
    job = store.claim_source()
    if job is None:
        return False
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "server.ics_sources"],
            input=json.dumps({"url": store.decrypt(job["url"])}), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=40, check=True, cwd=ROOT,
        )
        result = json.loads(completed.stdout)
        if isinstance(result.get("calendar"), str):
            store.finish_source(job, calendar=normalize_ics(result["calendar"]))
        else:
            error = result.get("error")
            store.finish_source(job, error=error if error in SOURCE_ERRORS else "source_unavailable")
    except subprocess.TimeoutExpired:
        store.finish_source(job, error="timeout")
    except SourceError as error:
        store.finish_source(job, error=error.code)
    except Exception:
        store.finish_source(job, error="source_unavailable")
    return True


if __name__ == "__main__":
    main()
