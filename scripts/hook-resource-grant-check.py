"""PreToolUse hook: answer a spawned child's tool call from the resources its stage was approved to change.

Difficulty removed: a spawned child runs headless, so a Bash call whose effect the plan approved but whose
spelling the child's allow list does not match (`cd X && git commit`, `python3 -c` writing the venue, a
`git -C` spelled with a different prefix) stops at an approval prompt nobody can answer, and the child
reports a permission stop for work the plan already sanctioned. The spawn wrapper writes the stage's typed
resources, its wildcard-tail rules and the child's final `permissions.allow` into the norm-handoff record
(`AGENTCTL_NORM_HANDOFF`); this hook resolves each segment of the call (`tool_contracts.resolve_segments`,
opaque code through `code_effects.effects_of`) and compares what it changes to that record.

Active only in a linked child: `AGENT_RECURSION_DEPTH >= 1` and a handoff record whose bytes match the
recorded sha256. Anything else, the kill switch included (config row `resource-grant-hook`,
`AGENTCTL_RESOURCE_HOOK`), makes no decision.

Decisions (Bash):
  allow  every segment is covered: its changed resources lie within the record's resources, or the segment
         matches a record rule pattern; a segment that changes nothing must also read only inside the
         record's resources or the parent scratchpad
  deny   some segment writes a resource the record does not cover, that segment is not matched by an entry of
         the child's `permissions.allow`, and the permission mode does not already let it run (acceptEdits and
         a write inside the child's cwd); the reason carries a `PERMISSION-REQUEST:` Rule:/Resource: template
  none   everything else: an unresolved or undeterminable segment, a harness-allowed call, an error. The
         harness then decides exactly as it would without the hook.
Edit, Write and NotebookEdit: allow when the file is covered, never deny. Read: allow only inside the
record's resources or the parent scratchpad. A deny from another hook or a settings deny rule still wins
(probe note Q3/Q4), so this hook cannot widen past an existing guard.

Every call appends one row to `<state_root>/resource-uses/<parent_session>-<stage>.jsonl`.

Residuals, named in docs/operations/resource-grant-hook.md: the hook sees its own process environment, not
the Bash tool's shell snapshot (CDPATH, GIT_*, HOME, XDG_CONFIG_HOME, PYTHON* set only there); a resolved
commit or push still runs the venue's git hooks; the parent scratchpad is recognised by path layout.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agentctl import code_effects, resources, segment_resolver, tool_contracts  # noqa: E402
from agentctl import config as agentctl_config  # noqa: E402
from agentctl.norm_handoff import NORM_HANDOFF_ENV, verify_env_value  # noqa: E402
from agentctl.store import safe_session_id  # noqa: E402
from lib import config_root  # noqa: E402

RECURSION_ENV = "AGENT_RECURSION_DEPTH"
USES_DIRNAME = "resource-uses"
UNLINKED_LOG = "_unlinked"

BASH_TOOLS = ("Bash",)
EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
READ_TOOLS = {"Read": "file_path"}

ACCEPT_EDITS = "acceptEdits"
#: Filesystem programs measured as run by acceptEdits inside the child's cwd (probe note Q6).
ACCEPT_EDITS_PROGRAMS = frozenset({"mkdir", "touch", "cp", "mv", "sed", "rm"})
#: A segment containing one of these is not matched against a rule: a redirect, a substitution or a
#: further command hides an effect the rule text does not name.
RULE_UNSAFE_CHARS = frozenset("<>`$\n\r;&|")
GLOB_CHARS = frozenset("*?[")

ALLOW, DENY, NONE = "allow", "deny", "none"
COVERED, UNCOVERED, UNDETERMINABLE = "covered", "uncovered", "undeterminable"
HARNESS = "harness"


@dataclass
class Approved:
    """What the handoff record sanctions, parsed once per call."""

    resources: list
    rule_patterns: list
    allow_entries: list
    parent_session: str
    stage_index: int
    cwd: str
    mode: str
    protected: list = field(default_factory=list)


@dataclass
class SegmentVerdict:
    text: str
    state: str
    changed: list = field(default_factory=list)
    uncovered: list = field(default_factory=list)
    matched: list = field(default_factory=list)
    opaque: bool = False
    reason: str = ""


@dataclass
class Verdict:
    decision: str
    reason_class: str
    resources: list = field(default_factory=list)
    matched: list = field(default_factory=list)
    message: str = ""
    venue: str = ""


# --- activation -----------------------------------------------------------------------------------

def _depth(env) -> int:
    try:
        return int(env.get(RECURSION_ENV, "0"))
    except ValueError:
        return 0


def _real(path: str) -> str:
    return os.path.realpath(path)


def _tool_text(tool_name: str, tool_input: dict) -> str:
    if tool_name in BASH_TOOLS:
        return str(tool_input.get("command") or "")
    for table in (EDIT_TOOLS, READ_TOOLS):
        key = table.get(tool_name)
        if key:
            return str(tool_input.get(key) or "")
    return ""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


# --- the log --------------------------------------------------------------------------------------

def _log_path(state_root: Path, link: "dict | None") -> Path:
    directory = state_root / USES_DIRNAME
    if link is None:
        return directory / f"{UNLINKED_LOG}.jsonl"
    return directory / f"{safe_session_id(link['parent_session'])}-{link['stage_index']}.jsonl"


def append_row(path: Path, row: dict) -> None:
    """Best effort: a log that cannot be written never changes the decision."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _row(tool: str, text: str, verdict: Verdict) -> dict:
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tool": tool,
        "segment_sha": _sha(text),
        "decision": verdict.decision,
        "reason_class": verdict.reason_class,
        "resources": verdict.resources,
        "venue": verdict.venue,
        "matched": verdict.matched,
    }


