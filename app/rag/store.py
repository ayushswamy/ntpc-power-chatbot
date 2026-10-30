"""
Qdrant vector store: similarity search over
  type = example   example questions with their tool call (few-shot for the planner / SQL writer)
  type = glossary  term definitions
  type = schema    table / metric descriptions

If Qdrant or the embedding model is down, search() returns [] and the chatbot keeps
working without examples (a bit less accurate) - it never fails because of RAG.
"""
import logging
import uuid

from qdrant_client import QdrantClient, models

from app.agent.llm import embed
from app.config import settings

log = logging.getLogger(__name__)
_client: QdrantClient | None = None


def client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=settings.qdrant_url, timeout=5)
    return _client


def point_id(kind: str, key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ntpc-chatbot/{kind}/{key}"))


def recreate_collection(dim: int) -> None:
    c = client()
    if c.collection_exists(settings.qdrant_collection):
        c.delete_collection(settings.qdrant_collection)
    c.create_collection(settings.qdrant_collection,
                        vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE))
    c.create_payload_index(settings.qdrant_collection, "type", models.PayloadSchemaType.KEYWORD)


def upsert(items: list[tuple[str, list[float], dict]]) -> None:
    """items: (id, vector, payload)"""
    client().upsert(settings.qdrant_collection,
                    points=[models.PointStruct(id=i, vector=v, payload=p) for i, v, p in items])


def search(text: str, kind: str, k: int = 3, where: dict | None = None) -> list[dict]:
    """Top-k payloads of the given type most similar to `text` (empty list if Qdrant is unavailable)."""
    try:
        vector = embed([text])[0]
        conditions = [models.FieldCondition(key="type", match=models.MatchValue(value=kind))]
        for key, value in (where or {}).items():
            conditions.append(models.FieldCondition(key=key, match=models.MatchValue(value=value)))
        hits = client().query_points(settings.qdrant_collection, query=vector, limit=k,
                                     query_filter=models.Filter(must=conditions), with_payload=True).points
        return [{**h.payload, "_score": round(h.score, 3)} for h in hits]
    except Exception as exc:   # Qdrant down, collection missing, embedding model down ...
        log.warning("RAG search unavailable: %s", exc)
        return []
