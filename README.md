# Thread — session continuity for Hermes

**Hermes never starts cold.**

Every AI agent — Hermes, Claude Code, Codex, Cursor — forgets what it was doing
between sessions. Not *facts about you* (you can put those in a memory file), but
**the thread**: the half-finished task, the open follow-up, the exact point you
were about to act on when you closed the laptop.

The funded memory startups (Mem0, Letta, Zep, Honcho, Hindsight) all chase
*knowledge* — "what do we know about this user." None of them solve what actually
costs you: *"what was I doing and what do I still owe."*

**Thread fixes that**, automatically. It does what a prompt or a skill cannot —
it hooks the Hermes runtime:

- **`on_session_end`** → auto-writes a closure (what you touched, what you asked,
  what the agent concluded, and whether the session finished or was interrupted).
- **`pre_llm_call`** (first turn) → injects a one-shot resume block so the next
  session opens *already oriented* — "you were working on X, here's what's owed."
- **`thread_whats_owed`** / **`thread_log`** tools → query and record outstanding
  items that survive across sessions.
- **`/resume`** → see your thread at any time.

No cloud, no API key, no database, no daemon. A single append-only file under
`~/.hermes/thread/`. Works offline, on any platform, with any provider.

## Install

```bash
hermes plugins install <owner>/hermes-thread   # after it's in the catalog
# or, locally:
cp -r hermes-thread ~/.hermes/plugins/thread
hermes plugins enable thread
```

## Use

Mid-task, just `/quit` or close the session. Open a new session — Thread has
already told Hermes what you were doing. Ask "what do I owe?" and it answers.

## Why this beats a prompt or a skill

A skill is a markdown doc the model *might* read. A prompt you re-type. Thread is
a **runtime hook** — it fires automatically at session end and start, with
stateful logic no injected text can reproduce.

## License

MIT
