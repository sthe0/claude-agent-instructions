"""Reaper `temp-residue`: abandoned test and session temp directories of this user.

Difficulty removed: tools leave directories in shared temp roots that nothing reclaims — the
instruction sandbox (`/tmp/instruction-sandbox.*`), a session's temp root (`tmp.*`) and a job's
sandbox (`<agent home>/jobs/*/tmp/sbx.*`) — and a busy machine's volume fills with them.

Three kinds, each found only as a direct child of its root, by name:

* `instruction-sandbox.*` under `/tmp` and under the session temp root (`tempfile.gettempdir()`
  at pass time, so TMPDIR decides);
* `tmp.*` under the session temp root;
* `sbx.*` under `<agent home>/jobs/<job>/tmp`.

Each root is resolved once with `os.path.realpath` and deduplicated by the resolved path, so a
temp root that is `/tmp`, or a symlink to it, is scanned once; the walk and its `st_dev`
reference come from the resolved root, never from a symlink.

A candidate is removed only on the proof of death in `reaper.liveness` (not held by any process,
not owned by a live session, no mount or nested repository inside, idle past
`IDLE_FLOOR_SECONDS`); everything else is kept with a slug and a reason. The floor is this
module's constant: tests move it or `ctx.now`, no environment variable or config key does.

One exemption from the nested-repository clause, for kind `instruction-sandbox` only: the
sandbox script makes `<candidate>/core` a `git clone --shared`, so every sandbox holds a `.git`.
That clone protects nothing when `disposable_shared_clone` proves, with a scrubbed git
environment, no network and a timeout, that (1) it is the only `.git` in the candidate, (2) its
`objects/info/alternates` lists only objects directories of repositories that exist outside the
candidate, (3) HEAD and every ref resolve to commits those repositories hold, and (4) its
working tree has no uncommitted or untracked change. Any other `.git`, and any failed check or
git failure, keeps the candidate as `nested-git`.

The reaper deletes only on a machine that opted in: `temp-residue = true` in
`<agent home>/reaper/opt-in.toml` (`reaper.opt_in`). Without it a live pass keeps every
candidate it would have removed (`opt-in-off-would-remove`), and `remove()` refuses; a dry run
still shows them as `remove`, so the owner sees what enabling would do.

`summary()` prints the dry-run report: one `root=` line per scanned root, one `kind=` line per
kind (zero counts included) and one `reason=` line per keep reason, each reason a slug from
`REASONS`. The long reason text and any OSError string go only into the runner's kept snapshot.

The roots are this reaper's pressure paths: a disk-pressure pass (`runner --on-pressure`)
makes it due before its 24-hour throttle, under the same rules.

remove() judges the candidate again, with a fresh /proc sweep, immediately before deleting it
through the parent's directory fd.
"""
from __future__ import annotations

import dataclasses
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from lib import config_root
from reaper import liveness, opt_in
from reaper.contract import KEEP, REMOVE, ReapContext, Verdict

NAME = "temp-residue"
THROTTLE_HOURS = 24.0
IDLE_FLOOR_SECONDS = 3 * 86400
SANDBOX_ROOT = "/tmp"

SANDBOX_KIND = "instruction-sandbox"
KINDS = (SANDBOX_KIND, "tmp", "sbx")
PATTERNS = {
    SANDBOX_KIND: re.compile(r"instruction-sandbox\.[A-Za-z0-9_-]+"),
    "tmp": re.compile(r"tmp\.[A-Za-z0-9_-]+"),
    "sbx": re.compile(r"sbx\.[A-Za-z0-9_-]+"),
}
OPT_IN_OFF = "opt-in-off-would-remove"
REASONS = (
    liveness.IDLE_FLOOR, liveness.HELD, liveness.SESSION_OWNED, liveness.FOREIGN_OWNER,
    liveness.NOT_A_DIRECTORY, liveness.MOUNT_INSIDE, liveness.NESTED_GIT,
    liveness.HOLDER_SWEEP_INCOMPLETE, OPT_IN_OFF, liveness.OS_ERROR,
)
CLONE_DIR = "core"
GIT_TIMEOUT_S = 20


@dataclasses.dataclass(frozen=True)
class Source:
    """A root to scan. ``per_job`` roots hold ``<job>/tmp`` directories whose children are the candidates."""

    kind: str
    base: str
    per_job: bool
    dev: int


