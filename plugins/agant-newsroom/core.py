"""Durable editorial decisions; no model or Telegram SDK dependencies."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import html
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import urlsplit


class NewsroomError(ValueError):
    """Safe, user-visible workflow error."""


class TelegramRejected(NewsroomError):
    """Telegram explicitly rejected a request, so it was not sent."""


@dataclass(frozen=True)
class Settings:
    owner_id: int
    editor_ids: frozenset[int]
    editor_chat_id: int
    channel_id: int
    db_path: Path

    @classmethod
    def from_env(cls):
        if os.environ.get("AGANT_NEWSROOM_ENABLED", "").lower().strip() != "true":
            return None
        try:
            owner = int(os.environ["AGANT_NEWSROOM_OWNER_ID"])
            group = int(os.environ["AGANT_NEWSROOM_EDITOR_CHAT_ID"])
            channel = int(os.environ["AGANT_NEWSROOM_CHANNEL_ID"])
            editors = {owner} | {int(v.strip()) for v in os.environ.get("AGANT_NEWSROOM_EDITOR_IDS", "").split(",") if v.strip()}
            path = Path(os.environ["AGANT_NEWSROOM_DB"])
            if owner <= 0 or any(v <= 0 for v in editors) or group >= 0 or not str(channel).startswith("-100"):
                raise ValueError()
            if group == channel or not path.is_absolute():
                raise ValueError()
        except (KeyError, ValueError):
            raise NewsroomError("تنظیمات تحریریه کامل نیست؛ شناسه‌ها و مسیر مطلق پایگاه داده را بررسی کن.") from None
        return cls(owner, frozenset(editors), group, channel, path)


def _length(text):
    return len(text.encode("utf-16-le")) // 2


def validate_payload(data):
    def text(name, limit, required=False):
        value = data.get(name, "")
        if not isinstance(value, str) or _length(value.strip()) > limit or (required and not value.strip()):
            raise NewsroomError(f"فیلد {name} خالی یا بیش از حد طولانی است.")
        return value.strip()

    title, body, humor = text("title", 120, True), text("text", 600, True), text("humor_text", 600)
    raw = text("source_url", 250, True)
    url = urlsplit(raw)
    if (url.scheme != "https" or url.netloc.lower() not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
            or not re.fullmatch(r"/[A-Za-z0-9_]{1,30}/status/[0-9]{1,24}/?", url.path)):
        raise NewsroomError("لینک مستقیم پست اصلی X لازم است.")
    source = "https://x.com" + url.path.rstrip("/")
    image = text("image_url", 600)
    if image:
        photo = urlsplit(image)
        if (photo.scheme != "https" or photo.netloc.lower() != "pbs.twimg.com" or not photo.path.startswith("/media/")
                or photo.fragment):
            raise NewsroomError("عکس باید لینک HTTPS عکس اصلی از pbs.twimg.com باشد.")
    return {"title": title, "text": body, "humor_text": humor, "source_url": source,
            "image_url": image, "variant": "plain"}


def caption(draft, preview=True):
    data = draft["payload"]
    body = data["humor_text"] if data["variant"] == "humor" else data["text"]
    content = f'<b>{html.escape(data["title"])}</b>\n\n{html.escape(body)}\n\n<a href="{html.escape(data["source_url"], quote=True)}">منبع اصلی</a>'
    if preview:
        labels = {"published": "منتشر شد", "rejected": "رد شد", "uncertain": "نتیجه ارسال نامشخص", "publishing": "در حال ارسال"}
        label = labels.get(draft["state"], "پیش‌نویس")
        variant = "طنز" if data["variant"] == "humor" else "جدی"
        content = f'<b>{label}</b> · {draft["id"]} · v{draft["revision"]} · {variant}\n\n' + content
    # Bounds are on displayed UTF-16, not HTML entity byte length.
    visible = html.unescape(re.sub(r"<[^>]*>", "", content))
    if _length(visible) > 1024:
        raise NewsroomError("متن پیش‌نمایش باید کوتاه‌تر شود.")
    return content


def buttons(draft):
    if draft["state"] != "draft":
        return {"inline_keyboard": []}
    prefix = f'nr:{draft["id"]}:{draft["revision"]}'
    variants = [{"text": "متن جدی", "callback_data": prefix + ":plain"}]
    if draft["payload"]["humor_text"]:
        variants.append({"text": "متن طنز", "callback_data": prefix + ":humor"})
    return {"inline_keyboard": [variants, [
        {"text": "✅ انتشار همین نسخه", "callback_data": prefix + ":publish"},
        {"text": "❌ رد", "callback_data": prefix + ":reject"}]]}


def post_link(draft):
    return f'https://t.me/c/{str(draft["channel_id"])[4:]}/{draft["published_id"]}'


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY, source_url TEXT NOT NULL UNIQUE,
                    editor_chat_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
                    state TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
                    preview_id INTEGER, published_id INTEGER, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY, draft_id TEXT NOT NULL,
                    action TEXT NOT NULL, actor INTEGER NOT NULL, revision INTEGER NOT NULL, at TEXT NOT NULL
                );
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def operation(self, draft_id):
        if not re.fullmatch(r"[a-f0-9]{12}", str(draft_id)):
            raise NewsroomError("شناسه پیش‌نویس نامعتبر است.")
        root = self.path.with_name(self.path.name + ".locks")
        root.mkdir(mode=0o700, exist_ok=True)
        with open(root / draft_id, "a") as handle:
            os.chmod(handle.name, 0o600)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise NewsroomError("این پیش‌نویس در حال پردازش است؛ کمی بعد دوباره امتحان کن.") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @staticmethod
    def decode(row):
        if row is None:
            raise NewsroomError("پیش‌نویس پیدا نشد.")
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        return data

    def get(self, draft_id):
        with self.connection() as db:
            return self.decode(db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())

    def by_message(self, chat, message_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM drafts WHERE editor_chat_id=? AND preview_id=?", (chat, message_id)).fetchone()
            return self.decode(row) if row else None

    def recent(self, settings):
        with self.connection() as db:
            return [self.decode(r) for r in db.execute(
                "SELECT * FROM drafts WHERE editor_chat_id=? AND channel_id=? ORDER BY updated_at DESC LIMIT 10",
                (settings.editor_chat_id, settings.channel_id))]

    def events(self, draft_id):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM decisions WHERE draft_id=? ORDER BY id", (draft_id,))]

    def create(self, payload, settings, actor):
        draft_id = secrets.token_hex(6)
        # X identifies a post by status ID; author names and tracking can change.
        source_key = "https://x.com/i/status/" + payload["source_url"].rsplit("/", 1)[-1]
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT id FROM drafts WHERE source_url=?", (source_key,)).fetchone()
            if old:
                raise NewsroomError(f'این منبع قبلاً ثبت شده: {old["id"]}')
            now = datetime.now(timezone.utc).isoformat()
            db.execute("INSERT INTO drafts VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (draft_id, source_key, settings.editor_chat_id, settings.channel_id,
                        "preparing", 1, json.dumps(payload, ensure_ascii=False), None, None, now))
            self.record(db, draft_id, "created", actor, 1, now)
        return self.get(draft_id)

    @staticmethod
    def record(db, draft_id, action, actor, revision, now):
        db.execute("INSERT INTO decisions (draft_id,action,actor,revision,at) VALUES (?,?,?,?,?)",
                   (draft_id, action, actor, revision, now))

    def transition(self, draft_id, expected_revision, states, actor, action, **updates):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.decode(db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())
            if row["revision"] != expected_revision or row["state"] not in states:
                raise NewsroomError("این دکمه قدیمی است یا پیش‌نویس قابل تغییر نیست؛ نسخه فعلی را بررسی کن.")
            row.update(updates)
            now = datetime.now(timezone.utc).isoformat()
            db.execute("UPDATE drafts SET state=?, revision=?, payload=?, preview_id=?, published_id=?, updated_at=? WHERE id=?",
                       (row["state"], row["revision"], json.dumps(row["payload"], ensure_ascii=False),
                        row["preview_id"], row["published_id"], now, draft_id))
            self.record(db, draft_id, action, actor, row["revision"], now)
        return self.get(draft_id)


class Newsroom:
    def __init__(self, settings, transport):
        self.settings, self.transport = settings, transport
        self.store = Store(settings.db_path)

    def authorize(self, actor, chat, owner=False):
        allowed = {self.settings.owner_id} if owner else self.settings.editor_ids
        if type(actor) is not int or actor not in allowed or chat != self.settings.editor_chat_id:
            raise NewsroomError("اجازه این کار فقط در گروه تحریریه و برای کاربر مجاز وجود دارد.")

    def bound(self, draft_id, message_id=None):
        draft = self.store.get(draft_id)
        if (draft["editor_chat_id"] != self.settings.editor_chat_id or draft["channel_id"] != self.settings.channel_id
                or (message_id is not None and draft["preview_id"] != message_id)):
            raise NewsroomError("این پیش‌نویس به این گروه، پیام یا کانال تعلق ندارد.")
        return draft

    async def create(self, data, *, actor, chat):
        self.authorize(actor, chat)
        draft = self.store.create(validate_payload(data), self.settings, actor)
        with self.store.operation(draft["id"]):
            try:
                # Controls render as draft, but the stored row stays preparing until receipt.
                receipt = await self.transport.preview({**draft, "state": "draft"})
                if type(receipt) is not int or receipt <= 0:
                    raise ValueError()
            except Exception:
                self.store.transition(draft["id"], 1, {"preparing"}, actor, "preview_failed", state="preview_failed")
                raise NewsroomError(f'پیش‌نمایش تأیید نشد؛ شناسه: {draft["id"]}. از refresh استفاده کن.') from None
            return self.store.transition(draft["id"], 1, {"preparing"}, actor, "preview_ready", state="draft", preview_id=receipt)

    async def _edit(self, draft, actor):
        try:
            await self.transport.edit({**draft, "state": "draft"})
        except Exception:
            self.store.transition(draft["id"], draft["revision"], {"updating"}, actor, "preview_failed", state="preview_failed")
            raise NewsroomError("پیش‌نمایش به‌روز نشد؛ انتشار متوقف است. از refresh استفاده کن.") from None
        return self.store.transition(draft["id"], draft["revision"], {"updating"}, actor, "preview_ready", state="draft")

    async def revise(self, draft_id, revision, data, *, actor, chat):
        self.authorize(actor, chat)
        with self.store.operation(draft_id):
            old = self.bound(draft_id)
            payload = validate_payload(data)
            if payload["source_url"] != old["payload"]["source_url"] or payload["image_url"] != old["payload"]["image_url"]:
                raise NewsroomError("برای تغییر منبع یا عکس، پیش‌نویس جدا بساز.")
            payload["variant"] = old["payload"]["variant"] if payload["humor_text"] else "plain"
            draft = self.store.transition(draft_id, revision, {"draft"}, actor, "revised", state="updating",
                                          revision=revision + 1, payload=payload)
            return await self._edit(draft, actor)

    async def select(self, draft_id, revision, variant, *, actor, chat, message_id):
        self.authorize(actor, chat)
        with self.store.operation(draft_id):
            old = self.bound(draft_id, message_id)
            if variant not in {"plain", "humor"} or (variant == "humor" and not old["payload"]["humor_text"]):
                raise NewsroomError("این نسخه موجود نیست.")
            payload = {**old["payload"], "variant": variant}
            draft = self.store.transition(draft_id, revision, {"draft"}, actor, "variant_selected", state="updating",
                                          revision=revision + 1, payload=payload)
            return await self._edit(draft, actor)

    async def refresh(self, draft_id, *, actor, chat, replace_preview=False):
        self.authorize(actor, chat)
        with self.store.operation(draft_id):
            old = self.bound(draft_id)
            if old["state"] not in {"draft", "preview_failed", "preparing", "updating"}:
                raise NewsroomError("این پیش‌نویس در وضعیت قابل بازسازی نیست.")
            draft = self.store.transition(draft_id, old["revision"], {old["state"]}, actor, "refresh", state="updating",
                                          revision=old["revision"] + 1)
            if replace_preview or not draft["preview_id"]:
                try:
                    receipt = await self.transport.preview({**draft, "state": "draft"})
                    if type(receipt) is not int or receipt <= 0:
                        raise ValueError()
                except Exception:
                    self.store.transition(draft_id, draft["revision"], {"updating"}, actor, "preview_failed", state="preview_failed")
                    raise NewsroomError("پیش‌نمایش ارسال نشد.") from None
                return self.store.transition(draft_id, draft["revision"], {"updating"}, actor, "preview_ready",
                                             state="draft", preview_id=receipt)
            return await self._edit(draft, actor)

    async def reject(self, draft_id, revision, *, actor, chat, message_id):
        self.authorize(actor, chat)
        with self.store.operation(draft_id):
            self.bound(draft_id, message_id)
            draft = self.store.transition(draft_id, revision, {"draft"}, actor, "rejected", state="rejected", revision=revision + 1)
            await self._best_effort_edit(draft)
            return draft

    async def _best_effort_edit(self, draft):
        try:
            await self.transport.edit(draft)
        except Exception:
            pass  # A failed UI update must not erase a durable decision or retry a send.

    async def publish(self, draft_id, revision, *, actor, chat, message_id):
        self.authorize(actor, chat, owner=True)
        with self.store.operation(draft_id):
            self.bound(draft_id, message_id)
            draft = self.store.transition(draft_id, revision, {"draft"}, actor, "send_started", state="publishing")
            try:
                receipt = await self.transport.publish(draft)
                if type(receipt) is not int or receipt <= 0:
                    raise ValueError()
            except TelegramRejected:
                self.store.transition(draft_id, revision, {"publishing"}, actor, "send_rejected", state="draft")
                raise NewsroomError("تلگرام ارسال را رد کرد؛ مجوز کانال یا عکس را بررسی کن.") from None
            except Exception:
                self.store.transition(draft_id, revision, {"publishing"}, actor, "send_uncertain", state="uncertain")
                raise NewsroomError("نتیجه ارسال نامشخص است؛ کانال را بررسی و سپس با /newsroom resolve تعیین تکلیف کن.") from None
            draft = self.store.transition(draft_id, revision, {"publishing"}, actor, "published", state="published", published_id=receipt)
            await self._best_effort_edit(draft)
            return draft

    async def resolve(self, draft_id, published_id, *, actor, chat):
        self.authorize(actor, chat, owner=True)
        if published_id is not None and (type(published_id) is not int or published_id <= 0):
            raise NewsroomError("شماره پیام کانال باید عدد مثبت باشد.")
        with self.store.operation(draft_id):
            old = self.bound(draft_id)
            # A live sender owns the same OS lock. Only an inactive attempt is recoverable.
            if published_id is None:
                draft = self.store.transition(draft_id, old["revision"], {"uncertain", "publishing"}, actor,
                                              "owner_confirmed_not_sent", state="updating", revision=old["revision"] + 1)
                return await self._edit(draft, actor)
            draft = self.store.transition(draft_id, old["revision"], {"uncertain", "publishing"}, actor,
                                          "owner_confirmed_sent", state="published", published_id=published_id)
            await self._best_effort_edit(draft)
            return draft
