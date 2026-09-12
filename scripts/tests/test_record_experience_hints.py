"""Tests for the runnable-path hints in record-experience.py.

Both hints that tell the caller to run `extend --leaf ...` must print an
argument the executor can actually read — an absolute filesystem path — while
keeping the leaf name as the human-scannable label. Cases (a)-(d) are RED
drivers: each fails against the pre-change hints for its own reason. Case (e)
is a green-before guard on the tier gate: no tier prints a runnable line today,
so it cannot be red before the change; its job is to catch a tier gate written
wrong or later removed, and it therefore binds the principles listing's FULL
stdout rather than the weaker "no --leaf line appears".

Every case runs against a controlled temp corpus AND a redirected edit ledger:
cmd_new/cmd_extend/cmd_set_last_verified all call edit_ledger.stamp, which
writes to the durable attribution ledger under the agent config root — outside
any worktree, and unfiltered by exempt_paths.is_ledger_noise.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _load():
    path = SCRIPTS_DIR / "record-experience.py"
    spec = importlib.util.spec_from_file_location("record_experience", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


re_mod = _load()

LEAF_NAME = "2026-01-01-alpha.md"
LEAF_DESC = "the printed hint named a leaf the consumer could not open"
LEAF_GROUND = (
    "A tool that prints a command must print one that runs. record-experience "
    "printed a leaf name into a slot the caller executes."
)

# The `--leaf` argument out of either hint: `extend --leaf <arg>`, where the
# refusal wraps the command in backticks and the listing does not.
HINT_RE = re.compile(r"extend --leaf ([^\s`]+)")


@pytest.fixture(autouse=True)
def _redirect_edit_ledger(tmp_path, monkeypatch):
    """No case may append rows about temp paths to the live durable ledger."""
    monkeypatch.setenv("AGENTCTL_EDIT_LEDGER", str(tmp_path / "edit-log.jsonl"))


def _write_leaf(directory: Path, name: str, desc: str, body: str,
                section: str = "Difficulty") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    leaf = directory / name
    leaf.write_text(
        "---\n"
        f"name: {name[:-3]}\n"
        f"description: {desc}\n"
        "schema: difficulty/v1\n"
        "created: 2026-01-01\n"
        "last_verified: 2026-01-01\n"
        "---\n\n"
        f"# {desc}\n\n"
        f"## {section}\n{body}\n\n"
        "## Contexts\n\n### 2026-01-01 — first context\n",
        encoding="utf-8")
    return leaf


def _ground_of(leaf: Path, section: str = "Difficulty") -> str:
    """The comparison ground cmd_new builds for an existing leaf.

    Reproduced here so the fragmentation guard is guaranteed to fire — the
    guard firing is this test's precondition, not the thing it measures.
    """
    text = leaf.read_text(encoding="utf-8")
    fm = re_mod.FRONTMATTER.match(text)
    desc = ""
    if fm:
        dm = re.search(r"^description:\s*(.*)$", fm.group(1), re.MULTILINE)
        desc = dm.group(1).strip() if dm else ""
    span = re_mod.section_span(text, section)
    return desc + " " + (text[span[0]:span[1]] if span else "")


def _new_argv(difficulty: str, *, scope: str, project_dir: str | None = None,
              slug: str = "beta") -> list[str]:
    argv = ["new", "--scope", scope, "--date", "2026-02-02",
            "--slug", slug, "--title", "T", "--description", "d",
            "--confirmed-by", "user", "--difficulty", difficulty,
            "--order", "o", "--criterion", "c",
            "--context-where", "W", "--plan", "P"]
    if project_dir is not None:
        argv += ["--project-dir", project_dir]
    return argv


def _run_expect_exit(argv: list[str]) -> str:
    with pytest.raises(SystemExit) as exc:
        re_mod.main(argv)
    return str(exc.value)


def _assert_runnable(arg: str) -> Path:
    """The hint's argument must run verbatim from any working directory."""
    path = Path(arg)
    assert path.is_absolute(), f"hint argument is not absolute: {arg!r}"
    assert path.exists(), f"hint argument does not exist on disk: {arg!r}"
    return path


def _extend_accepts(arg: str) -> None:
    re_mod.main(["extend", "--leaf", arg, "--date", "2026-02-02",
                 "--context-label", "second", "--context-where", "W", "--plan", "P"])


