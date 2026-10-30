"""
API tests (FastAPI TestClient) - no database and no LLM: the chatbot function `ask`
is replaced by a fake that sends two progress events and returns a fixed answer.
"""
import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

from app.api import main
from app.api.limits import RateLimiter

KEY = "test-key-123"
ANSWER = {"text": "Singrauli is generating 1,046 MW (live, as of 10:51).", "table": None, "chart": None,
          "sources": ["XML_RealView"], "as_of": "2026-09-28 10:51", "is_live": True, "source": "live",
          "tool": "get_live", "session_id": "abc123", "timings": {"total": 1.0}}


def fake_ask(message, session_id=None, emit=None, client=None):
    emit("status", {"step": "planning"})
    emit("status", {"step": "querying"})
    return dict(ANSWER)


@pytest.fixture
def client(monkeypatch):
    import sse_starlette.sse
    if hasattr(sse_starlette.sse, "AppStatus"):            # reset between tests (one event loop per TestClient)
        sse_starlette.sse.AppStatus.should_exit_event = None
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, api_key=KEY, warmup_on_start=False))
    monkeypatch.setattr(main, "ask", fake_ask)
    monkeypatch.setattr(main, "session_limiter", RateLimiter(20))
    monkeypatch.setattr(main, "client_limiter", RateLimiter(60))
    monkeypatch.setattr(main, "_slots", None)
    with TestClient(main.app) as c:
        yield c


def parse_events(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name, data = "message", []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if data:
            events.append((name, json.loads("\n".join(data))))
    return events


def test_health_needs_no_key(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_deep_health_needs_key(client, monkeypatch):
    assert client.get("/health?deep=true").status_code == 401
    monkeypatch.setattr(main, "deep_health", lambda: {"status": "ok", "checks": {}})
    r = client.get("/health?deep=true", headers={"X-API-Key": KEY})
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_chat_rejects_missing_or_wrong_key(client):
    assert client.post("/chat", json={"message": "hi"}).status_code == 401
    assert client.post("/chat", json={"message": "hi"}, headers={"X-API-Key": "wrong"}).status_code == 401


def test_chat_refuses_when_server_key_not_configured(client, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, api_key="change-me"))
    r = client.post("/chat", json={"message": "hi"}, headers={"X-API-Key": "change-me"})
    assert r.status_code == 503


def test_chat_streams_status_tokens_answer_done(client):
    with client.stream("POST", "/chat", json={"message": "current generation of singrauli"},
                       headers={"X-API-Key": KEY}) as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    events = parse_events(body)
    names = [n for n, _ in events]
    assert names[0] == "status" and "planning" in [d.get("step") for n, d in events if n == "status"]
    assert names[-2:] == ["answer", "done"]
    typed = "".join(d["text"] for n, d in events if n == "token")
    assert typed.strip() == ANSWER["text"]                       # the typed text is exactly the checked answer
    answer = dict(events)["answer"]
    assert answer["is_live"] and answer["session_id"] == "abc123"


def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(main, "session_limiter", RateLimiter(2))
    for _ in range(2):
        with client.stream("POST", "/chat", json={"message": "q", "session_id": "s1"}, headers={"X-API-Key": KEY}) as r:
            "".join(r.iter_text())
    r = client.post("/chat", json={"message": "q", "session_id": "s1"}, headers={"X-API-Key": KEY})
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_message_length_limit(client):
    r = client.post("/chat", json={"message": "x" * 1001}, headers={"X-API-Key": KEY})
    assert r.status_code == 422
    assert client.post("/chat", json={"message": ""}, headers={"X-API-Key": KEY}).status_code == 422


def test_widget_files_are_served(client):
    js = client.get("/widget/ntpc-chat.js")
    assert js.status_code == 200 and "attachShadow" in js.text
    assert client.get("/widget/vendor/chart.umd.min.js").status_code == 200
    assert client.get("/widget/demo.html").status_code == 200