# --- matching -------------------------------------------------------------------------------------

def _tokens(text: str) -> "list[str] | None":
    try:
        return shlex.split(text)
    except ValueError:
        return None


def rule_matches(entry: str, text: str) -> bool:
    """Strict reading of a `Bash(...)` rule against ONE segment: an exact spec, or a `prefix:*` spec on a
    whole-word boundary. Unlike `permission_entry_match.covers` it never errs toward covering: a segment
    with a redirect, a substitution or an unparsable quote matches nothing."""
    if not (entry.startswith("Bash(") and entry.endswith(")")):
        return False
    if any(ch in RULE_UNSAFE_CHARS for ch in text):
        return False
    spec = entry[len("Bash("):-1]
    tokens = _tokens(text)
    if not tokens:
        return False
    operand = " ".join(tokens)
    if spec.endswith(":*"):
        prefix_tokens = _tokens(spec[:-2])
        if not prefix_tokens:
            return False
        prefix = " ".join(prefix_tokens)
        return operand == prefix or operand.startswith(prefix + " ")
    spec_tokens = _tokens(spec)
    return bool(spec_tokens) and operand == " ".join(spec_tokens)


def _first_rule(entries: list, text: str) -> "str | None":
    return next((entry for entry in entries if rule_matches(entry, text)), None)


def _is_change(resource) -> bool:
    if isinstance(resource, resources.FileResource):
        return resource.mode == "write"
    if isinstance(resource, resources.ServiceResource):
        return resource.op_class != "read"
    return True


def _describe(resource) -> str:
    if isinstance(resource, resources.FileResource):
        return f"file {resource.mode} {resource.path}"
    if isinstance(resource, resources.VcsRefResource):
        return f"vcs_ref {resource.op} {resource.remote}:{resource.ref}"
    return json.dumps(resources.resource_to_dict(resource), sort_keys=True)


def covered_by(approved: Approved, resource) -> bool:
    return any(item.covers(resource, protected=approved.protected) for item in approved.resources)


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def scratch_roots() -> set:
    return {_real(tempfile.gettempdir()), _real("/tmp")}


