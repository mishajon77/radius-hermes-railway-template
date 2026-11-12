"""Exercise the two feeds, draft-only replies, and the private Hermes entry points."""
import importlib.util
import json
import sys
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.agent_server.x_radar.radar import Radar, now
from scripts.agent_server.x_radar.store import RadarError
from scripts.agent_server.x_radar.web import create_app


KEY = "example-secret-for-radar-tests-only"
HEADERS = {"X-Radar-Request": "1"}


def post(post_id, author, parent=None, **fields):
    return {
        "id": str(post_id),
        "full_text": "The migration window is short enough to matter for users",
        "user": {"id": "100", "username": author},
        "created_at": (now() - timedelta(seconds=5)).isoformat(),
        "is_reply": bool(parent), "in_reply_to_username": parent,
        "in_reply_to_tweet_id": "123" if parent else None,
    } | fields


@pytest.mark.asyncio
async def test_batched_feeds_draft_and_notify_the_owner_once(tmp_path, monkeypatch):
    monkeypatch.setenv("SORSA_API_KEY", "test-sorsa")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-model")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:fake-token")
    monkeypatch.setenv("AGANT_X_RADAR_TELEGRAM_OWNER", "22")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_MODEL", "a/paid-model")
    searches, messages = [], []

    def provider(request):
        payload = json.loads(request.content)
        if request.url.host == "api.telegram.org":
            messages.append(payload)
            return httpx.Response(200, json={"ok": True})
        if request.url.host == "openrouter.ai":
            assert payload["model"] == "openrouter/free"
            assert "migration window" in payload["messages"][1]["content"]
            return httpx.Response(200, json={"choices": [{"message": {
                "content": "The short window matters here, and I wonder how users get notified."
            }}]})
        assert request.url.host == "api.sorsa.io"
        assert request.url.path == "/v3/search-tweets"
        assert request.headers["ApiKey"] == "test-sorsa"
        assert payload["order"] == "latest"
        query = payload["query"]
        searches.append(query)
        if query.startswith("to:owner filter:replies"):
            posts = [
                post(12, "friend", "owner"), post(13, "friend", "someone_else"),
                post(14, "friend", full_text="Mentioning @owner"),
                post(15, "owner", "owner"),
            ]
        else:
            assert "from:alpha OR from:beta" in query
            posts = [
                post(21, "alpha"), post(22, "beta", "someone_else"),
                post(23, "outsider"), post(24, "alpha", retweeted_status={"id": "90"}),
            ]
        return httpx.Response(200, json={"tweets": posts})

    radar = Radar(tmp_path, KEY, httpx.MockTransport(provider))
    radar.save_options({"owner": "owner", "smart_handles": ["alpha", "beta"], "telegram_enabled": True})
    try:
        for _ in range(2):
            result = await radar.scan()
            assert result["requests"] == 2
            await radar.process_pending()
        items = radar.inbox()["rows"]
        assert {(item["kind"], item["post"]["id"]) for item in items} == {("comment", "12"), ("smart", "21")}
        assert len(searches) == 4 and len(messages) == 2
        assert {message["chat_id"] for message in messages} == {"22"}
        for item in items:
            draft = item["draft"]
            query = parse_qs(urlsplit(draft["intent"]).query)
            assert query == {"in_reply_to": [item["post"]["id"]], "text": [draft["text"]]}
            assert ", and" not in draft["text"] and not draft["text"].endswith(".")
            assert any(draft["intent"] in json.dumps(message) for message in messages)
            assert any(item["post"]["url"] in message["text"] for message in messages)
        before = len(searches)
        assert (await radar.draft("12"))["post"]["id"] == "12"
        assert len(searches) == before
    finally:
        await radar.api.client.aclose()


@pytest.mark.asyncio
async def test_public_list_cursor_survives_restart_and_daily_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("SORSA_API_KEY", "test-sorsa")
    seen = []

    def provider(request):
        assert request.method == "GET"
        assert request.url.path == "/v3/list-tweets"
        assert request.url.params["list_id"] == "123456"
        page = request.url.params.get("next_cursor")
        seen.append(page)
        if len(seen) == 1:
            return httpx.Response(200, json={"tweets": [post(51, "alpha")], "next_cursor": "next"})
        assert page == "next"
        return httpx.Response(200, json={"tweets": [post(50, "beta")]})

    radar = Radar(tmp_path, KEY, httpx.MockTransport(provider))
    radar.save_options({"smart_list": "123456", "daily_limit": 2})
    try:
        assert (await radar.scan())["pending"] == 1
    finally:
        await radar.api.client.aclose()
    radar = Radar(tmp_path, KEY, httpx.MockTransport(provider))
    try:
        assert (await radar.scan())["pending"] == 0
        assert {row["post"]["id"] for row in radar.inbox()["rows"]} == {"50", "51"}
        assert (await radar.scan())["errors"]
        assert seen == [None, "next"]
    finally:
        await radar.api.client.aclose()


@pytest.mark.asyncio
async def test_remaining_credit_does_not_starve_smart_feed(tmp_path, monkeypatch):
    monkeypatch.setenv("SORSA_API_KEY", "test-sorsa")
    calls = []

    def provider(request):
        calls.append(json.loads(request.content)["query"])
        return httpx.Response(200, json={"tweets": []})

    radar = Radar(tmp_path, KEY, httpx.MockTransport(provider))
    radar.save_options({"owner": "owner", "smart_handles": ["alpha"], "daily_limit": 1})
    try:
        first = await radar.scan()
        assert first["requests"] == 1 and first["pending"] == 1
        radar.store.delete("usage", now().date().isoformat())
        second = await radar.scan()
        assert second["requests"] == 1
        assert calls[0].startswith("to:owner ") and "from:alpha" in calls[1]
    finally:
        await radar.api.client.aclose()


