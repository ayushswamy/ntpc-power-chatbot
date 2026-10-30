"""
Web API for the chatbot (FastAPI).

  POST /chat          ask a question; the answer comes back as a stream of Server-Sent Events
  GET  /health        is the API up? (no key needed)   ?deep=true also checks DB, LLM and Qdrant (key needed)
  GET  /widget/...    the chat widget files (ntpc-chat.js, Chart.js, demo page)

Every /chat request must carry the shared secret in the header  X-API-Key: <API_KEY from .env>
In production the ASP.NET server adds this header (see docs/EMBEDDING.md) so the key
never reaches users' browsers.

Stream of events for one question (each "data" is JSON):
  event: status   {"step": "waiting" | "started" | "planning" | "querying" | "writing answer"}
  event: token    {"text": "..."}           the answer text, piece by piece (already checked)
  event: answer   {text, table, chart, sources, as_of, is_live, source, session_id, options?, sql_debug?}
  event: error    {"message": "..."}
  event: done     {}
Voice or a mobile app can use exactly the same endpoint (send the recognised text as "message").

Start (from the project folder):
    .\\.venv\\Scripts\\python.exe -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
"""
import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.agent.graph import ask
from app.api.health import deep_health
from app.api.limits import RateLimiter
from app.config import PROJECT_ROOT, settings

log = logging.getLogger("ntpc.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """On start-up, load the LLM and embedding models in the background (WARMUP_ON_START),
    so the first user does not wait minutes for the model to load."""
    if settings.warmup_on_start:
        def warm():
            from app.agent.llm import warm_up
            log.warning("Model warm-up: %s", warm_up())
        asyncio.get_running_loop().run_in_executor(None, warm)
    yield


app = FastAPI(title="NTPC Power Data Assistant API", version="1.0", lifespan=lifespan,
              docs_url="/docs" if settings.debug else None, redoc_url=None, openapi_url="/openapi.json" if settings.debug else None)
app.add_middleware(CORSMiddleware, allow_origins=list(settings.allowed_origins), allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type", "X-API-Key"])
app.mount("/widget", StaticFiles(directory=PROJECT_ROOT / "widget"), name="widget")

session_limiter = RateLimiter(settings.rate_limit_per_minute)
client_limiter = RateLimiter(settings.rate_limit_per_client) if settings.rate_limit_per_client > 0 else None
_slots: asyncio.Semaphore | None = None


def slots() -> asyncio.Semaphore:
    """Created on first use, inside the running event loop."""
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(settings.max_concurrent_chats)
    return _slots


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000, description="The user's question")
    session_id: str | None = Field(default=None, max_length=64, description="From the previous answer; omit for a new chat")


def check_api_key(x_api_key: str | None) -> None:
    if not settings.api_key or settings.api_key == "change-me":
        raise HTTPException(503, "The chatbot API key is not configured on the server (API_KEY in .env).")
    if not x_api_key or not secrets.compare_digest(x_api_key.encode(), settings.api_key.encode()):
        raise HTTPException(401, "Missing or invalid API key.")


def _chunks(text: str, size: int = 24):
    """Split the answer into small pieces so the widget can show it 'typing'."""
    words, piece = text.split(" "), ""
    for word in words:
        piece += word + " "
        if len(piece) >= size:
            yield piece
            piece = ""
    if piece:
        yield piece


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    log.exception("Unexpected API error")       # full details only in the server log
    return JSONResponse(status_code=500, content={"detail": "Internal error. The problem has been logged."})


@app.get("/health")
async def health(deep: bool = False, x_api_key: str | None = Header(default=None)):
    if not deep:
        return {"status": "ok", "model": settings.llm_model}
    check_api_key(x_api_key)
    return await asyncio.to_thread(deep_health)


@app.post("/chat")
async def chat(req: ChatRequest, request: Request, x_api_key: str | None = Header(default=None),
               x_user: str | None = Header(default=None)):
    check_api_key(x_api_key)
    address = request.client.host if request.client else "unknown"
    # X-User is set by the ASP.NET forwarding handler (logged-in user name); trusted because the
    # request carried the API key. Used for the audit log only.
    client = f"{(x_user or '')[:100]}@{address}" if x_user else address
    for limiter, key in ((session_limiter, req.session_id or f"new:{client}"), (client_limiter, address)):
        if limiter is not None and not limiter.allow(key):
            wait = limiter.retry_after(key)
            raise HTTPException(429, f"Too many questions - please wait {wait} seconds.", headers={"Retry-After": str(wait)})

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def emit(kind: str, data: dict) -> None:          # called from the worker thread
        loop.call_soon_threadsafe(queue.put_nowait, (kind, data))

    async def work() -> None:
        if slots().locked():
            queue.put_nowait(("status", {"step": "waiting"}))
        async with slots():
            queue.put_nowait(("status", {"step": "started"}))
            try:
                result = await asyncio.to_thread(ask, req.message, req.session_id, emit, client)
                queue.put_nowait(("answer", result))
            except Exception:
                log.exception("chat failed")
                queue.put_nowait(("error", {"message": "Sorry, something went wrong while answering."}))
            finally:
                queue.put_nowait(("end", None))

    asyncio.create_task(work())   # keeps running if the browser disconnects, so the answer is still saved

    async def events():
        while True:
            kind, data = await queue.get()
            if kind == "end":
                break
            if kind == "answer":
                for piece in _chunks(data.get("text") or ""):
                    yield {"event": "token", "data": json.dumps({"text": piece})}
                    await asyncio.sleep(0.015)
            yield {"event": kind, "data": json.dumps(data, default=str)}
        yield {"event": "done", "data": "{}"}

    return EventSourceResponse(events(), ping=15)
