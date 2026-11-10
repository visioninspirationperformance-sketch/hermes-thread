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

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import ledger, schemas, tools

try:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, HTMLResponse
    from starlette.routing import Route
    STARLETTE_AVAILABLE = True
except Exception:
    STARLETTE_AVAILABLE = False

logger = logging.getLogger(__name__)

# In-memory, per-session accumulation of the objective thread state, flushed to
# the ledger at on_session_end. Locked because post_tool_call fires concurrently
# across parallel tool calls.
_state: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()

# Sessions that already received their first-turn resume injection, so a resumed
# session does not re-inject the same thread mid-flight.
_injected: set = set()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _owner(task_id: str, session_id: str) -> str:
    """Buckets accidentally keyed to a subagent's task_id still flush under the
    session's own bucket at end (we resolve by whichever id is present)."""
    return session_id or task_id or "default"


def _session_bucket(session_id: str) -> Dict[str, Any]:
    with _lock:
        bucket = _state.setdefault(session_id or "default", {
            "files": set(),
            "tools": set(),
            "user": "",
            "assistant": "",
        })
    return bucket


def _extract_paths_touched(tool_name: str, args: Optional[Dict[str, Any]]) -> set:
    """Lightweight capture of which on-disk artifacts a call touched. We record
    the ``path`` argument for write_file/patch and note terminal commands by
    truncated text; we do not stat the filesystem (no false positives)."""
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
            out.add("terminal: " + cmd[:200])
    return out


# --------------------------------------------------------------------------- #
# Hooks
# --------------------------------------------------------------------------- #

def _on_post_tool_call(tool_name: str = "", args: Optional[Dict[str, Any]] = None,
                       session_id: str = "", task_id: str = "", **_: Any) -> None:
    try:
        bucket = _session_bucket(session_id or task_id)
        touched = _extract_paths_touched(tool_name, args)
        with _lock:
            bucket["tools"].add(tool_name)
            bucket["files"].update(touched)
    except Exception as exc:  # never break the agent loop
        logger.debug("thread post_tool_call failed: %s", exc)


def _on_post_llm_call(session_id: str = "", task_id: str = "",
                      user_message: str = "", assistant_response: str = "", **_: Any) -> None:
    try:
        bucket = _session_bucket(session_id or task_id)
        with _lock:
            if user_message:
                bucket["user"] = user_message
            if assistant_response:
                bucket["assistant"] = assistant_response
    except Exception as exc:
        logger.debug("thread post_llm_call failed: %s", exc)


def _on_session_end(session_id: str = "", completed: bool = True,
                    failed: bool = False, interrupted: bool = False,
                    model: str = "", platform: str = "", **_: Any) -> None:
    try:
        # Resolve the real bucket: a bare session_id if present, else drain the
        # "default" bucket (legacy CLI/TUI exit shapes may omit the id).
        with _lock:
            bucket = _state.pop(session_id or "default", None)
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


