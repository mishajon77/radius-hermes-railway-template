"""Authenticated dashboard API and local gateway bridge."""
import asyncio
import csv
import hashlib
import hmac
import io
import logging
import os
import re
import secrets
import sqlite3
import time
from collections import deque
from contextlib import suppress
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .providers import handle, post_id
from .radar import GRAPH, KINDS, Radar, dashboard_url
from .store import RadarError


COOKIE = "agant_radar_session"
STATIC = Path(__file__).parent / "static"
LOGGER = logging.getLogger(__name__)
SESSION_SECONDS = 12 * 3600


class Login(BaseModel):
    password: str = Field(max_length=256)


class Options(BaseModel):
    owner: str = Field("", max_length=120)
    public_url: str = Field("", max_length=500)
    model: str = Field("openrouter/free", min_length=1, max_length=150)
    scheduled: bool = False
    interval: int = Field(30, ge=3, le=1440)
    scan_limit: int = Field(10, ge=1, le=100)
    daily_limit: int = Field(80, ge=1, le=50000)
    telegram_enabled: bool = False
    browser_sound: bool = False


class Secret(BaseModel):
    service: str = Field(max_length=20)
    key: str = Field(max_length=500)


class Watch(BaseModel):
    username: str = Field(max_length=120)
    group: str = Field("", max_length=60)
    followers: bool = False
    following: bool = False
    replies: bool = True


class Scan(BaseModel):
    mode: str = Field("all", max_length=20)
    subject: str = Field("", max_length=250)
    restart: bool = False


class Target(BaseModel):
    url: str = Field(max_length=300)


class Rule(BaseModel):
    id: str = Field("", max_length=32)
    name: str = Field(min_length=1, max_length=70)
    type: str = Field(max_length=12)
    account: str = Field("", max_length=120)
    actor: str = Field("", max_length=120)
    keyword: str = Field("", max_length=120)
    enabled: bool = True
    channel: str = Field("", max_length=32)
    draft: bool = False


class Channel(BaseModel):
    id: str = Field("", max_length=32)
    name: str = Field(min_length=1, max_length=70)
    chat: str = Field(max_length=32)
    silent: bool = False


class Remove(BaseModel):
    kind: str = Field(max_length=20)
    id: str = Field(max_length=100)


