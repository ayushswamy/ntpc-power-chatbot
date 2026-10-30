"""
Append-only JSON-lines log files in LOG_DIR (queries.jsonl, audit.jsonl).

When a file reaches LOG_MAX_MB it is renamed to <name>.<date-time>.jsonl and a new
file is started, so logs never fill the disk silently. Old files can be archived or
deleted by the backup script (see DEPLOYMENT.md).
"""
import json
import threading
from datetime import datetime

from app.config import PROJECT_ROOT, settings

_lock = threading.Lock()


def append_jsonl(name: str, record: dict) -> None:
    folder = PROJECT_ROOT / settings.log_dir
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    line = json.dumps(record, default=str) + "\n"
    with _lock:
        if path.exists() and path.stat().st_size > settings.log_max_mb * 1024 * 1024:
            path.rename(folder / f"{name}.{datetime.now():%Y%m%d_%H%M%S}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
