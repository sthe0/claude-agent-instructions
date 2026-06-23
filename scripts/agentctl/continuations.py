"""Continuation-prompt templates for re-spawning a specialist after the manager
has resolved its return marker.

These are the deterministic string-assembly half of escalation handling: the
manager supplies the cognition (the CLARIFY answer, the user's grant decision)
and the engine renders the prompt around it. The wording mirrors the templates in
memory-global/leaves/handling-escalations.md so the prose there can later collapse
to a pointer at these functions without any behaviour change.
"""
from __future__ import annotations


def clarify(question: str) -> str:
    return (
        f"The earlier CLARIFY: question — {question} — is answered: <your answer>.\n"
        "Continue from where you stopped:\n"
        "<continuation context>"
    )


def permission_granted(action: str, scope: str) -> str:
    recorded = ""
    if scope in ("project", "global"):
        recorded = f"\nRecorded as a {scope} grant in the permissions file."
    return (
        f"The earlier PERMISSION-REQUEST for {action} was resolved: "
        f"GRANTED (scope: {scope}).{recorded}\n\n"
        "Continue from where you stopped:\n"
        "<continuation context>"
    )


def permission_denied(action: str) -> str:
    return (
        f"The earlier PERMISSION-REQUEST for {action} was resolved: DENIED.\n"
        f"Do not perform {action}; use your stated fallback or stop.\n\n"
        "Continue from where you stopped:\n"
        "<continuation context>"
    )
