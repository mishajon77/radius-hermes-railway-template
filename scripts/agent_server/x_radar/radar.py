"""Watch the owner's incoming replies and new posts from a supplied SMART list."""
import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from .providers import DRAFT_MODEL, Providers, handle, intent, list_id, post_id, tweet
from .store import RadarError, Store


logger = logging.getLogger(__name__)
DEFAULTS = {
    "owner": "", "public_url": "", "smart_list": "", "smart_handles": [],
    "scheduled": False, "interval": 30, "daily_limit": 100, "telegram_enabled": False,
}
SMART_BATCH_SIZE = 12
SCAN_PAGE_LIMIT = 12
FIRST_LOOKBACK = 30 * 60
INDEX_OVERLAP = 5 * 60
MODEL_PAUSE = 10 * 60
INBOX_TITLES = {"comment": "کامنت روی پست تو", "smart": "پست جدید لیست SMART"}


def now():
    return dt.datetime.now(dt.timezone.utc)


def timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (AttributeError, ValueError, OverflowError):
        return None


def dashboard_url(value):
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == "https" and parsed.hostname and not parsed.username
                and not parsed.password and not parsed.query and not parsed.fragment):
            root = parsed.geturl().rstrip("/")
            return root if root.endswith("/x-radar") else root + "/x-radar"
    except ValueError:
        pass
    raise RadarError("آدرس HTTPS معتبر داشبورد را وارد کن.")


def telegram_owner():
    owner = os.getenv("AGANT_X_RADAR_TELEGRAM_OWNER", "").strip()
    return owner if re.fullmatch(r"[1-9][0-9]{0,19}", owner) else ""


@dataclass(frozen=True)
class Feed:
    kind: str
    query: str = ""
    authors: tuple[str, ...] = ()
    owner: str = ""
    list_id: str = ""

    @property
    def key(self):
        if self.list_id:
            return "list:" + self.list_id
        return self.kind + ":" + hashlib.sha256(self.query.encode()).hexdigest()[:32]

    def accepts(self, post):
        if self.kind == "comment":
            return post["parent"] == self.owner and bool(post["parent_id"]) and post["author"] != self.owner
        return (
            not post["reply"] and not post["repost"]
            and (not self.authors or post["author"] in self.authors)
        )

    async def read(self, api, budget, work):
        if self.list_id:
            path, params = "/list-tweets", {"list_id": self.list_id}
        else:
            window = f"since_time:{work['since'] - 1} until_time:{work['until'] + 1}"
            path, params = "/search-tweets", {"query": f"{self.query} {window}", "order": "latest"}
        if work["cursor"]:
            params["next_cursor"] = work["cursor"]
        return await api.sorsa(path, params, budget)


def feeds(options):
    result = []
    owner = options["owner"]
    if owner:
        result.append(Feed("comment", f"to:{owner} filter:replies -from:{owner}", owner=owner))
    if options["smart_list"]:
        result.append(Feed("smart", list_id=options["smart_list"]))
    users = options["smart_handles"]
    for start in range(0, len(users), SMART_BATCH_SIZE):
        batch = tuple(users[start:start + SMART_BATCH_SIZE])
        query = " OR ".join(f"from:{name}" for name in batch)
        result.append(Feed("smart", f"({query}) -filter:replies -filter:nativeretweets", authors=batch))
    return result


def next_cursor(body):
    value = body.get("next_cursor")
    if value in (None, "", 0, "0"):
        return ""
    if isinstance(value, (str, int)) and not isinstance(value, bool) and len(str(value)) <= 4096:
        return str(value)
    raise RadarError("نشانگر صفحهٔ بعد سورسا معتبر نیست.", 502)


class Budget:
    def __init__(self, store, limit):
        self.store = store
        self.limit = limit
        self.used = 0

    def spend(self):
        self.store.reserve(now().date().isoformat(), self.limit)
        self.used += 1


