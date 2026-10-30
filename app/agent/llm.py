"""
Talks to the LLM and the embedding model through the OpenAI-compatible API.

Works with Ollama now (http://localhost:11434/v1) and vLLM later - only .env changes
(LLM_BASE_URL, LLM_MODEL, EMBED_MODEL). Nothing here is Ollama-specific except that
reasoning_effort="none" switches off Qwen3's slow "thinking" mode.
"""
import re
import time

from openai import OpenAI

from app.config import settings

_client: OpenAI | None = None
_embed_client: OpenAI | None = None


class LLMError(Exception):
    pass


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key or "none",
                         timeout=settings.llm_timeout, max_retries=0)
    return _client


def _strip_thinking(text: str | None) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()


def chat(messages: list[dict], tools: list[dict] | None = None, tool_choice: str | None = None,
         max_tokens: int = 400, temperature: float = 0.0) -> dict:
    """
    One chat completion. Returns {"content": str, "tool_calls": [(name, arguments_json)], "seconds": float,
    "prompt_tokens": int, "cached_tokens": int, "completion_tokens": int}.
    """
    extra = {}
    if settings.llm_disable_thinking:
        if settings.llm_provider == "vllm":
            # vLLM: Qwen3's chat template switch
            extra = {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
        else:
            # Ollama: OpenAI-style reasoning_effort
            extra = {"reasoning_effort": "none"}
    kwargs = dict(model=settings.llm_model, messages=messages, temperature=temperature, max_tokens=max_tokens, **extra)
    if tools:
        kwargs.update(tools=tools, tool_choice=tool_choice or "required")
    start = time.perf_counter()
    try:
        resp = client().chat.completions.create(**kwargs)
    except Exception as exc:  # connection refused, timeout, model not found ...
        raise LLMError(f"The language model is not reachable ({type(exc).__name__}).") from exc
    msg = resp.choices[0].message
    usage = resp.usage
    details = getattr(usage, "prompt_tokens_details", None)
    return {
        "content": _strip_thinking(msg.content),
        "tool_calls": [(c.function.name, c.function.arguments) for c in (msg.tool_calls or [])],
        "seconds": round(time.perf_counter() - start, 1),
        "prompt_tokens": getattr(usage, "prompt_tokens", 0),
        "cached_tokens": getattr(details, "cached_tokens", 0) if details else 0,
        "completion_tokens": getattr(usage, "completion_tokens", 0),
    }


def warm_up() -> dict:
    """Make the LLM and embedding servers load their models (the first load can take minutes on CPU)."""
    result = {}
    try:
        result["llm_seconds"] = chat([{"role": "user", "content": "Reply with OK."}], max_tokens=5)["seconds"]
    except LLMError as exc:
        result["llm_error"] = str(exc)
    try:
        start = time.perf_counter()
        embed(["warm up"])
        result["embed_seconds"] = round(time.perf_counter() - start, 1)
    except LLMError as exc:
        result["embed_error"] = str(exc)
    return result


def embed(texts: list[str]) -> list[list[float]]:
    """Embedding vectors (bge-m3: 1024 numbers per text)."""
    global _embed_client
    if _embed_client is None:
        _embed_client = OpenAI(base_url=settings.embed_base_url, api_key=settings.llm_api_key or "none",
                               timeout=120, max_retries=0)
    try:
        resp = _embed_client.embeddings.create(model=settings.embed_model, input=texts)
    except Exception as exc:
        raise LLMError(f"The embedding model is not reachable ({type(exc).__name__}).") from exc
    return [d.embedding for d in resp.data]