@pytest.mark.asyncio
async def test_model_limit_keeps_new_source_and_stops_repeated_free_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("SORSA_API_KEY", "test-sorsa")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-model")
    requests = []

    def provider(request):
        requests.append(request.url.host)
        if request.url.host == "api.sorsa.io":
            return httpx.Response(200, json={"tweets": [
                post(20, "first", "owner"), post(21, "second", "owner"),
            ]})
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    radar = Radar(tmp_path, KEY, httpx.MockTransport(provider))
    radar.save_options({"owner": "owner"})
    try:
        assert (await radar.scan())["new"] == 2
        await radar.process_pending()
        await radar.process_pending()
        assert requests == ["api.sorsa.io", "openrouter.ai"]
        assert len(radar.inbox()["rows"]) == 2
        assert all(item["draft_error"] and item["post"]["url"] for item in radar.inbox()["rows"])
    finally:
        await radar.api.client.aclose()


def test_private_settings_accept_handles_and_have_two_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("AGANT_X_RADAR_PUBLIC_URL", "https://radar.example")
    radar = Radar(tmp_path, KEY, httpx.MockTransport(lambda request: httpx.Response(503)))
    parent = FastAPI()
    parent.mount("/x-radar", create_app(radar, KEY))
    with TestClient(parent, base_url="https://testserver") as client:
        page = client.get("/x-radar/").text
        assert 'href="#dashboard"' in page and 'href="#settings"' in page
        assert 'href="#followers"' not in page and 'href="#alerts"' not in page
        assert client.get("/x-radar/api/state").status_code == 401
        assert client.get("/x-radar/api/inbox").status_code == 401
        assert client.post("/x-radar/api/login", json={"password": KEY}).status_code == 403
        assert client.post("/x-radar/api/login", headers=HEADERS, json={"password": KEY}).status_code == 200
        options = client.get("/x-radar/api/state").json()["options"]
        assert options["interval"] == 180
        assert options["scheduled"] is False
        assert client.post("/x-radar/api/options", json={"owner": "owner"}).status_code == 403
        saved = client.post("/x-radar/api/options", headers=HEADERS, json={
            "owner": "@Owner", "smart_handles": "@Alpha، https://x.com/Beta\nalpha",
            "smart_list": "https://x.com/i/lists/123456?s=20",
        })
        assert saved.status_code == 200
        assert saved.json()["smart_handles"] == ["alpha", "beta"] and saved.json()["smart_list"] == "123456"
        assert client.post("/x-radar/api/secret", headers=HEADERS, json={
            "service": "twitter", "key": "unused-paid-key",
        }).status_code == 400
        radar.store.set_secret("sorsa", "test-private-key")
        data = client.get("/x-radar/api/state")
        assert data.status_code == 200 and data.json()["keys"]["sorsa"]
        assert "test-private-key" not in data.text
        assert client.get("/x-radar/api/members").status_code == 404
        assert client.post("/x-radar/api/bridge", json={"command": "alerts"}).status_code == 401
        response = client.post("/x-radar/api/bridge", headers={"Authorization": "Bearer " + KEY}, json={})
        assert "https://radar.example/x-radar/" in response.json()["text"]


@pytest.mark.asyncio
async def test_old_dashboard_does_not_enable_unsolicited_paid_scans(tmp_path, monkeypatch):
    monkeypatch.setenv("SORSA_API_KEY", "test-sorsa")

    def provider(_request):
        pytest.fail("old configuration must not trigger provider calls")

    radar = Radar(tmp_path, KEY, httpx.MockTransport(provider))
    radar.store.put("setting", "options", {"owner": "owner", "scheduled": True, "telegram_enabled": True})
    radar.store.put("watch", "old", {"handle": "old"})
    try:
        assert radar.options()["owner"] == "owner"
        assert not radar.options()["scheduled"]
        assert not radar.options()["telegram_enabled"]
        assert radar.options()["smart_handles"] == []
        with pytest.raises(RadarError, match="لینک"):
            await radar.draft("https://example.org/owner/status/12")
    finally:
        await radar.api.client.aclose()


@pytest.mark.asyncio
async def test_command_owner_gate_uses_pinned_gateway_context(monkeypatch):
    context = ModuleType("gateway.session_context")
    session = {"HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_CHAT_TYPE": "dm",
               "HERMES_SESSION_USER_ID": "99"}
    context.get_session_env = lambda name: session.get(name, "")
    monkeypatch.setitem(sys.modules, "gateway.session_context", context)
    monkeypatch.setenv("AGANT_X_RADAR_TELEGRAM_OWNER", "100")
    plugin_path = Path(__file__).resolve().parents[1] / "plugins/agant-x-radar/__init__.py"
    spec = importlib.util.spec_from_file_location("test_radar_plugin", plugin_path)
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    assert "خصوصی مالک" in await plugin.radar_command("scan")
    session["HERMES_SESSION_USER_ID"] = "100"
    monkeypatch.delenv("AGANT_X_RADAR_KEY", raising=False)
    assert "کلید پنل" in await plugin.radar_command("")
