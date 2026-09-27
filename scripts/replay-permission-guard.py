#!/usr/bin/env python3
"""Offline replay of `hook-guard-permission-surface.py`'s `decide_detailed` over
recorded history, so the guard's calibration is measured against REAL past
calls instead of trusted on the strength of its own unit tests alone.

Difficulty removed: a guard tuned only against hand-written fixtures can still
misfire (or under-fire) on the shapes real sessions actually produce — the
prior guard this one replaces (`hook-guard-permission-self-grant.py`) was
reverted for exactly that gap. This tool answers "what would this
guard have fired on" two ways without ever installing it live:

  transcript mode — every PreToolUse-eligible tool_use (Bash, Edit, Write,
    MultiEdit, NotebookEdit — every tool `hook-guard-permission-surface.py`
    is itself wired to receive) in every transcript under `--corpus` (or,
    root-only with no `--corpus`, the two live project roots via
    `lib.config_root.iter_transcripts`), run through
    `decide_detailed(..., read_file=None)`. `read_file=None` means G1-edit
    (and its MultiEdit counterpart) can never actually fire here — there is
    no live disk to read the base document's "before" text from for a
    HISTORICAL call — so an Edit, Write, or MultiEdit onto a live-loaded
    settings document is reported separately as a **G1 CANDIDATE**: a
    human-review flag, not a would-fire verdict, since only a person (or the
    live hook, with real disk access) can tell whether that particular edit
    actually touched a security-relevant key.
  git-history mode — every commit touching a settings-shaped file
    (`*settings*.json`) anywhere in this checkout, running the guard's own
    `_security_relevant_diff` predicate on the parent/child blob pair via
    `git show`, reported under the synthetic branch **G1-keys-calibration**.
    This calibrates the SAME predicate G1-edit uses against real historical
    settings edits (mostly the repo's own template, which G1-edit itself
    would never fire on live, since `is_live_settings` excludes a repo
    template) — a false-positive-rate check on the predicate in isolation,
    not a claim any of these commits would have tripped the live guard.

`--until` cuts off both modes by event time. Given explicitly, it parses as an
ISO-8601 timestamp. Omitted, it defaults to the tool's own LAUNCH TIME
(`_launch_timestamp()`, captured once per run) — never "no cutoff" — so a
replay run is always bounded to "everything up to now" and two runs launched
minutes apart naturally see a growing, not identical, corpus.

Every finding gets a content-derived, run-stable `id` (a short sha256 of
`branch|locator|detail` — unaffected by `--until` or by the `source`/`day`/
`commit` fields below, so ids stay stable across runs over the same corpus)
and a `group` (`branch:normalized_target`, with the literal home directory
replaced by `~` so the grouping does not depend on which machine produced
it) — the id and the group are what `--check-classified` cross-checks a human
classification against. Each row also carries `source` (`"transcript"` or
`"git"`) and either `day` (the event's session-day, `YYYY-MM-DD` UTC — a
transcript-mode row, or `"unknown"` when the source entry carried no
parseable timestamp) or `commit` (the commit sha — a git-history-mode row).

`summary.md` reports, from these rows: a per-branch-per-session-day count
table (transcript-sourced rows), a per-commit count table for the
`G1-keys-calibration` branch (git-sourced rows), and a group list (group id,
its branch, its member count) — the group list is what a human classifies
from, since classifying by group is normally cheaper than classifying every
individual id.

`--check-classified` never re-runs the replay: it reads the `would-fires.jsonl`
`--out` already contains (from a prior plain invocation), extracts a fenced
` ```classification ` TSV block (columns `key`, `class`, optionally `reason`)
from the LAST assistant `message.id` of `--classification-from-transcript`
(joining the text of every transcript entry sharing that message.id, in
transcript order — the harness splits one logical assistant turn across
several JSONL entries), and rewrites `<out>/classification.tsv`. A TSV row's
`key` is EITHER a would-fire's own `id` OR a `group` id: a group-keyed row
classifies every member of that group at once, and a row keyed by an
individual id OVERRIDES its group's row for that one would-fire. `class` must
be one of exactly four values — `true-positive` (the fire is correct, e.g.
the pinned `cat x > .../settings.json` G1-bash case), `false-positive:ordinary`
(the guard should not have fired on ordinary work; feeds back into the
guard's own negative test suite), `false-positive:settings-routine` (a
`G1-keys-calibration` row flagging a routine, non-security settings change;
feeds into narrowing G1's security-key list specifically), `candidate-benign`
(a `G1-CANDIDATE` or another row a human judged benign but could not fully
resolve, or one outside the current review's scope). A TSV row naming any
other class value makes `--check-classified` exit non-zero, naming that row,
WITHOUT computing coverage. Once every row's class is valid, any would-fire
neither covered by its own id nor by its group's row is reported the same
way (exit non-zero, ids named). On full coverage, `--check-classified` prints
the count per class, resolved per would-fire after group expansion and
id-row overrides, and exits 0.

This module imports `decide_detailed` and `_security_relevant_diff` from the
guard module — the SAME two functions the shipped hook's own `main()` (for
the former) and this tool's git-history mode (for the latter) rely on — and
nothing else: never `main`, never `_log_fire`, never
`CLAUDE_PERMISSION_GUARD_LOG`. A replay run does not write to that log; the
only files it writes are under `--out`.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))
from lib import config_root, widening_targets  # noqa: E402

_GUARD_SPEC = importlib.util.spec_from_file_location(
    "hook_guard_permission_surface", _SCRIPTS_DIR / "hook-guard-permission-surface.py",
)
_guard = importlib.util.module_from_spec(_GUARD_SPEC)
_GUARD_SPEC.loader.exec_module(_guard)

_PRETOOLUSE_TOOLS = frozenset({"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"})
_CLASSIFICATION_FENCE_RE = re.compile(
    r"```classification\n(.*?)```", re.DOTALL,
)
_CLASSIFICATION_CLASSES = frozenset({
    "true-positive", "false-positive:ordinary", "false-positive:settings-routine", "candidate-benign",
})


def _launch_timestamp() -> datetime:
    """The replay run's own launch time — the default `--until` cutoff. A
    separate function (rather than inlining `datetime.now()`) so a test can
    monkeypatch it to pin a deterministic default without faking `--until`."""
    return datetime.now(timezone.utc)


def _parse_until(value: str | None) -> float:
    """Always returns a concrete cutoff: the parsed `--until` value, or —
    when `--until` is omitted — the launch timestamp. Never `None`; the
    internal per-mode row-collectors below still accept `None` as "no
    cutoff" for direct/unit-test use, but the CLI path never passes one."""
    if value:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    return _launch_timestamp().timestamp()


