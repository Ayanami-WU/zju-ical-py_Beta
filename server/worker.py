import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime

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
            if not run_once(store):
                time.sleep(3)
        except Exception as error:
            print(f"Worker error: {type(error).__name__}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
