"""Thread — operational session continuity for Hermes.

A general plugin (tools + hooks) that closes the "operational state propagation
gap": at the end of a session it auto-captures what you were working on and what
remains owed, and at the start of the *next* session it re-orients the model so
Hermes never starts cold.

Mechanism (all standard plugin hooks, no external service, no database):

- ``post_tool_call``  -> accumulate the files / commands touched this session.
- ``post_llm_call``   -> capture the user ask and the agent's final outcome.
- ``on_session_end``  -> atomically append a closure record to the thread ledger.
- ``pre_llm_call``    -> on a session's FIRST turn, inject a compact resume block
                         (last closure + open owed items) into the user message.

Storage is a single append-only JSON-lines file under ``$HERMES_HOME/thread/``,
so it is per-profile, portable, and survives restarts. No network, no keys, no
daemon — it works offline on any platform with any provider.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Dict, List, Optional

from . import ledger, schemas, tools

logger = logging.getLogger(__name__)

# In-memory, per-session accumulation of the objective thread state, flushed to
# the ledger at on_session_end. Locked because post_tool_call fires concurrently
# across parallel tool calls.
# Key: "platform:chat_id:sender_id:session_id" to isolate threads across users/chats on a shared gateway.
_state: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()

# Sessions that already received their first-turn resume injection, so a resumed
# session does not re-inject the same thread mid-flight.
# Key: "platform:chat_id:sender_id:session_id"
_injected: set = set()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

# Token pattern: common API key prefixes (sk-..., ghp_..., glpat-..., etc.)
_TOKEN_RE = re.compile(r"\b(sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{36}|glpat-[A-Za-z0-9_-]{20,}|gho_[A-Za-z0-9]{36}|ghu_[A-Za-z0-9]{36}|ghs_[A-Za-z0-9]{36}|ghr_[A-Za-z0-9]{36}|xoxb-[A-Za-z0-9-]{10,}|xoxp-[A-Za-z0-9-]{10,}|xoxa-[A-Za-z0-9-]{10,}|ya29\.[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{35})\b")


def _redact_tokens(text: str) -> str:
    """Redact common token patterns from command text before storing."""
    return _TOKEN_RE.sub("[REDACTED]", text)


def _session_key(platform: str = "", chat_id: str = "", sender_id: str = "",
                 session_id: str = "", task_id: str = "") -> str:
    """Build a scoping key that isolates threads by platform+chat+sender.

    Falls back to session_id/task_id for legacy CLI/TUI shapes that don't
    provide platform/chat/sender identifiers.
    """
    if platform and chat_id and sender_id:
        return f"{platform}:{chat_id}:{sender_id}:{session_id or task_id or 'default'}"
    # Legacy: no platform/chat/sender — fall back to session/task only
    return session_id or task_id or "default"


def _session_bucket(key: str) -> Dict[str, Any]:
    with _lock:
        bucket = _state.setdefault(key, {
            "files": set(),
            "tools": set(),
            "user": "",
            "assistant": "",
        })
    return bucket


def _extract_paths_touched(tool_name: str, args: Optional[Dict[str, Any]]) -> set:
    """Lightweight capture of which on-disk artifacts a call touched. We record
    the ``path`` argument for write_file/patch and note terminal commands by
    truncated text (with tokens redacted); we do not stat the filesystem (no
    false positives).
    """
    if not isinstance(args, dict):
        return set()
    out: set = set()
    if tool_name in ("write_file", "patch"):
        p = args.get("path")
        if isinstance(p, str) and p:
            out.add(p)
    elif tool_name == "terminal":
        cmd = args.get("command")
        if isinstance(cmd, str) and cmd:
            # Redact tokens and truncate
            redacted = _redact_tokens(cmd)
            out.add("terminal: " + redacted[:200])
    return out


# --------------------------------------------------------------------------- #
# Hooks
# --------------------------------------------------------------------------- #

def _on_post_tool_call(tool_name: str = "", args: Optional[Dict[str, Any]] = None,
                       session_id: str = "", task_id: str = "",
                       platform: str = "", chat_id: str = "", sender_id: str = "",
                       **_: Any) -> None:
    try:
        key = _session_key(platform, chat_id, sender_id, session_id, task_id)
        bucket = _session_bucket(key)
        touched = _extract_paths_touched(tool_name, args)
        with _lock:
            bucket["tools"].add(tool_name)
            bucket["files"].update(touched)
    except Exception as exc:  # never break the agent loop
        logger.debug("thread post_tool_call failed: %s", exc)


def _on_post_llm_call(session_id: str = "", task_id: str = "",
                      user_message: str = "", assistant_response: str = "",
                      platform: str = "", chat_id: str = "", sender_id: str = "",
                      **_: Any) -> None:
    try:
        key = _session_key(platform, chat_id, sender_id, session_id, task_id)
        bucket = _session_bucket(key)
        with _lock:
            if user_message:
                bucket["user"] = user_message
            if assistant_response:
                bucket["assistant"] = assistant_response
    except Exception as exc:
        logger.debug("thread post_llm_call failed: %s", exc)


def _on_session_end(session_id: str = "", completed: bool = True,
                    failed: bool = False, interrupted: bool = False,
                    model: str = "", platform: str = "", chat_id: str = "",
                    sender_id: str = "", **_: Any) -> None:
    try:
        key = _session_key(platform, chat_id, sender_id, session_id)
        with _lock:
            bucket = _state.pop(key, None)
            if bucket is None:
                # Fall back to the most recently touched bucket if any.
                bucket = _state.pop(next(iter(_state), "default"), None) if _state else None

        if bucket is None:
            return

        status = "failed" if failed else ("interrupted" if interrupted else "completed")
        files = sorted(bucket["files"])[:50]  # cap spurious noise
        tools_used = sorted(bucket["tools"])[:50]

        ledger.append_closure(
            session_id=session_id or "unknown",
            status=status,
            model=model,
            platform=platform,
            files=files,
            tools=tools_used,
            user=bucket.get("user", "")[:2000],
            assistant=bucket.get("assistant", "")[:3000],
        )
    except Exception as exc:
        logger.debug("thread on_session_end failed: %s", exc)


def _on_pre_llm_call(session_id: str = "", is_first_turn: bool = False,
                     platform: str = "", chat_id: str = "", sender_id: str = "",
                     **_: Any) -> Optional[str]:
    """Inject the resume block on a session's first turn. Return a string to be
    joined into the user message (directive/control hook contract), or None.

    Scoped by platform+chat+sender so user A's thread never leaks into user B's
    session on a shared gateway.
    """
    if not is_first_turn:
        return None

    key = _session_key(platform, chat_id, sender_id, session_id)
    if key in _injected:
        return None
    _injected.add(key)

    try:
        block = ledger.resume_block()
        return block or None
    except Exception as exc:
        logger.debug("thread pre_llm_call failed: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

def register(ctx) -> None:
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)

    ctx.register_tool(
        name="thread_whats_owed",
        toolset="thread",
        schema=schemas.WHATS_OWED_SCHEMA,
        handler=tools.handle_whats_owed,
    )
    ctx.register_tool(
        name="thread_log",
        toolset="thread",
        schema=schemas.LOG_SCHEMA,
        handler=tools.handle_log,
    )
    ctx.register_tool(
        name="thread_resolve",
        toolset="thread",
        schema=schemas.RESOLVE_SCHEMA,
        handler=tools.handle_resolve,
    )

    ctx.register_command(
        "thread",
        handler=tools.handle_thread_command,
        description="Show your current thread: last session, open items, resume point.",
    )