def _sources() -> "list[Source]":
    sources: "list[Source]" = []

    def add(kind: str, raw: str, per_job: bool) -> None:
        real = os.path.realpath(raw)
        if any(s.kind == kind and s.base == real for s in sources):
            return
        try:
            st = os.stat(real)
        except OSError:
            return
        if stat.S_ISDIR(st.st_mode):
            sources.append(Source(kind, real, per_job, st.st_dev))

    session_root = tempfile.gettempdir()
    add(SANDBOX_KIND, SANDBOX_ROOT, False)
    add(SANDBOX_KIND, session_root, False)
    add("tmp", session_root, False)
    add("sbx", str(config_root.agent_home() / "jobs"), True)
    return sources


def _real_dirs(parent: str) -> "list[str]":
    found = []
    with os.scandir(parent) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                found.append(entry.path)
    return sorted(found)


def _children(source: Source) -> "list[str]":
    """Paths directly under the source's root(s) whose names match its kind's pattern."""
    if source.per_job:
        parents = []
        for job in _real_dirs(source.base):
            job_tmp = os.path.join(job, "tmp")
            if os.path.isdir(job_tmp) and not os.path.islink(job_tmp):
                parents.append(job_tmp)
    else:
        parents = [source.base]
    pattern = PATTERNS[source.kind]
    found = []
    for parent in parents:
        try:
            names = os.listdir(parent)
        except FileNotFoundError:
            continue
        found.extend(os.path.join(parent, name) for name in names if pattern.fullmatch(name))
    return sorted(found)


def _is_child_of(source: Source, path: str) -> bool:
    """``path`` (its parent already resolved) is where ``_children(source)`` would list a candidate."""
    parent = os.path.dirname(path)
    if source.per_job:
        parent, job_tmp = os.path.dirname(parent), os.path.basename(parent)
        if job_tmp != "tmp" or os.path.dirname(parent) != source.base:
            return False
    elif parent != source.base:
        return False
    return PATTERNS[source.kind].fullmatch(os.path.basename(path)) is not None


def _mounts() -> "frozenset[str] | None":
    try:
        return liveness.read_mount_points()
    except OSError:
        return None


def _git_env() -> "dict[str, str]":
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent", "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }


def _git(args: "list[str]", stdin: str = "") -> "str | None":
    """Stdout of ``git <args>`` run in a scrubbed environment under a timeout; None on any failure.

    ``GIT_OPTIONAL_LOCKS=0`` keeps ``status`` from refreshing the index, so judging writes nothing.
    """
    try:
        done = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *args], input=stdin, capture_output=True, text=True,
            env=_git_env(), timeout=GIT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return done.stdout if done.returncode == 0 else None


def _alternate_repositories(git_dir: str, candidate: str) -> "list[str]":
    """Git dirs of the repositories the clone borrows objects from; [] unless every entry of its
    ``alternates`` file is the objects directory of an existing repository outside ``candidate``."""
    objects = os.path.join(git_dir, "objects")
    try:
        lines = Path(objects, "info", "alternates").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    repos = []
    for line in lines:
        if not line.strip() or line.startswith("#"):
            continue
        alt = os.path.realpath(os.path.join(objects, line))
        repo = os.path.dirname(alt)
        if (os.path.basename(alt) != "objects" or not os.path.isdir(alt)
                or not os.path.isfile(os.path.join(repo, "HEAD")) or not os.path.isdir(os.path.join(repo, "refs"))):
            return []
        if repo == candidate or repo.startswith(candidate + os.sep):
            return []
        repos.append(repo)
    return repos


def _refs_in_alternates(git_dir: str, repos: "list[str]") -> bool:
    """HEAD and every ref of the clone name a commit that one of ``repos`` holds."""
    listed = _git(["--git-dir", git_dir, "for-each-ref", "--format=%(objectname)"])
    head = _git(["--git-dir", git_dir, "rev-parse", "--verify", "--quiet", "HEAD^{commit}"])
    if listed is None or head is None or not head.strip():
        return False
    wanted = sorted({*listed.split(), head.strip()})
    request = "".join(f"{oid}^{{commit}}\n" for oid in wanted)
    missing = set(wanted)
    for repo in repos:
        answer = _git(["--git-dir", repo, "cat-file", "--batch-check"], request)
        if answer is None or len(answer.splitlines()) != len(wanted):
            return False
        for oid, line in zip(wanted, answer.splitlines()):
            if line.split()[1:2] == ["commit"]:
                missing.discard(oid)
    return not missing


