"""Engine-owned cleanup of a resolved task's workspaces.

Difficulty removed: a task's isolated workspace (a linked git worktree and its
branch, a VCS task mount, session scratch files) outlived the task, because
"remove it once the work landed" was left to the agent's memory and to a periodic
reaper that cannot know which workspace a resolved task owned.

Rule / perception split. Whether a workspace may go is a pure function of
observables (path class, git status, ancestry of HEAD in the trunk ref): `observe`
gathers them, `classify` decides, `execute` acts only on a `remove` verdict.
Nothing here asks a model.

What may be a candidate. Only a workspace that was REGISTERED for the session
(`register`, via the `workspace-register` command or the isolation scripts), a
pending record whose path is, or contains, the resolve cwd / the plan's
delivery_worktree (`bind_pending`: enter-task.sh runs before any engine session
exists), and the plan's delivery_worktree itself. A path nobody registered is
never touched.

What may never be removed (`protected_class`): the home directory, the agent home,
the Core instructions checkout, every `canon-roots.local` entry, and the anchor /
store roots named through the env seams below — a candidate that IS such a root or
CONTAINS one is refused. `execute` repeats that refusal on its own, independent of
`classify`, immediately before any deletion.

Removal is never forced. A git worktree goes through unforced `git worktree remove`
(git itself refuses a tree with uncommitted or untracked files), and its branch
through `git branch -d` only after the branch tip is verified to be an ancestor of
the trunk ref. A mount goes through the backend's own teardown, which refuses the
reserved anchor/store names and unpushed work itself. Scratch is removed only under
an allowed scratch root. Every failure resolves to `kept` with a reason; the cleanup
never raises into `resolve`.

Org-neutral by construction: anchor, store and teardown locations come from env
seams and from `canon-roots.local`, never from a hardcoded org path.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lib import config_root

from . import exempt_paths
from .store import _safe

REGISTRY_ENV = "AGENTCTL_WORKSPACE_REGISTRY_DIR"
CLEANUP_ENV = "AGENTCTL_WORKSPACE_CLEANUP"
ANCHOR_ROOTS_ENV = "AGENTCTL_WORKSPACE_ANCHOR_ROOTS"
STORE_ROOTS_ENV = "AGENTCTL_WORKSPACE_STORE_ROOTS"
SCRATCH_ROOTS_ENV = "AGENTCTL_WORKSPACE_SCRATCH_ROOTS"
TEARDOWN_ENV = "AGENTCTL_WORKSPACE_TEARDOWN_BIN"
CORE_REPO_ENV = "CLAUDE_INSTRUCTIONS_REPO"
GIT_BIN_ENV = "GIT_BIN"

TRUNK_REF = "origin/main"
TRUNK_BRANCHES = frozenset({"main", "master"})
DEFAULT_SCRATCH_ROOT = "/tmp/cc-scratch"
SCRATCHPAD_COMPONENT = "scratchpad"

KIND_GIT = "git-worktree"
KIND_MOUNT = "mount"
KIND_SCRATCH = "scratch"
KINDS = (KIND_GIT, KIND_MOUNT, KIND_SCRATCH)

REMOVE = "remove"
KEEP = "keep"

_PENDING_DIR = "pending.d"
_GIT_TIMEOUT_S = 60
_TEARDOWN_TIMEOUT_S = 180
_REASON_MAX = 200

_REGISTRY_SH = Path(__file__).resolve().parents[1] / "project_entry" / "registry.sh"
_TEARDOWN_SCRIPT = (
    'set -euo pipefail; source "$1"; f="$(registry_resolve_workspace "$2")"; '
    'source "$f"; declare -F backend_teardown_workspace >/dev/null; '
    '[[ "$3" == "--probe" ]] || backend_teardown_workspace "$3"'
)


@dataclass(frozen=True)
class Item:
    path: str
    kind: str
    backend: str | None = None
    name: str | None = None

    def to_record(self) -> dict:
        return {"path": self.path, "kind": self.kind, "backend": self.backend,
                "name": self.name,
                "registered_at": dt.datetime.now(dt.timezone.utc).isoformat()}

    @staticmethod
    def from_record(raw: object) -> "Item | None":
        if not isinstance(raw, dict):
            return None
        path, kind = raw.get("path"), raw.get("kind")
        if not isinstance(path, str) or not path or kind not in KINDS:
            return None
        backend, name = raw.get("backend"), raw.get("name")
        return Item(path, kind,
                    backend if isinstance(backend, str) and backend else None,
                    name if isinstance(name, str) and name else None)


@dataclass(frozen=True)
class Observation:
    exists: bool = True
    is_symlink: bool = False
    error: str | None = None
    protected: str | None = None
    linked_worktree: bool | None = None
    dirty: str | None = None
    trunk_known: bool = False
    landed: bool | None = None
    branch: str | None = None
    common_dir: str | None = None
    scratch_allowed: bool = False
    teardown_available: bool = False


@dataclass(frozen=True)
class Outcome:
    item: Item
    status: str  # removed | would-remove | kept
    reason: str
    branch: dict | None = None

    def to_dict(self) -> dict:
        out = {"path": self.item.path, "kind": self.item.kind, "reason": self.reason}
        if self.branch is not None:
            out["branch"] = self.branch
        return out


# ── registry ────────────────────────────────────────────────────────────────

def registry_root(root: Path | None = None) -> Path:
    """Resolved per call, never cached at import, so an env override a test sets
    after import still takes effect (task_accumulator._root's discipline)."""
    if root is not None:
        return Path(root)
    override = os.environ.get(REGISTRY_ENV)
    if override:
        return Path(override).expanduser()
    return config_root.agentctl_workspace_registry_dir()


def enabled() -> bool:
    return os.environ.get(CLEANUP_ENV, "1").strip().lower() not in ("0", "false", "off", "no")


def _record_file(directory: Path, path: str) -> Path:
    return directory / (hashlib.sha256(path.encode("utf-8")).hexdigest()[:24] + ".json")


def _session_dir(session_id: str, root: Path | None) -> Path:
    return registry_root(root) / _safe(session_id)


def _pending_dir(root: Path | None) -> Path:
    return registry_root(root) / _PENDING_DIR


def _write_record(directory: Path, item: Item) -> Path:
    target = _record_file(directory, item.path)
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(item.to_record(), ensure_ascii=False))
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def _read_dir(directory: Path) -> list[tuple[Path, Item]]:
    found: list[tuple[Path, Item]] = []
    try:
        entries = sorted(directory.glob("*.json"))
    except OSError:
        return found
    for file in entries:
        try:
            item = Item.from_record(json.loads(file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if item is not None:
            found.append((file, item))
    return found


def current_dir() -> str | None:
    try:
        return os.getcwd()
    except OSError:
        return None


def _normalize(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def infer_kind(backend: str | None) -> str:
    return KIND_GIT if backend in (None, "", "git") else KIND_MOUNT


def _new_item(path: str, kind: str | None, backend: str | None, name: str | None) -> Item:
    item = Item(_normalize(path), kind or infer_kind(backend), backend or None, name or None)
    if item.kind not in KINDS:
        raise ValueError(f"unknown workspace kind {item.kind!r}; expected one of {KINDS}")
    return item


def register(session_id: str, path: str, *, kind: str | None = None,
             backend: str | None = None, name: str | None = None,
             root: Path | None = None) -> Path:
    return _write_record(_session_dir(session_id, root), _new_item(path, kind, backend, name))


def register_pending(path: str, *, kind: str | None = None, backend: str | None = None,
                     name: str | None = None, root: Path | None = None) -> Path:
    return _write_record(_pending_dir(root), _new_item(path, kind, backend, name))


def _forget(directory: Path, path: str) -> None:
    try:
        _record_file(directory, path).unlink()
    except OSError:
        pass


# ── candidates ──────────────────────────────────────────────────────────────

def _contains(ancestor: str, path: str) -> bool:
    return path == ancestor or path.startswith(ancestor.rstrip(os.sep) + os.sep)


def _real(path: str) -> str:
    return os.path.realpath(os.path.normpath(path))


def bind_pending(session_id: str, anchors: list[str], *, commit: bool,
                 root: Path | None = None) -> list[Item]:
    """The pending records whose path equals or contains one of `anchors` (the
    resolve cwd, the plan's delivery_worktree). With `commit`, each bound record
    moves into the session's registry; a foreign record is never touched."""
    real_anchors = [_real(a) for a in anchors if a]
    bound: list[Item] = []
    for file, item in _read_dir(_pending_dir(root)):
        item_real = _real(item.path)
        if not any(_contains(item_real, a) for a in real_anchors):
            continue
        bound.append(item)
        if commit:
            _write_record(_session_dir(session_id, root), item)
            try:
                file.unlink()
            except OSError:
                pass
    return bound


def candidates(session_id: str, *, delivery_worktree: str | None,
               bound: list[Item], root: Path | None = None) -> list[Item]:
    items = [item for _, item in _read_dir(_session_dir(session_id, root))]
    items.extend(bound)
    if delivery_worktree:
        items.append(Item(_normalize(delivery_worktree), KIND_GIT, "git", None))
    seen: set[str] = set()
    unique: list[Item] = []
    for item in items:
        key = _real(item.path)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


# ── protected paths ─────────────────────────────────────────────────────────

def _split_roots(value: str | None) -> list[str]:
    return [p for p in (value or "").split(os.pathsep) if p]


def _canon_roots_file_entries() -> list[str]:
    try:
        lines = config_root.canon_roots_file().read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def protected_roots() -> list[tuple[str, str, bool]]:
    """(class, realpath, whole_tree) per protected root, read at call time.
    `whole_tree` roots protect everything beneath them as well; the others (home,
    the Core checkout) protect only themselves and their ancestors, because linked
    worktrees legitimately live beneath them."""
    roots: list[tuple[str, str, bool]] = []
    home = os.environ.get("HOME") or os.path.expanduser("~")
    roots.append(("home", _real(home), False))
    roots.append(("agent-home", _real(str(config_root.agent_home())), True))
    core = os.environ.get(CORE_REPO_ENV) or str(Path(home) / "claude-agent-instructions")
    roots.append(("canon", _real(core), False))
    for entry in _canon_roots_file_entries():
        roots.append(("canon", _real(entry), True))
    for entry in _split_roots(os.environ.get(ANCHOR_ROOTS_ENV)):
        roots.append(("anchor", _real(entry), True))
    for entry in _split_roots(os.environ.get(STORE_ROOTS_ENV)):
        roots.append(("store", _real(entry), True))
    return roots


def protected_class(path: str) -> str | None:
    real = _real(path)
    for cls, root, whole_tree in protected_roots():
        if real == root or _contains(real, root):
            return cls
        if whole_tree and _contains(root, real):
            return cls
    return None


def scratch_allowed(path: str) -> bool:
    """A scratch path must lie strictly beneath an allowed root: /tmp/cc-scratch
    (or the env override), or an OS scratch root plus a `scratchpad` component —
    the session scratchpad. A scratch root itself is never removable."""
    real = _real(path)
    explicit = _split_roots(os.environ.get(SCRATCH_ROOTS_ENV)) or [DEFAULT_SCRATCH_ROOT]
    for root in explicit:
        root_real = _real(root)
        if real != root_real and _contains(root_real, real):
            return True
    for root in exempt_paths.scratch_roots():
        root_real = _real(root)
        if real == root_real or not _contains(root_real, real):
            continue
        if SCRATCHPAD_COMPONENT in Path(real[len(root_real):]).parts[:-1]:
            return True
    return False


# ── observation and decision ────────────────────────────────────────────────

def _git(args: list[str], *, cwd: str | None = None) -> tuple[int, str, str]:
    cmd = [os.environ.get(GIT_BIN_ENV) or "git", *args]
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=_GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _short(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return (lines[-1] if lines else "no output")[:_REASON_MAX]


def _teardown_argv(item: Item, *, probe: bool) -> list[str] | None:
    tail = "--probe" if probe else (item.name or "")
    explicit = os.environ.get(TEARDOWN_ENV)
    if explicit:
        if probe:
            return [explicit, "--probe"] if os.access(explicit, os.X_OK) else None
        return [explicit, tail]
    if not item.backend or not _REGISTRY_SH.is_file():
        return None
    return ["bash", "-c", _TEARDOWN_SCRIPT, "workspace-teardown",
            str(_REGISTRY_SH), item.backend, tail]


def _probe_teardown(item: Item) -> bool:
    argv = _teardown_argv(item, probe=True)
    if argv is None:
        return False
    if os.environ.get(TEARDOWN_ENV):
        return True
    try:
        return subprocess.run(argv, capture_output=True, timeout=_GIT_TIMEOUT_S,
                              check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def observe(item: Item) -> Observation:
    path = item.path
    try:
        is_symlink = os.path.islink(path)
        exists = os.path.lexists(path)
        protected = protected_class(path)
        if not exists or is_symlink or protected:
            return Observation(exists=exists, is_symlink=is_symlink, protected=protected)
        if item.kind == KIND_SCRATCH:
            return Observation(scratch_allowed=scratch_allowed(path))
        if item.kind == KIND_MOUNT:
            return Observation(teardown_available=bool(item.name) and _probe_teardown(item))
        return _observe_git(path)
    except Exception as exc:  # noqa: BLE001 - an observation fault must resolve to keep
        return Observation(error=f"observation failed: {_short(str(exc))}")


def _observe_git(path: str) -> Observation:
    rc, out, err = _git(["-C", path, "rev-parse", "--show-toplevel", "--git-dir",
                         "--git-common-dir"])
    lines = out.splitlines()
    if rc != 0 or len(lines) < 3:
        return Observation(error=f"not a git checkout: {_short(err)}")
    toplevel, git_dir, common = lines[0], lines[1], lines[2]
    git_dir_real = _real(os.path.join(path, git_dir))
    common_real = _real(os.path.join(path, common))
    linked = git_dir_real != common_real and _real(toplevel) == _real(path)
    rc, status, err = _git(["-C", path, "status", "--porcelain", "--untracked-files=all"])
    if rc != 0:
        return Observation(error=f"git status failed: {_short(err)}",
                           linked_worktree=linked, common_dir=common_real)
    dirty = None
    if status.strip():
        only_untracked = all(ln.startswith("??") for ln in status.splitlines() if ln.strip())
        dirty = "untracked files" if only_untracked else "uncommitted changes"
    rc, _, _ = _git(["-C", path, "rev-parse", "--verify", "-q", f"{TRUNK_REF}^{{commit}}"])
    trunk_known = rc == 0
    landed = None
    if trunk_known:
        rc, _, err = _git(["-C", path, "merge-base", "--is-ancestor", "HEAD", TRUNK_REF])
        if rc in (0, 1):
            landed = rc == 0
        else:
            return Observation(error=f"ancestry check failed: {_short(err)}",
                               linked_worktree=linked, common_dir=common_real)
    rc, branch, _ = _git(["-C", path, "symbolic-ref", "-q", "--short", "HEAD"])
    return Observation(linked_worktree=linked, dirty=dirty, trunk_known=trunk_known,
                       landed=landed, branch=branch.strip() if rc == 0 else None,
                       common_dir=common_real)


def classify(item: Item, obs: Observation) -> tuple[str, str]:
    """The single removal decision: (`remove`|`keep`, reason). Pure in its inputs."""
    if obs.error:
        return KEEP, obs.error
    if obs.protected:
        return KEEP, f"protected path ({obs.protected})"
    if not obs.exists:
        return KEEP, "path already absent"
    if obs.is_symlink:
        return KEEP, "path is a symlink"
    if item.kind == KIND_SCRATCH:
        if obs.scratch_allowed:
            return REMOVE, "scratch under an allowed root"
        return KEEP, "scratch outside the allowed roots"
    if item.kind == KIND_MOUNT:
        if not item.name:
            return KEEP, "mount registered without a name"
        if not obs.teardown_available:
            return KEEP, "no teardown backend available"
        return REMOVE, "delegated to the backend teardown, which refuses unpushed work"
    if item.kind == KIND_GIT:
        if obs.linked_worktree is not True:
            return KEEP, "not the root of a linked worktree"
        if obs.dirty:
            return KEEP, obs.dirty
        if not obs.trunk_known:
            return KEEP, f"{TRUNK_REF} unknown"
        if obs.landed is not True:
            return KEEP, f"HEAD not an ancestor of {TRUNK_REF}"
        return REMOVE, f"clean and landed in {TRUNK_REF}"
    return KEEP, f"unknown kind {item.kind!r}"


# ── execution ───────────────────────────────────────────────────────────────

def _guard(item: Item) -> str | None:
    """execute's own refusal, recomputed from the path right before deletion and
    independent of `classify` and of the observation."""
    cls = protected_class(item.path)
    if cls:
        return f"protected path ({cls})"
    if item.kind == KIND_SCRATCH and not scratch_allowed(item.path):
        return "scratch outside the allowed roots"
    return None


def _remove_branch(obs: Observation) -> dict | None:
    branch, common = obs.branch, obs.common_dir
    if not branch or not common:
        return None
    if branch in TRUNK_BRANCHES:
        return {"name": branch, "deleted": False, "reason": "trunk branch is never deleted"}
    base = ["--git-dir", common]
    rc, _, _ = _git([*base, "merge-base", "--is-ancestor", f"refs/heads/{branch}", TRUNK_REF])
    if rc != 0:
        return {"name": branch, "deleted": False,
                "reason": f"branch tip not an ancestor of {TRUNK_REF}"}
    rc, _, err = _git([*base, "branch", "-d", branch])
    if rc != 0:
        return {"name": branch, "deleted": False, "reason": _short(err)}
    return {"name": branch, "deleted": True}


def _remove_git(item: Item, obs: Observation) -> Outcome:
    common = obs.common_dir
    if not common:
        return Outcome(item, "kept", "common git dir unknown")
    rc, _, err = _git(["--git-dir", common, "worktree", "remove", item.path],
                      cwd=os.path.dirname(common) or None)
    if rc != 0:
        return Outcome(item, "kept", f"git refused the removal: {_short(err)}")
    return Outcome(item, "removed", "clean and landed", branch=_remove_branch(obs))


def _remove_mount(item: Item) -> Outcome:
    argv = _teardown_argv(item, probe=False)
    if argv is None:
        return Outcome(item, "kept", "no teardown backend available")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=_TEARDOWN_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return Outcome(item, "kept", f"teardown failed: {_short(str(exc))}")
    if proc.returncode != 0:
        return Outcome(item, "kept",
                       f"teardown refused: {_short(proc.stderr or proc.stdout)}")
    return Outcome(item, "removed", "backend teardown completed")


def execute(item: Item, obs: Observation, *, dry_run: bool) -> Outcome:
    decision, reason = classify(item, obs)
    if decision != REMOVE:
        return Outcome(item, "kept", reason)
    refusal = _guard(item)
    if refusal:
        return Outcome(item, "kept", refusal)
    if dry_run:
        return Outcome(item, "would-remove", reason)
    if item.kind == KIND_SCRATCH:
        shutil.rmtree(item.path)
        return Outcome(item, "removed", reason)
    if item.kind == KIND_MOUNT:
        return _remove_mount(item)
    return _remove_git(item, obs)


def run(session_id: str, *, cwd: str | None, delivery_worktree: str | None,
        dry_run: bool | None = None, root: Path | None = None) -> dict:
    """Clean the session's registered workspaces and return the report that
    cmd_resolve exposes as data['workspace_cleanup']. A per-item fault becomes a
    `kept` entry; only a fault outside the loop propagates (to resolve's try/except)."""
    if not enabled():
        return {"enabled": False, "dry_run": False, "removed": [], "would_remove": [],
                "kept": []}
    if dry_run is None:
        dry_run = bool(os.environ.get("CLAUDE_DRY_RUN"))
    bound = bind_pending(session_id, [a for a in (cwd, delivery_worktree) if a],
                         commit=not dry_run, root=root)
    session_dir = _session_dir(session_id, root)
    report: dict = {"enabled": True, "dry_run": dry_run, "removed": [],
                    "would_remove": [], "kept": []}
    for item in candidates(session_id, delivery_worktree=delivery_worktree,
                           bound=bound, root=root):
        try:
            obs = observe(item)
            outcome = execute(item, obs, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - per-item fault resolves to keep
            obs = Observation(error="execution failed")
            outcome = Outcome(item, "kept", f"error: {_short(str(exc))}")
        if outcome.status == "removed" or (
                outcome.status == "kept" and not obs.exists and not dry_run):
            _forget(session_dir, item.path)
        bucket = {"removed": "removed", "would-remove": "would_remove"}.get(
            outcome.status, "kept")
        report[bucket].append(outcome.to_dict())
    return report
