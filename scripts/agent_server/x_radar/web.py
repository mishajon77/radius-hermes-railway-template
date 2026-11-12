"""Authenticated dashboard API and local Hermes gateway bridge."""
import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import time
from collections import deque
from contextlib import suppress
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .radar import DEFAULTS, INBOX_TITLES, Radar
from .store import RadarError
from .turso import TursoDatabase


COOKIE = "agant_radar_session"
STATIC = Path(__file__).parent / "static"
LOGGER = logging.getLogger(__name__)
SESSION_SECONDS = 12 * 3600


class Login(BaseModel):
    password: str = Field(max_length=256)


class Options(BaseModel):
    owner: str = Field("", max_length=120)
    public_url: str = Field("", max_length=500)
    smart_list: str = Field("", max_length=200)
    smart_handles: str = Field("", max_length=120000)
    scheduled: bool = DEFAULTS["scheduled"]
    interval: int = Field(DEFAULTS["interval"], ge=3, le=1440)
    daily_limit: int = Field(DEFAULTS["daily_limit"], ge=1, le=50000)
    telegram_enabled: bool = DEFAULTS["telegram_enabled"]


class Secret(BaseModel):
    service: str = Field(max_length=20)
    key: str = Field(max_length=500)


class Draft(BaseModel):
    url: str = Field(max_length=300)
    regenerate: bool = False


class Bridge(BaseModel):
    command: str = Field("", max_length=500)


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
            limit = 1024**2
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

    @app.get("/api/state", dependencies=access)
    def state():
        return radar.state()

    @app.get("/api/inbox", dependencies=access)
    def inbox(kind: str = "", page: int = 1):
        if (kind and kind not in INBOX_TITLES) or not 1 <= page <= 100000:
            raise RadarError("نوع فهرست یا شمارهٔ صفحه معتبر نیست.")
        return radar.inbox(kind, page)

    @app.post("/api/options", dependencies=access)
    def options(data: Options):
        return radar.save_options(data.model_dump(exclude_unset=True))

    @app.post("/api/secret", dependencies=access)
    def save_secret(data: Secret):
        if data.service not in ("sorsa", "openrouter"):
            raise RadarError("نام سرویس معتبر نیست.")
        if data.key and (not data.key.isascii() or any(c.isspace() for c in data.key)):
            raise RadarError("کلید سرویس باید بدون فاصله باشد.")
        radar.store.set_secret(data.service, data.key)
        if data.service == "openrouter":
            radar.store.delete("system", "model_resume")
        return {"configured": bool(radar.api.key(data.service))}

    @app.post("/api/scan", dependencies=access)
    async def scan():
        return await radar.scan()

    @app.post("/api/draft", dependencies=access)
    async def draft(data: Draft):
        return await radar.draft(data.url, data.regenerate)

    @app.post("/api/bridge")
    async def bridge(data: Bridge, request: Request):
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth.encode(), ("Bearer " + secret).encode()):
            raise HTTPException(401, "دسترسی به رادار مجاز نیست.")
        command, _, rest = data.command.strip().partition(" ")
        if command == "scan":
            result = await radar.scan()
            summary = f"رادار ایکس\\n{result['new']} مورد تازه از {result['requests']} درخواست سورسا"
            if result["errors"]:
                summary += "\\n" + "\\n".join(result["errors"][:5])
            return {"text": summary}
        if command == "reply":
            result = await radar.draft(rest)
            return {"text": f"پیش‌نویس پاسخ\\n{result['text']}\\n\\n{result['post']['url']}\\n{result['intent']}"}
        if command == "alerts":
            lines = []
            inbox = await asyncio.to_thread(radar.inbox)
            for item in inbox["rows"][:4]:
                post, draft = item["post"], item["draft"]
                entry = f"{INBOX_TITLES[item['kind']]} · @{post['author']}\\n{post['url']}"
                if draft:
                    entry += f"\\n{draft['text']}\\n{draft['intent']}"
                lines.append(entry)
            return {"text": "\\n\\n".join(lines) or "مورد تازه‌ای در داشبورد ثبت نشده است."}
        options = await asyncio.to_thread(radar.options)
        url = options["public_url"]
        address = url.rstrip("/") + "/" if url else "آدرس HTTPS را در تنظیمات ثبت کن."
        return {"text": (
            f"رادار ایکس\\nداشبورد {address}\\n\\n"
            "/xradar scan\\n/xradar alerts\\n/xradar reply <لینک پست>"
        )}

    return app


def install(parent):
    key = os.getenv("AGANT_X_RADAR_KEY", "").strip()
    directory = os.getenv("AGANT_X_RADAR_DATA_DIR", "").strip()
    url = os.getenv("AGANT_X_RADAR_TURSO_URL", "").strip()
    token = os.getenv("AGANT_X_RADAR_TURSO_TOKEN", "").strip()
    use_turso = bool(url or token)
    if not any((key, directory, use_turso)):
        return None
    if (len(key) < 24 or not key.isascii() or any(c.isspace() for c in key)
            or (not use_turso and (not directory or not Path(directory).is_absolute()))):
        LOGGER.warning("X radar disabled: configure its key and a volume path or Turso database")
        return None
    remote = None
    try:
        remote = TursoDatabase(url, token) if use_turso else None
        radar = Radar(directory, key, remote=remote)
    except (OSError, ValueError, ImportError, sqlite3.Error) as exc:
        if remote is not None:
            remote.close()
        LOGGER.warning("X radar disabled: invalid database configuration (%s)", type(exc).__name__)
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
        await asyncio.to_thread(radar.store.close)
