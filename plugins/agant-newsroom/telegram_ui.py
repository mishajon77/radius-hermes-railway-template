"""Telegram HTTP and scoped native handlers; Hermes retains its existing poller."""
from __future__ import annotations

import logging
import re

import httpx

from .core import NewsroomError, TelegramRejected, buttons, caption, post_link


CALLBACK = re.compile(r"^nr:([a-f0-9]{12}):([0-9]{1,9}):(plain|humor|publish|reject)$")
REPLY_ACTIONS = {"ارسال", "رد"}


class TelegramLogRedaction(logging.Filter):
    """httpx logs request URLs at INFO; Telegram embeds the credential in that URL."""
    _agant_telegram_redaction = True
    pattern = re.compile(r"(https://api\.telegram\.org/(?:file/)?bot)[^/\s\"']+")

    def filter(self, record):
        message = record.getMessage()
        clean = self.pattern.sub(r"\1[redacted]", message)
        if clean != message:
            record.msg, record.args = clean, ()
        return True


class HTTPTransport:
    def __init__(self, token, *, editor_chat_id=None, client=None):
        if not token:
            raise NewsroomError("توکن ربات در محیط اجرا تنظیم نشده است.")
        self._token, self._client, self.editor_chat_id = token, client, editor_chat_id
        logger = logging.getLogger("httpx")
        if not any(getattr(item, "_agant_telegram_redaction", False) for item in logger.filters):
            logger.addFilter(TelegramLogRedaction())

    async def send(self, method, payload):
        async def request(client):
            try:
                response = await client.post(f"https://api.telegram.org/bot{self._token}/{method}", json=payload)
                data = response.json()
            except (httpx.HTTPError, ValueError):
                raise NewsroomError("ارتباط تلگرام نتیجه قابل تأیید نداد.") from None
            if not isinstance(data, dict):
                raise NewsroomError("پاسخ تلگرام قابل تأیید نیست.")
            code = data.get("error_code")
            if data.get("ok") is False and type(code) is int and 400 <= code < 500:
                raise TelegramRejected("تلگرام درخواست را رد کرد.")
            if response.status_code != 200 or data.get("ok") is not True:
                raise NewsroomError("نتیجه درخواست تلگرام نامشخص است.")
            result = data.get("result")
            if method in {"sendPhoto", "sendMessage"}:
                receipt = result.get("message_id") if isinstance(result, dict) else None
                if type(receipt) is not int or receipt <= 0:
                    raise NewsroomError("رسید پیام تلگرام دریافت نشد.")
                return receipt
            return result

        if self._client is not None:
            return await request(self._client)
        async with httpx.AsyncClient(timeout=30) as client:
            return await request(client)

    async def _send_draft(self, draft, preview):
        payload = {"chat_id": draft["editor_chat_id"] if preview else draft["channel_id"], "parse_mode": "HTML"}
        photo = draft["payload"]["image_url"]
        payload["caption" if photo else "text"] = caption(draft, preview)
        if photo:
            payload["photo"] = photo
        else:
            payload["link_preview_options"] = {"is_disabled": True}
        if preview:
            payload["reply_markup"] = buttons(draft)
        return await self.send("sendPhoto" if photo else "sendMessage", payload)

    async def preview(self, draft):
        return await self._send_draft(draft, True)

    async def publish(self, draft):
        return await self._send_draft(draft, False)

    async def edit(self, draft):
        photo = draft["payload"]["image_url"]
        payload = {"chat_id": draft["editor_chat_id"], "message_id": draft["preview_id"],
                   "parse_mode": "HTML", "reply_markup": buttons(draft),
                   "caption" if photo else "text": caption(draft)}
        if not photo:
            payload["link_preview_options"] = {"is_disabled": True}
        return await self.send("editMessageCaption" if photo else "editMessageText", payload)

    async def notice(self, text, reply_to=None):
        payload = {"chat_id": self.editor_chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return await self.send("sendMessage", payload)


class NativeUI:
    def __init__(self, room):
        self.room = room

    @staticmethod
    def actor(user):
        if user is None or user.is_bot or type(user.id) is not int:
            raise NewsroomError("شناسه فرستنده قابل تأیید نیست.")
        return user.id

    async def on_callback(self, update, context):
        query = update.callback_query
        try:
            match = CALLBACK.fullmatch(query.data or "")
            if not match or query.message is None:
                raise NewsroomError("دکمه نامعتبر است.")
            draft_id, revision, action = match.groups()
            actor = self.actor(query.from_user)
            chat, message_id = query.message.chat_id, query.message.message_id
            self.room.authorize(actor, chat, owner=action == "publish")
            self.room.bound(draft_id, message_id)
            await query.answer()
            params = dict(actor=actor, chat=chat, message_id=message_id)
            if action == "publish":
                draft = await self.room.publish(draft_id, int(revision), **params)
                try:
                    await self.room.transport.notice("✅ در کانال منتشر شد:\n" + post_link(draft), reply_to=message_id)
                except Exception:
                    pass  # Receipt is durable even if the optional group notice fails.
            elif action == "reject":
                await self.room.reject(draft_id, int(revision), **params)
            else:
                await self.room.select(draft_id, int(revision), action, **params)
        except NewsroomError as error:
            await query.answer(str(error)[:180], show_alert=True)

    async def on_reply(self, update, context):
        message = update.effective_message
        try:
            if message.sender_chat is not None:
                raise NewsroomError("اقدام با هویت ناشناس پذیرفته نمی‌شود.")
            actor = self.actor(message.from_user)
            action = (message.text or "").strip()
            self.room.authorize(actor, message.chat_id, owner=action == "ارسال")
            draft = self.room.store.by_message(message.chat_id, message.reply_to_message.message_id)
            if not draft:
                return
            self.room.bound(draft["id"], message.reply_to_message.message_id)
            if action == "رد":
                await self.room.reject(draft["id"], draft["revision"], actor=actor, chat=message.chat_id,
                                       message_id=draft["preview_id"])
            else:
                # A mutable reply anchor cannot prove which version the owner saw.
                # The original inline button binds publication to an exact revision.
                await self.room.transport.notice(
                    f'برای انتشار نسخه v{draft["revision"]}، دکمه «انتشار همین نسخه» زیر پیش‌نمایش را بزن.',
                    reply_to=draft["preview_id"])
        except NewsroomError as error:
            if message.chat_id == self.room.settings.editor_chat_id and getattr(message.from_user, "id", None) in self.room.settings.editor_ids:
                await self.room.transport.notice(str(error), reply_to=message.message_id)

    async def on_command(self, update, context):
        message = update.effective_message
        try:
            if message.sender_chat is not None:
                return
            actor = self.actor(message.from_user)
            self.room.authorize(actor, message.chat_id)
            args = (message.text or "").split()[1:]
            if args and args[0] == "resolve":
                self.room.authorize(actor, message.chat_id, owner=True)
                if len(args) != 3:
                    raise NewsroomError("/newsroom resolve DRAFT_ID MESSAGE_ID|not-sent")
                try:
                    receipt = None if args[2] == "not-sent" else int(args[2])
                except ValueError:
                    raise NewsroomError("شماره پیام کانال معتبر نیست.") from None
                draft = await self.room.resolve(args[1], receipt, actor=actor, chat=message.chat_id)
                text = f'وضعیت {draft["id"]}: {draft["state"]}'
            else:
                rows = self.room.store.recent(self.room.settings)
                text = f'تحریریه فعال است.\nشناسه تو: {actor}\nگروه: {message.chat_id}\nکانال: {self.room.settings.channel_id}\n'
                text += "\n".join(f'{r["id"]} · v{r["revision"]} · {r["state"]}' for r in rows)
            await self.room.transport.notice(text, reply_to=message.message_id)
        except NewsroomError as error:
            if message.chat_id == self.room.settings.editor_chat_id and getattr(message.from_user, "id", None) in self.room.settings.editor_ids:
                await self.room.transport.notice(str(error), reply_to=message.message_id)

    def wire(self, application, adapter):
        # Native SDK imports stay here so loading/registering the plugin is portable.
        from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

        room = self.room

        class DraftReply(filters.MessageFilter):
            def filter(self, message):
                if message.chat_id != room.settings.editor_chat_id or not message.reply_to_message:
                    return False
                if (message.text or "").strip() not in REPLY_ACTIONS:
                    return False
                return room.store.by_message(message.chat_id, message.reply_to_message.message_id) is not None

        application.add_handler(CallbackQueryHandler(self.on_callback, pattern=CALLBACK))
        application.add_handler(CommandHandler("newsroom", self.on_command, filters=filters.Chat(room.settings.editor_chat_id)))
        application.add_handler(MessageHandler(DraftReply(), self.on_reply))
