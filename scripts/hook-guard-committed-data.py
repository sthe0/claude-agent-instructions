#!/usr/bin/env python3
"""PreToolUse hook (matcher: Bash): deny a `git`/`arc` add or commit that would
put RAW PRODUCTION OR PERSONAL DATA into a shared repository.

Difficulty removed: while measuring an agent's performance, a task committed its
measurement artifacts under a personal `junk/` path in the monorepo. Six of them
were dumps of real end-user chats — users' own first messages, model replies,
chat identifiers. A security monitor flagged them and remediation meant deleting
the data out of trunk. The norm that was supposed to prevent it
(memory-global/leaves/committed-files-earn-their-place.md) sorted candidate files
on ONE axis, who finds them useful, and a personal `junk/` tree is a legitimate
answer on that axis for a file whose content must never be committed at all. The
leaf now carries the content test; this hook is the mechanism, because a norm
that has to be remembered at the moment of `git commit` is a norm that will not
fire there.

Decided PER FILE, over the files the command would actually stage or commit:
DENY as soon as ONE file satisfies BOTH:
  1. agentctl.advisor.committed_data_prefilter fires on a bounded head sample of
     it — a deliberately HIGH-RECALL, cheap check over data-payload field cues
     (`first_message`, `chat_id`, `messages`, `content`, …) plus, outside a
     data-format extension, evidence of a long free-text value. AND
  2. agentctl.advisor.judge_committed_data — a fail-open semantic model judge,
     given that sample and the file's name — confirms the values are real
     production data rather than source code, a synthetic fixture, a schema, or
     a derived aggregate.

The split is load-bearing (memory-global/leaves/regex-not-for-semantic-
classification.md): the SAME field names, shape and column headers appear in a
dump of real chats, in a hand-written fixture, and in the code that reads either
— only the values say which, and that is meaning, not syntax. The prefilter may
only widen recall; the verdict is the model's. The remediation this hook comes
from had to clear five such scripts by hand precisely because a name-level match
cannot tell a column FROM a column's contents.

Cheap on the common path: a Bash call that is not a `git`/`arc` add/commit is
rejected by a structural command parse (shlex over the command's segments) and
costs nothing else. Only a real staging command reads any file.

Cap: at most _MAX_FILES_JUDGED = 1 file is judged per invocation, and the cap
costs the gate nothing. A deny blocks the WHOLE command, not the one file, so
the first file the prefilter fires on is already enough to stop a commit that
carries data; a second call could only turn one deny into the same deny. The cap
is what keeps the wait in front of an interactive `git commit` bounded — an
unbounded per-file loop over a 400-file commit is a different failure — and it
is what lets this hook declare K = 1 to lib/hook_wiring.TIMEOUT_REQUIREMENT_CALLS,
where the whole-invocation budget is also the per-call ceiling.

Every observable failure FAILS OPEN (allow): no runner, a disabled judge, an
exhausted budget, a timeout, an unparseable answer, an unreadable file, a VCS
that will not answer, a malformed payload, any unexpected error. The hook always
exits 0; DENY is delivered through the PreToolUse permissionDecision JSON
contract, as in hook-guard-destructive-rm.py.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import judge_ledger  # noqa: E402

# judge_ledger itself must import cleanly above for this to record anything —
# it is stdlib-only (see its own module docstring) and is what every other
# import failure here needs a working ledger to be recorded against.
try:
    from agentctl import advisor  # noqa: E402
    from lib import judge_budget  # noqa: E402
    from lib import judge_latency  # noqa: E402
    from lib.host_llm import JUDGE_CHILD_ENV_VAR  # noqa: E402
except BaseException as exc:
    judge_ledger.import_failed("committed_data", f"{type(exc).__name__}: {exc}")
    raise

# Whole-invocation budget for the judge, and the floor below which a call is not
# started. `committed_data` has NO measured row in lib/judge_latency.py yet, so
# neither call_floor_s nor call_ceiling_s can be applied to it — and borrowing a
# neighbour's tail is exactly what that module exists to refuse. The rule that
# DOES apply to a judge with no row of its own is last_resort_ceiling_s(): one
# second past the slowest run this model has been seen to make on ANY judge
# prompt. It is strictly more conservative than any single row's floor, so a call
# sized by it cannot be truncated by this budget; it is also the same number
# advisor already gives the other two unmeasured judges.
#
# Both constants are re-derived, not hand-typed: the moment a sample lands and
# `committed_data` gets a measured row, they move to that row's own floor and
# ceiling and this expression stops being reachable.
_COMMITTED_DATA_JUDGE_MIN_CALL_S = judge_latency.LAST_RESORT_CEILING_S
_COMMITTED_DATA_JUDGE_BUDGET_S = _COMMITTED_DATA_JUDGE_MIN_CALL_S + 4

# See the module docstring's "Cap" paragraph: one deny blocks the whole command,
# so a second call cannot change the verdict.
_MAX_FILES_JUDGED = 1

# How much of a file's head is sampled. Enough to show what the records are;
# small enough that sampling every candidate file of a large commit stays a
# handful of short reads. The judge never sees more of the file than this.
_SAMPLE_MAX_BYTES = 4096

# Below this size a file cannot be a data dump worth the read — a chat export is
# never 80 bytes. Skipped before the sample is taken.
_FILE_MIN_BYTES = 200

# Extensions a committed data dump plausibly wears. Deliberately includes source
# and prose extensions: the remediation's own dumps had no extension convention,
# and for those the prefilter demands a long free-text value on top of the field
# cue, so an ordinary script costs nothing. Everything else — binaries, images,
# archives, notebooks — is skipped, since the head sample would be unreadable
# noise the judge cannot rule on.
_CANDIDATE_EXTENSIONS = (
    ".jsonl", ".json", ".ndjson", ".csv", ".tsv", ".txt", ".log",
    ".yaml", ".yml", ".md", ".sql", ".py", ".sh", ".tab", ".dat", "",
)

# Kill-switch for the semantic committed-data judge: set to "0" to force it off
# without a code change. Safe-by-default — unset/unrecognised leaves the judge
# ENABLED, and with it off the gate can only allow, never deny.
_COMMITTED_DATA_KILLSWITCH_ENV = "CLAUDE_COMMITTED_DATA_SEMANTIC"

# Bound on how long the VCS is given to answer "what would this command touch".
# A repository that cannot answer in this long is a repository whose commit this
# hook has no opinion about — fail open.
_VCS_QUERY_TIMEOUT_S = 10

_DENY_REASON = (
    "This command would commit raw production / personal data. That is never "
    "committed to a shared repository — a personal `junk/` tree included — "
    "whatever its usefulness: ship a derived aggregate instead (counts, "
    "quantiles, scores, lengths), leave the raw rows in this task's uncommitted "
    "evidence directory, and cite the query that regenerates them. See "
    "memory-global/leaves/committed-files-earn-their-place.md § Guidance "
    "(\"Content first\"). If this file is a synthetic fixture rather than real "
    "data, say so in the commit message and re-run."
)

# Tokens that may precede the VCS binary in a segment without changing which
# binary runs. `env` is here for `env VAR=1 git commit`; the VAR=1 assignments
# themselves are recognised by their `=` and skipped separately.
_COMMAND_PREFIXES = ("command", "sudo", "nohup", "time", "env", "exec", "builtin")

# `git`/`arc` options that consume the NEXT token, so the subcommand search does
# not mistake that token for the subcommand. `-C <path>` is the one that matters
# in practice; the rest are listed because missing one silently turns the gate off
# for that command shape.
_VCS_GLOBAL_OPTS_WITH_VALUE = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path")

# The separators that make one Bash string several commands. Split on them and
# each piece is parsed on its own, so `cd x && git commit -am wip` is seen.
_SEGMENT_SEPARATORS = (";", "&&", "||", "|", "\n", "&")

_GIT = "git"
_ARC = "arc"
_VCS_BINARIES = (_GIT, _ARC)


def _segments(command: str) -> "list[list[str]]":
    """The command string as a list of token lists, one per shell segment.

    Structural, not semantic: the separators are shell syntax, and a segment
    that will not lex (unbalanced quotes, a heredoc) is dropped rather than
    guessed at — dropping it fails open, which is this hook's posture
    everywhere."""
    if not isinstance(command, str) or not command:
        return []
    text = command
    for sep in _SEGMENT_SEPARATORS:
        text = text.replace(sep, "\n")
    out = []
    for piece in text.split("\n"):
        piece = piece.strip()
        if not piece:
            continue
        try:
            tokens = shlex.split(piece)
        except ValueError:
            continue
        if tokens:
            out.append(tokens)
    return out


def _vcs_invocation(tokens: "list[str]") -> "tuple[str, str, list[str]] | None":
    """(binary, subcommand, remaining args) if these tokens invoke `git`/`arc`
    with a subcommand, else None. Handles `/usr/bin/git`, `command git`,
    `env FOO=1 git`, and the global options that swallow their next token."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if "=" in token and not token.startswith("-"):
            index += 1  # a leading VAR=value assignment
            continue
        if token in _COMMAND_PREFIXES:
            index += 1
            continue
        break
    if index >= len(tokens):
        return None
    binary = Path(tokens[index]).name
    if binary not in _VCS_BINARIES:
        return None
    index += 1
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            return binary, token, tokens[index + 1:]
        if token in _VCS_GLOBAL_OPTS_WITH_VALUE:
            index += 2
            continue
        index += 1
    return None


