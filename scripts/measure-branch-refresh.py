#!/usr/bin/env python3
"""Measure what branch-refresh conflicts cost a landing, and check the squash decision.

Difficulty removed: whether to land by squash was argued on impression ("we
spend a lot of time on conflicts"), while no record held the minutes a
conflict costs. This script measures the inputs of a rule fixed in advance
(adopt a squash landing only if it saves at least THRESHOLD_MIN minutes per
landing and the `Task:` trailers survive the squash) and checks that a
decision document's numbers and its `Decision:` line follow from history.

Definitions (over the first-parent chain of --rev, restricted to commits
whose committer date falls in [--since, --until], inclusive):

* PR merge: a merge commit made by the hosting service's web merge
  (committer e-mail WEB_MERGE_EMAIL). It lands a branch, so it is a landing
  candidate, never a refresh.
* Refresh merge: every other merge commit, i.e. the trunk merged into a
  branch.
* Conflicting refresh: a refresh merge whose `git show --remerge-diff` shows
  conflict markers (git >= 2.36), or whose recomputed merge with
  `git merge-tree` does (older git).
* Landing: one distinct landing key among the non-refresh commits; the key is
  the `Task:` trailer value, else the commit's own hash (fast-forward landing
  leaves no other boundary in history).
* Resolution sample: in a session transcript, a tool result carrying
  `CONFLICT (` for a path that exists in --rev's tree, and the minutes from
  it to the session's next `git commit` call.

With fast-forward landing the first-parent chain follows the branch landed
last, so branches landed earlier, and their refreshes, fall off it. The
script therefore also counts the whole reachable graph in the window
(`graph_*` figures) as a cross-check; its saving uses the distinct `Task:`
values as the landing count, the fewest landings, so it is an upper bound.

The expected saving per landing is conflicting refreshes times the median
sample, divided by landings.

Exit codes: 0 ok; 1 a number or the trailer line disagrees; 2 usage or git
error; 3 the document's Decision contradicts the rule for its own numbers.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

THRESHOLD_MIN = 5  # the rediscovery-threshold-min coordination constant
WEB_MERGE_EMAIL = "noreply@github.com"
CONFLICT_MARKERS = ("+<<<<<<<", "-<<<<<<<")
ROUNDING = 0.05
EXIT_MISMATCH, EXIT_USAGE, EXIT_CONTRADICTS = 1, 2, 3

LINE_WINDOW = "Window: "
LINE_REVISION = "Revision: "
LINE_LANDINGS = "Landings: "
LINE_REFRESHES = "Refresh merges: "
LINE_CONFLICTING = "Conflicting refreshes: "
LINE_SAMPLE = "- Sample (min): "
LINE_MEDIAN = "Median resolution (min): "
LINE_SAVED = "Saved per landing (min): "
LINE_THRESHOLD = "Threshold (min): "
LINE_TRAILERS = "Task trailers survive squash: "
LINE_DECISION = "Decision: "
LINE_GRAPH_REFRESHES = "Whole-graph refresh merges: "
LINE_GRAPH_CONFLICTING = "Whole-graph conflicting refreshes: "
LINE_GRAPH_TASKS = "Whole-graph Task values: "
LINE_GRAPH_SAVED = "Whole-graph saved per landing, upper bound (min): "


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


@dataclass
class Commit:
    sha: str
    parents: list[str]
    date: str
    committer_email: str
    task: str

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def landing_key(self) -> str:
        return self.task or self.sha


@dataclass
class GitFigures:
    revision: str
    landings: int
    refresh_merges: int
    conflicting_refreshes: list[str] = field(default_factory=list)
    task_values: int = 0


def window_commits(repo: Path, rev: str, since: str, until: str,
                   first_parent: bool = True) -> list[Commit]:
    fmt = ("%H%x1f%P%x1f%cs%x1f%ce%x1f"
           "%(trailers:key=Task,valueonly,separator=%x2C)%x1e")
    scope = ["--first-parent"] if first_parent else []
    commits = []
    for record in git(repo, "log", *scope, f"--format={fmt}", rev).split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, parents, date, email, task = record.split("\x1f")
        if since <= date <= until:
            commits.append(Commit(sha, parents.split(), date, email, task.strip()))
    return commits


def _git_version(repo: Path) -> tuple[int, int]:
    words = git(repo, "--version").split()
    major, minor = words[2].split(".")[:2]
    return int(major), int(minor)


def has_conflict(repo: Path, merge: Commit, remerge_diff: bool) -> bool:
    if remerge_diff:
        out = git(repo, "show", "--remerge-diff", "--format=", merge.sha)
        return any(line.startswith(CONFLICT_MARKERS) for line in out.splitlines())
    base = git(repo, "merge-base", merge.parents[0], merge.parents[1]).strip()
    out = git(repo, "merge-tree", base, merge.parents[0], merge.parents[1])
    return any(line.startswith(CONFLICT_MARKERS) for line in out.splitlines())


def git_figures(repo: Path, rev: str, since: str, until: str,
                first_parent: bool = True) -> GitFigures:
    revision = git(repo, "rev-parse", rev).strip()
    commits = window_commits(repo, revision, since, until, first_parent)
    refreshes = [c for c in commits
                 if c.is_merge and c.committer_email != WEB_MERGE_EMAIL]
    refresh_shas = {c.sha for c in refreshes}
    landings = {c.landing_key for c in commits if c.sha not in refresh_shas}
    remerge = _git_version(repo) >= (2, 36)
    conflicting = [c.sha for c in refreshes if has_conflict(repo, c, remerge)]
    tasks = {c.task for c in commits if c.task and c.sha not in refresh_shas}
    return GitFigures(revision, len(landings), len(refreshes), conflicting, len(tasks))


# --- transcripts -----------------------------------------------------------

def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text_of(part.get("content", part.get("text", "")))
                         if isinstance(part, dict) else str(part)
                         for part in content)
    return ""


def _conflict_paths(text: str) -> list[str]:
    paths = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("CONFLICT (") and " in " in line:
            paths.append(line.rsplit(" in ", 1)[1].strip())
    return paths


def _is_commit_call(command: str) -> bool:
    words = command.replace(";", " ").replace("&&", " ").split()
    return any(words[i] == "git" and "commit" in words[i + 1:i + 6]
               for i in range(len(words) - 1))


def _events(path: Path):
    """Yield (timestamp, kind, payload) from one JSONL transcript."""
    data = path.read_bytes()
    if b"CONFLICT (" not in data:
        return
    for raw in data.decode("utf-8", errors="replace").splitlines():
        if "CONFLICT (" not in raw and "commit" not in raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        stamp = entry.get("timestamp")
        message = entry.get("message")
        if not stamp or not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use":
                command = (part.get("input") or {}).get("command", "")
                if isinstance(command, str) and _is_commit_call(command):
                    yield stamp, "commit", command
            elif part.get("type") == "tool_result":
                text = _text_of(part.get("content"))
                if "CONFLICT (" in text:
                    yield stamp, "conflict", text


def _parse_time(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def transcript_samples(agent_home: Path, repo: Path, rev: str,
                       since: str, until: str) -> list[float]:
    """Minutes from a conflict on a path of --rev's tree to the next commit."""
    tree = set(git(repo, "ls-tree", "-r", "--name-only", rev).splitlines())
    samples = []
    earliest = datetime.fromisoformat(since).timestamp()
    for path in sorted((agent_home / "projects").rglob("*.jsonl")):
        if path.stat().st_mtime < earliest:
            continue
        pending = None
        for stamp, kind, payload in _events(path):
            if not since <= stamp[:10] <= until:
                continue
            if kind == "conflict" and pending is None:
                if any(p in tree for p in _conflict_paths(payload)):
                    pending = _parse_time(stamp)
            elif kind == "commit" and pending is not None:
                minutes = (_parse_time(stamp) - pending).total_seconds() / 60
                samples.append(round(minutes, 1))
                pending = None
    return sorted(samples)