def parent_scratchpad_path(path: str, parent_session: str) -> bool:
    """True when `path` (already absolute) lies in the parent session's scratchpad: an OS scratch root
    holding `<...>/<parent session id>/scratchpad/...`. The layout is the harness's, not a field of the
    record, so the check is on the realpath: a symlink out of the scratchpad resolves to where it points."""
    real = _real(path)
    parts = Path(real).parts
    roots = scratch_roots()
    if not any(_under(real, root) for root in roots):
        return False
    return any(parts[i] == parent_session and parts[i + 1] == "scratchpad" for i in range(len(parts) - 1))


def readable(approved: Approved, path: str) -> "str | None":
    """What allows reading `path`: a record resource (a write covers a read) or the parent scratchpad."""
    if not os.path.isabs(path):
        return None
    if covered_by(approved, resources.FileResource(path, "read")):
        return "record-resource"
    if parent_scratchpad_path(path, approved.parent_session):
        return "parent-scratchpad"
    return None


def _operand_paths(tokens: list, venue: str) -> "list[str] | None":
    """Absolute paths the operands of a read-only program name, or None when an operand cannot be pinned
    down (a glob, `~`, a variable)."""
    out: list = []
    for tok in tokens[1:]:
        candidate = tok
        if tok.startswith("-"):
            if "=" not in tok:
                continue
            candidate = tok.split("=", 1)[1]
        if candidate.startswith("~") or "$" in candidate or any(ch in candidate for ch in GLOB_CHARS):
            return None
        pathlike = "/" in candidate or candidate.startswith(".") or os.path.lexists(os.path.join(venue, candidate))
        if not pathlike:
            continue
        out.append(candidate if os.path.isabs(candidate) else os.path.join(venue, candidate))
    return out


def reads_confined(approved: Approved, text: str, venue: str) -> "tuple[bool, list]":
    """Whether every path a segment's operands name may be read. git is exempt: its operands are
    repo-relative pathspecs the resolver already models (`-C` included)."""
    tokens = _tokens(text)
    if not tokens:
        return False, []
    if os.path.basename(tokens[0]) == "git":
        return True, []
    paths = _operand_paths(tokens, venue)
    if paths is None:
        return False, []
    kinds = []
    for path in paths:
        kind = readable(approved, path)
        if kind is None:
            return False, []
        kinds.append(kind)
    return True, sorted(set(kinds))


# --- harness-allowed ------------------------------------------------------------------------------

def segment_allow_entry(approved: Approved, text: str) -> "str | None":
    """The entry of the child's `permissions.allow` that covers THIS segment. Per segment on purpose: the
    harness's own whole-call match errs toward covering when any one segment matches, which would let an
    allowed prefix shelter an uncovered tail."""
    return next((e for e in approved.allow_entries if e in ("Bash", "Bash(*)") or rule_matches(e, text)), None)


def _all_files_in_cwd(approved: Approved, changed: list) -> bool:
    return all(isinstance(r, resources.FileResource) and _under(r.path, approved.cwd) for r in changed)


def accept_edits_runs(approved: Approved, text: str, changed: list) -> bool:
    """acceptEdits already runs the measured filesystem programs on a path inside the child's cwd."""
    if approved.mode != ACCEPT_EDITS or not changed:
        return False
    tokens = _tokens(text)
    if not tokens or os.path.basename(tokens[0]) not in ACCEPT_EDITS_PROGRAMS:
        return False
    return _all_files_in_cwd(approved, changed)


def accept_edits_may_run(approved: Approved, uncovered: list) -> bool:
    """A write inside the cwd under acceptEdits may be auto-allowed even for a command the probe did not
    measure, so no deny is made for it."""
    return approved.mode == ACCEPT_EDITS and _all_files_in_cwd(approved, uncovered)


# --- segments -------------------------------------------------------------------------------------