# --------------------------------------------------------------------------
# (a) RED — cmd_new's refusal, GLOBAL scope (root comes from REPO_ROOT)
# --------------------------------------------------------------------------
def test_a_new_refusal_global_scope_prints_runnable_path(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    exp = repo / "memory-global/leaves/experience"
    leaf = _write_leaf(exp, LEAF_NAME, LEAF_DESC, LEAF_GROUND)
    monkeypatch.setattr(re_mod, "REPO_ROOT", repo)

    msg = _run_expect_exit(_new_argv(_ground_of(leaf), scope="global"))

    assert LEAF_NAME in msg, "the leaf name must survive as the readable label"
    hit = HINT_RE.search(msg)
    assert hit, f"no `extend --leaf <arg>` hint in the refusal: {msg!r}"
    _assert_runnable(hit.group(1))
    _extend_accepts(hit.group(1))
    body = leaf.read_text(encoding="utf-8")
    assert len(re.findall(r"^### ", body, re.MULTILINE)) == 2, "extend did not append"


# --------------------------------------------------------------------------
# (b) RED — cmd_new's refusal, PROJECT scope, RELATIVE --project-dir
# --------------------------------------------------------------------------
def test_b_new_refusal_relative_project_dir_prints_absolute_path(tmp_path, monkeypatch):
    exp = tmp_path / "proj/.claude/agent-memory/experience"
    leaf = _write_leaf(exp, LEAF_NAME, LEAF_DESC, LEAF_GROUND)
    monkeypatch.chdir(tmp_path)

    msg = _run_expect_exit(
        _new_argv(_ground_of(leaf), scope="project", project_dir="proj"))

    assert LEAF_NAME in msg
    hit = HINT_RE.search(msg)
    assert hit, f"no `extend --leaf <arg>` hint in the refusal: {msg!r}"
    _assert_runnable(hit.group(1))
    _extend_accepts(hit.group(1))


# --------------------------------------------------------------------------
# (c) RED — cmd_search's experience listing, RELATIVE --project-dir
# --------------------------------------------------------------------------
def test_c_search_experience_listing_prints_runnable_path(tmp_path, monkeypatch, capsys):
    exp = tmp_path / "proj/.claude/agent-memory/experience"
    _write_leaf(exp, LEAF_NAME, LEAF_DESC, LEAF_GROUND)
    monkeypatch.chdir(tmp_path)

    assert re_mod.main(["search", "--scope", "project", "--project-dir", "proj",
                        "--tier", "experience", "printed hint leaf"]) == 0
    out = capsys.readouterr().out

    assert LEAF_NAME in out, "the leaf name must survive as the scannable label"
    assert LEAF_DESC in out, "the description line must survive"
    hit = HINT_RE.search(out)
    assert hit, f"no runnable `--leaf <path>` line in the listing: {out!r}"
    _assert_runnable(hit.group(1))
    _extend_accepts(hit.group(1))


# --------------------------------------------------------------------------
# (d) RED — both `leaf not found` messages name the expected argument kind
# --------------------------------------------------------------------------
@pytest.mark.parametrize("argv", [
    ["extend", "--leaf", "2026-01-01-alpha", "--date", "2026-02-02",
     "--context-label", "L", "--context-where", "W", "--plan", "P"],
    ["set-last-verified", "--leaf", "2026-01-01-alpha", "--date", "2026-02-02"],
])
def test_d_leaf_not_found_names_expected_kind_and_the_command(argv):
    msg = _run_expect_exit(argv)
    low = msg.lower()
    assert "path" in low, f"failure does not name the expected argument kind: {msg!r}"
    assert "search" in low, f"failure does not point at the command that prints one: {msg!r}"


# --------------------------------------------------------------------------
# (e) GREEN-BEFORE GUARD — the principles listing stays byte-identical
# --------------------------------------------------------------------------
def test_e_principles_listing_carries_no_runnable_line(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    principles = repo / "memory-global/leaves/principles"
    desc = "an executed identifier belongs to the executor namespace"
    body = "A printed command must be runnable as printed."
    leaf = _write_leaf(principles, LEAF_NAME, desc, body, section="Principle")
    monkeypatch.setattr(re_mod, "REPO_ROOT", repo)

    keywords = "printed command"
    span = re_mod.section_span(leaf.read_text(encoding="utf-8"), "Principle")
    sec = leaf.read_text(encoding="utf-8")[span[0]:span[1]]
    score = re_mod.term_score(desc + " " + sec, re_mod.tokenize(keywords))
    assert score > 0, "fixture must be reached — a zero score returns before the listing"

    assert re_mod.main(["search", "--tier", "principles", keywords]) == 0
    out = capsys.readouterr().out

    assert out == (
        "analogous principles (ground a stage in one instead of duplicating):\n"
        f"  [{score:>3}] {LEAF_NAME}\n"
        f"        {desc}\n"
    )
