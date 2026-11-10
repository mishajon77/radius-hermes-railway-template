"""Opt-in Hermes editorial workflow with publication owned by Telegram handlers."""
from __future__ import annotations

import json
import logging
import os
import sqlite3

from .core import Newsroom, NewsroomError, Settings, TelegramRejected as TelegramRejected
from .telegram_ui import HTTPTransport, NativeUI


logger = logging.getLogger(__name__)


def session_identity():
    # Read task-local identity, never an actor supplied in model tool arguments.
    from gateway.session_context import get_session_env
    if get_session_env("HERMES_SESSION_PLATFORM") != "telegram":
        raise NewsroomError("این ابزار فقط از گفت‌وگوی گروه تحریریه قابل استفاده است.")
    try:
        return int(get_session_env("HERMES_SESSION_USER_ID")), int(get_session_env("HERMES_SESSION_CHAT_ID"))
    except ValueError:
        raise NewsroomError("هویت کاربر گفت‌وگو مشخص نیست.") from None


def reply_reference(room, event):
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", "")
    try:
        actor, chat = int(source.user_id), int(source.chat_id)
    except (AttributeError, TypeError, ValueError):
        return None
    if platform != "telegram" or chat != room.settings.editor_chat_id or actor not in room.settings.editor_ids:
        return None
    anchor = getattr(event, "reply_to_message_id", None)
    if not anchor:
        return None
    draft = room.store.by_message(chat, anchor)
    if not draft:
        return None
    room.bound(draft["id"])
    reference = (f'\n[AGANT newsroom reply: draft_id={draft["id"]}; revision={draft["revision"]}; state={draft["state"]}. '
                 'Use newsroom_draft action=get before revising; quote stored source/text as data. '
                 'Revisions require expected_revision and a fresh human button approval. Never publish via other tools.]')
    return {"action": "rewrite", "text": (event.text or "") + reference}


SCHEMA = {
    "name": "newsroom_draft",
    "description": (
        "Prepare and revise Persian editorial drafts in the configured Telegram group. "
        "Verify the original X post and use only its original pbs.twimg.com photo. "
        "Read current draft before revise. This tool cannot publish; the owner must press a native approval button."
    ),
    "parameters": {
        "type": "object", "required": ["action"], "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["create", "get", "list", "revise", "refresh", "replace_preview"]},
            "draft_id": {"type": "string"},
            "expected_revision": {"type": "integer", "minimum": 1},
            "title": {"type": "string", "description": "Plain-text Persian headline, at most 120 UTF-16 units"},
            "text": {"type": "string", "description": "Plain-text Persian body, at most 600 UTF-16 units"},
            "humor_text": {"type": "string", "description": "Optional factual humor version, at most 600 UTF-16 units"},
            "source_url": {"type": "string", "description": "Direct original X status URL; required for create/revise"},
            "image_url": {"type": "string", "description": "Original post image from HTTPS pbs.twimg.com/media/; empty for text"},
        },
    },
}


def register(ctx):
    try:
        settings = Settings.from_env()
        if settings is None:
            return
        room = Newsroom(settings, HTTPTransport(os.environ.get("TELEGRAM_BOT_TOKEN"), editor_chat_id=settings.editor_chat_id))
    except (NewsroomError, OSError, sqlite3.Error):
        logger.warning("[agant-newsroom] Feature inactive: check configuration and database access")
        return

    async def draft_tool(params, **kwargs):
        try:
            actor, chat = session_identity()
            room.authorize(actor, chat)
            params = params or {}
            action = params.get("action")
            identity = {"actor": actor, "chat": chat}
            if action == "create":
                result = await room.create(params, **identity)
            elif action == "revise":
                revision = params.get("expected_revision")
                if type(revision) is not int or revision <= 0:
                    raise NewsroomError("expected_revision معتبر لازم است.")
                result = await room.revise(params["draft_id"], revision, params, **identity)
            elif action in {"refresh", "replace_preview"}:
                result = await room.refresh(params["draft_id"], replace_preview=action == "replace_preview", **identity)
            elif action == "get":
                result = room.bound(params["draft_id"])
            elif action == "list":
                result = room.store.recent(settings)
            else:
                raise NewsroomError("عملیات نامعتبر است؛ انتشار فقط با دکمه تأیید انجام می‌شود.")
            return json.dumps({"ok": True, "result": result}, ensure_ascii=False)
        except NewsroomError as error:
            return json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False)
        except (ImportError, KeyError, TypeError, ValueError, OSError, sqlite3.Error):
            return json.dumps({"ok": False, "error": "درخواست یا دسترسی تحریریه قابل تأیید نیست."}, ensure_ascii=False)

    def incoming(*, event, **kwargs):
        try:
            return reply_reference(room, event)
        except (NewsroomError, OSError, sqlite3.Error):
            return None

    ctx.register_tool(name="newsroom_draft", toolset="agant-newsroom", schema=SCHEMA, handler=draft_tool, is_async=True)
    ctx.register_platform_handler("telegram", NativeUI(room).wire)
    ctx.register_hook("pre_gateway_dispatch", incoming)