def evaluate_segment(approved: Approved, segment, line: list, runner, judge_timeout, cache_dir) -> SegmentVerdict:
    text = segment.text
    venue = _real(segment.venue or approved.cwd)
    entry = segment_allow_entry(approved, text)
    if entry is not None:
        return SegmentVerdict(text, HARNESS, matched=[entry])
    pattern = _first_rule(approved.rule_patterns, text)
    if pattern is not None:
        return SegmentVerdict(text, COVERED, matched=[pattern])
    opaque = segment.status == "opaque"
    if segment.status == "unresolved":
        return SegmentVerdict(text, UNDETERMINABLE, reason=f"unresolved: {segment.reason_class}")
    changed = list(segment.resources)
    if opaque:
        found = code_effects.effects_of(segment, line, runner=runner, timeout=judge_timeout, cache_dir=cache_dir)
        if isinstance(found, code_effects.Undeterminable):
            return SegmentVerdict(text, UNDETERMINABLE, changed=changed, opaque=True, reason=found.reason)
        changed += [r for r in found.resources if r not in changed]
    changes = [r for r in changed if _is_change(r)]
    uncovered = [r for r in changes if not covered_by(approved, r)]
    if accept_edits_runs(approved, text, changes):
        return SegmentVerdict(text, HARNESS, changed=changes, matched=["mode:acceptEdits"], opaque=opaque)
    if uncovered:
        return SegmentVerdict(text, UNCOVERED, changed=changes, uncovered=uncovered, opaque=opaque)
    confined, kinds = (True, []) if opaque else reads_confined(approved, text, venue)
    if not confined:
        return SegmentVerdict(text, UNDETERMINABLE, changed=changes, opaque=opaque, reason="reads outside the approved resources")
    return SegmentVerdict(text, COVERED, changed=changes, matched=kinds, opaque=opaque)


def deny_message(uncovered: list, text: str) -> str:
    resource_lines = "".join(f"Resource: {_describe(r)}\n" for r in uncovered)
    return (
        "Resource-grant check: this call changes a resource the approved stage does not cover "
        f"({'; '.join(_describe(r) for r in uncovered)}). Do not retry it. If the step needs it, end your "
        "reply with this request so the parent can decide:\n"
        "PERMISSION-REQUEST:\n"
        f"Action: {text}\n"
        "Why: the step's plan does not list this change\n"
        "Fallback if denied: stop the step\n"
        f"Rule: Bash({text})\n"
        f"{resource_lines}"
    )


def decide_bash(approved: Approved, command: str, runner, judge_timeout, cache_dir) -> Verdict:
    split = tool_contracts.resolve_segments(command, approved.cwd)
    if not split.segments:
        return Verdict(NONE, UNDETERMINABLE, venue=approved.cwd)
    verdicts = [evaluate_segment(approved, seg, split.segments, runner, judge_timeout, cache_dir) for seg in split.segments]
    changed = list(dict.fromkeys(r for v in verdicts for r in v.changed))
    shown = [resources.resource_to_dict(r) for r in changed]
    matched = list(dict.fromkeys(m for v in verdicts for m in v.matched))
    opaque = any(v.opaque for v in verdicts)
    base = {"resources": shown, "matched": matched, "venue": approved.cwd}
    denied = [v for v in verdicts if v.state == UNCOVERED and not accept_edits_may_run(approved, v.uncovered)]
    if denied:
        uncovered = list(dict.fromkeys(r for v in denied for r in v.uncovered))
        return Verdict(DENY, "uncovered-write", message=deny_message(uncovered, denied[0].text), **base)
    if all(v.state == HARNESS for v in verdicts):
        return Verdict(NONE, "harness-allowed", **base)
    if all(v.state in (COVERED, HARNESS) for v in verdicts):
        return Verdict(ALLOW, "opaque-judged" if opaque else "covered", **base)
    return Verdict(NONE, UNDETERMINABLE, **base)


# --- file tools -----------------------------------------------------------------------------------

def _absolute(path: str, cwd: str) -> str:
    return path if os.path.isabs(path) else os.path.join(cwd, path)


def decide_edit(approved: Approved, path: str) -> Verdict:
    target = _absolute(path, approved.cwd)
    resource = resources.FileResource(target, "write")
    shown = [resources.resource_to_dict(resource)]
    if covered_by(approved, resource):
        return Verdict(ALLOW, "covered", resources=shown, matched=["record-resource"], venue=approved.cwd)
    return Verdict(NONE, "uncovered-write", resources=shown, venue=approved.cwd)


