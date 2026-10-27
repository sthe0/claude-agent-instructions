"""Shared builders for the resource-grant hook tests (`test_hook_resource_grant_check.py`,
`test_resource_grant_replay.py`, `e2e_resource_grant_hook.py`).

Both the unit tests and the replay need a hook module loaded from its hyphenated file name and a
linked-child environment: a norm-handoff record built the way `spawn-specialist.py` builds it (the
stage's resource set from `plan_resources.resource_set_from_entries`, the workdir baseline under
acceptEdits, the child's real `permissions.allow` from `build_child_settings`), written under a
temporary state root and named by `AGENTCTL_NORM_HANDOFF`.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agentctl import grants, norm_handoff, plan_resources  # noqa: E402
from agentctl import resources as R  # noqa: E402
from agentctl.stage_norm import StageNorm  # noqa: E402
from agentctl.state import Actor, Criterion, Means, Stage, Subject  # noqa: E402

HOOK_FILE = SCRIPTS / "hook-resource-grant-check.py"
SPAWN_FILE = SCRIPTS / "spawn-specialist.py"
PARENT_SESSION = "parent-session-0001"
STAGE_INDEX = 1


def load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_hook():
    return load_script("hook_resource_grant_check_under_test", HOOK_FILE)


def load_spawn():
    return load_script("spawn_specialist_under_test", SPAWN_FILE)


def stage_for(kind: str, branch: "str | None") -> Stage:
    return Stage(
        index=STAGE_INDEX, title="t", subject=Subject(material="m", result="r"),
        means=Means(means="Edit", method="apply"), actor=Actor(executor=f"spawn:{kind}"),
        criterion=Criterion(criterion_type="measurable", done_criterion="d"), branch=branch,
    )


@dataclass
class LinkedChild:
    """A linked child as the hook sees it: the environment, the permission mode and the record."""

    env: dict
    mode: str
    cwd: str
    record: dict
    state_root: Path

    def payload(self, tool: str, **tool_input) -> str:
        return json.dumps({"tool_name": tool, "tool_input": tool_input, "cwd": self.cwd,
                           "permission_mode": self.mode})

    def log_rows(self) -> list:
        path = self.state_root / "resource-uses" / f"{PARENT_SESSION}-{STAGE_INDEX}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def link_child(
    spawn, state_root: Path, venue, *, kind: str = "developer", branch: "str | None" = None,
    rules=(), mode: "str | None" = None, extra_resources=(), extra_patterns=(), allow=None,
    remote_head: str = "main",
) -> LinkedChild:
    """Write the handoff record `spawn-specialist.py` would write for a stage of `kind` running in
    `venue` under `rules` and return the environment of the child it spawns.

    `allow` overrides the child's `permissions.allow` (default: what `build_child_settings` yields
    for these grants, kind baselines included); `extra_resources` / `extra_patterns` add to the set."""
    venue = str(venue)
    entries = [{"rule": rule} for rule in rules]
    stage = stage_for(kind, branch)
    derived, _dropped = grants.derive_stage_grants(stage, venue=venue, remote_head=remote_head)
    resource_set = plan_resources.resource_set_from_entries(entries, stage, venue, derived.resources)
    resolved_mode = mode or spawn.resolve_permission_mode(Namespace(kind=kind, permission_mode=None), entries)
    if spawn.workdir_baseline_applies(kind, resolved_mode, entries):
        resource_set.resources.append(R.FileResource(venue, "write"))
    resource_set.resources.extend(extra_resources)
    resource_set.rule_patterns.extend(extra_patterns)
    if allow is None:
        # `Read(<dir>/**)` grants of the replay fixture are refused as glob paths by today's validator.
        accepted = [e for e in entries if not e["rule"].startswith("Read(")]
        settings = spawn.build_child_settings(kind, None, None, accepted, workdir=venue)
        allow = settings.get("permissions", {}).get("allow", [])
    packed = resource_set.to_dict()
    record = norm_handoff.build_record(
        parent_session=PARENT_SESSION, stage_index=STAGE_INDEX, kind=kind,
        norm=StageNorm.from_stage(stage), grants=entries, depth=1, parent_lineage=None,
        resources=packed["resources"], rule_patterns=packed["rule_patterns"],
        permissions_allow=list(allow),
    )
    path, sha = norm_handoff.write_record(state_root, record)
    env = {"AGENT_RECURSION_DEPTH": "1", norm_handoff.NORM_HANDOFF_ENV: norm_handoff.format_env_value(path, sha)}
    return LinkedChild(env=env, mode=resolved_mode, cwd=venue, record=record, state_root=Path(state_root))
