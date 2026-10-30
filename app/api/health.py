"""
Health checks for GET /health?deep=true: database, LLM, embedding model, Qdrant.
Each check is small and has a short timeout. Results are cached for 30 seconds so a
monitoring tool polling /health cannot add load to the production database.
"""
import time

import httpx

from app.config import settings
from app.db.cache import TTLCache
from app.db.runner import get_runner

_cache = TTLCache(30)


def _timed(fn) -> dict:
    start = time.perf_counter()
    try:
        detail = fn()
        return {"ok": True, "ms": round((time.perf_counter() - start) * 1000), **(detail or {})}
    except Exception as exc:   # report which part is down, without internal details
        return {"ok": False, "ms": round((time.perf_counter() - start) * 1000), "error": type(exc).__name__}


def _database():
    rows = get_runner().run("SELECT TOP 1 BlockNo FROM dbo.Master_Blocks", purpose="health check").rows
    return {"rows": len(rows)}


def _llm():
    base = settings.llm_base_url.rstrip("/")
    r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {settings.llm_api_key}"}, timeout=5)
    r.raise_for_status()
    names = [m.get("id") for m in r.json().get("data", [])]
    return {"model": settings.llm_model, "model_available": settings.llm_model in names or f"{settings.llm_model}:latest" in names}


def _qdrant():
    r = httpx.get(f"{settings.qdrant_url.rstrip('/')}/healthz", timeout=3)
    r.raise_for_status()
    return {}


def deep_health() -> dict:
    def load():
        checks = {"database": _timed(_database), "llm": _timed(_llm), "vector_db": _timed(_qdrant)}
        # the chatbot works without Qdrant (fewer examples), so only DB and LLM decide "ok"
        status = "ok" if checks["database"]["ok"] and checks["llm"]["ok"] else "degraded"
        if checks["llm"].get("ok") and not checks["llm"].get("model_available"):
            status = "degraded"
        return {"status": status, "checks": checks}
    return _cache.get_or_load("deep", load)
