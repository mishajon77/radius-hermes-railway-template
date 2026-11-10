"""Newsroom decisions use real SQLite, with Telegram replaced at the network seam."""
import asyncio
import importlib.util
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest


PLUGIN = Path(__file__).resolve().parents[1] / "plugins/agant-newsroom"
OWNER, EDITOR, GROUP, CHANNEL = 7, 8, -10010, -10020
POST = {
    "title": "خبر آزمایشی",
    "text": "این متن برای بازبینی آماده شده است.",
    "humor_text": "این نسخه کمی شوخی دارد.",
    "source_url": "https://x.com/club/status/12345?s=20",
    "image_url": "https://pbs.twimg.com/media/example.jpg",
}


@pytest.fixture(scope="module")
def core():
    assert (PLUGIN / "core.py").exists(), "Newsroom state/controller has not been implemented"
    name = "template_newsroom_core"
    spec = importlib.util.spec_from_file_location(name, PLUGIN / "core.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Telegram:
    def __init__(self):
        self.previews = []
        self.edits = []
        self.posts = []
        self.failure = None
        self.edit_failure = None
        self.gate = None

    async def preview(self, draft):
        self.previews.append(draft)
        return 100 + len(self.previews)

    async def edit(self, draft):
        if self.edit_failure:
            raise self.edit_failure
        self.edits.append(draft)

    async def publish(self, draft):
        self.posts.append(draft)
        if self.gate:
            await self.gate.wait()
        if self.failure:
            raise self.failure
        return 200 + len(self.posts)


@pytest.fixture
def room(core, tmp_path):
    settings = core.Settings(OWNER, frozenset({OWNER, EDITOR}), GROUP, CHANNEL, tmp_path / "newsroom.db")
    return core.Newsroom(settings, Telegram())


async def create(room, **changes):
    return await room.create({**POST, **changes}, actor=OWNER, chat=GROUP)


async def publish(room, draft, **changes):
    args = dict(actor=OWNER, chat=GROUP, message_id=draft["preview_id"])
    args.update(changes)
    return await room.publish(draft["id"], draft["revision"], **args)


def test_settings_default_off_and_fail_closed(core, monkeypatch, tmp_path):
    monkeypatch.delenv("AGANT_NEWSROOM_ENABLED", raising=False)
    assert core.Settings.from_env() is None
    monkeypatch.setenv("AGANT_NEWSROOM_ENABLED", "true")
    with pytest.raises(core.NewsroomError):
        core.Settings.from_env()
    for key, value in {
        "AGANT_NEWSROOM_OWNER_ID": str(OWNER),
        "AGANT_NEWSROOM_EDITOR_CHAT_ID": str(GROUP),
        "AGANT_NEWSROOM_CHANNEL_ID": str(CHANNEL),
        "AGANT_NEWSROOM_DB": str(tmp_path / "nr.db"),
    }.items():
        monkeypatch.setenv(key, value)
    assert core.Settings.from_env().editor_ids == frozenset({OWNER})
    monkeypatch.setenv("AGANT_NEWSROOM_CHANNEL_ID", str(GROUP))
    with pytest.raises(core.NewsroomError):
        core.Settings.from_env()


@pytest.mark.asyncio
async def test_draft_and_decisions_survive_new_controller(core, room):
    draft = await create(room)
    other = core.Newsroom(room.settings, Telegram())
    assert other.store.get(draft["id"])["payload"]["text"] == POST["text"]
    sent = await publish(other, draft)
    assert room.store.get(draft["id"])["state"] == "published"
    assert sent["published_id"] == 201
    assert room.store.events(draft["id"])[-1]["action"] == "published"


@pytest.mark.asyncio
async def test_same_source_with_tracking_or_twitter_hostname_is_one_draft(core, room):
    draft = await create(room)
    assert draft["payload"]["source_url"] == "https://x.com/club/status/12345"
    with pytest.raises(core.NewsroomError, match=draft["id"]):
        await create(room, source_url="https://twitter.com/club/status/12345?utm_source=a")
    assert len(room.transport.previews) == 1


@pytest.mark.asyncio
async def test_same_status_id_under_different_author_path_is_not_posted_twice(core, room):
    draft = await create(room)
    with pytest.raises(core.NewsroomError, match=draft["id"]):
        await create(room, source_url="https://x.com/another_author/status/12345")
    assert len(room.transport.previews) == 1


@pytest.mark.parametrize("changes", [
    {"source_url": "https://x.com.evil.example/club/status/12345"},
    {"source_url": "https://x.com/club"},
    {"image_url": "http://pbs.twimg.com/media/photo.jpg"},
    {"image_url": "https://pbs.twimg.com@127.0.0.1/photo.jpg"},
    {"image_url": "/app/local-secret"},
    {"text": "😀" * 600},
    {"title": ""},
])
@pytest.mark.asyncio
async def test_invalid_source_photo_or_oversized_caption_never_reaches_telegram(core, room, changes):
    with pytest.raises(core.NewsroomError):
        await create(room, **changes)
    assert not room.transport.previews


@pytest.mark.asyncio
async def test_non_editor_cannot_prepare_drafts(core, room):
    with pytest.raises(core.NewsroomError):
        await room.create(POST, actor=99, chat=GROUP)
    assert not room.transport.previews


@pytest.mark.parametrize("changes", [{"actor": EDITOR}, {"chat": -10099}, {"message_id": 999}, {"actor": None}])
@pytest.mark.asyncio
async def test_publish_requires_owner_group_and_original_preview(core, room, changes):
    draft = await create(room)
    with pytest.raises(core.NewsroomError):
        await publish(room, draft, **changes)
    assert not room.transport.posts


@pytest.mark.asyncio
async def test_stale_button_cannot_publish_after_edit(core, room):
    draft = await create(room)
    updated = await room.revise(draft["id"], draft["revision"], {**POST, "text": "نسخه جدید"},
                                actor=EDITOR, chat=GROUP)
    with pytest.raises(core.NewsroomError):
        await publish(room, draft)
    await publish(room, updated)
    assert room.transport.posts[0]["payload"]["text"] == "نسخه جدید"


@pytest.mark.asyncio
async def test_switch_variant_invalidates_old_buttons(core, room):
    draft = await create(room)
    chosen = await room.select(draft["id"], draft["revision"], "humor", actor=EDITOR,
                               chat=GROUP, message_id=draft["preview_id"])
    assert chosen["payload"]["variant"] == "humor"
    with pytest.raises(core.NewsroomError):
        await publish(room, draft)
    await publish(room, chosen)
    assert room.transport.posts[0]["payload"]["variant"] == "humor"


@pytest.mark.asyncio
async def test_double_click_while_sending_calls_telegram_once(core, room):
    draft = await create(room)
    room.transport.gate = asyncio.Event()
    task = asyncio.create_task(publish(room, draft))
    await asyncio.sleep(0)
    other = core.Newsroom(room.settings, room.transport)
    with pytest.raises(core.NewsroomError):
        await publish(other, draft)
    room.transport.gate.set()
    await task
    assert len(room.transport.posts) == 1


@pytest.mark.asyncio
async def test_network_timeout_requires_owner_reconciliation_even_after_restart(core, room):
    draft = await create(room)
    room.transport.failure = TimeoutError("network outcome unknown")
    with pytest.raises(core.NewsroomError):
        await publish(room, draft)
    restarted = core.Newsroom(room.settings, Telegram())
    assert restarted.store.get(draft["id"])["state"] == "uncertain"
    with pytest.raises(core.NewsroomError):
        await publish(restarted, draft)
    with pytest.raises(core.NewsroomError):
        await restarted.resolve(draft["id"], 999, actor=EDITOR, chat=GROUP)
    result = await restarted.resolve(draft["id"], 999, actor=OWNER, chat=GROUP)
    assert result["published_id"] == 999
    assert not restarted.transport.posts


@pytest.mark.asyncio
async def test_restart_during_send_does_not_retry_and_owner_can_confirm_not_sent(core, room):
    draft = await create(room)
    room.transport.gate = asyncio.Event()
    task = asyncio.create_task(publish(room, draft))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    restarted = core.Newsroom(room.settings, Telegram())
    assert restarted.store.get(draft["id"])["state"] == "publishing"
    with pytest.raises(core.NewsroomError):
        await publish(restarted, draft)
    repaired = await restarted.resolve(draft["id"], None, actor=OWNER, chat=GROUP)
    assert repaired["state"] == "draft"
    assert repaired["revision"] > draft["revision"]
    await publish(restarted, repaired)
    assert len(restarted.transport.posts) == 1


@pytest.mark.asyncio
async def test_explicit_telegram_rejection_can_retry_without_reconciliation(core, room):
    draft = await create(room)
    room.transport.failure = core.TelegramRejected("Telegram rejected request")
    with pytest.raises(core.NewsroomError):
        await publish(room, draft)
    assert room.store.get(draft["id"])["state"] == "draft"
    room.transport.failure = None
    await publish(room, draft)


@pytest.mark.asyncio
async def test_failed_preview_edit_blocks_post_until_refresh(core, room):
    draft = await create(room)
    room.transport.edit_failure = TimeoutError()
    with pytest.raises(core.NewsroomError):
        await room.revise(draft["id"], 1, {**POST, "text": "متن تازه"}, actor=OWNER, chat=GROUP)
    changed = room.store.get(draft["id"])
    assert changed["state"] == "preview_failed"
    with pytest.raises(core.NewsroomError):
        await publish(room, changed)
    room.transport.edit_failure = None
    repaired = await room.refresh(draft["id"], actor=OWNER, chat=GROUP)
    await publish(room, repaired)
    assert room.transport.posts[0]["payload"]["text"] == "متن تازه"


@pytest.mark.asyncio
async def test_receipt_saved_even_if_preview_cannot_be_marked_published(core, room):
    draft = await create(room)
    room.transport.edit_failure = TimeoutError()
    result = await publish(room, draft)
    assert result["state"] == "published"
    with pytest.raises(core.NewsroomError):
        await publish(room, draft)
    assert len(room.transport.posts) == 1


@pytest.mark.asyncio
async def test_deleted_preview_can_be_explicitly_replaced_without_reusing_old_buttons(core, room):
    draft = await create(room)
    room.transport.edit_failure = core.TelegramRejected("preview was deleted")
    with pytest.raises(core.NewsroomError):
        await room.refresh(draft["id"], actor=OWNER, chat=GROUP)
    replaced = await room.refresh(draft["id"], actor=OWNER, chat=GROUP, replace_preview=True)
    assert replaced["preview_id"] != draft["preview_id"]
    assert replaced["revision"] > draft["revision"]
    with pytest.raises(core.NewsroomError):
        await publish(room, {**replaced, "preview_id": draft["preview_id"]})
    await publish(room, replaced)
    assert len(room.transport.posts) == 1


@pytest.mark.asyncio
async def test_editor_can_reject_but_rejected_draft_cannot_publish(core, room):
    draft = await create(room)
    await room.reject(draft["id"], 1, actor=EDITOR, chat=GROUP, message_id=draft["preview_id"])
    with pytest.raises(core.NewsroomError):
        await publish(room, draft)
    assert room.store.get(draft["id"])["state"] == "rejected"


@pytest.mark.asyncio
async def test_database_cannot_be_rebound_to_a_different_destination(core, room):
    draft = await create(room)
    changed = core.Settings(OWNER, frozenset({OWNER}), GROUP, -10099, room.settings.db_path)
    other = core.Newsroom(changed, Telegram())
    with pytest.raises(core.NewsroomError):
        await publish(other, draft)
    assert not other.transport.posts


@pytest.mark.asyncio
async def test_owner_cannot_reconcile_while_another_controller_is_sending(core, room):
    draft = await create(room)
    room.transport.gate = asyncio.Event()
    task = asyncio.create_task(publish(room, draft))
    await asyncio.sleep(0)
    other = core.Newsroom(room.settings, Telegram())
    with pytest.raises(core.NewsroomError):
        await other.resolve(draft["id"], None, actor=OWNER, chat=GROUP)
    room.transport.gate.set()
    await task
    assert len(room.transport.posts) == 1


@pytest.mark.asyncio
async def test_slow_variant_edit_blocks_other_selection_and_publication(core, room):
    draft = await create(room)
    started, finish = asyncio.Event(), asyncio.Event()

    async def slow_edit(updated):
        started.set()
        await finish.wait()

    room.transport.edit = slow_edit
    task = asyncio.create_task(room.select(draft["id"], 1, "humor", actor=OWNER,
                                           chat=GROUP, message_id=draft["preview_id"]))
    await started.wait()
    with pytest.raises(core.NewsroomError):
        await publish(room, draft)
    with pytest.raises(core.NewsroomError):
        await room.select(draft["id"], 1, "plain", actor=EDITOR, chat=GROUP, message_id=draft["preview_id"])
    finish.set()
    await task
    assert not room.transport.posts


@pytest.fixture(scope="module")
def plugin(core):
    assert (PLUGIN / "__init__.py").exists(), "Newsroom native integration has not been implemented"
    name = "template_newsroom"
    spec = importlib.util.spec_from_file_location(name, PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.modules[name + ".core"] = core
    spec.loader.exec_module(module)
    return module


class Context:
    def __init__(self):
        self.tools = {}
        self.factories = {}
        self.hooks = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_platform_handler(self, platform, factory):
        self.factories[platform] = factory

    def register_hook(self, name, handler):
        self.hooks[name] = handler


@pytest.fixture
def enabled(monkeypatch, tmp_path):
    for key, value in {
        "AGANT_NEWSROOM_ENABLED": "true", "AGANT_NEWSROOM_OWNER_ID": str(OWNER),
        "AGANT_NEWSROOM_EDITOR_IDS": str(EDITOR), "AGANT_NEWSROOM_EDITOR_CHAT_ID": str(GROUP),
        "AGANT_NEWSROOM_CHANNEL_ID": str(CHANNEL), "AGANT_NEWSROOM_DB": str(tmp_path / "ui.db"),
        "TELEGRAM_BOT_TOKEN": "123:offline-test-token",
    }.items():
        monkeypatch.setenv(key, value)


def test_plugin_off_registers_no_handlers_tools_or_hooks(plugin, monkeypatch):
    monkeypatch.delenv("AGANT_NEWSROOM_ENABLED", raising=False)
    ctx = Context()
    plugin.register(ctx)
    assert not ctx.tools and not ctx.factories and not ctx.hooks


@pytest.mark.asyncio
async def test_tools_cannot_publish_and_use_trusted_session_identity(plugin, enabled, monkeypatch):
    ctx = Context()
    plugin.register(ctx)
    tool = ctx.tools["newsroom_draft"]
    assert "publish" not in tool["schema"]["parameters"]["properties"]["action"]["enum"]
    monkeypatch.setattr(plugin, "session_identity", lambda: (99, GROUP))
    result = await tool["handler"]({"action": "create", **POST, "actor": OWNER})
    assert '"ok": false' in result
    assert "telegram" in ctx.factories
    from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler
    table = []
    app = SimpleNamespace(handlers={0: table}, add_handler=table.append)
    ctx.factories["telegram"](app, None)
    handlers = app.handlers[0]
    callback = next(h for h in handlers if isinstance(h, CallbackQueryHandler))
    assert callback.pattern.match("nr:012345abcdef:1:publish")
    assert not callback.pattern.match("approval:other")
    assert any(isinstance(h, CommandHandler) for h in handlers)
    assert any(isinstance(h, MessageHandler) for h in handlers)


@pytest.mark.asyncio
async def test_http_preview_is_original_photo_with_safe_caption_and_revision_buttons(plugin, room):
    calls = []

    def respond(request):
        import json
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 555}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        transport = plugin.HTTPTransport("123:test", client=client)
        draft = await create(room, title="<unsafe>")
        assert await transport.preview(draft) == 555
        assert await transport.publish(draft) == 555
    preview, posted = calls
    assert preview[0].endswith("/sendPhoto")
    assert preview[1]["photo"] == POST["image_url"]
    assert preview[1]["chat_id"] == GROUP
    assert "&lt;unsafe&gt;" in preview[1]["caption"]
    assert "nr:" in str(preview[1]["reply_markup"])
    assert posted[1]["chat_id"] == CHANNEL
    assert "reply_markup" not in posted[1]
    assert draft["id"] not in posted[1]["caption"]
    assert "https://x.com/club/status/12345" in posted[1]["caption"]


@pytest.mark.asyncio
async def test_http_failure_classes_do_not_leak_token(plugin):
    from_template = plugin.NewsroomError
    for status, data, known_rejection in [
        (400, {"ok": False, "error_code": 400, "description": "fixture-token-secret"}, True),
        (502, {"error": "proxy"}, False),
        (200, {"ok": True, "result": {}}, False),
    ]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=data))) as client:
            transport = plugin.HTTPTransport("fixture-token-secret", client=client)
            with pytest.raises(from_template) as caught:
                await transport.send("sendMessage", {"chat_id": GROUP, "text": "test"})
        assert "fixture-token-secret" not in str(caught.value)
        assert isinstance(caught.value, plugin.TelegramRejected) == known_rejection


