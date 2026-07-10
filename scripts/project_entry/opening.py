"""opening.py — the mechanized first-turn brief for claude-task's opening dialogue.

Pure probes over injected callables (plan-file lister/reader, tracker-read result,
git branch lister + commit-ahead check), mirroring the detect_backend.py /
projects.py idiom: pure logic here, real I/O confined to the bottom `_real_*`
helpers and wired only in main()/__main__.

CLI:
  opening.py emit --dir DIR (--key KEY | --title TITLE)
                  [--plans-dir PATH]
                  [--ticket-file PATH | --ticket-unavailable REASON]

    Prints the composed brief (ticket: / artifacts: / mode:) on stdout and
    exits 0. Prints nothing and exits 3 when the opening dialogue is
    suppressed (CLAUDE_OPENING=off). Any OTHER nonzero exit means opening.py
    itself failed internally — never conflate that with suppression.

The `--key` path runs the three resume-candidacy probes (P1 plan-file
content, P2 tracker-comment authorship, P3 git branch) and computes
`mode: resume-candidate` when any probe fires, `mode: opening` otherwise.
The `--title` path (a brand-new task) always yields `mode: opening` by
construction — it never runs the key-keyed probes.
"""
from __future__ import annotations

import glob as _glob
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Iterable, Optional


class UsageError(Exception):
    pass


@dataclass(frozen=True)
class Artifact:
    kind: str  # "plan" | "tracker-comment" | "branch"
    detail: str  # human-readable line for the artifacts: section


# ── P1: plan-file probe ─────────────────────────────────────────────────────


def probe_plan_files(
    key: str,
    list_plan_files: Callable[[], "Iterable[str]"],
    read_file: Callable[[str], "Optional[str]"],
) -> "list[Artifact]":
    """A plan file whose CONTENT (never its filename) mentions key, case-insensitive."""
    needle = key.lower()
    found: "list[Artifact]" = []
    for path in list_plan_files():
        text = read_file(path)
        if text is not None and needle in text.lower():
            found.append(Artifact("plan", f"{path} mentions {key}"))
    return found


# ── P2: tracker-comment probe ────────────────────────────────────────────────

_COMMENT_RE = re.compile(r"^--- comment \d+ by (\S+) at ")


def probe_tracker_comments(
    agent_login: "Optional[str]",
    ticket_ok: bool,
    ticket_text: str,
) -> "list[Artifact]":
    """>=1 rendered '--- comment N by LOGIN at ...' line authored by agent_login.

    Abstains (returns []) when the identity is unknown or the ticket couldn't
    be read — neither condition may force a verdict either way (Q6/Q7)."""
    if not agent_login or not ticket_ok:
        return []
    found: "list[Artifact]" = []
    for line in ticket_text.splitlines():
        m = _COMMENT_RE.match(line.strip())
        if m and m.group(1) == agent_login:
            found.append(Artifact("tracker-comment", line.strip()))
    return found


# ── P3: branch probe ─────────────────────────────────────────────────────────


def probe_branches(
    key: str,
    list_branches: Callable[[], "Iterable[str]"],
    commits_ahead: Callable[[str], int],
) -> "list[Artifact]":
    """A branch matching the `<KEY>*` prefix (never exact-name), >=1 commit ahead."""
    found: "list[Artifact]" = []
    for branch in list_branches():
        if branch.startswith(key) and commits_ahead(branch) >= 1:
            found.append(Artifact("branch", f"{branch} is {commits_ahead(branch)} commit(s) ahead"))
    return found


def resume_candidacy(
    key: str,
    list_plan_files: Callable[[], "Iterable[str]"],
    read_file: Callable[[str], "Optional[str]"],
    agent_login: "Optional[str]",
    ticket_ok: bool,
    ticket_text: str,
    list_branches: Callable[[], "Iterable[str]"],
    commits_ahead: Callable[[str], int],
) -> "list[Artifact]":
    """OR of P1/P2/P3 — any single probe firing is enough evidence to resume."""
    return (
        probe_plan_files(key, list_plan_files, read_file)
        + probe_tracker_comments(agent_login, ticket_ok, ticket_text)
        + probe_branches(key, list_branches, commits_ahead)
    )


def build_brief(
    ticket_ok: bool,
    ticket_text: str,
    ticket_reason: "Optional[str]",
    artifacts: "list[Artifact]",
) -> str:
    """Render ticket: / artifacts: / mode: — mode is opening iff artifacts is empty."""
    lines: "list[str]" = []
    if ticket_ok:
        lines.append("ticket:")
        for tl in ticket_text.splitlines():
            lines.append(f"  {tl}")
    else:
        lines.append(f"ticket: unavailable ({ticket_reason or 'no tracker configured'})")

    if artifacts:
        lines.append("artifacts:")
        for a in artifacts:
            lines.append(f"  - {a.kind}: {a.detail}")
    else:
        lines.append("artifacts: (none)")

    mode = "resume-candidate" if artifacts else "opening"
    lines.append(f"mode: {mode}")
    return "\n".join(lines) + "\n"


