"""Typed resources: the unit a permission actually grants access to.

Difficulty removed: `grants.py` decides whether a materialized RULE covers a
CALL, but a rule's *text* is not the thing a human approves — the user
approves changing a FILE, pushing a VCS REF, spawning a SPECIALIST, calling a
SERVICE, or touching a DATASET. Two differently-spelled rules can name the
same resource, and one rule can (once wildcarded) admit many different
resources — so "was this call covered" must be answered at the resource
level, not by comparing rule strings. This module is the ONE place that
knows what a resource IS and when one covers another; `agentctl/tool_
contracts.py` (added alongside this module) is the one place that knows how
to turn a COMMAND into the resource(s) it can change — resources.py itself
never inspects a command or a rule string.

Five kinds are typed here (REQ2): `file`, `vcs_ref`, `specialist`, `service`,
`dataset`. Every other candidate resource shape considered while designing
this model (a raw shell environment variable; an arbitrary network
endpoint; a money/quota amount; a read-confidentiality tier; an Org-layer
dataset/service contract) is deferred because no plan element in this
codebase currently declares one, and inventing coverage semantics for a
resource nothing yet resolves to would be untested by construction:

- environment-variable resources: no contract entry in this plan mutates
  process environment as its OWN effect (env-var writes only ever show up as
  a side effect of a shell already covered by a `file` write); deferred
  until a tool contract needs to name one.
- arbitrary network-endpoint resources: `service` exists for a named,
  already-classified external service (the resolver's `service` status);
  a raw URL/host resource would need its own coverage story (wildcards,
  ports, schemes) with no current caller — deferred.
- money/quota-amount resources: no contract entry names a spend ceiling or
  a quota amount as its own resource — a paid external call is typed as a
  `service` (exact match on name + `op_class`), never as a bounded amount;
  deferred until a contract needs to gate on a ceiling rather than on which
  service is called.
- read-confidentiality resources: `file.mode` distinguishes read from
  write but not WHICH reads are sensitive (secrets, customer PII) versus
  ordinary; deferred until a contract needs to gate a read by sensitivity
  rather than by path alone.
- Org-layer dataset/service contracts: `dataset`/`service` here type only
  what a Core resolver can name from a command line (a `(system, locator)`
  pair, a `(name, op_class)` pair) — an Org-layer plugin's own catalog
  (internal tracker keys, org-internal service names) is out of scope for
  Core and deferred to whatever plugin seam names those resources.

`covers(approved, requested)` is asymmetric and fails toward NOT-covered:
the same bias `grants.grant_covers_call` documents for the same reason — a
false "covered" here would let `resolve-permission --by agent` self-grant a
call nobody approved.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from lib import widening_targets


def _realpath(path: str) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


def _path_is_or_under(container: str, member: str) -> bool:
    """True iff `member` equals `container` or lies strictly under it, by
    path SEGMENT (never a bare string prefix — `/a/foo` must not be read as
    containing `/a/foobar`)."""
    if member == container:
        return True
    prefix = container if container.endswith("/") else container + "/"
    return member.startswith(prefix)


def _git_common_dir(base_real: str) -> str | None:
    """Realpath of the git common dir for `base_real` (where refs, config
    and hooks actually live — the same dir across every linked worktree of
    one repo), or `None` if `base_real` is not a git checkout at all.
    Duplicated in miniature from `script_effects.py`'s own `_git_common_dir`
    (that module already imports THIS one for `_resources.FileResource`, so
    importing back here would cycle) — both only need git's own documented
    `.git`-file/`commondir` resolution, nothing else, so keeping two small
    copies in sync by hand is cheaper than restructuring the import graph
    for one four-line function."""
    git_path = os.path.join(base_real, ".git")
    if os.path.isdir(git_path):
        return os.path.realpath(git_path)
    if not os.path.isfile(git_path):
        return None
    try:
        with open(git_path, encoding="utf-8") as fh:
            text = fh.read().strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    target = text[len("gitdir:") :].strip()
    resolved = target if os.path.isabs(target) else os.path.join(base_real, target)
    common_file = os.path.join(resolved, "commondir")
    if os.path.isfile(common_file):
        try:
            with open(common_file, encoding="utf-8") as fh:
                common = fh.read().strip()
        except OSError:
            return os.path.realpath(resolved)
        base = common if os.path.isabs(common) else os.path.join(resolved, common)
        return os.path.realpath(base)
    return os.path.realpath(resolved)


def _git_protected_surfaces(base_real: str) -> list[str]:
    """The git configuration/hook surfaces a read-only git subcommand's own
    documented contract can be redirected through (F2): `core.fsmonitor` on
    `status`, `diff.external`/`GIT_EXTERNAL_DIFF`/a configured `diff.<driver>.
    command` textconv filter on `diff`/`log -p`/`show`, and `log.
    showSignature`-triggered `gpg` invocation are each driven by repo
    CONFIG, not by anything on the command line `tool_contracts.py` reviews
    — so the config/hook files themselves, not a flag, are the actual
    control surface and must be protected the same way the contract table
    and script registry already are. Returns `[]` when `base_real` is not a
    git checkout at all (nothing to protect).

    Named residual, NOT covered here: `core.hooksPath` can retarget the
    hooks directory entirely, but resolving its live value would require
    parsing `config`'s INI grammar (multiple `[core]` sections, quoting,
    include directives) rather than a cheap fixed-path join — out of scope
    for this fail-toward-protecting helper; the default `<common>/hooks`
    location is protected regardless, and a custom `core.hooksPath` target
    is not."""
    common = _git_common_dir(base_real)
    if common is None:
        return []
    surfaces = [
        os.path.join(common, "config"),
        os.path.join(common, "config.worktree"),
        os.path.join(common, "hooks"),
        os.path.join(common, "info", "attributes"),
    ]
    worktrees_dir = os.path.join(common, "worktrees")
    if os.path.isdir(worktrees_dir):
        try:
            names = os.listdir(worktrees_dir)
        except OSError:
            names = []
        for name in names:
            surfaces.append(os.path.join(worktrees_dir, name, "config.worktree"))
    return [_realpath(p) for p in surfaces]


def protected_permission_surfaces(
    *,
    repo_root: str | None = None,
    delivery_worktree: str | None = None,
    ledger_dir: str | None = None,
) -> list[str]:
    """The realpath-resolved set of files/dirs a `file` resource may never
    be, or lie under, regardless of what any actor approved: the effective
    tool-contracts table and script-effects registry under EITHER of the
    plan's `repo_root`/`delivery_worktree` (a plan may reference either
    checkout), plus an `AGENTCTL_TOOL_CONTRACTS` env override if set, plus
    the effective order-approvals ledger directory (the `ledger_dir` this
    resolution context is using, plus an `AGENTCTL_ORDER_APPROVALS_DIR` env
    override if set), plus — for each base — its git common dir's `config`,
    `config.worktree`, every linked worktree's `worktrees/*/config.worktree`,
    its `hooks/` directory, its `info/attributes`, and the base's own
    `.gitattributes` (these are the config/hook surfaces a nominally
    read-only git subcommand's execution can be redirected through — see
    `_git_protected_surfaces`). Two bases can yield the same realpath (a
    delivery worktree and its repo_root sharing a filesystem) — callers do
    not need a deduplicated list, `_path_is_or_under` doesn't care about
    duplicates."""
    surfaces: list[str] = []
    for base in (repo_root, delivery_worktree):
        if not base:
            continue
        surfaces.append(_realpath(os.path.join(base, "scripts", "agentctl", "tool_contracts.toml")))
        surfaces.append(_realpath(os.path.join(base, "scripts", "script_effects.toml")))
        base_real = _realpath(base)
        surfaces.append(_realpath(os.path.join(base_real, ".gitattributes")))
        surfaces.extend(_git_protected_surfaces(base_real))
    contracts_override = os.environ.get("AGENTCTL_TOOL_CONTRACTS")
    if contracts_override:
        surfaces.append(_realpath(contracts_override))
    if ledger_dir:
        surfaces.append(_realpath(ledger_dir))
    ledger_override = os.environ.get("AGENTCTL_ORDER_APPROVALS_DIR")
    if ledger_override:
        surfaces.append(_realpath(ledger_override))
    return surfaces


class Resource:
    """Base shape every typed resource shares: a `kind` string and a
    `covers` method. Deliberately NOT a dataclass — a dataclass base
    contributing a `kind` field would force every subclass's OWN fields to
    sit after `kind` in generated-`__init__` field order regardless of
    where the subclass declares it, and a subclass giving `kind` a default
    then makes any of its own non-default fields declared after it invalid
    (`TypeError: non-default argument follows default argument`). Each
    subclass below is its own independent frozen dataclass instead, free to
    order `kind` last with a default of its own.

    `covers` is asymmetric — `self` is the APPROVED resource, `other` is
    the REQUESTED one."""

    kind: str

    def covers(self, other: "Resource", *, protected: list[str] | None = None) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError


@dataclass(frozen=True)
class FileResource(Resource):
    """A file or directory, at a given access `mode` (`"read"` | `"write"`).
    `path` is stored realpath-resolved at construction so every comparison
    — including against `protected_permission_surfaces` and against
    `widening_targets`' protected roots — is symlink- and `..`-safe."""

    path: str
    mode: str
    kind: str = "file"

    def __post_init__(self) -> None:
        if self.mode not in ("read", "write"):
            raise ValueError(f"FileResource mode must be 'read' or 'write', got {self.mode!r}")
        object.__setattr__(self, "path", _realpath(self.path))

    def covers(self, other: "Resource", *, protected: list[str] | None = None) -> bool:
        if not isinstance(other, FileResource):
            return False
        if self.mode == "read" and other.mode == "write":
            return False
        req = other.path

        # The harness's own protected roots ($HOME, the agent home, ...) are
        # never covered by any file approval, however broad — same bias
        # `widening_targets.add_dir_is_or_contains_protected_root`/
        # `add_dir_under_protected_root` already enforce for add_dir grants.
        if widening_targets.add_dir_is_or_contains_protected_root(req):
            return False
        if widening_targets.add_dir_under_protected_root(req):
            return False

        protected = protected or []
        if any(_path_is_or_under(p, req) for p in protected):
            # The requested resource IS, or lies under, a protected
            # permission surface (the contract table, the script-effects
            # registry, the order-approvals ledger dir) — never covered,
            # by any approval, exact or broader.
            return False
        if any(_path_is_or_under(req, p) and req != p for p in protected):
            # The requested resource CONTAINS a protected surface as a
            # descendant (e.g. requesting write on the directory that
            # holds the contract table) — covered ONLY by an approval that
            # is EXACTLY this same resource; ordinary broader-covers-
            # narrower containment is disabled here so a broad approval
            # cannot reach the protected surface through a wider request.
            return self.path == req

        return self.path == req or _path_is_or_under(self.path, req)


