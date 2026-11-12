"""Collect, compare, and prepare replies from read-only provider data."""
import asyncio
import datetime as dt
import json
import logging
import os
import re
import secrets
import time
from urllib.parse import urlsplit

from .providers import Providers, handle, intent, number, post_id, profile, tweet
from .store import RadarError, Store


logger = logging.getLogger(__name__)
KINDS = {"follow", "unfollow", "reply"}
GRAPH = {"followers": "/followers", "following": "/follows"}
DEFAULTS = {
    "owner": "", "public_url": "", "model": "openrouter/free",
    "scheduled": False, "interval": 30, "scan_limit": 10, "daily_limit": 80,
    "telegram_enabled": False, "browser_sound": False,
}


def now():
    return dt.datetime.now(dt.timezone.utc)


def timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo else None
    except (AttributeError, ValueError):
        return None


def rows(body, field):
    result = body.get(field)
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise RadarError("دادهٔ دریافتی ناقص یا نامعتبر است.", 502)
    return result


def cursor(body):
    value = body.get("next_cursor")
    if value in (None, "", 0, "0"):
        return ""
    if isinstance(value, (str, int)) and not isinstance(value, bool) and len(str(value)) <= 4096:
        return str(value)
    raise RadarError("نشانگر صفحهٔ بعد معتبر نیست.", 502)


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


class Budget:
    def __init__(self, store, limit, daily_limit):
        self.store = store
        self.limit = limit
        self.daily_limit = daily_limit
        self.used = 0

    def spend(self):
        if self.used >= self.limit:
            raise RadarError("سقف درخواست این بررسی پر شده است.", 429)
        self.store.reserve(now().date().isoformat(), self.daily_limit)
        self.used += 1


