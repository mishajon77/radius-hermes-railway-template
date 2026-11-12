"""Exercise the read-only radar through its service and HTTP entry points."""
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
from scripts.agent_server.x_radar.store import RadarError, Store
from scripts.agent_server.x_radar.web import create_app


KEY = "example-secret-for-radar-tests-only"
HEADERS = {"X-Radar-Request": "1"}


def user(account, uid, followers=100, following=50):
    return {
        "id": str(uid), "username": account, "display_name": account,
        "followers_count": followers, "followings_count": following,
    }


def reply(post_id, author="friend", parent="owner", created=None):
    return {
        "id": str(post_id), "full_text": "The first claim will depend on the migration window",
        "user": user(author, 100), "is_reply": True, "in_reply_to_username": parent,
        "in_reply_to_tweet_id": "123", "created_at": (created or now()).isoformat(),
    }


@pytest.mark.asyncio
async def test_complete_snapshots_only_and_alert_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("SORSA_API_KEY", "test-sorsa")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:fake-token")
    pages = iter([
        {"users": [user("alpha", 1), user("beta", 2)], "next_cursor": None},
        {"users": [user("alpha", 1)], "next_cursor": "2"},
        {"users": [user("charlie", 3)], "next_cursor": None},
    ])
    messages = []

    def provider(request):
        if request.url.host == "api.telegram.org":
            messages.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        assert request.url.host == "api.sorsa.io"
        assert request.method == "GET"
        assert request.headers["ApiKey"] == "test-sorsa"
        if request.url.path.endswith("/info"):
            return httpx.Response(200, json=user("owner", 4, followers=2))
        assert request.url.path.endswith("/followers")
        return httpx.Response(200, json=next(pages))

    radar = Radar(tmp_path, KEY, httpx.MockTransport(provider))
    radar.store.put("setting", "options", {"telegram_enabled": True})
    radar.store.put("channel", "private", {"id": "private", "chat": "22", "silent": True})
    radar.store.put("rule", "follow", {
        "id": "follow", "name": "New follower", "enabled": True, "type": "follow",
        "account": "owner", "actor": "", "keyword": "", "channel": "private", "draft": False,
    })
    try:
        await radar.graph_page("owner", "followers", radar.budget())
        assert radar.store.list("event") == []
        pending = await radar.graph_page("owner", "followers", radar.budget())
        assert not pending["complete"]
        assert radar.store.list("event") == []
        assert [p["handle"] for p in radar.members("owner", "followers")["rows"]] == ["alpha", "beta"]
        await radar.graph_page("owner", "followers", radar.budget())
        assert {(e["type"], e["actor"]) for e in radar.store.list("event")} == {
            ("follow", "charlie"), ("unfollow", "beta"),
        }
        assert radar.members("owner", "followers", {"ff_min": 50})["rows"][0]["ff"] == 50
        assert radar.store.unread() == 1
        await radar.flush()
        await radar.flush()
        assert len(messages) == 1
        assert messages[0]["chat_id"] == "22" and messages[0]["disable_notification"] is True
    finally:
        await radar.api.client.aclose()


@pytest.mark.asyncio
async def test_reply_source_deduplication_and_draft_intent(tmp_path, monkeypatch):
    monkeypatch.setenv("SORSA_API_KEY", "test-sorsa")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-model")
    phase = 0
    seen = []

    def provider(request):
        nonlocal phase
        seen.append((request.url.host, request.url.path))
        payload = json.loads(request.content)
        if request.url.host == "openrouter.ai":
            assert payload["messages"][1]["content"].startswith("The first claim")
            return httpx.Response(200, json={"choices": [{"message": {
                "content": "The migration window matters here, and I'd watch what users need to do."
            }}]})
        if request.url.path.endswith("/tweet-info"):
            return httpx.Response(200, json=reply(payload["tweet_link"]))
        assert request.url.path.endswith("/search-tweets")
        assert payload["query"].startswith("to:owner filter:replies since:")
        phase += 1
        posts = [reply(12, created=now()-timedelta(minutes=1))]
        if phase > 1:
            posts += [reply(13), reply(14, parent="someone_else")]
        return httpx.Response(200, json={"tweets": posts, "next_cursor": None})

    radar = Radar(tmp_path, KEY, httpx.MockTransport(provider))
    radar.store.put("rule", "all-replies", {
        "id": "all-replies", "name": "Replies", "enabled": True, "type": "reply",
        "account": "owner", "actor": "friend", "keyword": "", "channel": "", "draft": False,
    })
    try:
        await radar.scan("incoming", "owner")
        assert radar.store.unread() == 0
        await radar.scan("incoming", "owner")
        await radar.scan("incoming", "owner")
        assert {e["id"] for e in radar.store.list("event")} == {"reply:12", "reply:13"}
        assert radar.store.unread() == 1
        result = await radar.draft("https://x.com/friend/status/13")
        query = parse_qs(urlsplit(result["intent"]).query)
        assert query == {"in_reply_to": ["13"], "text": [result["text"]]}
        assert ", and" not in result["text"] and not result["text"].endswith(".")
        assert set(host for host, _ in seen) == {"api.sorsa.io", "openrouter.ai"}
        count = len(seen)
        assert await radar.draft("13") == result
        assert len(seen) == count
    finally:
        await radar.api.client.aclose()


