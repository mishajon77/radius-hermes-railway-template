"""Durable settings, feeds, and drafts for the private radar."""
import base64
import hmac
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from cryptography.fernet import Fernet, InvalidToken


PENDING = (
    "kind='inbox' AND (json_extract(value,'$.draft_state')='pending' "
    "OR json_extract(value,'$.telegram')='pending')"
)
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS records ("
    "kind TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL, "
    "changed INTEGER NOT NULL, PRIMARY KEY(kind,id))",
    "CREATE INDEX IF NOT EXISTS record_order ON records(kind,changed DESC)",
    "CREATE INDEX IF NOT EXISTS inbox_order "
    "ON records(json_extract(value,'$.published_at') DESC,id DESC) WHERE kind='inbox'",
    f"CREATE INDEX IF NOT EXISTS pending_inbox ON records(changed) WHERE {PENDING}",
    "UPDATE records SET value=json_set(value,'$.telegram','unknown') "
    "WHERE kind='inbox' AND json_extract(value,'$.telegram')='sending'",
)


class RadarError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def write(kind, key, value, *, unique=False):
    upsert = "ON CONFLICT(kind,id) DO UPDATE SET value=excluded.value,changed=excluded.changed"
    sql = "INSERT OR IGNORE INTO records VALUES(?,?,?,?)" if unique else "INSERT INTO records VALUES(?,?,?,?) " + upsert
    return sql, (kind, key, json.dumps(value, ensure_ascii=False, allow_nan=False), time.time_ns())


class Store:
    def __init__(self, directory, key, *, remote=None):
        self.remote = remote
        self.schema_lock = Lock()
        self.schema_ready = False
        self.cipher = Fernet(base64.urlsafe_b64encode(
            hmac.digest(key.encode(), b"agant-x-radar-data-v1", "sha256")
        ))
        if remote is None:
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path = directory / "radar.sqlite3"
            with self.connect() as db:
                db.execute("PRAGMA journal_mode=WAL")
            self.initialize()
            self.path.chmod(0o600)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        # A temporary database outage should not prevent Hermes from starting.
        with self.schema_lock:
            if not self.schema_ready:
                self.execute([(sql, ()) for sql in SCHEMA], atomic=True)
                self.schema_ready = True

    def execute(self, commands, atomic=False):
        if self.remote is not None:
            return self.remote.run(commands, atomic)
        results = []
        with self.connect(write=atomic) as db:
            for sql, params in commands:
                cursor = db.execute(sql, params)
                results.append((cursor.fetchall(), max(0, cursor.rowcount)))
        return results

    def run(self, commands, atomic=False):
        if not self.schema_ready:
            self.initialize()
        return self.execute(commands, atomic)

    def get(self, kind, key, default=None):
        [(rows, _)] = self.run([("SELECT value FROM records WHERE kind=? AND id=?", (kind, key))])
        return json.loads(rows[0][0]) if rows else default

    def put(self, kind, key, value):
        self.run([write(kind, key, value)])

    def delete(self, kind, key):
        self.run([("DELETE FROM records WHERE kind=? AND id=?", (kind, key))])

    def secret(self, name):
        encrypted = self.get("secret", name)
        if not encrypted:
            return ""
        try:
            return self.cipher.decrypt(encrypted.encode()).decode()
        except InvalidToken as exc:
            raise RadarError("کلید پنل تغییر کرده است. کلید سرویس را دوباره وارد کن.", 409) from exc

    def set_secret(self, name, value):
        if value:
            self.put("secret", name, self.cipher.encrypt(value.encode()).decode())
        else:
            self.delete("secret", name)

    def reserve(self, date, limit):
        # The update and limit check are one statement for both SQLite backends.
        sql = (
            "INSERT INTO records VALUES('usage',?,'1',?) "
            "ON CONFLICT(kind,id) DO UPDATE SET "
            "value=CAST(CAST(records.value AS INTEGER)+1 AS TEXT),changed=excluded.changed "
            "WHERE CAST(records.value AS INTEGER) < ?"
        )
        [(_, changed)] = self.run([(sql, (date, time.time_ns(), limit))])
        if changed == 0:
            raise RadarError("سقف درخواست سورسا برای امروز پر شده است.", 429)

    def inbox(self, kind="", page=1, page_size=24):
        query = (
            "SELECT inbox.value,draft.value FROM records inbox "
            "LEFT JOIN records draft ON draft.kind='draft' AND draft.id=inbox.id "
            "WHERE inbox.kind='inbox' AND (?='' OR json_extract(inbox.value,'$.kind')=?) "
            "ORDER BY json_extract(inbox.value,'$.published_at') DESC,inbox.id DESC LIMIT ? OFFSET ?"
        )
        [(rows, _)] = self.run([(query, (kind, kind, page_size + 1, (page - 1) * page_size))])
        return {
            "rows": [json.loads(item) | {"draft": json.loads(draft) if draft is not None else None}
                     for item, draft in rows[:page_size]],
            "more": len(rows) > page_size,
        }

    def pending(self, limit):
        [(rows, _)] = self.run([(f"SELECT value FROM records WHERE {PENDING} ORDER BY changed LIMIT ?", (limit,))])
        return [json.loads(row[0]) for row in rows]

    def record_page(self, feed, items, checkpoint):
        commands = [write("inbox", item["post"]["id"], item, unique=True) for item in items]
        commands.append(write("feed", feed, checkpoint))
        results = self.run(commands, atomic=True)
        return sum(count for _, count in results[:-1])

    def record_draft(self, item, draft=None, error=""):
        commands = [write("draft", draft["post"]["id"], draft)] if draft else []
        updates = {"draft_state": "error" if error else "ready", "draft_error": error}
        if item:
            commands.append(write("inbox", item["post"]["id"], item | updates))
        if commands:
            self.run(commands, atomic=True)
        if item:
            item.update(updates)

    def close(self):
        if self.remote is not None:
            self.remote.close()
