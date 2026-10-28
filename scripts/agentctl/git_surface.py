"""Trusted snapshot of what git would execute in a repository.

Difficulty removed: git runs code the caller never named -- a hook on `git status` (post-index-change),
a hooks directory named by `core.hooksPath`, a symlinked hook entry, a program named in the config
(`core.fsmonitor`, `filter.*.clean`, `alias.* = !...`), a hook framework reading a file of the worktree.
A rule that tried to recognise each channel as it appeared was always one channel behind. The spawn wrapper
takes ONE digest of every such surface of the child's venues (`snapshot_venues`) into the norm-handoff
record, and the resource-grant hook later compares the live digest of the repository a git segment would run
in (`capture` + `mismatch`): the hook allows a git run only while nothing git would execute differs from the
state the parent saw.

What is pinned, per repository (keyed by the realpath of its absolute git dir):
  hooks       the hooks directory (`rev-parse --git-path hooks`, which honours `core.hooksPath`, read from
              the worktree root): each entry but `*.sample` with its mode and, for a symlink, the
              realpath it resolves to and the bytes of that target
  config      every merged config entry that names a program or a hook path (see EXEC_KEY_PATTERNS, plus
              `alias.*` / `submodule.*.update` values starting with `!`), as key and value, and the bytes of
              each program or path such a value names
  attributes  the top-level and tracked nested `.gitattributes`, `info/attributes` and `core.attributesFile`
  frameworks  `.pre-commit-config.yaml`, `lefthook.yml`, `.husky/`: files a hook may read to decide what to run

Config entries that name nothing executable are not pinned, so the child's own `git push -u` (which writes
`branch.*`) does not invalidate the snapshot. Anything the snapshot cannot read raises `Unreadable`; the
caller treats a repository missing from the snapshot as unverifiable, never as unchanged.

Not pinned (named in docs/operations/resource-grant-hook.md): what a hook or filter does by running a file of
the worktree it names only by path (`githooks/pre-push` running `scripts/gate.py`), untracked nested
`.gitattributes`, and config keys outside the list.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
from dataclasses import dataclass, field
from typing import Callable

SCHEMA = 1
MAX_ENTRIES = 512
FRAMEWORK_FILES = (".pre-commit-config.yaml", "lefthook.yml", ".lefthook.yml", "lefthook.yaml")
FRAMEWORK_DIRS = (".husky",)
#: Config keys (lowercased) whose value names a program, a hook directory or a file git reads.
EXEC_KEY_PATTERNS = re.compile(
    r"^(?:"
    r"core\.(?:fsmonitor|hookspath|sshcommand|pager|editor|askpass|gitproxy|attributesfile|worktree)"
    r"|pager\..+"
    r"|credential\.(?:.+\.)?helper"
    r"|filter\..+\.(?:clean|smudge|process)"
    r"|diff\.external|diff\..+\.(?:command|textconv)"
    r"|merge\..+\.driver|mergetool\..+\.cmd|difftool\..+\.cmd"
    r"|gpg\.(?:.+\.)?program"
    r"|sequence\.editor|uploadpack\.packobjectshook|init\.templatedir"
    r"|remote\..+\.(?:uploadpack|receivepack|vcs|proxy)"
    r"|trailer\..+\.command|hook\..+\.command"
    r")$"
)
BANG_KEY_PATTERNS = re.compile(r"^(?:alias\..+|submodule\..+\.update)$")


class NotARepo(Exception):
    """The directory is not inside a git repository: no git operation there runs a hook."""


class Unreadable(Exception):
    """A surface could not be read: no digest is better than a digest of part of it."""


@dataclass
class Surface:
    repo: str
    parts: dict
    digest: str
    #: Realpaths of everything the digest covers, for the hook's overlap test against a neighbour's writes.
    paths: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"digest": self.digest, "parts": dict(self.parts)}


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()


def _real(path: str) -> str:
    return os.path.realpath(path)


def _git(directory: str, args: list, timeout: float) -> bytes:
    if timeout <= 0:
        raise Unreadable("no time left to ask git")
    try:
        proc = subprocess.run(
            ["git", "-C", directory, *args], capture_output=True, timeout=timeout, check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unreadable(f"git {args[0]} failed: {exc}") from exc
    if proc.returncode != 0:
        if b"not a git repository" in proc.stderr:
            raise NotARepo(directory)
        raise Unreadable(f"git {args[0]} exited {proc.returncode}")
    return proc.stdout


def _file_digest(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                digest.update(chunk)
    except OSError as exc:
        raise Unreadable(f"cannot read {path}: {exc.strerror or exc}") from exc
    return digest.hexdigest()


def _entry(path: str) -> dict:
    """What identifies one filesystem entry for git: its kind and mode and, for a file or a symlink, the
    bytes it resolves to. A missing path is a stated absence, not an error."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {"absent": True}
    except OSError as exc:
        raise Unreadable(f"cannot stat {path}: {exc.strerror or exc}") from exc
    record = {"mode": info.st_mode}
    if stat.S_ISLNK(info.st_mode):
        real = _real(path)
        record["link"] = os.readlink(path)
        record["real"] = real
        target = os.stat(real) if os.path.exists(real) else None
        record["content"] = _file_digest(real) if target is not None and stat.S_ISREG(target.st_mode) else "not-a-file"
    elif stat.S_ISREG(info.st_mode):
        record["content"] = _file_digest(path)
    return record