class Radar:
    def __init__(self, directory, key, transport=None):
        self.store = Store(directory, key)
        self.api = Providers(self.store, transport)
        self.lock = asyncio.Lock()
        with self.store.connect(write=True) as db:
            interrupted = db.execute(
                "SELECT id,value FROM records WHERE kind='inbox' AND json_extract(value,'$.telegram')='sending'"
            ).fetchall()
            for item_id, value in interrupted:
                Store.write(db, "inbox", item_id, json.loads(value) | {"telegram": "unknown"})

    def options(self):
        saved = self.store.get("setting", "monitor")
        if saved is None:
            legacy = self.store.get("setting", "options", {})
            saved = {field: legacy[field] for field in ("owner", "public_url") if field in legacy}
        options = DEFAULTS | {"public_url": os.getenv("AGANT_X_RADAR_PUBLIC_URL", "")} | saved
        try:
            options["public_url"] = dashboard_url(options["public_url"])
        except RadarError:
            options["public_url"] = ""
        return options

    def save_options(self, changes):
        options = self.options() | changes
        options["owner"] = handle(options["owner"]) if options["owner"] else ""
        options["public_url"] = dashboard_url(options["public_url"])
        options["smart_list"] = list_id(options["smart_list"]) if options["smart_list"] else ""
        names = options["smart_handles"]
        if isinstance(names, str):
            names = re.split(r"[\s,،;؛]+", names.strip())
        options["smart_handles"] = sorted({handle(name) for name in names if name})
        self.store.put("setting", "monitor", options)
        return options

    def used_today(self):
        return self.store.get("usage", now().date().isoformat(), 0)

    def state(self):
        options = self.options()
        keys = {}
        for name in ("sorsa", "openrouter", "telegram"):
            try:
                keys[name] = bool(self.api.key(name))
            except RadarError:
                keys[name] = False
        return {
            "options": options,
            "keys": keys,
            "telegram_ready": bool(telegram_owner() and keys["telegram"]),
            "model": DRAFT_MODEL,
            "busy": self.lock.locked(),
            "used_today": self.used_today(),
            "feeds": len(feeds(options)),
            "last_scan": self.store.get("system", "scan"),
        }

    def inbox(self, kind="", page=1):
        return self.store.inbox(kind, page)

    async def collect_page(self, feed, budget, notify_to):
        saved = self.store.get("feed", feed.key, {})
        work = saved.get("work")
        if work is None:
            until = int(now().timestamp())
            since = saved["done"] - INDEX_OVERLAP if "done" in saved else until - FIRST_LOOKBACK
            work = {"since": since, "until": until, "cursor": "", "previous": []}

        try:
            body = await feed.read(self.api, budget, work)
        except RadarError as exc:
            if work["cursor"] and exc.status in (400, 404):
                self.store.put("feed", feed.key, saved | {"work": work | {"cursor": "", "previous": []}})
            raise
        if not isinstance(body.get("tweets"), list):
            raise RadarError("فهرست پست‌ها از سورسا دریافت نشد.", 502)
        cursor = next_cursor(body)
        if cursor and (cursor == work["cursor"] or cursor in work["previous"]):
            self.store.put("feed", feed.key, saved | {"work": work | {"cursor": "", "previous": []}})
            raise RadarError("سورسا نشانگر تکراری برای صفحهٔ بعد برگرداند.", 502)
        items, dates, skipped = [], [], 0
        for raw in body["tweets"]:
            try:
                post = tweet(raw)
                published = timestamp(post["date"])
                if published is None:
                    raise RadarError("زمان پست مشخص نیست.", 422)
            except RadarError:
                skipped += 1
                continue
            dates.append(published)
            if published < work["since"] or not feed.accepts(post):
                continue
            items.append({
                "kind": feed.kind, "post": post, "published_at": published,
                "draft_state": "pending", "telegram": "pending" if notify_to else "off",
                "telegram_owner": notify_to,
            })

        complete = not cursor or max(dates, default=work["since"]) < work["since"]
        if complete:
            updated = {"done": work["until"]}
        else:
            previous = [*work["previous"], work["cursor"]][-16:]
            updated = saved | {"work": work | {"cursor": cursor, "previous": previous}}

        with self.store.connect(write=True) as db:
            added = sum(Store.write(db, "inbox", item["post"]["id"], item, unique=True) for item in items)
            Store.write(db, "feed", feed.key, updated)
        return added, not complete, skipped

    async def scan(self):
        if self.lock.locked():
            raise RadarError("یک بررسی در حال اجراست.", 409)
        async with self.lock:
            options = self.options()
            jobs = feeds(options)
            if not jobs:
                raise RadarError("نام حساب خودت یا لیست SMART را در تنظیمات وارد کن.", 409)
            if not self.api.key("sorsa"):
                raise RadarError("کلید سورسا را در تنظیمات وارد کن.", 409)
            budget = Budget(self.store, options["daily_limit"])
            notify_to = telegram_owner() if options["telegram_enabled"] else ""
            rotation = self.store.get("system", "feed_rotation", 0) % len(jobs)
            jobs = jobs[rotation:] + jobs[:rotation]
            status = {"at": now().isoformat(), "new": 0, "errors": [], "pending": len(jobs), "skipped": 0}
            for job in jobs[:SCAN_PAGE_LIMIT]:
                try:
                    count, pending, skipped = await self.collect_page(job, budget, notify_to)
                    status["new"] += count
                    status["skipped"] += skipped
                    status["pending"] -= int(not pending)
                except RadarError as exc:
                    status["errors"].append(f"{INBOX_TITLES[job.kind]} · {exc}")
                    if exc.status in (401, 402, 403, 409, 429) or exc.status >= 500:
                        break
                rotation = (rotation + 1) % len(jobs)
                self.store.put("system", "feed_rotation", rotation)
            status["requests"] = budget.used
            self.store.put("system", "scan", status)
            return status

    async def make_draft(self, post):
        if re.match(r"^\s*(?:gm|good morning)\b", post["text"], re.IGNORECASE):
            text = "Good morning hope your day goes well"
        elif re.match(r"^\s*(?:gn|good night)\b", post["text"], re.IGNORECASE):
            text = "Good night sleep well"
        elif post["media"] and len(post["text"].split()) < 5:
            raise RadarError("برای فهم این پست باید تصویر یا ویدئو را دید.", 422)
        else:
            if time.time() < self.store.get("system", "model_resume", 0):
                raise RadarError("مدل رایگان موقتاً محدود شده است. بعداً دوباره تلاش کن.", 429)
            try:
                text = await self.api.generate(post)
            except RadarError as exc:
                if exc.status == 429:
                    self.store.put("system", "model_resume", time.time() + MODEL_PAUSE)
                    raise RadarError("مدل رایگان موقتاً محدود شده است. بعداً دوباره تلاش کن.", 429) from exc
                raise
        return {"post": post, "text": text, "intent": intent(post["id"], text), "at": now().isoformat()}

    async def draft(self, url, regenerate=False):
        if self.lock.locked():
            raise RadarError("صبر کن بررسی جاری تمام شود.", 409)
        async with self.lock:
            item_id = post_id(url)
            existing = self.store.get("draft", item_id)
            if existing and not regenerate:
                return existing
            item = self.store.get("inbox", item_id)
            if item or existing:
                post = (item or existing)["post"]
            else:
                budget = Budget(self.store, self.options()["daily_limit"])
                post = tweet(await self.api.sorsa("/tweet-info", {"tweet_link": item_id}, budget))
                if post["id"] != item_id:
                    raise RadarError("شناسهٔ پست دریافتی با لینک یکی نیست.", 502)
            draft = await self.make_draft(post)
            self.store.record_draft(item, draft)
            return draft

    async def deliver(self, item, draft):
        post = item["post"]
        owner = telegram_owner()
        if not self.options()["telegram_enabled"] or not owner or owner != item["telegram_owner"]:
            self.store.put("inbox", post["id"], item | {"telegram": "canceled"})
            return
        message = f"{INBOX_TITLES[item['kind']]}\n@{post['author']}\n{post['text'][:750]}\n{post['url']}"
        url, label = post["url"], "مشاهدهٔ پست"
        if draft:
            message += "\n\nپیش‌نویس پاسخ\n" + draft["text"]
            url, label = draft["intent"], "باز کردن فرم پاسخ در X"
        elif item.get("draft_error"):
            message += "\n\n" + item["draft_error"]
        self.store.put("inbox", post["id"], item | {"telegram": "sending"})
        try:
            await self.api.notify(owner, message, url, label)
            self.store.put("inbox", post["id"], item | {"telegram": "sent"})
        except RadarError as exc:
            self.store.put("inbox", post["id"], item | {"telegram": "error", "telegram_error": str(exc)})

    async def process_pending(self):
        if self.lock.locked():
            return
        async with self.lock:
            for item in self.store.pending(3):
                post = item["post"]
                draft = self.store.get("draft", post["id"])
                if item["draft_state"] == "pending":
                    try:
                        draft = draft or await self.make_draft(post)
                        self.store.record_draft(item, draft)
                    except RadarError as exc:
                        self.store.record_draft(item, error=str(exc))
                if item["telegram"] == "pending":
                    await self.deliver(item, draft)

    async def loop(self):
        while True:
            try:
                options = self.options()
                last = self.store.get("system", "scheduled_at", 0)
                if (options["scheduled"] and not self.lock.locked() and feeds(options)
                        and self.used_today() < options["daily_limit"]
                        and time.time() - last >= options["interval"] * 60 and self.api.key("sorsa")):
                    self.store.put("system", "scheduled_at", time.time())
                    await self.scan()
                await self.process_pending()
            except RadarError as exc:
                logger.warning("X radar: %s", exc)
            except Exception as exc:
                logger.error("X radar cycle failed: %s", type(exc).__name__)
            await asyncio.sleep(15)
