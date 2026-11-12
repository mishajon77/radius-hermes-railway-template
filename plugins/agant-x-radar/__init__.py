"""Expose the radar in the owner's existing Telegram conversation."""
import hmac
import os

import httpx


async def radar_command(raw_args):
    from gateway.session_context import get_session_env

    owner = os.getenv("AGANT_X_RADAR_TELEGRAM_OWNER", "")
    platform = get_session_env("HERMES_SESSION_PLATFORM")
    user_id = get_session_env("HERMES_SESSION_USER_ID")
    chat_type = get_session_env("HERMES_SESSION_CHAT_TYPE")
    if not owner or platform != "telegram" or chat_type != "dm":
        return "رادار ایکس در گفت‌وگوی خصوصی مالک در دسترس است."
    if not hmac.compare_digest(owner, user_id):
        return "رادار ایکس در گفت‌وگوی خصوصی مالک در دسترس است."
    key = os.getenv("AGANT_X_RADAR_KEY", "").strip()
    if not key:
        return "کلید پنل رادار در تنظیمات سرویس ثبت نشده است."
    port = os.getenv("PORT", "3000")
    if not port.isdecimal():
        return "پورت سرویس رادار معتبر نیست."
    try:
        async with httpx.AsyncClient(timeout=150, trust_env=False, follow_redirects=False) as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/x-radar/api/bridge",
                headers={"Authorization": "Bearer " + key},
                json={"command": raw_args[:500]},
            )
            if response.status_code == 404:
                return "پنل رادار فعال نیست. مسیر ذخیره‌سازی را در سرویس بررسی کن."
            payload = response.json()
            return payload.get("text") or str(payload.get("detail", "درخواست رادار انجام نشد."))
    except (httpx.HTTPError, ValueError, AttributeError):
        return "ارتباط با پنل رادار برقرار نشد."


def register(ctx):
    ctx.register_command(
        "xradar", radar_command, "پنل خصوصی رصد و پیش‌نویس پاسخ X", args_hint="[scan | alerts | reply <URL>]",
    )
