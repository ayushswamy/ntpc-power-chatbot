"""
Phase 3 - End-to-end check of the full chatbot on a few questions (uses the LLM, Qdrant
and the PRODUCTION database - read-only, through the safe runner).

Runs the questions below as ONE conversation (so follow-ups are tested), prints each
answer and saves a transcript with tool, arguments, source, SQL and timings to
outputs/agent/smoke_<time>.json

Run from the project folder (about 1-2 minutes per question on this CPU-only PC):
    .\\.venv\\Scripts\\python.exe scripts\\agent_smoke_test.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.config  # noqa: E402

# show SQL and plan details in the transcript
object.__setattr__(app.config.settings, "debug", True)

from app.agent.graph import ask  # noqa: E402
from app.memory.store import get_store  # noqa: E402

QUESTIONS = [
    "Hello",
    "What is the current generation of Singrauli?",
    "How much has it generated today so far?",
    "Compare with yesterday",
    "What was the PLF of Rihand last month?",
    "And Korba?",
    "Top 5 coal plants by PLF this month",
    "Current total generation of NTPC",
    "Generation of Dadri yesterday",
    "The coal one",
    "What is PLF?",
    "How many supercritical stages does NTPC have?",
]


def main() -> None:
    session = get_store().new_session("smoke-test")
    out_dir = ROOT / "outputs" / "agent"
    out_dir.mkdir(parents=True, exist_ok=True)
    transcript = []
    for q in QUESTIONS:
        print(f"\nYou: {q}", flush=True)
        r = ask(q, session)
        print(f"Bot: {r['text']}", flush=True)
        print(f"     [tool={r.get('tool')} source={r.get('source')} live={r.get('is_live')} as_of={r.get('as_of')} "
              f"timings={r.get('timings')}]", flush=True)
        transcript.append({"question": q, **r})
    path = out_dir / f"smoke_{datetime.now():%Y%m%d_%H%M}.json"
    path.write_text(json.dumps(transcript, default=str, indent=1), encoding="utf-8")
    print(f"\nTranscript saved to {path}")


if __name__ == "__main__":
    main()
