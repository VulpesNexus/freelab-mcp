"""Risky mode: actions FreeLab MCP offers but keeps off unless the user asked for risky mode in their own words.

The list of risky actions is the listener's RISKY_ACTIONS (listener/freelab_listener.py), which the listener
enforces: a request without risky: true is refused there whatever the client. It is read here from that
file's source as a literal (never run), so there is one list; a server that cannot read the file asks the
running listener for it instead (the risky_actions operation). docs/RISKY.md describes each action.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

LISTENER_FILE = Path(__file__).resolve().parents[2] / "listener" / "freelab_listener.py"
FIELDS = ("action", "tools", "reason", "enforced")


def load_actions(path: Path = LISTENER_FILE) -> list[dict[str, Any]]:
    """The listener's RISKY_ACTIONS, as [{action, tools, reason, enforced}]."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "RISKY_ACTIONS" for t in node.targets):
            return [dict(zip(FIELDS, entry)) for entry in ast.literal_eval(node.value)]
    raise LookupError(f"{path.name} has no RISKY_ACTIONS list")


try:
    ACTIONS: list[dict[str, Any]] | None = load_actions()
    LOAD_ERROR: str | None = None
except (OSError, SyntaxError, ValueError, LookupError) as exc:  # (a server installed without the listener's file)
    ACTIONS, LOAD_ERROR = None, f"{type(exc).__name__}: {exc}"


class Mode:
    """The session's risky mode: off when the server starts, turned on only by set_risky_mode."""

    enabled = False


def reason(action: str) -> str:
    for entry in ACTIONS or []:
        if entry["action"] == action:
            return entry["reason"]
    return "It is listed in docs/RISKY.md."


def require(action: str, args: dict[str, Any]) -> dict[str, Any]:
    """args with risky: true while risky mode is on; otherwise the tool error risky_mode_off."""
    if not Mode.enabled:
        raise RuntimeError(f"[risky_mode_off] {action} is a risky action, refused while risky mode is off: "
                           f"{reason(action)} set_risky_mode(true) turns risky mode on, and only when the user has "
                           "asked for risky mode in their own words in this conversation. Nothing was done.")
    args["risky"] = True
    return args