# ── CLI argument parsing (pure) ──────────────────────────────────────────────

_FLAGS = {
    "--dir": "dir",
    "--key": "key",
    "--title": "title",
    "--plans-dir": "plans_dir",
    "--ticket-file": "ticket_file",
    "--ticket-unavailable": "ticket_unavailable",
}


def parse_args(argv: "list[str]") -> "dict[str, Optional[str]]":
    if not argv or argv[0] != "emit":
        raise UsageError("expected 'emit' subcommand")
    opts: "dict[str, Optional[str]]" = {v: None for v in _FLAGS.values()}
    args = argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a not in _FLAGS:
            raise UsageError(f"unknown argument: {a}")
        if i + 1 >= len(args):
            raise UsageError(f"{a} needs a value")
        opts[_FLAGS[a]] = args[i + 1]
        i += 2
    if not opts["dir"]:
        raise UsageError("--dir is required")
    if bool(opts["key"]) == bool(opts["title"]):
        raise UsageError("exactly one of --key / --title is required")
    if opts["ticket_file"] and opts["ticket_unavailable"]:
        raise UsageError("--ticket-file and --ticket-unavailable are mutually exclusive")
    return opts


# ── Host I/O (real implementations; only main() wires these in) ─────────────


def _real_read_file(path: str) -> "Optional[str]":
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _resolve_ticket(opts: "dict[str, Optional[str]]") -> "tuple[bool, str, Optional[str]]":
    if opts["ticket_unavailable"]:
        return False, "", opts["ticket_unavailable"]
    if opts["ticket_file"]:
        text = _real_read_file(opts["ticket_file"])
        if text is None:
            return False, "", f"cannot read ticket file {opts['ticket_file']}"
        return True, text, None
    return False, "", "no ticket context provided"


def _git(git_dir: str, *args: str) -> "subprocess.CompletedProcess":
    git_bin = os.environ.get("GIT_BIN", "git")
    return subprocess.run([git_bin, "-C", git_dir, *args], capture_output=True, text=True)


def _real_list_branches(git_dir: str) -> "list[str]":
    proc = _git(git_dir, "branch", "--list", "--format=%(refname:short)")
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _real_merge_base_ref(git_dir: str) -> str:
    proc = _git(git_dir, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    for candidate in ("origin/main", "main"):
        chk = _git(git_dir, "rev-parse", "--verify", "--quiet", candidate)
        if chk.returncode == 0:
            return candidate
    return "HEAD"


def _real_commits_ahead(git_dir: str, branch: str, base_ref: str) -> int:
    proc = _git(git_dir, "rev-list", "--count", f"{base_ref}..{branch}")
    if proc.returncode != 0:
        return 0
    # Deliberately unguarded: a git plumbing command emitting a non-numeric
    # count is a genuine internal failure, not a normal degrade path — let it
    # crash rather than silently report 0.
    return int(proc.stdout.strip())


def main(argv: "list[str]") -> int:
    try:
        opts = parse_args(argv)
    except UsageError as exc:
        print(f"opening.py: {exc}", file=sys.stderr)
        return 2

    if os.environ.get("CLAUDE_OPENING", "").strip().lower() == "off":
        return 3

    if opts["title"]:
        brief = build_brief(
            ticket_ok=False,
            ticket_text="",
            ticket_reason="new task (no ticket yet)",
            artifacts=[],
        )
        sys.stdout.write(brief)
        return 0

    key = opts["key"]
    assert key is not None  # parse_args guarantees exactly one of key/title
    plans_dir = os.path.expanduser(opts["plans_dir"] or "~/.claude-agent/plans")
    ticket_ok, ticket_text, ticket_reason = _resolve_ticket(opts)
    agent_login = os.environ.get("CLAUDE_AGENT_LOGIN", "").strip() or None
    git_dir = opts["dir"]
    assert git_dir is not None
    base_ref = _real_merge_base_ref(git_dir)

    artifacts = resume_candidacy(
        key=key,
        list_plan_files=lambda: sorted(_glob.glob(os.path.join(plans_dir, "*.toml"))),
        read_file=_real_read_file,
        agent_login=agent_login,
        ticket_ok=ticket_ok,
        ticket_text=ticket_text,
        list_branches=lambda: _real_list_branches(git_dir),
        commits_ahead=lambda b: _real_commits_ahead(git_dir, b, base_ref),
    )

    brief = build_brief(
        ticket_ok=ticket_ok,
        ticket_text=ticket_text,
        ticket_reason=ticket_reason,
        artifacts=artifacts,
    )
    sys.stdout.write(brief)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
