"""Exercise the Turso HTTP path against SQLite with mocked provider traffic."""
import json
import sqlite3
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.agent_server.x_radar import web
from scripts.agent_server.x_radar.radar import Radar, now
from scripts.agent_server.x_radar.store import RadarError, Store


KEY = "example-secret-for-remote-radar-tests"
URL = "libsql://agant-sample.turso.io"
TOKEN = "mock-db-token"
HEADERS = {"X-Radar-Request": "1"}


class DatabaseEndpoint:
    """Apply the pipeline in a separate SQLite connection for each HTTP request."""

    def __init__(self, path):
        self.path = path
        self.fail_checkpoint = False

    def __call__(self, request):
        assert request.url == "https://agant-sample.turso.io/v2/pipeline"
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        commands = json.loads(request.content)["requests"]
        assert commands[-1] == {"type": "close"}
        with sqlite3.connect(self.path, isolation_level=None) as db:
            results = []
            for command in commands[:-1]:
                if command["type"] == "execute":
                    value = self.execute(db, command["stmt"])
                    results.append({"type": "ok", "response": {"type": "execute", "result": value}})
                    continue
                assert command["type"] == "batch"
                values, errors = [], []
                for step in command["batch"]["steps"]:
                    condition = step.get("condition")
                    if condition:
                        assert condition == {"type": "ok", "step": len(values) - 1}
                        if values[-1] is None:
                            values.append(None)
                            errors.append(None)
                            continue
                    try:
                        values.append(self.execute(db, step["stmt"]))
                        errors.append(None)
                    except sqlite3.Error:
                        values.append(None)
                        errors.append({"message": "fixture rejected statement"})
                results.append({"type": "ok", "response": {
                    "type": "batch", "result": {"step_results": values, "step_errors": errors},
                }})
            if db.in_transaction:
                db.rollback()
        return httpx.Response(200, json={
            "baton": None, "base_url": None,
            "results": results + [{"type": "ok", "response": {"type": "close"}}],
        })

    def execute(self, db, stmt):
        params = [
            int(arg["value"]) if arg["type"] == "integer" else arg["value"]
            for arg in stmt["args"]
        ]
        if self.fail_checkpoint and params[:1] == ["feed"]:
            raise sqlite3.OperationalError("checkpoint failed")
        cursor = db.execute(stmt["sql"], params)
        rows = [[{"type": "text", "value": item} if item is not None else {"type": "null"}
                 for item in row] for row in cursor.fetchall()]
        return {"rows": rows, "affected_row_count": max(0, cursor.rowcount)}


def test_remote_backend_mounts_without_a_volume_and_survives_restart(tmp_path, monkeypatch):
    from scripts.agent_server.x_radar.turso import TursoDatabase

    endpoint = DatabaseEndpoint(tmp_path / "remote.sqlite")
    monkeypatch.setenv("AGANT_X_RADAR_KEY", KEY)
    monkeypatch.setenv("AGANT_X_RADAR_TURSO_URL", URL)
    monkeypatch.setenv("AGANT_X_RADAR_TURSO_TOKEN", TOKEN)
    monkeypatch.delenv("AGANT_X_RADAR_DATA_DIR", raising=False)
    monkeypatch.setattr(web, "TursoDatabase", lambda url, token: TursoDatabase(
        url, token, transport=httpx.MockTransport(endpoint),
    ))

    def provider(request):
        assert request.url == "https://api.sorsa.io/v3/search-tweets"
        query = json.loads(request.content)["query"]
        comment = query.startswith("to:owner")
        return httpx.Response(200, json={"tweets": [{
            "id": "10" if comment else "11",
            "user": {"username": "friend" if comment else "alpha"},
            "created_at": (now() - timedelta(seconds=5)).isoformat(),
            "full_text": "Good morning",
            "in_reply_to_username": "owner" if comment else None,
            "in_reply_to_tweet_id": "123" if comment else None,
        }]})

    monkeypatch.setattr(web, "Radar", lambda directory, key, **options: Radar(
        directory, key, httpx.MockTransport(provider), **options,
    ))
    for attempt in range(2):
        app = FastAPI()
        radar = web.install(app)
        assert radar is not None
        try:
            with TestClient(app, base_url="https://testserver") as client:
                assert client.post("/x-radar/api/login", headers=HEADERS, json={"password": KEY}).status_code == 200
                if attempt == 0:
                    assert client.post("/x-radar/api/options", headers=HEADERS, json={
                        "owner": "owner", "smart_handles": "@alpha, @beta",
                    }).status_code == 200
                    radar.store.set_secret("sorsa", "mock-sorsa-key")
                    scan = client.post("/x-radar/api/scan", headers=HEADERS, json={})
                    assert scan.status_code == 200 and scan.json()["new"] == 2
                    client.portal.call(radar.process_pending)
                    radar.store.reserve("2000-01-01", 1)
                assert client.get("/x-radar/api/state").json()["options"]["smart_handles"] == ["alpha", "beta"]
                rows = client.get("/x-radar/api/inbox").json()["rows"]
                assert len(rows) == 2 and all("in_reply_to=" in row["draft"]["intent"] for row in rows)
                assert radar.store.get("usage", "2000-01-01") == 1
                with pytest.raises(RadarError, match="سقف درخواست"):
                    radar.store.reserve("2000-01-01", 1)
        finally:
            radar.store.close()


def test_failed_checkpoint_rolls_back_remote_page(tmp_path):
    from scripts.agent_server.x_radar.turso import TursoDatabase

    endpoint = DatabaseEndpoint(tmp_path / "remote.sqlite")
    database = TursoDatabase(URL, TOKEN, transport=httpx.MockTransport(endpoint))
    store = Store(None, KEY, remote=database)
    item = {"post": {"id": "10"}, "published_at": 100, "draft_state": "pending", "telegram": "off"}
    try:
        endpoint.fail_checkpoint = True
        with pytest.raises(RadarError):
            store.record_page("comments", [item], {"done": 100})
        assert store.inbox()["rows"] == []
        endpoint.fail_checkpoint = False
        assert store.record_page("comments", [item], {"done": 100}) == 1
        assert store.record_page("comments", [item], {"done": 100}) == 0
        assert store.get("feed", "comments") == {"done": 100}
        assert len(store.inbox()["rows"]) == 1
    finally:
        store.close()