def _worktree_clean(git_dir: str, work_tree: str) -> bool:
    return _git(["--git-dir", git_dir, "--work-tree", work_tree, "status", "--porcelain"]) == ""


def disposable_shared_clone(candidate: str, git_entries: "list[str]") -> bool:
    """True when the only ``.git`` of a sandbox candidate is its throw-away ``--shared`` clone. See the module docstring."""
    clone = os.path.join(candidate, CLONE_DIR)
    git_dir = os.path.join(clone, liveness.GIT_NAME)
    if git_entries != [git_dir] or not stat.S_ISDIR(os.lstat(git_dir).st_mode):
        return False
    repos = _alternate_repositories(git_dir, candidate)
    if not _refs_in_alternates(git_dir, repos):
        return False
    return _worktree_clean(git_dir, clone)


def _judge(path: str, source: Source, ctx: ReapContext,
           sweep: liveness.HolderSweep, mounts: "frozenset[str] | None") -> liveness.Judgment:
    return liveness.judge_residue(
        path, root_dev=source.dev, now=ctx.now, floor_s=IDLE_FLOOR_SECONDS, sweep=sweep,
        mounts=mounts, owned=ctx.owned_path,
        git_exemption=disposable_shared_clone if source.kind == SANDBOX_KIND else None,
    )


def _verdict(kind: str, path: str, judgment: liveness.Judgment, ctx: ReapContext, enabled: bool) -> Verdict:
    tags = (f"kind={kind}",)
    if judgment.action == REMOVE:
        if not ctx.dry_run and not enabled:
            reason = f"{OPT_IN_OFF}: {judgment.detail}; set {NAME} = true in the machine's reaper opt-in file to remove"
            return Verdict(path, KEEP, reason, tags=tags + (f"reason={OPT_IN_OFF}",), size=judgment.size)
        return Verdict(path, REMOVE, judgment.detail, tags=tags, size=judgment.size)
    return Verdict(path, KEEP, f"{judgment.slug}: {judgment.detail}", tags=tags + (f"reason={judgment.slug}",), size=judgment.size)


def pressure_paths(ctx: ReapContext) -> "list[str]":
    return list(dict.fromkeys(s.base for s in _sources()))


def scan(ctx: ReapContext) -> "list[Verdict]":
    candidates = [(s, path) for s in _sources() for path in _children(s)]
    if not candidates:
        return []
    enabled = opt_in.enabled(NAME)
    mounts = _mounts()
    sweep = liveness.sweep_holders()
    return [
        _verdict(source.kind, path, _judge(path, source, ctx, sweep, mounts), ctx, enabled)
        for source, path in candidates
    ]


def remove(path: str, ctx: ReapContext) -> bool:
    if not opt_in.enabled(NAME):
        return False
    candidate = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
    source = next((s for s in _sources() if _is_child_of(s, candidate)), None)
    if source is None:
        return False
    if _judge(candidate, source, ctx, liveness.sweep_holders(), _mounts()).action != REMOVE:
        return False
    liveness.remove_tree(os.path.dirname(candidate), os.path.basename(candidate))
    return True


def summary(verdicts: "list[Verdict]") -> str:
    counts = {kind: {"removable": 0, "removable_bytes": 0, "kept": 0, "kept_bytes": 0} for kind in KINDS}
    reasons: "dict[str, list[int]]" = {slug: [0, 0] for slug in REASONS}
    for verdict in verdicts:
        tags = dict(tag.split("=", 1) for tag in verdict.tags if "=" in tag)
        kind = tags.get("kind")
        if kind not in counts:
            continue
        bucket = "removable" if verdict.action == REMOVE else "kept"
        counts[kind][bucket] += 1
        counts[kind][f"{bucket}_bytes"] += verdict.size
        slug = tags.get("reason")
        if verdict.action == KEEP and slug in reasons:
            reasons[slug][0] += 1
            reasons[slug][1] += verdict.size
    lines = [f"{NAME} root={base}" for base in dict.fromkeys(s.base for s in _sources())]
    for kind in KINDS:
        c = counts[kind]
        lines.append(
            f"{NAME} kind={kind} removable={c['removable']} removable_bytes={c['removable_bytes']}"
            f" kept={c['kept']} kept_bytes={c['kept_bytes']}"
        )
    lines.extend(f"{NAME} reason={slug} count={n} bytes={b}" for slug, (n, b) in reasons.items() if n)
    return "\n".join(lines)
