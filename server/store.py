import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager

from cryptography.fernet import Fernet

from server.settings import Settings


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cipher = Fernet(settings.encryption_key)
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = settings.data_dir / "subscriptions.sqlite3"
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY, credentials BLOB NOT NULL, options TEXT NOT NULL,
                    feed_hash TEXT NOT NULL UNIQUE, feed_secret BLOB NOT NULL,
                    manage_hash TEXT NOT NULL UNIQUE, calendar BLOB,
                    created REAL NOT NULL, updated REAL, due REAL NOT NULL,
                    requested REAL NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
                    lease TEXT, lease_until REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    hash TEXT PRIMARY KEY, owner TEXT, csrf TEXT NOT NULL, expires REAL NOT NULL,
                    FOREIGN KEY(owner) REFERENCES subscriptions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS limits (
                    key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value BLOB NOT NULL);
                CREATE INDEX IF NOT EXISTS subscriptions_due ON subscriptions(due, lease_until);
            """)
            check = db.execute("SELECT value FROM metadata WHERE key='encryption-check'").fetchone()
            if check:
                try:
                    assert self.decrypt(check["value"]) == "zjuical-service-v1"
                except Exception:
                    raise ValueError(
                        "加密密钥与现有数据库不匹配；请恢复原密钥，不要删除数据库"
                    ) from None
            else:
                db.execute(
                    "INSERT INTO metadata VALUES ('encryption-check', ?)",
                    (self.encrypt("zjuical-service-v1"),),
                )
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def encrypt(self, value):
        return self.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode())

    def decrypt(self, value):
        return json.loads(self.cipher.decrypt(value))

    def new_session(self, owner=None):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (digest(token), owner, csrf, time.time() + self.settings.session_ttl),
            )
        return token, csrf

    def session(self, token):
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM sessions WHERE hash=? AND expires>?", (digest(token), time.time())
            ).fetchone()

    def drop_session(self, token):
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE hash=?", (digest(token),))

    def limit(self, category, value, maximum, seconds):
        # Hash IPs with the installation key; do not retain raw client addresses.
        key = hmac.new(
            self.settings.encryption_key, f"{category}:{value}".encode(), "sha256"
        ).hexdigest()
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM limits WHERE expires<?", (now,))
            row = db.execute("SELECT count FROM limits WHERE key=?", (key,)).fetchone()
            if row and row["count"] >= maximum:
                return False
            db.execute(
                "INSERT INTO limits VALUES (?, 1, ?) ON CONFLICT(key) DO UPDATE SET count=count+1",
                (key, now + seconds),
            )
        return True

    def create(self, credentials, options, *, owner=None, calendar=None, manual=False):
        owner = owner or secrets.token_urlsafe(32)
        feed, manage = (secrets.token_urlsafe(32) for _ in range(2))
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT count(*) FROM subscriptions").fetchone()[0]
            if count >= self.settings.max_subscriptions:
                raise ValueError("服务已达到订阅数量上限")
            db.execute(
                """INSERT INTO subscriptions
                   (id, credentials, options, feed_hash, feed_secret, manage_hash, created, due, requested, calendar, updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    owner,
                    self.encrypt(credentials),
                    json.dumps(options),
                    digest(feed),
                    self.encrypt(feed),
                    digest(manage),
                    now,
                    1e30 if manual else now,
                    now,
                    self.encrypt(calendar) if calendar is not None else None,
                    now if calendar is not None else None,
                ),
            )
        return owner, manage

    def get(self, owner):
        with self.connection() as db:
            return db.execute("SELECT * FROM subscriptions WHERE id=?", (owner,)).fetchone()

    def owner_for_key(self, key):
        with self.connection() as db:
            row = db.execute(
                "SELECT id FROM subscriptions WHERE manage_hash=?", (digest(key),)
            ).fetchone()
            return row["id"] if row else None

    def feed(self, token):
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM subscriptions WHERE feed_hash=?", (digest(token),)
            ).fetchone()

    def update(self, owner, credentials, options):
        with self.connection() as db:
            db.execute(
                """UPDATE subscriptions SET credentials=?, options=?, version=version+1,
                   failures=0, error='', due=?, requested=? WHERE id=?""",
                (self.encrypt(credentials), json.dumps(options), time.time(), time.time(), owner),
            )

    def refresh(self, owner):
        now = time.time()
        with self.connection() as db:
            cursor = db.execute(
                "UPDATE subscriptions SET due=?, requested=? WHERE id=? AND requested<=?",
                (now, now, owner, now - 60),
            )
            return cursor.rowcount == 1

    def replace_snapshot(self, owner, credentials, options, calendar, version):
        now = time.time()
        with self.connection() as db:
            cursor = db.execute(
                """UPDATE subscriptions SET credentials=?, options=?, calendar=?, updated=?,
                   due=1e30, requested=?, version=version+1, failures=0, error='', lease=NULL, lease_until=0
                   WHERE id=? AND version=?""",
                (self.encrypt(credentials), json.dumps(options), self.encrypt(calendar), now, now, owner, version),
            )
            return cursor.rowcount == 1

    def rotate(self, owner, *, management=False):
        token = secrets.token_urlsafe(32)
        with self.connection() as db:
            if management:
                db.execute(
                    "UPDATE subscriptions SET manage_hash=? WHERE id=?", (digest(token), owner)
                )
                db.execute("DELETE FROM sessions WHERE owner=?", (owner,))
            else:
                db.execute(
                    "UPDATE subscriptions SET feed_hash=?, feed_secret=? WHERE id=?",
                    (digest(token), self.encrypt(token), owner),
                )
        return token

    def delete(self, owner):
        with self.connection() as db:
            db.execute("DELETE FROM subscriptions WHERE id=?", (owner,))

    def claim(self):
        now, lease = time.time(), secrets.token_hex(16)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM subscriptions WHERE due<=? AND lease_until<? ORDER BY due LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE subscriptions SET lease=?, lease_until=? WHERE id=?",
                (lease, now + self.settings.job_timeout + 60, row["id"]),
            )
            return {**dict(row), "lease": lease}

    def finish(self, job, *, calendar=None, error="", credential_updates=None):
        now = time.time()
        failures = job["failures"] + 1 if error else 0
        delay = min(21600, 900 * 2 ** min(failures - 1, 5)) if error else self.settings.interval
        # Three failed refreshes suspend retries until credentials are updated or manually retried.
        due = now + delay if failures < 3 else 253402300799
        updated_credentials = None
        if calendar is not None and isinstance(credential_updates, dict):
            route = credential_updates.get("route")
            if (
                isinstance(route, str)
                and 1 <= len(route) <= 512
                and route.isascii()
                and not any(c.isspace() or c in ';,"\\' for c in route)
            ):
                credentials = self.decrypt(job["credentials"])
                credentials["route"] = route
                updated_credentials = self.encrypt(credentials)
        with self.connection() as db:
            db.execute(
                """UPDATE subscriptions SET calendar=COALESCE(?, calendar), credentials=COALESCE(?, credentials), updated=CASE WHEN ?='' THEN ? ELSE updated END,
                   error=?, failures=?, due=?, lease=NULL, lease_until=0
                   WHERE id=? AND version=? AND lease=?""",
                (
                    self.encrypt(calendar) if calendar is not None else None,
                    updated_credentials,
                    error,
                    now,
                    error,
                    failures,
                    due,
                    job["id"],
                    job["version"],
                    job["lease"],
                ),
            )
            # A credential edit during fetching must not let the old task overwrite newer data.
            db.execute(
                "UPDATE subscriptions SET lease=NULL, lease_until=0 WHERE id=? AND lease=?",
                (job["id"], job["lease"]),
            )