def _resolve(answer: str, *bases: "str | None") -> list:
    """Every absolute path `answer` can mean from the given base directories."""
    answer = os.path.expanduser(answer)
    if os.path.isabs(answer):
        return [os.path.normpath(answer)]
    return list(dict.fromkeys(os.path.normpath(os.path.join(base, answer)) for base in bases if base))


def _config_entries(raw: bytes) -> list:
    entries = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        key, _sep, value = item.partition(b"\n")
        entries.append((key.decode("utf-8", "surrogateescape"), value.decode("utf-8", "surrogateescape")))
    return entries


def _named_program(value: str) -> "str | None":
    """The path a command value names as its program, when it is one (contains a `/` or is `~`-relative)."""
    text = value[1:] if value.startswith("!") else value
    try:
        tokens = shlex.split(text)
    except ValueError:
        return None
    if not tokens:
        return None
    program = tokens[0]
    return program if "/" in program or program.startswith("~") else None


def _exec_entries(entries: list, base: str, paths: list) -> list:
    chosen = []
    for key, value in entries:
        lowered = key.lower()
        if not (EXEC_KEY_PATTERNS.match(lowered) or (BANG_KEY_PATTERNS.match(lowered) and value.startswith("!"))):
            continue
        record = {"key": key, "value": value}
        named = _named_program(value)
        if named is not None:
            targets = _resolve(named, base)
            record["programs"] = {target: _entry(target) for target in targets}
            paths.extend(targets)
        chosen.append(record)
    return sorted(chosen, key=lambda item: (item["key"], item["value"]))


def _hooks_part(dirs: list, paths: list) -> dict:
    part = {}
    for directory in sorted(dirs):
        info = _entry(directory)
        record = {"dir": info}
        paths.extend([directory, _real(directory)])
        if "absent" not in info:
            try:
                names = sorted(name for name in os.listdir(directory) if not name.endswith(".sample"))
            except OSError as exc:
                raise Unreadable(f"cannot list {directory}: {exc.strerror or exc}") from exc
            if len(names) > MAX_ENTRIES:
                raise Unreadable(f"{directory} holds more than {MAX_ENTRIES} entries")
            record["real"] = _real(directory)
            record["entries"] = {}
            for name in names:
                path = os.path.join(directory, name)
                record["entries"][name] = _entry(path)
                paths.extend([path, _real(path)])
        part[directory] = record
    return part


def _attributes_part(toplevel: "str | None", info_attributes: list, configured: list, timeout: float, paths: list) -> dict:
    files = [*info_attributes, *configured]
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    files.append(os.path.join(xdg, "git", "attributes"))
    if toplevel:
        files.append(os.path.join(toplevel, ".gitattributes"))
        listed = _git(toplevel, ["ls-files", "-z", "--", ":(glob)**/.gitattributes"], timeout)
        nested = [os.path.join(toplevel, name.decode("utf-8", "surrogateescape")) for name in listed.split(b"\0") if name]
        if len(nested) > MAX_ENTRIES:
            raise Unreadable("more nested .gitattributes than the snapshot holds")
        files.extend(nested)
    part = {}
    for path in dict.fromkeys(os.path.normpath(item) for item in files):
        part[path] = _entry(path)
        paths.extend([path, _real(path)])
    return part


def _tree(path: str, paths: list) -> dict:
    """A directory's entries (names, modes, bytes), recursively, bounded."""
    found = {}
    count = 0
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(files):
            count += 1
            if count > MAX_ENTRIES:
                raise Unreadable(f"{path} holds more than {MAX_ENTRIES} files")
            full = os.path.join(root, name)
            found[os.path.relpath(full, path)] = _entry(full)
            paths.extend([full, _real(full)])
    return found