class Radar:
    def __init__(self, directory, key, transport=None):
        self.store = Store(directory, key)
        self.api = Providers(self.store, transport)
        self.lock = asyncio.Lock()
        with self.store.connect(write=True) as db:
            pending = db.execute(
                "SELECT id,value FROM records WHERE kind='delivery' AND json_extract(value,'$.status')='sending'"
            ).fetchall()
            for key, value in pending:
                Store.write(db, "delivery", key, json.loads(value) | {"status": "unknown"})

    def options(self):
        defaults = DEFAULTS.copy()
        configured = os.getenv("LLM_MODEL", "")
        if os.getenv("HERMES_INFERENCE_PROVIDER") == "openrouter" and configured:
            defaults["model"] = configured
        defaults["public_url"] = os.getenv("AGANT_X_RADAR_PUBLIC_URL", "")
        return defaults | self.store.get("setting", "options", {})

    def budget(self):
        opts = self.options()
        return Budget(self.store, opts["scan_limit"], opts["daily_limit"])

    def state(self):
        options = self.options()
        keys = {}
        for name in ("sorsa", "twitter", "openrouter", "telegram"):
            try:
                keys[name] = bool(self.api.key(name))
            except RadarError:
                keys[name] = False
        return {
            "options": options,
            "keys": keys,
            "watch": self.store.list("watch"),
            "targets": self.store.list("target"),
            "rules": self.store.list("rule"),
            "channels": self.store.list("channel"),
            "drafts": self.store.list("draft", 25),
            "last_scan": self.store.get("system", "scan"),
            "busy": self.lock.locked(),
            "used_today": self.store.get("usage", now().date().isoformat(), 0),
        }

    def emit(self, db, event, alert=True):
        if not Store.write(db, "event", event["id"], event, unique=True) or not alert:
            return
        options = Store.read(db, "setting", "options", DEFAULTS)
        rules = [json.loads(row[0]) for row in db.execute("SELECT value FROM records WHERE kind='rule'")]
        for rule in rules:
            if not rule["enabled"] or rule["type"] != event["type"]:
                continue
            if rule["account"] and rule["account"] != event["account"]:
                continue
            if rule["actor"] and rule["actor"] != event["actor"]:
                continue
            if rule["keyword"].casefold() not in event["text"].casefold():
                continue
            alert_id = event["id"] + ":" + rule["id"]
            item = {
                "id": alert_id, "event": event, "rule": rule["name"], "read": False,
                "draft_requested": rule["draft"] and event["type"] == "reply",
            }
            Store.write(db, "alert", alert_id, item, unique=True)
            channel = Store.read(db, "channel", rule["channel"])
            if channel and options["telegram_enabled"]:
                Store.write(db, "delivery", alert_id, {
                    "id": alert_id, "status": "pending", "channel": channel, "rule_id": rule["id"],
                }, unique=True)

    def dashboard(self):
        watches = self.store.list("watch")
        for watch in watches:
            user = watch["handle"]
            watch["profile"] = self.store.get("profile", user, {})
            watch["score_history"] = self.store.get("score_history", user, [])[-60:]
            watch["top"] = self.store.get("top", user, [])
            watch["vc"] = self.store.get("vc", user)
            watch["activity"] = self.store.get("activity", user, [])
            watch["score_change"] = self.store.get("score_change", user)
        return watches

    def update_profile(self, user, data):
        current = self.store.get("profile", user, {})
        self.store.put("profile", user, current | data | {"checked_at": now().isoformat()})

    def record_score(self, user, score):
        self.update_profile(user, {"sorsa": score})
        if score is not None:
            history = self.store.get("score_history", user, [])
            history.append({"at": now().isoformat(), "score": score})
            self.store.put("score_history", user, history[-400:])

    async def profile_info(self, user, budget):
        data = profile(await self.api.sorsa("/info", {"username": user}, budget))
        self.update_profile(user, {k: v for k, v in data.items() if k != "sorsa"})
        return data

    async def profile_preview(self, user, budget):
        await self.profile_info(user, budget)
        errors = []
        for endpoint, name in (
            ("/score", "score"), ("/followers-stats", "vc"), ("/top-followers", "top"),
            ("/score-changes", "score_change"), ("/user-tweets", "activity"),
        ):
            if budget.used == budget.limit:
                errors.append("بقیهٔ داده‌های پروفایل در سقف درخواست این اجرا جا نشدند.")
                break
            try:
                result = await self.api.sorsa(endpoint, {"username": user}, budget)
                if name == "score":
                    self.record_score(user, number(result.get("score")))
                elif name == "vc":
                    self.store.put(name, user, number(result.get("venture_capitals_count")))
                elif name == "top":
                    self.store.put(name, user, [profile(row) for row in rows(result, "users")])
                elif name == "activity":
                    self.store.put(name, user, self.available_tweets(result))
                else:
                    self.store.put(name, user, {
                        "week": result.get("week_delta"), "month": result.get("month_delta"),
                    })
            except RadarError as exc:
                errors.append(f"{name}: {exc}")
                if exc.status == 429:
                    break
        if self.api.key("twitter"):
            try:
                result = await self.api.twitter("get_twitter_score", {"username": user})
                self.update_profile(user, {"twitter": number(result.get("twitter_score"))})
            except RadarError as exc:
                errors.append(f"توییتر‌اسکور: {exc}")
        return {"errors": errors}

    async def graph_page(self, user, kind, budget):
        key = user + ":" + kind
        pending = self.store.get("graph_work", key)
        if not pending:
            info = await self.profile_info(user, budget)
            if info["protected"]:
                raise RadarError("فهرست این حساب خصوصی است.", 422)
            pending = {
                "id": secrets.token_hex(10), "start": now().isoformat(), "cursor": "",
                "cursors": [], "members": {}, "count": info[kind],
            }
            self.store.put("graph_work", key, pending)
            if budget.used == budget.limit:
                return {"complete": False, "count": 0}
        args = {"username": user}
        if pending["cursor"]:
            args["next_cursor"] = pending["cursor"]
        body = await self.api.sorsa(GRAPH[kind], args, budget)
        members = [profile(row) for row in rows(body, "users")]
        next_page = cursor(body)
        if body.get("user_protected") is True or (not members and not pending["members"] and pending["count"]):
            raise RadarError("فهرست کامل از منبع دریافت نشد. تاریخچهٔ قبلی حفظ شد.", 502)
        if next_page and (next_page == pending["cursor"] or next_page in pending["cursors"]):
            raise RadarError("سورسا نشانگر تکراری داد. برداشت ناقص را از نو شروع کن.", 502)
        pending["members"].update({item["id"]: item for item in members})
        if next_page:
            pending["cursors"].append(next_page)
            pending["cursor"] = next_page
            self.store.put("graph_work", key, pending)
            return {"complete": False, "count": len(pending["members"])}

        at = now().isoformat()
        snapshot = {
            "id": pending["id"], "start": pending["start"], "at": at, "members": pending["members"],
        }
        previous = self.store.get("graph", key)
        with self.store.connect(write=True) as db:
            if previous is not None:
                old, new = previous["members"], snapshot["members"]
                for typ, source, delta in (
                    ("follow", new, new.keys() - old.keys()),
                    ("unfollow", old, old.keys() - new.keys()),
                ):
                    for member in sorted(delta):
                        actor = source[member]["handle"]
                        self.emit(db, {
                            "id": f"{snapshot['id']}:{typ}:{member}", "type": typ,
                            "account": user, "actor": actor, "at": at, "observed": at,
                            "since": previous["at"], "direction": kind,
                            "text": "", "url": f"https://x.com/{actor}",
                        })
            Store.write(db, "graph", key, snapshot)
            db.execute("DELETE FROM records WHERE kind='graph_work' AND id=?", (key,))
        return {"complete": True, "count": len(snapshot["members"]), "at": at}

    @staticmethod
    def available_tweets(body):
        result = []
        for item in rows(body, "tweets"):
            try:
                result.append(tweet(item))
            except RadarError:
                continue
        return result

    async def reply_page(self, mode, subject, budget):
        key = mode + ":" + subject
        last = self.store.get("reply_done", key)
        state = self.store.get("reply_work", key)
        if not state:
            start = now()
            cutoff = timestamp(last) - dt.timedelta(minutes=5) if last else start - dt.timedelta(days=1)
            state = {
                "start": start.isoformat(), "cutoff": cutoff.isoformat(), "cursors": [], "cursor": "",
            }
        cutoff = timestamp(state["cutoff"])
        if mode == "comments":
            args = {"tweet_link": subject, "order_by": "Recency"}
            endpoint = "/comments"
        else:
            operator = "to" if mode == "incoming" else "from"
            args = {
                "query": f"{operator}:{subject} filter:replies since:{cutoff.date().isoformat()}",
                "order": "latest",
            }
            endpoint = "/search-tweets"
        if state["cursor"]:
            args["next_cursor"] = state["cursor"]
        body = await self.api.sorsa(endpoint, args, budget)
        items = self.available_tweets(body)
        next_page = cursor(body)
        if next_page and next_page in state["cursors"]:
            raise RadarError("صفحه‌بندی ریپلای‌ها تکراری شد. برداشت را از نو شروع کن.", 502)
        if mode == "comments" and items and all(timestamp(p["date"]) and timestamp(p["date"]) < cutoff for p in items):
            next_page = ""
        count = 0
        with self.store.connect(write=True) as db:
            for post in items:
                published = timestamp(post["date"])
                if not post["reply"] or (published and published < cutoff):
                    continue
                if mode == "incoming" and post["parent"] != subject:
                    continue
                if mode == "replies" and post["author"] != subject:
                    continue
                if mode == "comments" and post["parent_id"] != subject:
                    continue
                event_id = "reply:" + post["id"]
                if Store.read(db, "event", event_id):
                    continue
                observed = now().isoformat()
                self.emit(db, {
                    "id": event_id, "type": "reply", "account": post["parent"] or subject,
                    "actor": post["author"], "at": published.isoformat() if published else observed,
                    "observed": observed, "text": post["text"], "url": post["url"], "post": post,
                }, alert=bool(last))
                count += 1
            if next_page:
                state["cursors"].append(next_page)
                state["cursor"] = next_page
                Store.write(db, "reply_work", key, state)
            else:
                Store.write(db, "reply_done", key, state["start"])
                db.execute("DELETE FROM records WHERE kind='reply_work' AND id=?", (key,))
        return {"complete": not next_page, "count": count}

    async def smart_page(self, user):
        page = self.store.get("smart_next", user, 1)
        response = await self.api.twitter("get_followers", {"username": user, "page": page, "size": 25})
        accounts = self.store.get("smart_work", user, {}) if page > 1 else {}
        items = rows(response, "top_followers")
        if items and all(handle(row.get("username")) in accounts for row in items):
            raise RadarError("توییتر‌اسکور یک صفحه را تکرار کرد. برداشت را از نو شروع کن.", 502)
        with self.store.connect(write=True) as db:
            for raw in items:
                username = handle(raw.get("username"))
                accounts[username] = {
                    "handle": username, "name": str(raw.get("name") or username)[:160],
                    "twitter": number(raw.get("twitter_score")),
                    "followers": number(raw.get("followers_count")),
                    "tags": [str(t.get("name"))[:100] for t in raw.get("tags", []) if isinstance(t, dict)],
                }
                current = Store.read(db, "profile", username, {})
                Store.write(db, "profile", username, current | {"twitter": accounts[username]["twitter"]})
            complete = len(items) < 25
            if complete:
                Store.write(db, "smart", user, accounts)
                db.execute("DELETE FROM records WHERE id=? AND kind IN ('smart_work','smart_next')", (user,))
            else:
                Store.write(db, "smart_work", user, accounts)
                Store.write(db, "smart_next", user, page + 1)
        return {"complete": complete, "count": len(accounts)}

    async def enrich(self, users):
        result = {"errors": []}
        budget = self.budget()
        if self.api.key("twitter"):
            data = await self.api.twitter("bulk_scores_check", {"usernames": ",".join(users), "size": len(users)})
            for row in rows(data, "data"):
                self.update_profile(handle(row.get("username")), {"twitter": number(row.get("twitter_score"))})
        for user in users[:budget.limit]:
            try:
                body = await self.api.sorsa("/score", {"username": user}, budget)
                self.record_score(user, number(body.get("score")))
            except RadarError as exc:
                result["errors"].append(f"@{user}: {exc}")
                if exc.status in (409, 429):
                    break
        return result | {"requests": budget.used}

    def members(self, user, kind, filters=None):
        followers = self.store.get("graph", user + ":followers")
        following = self.store.get("graph", user + ":following")
        smart = self.store.get("smart", user, {})
        pending = self.store.get("graph_work", user + ":" + kind)
        graph = followers if kind == "followers" else following
        complete = graph is not None
        data = (graph or pending or {}).get("members", {})
        if kind == "nonfollowers":
            complete = followers is not None and following is not None
            data = {k: v for k, v in following["members"].items() if k not in followers["members"]} if complete else {}
        if kind == "smart":
            complete = self.store.get("smart", user) is not None
            data = smart if complete else self.store.get("smart_work", user, {})
        with self.store.connect() as db:
            result = []
            for item in data.values():
                updated = Store.read(db, "profile", item["handle"], {})
                item = item | {k: updated[k] for k in ("sorsa", "twitter") if updated.get(k) is not None}
                item["smart"] = item["handle"] in smart or kind == "smart"
                if self.matches(item, filters or {}):
                    result.append(item)
        if kind == "nonfollowers":
            result.sort(key=lambda p: (
                p.get("sorsa") is None, p.get("sorsa") or 0, -(p.get("ff") or 0), p["handle"],
            ))
        else:
            result.sort(key=lambda p: p["handle"])
        return {
            "complete": complete, "rows": result, "at": graph["at"] if graph and kind in GRAPH else None,
            "pending": len(pending["members"]) if pending else None,
        }

    @staticmethod
    def matches(item, filters):
        search = filters.get("q", "").casefold()
        if search and search not in (item["handle"] + " " + item["name"]).casefold():
            return False
        if filters.get("smart") and not item["smart"]:
            return False
        for field in ("ff", "sorsa", "twitter"):
            value = item.get(field)
            low, high = filters.get(field + "_min"), filters.get(field + "_max")
            if low is not None and (value is None or value < low):
                return False
            if high is not None and (value is None or value > high):
                return False
        return True

    def compare(self, left, right, kind, page=1):
        a = self.store.get("graph", left + ":" + kind)
        b = self.store.get("graph", right + ":" + kind)
        if a is None or b is None:
            raise RadarError("اول فهرست کامل هر دو حساب را دریافت کن.", 409)
        left_ids, right_ids = a["members"].keys(), b["members"].keys()
        shared = sorted((a["members"][k] for k in left_ids & right_ids), key=lambda row: row["handle"])
        return {
            "left": len(left_ids), "right": len(right_ids), "total": len(shared),
            "rows": shared[(page-1)*100:page*100], "left_at": a["at"], "right_at": b["at"],
        }

    def snapshot(self, name, handles):
        previous = self.store.get("manual", name)
        before, after = set((previous or {}).get("handles", [])), set(handles)
        result = {
            "name": name, "at": now().isoformat(), "handles": sorted(after),
            "before": (previous or {}).get("at"),
            "added": sorted(after - before) if previous else [], "removed": sorted(before - after),
            "shared": len(after & before),
        }
        self.store.put("manual", name, result)
        return result

    def tasks(self):
        jobs = []
        for watch in sorted(self.store.list("watch"), key=lambda w: w["handle"]):
            user = watch["handle"]
            jobs.extend([("info", user), ("score", user)])
            for kind in ("followers", "following", "replies"):
                if watch[kind]:
                    jobs.append((kind, user))
        owner = self.options()["owner"]
        if owner:
            jobs.append(("incoming", owner))
        jobs.extend(("comments", item["id"]) for item in self.store.list("target"))
        return jobs

    async def step(self, mode, subject, budget):
        if mode in GRAPH:
            return await self.graph_page(subject, mode, budget)
        if mode in ("replies", "comments", "incoming"):
            return await self.reply_page(mode, subject, budget)
        if mode == "profile":
            return await self.profile_preview(subject, budget)
        if mode == "info":
            await self.profile_info(subject, budget)
            return {"complete": True}
        if mode == "score":
            result = await self.api.sorsa("/score", {"username": subject}, budget)
            self.record_score(subject, number(result.get("score")))
            return {"complete": True}
        if mode == "smart":
            return await self.smart_page(subject)
        if mode == "usage":
            return await self.api.sorsa("/key-usage-info", {}, budget)
        raise RadarError("نوع بررسی معتبر نیست.")

    async def scan(self, mode="all", subject=""):
        if self.lock.locked():
            raise RadarError("یک بررسی در حال اجراست.", 409)
        async with self.lock:
            budget = self.budget()
            results, errors = [], []
            if mode == "all":
                jobs = self.tasks()
                if not jobs:
                    raise RadarError("حساب یا پستی برای رصد اضافه کن.", 409)
                start = self.store.get("system", "rotation", 0) % len(jobs)
                for offset in range(len(jobs)):
                    if budget.used >= budget.limit:
                        break
                    index = (start + offset) % len(jobs)
                    kind, target = jobs[index]
                    try:
                        data = await self.step(kind, target, budget)
                        results.append({"mode": kind, "subject": target} | data)
                    except RadarError as exc:
                        errors.append(f"{kind} {target}: {exc}")
                        if exc.status in (409, 429):
                            break
                    self.store.put("system", "rotation", index + 1)
            else:
                if mode == "comments":
                    subject = post_id(subject)
                elif mode != "usage":
                    subject = handle(subject)
                count = 0
                while True:
                    data = await self.step(mode, subject, budget)
                    count += data.get("count", 0)
                    if (data.get("complete", True) or budget.used >= budget.limit or mode == "smart"):
                        break
                if mode in {"incoming", "replies", "comments"}:
                    data["count"] = count
                errors.extend(data.pop("errors", []))
                results.append({"mode": mode, "subject": subject} | data)
            status = {"at": now().isoformat(), "requests": budget.used, "results": results, "errors": errors}
            self.store.put("system", "scan", status)
        return status

    async def draft_post(self, post):
        if post["media"] and len(post["text"].split()) < 5:
            raise RadarError("محتوای تصویر یا ویدئو برای فهم این پست لازم است.", 422)
        if re.match(r"^\s*(?:gm|good morning)\b", post["text"], re.IGNORECASE):
            text = "Good morning hope your day goes well"
        elif re.match(r"^\s*(?:gn|good night)\b", post["text"], re.IGNORECASE):
            text = "Good night sleep well"
        else:
            text = await self.api.generate(post, self.options()["model"])
        result = {"post": post, "text": text, "intent": intent(post["id"], text), "at": now().isoformat()}
        self.store.put("draft", post["id"], result)
        return result

    async def draft(self, url, regenerate=False):
        if self.lock.locked():
            raise RadarError("صبر کن بررسی جاری تمام شود.", 409)
        async with self.lock:
            item_id = post_id(url)
            existing = self.store.get("draft", item_id)
            if existing and not regenerate:
                return existing
            post = tweet(await self.api.sorsa("/tweet-info", {"tweet_link": item_id}, self.budget()))
            if post["id"] != item_id:
                raise RadarError("شناسهٔ پست دریافتی با لینک یکی نیست.", 502)
            return await self.draft_post(post)

    async def flush(self):
        with self.store.connect() as db:
            work = [json.loads(row[0]) for row in db.execute(
                "SELECT value FROM records WHERE kind='delivery' "
                "AND json_extract(value,'$.status')='pending' ORDER BY changed LIMIT 15"
            )]
        for item in work:
            if not self.options()["telegram_enabled"]:
                break
            alert = self.store.get("alert", item["id"])
            rule = self.store.get("rule", item.get("rule_id", ""))
            current_channel = self.store.get("channel", rule["channel"]) if rule else None
            if not alert or not rule or not rule["enabled"] or current_channel != item["channel"]:
                self.store.put("delivery", item["id"], item | {"status": "canceled"})
                continue
            item["status"] = "sending"
            self.store.put("delivery", item["id"], item)
            event, destination = alert["event"], item["channel"]
            title = {"follow": "فالو", "unfollow": "آنفالو", "reply": "ریپلای"}[event["type"]]
            message = f"رادار ایکس | {title}\n@{event['actor']} · @{event['account']}\n{event['text'][:650]}\n{event['url']}"
            url = event["url"]
            if alert["draft_requested"]:
                try:
                    post = event["post"]
                    draft = self.store.get("draft", post["id"]) or await self.draft_post(post)
                    message += "\n\nپیش‌نویس پاسخ\n" + draft["text"]
                    url = draft["intent"]
                except RadarError as exc:
                    alert["draft_error"] = str(exc)
                    self.store.put("alert", alert["id"], alert)
            try:
                await self.api.notify(destination["chat"], message, destination["silent"], url)
                item["status"] = "sent"
            except RadarError as exc:
                item["status"], item["error"] = "error", str(exc)
            self.store.put("delivery", item["id"], item)

    async def loop(self):
        while True:
            try:
                options = self.options()
                last = self.store.get("system", "scheduled_at", 0)
                if options["scheduled"] and time.time() - last >= options["interval"] * 60:
                    if self.tasks() and self.api.key("sorsa") and not self.lock.locked():
                        self.store.put("system", "scheduled_at", time.time())
                        await self.scan()
                if options["telegram_enabled"] and not self.lock.locked():
                    async with self.lock:
                        await self.flush()
            except RadarError as exc:
                logger.warning("X radar: %s", exc)
            except Exception as exc:
                logger.error("X radar cycle failed: %s", type(exc).__name__)
            await asyncio.sleep(15)