def _entry_timestamp(entry: dict) -> float | None:
    raw = entry.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _day_from_ts(ts: float | None) -> str:
    if ts is None:
        return "unknown"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _normalize_target(target: str) -> str:
    normalized = target.replace(str(Path.home()), "~")
    normalized = re.sub(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", normalized,
    )
    return normalized


_TOUCHED_KEY_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:')


def _touched_keys(tool_input: dict) -> list[str]:
    """Sorted, deduped `"key":` names appearing in an Edit's
    `old_string`/`new_string` or a Write's `content` — a regex scan, not a
    full JSON parse, since an Edit's old/new fragments are rarely
    standalone-valid JSON on their own (round-2 should-fix S7: a
    G1-CANDIDATE group used to carry only the file path, collapsing every
    edit to the same live settings document into one group regardless of
    which keys it actually touched)."""
    text = "".join(str(tool_input.get(field, "")) for field in ("old_string", "new_string", "content"))
    return sorted(set(_TOUCHED_KEY_RE.findall(text)))


def _bash_group_shape(command: str) -> str:
    """`program:sorted,flags` — a single-line, argument-value-free shape so
    two Bash fires of the same program and flags (differing only in a
    free-text argument, e.g. two `claude -p '...'` calls with different
    briefs) collapse into one group, and a command that itself contains a
    literal newline (a crontab heredoc) can never leak one into the group
    key (round-2 should-fix S7)."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    stripped = widening_targets.strip_wrappers(tokens) or tokens
    program = widening_targets.program_name(stripped[0]).casefold() if stripped else "?"
    flags = sorted({tok for tok in stripped[1:] if tok.startswith("-")})
    return f"{program}:{','.join(flags)}"


def _target_path(tool_name: str, tool_input: dict) -> str:
    """The single file this tool call names, for grouping/candidate-detection
    purposes -- `NotebookEdit` carries it under `notebook_path`, every other
    file-shaped tool (`Edit`/`Write`/`MultiEdit`) under `file_path`; `Bash`
    has no single target and returns `""` (its own group key comes from
    `_bash_group_shape` instead, via `_group_for`)."""
    key = "notebook_path" if tool_name == "NotebookEdit" else "file_path"
    value = tool_input.get(key)
    return value if isinstance(value, str) else ""


def _group_for(branch: str, tool_name: str | None, tool_input: dict, target: str) -> str:
    """The normalized grouping key — deliberately NOT derived from the
    human-readable `detail` message (round-2 should-fix S7): a Bash fire
    groups on program+flags (`_bash_group_shape`), a G1-CANDIDATE edit/write
    groups on the live document plus the keys it actually touched
    (`_touched_keys`), and everything else groups on the normalized target
    path alone."""
    if tool_name == "Bash":
        command = tool_input.get("command")
        shape = _bash_group_shape(command) if isinstance(command, str) else "?"
        return f"{branch}:{shape}"
    if branch == "G1-CANDIDATE":
        keys = ",".join(_touched_keys(tool_input))
        suffix = f":{keys}" if keys else ""
        return f"{branch}:{_normalize_target(target)}{suffix}"
    return f"{branch}:{_normalize_target(target)}"


def _make_row(
    branch: str, locator: str, detail: str, *, source: str, group: str,
    day: str | None = None, commit: str | None = None,
) -> dict:
    """The `id` is a hash of `branch|locator|detail` alone — `source`/`day`/
    `commit` never enter it, so ids stay stable across runs over the same
    corpus regardless of when the run happened. `group` is supplied by the
    caller (`_group_for` for a transcript row) rather than derived here, so
    the grouping shape can differ by branch/tool without `_make_row` itself
    knowing about tool_input."""
    stable_id = hashlib.sha256(f"{branch}|{locator}|{detail}".encode("utf-8")).hexdigest()[:16]
    row = {
        "id": stable_id, "branch": branch, "locator": locator, "detail": detail,
        "group": group, "source": source,
    }
    if day is not None:
        row["day"] = day
    if commit is not None:
        row["commit"] = commit
    return row


def _iter_transcript_paths(corpus: str | None) -> list[Path]:
    if corpus:
        return sorted(Path(corpus).glob("**/*.jsonl"))
    return config_root.iter_transcripts("**/*.jsonl")


def _transcript_rows(paths: list[Path], until_ts: float | None) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        try:
            # A harness transcript can carry a multi-byte character cut mid-sequence.
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_no, raw in enumerate(lines, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("type") != "assistant":
                continue
            ts = _entry_timestamp(entry)
            if until_ts is not None and ts is None:
                # round-2 nit: an unparseable/absent timestamp used to slip
                # past every cutoff unconditionally, so the would-fire set
                # could grow across runs sharing the same fixed --until.
                continue
            if until_ts is not None and ts is not None and ts > until_ts:
                continue
            content = entry.get("message", {}).get("content") or []
            if not isinstance(content, list):
                continue
            cwd = entry.get("cwd") or ""
            day = _day_from_ts(ts)
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_name = block.get("name")
                if tool_name not in _PRETOOLUSE_TOOLS:
                    continue
                tool_input = block.get("input")
                if not isinstance(tool_input, dict):
                    continue
                locator = f"{path}:{line_no}:{block.get('id', '')}"

                if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                    file_path = _target_path(tool_name, tool_input)
                    if file_path and widening_targets.is_live_settings(file_path):
                        rows.append(_make_row(
                            "G1-CANDIDATE", locator, file_path, source="transcript", day=day,
                            group=_group_for("G1-CANDIDATE", tool_name, tool_input, file_path),
                        ))

                if tool_name in _PRETOOLUSE_TOOLS:
                    try:
                        decision, branch, message = _guard.decide_detailed(
                            tool_name, tool_input, cwd, "default", None,
                        )
                    except Exception:
                        continue
                    if decision == "ask":
                        branch = branch or "unknown"
                        target = _target_path(tool_name, tool_input)
                        rows.append(_make_row(
                            branch, locator, message or "", source="transcript", day=day,
                            group=_group_for(branch, tool_name, tool_input, target),
                        ))
    return rows


def _git_show(repo_root: Path, rev: str, relpath: str) -> str | None:
    res = subprocess.run(
        ["git", "show", f"{rev}:{relpath}"],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    if res.returncode != 0:
        return None
    return res.stdout


def _git_history_rows(repo_root: Path, until_ts: float | None) -> list[dict]:
    ls_files = subprocess.run(
        ["git", "ls-files", "*settings*.json"],
        cwd=str(repo_root), capture_output=True, text=True, check=True,
    )
    relpaths = sorted(p for p in ls_files.stdout.splitlines() if p.strip())

    rows: list[dict] = []
    for relpath in relpaths:
        log = subprocess.run(
            ["git", "log", "--format=%H %ct", "--follow", "--", relpath],
            cwd=str(repo_root), capture_output=True, text=True, check=True,
        )
        for line in log.stdout.splitlines():
            if not line.strip():
                continue
            sha, commit_ts = line.split(maxsplit=1)
            if until_ts is not None and int(commit_ts) > until_ts:
                continue
            parent = subprocess.run(
                ["git", "rev-parse", f"{sha}^"],
                cwd=str(repo_root), capture_output=True, text=True,
            )
            if parent.returncode != 0:
                continue  # root commit — no parent blob to diff against
            parent_sha = parent.stdout.strip()
            old_text = _git_show(repo_root, parent_sha, relpath)
            new_text = _git_show(repo_root, sha, relpath)
            if old_text is None or new_text is None:
                continue
            if _guard._security_relevant_diff(old_text, new_text):
                rows.append(_make_row(
                    "G1-keys-calibration", f"{relpath}@{sha}", relpath, source="git", commit=sha,
                    group=_group_for("G1-keys-calibration", None, {}, relpath),
                ))
    return rows


def _write_summary(out_dir: Path, rows: list[dict], until_ts: float) -> None:
    day_counts: dict[tuple[str, str], int] = {}
    commit_counts: dict[tuple[str, str], int] = {}
    group_info: dict[str, dict] = {}
    for row in rows:
        if row.get("source") == "git":
            key = (row["branch"], row.get("commit", "?"))
            commit_counts[key] = commit_counts.get(key, 0) + 1
        else:
            key = (row["branch"], row.get("day", "unknown"))
            day_counts[key] = day_counts.get(key, 0) + 1
        group = row["group"]
        info = group_info.setdefault(group, {"branch": row["branch"], "count": 0})
        info["count"] += 1

    cutoff = datetime.fromtimestamp(until_ts, tz=timezone.utc).isoformat()
    lines = [
        "# Permission-guard replay summary",
        "",
        f"cutoff: {cutoff}",
        f"total would-fires: {len(rows)}",
        "",
        "## Counts by branch and session-day",
        "",
        "| branch | day | count |",
        "|---|---|---|",
    ]
    for branch, day in sorted(day_counts):
        lines.append(f"| {branch} | {day} | {day_counts[(branch, day)]} |")
    lines += [
        "",
        "## Counts by branch and commit (G1-keys-calibration)",
        "",
        "| branch | commit | count |",
        "|---|---|---|",
    ]
    for branch, commit in sorted(commit_counts):
        lines.append(f"| {branch} | {commit} | {commit_counts[(branch, commit)]} |")
    lines += [
        "",
        "## Groups",
        "",
        "| group | branch | members |",
        "|---|---|---|",
    ]
    for group in sorted(group_info):
        info = group_info[group]
        lines.append(f"| {group} | {info['branch']} | {info['count']} |")
    out_dir.joinpath("summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_replay(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    until_ts = _parse_until(args.until)

    rows = _transcript_rows(_iter_transcript_paths(args.corpus), until_ts)
    if not args.corpus:
        rows.extend(_git_history_rows(_SCRIPTS_DIR.parent, until_ts))

    with out_dir.joinpath("would-fires.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    _write_summary(out_dir, rows, until_ts)
    return 0


def _extract_classification_tsv(transcript_path: Path) -> str | None:
    """The LAST assistant `message.id`'s text, joining every transcript entry
    that shares it (in scan order) — the harness can split one logical
    assistant turn across several JSONL `type: "assistant"` entries, each
    contributing its own text block(s); only entries with an `id` AND
    non-empty text participate, so a trailing tool-only entry never becomes
    "last" on an empty text technicality."""
    texts_by_message_id: dict[str, list[str]] = {}
    last_message_id: str | None = None
    for raw in transcript_path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message") or {}
        message_id = message.get("id")
        if not isinstance(message_id, str):
            continue
        content = message.get("content") or []
        if not isinstance(content, list):
            continue
        text = "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if not text.strip():
            continue
        texts_by_message_id.setdefault(message_id, []).append(text)
        last_message_id = message_id
    if last_message_id is None:
        return None
    full_text = "".join(texts_by_message_id[last_message_id])
    match = _CLASSIFICATION_FENCE_RE.search(full_text)
    if not match:
        return None
    return match.group(1).strip("\n")


def _run_check_classified(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    would_fires_path = out_dir / "would-fires.jsonl"
    if not would_fires_path.exists():
        print(f"error: {would_fires_path} does not exist — run a plain replay first", file=sys.stderr)
        return 2

    would_fires = [
        json.loads(raw) for raw in would_fires_path.read_text(encoding="utf-8").splitlines() if raw.strip()
    ]
    id_set = {wf["id"] for wf in would_fires}
    group_set = {wf["group"] for wf in would_fires}

    if not args.classification_from_transcript:
        print("error: --check-classified requires --classification-from-transcript", file=sys.stderr)
        return 2
    tsv_text = _extract_classification_tsv(Path(args.classification_from_transcript))
    if tsv_text is None:
        print("error: no ```classification fenced TSV block found in the transcript's last assistant message", file=sys.stderr)
        return 2

    tsv_lines = [ln for ln in tsv_text.splitlines() if ln.strip()]
    data_lines = tsv_lines[1:] if tsv_lines and tsv_lines[0].startswith("key\t") else tsv_lines

    out_dir.joinpath("classification.tsv").write_text(
        "key\tclass\treason\n" + "\n".join(data_lines) + "\n", encoding="utf-8",
    )

    id_rows: dict[str, str] = {}
    group_rows: dict[str, str] = {}
    invalid_rows: list[str] = []
    unknown_keys: list[str] = []
    for line in data_lines:
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        key, cls = fields[0].strip(), fields[1].strip()
        if cls not in _CLASSIFICATION_CLASSES:
            invalid_rows.append(line)
            continue
        if key in id_set:
            id_rows[key] = cls
        elif key in group_set:
            group_rows[key] = cls
        else:
            # round-2 nit: a key matching neither a real id nor a real group
            # used to be silently accepted as a group row, so a typo only
            # surfaced indirectly, as an "uncovered" id.
            unknown_keys.append(key)

    if invalid_rows:
        print("classification rows with an unrecognized class value:", file=sys.stderr)
        for line in invalid_rows:
            print(f"  {line}", file=sys.stderr)
        return 2

    if unknown_keys:
        print("classification rows whose key matches neither a would-fire id nor a group:", file=sys.stderr)
        for key in unknown_keys:
            print(f"  {key}", file=sys.stderr)
        return 2

    uncovered: list[str] = []
    counts: dict[str, int] = {}
    for wf in would_fires:
        if wf["id"] in id_rows:
            cls = id_rows[wf["id"]]
        elif wf["group"] in group_rows:
            cls = group_rows[wf["group"]]
        else:
            uncovered.append(wf["id"])
            continue
        counts[cls] = counts.get(cls, 0) + 1

    if uncovered:
        print("uncovered would-fire ids:", file=sys.stderr)
        for i in uncovered:
            print(f"  {i}", file=sys.stderr)
        return 1

    for cls in sorted(counts):
        print(f"{cls}: {counts[cls]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--corpus", default=None)
    parser.add_argument("--until", default=None)
    parser.add_argument("--check-classified", action="store_true")
    parser.add_argument("--classification-from-transcript", default=None)
    args = parser.parse_args(argv)

    if args.check_classified:
        return _run_check_classified(args)
    return _run_replay(args)


if __name__ == "__main__":
    sys.exit(main())