def _on_pre_llm_call(session_id: str = "", is_first_turn: bool = False, **_: Any) -> Optional[str]:
    """Inject the resume block on a session's first turn. Return a string to be
    joined into the user message (directive/control hook contract), or None."""
    if not is_first_turn:
        return None
    if session_id in _injected:
        return None
    _injected.add(session_id)
    try:
        block = ledger.resume_block()
        return block or None
    except Exception as exc:
        logger.debug("thread pre_llm_call failed: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Dashboard / REST API (v2.0) — standalone Starlette server
# --------------------------------------------------------------------------- #

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang=\"en\">
<head>
<meta charset=\"UTF-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
<title>Thread Dashboard</title>
<style>
  :root { --bg:#000; --fg:#fff; --muted:#888; --accent:#0ff; --card:#111; --border:#333; --font:'JetBrains Mono',monospace; }
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:var(--font);background:var(--bg);color:var(--fg);line-height:1.5;padding:16px}
  h1{color:var(--accent);font-size:1.5rem;margin-bottom:16px;display:flex;align-items:center;gap:8px}
  .badge{background:var(--accent);color:#000;padding:2px 8px;border-radius:4px;font-size:0.75rem}
  .controls{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}
  button{background:var(--card);border:1px solid var(--border);color:var(--fg);padding:8px 12px;border-radius:4px;cursor:pointer;font-family:var(--font);font-size:0.85rem}
  button:hover{border-color:var(--accent);background:#1a1a1a}
  button:disabled{opacity:0.5;cursor:not-allowed}
  .status{color:var(--muted);font-size:0.85rem}
  .status.live{color:var(--accent)}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
  .session-card{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:12px;transition:border-color .2s}
  .session-card:hover{border-color:var(--accent)}
  .session-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
  .session-id{color:var(--accent);font-weight:600;font-size:0.85rem}
  .session-time{color:var(--muted);font-size:0.75rem}
  .session-status{padding:2px 8px;border-radius:3px;font-size:0.7rem;font-weight:600}
  .status-completed{background:#0a3d0a;color:#0f0}
  .status-failed{background:#3d0a0a;color:#f00}
  .session-files{margin-top:8px;max-height:80px;overflow:auto}
  .session-files summary{cursor:pointer;color:var(--muted);font-size:0.8rem}
  .file-list{font-size:0.75rem;color:var(--fg);margin-top:4px;display:grid;gap:2px}
  .session-outcome{margin-top:8px;font-size:0.8rem;color:var(--muted)}
  .session-outcome strong{color:var(--fg)}
  .open-items{margin-top:16px;padding:12px;background:var(--card);border:1px solid var(--border);border-radius:6px}
  .open-items h3{color:var(--accent);font-size:0.9rem;margin-bottom:8px}
  .item{padding:6px 8px;margin:4px 0;background:#0a0a0a;border:1px solid var(--border);border-radius:3px;font-size:0.8rem}
  .item.kind-decision{border-left:3px solid var(--accent)}
  .item.kind-blocker{border-left:3px solid #f00}
  .empty{color:var(--muted);text-align:center;padding:32px}
</style>
</head>
<body>
  <h1>Thread <span class=\"badge\">v2.0</span></h1>
  <div class=\"controls\">
    <button id=\"refreshBtn\">Refresh</button>
    <button id=\"clearBtn\">Clear Owed</button>
    <span class=\"status\" id=\"status\">Loading...</span>
  </div>
  <div id=\"sessions\" class=\"grid\"></div>
  <div class=\"open-items\">
    <h3>Open Items (<span id=\"owedCount\">0</span>)</h3>
    <div id=\"owedList\"></div>
  </div>
<script>
  const API = '/api/thread';
  const statusEl = document.getElementById('status');
  const sessionsEl = document.getElementById('sessions');
  const owedListEl = document.getElementById('owedList');
  const owedCountEl = document.getElementById('owedCount');
  let pollInterval;

  async function fetchData() {
    try {
      const res = await fetch(API);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return await res.json();
    } catch (e) {
      statusEl.textContent = 'Error: ' + e.message;
      statusEl.className = 'status';
      return null;
    }
  }

  function render(data) {
    if (!data) return;
    const { closures, owed } = data;
    statusEl.textContent = 'Live - updated ' + new Date().toLocaleTimeString();
    statusEl.className = 'status live';

    sessionsEl.innerHTML = '';
    if (closures.length === 0) {
      sessionsEl.innerHTML = '<div class=\"empty\">No sessions recorded yet</div>';
    } else {
      closures.slice().reverse().forEach(c => {
        const card = document.createElement('div');
        card.className = 'session-card';
        const files = c.files || [];
        card.innerHTML = `
          <div class="session-header">
            <span class="session-id">${c.session_id.slice(0,32)}</span>
            <span class="session-time">${new Date(c.ts * 1000).toLocaleString()}</span>
            <span class="session-status status-${c.status}">${c.status}</span>
          </div>
          <div class="session-files">
            <summary>Files touched (${files.length})</summary>
            <div class="file-list">${files.slice(0,20).map(f => '<div>' + f + '</div>').join('')}</div>
          </div>
          <div class="session-outcome"><strong>Outcome:</strong> ${(c.assistant || '').slice(0,400)}</div>
        `;
        sessionsEl.appendChild(card);
      });
    }

    owedCountEl.textContent = owed.length;
    owedListEl.innerHTML = '';
    if (owed.length === 0) {
      owedListEl.innerHTML = '<div class="empty">Nothing owed</div>';
    } else {
      owed.forEach(o => {
        const div = document.createElement('div');
        div.className = 'item kind-' + (o.kind || 'todo');
        div.textContent = o.text;
        owedListEl.appendChild(div);
      });
    }
  }

  async function load() {
    const data = await fetchData();
    render(data);
  }

  document.getElementById('refreshBtn').onclick = load;
  document.getElementById('clearBtn').onclick = async () => {
    if (confirm('Clear all open items?')) {
      await fetch(API + '/owed', { method: 'DELETE' });
      load();
    }
  };

  load();
  pollInterval = setInterval(load, 3000);
  window.addEventListener('beforeunload', () => clearInterval(pollInterval));
</script>
</body>
</html>"""


_dashboard_server: Optional[Any] = None
_dashboard_port: int = 0


async def _dashboard_thread_dashboard_endpoint(request):
    return HTMLResponse(DASHBOARD_HTML)


async def _dashboard_thread_api_endpoint(request):
    closures = []
    owed = []
    for r in ledger.read_ledger(limit=500):
        if r.get('type') == 'closure':
            closures.append(r)
        elif r.get('type') == 'owed' and r.get('status') == 'open':
            owed.append(r)
    return JSONResponse({'closures': closures, 'owed': owed})


async def _dashboard_thread_api_owed_clear(request):
    # Mark all open owed items as cleared by appending cleared records
    for r in ledger.read_ledger(limit=1000):
        if r.get('type') == 'owed' and r.get('status') == 'open':
            ledger.append_owed(r.get('session_id', 'unknown'), r.get('text', ''), kind=r.get('kind', 'todo'))
    return JSONResponse({'success': True})


def _build_dashboard_app():
    """Build the Starlette app for the Thread dashboard."""
    if not STARLETTE_AVAILABLE:
        return None
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, JSONResponse
    from starlette.routing import Route

    return Starlette(routes=[
        Route('/thread/dashboard', _dashboard_thread_dashboard_endpoint, methods=['GET']),
        Route('/api/thread', _dashboard_thread_api_endpoint, methods=['GET']),
        Route('/api/thread/owed', _dashboard_thread_api_owed_clear, methods=['DELETE']),
    ])


def _dashboard_port_file() -> Path:
    """File to persist the dashboard port across processes."""
    return ledger.dir_path() / "dashboard_port.txt"


def _start_dashboard_server(ctx=None):
    """Start the dashboard server on a random available port."""
    global _dashboard_server, _dashboard_port
    if not STARLETTE_AVAILABLE:
        return 0
    if _dashboard_server is not None:
        return _dashboard_port

    import socket
    import uvicorn
    from threading import Thread

    # Find a free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]

    app = _build_dashboard_app()
    if app is None:
        return 0

    config = uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning')
    server = uvicorn.Server(config)

    def run_server():
        import asyncio
        asyncio.run(server.serve())

    thread = Thread(target=run_server, daemon=True, name='thread-dashboard')
    thread.start()

    # Wait briefly for server to start
    import time
    time.sleep(0.5)

    _dashboard_server = server
    _dashboard_port = port
    # Persist port to file for cross-process access (agent process)
    try:
        _dashboard_port_file().write_text(str(port), encoding='utf-8')
    except Exception:
        pass
    if ctx:
        ctx.emit('thread.dashboard_started', {'port': port, 'url': f'http://127.0.0.1:{port}/thread/dashboard'})
    return port


def _get_dashboard_port() -> int:
    global _dashboard_port
    if _dashboard_port:
        return _dashboard_port
    # Try reading from persisted file (for agent process)
    try:
        port_file = _dashboard_port_file()
        if port_file.exists():
            _dashboard_port = int(port_file.read_text(encoding='utf-8').strip())
            return _dashboard_port
    except Exception:
        pass
    return 0


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
        name="thread_dashboard",
        toolset="thread",
        schema=schemas.DASHBOARD_SCHEMA,
        handler=tools.handle_dashboard,
    )

    ctx.register_command(
        "resume",
        handler=tools.handle_resume_command,
        description="Show your current thread: last session, open items, resume point.",
    )
    ctx.register_command(
        "thread-dashboard",
        handler=tools.handle_dashboard_command,
        description="Open the Thread dashboard in the Desktop pane (or print URL).",
    )

    # Start the dashboard server (v2.0)
    _start_dashboard_server(ctx)
