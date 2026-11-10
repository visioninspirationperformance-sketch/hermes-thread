"""Tool and slash-command handlers for Thread."""

from __future__ import annotations

import json
from typing import Any, Optional

from . import ledger


def handle_whats_owed(params: dict, **_: Any) -> str:
    owed = ledger.open_owed()
    closure = ledger.latest_closure()

    out = []

    if closure is not None:
        cid = closure.get("session_id", "?")[:32]
        status = closure.get("status", "completed")
        model = closure.get("model") or "?"
        files = closure.get("files") or []
        user = (closure.get("user") or "").strip()
        assistant = (closure.get("assistant") or "").strip()
        out.append(f"Last session: {cid} ({model}) — ended {status}.")
        if user:
            out.append(f"  Working on: {user[:500]}")
        if files:
            out.append("  Files: " + ", ".join(files[:20]))
        if assistant:
            out.append(f"  Outcome: {assistant[:800]}")

    if owed:
        out.append(f"Open items ({len(owed)}):")
        for o in owed:
            out.append(f"  [{o.get('kind', 'todo')}] {o.get('text', '')}")
    else:
        out.append("Open items: none.")

    return "\n".join(out) if out else "No thread state yet — nothing owed and no prior session recorded."


def handle_log(params: dict, session_id: str = "", **_: Any) -> str:
    text = (params.get("text") or "").strip()
    if not text:
        return json.dumps({"success": False, "error": "text is required"})
    kind = params.get("kind") or "todo"
    if kind not in ("todo", "decision", "blocker"):
        kind = "todo"
    ledger.append_owed(session_id, text, kind=kind)
    return json.dumps({"success": True, "logged": text, "kind": kind})


def handle_dashboard(params: dict, **_: Any) -> str:
    limit = params.get("limit") or 50
    closures = []
    owed = []
    for r in ledger.read_ledger(limit=limit):
        if r.get("type") == "closure":
            closures.append(r)
        elif r.get("type") == "owed" and r.get("status") == "open":
            owed.append(r)
    return json.dumps({"closures": closures, "owed": owed}, default=str)


def handle_resume_command(raw_args: str) -> Optional[str]:
    """/resume — print the current thread state."""
    lines = ["Thread — session continuity"]

    closure = ledger.latest_closure()
    if closure is not None:
        cid = closure.get("session_id", "?")[:32]
        status = closure.get("status", "completed")
        model = closure.get("model") or "?"
        files = closure.get("files") or []
        lines.append(f"Last session: {cid} ({model}) — {status}")
        if files:
            lines.append("Files: " + ", ".join(files[:15]))
        assistant = (closure.get("assistant") or "").strip()
        if assistant:
            lines.append(f"Outcome: {assistant[:600]}")
    else:
        lines.append("No prior session recorded yet.")

    owed = ledger.open_owed()
    lines.append("")
    if owed:
        lines.append(f"Open items ({len(owed)}):")
        for o in owed:
            lines.append(f"  - [{o.get('kind', 'todo')}] {o.get('text', '')}")
    else:
        lines.append("Open items: none.")

    return "\n".join(lines)


def handle_dashboard_command(raw_args: str) -> Optional[str]:
    """/thread-dashboard — show dashboard URL or print summary."""
    from . import _get_dashboard_port
    port = _get_dashboard_port()
    if port:
        url = f"http://127.0.0.1:{port}/thread/dashboard"
        return f"Thread Dashboard: {url}\nOpen in Desktop pane or browser."
    return "Thread Dashboard not running (Starlette/uvicorn not available or failed to start)"
