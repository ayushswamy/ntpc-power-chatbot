"""
Central settings for the whole project.

Reads the .env file in the project root once. Every other module does
`from app.config import settings` instead of reading .env itself.
Secrets (DB password, API key) are stored here but must never be printed or logged.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUTS_DIR = PROJECT_ROOT / "outputs"

load_dotenv(PROJECT_ROOT / ".env")


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    return int(_str(name, str(default)))


def _bool(name: str, default: bool = False) -> bool:
    return _str(name, str(default)).lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class Settings:
    # SQL Server
    db_server: str = _str("DB_SERVER")
    db_name: str = _str("DB_NAME")
    db_user: str = _str("DB_USER")
    db_password: str = field(default=_str("DB_PASSWORD"), repr=False)  # repr=False: never shown when printed
    db_driver: str = _str("DB_DRIVER", "ODBC Driver 17 for SQL Server")
    db_login_timeout: int = _int("DB_LOGIN_TIMEOUT_SECONDS", 15)
    query_timeout: int = _int("QUERY_TIMEOUT_SECONDS", 30)
    max_rows: int = _int("MAX_ROWS", 5000)

    # Live data
    live_cache_ttl: int = min(_int("LIVE_CACHE_TTL_SECONDS", 60), 60)  # hard cap: 60 s
    live_freshness_minutes: int = _int("LIVE_FRESHNESS_MINUTES", 30)

    # What "NTPC total" means: group = NTPC + JV/subsidiaries, standalone = NTPC-owned plants only
    ntpc_total_scope: str = _str("NTPC_TOTAL_SCOPE", "group")
    log_dir: str = _str("LOG_DIR", "logs")

    # LLM
    llm_base_url: str = _str("LLM_BASE_URL", "http://localhost:11434/v1")
    llm_api_key: str = field(default=_str("LLM_API_KEY", "ollama"), repr=False)
    llm_model: str = _str("LLM_MODEL", "qwen3:8b")
    llm_timeout: int = _int("LLM_TIMEOUT_SECONDS", 300)
    # Qwen3 "thinking" makes answers ~4x slower on CPU and is not needed for tool selection
    llm_disable_thinking: bool = _bool("LLM_DISABLE_THINKING", True)
    # ollama or vllm - only changes HOW thinking is switched off (the API is the same OpenAI format)
    llm_provider: str = _str("LLM_PROVIDER", "ollama").lower()
    # load the model when the API starts, so the first user does not wait for it
    warmup_on_start: bool = _bool("WARMUP_ON_START", True)
    # log files are renamed to *.1.jsonl when they reach this size (MB)
    log_max_mb: int = _int("LOG_MAX_MB", 50)
    # llm = the LLM writes the answer text (numbers are checked); template = fixed wording, no LLM call
    answer_mode: str = _str("ANSWER_MODE", "llm")

    # Embeddings / vector DB
    embed_base_url: str = _str("EMBED_BASE_URL", "http://localhost:11434/v1")
    embed_model: str = _str("EMBED_MODEL", "bge-m3")
    qdrant_url: str = _str("QDRANT_URL", "http://localhost:6333")
    qdrant_collection: str = _str("QDRANT_COLLECTION", "ntpc_chatbot")

    # API
    api_key: str = field(default=_str("API_KEY"), repr=False)
    rate_limit_per_minute: int = _int("RATE_LIMIT_PER_MINUTE", 20)
    # limit per client address; 0 = off (behind the ASP.NET forwarding handler every request has the
    # web server's address, so only the per-session limit is meaningful there)
    rate_limit_per_client: int = _int("RATE_LIMIT_PER_CLIENT_MINUTE", 60)
    # websites (origins) whose pages may call the API directly from the browser, comma-separated
    allowed_origins: tuple = tuple(o.strip() for o in _str("ALLOWED_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",") if o.strip())
    # how many questions the LLM works on at the same time (others wait in a queue); 1 on a CPU-only PC
    max_concurrent_chats: int = _int("MAX_CONCURRENT_CHATS", 1)
    sqlite_path: str = _str("SQLITE_PATH", "data/chatbot.sqlite")
    debug: bool = _bool("DEBUG", False)

    def missing_db_settings(self) -> list[str]:
        """Names of required DB settings that are empty in .env."""
        required = {
            "DB_SERVER": self.db_server,
            "DB_NAME": self.db_name,
            "DB_USER": self.db_user,
            "DB_PASSWORD": self.db_password,
            "DB_DRIVER": self.db_driver,
        }
        return [name for name, value in required.items() if not value]


settings = Settings()
