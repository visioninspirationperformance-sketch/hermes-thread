"""Thread ledger — an append-only JSON-lines store under ``$HERMES_HOME/thread/``.

Two record kinds share one file:

- ``{"type": "closure", ...}`` — written automatically at session end.
- ``{"type": "owed", ...}``     — written by the ``thread_log`` tool when the
  agent or operator records an outstanding item ("what's next").

Append-only means history is never lost and reads are trivial. Writes use a
single ``O_APPEND`` write syscall (atomic for these small lines) so concurrent
sessions on one profile cannot interleave a half-written record.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, List, Optional


def home() -> Path:
    """Resolve the real Hermes home (per profile) — never hardcode ~/.hermes."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    return Path.home() / ".hermes"


def dir_path() -> Path:
    p = home() / "thread"
    p.mkdir(parents=True, exist_ok=True)
    return p


def ledger_path() -> Path:
    return dir_path() / "ledger.jsonl"


def _append(record: dict) -> None:
    record.setdefault("ts", time.time())
    line = json.dumps(record, ensure_ascii=False) + "\n"
    fd = os.open(ledger_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def append_closure(session_id: str, status: str, model: str, platform: str,
                   files: list, tools: list, user: str, assistant: str) -> None:
    _append({
        "type": "closure",
        "session_id": session_id,
        "status": status,
        "model": model,
        "platform": platform,
        "files": files,
        "tools": tools,
        "user": user,
        "assistant": assistant,
    })


def append_owed(session_id: str, text: str, kind: str = "todo") -> None:
    _append({
        "type": "owed",
        "session_id": session_id or "unknown",
        "kind": kind,
        "text": text,
        "status": "open",
    })


def read_ledger(limit: int = 200) -> List[dict]:
    """Return the tail of the ledger as parsed records (corrupt lines skipped)."""
    path = ledger_path()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    out: List[dict] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def open_owed() -> List[dict]:
    """Outstanding items (``type == owed`` and ``status == open``), oldest first."""
    return [r for r in read_ledger() if r.get("type") == "owed" and r.get("status") == "open"]


def latest_closure() -> Optional[dict]:
    for r in reversed(read_ledger()):
        if r.get("type") == "closure":
            return r
    return None


def _fmt_ts(ts: float) -> str:
    try:
        import datetime
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


def resume_block(max_chars: int = 1600) -> Optional[str]:
    """Build the compact resume text injected on the next session's first turn.

    Returns None when there is nothing to carry forward (no prior closure and no
    open items), so a brand-new user sees no injected noise.
    """
    closure = latest_closure()
    owed = open_owed()

    if closure is None and not owed:
        return None

    lines = ["[THREAD] Resuming your work — here's the thread:"]

    if closure is not None:
        cid = closure.get("session_id", "?")[:32]
        status = closure.get("status", "completed")
        model = closure.get("model") or "?"
        files = closure.get("files") or []
        user = (closure.get("user") or "").strip()
        assistant = (closure.get("assistant") or "").strip()

        lines.append(f"- Last session {cid} ({model}) ended {status}.")
        if user:
            lines.append(f"- You were working on: {user[:400]}")
        if files:
            lines.append("- Files touched: " + ", ".join(files[:15]))
        if assistant:
            lines.append(f"- Last outcome: {assistant[:600]}")

    if owed:
        lines.append("- Open items:")
        for o in owed[:10]:
            lines.append(f"    - {o.get('text', '')[:300]}")

    lines.append("- Use /resume for the full thread; whats_owed lists open items.")

    block = "\n".join(lines)
    return block[:max_chars]
