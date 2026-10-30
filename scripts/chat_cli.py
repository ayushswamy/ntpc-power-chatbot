"""
Chat with the NTPC data assistant in the terminal.

Needs: Ollama running (qwen3:8b + bge-m3) and Qdrant running (scripts\\start_qdrant.ps1).
Questions query the PRODUCTION database (read-only, through the safe runner).

Run from the project folder:
    .\\.venv\\Scripts\\python.exe scripts\\chat_cli.py

Commands:  /new  = start a new conversation     /debug = show tool, arguments, SQL and timings
           /quit = exit
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.graph import ask  # noqa: E402
from app.memory.store import get_store  # noqa: E402


def show_table(table: dict, max_rows: int = 15) -> None:
    cols = table["columns"]
    rows = [[("" if v is None else str(v)) for v in r] for r in table["rows"][:max_rows]]
    widths = [min(max(len(c), *(len(r[i]) for r in rows)) if rows else len(c), 40) for i, c in enumerate(cols)]
    print("  " + " | ".join(c[:w].ljust(w) for c, w in zip(cols, widths)))
    print("  " + "-+-".join("-" * w for w in widths))
    for r in rows:
        print("  " + " | ".join(v[:w].ljust(w) for v, w in zip(r, widths)))
    if len(table["rows"]) > max_rows:
        print(f"  ... {len(table['rows']) - max_rows} more rows")


def main() -> None:
    session = get_store().new_session("cli")
    debug = False
    print("NTPC Power Data Assistant (type /quit to exit, /new for a new conversation, /debug to toggle details)")
    while True:
        try:
            question = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not question:
            continue
        if question == "/quit":
            break
        if question == "/new":
            session = get_store().new_session("cli")
            print("(new conversation)")
            continue
        if question == "/debug":
            debug = not debug
            print(f"(debug {'on' if debug else 'off'})")
            continue

        print("  ...thinking", end="", flush=True)
        r = ask(question, session, emit=lambda kind, data: print(f" .{data.get('step', '')}", end="", flush=True))
        print()
        badge = f"[Live - as of {r['as_of'][-5:]}] " if r.get("is_live") and r.get("as_of") else ""
        print(f"Bot: {badge}{r['text']}")
        if r.get("table"):
            show_table(r["table"])
        if r.get("chart"):
            print(f"  (chart: {r['chart']['type']} - {r['chart']['title']})")
        if r.get("sources"):
            print(f"  sources: {', '.join(r['sources'])}")
        if debug:
            print(f"  tool: {r.get('tool')}  source: {r.get('source')}  timings: {r.get('timings')}")
            for key in ("debug", "sql_debug"):
                if r.get(key):
                    print(f"  {key}: {r[key]}")


if __name__ == "__main__":
    main()