def _explicit_paths(args: "list[str]") -> "list[str]":
    """The non-flag path arguments of an `add`/`commit`, with `--`'s "everything
    after me is a path" honoured and `-m <message>` excluded — a commit message
    is not a path and would otherwise be sampled as one."""
    paths = []
    index = 0
    literal = False
    while index < len(args):
        token = args[index]
        if literal:
            paths.append(token)
            index += 1
            continue
        if token == "--":
            literal = True
            index += 1
            continue
        if token in ("-m", "--message", "-F", "--file", "--author", "--date"):
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        paths.append(token)
        index += 1
    return paths


def _wants_everything(subcommand: str, args: "list[str]", paths: "list[str]") -> bool:
    """True when the command's own arguments do not name the file set — an
    `add -A`, an `add .`, a `commit -a`, or a bare `commit` over whatever is
    already staged. Those have to be asked of the VCS."""
    flags = {a for a in args if a.startswith("-")}
    if flags & {"-A", "--all", "-a", "-am", "-u", "--update"}:
        return True
    if any(f.startswith("-") and not f.startswith("--") and "a" in f[1:] for f in flags):
        return True
    if subcommand == "commit":
        return True  # a commit's file set is the index, even when paths are named
    return not paths or any(p in (".", "./") for p in paths)