# --- the rule and the document -----------------------------------------------

def median_of(samples: list[float]) -> float | None:
    return statistics.median(samples) if samples else None


def saving(conflicting: int, median: float | None, landings: int) -> float:
    if median is None or landings == 0:
        return 0.0
    return conflicting * median / landings


def rule_decision(saved: float, threshold: float, trailers_survive: bool,
                  median: float | None) -> str:
    if median is None:
        return "reject"
    return "adopt" if saved >= threshold and trailers_survive else "reject"


def check_trailers() -> bool:
    """Squash two `Task:`-trailered commits and parse the result's trailers."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        env_args = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")

        def run(*args: str, stdin: str | None = None) -> str:
            proc = subprocess.run(["git", *env_args, "-C", str(repo), *args],
                                  input=stdin, capture_output=True, text=True,
                                  check=True)
            return proc.stdout

        run("init", "-q", "-b", "main")
        run("commit", "-q", "--allow-empty", "-m", "base")
        run("switch", "-q", "-c", "topic")
        for n, task in ((1, "task-a"), (2, "task-b")):
            (repo / f"f{n}").write_text(str(n))
            run("add", f"f{n}")
            run("commit", "-q", "-m", f"change {n}\n\nTask: {task}")
        bodies = run("log", "--reverse", "--format=%B%x00", "main..topic")
        messages = [m.strip() for m in bodies.split("\x00") if m.strip()]
        subjects = "\n".join(m.splitlines()[0] for m in messages)
        # Only a message's last paragraph holds trailers, so each is parsed alone.
        trailer_lines = dict.fromkeys(
            line for m in messages
            for line in run("interpret-trailers", "--parse", stdin=m).splitlines())
        trailers = "\n".join(trailer_lines)
        message = run("interpret-trailers", "--trim-empty",
                      stdin=f"Squash topic\n\n{subjects}\n\n{trailers}")
        run("switch", "-q", "main")
        run("merge", "-q", "--squash", "topic")
        run("commit", "-q", "-m", message)
        parsed = run("log", "-1", "--format=%B").strip()
        found = run("interpret-trailers", "--parse", stdin=parsed)
        values = {line.split(":", 1)[1].strip() for line in found.splitlines()
                  if line.startswith("Task:")}
        return values == {"task-a", "task-b"}


@dataclass
class Document:
    window: tuple[str, str]
    revision: str
    landings: int
    refresh_merges: int
    conflicting: int
    samples: list[float]
    median: float | None
    saved: float
    threshold: float
    trailers_survive: bool
    decision: str
    graph_refreshes: int
    graph_conflicting: int
    graph_tasks: int
    graph_saved: float


def _value(lines: list[str], prefix: str) -> str:
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    raise ValueError(f"missing line {prefix!r}")


def parse_document(text: str) -> Document:
    lines = text.splitlines()
    since, until = _value(lines, LINE_WINDOW).split("..")
    median_text = _value(lines, LINE_MEDIAN)
    return Document(
        window=(since.strip(), until.strip()),
        revision=_value(lines, LINE_REVISION),
        landings=int(_value(lines, LINE_LANDINGS)),
        refresh_merges=int(_value(lines, LINE_REFRESHES)),
        conflicting=int(_value(lines, LINE_CONFLICTING)),
        samples=[float(line[len(LINE_SAMPLE):].split()[0])
                 for line in lines if line.startswith(LINE_SAMPLE)],
        median=None if median_text == "unmeasurable" else float(median_text),
        saved=float(_value(lines, LINE_SAVED)),
        threshold=float(_value(lines, LINE_THRESHOLD)),
        trailers_survive=_value(lines, LINE_TRAILERS) == "yes",
        decision=_value(lines, LINE_DECISION),
        graph_refreshes=int(_value(lines, LINE_GRAPH_REFRESHES)),
        graph_conflicting=int(_value(lines, LINE_GRAPH_CONFLICTING)),
        graph_tasks=int(_value(lines, LINE_GRAPH_TASKS)),
        graph_saved=float(_value(lines, LINE_GRAPH_SAVED)),
    )


def _close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= ROUNDING


def check_document(doc: Document, repo: Path,
                   agent_home: Path | None) -> tuple[int, list[str]]:
    problems = []
    figures = git_figures(repo, doc.revision, *doc.window)
    graph = git_figures(repo, doc.revision, *doc.window, first_parent=False)
    for name, stated, actual in (
            ("landings", doc.landings, figures.landings),
            ("refresh merges", doc.refresh_merges, figures.refresh_merges),
            ("conflicting refreshes", doc.conflicting,
             len(figures.conflicting_refreshes)),
            ("whole-graph refresh merges", doc.graph_refreshes, graph.refresh_merges),
            ("whole-graph conflicting refreshes", doc.graph_conflicting,
             len(graph.conflicting_refreshes)),
            ("whole-graph Task values", doc.graph_tasks, graph.task_values)):
        if stated != actual:
            problems.append(f"{name}: document {stated}, history {actual}")
    median = median_of(doc.samples)
    if not _close(doc.median, median):
        problems.append(f"median: document {doc.median}, samples give {median}")
    saved = saving(doc.conflicting, median, doc.landings)
    if not _close(doc.saved, saved):
        problems.append(f"saved per landing: document {doc.saved}, computed {saved:.2f}")
    graph_saved = saving(doc.graph_conflicting, median, doc.graph_tasks)
    if not _close(doc.graph_saved, graph_saved):
        problems.append(f"whole-graph saved per landing: document {doc.graph_saved}, "
                        f"computed {graph_saved:.2f}")
    if doc.threshold != THRESHOLD_MIN:
        problems.append(f"threshold: document {doc.threshold}, constant {THRESHOLD_MIN}")
    if doc.trailers_survive != check_trailers():
        problems.append("Task trailers line disagrees with --check-trailers")
    if agent_home is not None:
        listed = transcript_samples(agent_home, repo, doc.revision, *doc.window)
        if listed != sorted(doc.samples):
            problems.append(f"samples: document {sorted(doc.samples)}, transcripts {listed}")
    if problems:
        return EXIT_MISMATCH, problems
    expected = rule_decision(doc.saved, doc.threshold, doc.trailers_survive, doc.median)
    if doc.decision != expected:
        return EXIT_CONTRADICTS, [
            f"DECISION CONTRADICTS RULE: document says {doc.decision}, "
            f"the rule gives {expected} for its own numbers"]
    return 0, []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--rev", default="origin/main")
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--transcripts", type=Path,
                        help="agent home whose projects/*/*.jsonl are read")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--check-doc", type=Path)
    parser.add_argument("--check-trailers", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.check_trailers:
            ok = check_trailers()
            print(f"Task trailers survive squash: {'yes' if ok else 'no'}")
            return 0
        if args.check_doc:
            doc = parse_document(args.check_doc.read_text(encoding="utf-8"))
            code, problems = check_document(doc, args.repo, args.transcripts)
            for problem in problems:
                print(problem)
            if code == 0:
                print(f"OK: {args.check_doc} follows from history and the rule")
            return code
        if not (args.since and args.until):
            parser.error("--since and --until are required to measure")
        figures = git_figures(args.repo, args.rev, args.since, args.until)
        graph = git_figures(args.repo, args.rev, args.since, args.until,
                            first_parent=False)
        result = {**asdict(figures),
                  **{f"graph_{k}": v for k, v in asdict(graph).items()
                     if k != "revision"}}
        if args.transcripts:
            samples = transcript_samples(args.transcripts, args.repo,
                                         figures.revision, args.since, args.until)
            median = median_of(samples)
            result.update(
                samples=samples, median=median,
                saved=saving(len(figures.conflicting_refreshes), median,
                             figures.landings),
                graph_saved_upper_bound=saving(len(graph.conflicting_refreshes),
                                               median, graph.task_values))
        print(json.dumps(result, indent=2) if args.json else
              "\n".join(f"{k}: {v}" for k, v in result.items()))
        return 0
    except (GitError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