def decide_read(approved: Approved, path: str) -> Verdict:
    target = _absolute(path, approved.cwd)
    kind = readable(approved, target)
    if kind is None:
        return Verdict(NONE, UNDETERMINABLE, venue=approved.cwd)
    return Verdict(ALLOW, "covered", matched=[kind], venue=approved.cwd)


# --- the hook -------------------------------------------------------------------------------------

def _approved(link: dict, payload: dict) -> Approved:
    cwd = _real(str(payload.get("cwd") or os.getcwd()))
    typed = [resources.resource_from_dict(item) for item in (link.get("resources") or [])]
    root = segment_resolver.worktree_root(cwd)
    protected = resources.protected_permission_surfaces(
        repo_root=str(Path(__file__).resolve().parent.parent), delivery_worktree=root)
    return Approved(
        resources=typed,
        rule_patterns=[str(p) for p in (link.get("rule_patterns") or [])],
        allow_entries=[str(e) for e in (link.get("permissions_allow") or [])],
        parent_session=str(link["parent_session"]),
        stage_index=int(link["stage_index"]),
        cwd=cwd,
        mode=str(payload.get("permission_mode") or ""),
        protected=protected,
    )


def decide(payload: dict, env, *, runner=None, cache_dir=None, constants=None) -> "tuple[Verdict, dict | None, Path | None]":
    """The verdict for one hook payload, the verified record link (None when inactive) and the state root.
    Never raises for a call it cannot judge: such a call is `none` with reason class `error`."""
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if _depth(env) < 1:
        return Verdict(NONE, "inactive"), None, None
    link, why = verify_env_value(env.get(NORM_HANDOFF_ENV, ""))
    if link is None:
        return Verdict(NONE, "inactive", message=why or ""), None, config_root.agentctl_state_dir()
    state_root = Path(link["handoff"]).parent.parent
    if not agentctl_config.resource_grant_hook_enabled(env, constants):
        return Verdict(NONE, "kill-switch"), link, state_root
    try:
        approved = _approved(link, payload)
        if tool in BASH_TOOLS:
            judge = agentctl_config.resource_grant_hook_timeout_s(constants) - 5
            verdict = decide_bash(approved, str(tool_input.get("command") or ""), runner, max(judge, 1), cache_dir)
        elif tool in EDIT_TOOLS:
            verdict = decide_edit(approved, str(tool_input.get(EDIT_TOOLS[tool]) or ""))
        elif tool in READ_TOOLS:
            verdict = decide_read(approved, str(tool_input.get(READ_TOOLS[tool]) or ""))
        else:
            verdict = Verdict(NONE, UNDETERMINABLE)
    except Exception as exc:  # noqa: BLE001 - a hook error must never become an allow
        return Verdict(NONE, "error", message=f"{type(exc).__name__}: {exc}"), link, state_root
    return verdict, link, state_root


def render(verdict: Verdict) -> str:
    """The harness's PreToolUse answer for an allow or a deny; the empty string for no decision."""
    if verdict.decision not in (ALLOW, DENY):
        return ""
    reason = verdict.message or f"resource-grant check: {verdict.reason_class}"
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": verdict.decision,
        "permissionDecisionReason": reason,
    }})


def run(stdin_text: str, env, *, runner=None, cache_dir=None, constants=None) -> str:
    """The whole hook: payload text in, the answer text out, one log row appended. Tests drive this."""
    try:
        payload = json.loads(stdin_text)
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    verdict, link, state_root = decide(payload, env, runner=runner, cache_dir=cache_dir, constants=constants)
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if state_root is not None:
        append_row(_log_path(state_root, link), _row(tool, _tool_text(tool, tool_input), verdict))
    return render(verdict)


def main() -> int:
    try:
        answer = run(sys.stdin.read(), os.environ)
    except Exception as exc:  # noqa: BLE001
        print(f"hook-resource-grant-check: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
    if answer:
        print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
