"""Watch the owner's incoming replies and new posts from a supplied SMART list."""
import asyncio
import datetime as dt
import hashlib
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
    "scheduled": False, "interval": 180, "daily_limit": 100, "telegram_enabled": False,
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
    def __init__(self, directory, key, transport=None, *, remote=None):
        self.store = Store(directory, key, remote=remote)
        self.api = Providers(self.store, transport)
        self.lock = asyncio.Lock()

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
        saved = await asyncio.to_thread(self.store.get, "feed", feed.key, {})
        work = saved.get("work")
        if work is None:
            until = int(now().timestamp())
            since = saved["done"] - INDEX_OVERLAP if "done" in saved else until - FIRST_LOOKBACK
            work = {"since": since, "until": until, "cursor": "", "previous": []}

        try:
            body = await feed.read(self.api, budget, work)
        except RadarError as exc:
            if work["cursor"] and exc.status in (400, 404):
                reset = saved | {"work": work | {"cursor": "", "previous": []}}
                await asyncio.to_thread(self.store.put, "feed", feed.key, reset)
            raise
        if not isinstance(body.get("tweets"), list):
            raise RadarError("فهرست پست‌ها از سورسا دریافت نشد.", 502)
        cursor = next_cursor(body)
        if cursor and (cursor == work["cursor"] or cursor in work["previous"]):
            reset = saved | {"work": work | {"cursor": "", "previous": []}}
            await asyncio.to_thread(self.store.put, "feed", feed.key, reset)
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

        added = await asyncio.to_thread(self.store.record_page, feed.key, items, updated)
        return added, not complete, skipped

    async def scan(self):
        if self.lock.locked():
            raise RadarError("یک بررسی در حال اجراست.", 409)
        async with self.lock:
            options = await asyncio.to_thread(self.options)
            jobs = feeds(options)
            if not jobs:
                raise RadarError("نام حساب خودت یا لیست SMART را در تنظیمات وارد کن.", 409)
            if not await asyncio.to_thread(self.api.key, "sorsa"):
                raise RadarError("کلید سورسا را در تنظیمات وارد کن.", 409)
            budget = Budget(self.store, options["daily_limit"])
            notify_to = telegram_owner() if options["telegram_enabled"] else ""
            rotation = await asyncio.to_thread(self.store.get, "system", "feed_rotation", 0) % len(jobs)
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
                await asyncio.to_thread(self.store.put, "system", "feed_rotation", rotation)
            status["requests"] = budget.used
            await asyncio.to_thread(self.store.put, "system", "scan", status)
            return status

    async def make_draft(self, post):
        if re.match(r"^\s*(?:gm|good morning)\b", post["text"], re.IGNORECASE):
            text = "Good morning hope your day goes well"
        elif re.match(r"^\s*(?:gn|good night)\b", post["text"], re.IGNORECASE):
            text = "Good night sleep well"
        elif post["media"] and len(post["text"].split()) < 5:
            raise RadarError("برای فهم این پست باید تصویر یا ویدئو را دید.", 422)
        else:
            retry_at = await asyncio.to_thread(self.store.get, "system", "model_resume", 0)
            if time.time() < retry_at:
                raise RadarError("مدل رایگان موقتاً محدود شده است. بعداً دوباره تلاش کن.", 429)
            try:
                text = await self.api.generate(post)
            except RadarError as exc:
                if exc.status == 429:
                    await asyncio.to_thread(self.store.put, "system", "model_resume", time.time() + MODEL_PAUSE)
                    raise RadarError("مدل رایگان موقتاً محدود شده است. بعداً دوباره تلاش کن.", 429) from exc
                raise
        return {"post": post, "text": text, "intent": intent(post["id"], text), "at": now().isoformat()}

    async def draft(self, url, regenerate=False):
        if self.lock.locked():
            raise RadarError("صبر کن بررسی جاری تمام شود.", 409)
        async with self.lock:
            item_id = post_id(url)
            existing = await asyncio.to_thread(self.store.get, "draft", item_id)
            if existing and not regenerate:
                return existing
            item = await asyncio.to_thread(self.store.get, "inbox", item_id)
            if item or existing:
                post = (item or existing)["post"]
            else:
                options = await asyncio.to_thread(self.options)
                budget = Budget(self.store, options["daily_limit"])
                post = tweet(await self.api.sorsa("/tweet-info", {"tweet_link": item_id}, budget))
                if post["id"] != item_id:
                    raise RadarError("شناسهٔ پست دریافتی با لینک یکی نیست.", 502)
            draft = await self.make_draft(post)
            await asyncio.to_thread(self.store.record_draft, item, draft)
            return draft

    async def deliver(self, item, draft):
        post = item["post"]
        owner = telegram_owner()
        options = await asyncio.to_thread(self.options)
        if not options["telegram_enabled"] or not owner or owner != item["telegram_owner"]:
            await asyncio.to_thread(self.store.put, "inbox", post["id"], item | {"telegram": "canceled"})
            return
        message = f"{INBOX_TITLES[item['kind']]}\n@{post['author']}\n{post['text'][:750]}\n{post['url']}"
        url, label = post["url"], "مشاهدهٔ پست"
        if draft:
            message += "\n\nپیش‌نویس پاسخ\n" + draft["text"]
            url, label = draft["intent"], "باز کردن فرم پاسخ در X"
        elif item.get("draft_error"):
            message += "\n\n" + item["draft_error"]
        await asyncio.to_thread(self.store.put, "inbox", post["id"], item | {"telegram": "sending"})
        try:
            await self.api.notify(owner, message, url, label)
        except RadarError as exc:
            result = {"telegram": "error", "telegram_error": str(exc)}
        else:
            result = {"telegram": "sent"}
        await asyncio.to_thread(self.store.put, "inbox", post["id"], item | result)

    async def process_pending(self):
        if self.lock.locked():
            return
        async with self.lock:
            for item in await asyncio.to_thread(self.store.pending, 3):
                post = item["post"]
                draft = await asyncio.to_thread(self.store.get, "draft", post["id"])
                if item["draft_state"] == "pending":
                    try:
                        draft = draft or await self.make_draft(post)
                    except RadarError as exc:
                        await asyncio.to_thread(self.store.record_draft, item, error=str(exc))
                    else:
                        await asyncio.to_thread(self.store.record_draft, item, draft)
                if item["telegram"] == "pending":
                    await self.deliver(item, draft)

    def scheduled_scan_due(self):
        options = self.options()
        if not options["scheduled"] or not feeds(options):
            return False
        last = self.store.get("system", "scheduled_at", 0)
        return (time.time() - last >= options["interval"] * 60
                and self.used_today() < options["daily_limit"] and bool(self.api.key("sorsa")))

    async def loop(self):
        while True:
            try:
                if not self.lock.locked() and await asyncio.to_thread(self.scheduled_scan_due):
                    await asyncio.to_thread(self.store.put, "system", "scheduled_at", time.time())
                    await self.scan()
                await self.process_pending()
            except RadarError as exc:
                logger.warning("X radar: %s", exc)
            except Exception as exc:
                logger.error("X radar cycle failed: %s", type(exc).__name__)
            await asyncio.sleep(15)