@dataclass(frozen=True)
class VcsRefResource(Resource):
    """A version-control ref at a given `op` — `"push"` or `"land"`. `op`
    is part of the identity: an approved `land` never covers a `push`
    request and vice versa (revision 6, R1) — landing and force/plain
    pushing are different real-world effects even against the same ref."""

    remote: str
    ref: str
    op: str
    kind: str = "vcs_ref"

    def covers(self, other: "Resource", *, protected: list[str] | None = None) -> bool:
        if not isinstance(other, VcsRefResource):
            return False
        return (self.remote, self.ref, self.op) == (other.remote, other.ref, other.op)


@dataclass(frozen=True)
class SpecialistResource(Resource):
    """A specialist role a stage may spawn (e.g. `"developer"`,
    `"planner"`) — exact match, no hierarchy between roles."""

    role: str
    kind: str = "specialist"

    def covers(self, other: "Resource", *, protected: list[str] | None = None) -> bool:
        if not isinstance(other, SpecialistResource):
            return False
        return self.role == other.role


@dataclass(frozen=True)
class ServiceResource(Resource):
    """A named external service a command calls, at a given `op_class`
    (e.g. `"read"` / `"write"` / `"spend"`) — exact match on both. `op_class`
    is part of the identity for the same reason `VcsRefResource.op` is: an
    approval for one class of operation on a service (a read-only lookup)
    must never cover a different class (a spend-incurring call) against the
    same service name."""

    name: str
    op_class: str
    kind: str = "service"

    def covers(self, other: "Resource", *, protected: list[str] | None = None) -> bool:
        if not isinstance(other, ServiceResource):
            return False
        return (self.name, self.op_class) == (other.name, other.op_class)


@dataclass(frozen=True)
class DatasetResource(Resource):
    """A dataset a command reads or writes, identified by the `system` that
    hosts it (e.g. a warehouse/orchestration-platform name) plus a
    `locator` within that system (a table/path name) — exact match on both.
    A bare name is not enough: two systems can reuse the same locator
    spelling for unrelated datasets, and treating that as a match would let
    an approval for one silently cover the other."""

    system: str
    locator: str
    kind: str = "dataset"

    def covers(self, other: "Resource", *, protected: list[str] | None = None) -> bool:
        if not isinstance(other, DatasetResource):
            return False
        return (self.system, self.locator) == (other.system, other.locator)
