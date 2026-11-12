"""Durable data shared by the dashboard, scheduler, and Hermes command."""
import base64
import gzip
import hashlib
import hmac
import io
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


def pack(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


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
        changed = time.time_ns()
        values = (kind, key, pack(value), changed)
        if unique:
            return db.execute(
                "INSERT OR IGNORE INTO records VALUES(?,?,?,?)", values
            ).rowcount == 1
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

    def list(self, kind, limit=None):
        query = "SELECT value FROM records WHERE kind=? ORDER BY changed DESC,id"
        params = (kind,) if limit is None else (kind, limit)
        if limit is not None:
            query += " LIMIT ?"
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(query, params)]

    @staticmethod
    def history_query(filters):
        sql = "SELECT value FROM records WHERE kind='event'"
        params = []
        for field in ("type", "account", "actor"):
            if filters.get(field):
                sql += f" AND json_extract(value,'$.{field}')=?"
                params.append(filters[field])
        for field, op in (("start", ">="), ("end", "<=")):
            if filters.get(field):
                sql += f" AND substr(json_extract(value,'$.at'),1,10){op}?"
                params.append(filters[field])
        if filters.get("q"):
            sql += " AND instr(lower(value),lower(?))>0"
            params.append(filters["q"])
        sql += " ORDER BY json_extract(value,'$.at') DESC,changed DESC,id"
        return sql, params

    def history(self, filters, limit, offset=0):
        sql, params = self.history_query(filters)
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(sql + " LIMIT ? OFFSET ?", (*params, limit, offset))]

    def iter_history(self, filters):
        sql, params = self.history_query(filters)
        with self.connect() as db:
            for row in db.execute(sql, params):
                yield json.loads(row[0])

    def unread(self):
        with self.connect() as db:
            return db.execute(
                "SELECT count(*) FROM records WHERE kind='alert' AND json_extract(value,'$.read')=0"
            ).fetchone()[0]

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

    @staticmethod
    def backup_cipher(passphrase, salt):
        key = hashlib.scrypt(passphrase.encode(), salt=salt, n=2**15, r=8, p=1, dklen=32, maxmem=64*1024**2)
        return Fernet(base64.urlsafe_b64encode(key))

    def backup(self, passphrase, salt):
        with self.connect() as db:
            rows = list(db.execute("SELECT kind,id,value,changed FROM records ORDER BY kind,id"))
        portable = []
        for kind, key, value, changed in rows:
            data = json.loads(value)
            if kind == "secret":
                try:
                    data = self.cipher.decrypt(data.encode()).decode()
                except InvalidToken as exc:
                    raise RadarError("کلیدهای ذخیره‌شده قابل خواندن نیستند.", 409) from exc
            portable.append([kind, key, data, changed])
        cipher = self.backup_cipher(passphrase, salt)
        return {
            "format": "agant-x-radar-v1",
            "salt": base64.urlsafe_b64encode(salt).decode(),
            "encrypted": cipher.encrypt(gzip.compress(pack(portable).encode(), compresslevel=4)).decode(),
        }

    def restore(self, passphrase, envelope):
        try:
            if envelope["format"] != "agant-x-radar-v1":
                raise ValueError
            salt = base64.urlsafe_b64decode(envelope["salt"])
            if len(salt) != 16 or len(envelope["encrypted"]) > 32*1024**2:
                raise ValueError
            decrypted = self.backup_cipher(passphrase, salt).decrypt(envelope["encrypted"].encode())
            with gzip.GzipFile(fileobj=io.BytesIO(decrypted)) as payload:
                plain = payload.read(192*1024**2 + 1)
            if len(plain) > 192*1024**2:
                raise ValueError
            rows = json.loads(plain)
            if not isinstance(rows, list):
                raise ValueError
            restored = []
            for kind, key, data, changed in rows:
                if not isinstance(kind, str) or not isinstance(key, str) or not isinstance(changed, int):
                    raise ValueError
                if kind == "secret":
                    data = self.cipher.encrypt(data.encode()).decode()
                if kind == "setting" and key == "options":
                    data["scheduled"] = False
                    data["telegram_enabled"] = False
                if kind == "delivery" and data.get("status") in ("pending", "sending"):
                    data["status"] = "paused"
                restored.append((kind, key, pack(data), changed))
        except (KeyError, AttributeError, TypeError, ValueError, OSError, EOFError, InvalidToken) as exc:
            raise RadarError("فایل پشتیبان یا عبارت عبور آن معتبر نیست.") from exc

        try:
            with self.connect(write=True) as db:
                db.execute("DELETE FROM records")
                db.executemany("INSERT INTO records VALUES(?,?,?,?)", restored)
        except sqlite3.IntegrityError as exc:
            raise RadarError("رکوردهای فایل پشتیبان معتبر نیستند.") from exc