@pytest.mark.asyncio
async def test_httpx_request_logs_redact_telegram_token(plugin, caplog):
    secret = "12345:private-bot-token-fixture"
    with caplog.at_level(logging.INFO, logger="httpx"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"ok": True, "result": {"message_id": 1}}))) as client:
            transport = plugin.HTTPTransport(secret, client=client)
            await transport.send("sendMessage", {"chat_id": GROUP, "text": "test"})
    assert secret not in caplog.text
    assert "[redacted]" in caplog.text


@pytest.mark.asyncio
async def test_native_reply_send_requires_button_and_revision_reply_hook_uses_message_id(plugin, room):
    draft = await create(room)
    transport = room.transport
    transport.notices = []

    async def notice(text, reply_to=None):
        transport.notices.append(text)

    transport.notice = notice
    ui = plugin.NativeUI(room)
    message = SimpleNamespace(text="ارسال", chat_id=GROUP, message_id=50, sender_chat=None,
                              from_user=SimpleNamespace(id=OWNER, is_bot=False),
                              reply_to_message=SimpleNamespace(message_id=draft["preview_id"]))
    await ui.on_reply(SimpleNamespace(effective_message=message), None)
    assert not transport.posts
    assert transport.notices and "دکمه" in transport.notices[0]
    event = SimpleNamespace(text="کوتاه‌تر کن", reply_to_message_id=str(draft["preview_id"]),
                            source=SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id=str(GROUP), user_id=str(OWNER)))
    injected = plugin.reply_reference(room, event)
    assert draft["id"] in injected["text"]
    assert "revision=1" in injected["text"]
    assert "کوتاه‌تر کن" in injected["text"]


