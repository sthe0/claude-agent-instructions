#!/usr/bin/env python3
"""Offline replay of `hook-guard-permission-surface.py`'s `decide_detailed` over
recorded history, so the guard's calibration is measured against REAL past
calls instead of trusted on the strength of its own unit tests alone.

Difficulty removed: a guard tuned only against hand-written fixtures can still
misfire (or under-fire) on the shapes real sessions actually produce — the
prior guard this one replaces (`hook-guard-permission-self-grant.py`) was
reverted for exactly that gap. This tool answers "what would this
guard have fired on" two ways without ever installing it live:

  transcript mode — every PreToolUse-eligible tool_use (Bash, Edit, Write) in
    every transcript under `--corpus` (or, root-only with no `--corpus`, the
    two live project roots via `lib.config_root.iter_transcripts`), run
    through `decide_detailed(..., read_file=None)`. `read_file=None` means
    G1-edit can never actually fire here — there is no live disk to read the
    base document's "before" text from for a HISTORICAL call — so an Edit or
    Write onto a live-loaded settings document is reported separately as a
    **G1 CANDIDATE**: a human-review flag, not a would-fire verdict, since
    only a person (or the live hook, with real disk access) can tell whether
    that particular edit actually touched a security-relevant key.
  git-history mode — every commit touching a settings-shaped file
    (`*settings*.json`) anywhere in this checkout, running the guard's own
    `_security_relevant_diff` predicate on the parent/child blob pair via
    `git show`, reported under the synthetic branch **G1-keys-calibration**.
    This calibrates the SAME predicate G1-edit uses against real historical
    settings edits (mostly the repo's own template, which G1-edit itself
    would never fire on live, since `is_live_settings` excludes a repo
    template) — a false-positive-rate check on the predicate in isolation,
    not a claim any of these commits would have tripped the live guard.

Every finding gets a content-derived, run-stable `id` (a short sha256 of
`branch|locator|detail`) and a `group` (`branch:normalized_target`, with the
literal home directory replaced by `~` so the grouping does not depend on
which machine produced it) — the id is what `--check-classified` cross-checks
a human classification against.

`--check-classified` never re-runs the replay: it reads the `would-fires.jsonl`
`--out` already contains (from a prior plain invocation), extracts a fenced
` ```classification ` TSV block (columns `id`, `verdict`, optionally `note`)
from the LAST assistant message of `--classification-from-transcript`,
rewrites `<out>/classification.tsv`, and exits non-zero naming any would-fire
id the TSV does not cover. `verdict` is one of the four values `intended`
(the fire is correct — e.g. the pinned `cat x > .../settings.json` G1-bash
case), `false-positive` (the guard should not have fired; feeds back into the
guard's own test suite as a new negative case), `needs-follow-up` (a G1
CANDIDATE or a calibration row a human could not resolve from the transcript
alone), `not-applicable` (a row from a corpus slice outside the current
review's scope).

This module imports `decide_detailed` — the SAME function the shipped hook's
own `main()` calls — and nothing else from that module: never its `main`,
never its `_log_fire`, never `CLAUDE_PERMISSION_GUARD_LOG`. A replay run does
not write to that log; the only files it writes are under `--out`.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
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

_PRETOOLUSE_TOOLS = frozenset({"Bash", "Edit", "Write"})
_CLASSIFICATION_FENCE_RE = re.compile(
    r"```classification\n(.*?)```", re.DOTALL,
)
_CLASSIFICATION_VERDICTS = frozenset({
    "intended", "false-positive", "needs-follow-up", "not-applicable",
})


def _parse_until(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _entry_timestamp(entry: dict) -> float | None:
    raw = entry.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _normalize_target(target: str) -> str:
    normalized = target.replace(str(Path.home()), "~")
    normalized = re.sub(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", normalized,
    )
    return normalized


def _make_row(branch: str, locator: str, detail: str) -> dict:
    group_target = _normalize_target(detail)
    group = f"{branch}:{group_target}"
    stable_id = hashlib.sha256(f"{branch}|{locator}|{detail}".encode("utf-8")).hexdigest()[:16]
    return {"id": stable_id, "branch": branch, "locator": locator, "detail": detail, "group": group}


def _iter_transcript_paths(corpus: str | None) -> list[Path]:
    if corpus:
        return sorted(Path(corpus).glob("**/*.jsonl"))
    return config_root.iter_transcripts("**/*.jsonl")


def _transcript_rows(paths: list[Path], until_ts: float | None) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
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
            if until_ts is not None and ts is not None and ts > until_ts:
                continue
            content = entry.get("message", {}).get("content") or []
            if not isinstance(content, list):
                continue
            cwd = entry.get("cwd") or ""
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

                if tool_name in ("Edit", "Write"):
                    file_path = tool_input.get("file_path")
                    if isinstance(file_path, str) and widening_targets.is_live_settings(file_path):
                        rows.append(_make_row("G1-CANDIDATE", locator, file_path))

                if tool_name in ("Bash", "Edit"):
                    try:
                        decision, branch, message = _guard.decide_detailed(
                            tool_name, tool_input, cwd, "default", None,
                        )
                    except Exception:
                        continue
                    if decision == "ask":
                        rows.append(_make_row(branch or "unknown", locator, message or ""))
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
                rows.append(_make_row("G1-keys-calibration", f"{relpath}@{sha}", relpath))
    return rows


def _write_summary(out_dir: Path, rows: list[dict], until_ts: float | None) -> None:
    by_branch: dict[str, int] = {}
    for row in rows:
        by_branch[row["branch"]] = by_branch.get(row["branch"], 0) + 1
    cutoff = (
        datetime.fromtimestamp(until_ts, tz=timezone.utc).isoformat()
        if until_ts is not None else "(none)"
    )
    lines = [
        "# Permission-guard replay summary",
        "",
        f"cutoff: {cutoff}",
        f"total would-fires: {len(rows)}",
        "",
        "| branch | count |",
        "|---|---|",
    ]
    for branch in sorted(by_branch):
        lines.append(f"| {branch} | {by_branch[branch]} |")
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
    last_assistant_text = None
    for raw in transcript_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        content = entry.get("message", {}).get("content") or []
        text = "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if text.strip():
            last_assistant_text = text
    if last_assistant_text is None:
        return None
    match = _CLASSIFICATION_FENCE_RE.search(last_assistant_text)
    if not match:
        return None
    return match.group(1).strip("\n")


def _run_check_classified(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    would_fires_path = out_dir / "would-fires.jsonl"
    if not would_fires_path.exists():
        print(f"error: {would_fires_path} does not exist — run a plain replay first", file=sys.stderr)
        return 2

    all_ids = []
    for raw in would_fires_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw:
            all_ids.append(json.loads(raw)["id"])

    if not args.classification_from_transcript:
        print("error: --check-classified requires --classification-from-transcript", file=sys.stderr)
        return 2
    tsv_text = _extract_classification_tsv(Path(args.classification_from_transcript))
    if tsv_text is None:
        print("error: no ```classification fenced TSV block found in the transcript's last assistant message", file=sys.stderr)
        return 2

    classified_ids: set[str] = set()
    tsv_lines = [ln for ln in tsv_text.splitlines() if ln.strip()]
    for line in tsv_lines[1:] if tsv_lines and tsv_lines[0].startswith("id\t") else tsv_lines:
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        row_id, verdict = fields[0].strip(), fields[1].strip()
        if verdict not in _CLASSIFICATION_VERDICTS:
            continue
        classified_ids.add(row_id)

    out_dir.joinpath("classification.tsv").write_text(
        "id\tverdict\tnote\n" + "\n".join(tsv_lines) + "\n", encoding="utf-8",
    )

    uncovered = [i for i in all_ids if i not in classified_ids]
    if uncovered:
        print("uncovered would-fire ids:", file=sys.stderr)
        for i in uncovered:
            print(f"  {i}", file=sys.stderr)
        return 1
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
