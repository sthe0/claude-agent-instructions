"""The Directive — every CLI subcommand's return value.

A Directive is the uniform answer the engine gives the caller (the LLM coordinator
today, an MCP client tomorrow): what node we're in, what the caller should do next,
and any human-readable detail. Keeping this a plain dataclass means the CLI layer
and a future MCP server can share one contract — the CLI just JSON-prints it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Directive.marker value for an unacknowledged effort-divergence fire blocking
# dispatch/replan/submit_plan: a synchronous, typed escalation the coordinator's
# spine-follow code must satisfy via `agentctl fire-acknowledge`, distinct from the
# strategic-escalation "ESCALATE" marker.
DIRECTIVE_ESCALATE_TO_USER = "ESCALATE_TO_USER"


@dataclass
class Directive:
    ok: bool
    node: str
    action: str          # what the coordinator should do next, e.g. "await_user_approval"
    detail: str = ""
    marker: str | None = None   # COMPLETED/CLARIFY/... when the directive maps to a return marker
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
