"""Refuse a stage grant collision when the plan is written, not when the child is launched.

Difficulty removed: `spawn-specialist.py::build_child_settings` refuses a child
`--settings` payload in which an Edit deny covers an Edit allow entirely — the
client resolves deny over allow, so such an allow would be void. That refusal used
to fire only at dispatch, after the plan had been reviewed and approved, so a plan
whose own refs produced the collision (a venue-relative write ref beside an
absolute ref into the same directory: DR-E's Edit allow under DR-R's read add_dir
deny) passed every authoring gate and failed at launch. This module runs the very
same assembler over every spawn stage at submit-plan / plan-grants time.

The authoring check calls the spawn-time assembler; it does not restate it.

`mixed_spelling_advisories` is the advisory companion: a stage that spells one
directory both absolute and venue-relative is the authoring habit that produces
the collision, but it is not itself a collision (a thinker stage derives no Edit
allow), so it is reported, never refused.
"""
from __future__ import annotations

import functools
import importlib.util
import os
from pathlib import Path, PurePosixPath

from . import grants as _grants
from . import render as _render
from .plan import PlanDoc, _venue_for

_SPAWN_SPECIALIST = Path(__file__).resolve().parent.parent / "spawn-specialist.py"


@functools.lru_cache(maxsize=1)
def _spawn_specialist():
    spec = importlib.util.spec_from_file_location("spawn_specialist_grant_shadow", _SPAWN_SPECIALIST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dispatch_workdir(doc: PlanDoc) -> "str | None":
    """The `--workdir` dispatch hands the child. Deliberately no existence check
    and no `os.getcwd()` fallback: the delivery worktree may not exist yet at
    authoring time, and the author's own cwd says nothing about where the child
    will run."""
    return doc.meta.delivery_worktree or doc.meta.repo_root or None


def _guard_exempt_abs_paths(stage, workdir: "str | None") -> list[str]:
    if workdir is None:
        return []
    return [
        os.path.normpath(Path(workdir) / exempt)
        for exempt in stage.actor.guard_exempt_paths
        if ".." not in Path(exempt).parts
    ]


def plan_grant_shadow_problems(doc: PlanDoc) -> list[str]:
    """One problem per spawn stage whose child settings `build_child_settings`
    would refuse, worded as the spawn-time refusal itself."""
    spawn = _spawn_specialist()
    venue = _venue_for(doc)
    workdir = _dispatch_workdir(doc)
    problems: list[str] = []
    for s in doc.stages:
        if not s.is_spawn():
            continue
        declared, derived, _dropped = _render._stage_declared_and_derived_grants(s, venue)
        entries = [r.to_dict() for r in declared.allow] + [a.to_dict() for a in declared.add_dirs]
        entries += [r.to_dict() for r in derived.allow] + [a.to_dict() for a in derived.add_dirs]
        try:
            spawn.build_child_settings(
                s.spawn_kind(),
                engine_grants=entries,
                workdir=workdir,
                guard_exempt_paths=_guard_exempt_abs_paths(s, workdir),
            )
        except (spawn.GrantShadowError, _grants.GrantValidationError) as exc:
            problems.append(f"stage {s.index} ({s.title}): {exc}")
    return problems


def _stage_refs(s) -> list[tuple[str, str]]:
    return [
        (ref, list_name)
        for list_name, refs in (
            ("output_artifacts", s.output_artifacts),
            ("material_refs", s.subject.material_refs),
            ("knowledge_refs", s.subject.knowledge_refs),
        )
        for ref in refs
    ]


def _mixed_pair(first, second, venue: str):
    """`(absolute, relative)` when exactly one of the two refs is absolute and the
    relative one, resolved against `venue`, lies at or under the absolute one's
    directory; None otherwise."""
    if first[0].startswith("/") == second[0].startswith("/"):
        return None
    absolute, relative = (first, second) if first[0].startswith("/") else (second, first)
    directory = PurePosixPath(os.path.normpath(absolute[0])).parent
    resolved = PurePosixPath(os.path.normpath(os.path.join(venue, relative[0])))
    under = resolved == directory or directory in resolved.parents
    if not under:
        return None
    return absolute, relative


def mixed_spelling_advisories(doc: PlanDoc) -> list[str]:
    """One advisory per same-stage ref pair that spells one directory both
    absolute and venue-relative. Advisory only: it never blocks a submission."""
    venue = _dispatch_workdir(doc)
    if venue is None:
        return []
    advisories: list[str] = []
    for s in doc.stages:
        refs = _stage_refs(s)
        for i, first in enumerate(refs):
            for second in refs[i + 1:]:
                pair = _mixed_pair(first, second, venue)
                if pair is None:
                    continue
                (abs_ref, abs_list), (rel_ref, rel_list) = pair
                advisories.append(
                    f"stage {s.index} ({s.title}): {abs_list} spells {abs_ref!r} absolute "
                    f"while {rel_list} spells {rel_ref!r} relative to the venue, in the same "
                    f"directory {str(PurePosixPath(os.path.normpath(abs_ref)).parent)!r} — spell "
                    f"both relative when the stage writes into that directory, both absolute "
                    f"only when it writes nothing there"
                )
    return advisories
