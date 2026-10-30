"""explain_term: definitions from semantic/glossary.yaml (fuzzy match, then Qdrant search). No database."""
import yaml
from rapidfuzz import fuzz, process

from app.config import PROJECT_ROOT
from app.rag.store import search
from app.tools.common import ToolResult

_terms: dict | None = None


def _load() -> dict:
    global _terms
    if _terms is None:
        _terms = yaml.safe_load(open(PROJECT_ROOT / "semantic" / "glossary.yaml", encoding="utf-8"))["terms"]
    return _terms


def explain_term(term: str) -> ToolResult:
    terms = _load()
    keys = {}
    for key, g in terms.items():
        keys[key.lower()] = key
        if g.get("full"):
            keys[str(g["full"]).lower()] = key
    match = process.extractOne(str(term).lower(), list(keys), scorer=fuzz.WRatio, score_cutoff=85)
    key = keys[match[0]] if match else None
    if key is None:
        hits = search(str(term), "glossary", k=1)
        if hits and hits[0]["_score"] >= 0.55:
            key = hits[0]["term"]
    if key is None:
        return ToolResult("explain_term", ok=False, error=f"I don't have a definition for '{term}' yet.")
    g = terms[key]
    return ToolResult("explain_term", answer={"term": key, "full": g.get("full"), "definition": g["definition"]},
                      sources=["glossary"])
