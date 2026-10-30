"""
Phase 5 - End-to-end check of the RUNNING API, exactly as the widget uses it.
Uses the production database (read-only) and the local LLM through the API.

What it does:
  1. GET /health and GET /health?deep=true (checks DB, LLM, Qdrant - one tiny DB query)
  2. Security checks: /chat without a key must give 401; a 1,001-character message must give 422
  3. Asks 4 questions as one conversation and prints the streamed events:
       "Hello"                                (no DB, no LLM)
       "Current total generation of NTPC"     (live data)
       "PLF of Singrauli yesterday"           (daily data)
       "And Korba?"                           (follow-up: same metric and day, from memory)
  4. Saves everything to outputs/api/smoke_<time>.json

Start the API first (in its own PowerShell window, from the project folder):
    .\\.venv\\Scripts\\python.exe -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
Then run:
    .\\.venv\\Scripts\\python.exe scripts\\api_smoke_test.py
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import settings  # noqa: E402

BASE = "http://127.0.0.1:8000"
HEADERS = {"X-API-Key": settings.api_key}
QUESTIONS = ["Hello", "Current total generation of NTPC", "PLF of Singrauli yesterday", "And Korba?"]


def read_events(response) -> list[tuple[str, dict]]:
    events, name, data = [], "message", []
    for line in response.iter_lines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
        elif line == "":
            if data:
                events.append((name, json.loads("\n".join(data))))
            name, data = "message", []
    return events


def main() -> None:
    log = {"time": datetime.now().isoformat(timespec="seconds"), "checks": {}, "conversation": []}
    with httpx.Client(base_url=BASE, timeout=900) as client:
        r = client.get("/health")
        log["checks"]["health"] = r.json()
        print("health:", r.status_code, r.json())
        r = client.get("/health", params={"deep": "true"}, headers=HEADERS)
        log["checks"]["deep_health"] = r.json()
        print("deep health:", r.status_code, json.dumps(r.json()))

        r = client.post("/chat", json={"message": "hi"})
        log["checks"]["no_key_status"] = r.status_code
        print("no key ->", r.status_code, "(expected 401)")
        r = client.post("/chat", json={"message": "x" * 1001}, headers=HEADERS)
        log["checks"]["too_long_status"] = r.status_code
        print("1,001 characters ->", r.status_code, "(expected 422)")

        session = None
        for q in QUESTIONS:
            start = time.perf_counter()
            with client.stream("POST", "/chat", json={"message": q, "session_id": session}, headers=HEADERS) as r:
                events = read_events(r)
            seconds = round(time.perf_counter() - start, 1)
            answer = next((d for n, d in events if n == "answer"), {})
            session = answer.get("session_id", session)
            steps = [d.get("step") for n, d in events if n == "status"]
            tokens = sum(1 for n, _ in events if n == "token")
            print(f"\nYou: {q}\n  status: {steps}  token events: {tokens}  total {seconds}s")
            print(f"Bot: {answer.get('text')}")
            print(f"  live={answer.get('is_live')} as_of={answer.get('as_of')} source={answer.get('source')} "
                  f"table={'yes' if answer.get('table') else 'no'} chart={'yes' if answer.get('chart') else 'no'}")
            log["conversation"].append({"question": q, "seconds": seconds, "steps": steps, "token_events": tokens,
                                        "answer": answer, "event_names": [n for n, _ in events]})

    out = ROOT / "outputs" / "api"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"smoke_{datetime.now():%Y%m%d_%H%M}.json"
    path.write_text(json.dumps(log, default=str, indent=1), encoding="utf-8")
    print(f"\nSaved {path}")


if __name__ == "__main__":
    main()
