"""
Phase 3 - Build the Qdrant collection from the YAML files. Does NOT touch SQL Server.

Indexes (each point has payload "type"):
  example   - examples/examples.yaml questions + conversation turns (tool, args, sql)
  glossary  - semantic/glossary.yaml terms
  schema    - semantic/tables.yaml tables and semantic/metrics.yaml metrics

Needs: Qdrant running (scripts\\start_qdrant.ps1) and Ollama with the embedding model (bge-m3).
Re-run whenever you change examples, glossary or table descriptions:
    .\\.venv\\Scripts\\python.exe scripts\\index_qdrant.py
"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.llm import embed  # noqa: E402
from app.rag.store import point_id, recreate_collection, upsert  # noqa: E402


def load(path: str) -> dict:
    return yaml.safe_load(open(ROOT / path, encoding="utf-8"))


def masked(question: str, args: dict) -> str:
    """'Current generation of Korba' -> 'current generation of PLANT' (the search side does the same),
    so similar-looking questions about DIFFERENT plants match on the kind of question."""
    import re
    text = question
    if args.get("plant"):
        text = re.sub(re.escape(str(args["plant"])), "PLANT", text, flags=re.IGNORECASE)
    return text


def build_items() -> list[tuple[str, str, dict]]:
    """(id, text to embed, payload)"""
    items = []
    ex = load("examples/examples.yaml")
    for e in ex["examples"]:
        payload = {"type": "example", "id": e["id"], "question": e["question"], "tool": e["tool"], "args": e["args"],
                   "source": e["source"]}
        if e.get("sql"):
            payload["sql"] = e["sql"]
        items.append((point_id("example", e["id"]), masked(e["question"], e["args"]), payload))
    for conv in ex.get("conversations", []):
        previous = None
        for i, t in enumerate(conv["turns"], 1):
            key = f"{conv['id']}-{i}"
            payload = {"type": "example", "id": key, "question": t["question"], "tool": t["tool"], "args": t["args"],
                       "source": t["source"], "follow_up_of": previous}
            items.append((point_id("example", key), masked(t["question"], t["args"]), payload))
            previous = t["question"]

    for term, g in load("semantic/glossary.yaml")["terms"].items():
        text = f"{term}: {g.get('full', '')} - {g['definition']}"
        items.append((point_id("glossary", term), text, {"type": "glossary", "term": term, "full": g.get("full"),
                                                          "definition": g["definition"]}))

    tables = load("semantic/tables.yaml")
    for name, t in tables["tables"].items():
        text = f"{name}: {t.get('purpose', '')} {t.get('notes', '') if isinstance(t.get('notes'), str) else ''}"
        items.append((point_id("schema", name), text[:1500], {"type": "schema", "kind": "table", "name": name,
                                                              "text2sql": bool(t.get("text2sql")), "purpose": t.get("purpose")}))
    for key, m in load("semantic/metrics.yaml")["metrics"].items():
        text = f"{m['name']} ({key}): synonyms {', '.join(m.get('synonyms', []))}. Unit {m.get('unit')}."
        items.append((point_id("schema", f"metric-{key}"), text, {"type": "schema", "kind": "metric", "name": key,
                                                                  "label": m["name"]}))
    return items


def main() -> None:
    items = build_items()
    print(f"Embedding {len(items)} items with the local embedding model...")
    vectors = []
    for i in range(0, len(items), 16):
        vectors += embed([text for _, text, _ in items[i:i + 16]])
        print(f"  {min(i + 16, len(items))}/{len(items)}")
    recreate_collection(len(vectors[0]))
    upsert([(pid, vec, payload) for (pid, _, payload), vec in zip(items, vectors)])
    counts = {}
    for _, _, p in items:
        counts[p["type"]] = counts.get(p["type"], 0) + 1
    print(f"Indexed: {counts}")


if __name__ == "__main__":
    main()
