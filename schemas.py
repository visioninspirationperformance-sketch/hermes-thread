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

DASHBOARD_SCHEMA = {
    "name": "thread_dashboard",
    "description": (
        "Get the full thread dashboard data: all session closures and open owed items. "
        "Returns structured data for the Desktop pane. The agent can call this to "
        "show the user a live view of their thread history."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of closure records to return (default 50).",
                "default": 50
            }
        },
        "required": [],
    },
}
