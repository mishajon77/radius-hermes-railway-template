"""Durable settings, feeds, and drafts for the private radar."""
import base64
import hmac
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class RadarError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Store:
    def __init__(self, directory, key):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "radar.sqlite3"
        self.cipher = Fernet(base64.urlsafe_b64encode(
            hmac.digest(key.encode(), b"agant-x-radar-data-v1", "sha256")
        ))
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS records ("
                "kind TEXT NOT NULL, id TEXT NOT NULL, value TEXT NOT NULL, "
                "changed INTEGER NOT NULL, PRIMARY KEY(kind,id))"
            )
            db.execute("CREATE INDEX IF NOT EXISTS record_order ON records(kind,changed DESC)")
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

    def get(self, kind, key, default=None):
        with self.connect() as db:
            return self.read(db, kind, key, default)

    @staticmethod
    def read(db, kind, key, default=None):
        row = db.execute("SELECT value FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def write(db, kind, key, value, *, unique=False):
        values = (kind, key, json.dumps(value, ensure_ascii=False, allow_nan=False), time.time_ns())
        if unique:
            return db.execute("INSERT OR IGNORE INTO records VALUES(?,?,?,?)", values).rowcount == 1
        db.execute(
            "INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(kind,id) "
            "DO UPDATE SET value=excluded.value,changed=excluded.changed", values,
        )
        return True

    def put(self, kind, key, value):
        with self.connect(write=True) as db:
            self.write(db, kind, key, value)

    def delete(self, kind, key):
        with self.connect(write=True) as db:
            db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, key))

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
        with self.connect(write=True) as db:
            used = self.read(db, "usage", date, 0)
            if used >= limit:
                raise RadarError("سقف درخواست سورسا برای امروز پر شده است.", 429)
            self.write(db, "usage", date, used + 1)

    def inbox(self, kind="", page=1, page_size=24):
        query = (
            "SELECT value FROM records WHERE kind='inbox' "
            "AND (?='' OR json_extract(value,'$.kind')=?) "
            "ORDER BY json_extract(value,'$.published_at') DESC, id DESC LIMIT ? OFFSET ?"
        )
        with self.connect() as db:
            items = [json.loads(row[0]) for row in db.execute(
                query, (kind, kind, page_size + 1, (page - 1) * page_size)
            )]
            for item in items[:page_size]:
                item["draft"] = self.read(db, "draft", item["post"]["id"])
        return {"rows": items[:page_size], "more": len(items) > page_size}

    def pending(self, limit):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT value FROM records WHERE kind='inbox' "
                "AND (json_extract(value,'$.draft_state')='pending' "
                "OR json_extract(value,'$.telegram')='pending') ORDER BY changed LIMIT ?", (limit,)
            )]

    def record_draft(self, item, draft=None, error=""):
        with self.connect(write=True) as db:
            if draft:
                self.write(db, "draft", draft["post"]["id"], draft)
            if item:
                item["draft_state"] = "error" if error else "ready"
                item["draft_error"] = error
                self.write(db, "inbox", item["post"]["id"], item)