def _frameworks_part(toplevel: "str | None", paths: list) -> dict:
    if not toplevel:
        return {}
    part = {}
    for name in FRAMEWORK_FILES:
        full = os.path.join(toplevel, name)
        part[name] = _entry(full)
        paths.extend([full, _real(full)])
    for name in FRAMEWORK_DIRS:
        full = os.path.join(toplevel, name)
        part[name] = _tree(full, paths) if os.path.isdir(full) else {"absent": True}
        paths.extend([full, _real(full)])
    return part


def capture(directory: str, timeout: Callable[[], float]) -> Surface:
    """The surface of the repository `directory` lies in. Raises `NotARepo` or `Unreadable`.
    `timeout` is asked before each git call and returns the seconds it may take (<= 0: none left)."""
    paths: list = []
    gitdir = _git(directory, ["rev-parse", "--absolute-git-dir"], timeout()).decode("utf-8", "surrogateescape").strip()
    if not gitdir:
        raise Unreadable("git rev-parse gave no git dir")
    repo = _real(gitdir)
    try:
        toplevel = _git(directory, ["rev-parse", "--show-toplevel"], timeout()).decode("utf-8", "surrogateescape").strip() or None
    except Unreadable:
        toplevel = None
    # Every relative answer below is read from one fixed directory, so the digest does not depend on
    # which subdirectory of the repository the call started in.
    base = toplevel or gitdir
    answer = _git(base, [
        "rev-parse", "--git-path", "hooks", "--git-path", "info/attributes",
        "--git-path", "config", "--git-path", "config.worktree",
    ], timeout()).decode("utf-8", "surrogateescape").split("\n")
    if len(answer) < 4:
        raise Unreadable("git rev-parse answered too little")
    hooks_answer, info_attributes, config_file, worktree_config = answer[:4]
    entries = _config_entries(_git(base, ["config", "--list", "-z"], timeout()))
    values = {key.lower(): value for key, value in entries}
    hooks_dirs = _resolve(hooks_answer, base)
    attributes_file = _resolve(values["core.attributesfile"], base) if values.get("core.attributesfile") else []
    config_paths = [
        *_resolve(config_file, base), *_resolve(worktree_config, base),
        os.path.join(os.path.expanduser("~"), ".gitconfig"), "/etc/gitconfig",
    ]
    paths.extend([*config_paths, *(_real(item) for item in config_paths)])
    parts = {
        "hooks": _hooks_part(list(dict.fromkeys(hooks_dirs)), paths),
        "config": _exec_entries(entries, base, paths),
        "attributes": _attributes_part(toplevel, _resolve(info_attributes, base), attributes_file, timeout(), paths),
        "frameworks": _frameworks_part(toplevel, paths),
    }
    digests = {name: _sha(part) for name, part in parts.items()}
    return Surface(repo=repo, parts=digests, digest=_sha(digests), paths=sorted(set(paths)))


def snapshot_venues(directories, timeout: Callable[[], float]) -> dict:
    """The record's `git_surface`: the digest of each distinct repository the directories lie in. A
    directory outside a repository or one whose surface cannot be read adds nothing, so its repository is
    absent from the snapshot and the hook cannot verify a git run there."""
    repos = {}
    for directory in directories:
        if not directory:
            continue
        try:
            surface = capture(str(directory), timeout)
        except (NotARepo, Unreadable):
            continue
        repos[surface.repo] = surface.to_dict()
    return {"schema": SCHEMA, "repos": repos}


def mismatch(snapshot, live: Surface) -> "str | None":
    """Why `live` is not the state the snapshot saw (None when it is the same). Never returns None for a
    snapshot that is missing, malformed or silent about the repository."""
    if not isinstance(snapshot, dict) or snapshot.get("schema") != SCHEMA or not isinstance(snapshot.get("repos"), dict):
        return "the handoff record carries no git-surface snapshot"
    saved = snapshot["repos"].get(live.repo)
    if not isinstance(saved, dict) or not isinstance(saved.get("parts"), dict) or not isinstance(saved.get("digest"), str):
        return f"{live.repo} is not in the git-surface snapshot taken at spawn"
    if saved["digest"] == live.digest and saved["parts"] == live.parts:
        return None
    changed = sorted(name for name in {*saved["parts"], *live.parts} if saved["parts"].get(name) != live.parts.get(name))
    return f"git would run code that differs from the spawn-time snapshot ({', '.join(changed) or 'digest'} of {live.repo})"
