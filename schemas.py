"""Tool schemas (what the LLM sees) for Thread's tools."""

WHATS_OWED_SCHEMA = {
    "name": "thread_whats_owed",
    "description": (
        "List the outstanding items and the current thread state carried forward "
        "from prior sessions. Use this when the user asks what's left to do, what "
        "you owe, or what you were working on last session. Returns open owed "
        "items plus the most recent session closure."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

LOG_SCHEMA = {
    "name": "thread_log",
    "description": (
        "Record an outstanding work item, decision, or follow-up into the thread "
        "ledger so a future session carries it forward. Call this when the user "
        "explicitly says something is owed, deferred, or incomplete ('next step: "
        "X', 'still need to do Y'). The item persists across sessions until "
        "resolved."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The outstanding item, decision, or next step, written as a clear imperative. E.g. 'Re-run the validation gate after the Thinkspace migration.'",
            },
            "kind": {
                "type": "string",
                "enum": ["todo", "decision", "blocker"],
                "description": "Category of the item. Default 'todo'.",
            },
        },
        "required": ["text"],
    },
}

RESOLVE_SCHEMA = {
    "name": "thread_resolve",
    "description": (
        "Mark an owed item as resolved (done). Matches by substring against the "
        "item text. Use this when an outstanding item has been completed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "Substring to match against owed item text for resolution. E.g. 'validation gate' resolves items containing that text.",
            },
        },
        "required": ["text"],
    },
}