@pytest.mark.asyncio
async def test_following_collection_resumes_under_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("SORSA_API_KEY", "test-sorsa")
    paths = []

    def provider(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/info"):
            return httpx.Response(200, json=user("owner", 4, following=2))
        next_page = request.url.params.get("next_cursor")
        if next_page:
            assert next_page == "17"
            return httpx.Response(200, json={"users": [user("bravo", 3)]})
        return httpx.Response(200, json={"users": [user("alpha", 2)], "next_cursor": 17})

    radar = Radar(tmp_path, KEY, httpx.MockTransport(provider))
    radar.store.put("setting", "options", {"scan_limit": 1, "daily_limit": 3})
    try:
        for expected in (False, False, True):
            result = await radar.scan("following", "owner")
            assert result["requests"] == 1 and result["results"][0]["complete"] is expected
        assert paths == ["/v3/info", "/v3/follows", "/v3/follows"]
        assert radar.members("owner", "following")["complete"]
        with pytest.raises(RadarError, match="سقف درخواست"):
            await radar.scan("following", "owner")
        assert len(paths) == 3
    finally:
        await radar.api.client.aclose()


def test_backup_roundtrip_and_bad_password_is_atomic(tmp_path):
    source = Store(tmp_path / "source", KEY)
    source.set_secret("sorsa", "secret-example-sorsa-key")
    source.put("setting", "options", {"scheduled": True, "telegram_enabled": True})
    source.put("watch", "owner", {"handle": "owner"})
    encoded = source.backup("long backup passphrase", b"\0"*16)
    assert "secret-example-sorsa-key" not in json.dumps(encoded)
    target = Store(tmp_path / "target", KEY + "-different")
    target.put("watch", "current", {"handle": "current"})
    with pytest.raises(RadarError):
        target.restore("wrong passphrase", encoded)
    assert target.get("watch", "current")["handle"] == "current"
    target.restore("long backup passphrase", encoded)
    assert target.get("watch", "owner")["handle"] == "owner"
    assert target.secret("sorsa") == "secret-example-sorsa-key"
    assert target.get("setting", "options") == {"scheduled": False, "telegram_enabled": False}


def test_dashboard_login_and_history_export(tmp_path):
    radar = Radar(tmp_path, KEY, httpx.MockTransport(lambda request: httpx.Response(503)))
    parent = FastAPI()
    parent.mount("/x-radar", create_app(radar, KEY))
    with TestClient(parent, base_url="https://testserver") as client:
        assert client.get("/x-radar/").status_code == 200
        assert client.get("/x-radar/api/state").status_code == 401
        assert client.post("/x-radar/api/login", json={"password": KEY}).status_code == 403
        assert client.post("/x-radar/api/login", headers=HEADERS, json={"password": KEY}).status_code == 200
        assert client.post("/x-radar/api/watch", json={"username": "owner"}).status_code == 403
        assert client.post("/x-radar/api/watch", headers=HEADERS, json={"username": "owner"}).status_code == 200
        radar.store.set_secret("sorsa", "test-private-key")
        data = client.get("/x-radar/api/state")
        assert data.status_code == 200 and data.json()["keys"]["sorsa"]
        assert "test-private-key" not in data.text
        radar.store.put("event", "csv", {
            "id": "csv", "type": "reply", "account": "owner", "actor": "someone",
            "at": now().isoformat(), "text": "=1+1", "url": "https://x.com/someone/status/99",
        })
        output = client.get("/x-radar/api/history", params={"export": "true"})
        assert output.content.startswith(b"\xef\xbb\xbf") and b"'=1+1" in output.content
        assert client.post("/x-radar/api/bridge", json={"command": "alerts"}).status_code == 401


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


@pytest.mark.asyncio
async def test_smart_pagination_and_score_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("TWITTERSCORE_API_KEY", "test-twitter")

    def provider(request):
        assert request.url.host == "twitterscore.io"
        assert request.headers["X-API-Key"] == "test-twitter"
        assert request.url.path.endswith("/get_followers")
        page = int(request.url.params["page"])
        names = range(25) if page == 1 else range(25, 27)
        return httpx.Response(200, json={"top_followers": [
            {"username": f"smart_{i}", "twitter_score": 700+i, "name": f"Smart {i}",
             "followers_count": 42, "tags": []}
            for i in names
        ]})

    radar = Radar(tmp_path, KEY, httpx.MockTransport(provider))
    try:
        first = await radar.scan("smart", "owner")
        assert not first["results"][0]["complete"]
        assert len(radar.members("owner", "smart")["rows"]) == 25
        second = await radar.scan("smart", "owner")
        assert second["results"][0]["complete"]
        rows = radar.members("owner", "smart", {"twitter_min": 725, "smart": True})["rows"]
        assert {row["handle"] for row in rows} == {"smart_25", "smart_26"}
    finally:
        await radar.api.client.aclose()
