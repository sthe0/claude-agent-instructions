"""Tenet: side-effect-free actions are pre-authorized (CLAUDE.md § Acting without asking).

Reads / searches / --help / --dry-run never need an ask. A candidate edit that would require
approval for side-effect-free actions semantically contradicts this rule — a class-2 conflict.
"""
from ..runner import Tenet

TENET = Tenet(
    name="side-effect-free-preauthorized",
    description="Side-effect-free actions (read/search/--help/--dry-run) are pre-authorized; "
                "they never require an ask.",
    protected_terms=frozenset({"side", "effect", "free", "actions", "pre", "authorized", "read"}),
    must_affirm=True,
)