def _vcs_reported_paths(binary: str, subcommand: str, cwd: "str | None") -> "list[str]":
    """Ask the VCS which files the command would touch. Any failure — a missing
    binary, a non-repository cwd, a timeout, a non-zero exit — yields no paths,
    which allows the command."""
    if binary == _GIT:
        commands = [
            ["git", "diff", "--cached", "--name-only"],
            ["git", "status", "--porcelain"],
        ] if subcommand == "commit" else [["git", "status", "--porcelain"]]
    else:
        commands = [["arc", "status", "--porcelain"], ["arc", "status"]]
    seen: "list[str]" = []
    for argv in commands:
        try:
            result = subprocess.run(
                argv, cwd=cwd or None, capture_output=True, text=True,
                timeout=_VCS_QUERY_TIMEOUT_S,
            )
        except Exception:
            continue
        if result.returncode != 0:
            continue
        for line in (result.stdout or "").splitlines():
            path = _porcelain_path(line) if "status" in argv else line.strip()
            if path and path not in seen:
                seen.append(path)
        if seen:
            break
    return seen


def _porcelain_path(line: str) -> str:
    """The path out of one `--porcelain` line (`XY path`, or `XY old -> new` for
    a rename, where the NEW name is the one that would land)."""
    body = line[3:] if len(line) > 3 else ""
    body = body.strip()
    if " -> " in body:
        body = body.split(" -> ", 1)[1]
    return body.strip().strip('"')


def _candidate_files(payload_cwd: "str | None", tokens: "list[str]") -> "list[Path]":
    """Absolute paths of the files this command would stage or commit that are
    worth sampling at all — plausible extension, present, and past the size
    floor. Order follows the command's own argument order, then the VCS's."""
    invocation = _vcs_invocation(tokens)
    if invocation is None:
        return []
    binary, subcommand, args = invocation
    if subcommand not in ("add", "commit"):
        return []
    named = _explicit_paths(args)
    paths = list(named)
    if _wants_everything(subcommand, args, named):
        paths.extend(p for p in _vcs_reported_paths(binary, subcommand, payload_cwd) if p not in paths)
    root = Path(payload_cwd) if payload_cwd else Path.cwd()
    out: "list[Path]" = []
    for raw in paths:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate in out:
            continue
        if candidate.suffix.lower() not in _CANDIDATE_EXTENSIONS:
            continue
        try:
            if not candidate.is_file() or candidate.stat().st_size < _FILE_MIN_BYTES:
                continue
        except OSError:
            continue
        out.append(candidate)
    return out