class Snapshot(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    handles: str = Field(max_length=320000)


class Enrich(BaseModel):
    handles: list[str] = Field(min_length=1, max_length=50)


class Draft(Target):
    regenerate: bool = False


class ReadAlert(BaseModel):
    id: str = Field("", max_length=120)


class Backup(BaseModel):
    passphrase: str = Field(min_length=12, max_length=250)
    envelope: dict | None = None


class Bridge(BaseModel):
    command: str = Field("", max_length=500)


def csv_cell(value):
    text = str(value if value is not None else "")
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def create_app(radar, secret, secure=True):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    failures = deque()

    def signature(value):
        return hmac.new(secret.encode(), ("agant-radar-session:" + value).encode(), hashlib.sha256).hexdigest()

    def authorized(request: Request):
        if request.method not in {"GET", "HEAD"} and request.headers.get("X-Radar-Request") != "1":
            raise HTTPException(403, "درخواست باید از پنل ارسال شود.")
        cookie = request.cookies.get(COOKIE, "")
        try:
            payload, proof = cookie.rsplit(".", 1)
            expires = int(payload.split(".")[0])
            if expires < time.time() or not hmac.compare_digest(signature(payload), proof):
                raise ValueError
        except ValueError:
            raise HTTPException(401, "وارد پنل شو.") from None

    access = [Depends(authorized)]

    @app.exception_handler(RequestValidationError)
    async def invalid_data(_request, _exc):
        return JSONResponse({"detail": "ورودی یا اندازهٔ آن معتبر نیست."}, status_code=422)

    @app.exception_handler(RadarError)
    async def radar_error(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.middleware("http")
    async def secure_headers(request, call_next):
        if request.method == "POST":
            limit = 33*1024**2 if request.url.path.endswith("/api/restore") else 1024**2
            length = request.headers.get("content-length", "")
            if not length.isascii() or not length.isdecimal() or len(length) > 10 or int(length) > limit:
                return JSONResponse({"detail": "اندازهٔ درخواست معتبر نیست."}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/app.js")
    def js():
        return FileResponse(STATIC / "app.js", media_type="application/javascript")

    @app.get("/style.css")
    def css():
        return FileResponse(STATIC / "style.css", media_type="text/css")

    @app.post("/api/login")
    def login(data: Login, request: Request, response: Response):
        if request.headers.get("X-Radar-Request") != "1":
            raise HTTPException(403, "درخواست باید از پنل ارسال شود.")
        moment = time.monotonic()
        while failures and moment - failures[0] > 60:
            failures.popleft()
        if len(failures) >= 8:
            raise HTTPException(429, "یک دقیقه صبر کن و دوباره تلاش کن.")
        if not hmac.compare_digest(data.password.encode(), secret.encode()):
            failures.append(moment)
            raise HTTPException(401, "رمز پنل درست نیست.")
        failures.clear()
        payload = f"{int(time.time())+SESSION_SECONDS}.{secrets.token_hex(12)}"
        response.set_cookie(
            COOKIE, f"{payload}.{signature(payload)}", httponly=True, secure=secure,
            samesite="strict", path="/", max_age=SESSION_SECONDS,
        )
        return {"ok": True}

    @app.post("/api/logout", dependencies=access)
    def logout(response: Response):
        response.delete_cookie(COOKIE, path="/", secure=secure, httponly=True, samesite="strict")
        return {"ok": True}

    @app.get("/api/pulse", dependencies=access)
    def pulse():
        return {"unread": radar.store.unread(), "last_scan": radar.store.get("system", "scan")}

    @app.get("/api/state", dependencies=access)
    def state():
        return radar.state() | {
            "dashboard": radar.dashboard(),
            "unread": radar.store.unread(),
        }

    @app.post("/api/options", dependencies=access)
    def options(data: Options):
        options = data.model_dump()
        options["owner"] = handle(data.owner) if data.owner else ""
        options["public_url"] = dashboard_url(data.public_url)
        if not re.fullmatch(r"[A-Za-z0-9_./:@-]+", data.model):
            raise RadarError("نام مدل معتبر نیست.")
        radar.store.put("setting", "options", options)
        return options

    @app.post("/api/secret", dependencies=access)
    def save_secret(data: Secret):
        if data.service not in ("sorsa", "twitter", "openrouter", "telegram"):
            raise RadarError("نام سرویس معتبر نیست.")
        if data.key and (not data.key.isascii() or any(c.isspace() for c in data.key)):
            raise RadarError("کلید سرویس باید بدون فاصله باشد.")
        radar.store.set_secret(data.service, data.key)
        return {"configured": bool(radar.api.key(data.service))}

    @app.post("/api/watch", dependencies=access)
    def watch(data: Watch):
        user = handle(data.username)
        item = data.model_dump(exclude={"username"}) | {"handle": user}
        radar.store.put("watch", user, item)
        return item

    @app.post("/api/target", dependencies=access)
    def target(data: Target):
        item = {"id": post_id(data.url)}
        item["url"] = f"https://x.com/i/web/status/{item['id']}"
        radar.store.put("target", item["id"], item)
        return item

    @app.post("/api/scan", dependencies=access)
    async def scan(data: Scan):
        if data.restart:
            if radar.lock.locked():
                raise RadarError("صبر کن بررسی جاری تمام شود.", 409)
            if data.mode in GRAPH:
                radar.store.delete("graph_work", handle(data.subject) + ":" + data.mode)
            elif data.mode in {"comments", "incoming", "replies"}:
                subject = post_id(data.subject) if data.mode == "comments" else handle(data.subject)
                radar.store.delete("reply_work", data.mode + ":" + subject)
            elif data.mode == "smart":
                radar.store.delete("smart_work", handle(data.subject))
                radar.store.delete("smart_next", handle(data.subject))
        return await radar.scan(data.mode, data.subject)

    @app.get("/api/members", dependencies=access)
    def members(
        account: str, kind: str, q: str = "", smart: bool = False,
        ff_min: float | None = None, ff_max: float | None = None,
        sorsa_min: float | None = None, sorsa_max: float | None = None,
        twitter_min: float | None = None, twitter_max: float | None = None, page: int = 1,
    ):
        if kind not in {*GRAPH, "nonfollowers", "smart"} or not 1 <= page <= 100000:
            raise RadarError("فهرست یا شمارهٔ صفحه معتبر نیست.")
        filters = {
            "q": q, "smart": smart, "ff_min": ff_min, "ff_max": ff_max,
            "sorsa_min": sorsa_min, "sorsa_max": sorsa_max,
            "twitter_min": twitter_min, "twitter_max": twitter_max,
        }
        result = radar.members(handle(account), kind, filters)
        data = result.pop("rows")
        offset = (page - 1) * 100
        return result | {"total": len(data), "rows": data[offset:offset+100], "page": page}

    @app.post("/api/enrich", dependencies=access)
    async def enrich(data: Enrich):
        if radar.lock.locked():
            raise RadarError("صبر کن بررسی جاری تمام شود.", 409)
        async with radar.lock:
            return await radar.enrich(list(dict.fromkeys(handle(v) for v in data.handles)))

    @app.get("/api/compare", dependencies=access)
    def compare(left: str, right: str, kind: str = "followers", page: int = 1):
        if kind not in GRAPH or not 1 <= page <= 100000:
            raise RadarError("نوع فهرست معتبر نیست.")
        return radar.compare(handle(left), handle(right), kind, page)

    @app.post("/api/snapshot", dependencies=access)
    def snapshot(data: Snapshot):
        handles = [handle(name) for name in re.split(r"[\s,،;]+", data.handles.strip()) if name]
        return radar.snapshot(data.name.strip(), handles)

    @app.get("/api/snapshots", dependencies=access)
    def snapshots():
        return radar.store.list("manual")

    @app.get("/api/snapshot", dependencies=access)
    def snapshot_by_name(name: str):
        item = radar.store.get("manual", name)
        if not item:
            raise RadarError("فهرست پیدا نشد.", 404)
        return item

    @app.get("/api/history", dependencies=access)
    def history(
        type: str = "", account: str = "", actor: str = "", q: str = "",
        start: str = "", end: str = "", page: int = 1, export: bool = False,
    ):
        if type and type not in KINDS:
            raise RadarError("نوع رویداد معتبر نیست.")
        if not 1 <= page <= 100000:
            raise RadarError("شمارهٔ صفحه معتبر نیست.")
        filters = {
            "type": type, "account": handle(account) if account else "",
            "actor": handle(actor) if actor else "", "q": q[:150], "start": start, "end": end,
        }
        if not export:
            events = radar.store.history(filters, 101, offset=(page-1)*100)
            return {"rows": events[:100], "more": len(events)>100}
        fields = ("type", "account", "actor", "at", "observed", "since", "direction", "text", "url")

        def csv_rows():
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(fields)
            yield output.getvalue().encode("utf-8-sig")
            for item in radar.store.iter_history(filters):
                output.seek(0)
                output.truncate()
                writer.writerow(csv_cell(item.get(field)) for field in fields)
                yield output.getvalue().encode()

        return StreamingResponse(
            csv_rows(), media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="agant-x-history.csv"'},
        )

    @app.post("/api/rule", dependencies=access)
    def rule(data: Rule):
        if data.type not in KINDS:
            raise RadarError("نوع هشدار معتبر نیست.")
        if data.channel and not radar.store.get("channel", data.channel):
            raise RadarError("مقصد تلگرام را اول ثبت کن.")
        item = data.model_dump()
        item["id"] = data.id or secrets.token_hex(8)
        item["account"] = handle(data.account) if data.account else ""
        item["actor"] = handle(data.actor) if data.actor else ""
        radar.store.put("rule", item["id"], item)
        return item

    @app.post("/api/channel", dependencies=access)
    def channel(data: Channel):
        if not re.fullmatch(r"-?[0-9]{1,20}|@[a-zA-Z][a-zA-Z0-9_]{4,31}", data.chat):
            raise RadarError("شناسهٔ عددی چت یا نام کاربری کانال معتبر نیست.")
        item = data.model_dump() | {"id": data.id or secrets.token_hex(8)}
        radar.store.put("channel", item["id"], item)
        return item

    @app.post("/api/remove", dependencies=access)
    def remove(data: Remove):
        if data.kind not in {"watch", "target", "rule", "channel"}:
            raise RadarError("نوع مورد معتبر نیست.")
        radar.store.delete(data.kind, data.id)
        return {"ok": True}

    @app.get("/api/alerts", dependencies=access)
    def alerts(page: int = 1, unread: bool = False):
        if not 1 <= page <= 100000:
            raise RadarError("شمارهٔ صفحه معتبر نیست.")
        items = [item for item in radar.store.list("alert") if not unread or not item["read"]]
        start = (page-1)*50
        for item in items[start:start+50]:
            delivery = radar.store.get("delivery", item["id"], {})
            item["delivery"] = delivery.get("status")
            item["delivery_error"] = delivery.get("error")
            item["draft"] = radar.store.get("draft", item["event"].get("post", {}).get("id", ""))
        return {"rows": items[start:start+50], "total": len(items)}

    @app.post("/api/alerts/read", dependencies=access)
    def read_alert(data: ReadAlert):
        with radar.store.connect(write=True) as db:
            items = radar.store.list("alert") if not data.id else [radar.store.read(db, "alert", data.id)]
            for item in items:
                if item and not item["read"]:
                    item["read"] = True
                    radar.store.write(db, "alert", item["id"], item)
        return {"ok": True}

    @app.post("/api/draft", dependencies=access)
    async def draft(data: Draft):
        return await radar.draft(data.url, data.regenerate)

    @app.post("/api/backup", dependencies=access)
    def backup(data: Backup):
        return radar.store.backup(data.passphrase, secrets.token_bytes(16))

    @app.post("/api/restore", dependencies=access)
    async def restore(data: Backup):
        if not data.envelope:
            raise RadarError("فایل پشتیبان را انتخاب کن.")
        if radar.lock.locked():
            raise RadarError("صبر کن بررسی جاری تمام شود.", 409)
        async with radar.lock:
            await asyncio.to_thread(radar.store.restore, data.passphrase, data.envelope)
        return {"ok": True}

    @app.post("/api/bridge")
    async def bridge(data: Bridge, request: Request):
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth.encode(), ("Bearer " + secret).encode()):
            raise HTTPException(401, "دسترسی به رادار مجاز نیست.")
        command, _, rest = data.command.strip().partition(" ")
        if command == "scan":
            result = await radar.scan()
            summary = f"رادار ایکس\nدرخواست‌های سورسا: {result['requests']}"
            if result["errors"]:
                summary += "\n" + "\n".join(result["errors"][:5])
            return {"text": summary}
        if command == "reply":
            result = await radar.draft(rest)
            return {"text": f"پیش‌نویس پاسخ\n{result['text']}\n\n{result['post']['url']}\n{result['intent']}"}
        if command == "alerts":
            items = [alert for alert in radar.store.list("alert", 25) if not alert["read"]][:7]
            lines = [f"{item['rule']} · @{item['event']['actor']}\n{item['event']['url']}" for item in items]
            return {"text": "\n\n".join(lines) or "هشدار خوانده‌نشده‌ای ثبت نشده است."}
        url = radar.options()["public_url"]
        return {"text": (
            f"رادار ایکس\nحساب‌های رصد: {len(radar.store.list('watch'))}\n"
            f"داشبورد: {url + '/' if url else 'آدرس HTTPS را در تنظیمات ثبت کن.'}\n\n"
            "/xradar scan\n/xradar alerts\n/xradar reply <لینک پست>"
        )}

    return app


def install(parent):
    key = os.getenv("AGANT_X_RADAR_KEY", "").strip()
    directory = os.getenv("AGANT_X_RADAR_DATA_DIR", "").strip()
    if not key and not directory:
        return None
    if (len(key) < 24 or not key.isascii() or any(c.isspace() for c in key)
            or not directory or not Path(directory).is_absolute()):
        LOGGER.warning("X radar disabled: configure its key and absolute data directory")
        return None
    try:
        radar = Radar(directory, key)
    except (OSError, ValueError, ImportError, sqlite3.Error) as exc:
        LOGGER.warning("X radar disabled: data directory is not writable (%s)", type(exc).__name__)
        return None
    parent.mount("/x-radar", create_app(radar, key))
    return radar


async def shutdown(radar, task):
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    if radar:
        await radar.api.client.aclose()
