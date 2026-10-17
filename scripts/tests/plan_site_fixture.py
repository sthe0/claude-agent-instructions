"""Shared fixture of the plan-view suites: a four-stage plan whose every key outside the
projected fields carries its own start/end token, and launchers that run each spawner's
real prompt assembly with only the model launch stubbed.

Reliance graph: 2 relies on 1, 3 on 2, 4 on 3. Stage 1 relies on nothing (a source stage),
stage 3 has a blank done criterion (an empty interface), stages 2 and 4 are ordinary.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent

STAGES = (1, 2, 3, 4)
STAGE_KEYS = (
    "knowledge", "material", "invariants", "means", "method", "procedure", "preconditions",
    "conditions", "capability_required",
)
META_KEYS = ("landing_waiver",)
NODES = ("meta", *STAGES)


def token(node, key: str, end: str) -> str:
    return f"QSENT{node}x{key}x{end}"


def tokens_of(node) -> list[str]:
    keys = META_KEYS if node == "meta" else STAGE_KEYS
    return [token(node, key, end) for key in keys for end in "AZ"]


def hidden_tokens(shown) -> list[str]:
    return [t for node in NODES if node not in shown for t in tokens_of(node)]


def shown_tokens(shown) -> list[str]:
    return [t for node in NODES if node in shown for t in tokens_of(node)]


def _value(node, key: str) -> str:
    return f"{token(node, key, 'A')} text of {key} {token(node, key, 'Z')}"


def _stage(n: int, *, relies_on: "int | None", blank_done: bool = False) -> str:
    lines = [
        "[[stage]]",
        f"index = {n}",
        f"depends_on = [{n - 1 if n > 1 else ''}]",
        f'title = "Stage {n} title"',
        'executor = "spawn:developer"',
        f'expected_result_image = "result image of stage {n}"',
        'criterion_type = "measurable"',
        f'done_criterion = "{" " if blank_done else f"done criterion of stage {n}"}"',
        'verify_command = "pytest -q"',
        f'output_artifacts = ["out/stage{n}.txt"]',
    ]
    for key in STAGE_KEYS:
        target = {"means": "means", "method": "method"}.get(key, key)
        lines.append(f'{target} = "{_value(n, key)}"')
    lines += [
        "",
        "[stage.principle]",
        'statement = "s"',
        'source = "s"',
        'derivation = "d"',
        'confidence = "high"',
        'refutation = "r"',
        "",
    ]
    if relies_on is not None:
        lines += ["[[stage.supplies]]", f"on = {relies_on}", ""]
    return "\n".join(lines)


def plan_text(*, extra_stage_fields: "dict[int, str] | None" = None) -> str:
    meta = "\n".join([
        "[meta]",
        'task_id = "site-fixture"',
        'goal = "goal of the fixture plan"',
        'done_criterion = "dc"',
        'criterion_type = "measurable"',
        'weight_class = "substantive"',
        'external_research = "none applies"',
        f'landing_waiver = "{_value("meta", "landing_waiver")}"',
        "",
    ])
    body = "\n".join([
        _stage(1, relies_on=None),
        _stage(2, relies_on=1),
        _stage(3, relies_on=2, blank_done=True),
        _stage(4, relies_on=3),
    ])
    footer = '[[final_check]]\ncommand = "true"\nlabel = "fixture check"\n'
    return "\n".join([meta, body, footer])


def write_plan(directory: Path, name: str = "plan.toml") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(plan_text(), encoding="utf-8")
    return path


LONG_FIELD = "y" * 5000


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_hypertext_plan(directory: Path, *, long_method: bool = False) -> Path:
    """The fixture plan with every stage held in its own file and stage 2 behind a
    reference to a reference. `long_method` makes stage 2's `method` longer than any
    inline limit."""
    text = plan_text()
    head, *blocks = text.split("[[stage]]\n")
    blocks[-1], footer = blocks[-1].split("[[final_check]]\n")
    parts = directory / "parts"
    parts.mkdir(parents=True, exist_ok=True)
    refs = []
    for number, block in zip(STAGES, blocks):
        block = block.replace("[stage.principle]", "[principle]").replace(
            "[[stage.supplies]]", "[[supplies]]")
        if long_method and number == 2:
            block = block.replace(_value(2, "method"), LONG_FIELD)
        data = block.encode("utf-8")
        name = f"stage-{number}.toml"
        (parts / name).write_bytes(data)
        if number == 2:
            hop = f'ref = "{name}"\nsha256 = "{_sha256(data)}"\n'.encode("utf-8")
            name = "stage-2-hop.toml"
            (parts / name).write_bytes(hop)
            data = hop
        refs.append(f'  {{ ref = "parts/{name}", sha256 = "{_sha256(data)}" }}')
    index = directory / "plan.toml"
    index.write_text(
        "stage = [\n" + ",\n".join(refs) + "\n]\n" + head + "\n[[final_check]]\n" + footer,
        encoding="utf-8")
    return index


def load_script(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeProc:
    def __init__(self) -> None:
        self.returncode = 0
        self.stdin_input = None
        self.pid = 424242

    def communicate(self, input=None):
        self.stdin_input = input
        return ('{"result": "COMPLETED: ok", "cost_usd": 0}', "")


def launch_specialist(monkeypatch, tmp_path: Path, argv: list[str], *, configure=None):
    """Run spawn-specialist's `main(argv)` with only the model launch stubbed; returns
    (exit code, the launched prompt or None, the launched command or None). `configure`
    receives the loaded module before the run."""
    mod = load_script("spawn-specialist.py", "spawn_specialist_site")
    captured: dict = {}

    def fake_launch(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["proc"] = FakeProc()
        return captured["proc"]

    monkeypatch.setattr(mod.proc_tree, "launch_supervised", fake_launch)
    monkeypatch.setattr(mod.proc_tree, "install_teardown", lambda p: None)
    monkeypatch.setattr(mod.proc_tree, "kill_tree", lambda p: None)
    monkeypatch.setattr(mod, "_snapshot_transcripts", lambda *a, **k: set())
    monkeypatch.setattr(mod, "_discover_transcript_path", lambda *a, **k: None)
    monkeypatch.setattr(mod, "permissions_digest", lambda *a, **k: "")
    monkeypatch.setattr(mod, "log_cost_entry", lambda entry: None)
    monkeypatch.setattr(mod, "log_refused", lambda *a, **k: None)
    monkeypatch.setattr(mod, "deregister_child_scope", lambda *a, **k: None)
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/claude")
    sysprompt = tmp_path / "sysprompt.md"
    sysprompt.write_text("system prompt", encoding="utf-8")
    monkeypatch.setattr(mod, "composed_system_prompt_file", lambda skill: sysprompt)
    if configure is not None:
        configure(mod)
    rc = mod.main(argv)
    proc = captured.get("proc")
    return rc, (proc.stdin_input if proc else None), captured.get("cmd")


def specialist_argv(plan: Path, *extra: str, kind: str = "developer") -> list[str]:
    return [
        "--kind", kind, "--plan", str(plan), "--done-criterion", "done",
        "--criterion-type", "measurable", "--complexity", "medium", "--effort", "medium",
        *extra,
    ]


def launch_cursor(monkeypatch, tmp_path: Path, argv: list[str]):
    """Run spawn-cursor-specialist's `main(argv)` with only the model launch stubbed;
    returns (exit code, the launched prompt or None)."""
    mod = load_script("spawn-cursor-specialist.py", "spawn_cursor_specialist_site")
    captured: dict = {}

    def fake_launch(cmd, **kwargs):
        captured["proc"] = FakeProc()
        return captured["proc"]

    monkeypatch.setattr(mod.proc_tree, "launch_supervised", fake_launch)
    monkeypatch.setattr(mod.proc_tree, "install_teardown", lambda p: None)
    monkeypatch.setattr(mod.proc_tree, "kill_tree", lambda p: None)
    monkeypatch.setattr(mod, "permissions_digest", lambda *a, **k: "")
    monkeypatch.setattr(mod, "log_cost_entry", lambda entry: None)
    monkeypatch.setattr(mod, "log_refused", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_build_extraction", lambda *a, **k: None)
    monkeypatch.setattr(mod, "resolve_api_key", lambda *a, **k: "stub-key")
    monkeypatch.setattr(mod.shutil, "which",
                        lambda name: "/usr/bin/agent" if name != "timeout" else None)
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: developer\n---\n\nspecialization body\n", encoding="utf-8")
    monkeypatch.setattr(mod, "skill_path", lambda kind: skill)
    rc = mod.main(["--workspace", str(tmp_path), *argv])
    proc = captured.get("proc")
    return rc, (proc.stdin_input if proc else None)


def cursor_argv(plan: Path, *extra: str) -> list[str]:
    return [
        "--kind", "developer", "--plan", str(plan), "--done-criterion", "done",
        "--criterion-type", "measurable", *extra,
    ]