@pytest.mark.asyncio
async def test_native_callback_checks_telegram_sender_and_original_message(plugin, room):
    draft = await create(room)
    ui = plugin.NativeUI(room)
    answers = []

    async def answer(*args, **kwargs):
        answers.append((args, kwargs))

    query = SimpleNamespace(data=f'nr:{draft["id"]}:1:publish', answer=answer,
                            from_user=SimpleNamespace(id=EDITOR, is_bot=False),
                            message=SimpleNamespace(chat_id=GROUP, message_id=draft["preview_id"]))
    await ui.on_callback(SimpleNamespace(callback_query=query), None)
    assert not room.transport.posts
    query.from_user.id = OWNER
    query.message.message_id = 999
    await ui.on_callback(SimpleNamespace(callback_query=query), None)
    assert not room.transport.posts
    assert answers


@pytest.mark.asyncio
async def test_owner_callback_publishes_visible_variant_and_returns_real_receipt_link(plugin, room):
    draft = await create(room)
    ui = plugin.NativeUI(room)
    notices = []

    async def answer(*args, **kwargs):
        pass

    async def notice(text, reply_to=None):
        notices.append(text)

    room.transport.notice = notice
    query = SimpleNamespace(data=f'nr:{draft["id"]}:1:humor', answer=answer,
                            from_user=SimpleNamespace(id=OWNER, is_bot=False),
                            message=SimpleNamespace(chat_id=GROUP, message_id=draft["preview_id"]))
    await ui.on_callback(SimpleNamespace(callback_query=query), None)
    updated = room.store.get(draft["id"])
    query.data = f'nr:{draft["id"]}:{updated["revision"]}:publish'
    await ui.on_callback(SimpleNamespace(callback_query=query), None)
    assert len(room.transport.posts) == 1
    assert room.transport.posts[0]["payload"]["variant"] == "humor"
    assert "https://t.me/c/20/201" in notices[-1]