def _head_sample(path: Path) -> str:
    """The first _SAMPLE_MAX_BYTES of a file as text, or "" if it cannot be read
    or is not text at all. Decoded with errors="replace" rather than strictly:
    a mostly-text dump with one bad byte is still exactly what this gate is
    looking for."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(_SAMPLE_MAX_BYTES)
    except OSError:
        return ""
    if b"\x00" in raw:
        return ""  # binary; the judge has nothing to read
    return raw.decode("utf-8", errors="replace")


def decide(payload: dict, *, runner: Callable | None = None) -> "dict | None":
    """Core decision. Returns the PreToolUse deny payload to print, or None to
    allow.

    The whole invocation runs under one _COMMITTED_DATA_JUDGE_BUDGET_S deadline;
    each judge call gets whatever remains, capped at that budget (this hook's OWN
    ceiling, not advisor._COMMITTED_DATA_LAST_RESORT_TIMEOUT_S, which is the
    default for a caller with no budget at all). Once the remainder can no longer
    fund a call (_COMMITTED_DATA_JUDGE_MIN_CALL_S), judging stops and the command
    is allowed — fail-open, the same posture as every other unreachable-judge
    path here.

    ``runner`` is injected straight into advisor.judge_committed_data (None ->
    that judge fails open to False, never denies).

    The deny payload is BUILT HERE, in the same scope as the judge call, instead
    of in a separate emitter: the repo's mechanical audit (crutch-inventory.py,
    tests/test_no_semantic_unguarded.py) reads guard and sink per SCOPE, so a
    deny split from its fail-open judge reads as unguarded."""
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input") or {}).get("command")
    cwd = payload.get("cwd")
    enabled = os.environ.get(_COMMITTED_DATA_KILLSWITCH_ENV) != "0"
    files: "list[Path]" = []
    for tokens in _segments(command):
        for candidate in _candidate_files(cwd, tokens):
            if candidate not in files:
                files.append(candidate)
    if not files:
        return None
    budget = judge_budget.JudgeBudget(
        _COMMITTED_DATA_JUDGE_BUDGET_S, _COMMITTED_DATA_JUDGE_MIN_CALL_S, clock=time.monotonic
    )
    judged = 0
    for path in files:
        if judged >= _MAX_FILES_JUDGED:
            break
        sample = _head_sample(path)
        prefilter_fired = advisor.committed_data_prefilter(sample, path.name)
        judge_ledger.entered("committed_data", prefilter_fired=prefilter_fired)
        if not prefilter_fired:
            continue  # cheap common path: this file carries no data-payload cue
        remaining_before_call, call_timeout = budget.remaining_and_timeout(
            _COMMITTED_DATA_JUDGE_BUDGET_S
        )
        if call_timeout is None:
            judge_ledger.decided(
                "committed_data", stage="budget", verdict=False,
                reason="budget exhausted before call (fail-open)",
                remaining=remaining_before_call, threshold=None,
                ceiling=_COMMITTED_DATA_JUDGE_BUDGET_S,
            )
            break  # budget exhausted — fail open
        judged += 1
        fires, _reason = advisor.judge_committed_data(
            sample, runner, filename=str(path), enabled=enabled, timeout=call_timeout,
            remaining=remaining_before_call, ceiling=_COMMITTED_DATA_JUDGE_BUDGET_S,
        )
        if fires:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"{_DENY_REASON} Offending file: {path}.",
                }
            }
    return None


def main() -> int:
    if os.environ.get(JUDGE_CHILD_ENV_VAR):
        return 0  # a sandboxed judge subprocess, not a real user turn — allow, no opinion
    judge_ledger.hook_start("committed_data")
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    judge_ledger.source_from_payload(payload)

    try:
        decision = decide(payload, runner=advisor.subprocess_runner)
        has_directive = decision is not None
        judge_ledger.final(has_directive=has_directive)
        emit_ok = True
        try:
            if has_directive:
                print(json.dumps(decision))
        except Exception:
            emit_ok = False
        judge_ledger.emitted(ok=emit_ok, had_directive=has_directive)
    except Exception as exc:
        judge_ledger.discarded(reason=repr(exc))
        return 0  # fail-open — a hook must never wedge a commit
    return 0


if __name__ == "__main__":
    sys.exit(main())
