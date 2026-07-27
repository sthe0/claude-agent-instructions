#!/usr/bin/env python3
"""PreToolUse(Bash) hook: nudge to prefer a domain Skill when a Bash command
hand-rolls a known domain operation.

Rule (CLAUDE.md § Skill-first dispatch + memory leaf skill-first-dispatch):
before issuing Bash for a known domain operation (VCS, secrets, tracker REST,
monorepo code search, …) scan the skill list and prefer the Skill — it is the
cheaper, single-call, auditable, write-capable path. Passive listing is not a
trigger; this hook makes the scan mechanical by matching operation-class
signatures in the raw command.

Each matched class fires once per session (state file) so a repeated operation
does not flood context. Advisory only: stdout, exit 0, never blocks.

Core ships exactly one operation class — `tracker`, whose signature is a
host-agnostic REST-API shape (a `tracker.` subdomain, a `/v2/issues` or
`/rest/api/<n>/issue` path) plus any operator-supplied host/path fragments from
`skill_first_tracker_hosts=` (comma/space-separated) in the system config root's
`agent-identity.local`. Every other class is machine-local: a JSON array of
`{"name", "pattern", "skill"}` objects at `config_root.skill_first_classes_file()`.
Which command verbs count as "a known domain operation" is an org fact, so Core
holds the mechanism and the deployment holds the data.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def _tracker_hosts(identity_path=None) -> tuple[str, ...]:
    """Resolve extra tracker-API host/path fragments from `skill_first_tracker_hosts=`
    in the resolved agent-identity.local. Fail-open: any error yields no extra
    fragments (a hook must never crash the Bash call it advises on)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from difficulty_channel.authority import read_local_identity, LOCAL_IDENTITY_PATH

        raw = read_local_identity(identity_path or LOCAL_IDENTITY_PATH).get(
            "skill_first_tracker_hosts", ""
        )
        return tuple(n for n in re.split(r"[,\s]+", raw.strip()) if n)
    except Exception:
        return ()


def _build_tracker_re(extra_hosts=None) -> "re.Pattern[str]":
    """Compile the tracker-API detection pattern: a host-agnostic REST-shape
    fragment plus any operator-supplied host/path fragments."""
    fragments = [r"tracker\.[\w.-]+", r"/v2/issues", r"/rest/api/\d+/issue"]
    resolved = extra_hosts if extra_hosts is not None else _tracker_hosts()
    fragments.extend(re.escape(h) for h in resolved)
    return re.compile(r"curl\b[^\n]*\b(" + "|".join(fragments) + r")\b", re.IGNORECASE)


def _local_classes(classes_path=None) -> list[tuple[str, "re.Pattern[str]", str]]:
    """Load machine-local operation classes: a JSON array of objects carrying
    `name`, `pattern` (a Python regex, matched case-insensitively) and `skill`.
    Fail-open per entry — a malformed file or an uncompilable pattern costs the
    nudge, never the Bash call this hook advises on."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from lib import config_root

        path = classes_path or config_root.skill_first_classes_file()
        entries = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for e in entries if isinstance(entries, list) else []:
        try:
            out.append((e["name"], re.compile(e["pattern"], re.IGNORECASE), e["skill"]))
        except Exception:
            continue
    return out


def build_classes(classes_path=None, extra_hosts=None) -> list[tuple[str, "re.Pattern[str]", str]]:
    """operation-class -> (compiled signature, suggested skill family). High
    precision: each pattern targets a write/domain op a skill clearly covers.
    Core's only builtin is `tracker`; the rest come from the machine-local file."""
    return [
        ("tracker", _build_tracker_re(extra_hosts),
         "tracker / tracker-management / tracker-client"),
    ] + _local_classes(classes_path)


CLASSES = build_classes()


def detect(cmd: str, classes=None) -> list[tuple[str, str]]:
    """Return [(class_name, skill_family), …] for every matched operation class."""
    return [(name, skill) for name, rx, skill in (CLASSES if classes is None else classes)
            if rx.search(cmd)]


def state_path(session_id: str) -> Path:
    safe = "".join(c for c in (session_id or "nosession") if c.isalnum() or c in "-_")
    return Path(f"/tmp/cc-skill-first-{safe or 'nosession'}.json")


def load_fired(p: Path) -> set[str]:
    try:
        return set(json.loads(p.read_text()))
    except Exception:
        return set()


def save_fired(p: Path, fired: set[str]) -> None:
    try:
        p.write_text(json.dumps(sorted(fired)))
    except Exception:
        pass


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    if payload.get("tool_name") != "Bash":
        return 0

    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not cmd.strip():
        return 0

    matches = detect(cmd)
    if not matches:
        return 0

    sp = state_path(payload.get("session_id") or "")
    fired = load_fired(sp)
    fresh = [(name, skill) for name, skill in matches if name not in fired]
    if not fresh:
        return 0
    fired.update(name for name, _ in fresh)
    save_fired(sp, fired)

    lines = "\n".join(f"  - {name}: prefer Skill family → {skill}" for name, skill in fresh)
    print(
        "[skill-first] This Bash command hand-rolls a known domain operation:\n"
        f"{lines}\n"
        "Per CLAUDE.md § Skill-first dispatch: a Skill is the cheaper, single-call,\n"
        "auditable, write-capable path. Scan the system-reminder skill list and prefer\n"
        "the Skill over raw CLI (and over an mcp__* tool for the same op)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
