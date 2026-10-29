"""Order provenance on regularities: `order_derived` is a root of a regularity beside an
experience/case source and `rule_derived`; its quote is stored verbatim; it is a defect on a
principle. One test per rule: `promote` and `verify-principle-provenance` run in-process through
`main(argv)` over a memory under tmp_path, so replacing one library function changes what the
command does."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

DATE = "2026-10-29"
TASK = "grounding-norm"
QUOTE = "The ledger is written only by the engine."


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from lib import worktree_route
    monkeypatch.setenv("AGENTCTL_EDIT_LEDGER", str(tmp_path / "edit-ledger.jsonl"))
    monkeypatch.setenv("CLAUDE_MEMORY_RETRIEVAL_LEDGER", str(tmp_path / "retrieval.jsonl"))
    monkeypatch.setenv("CLAUDE_SAME_DIFFICULTY_JUDGE_BUDGET_S", "0")
    monkeypatch.setenv("AGENTCTL_ADVISOR", "0")
    monkeypatch.setenv(worktree_route.GUARD_ENV, "1")


def _run(module, argv) -> int:
    try:
        return module.main([str(a) for a in argv])
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


class World:
    """`<tmp>/memory-global/leaves` with an experience leaf `exp` (not yet generalized)."""

    def __init__(self, tmp: Path) -> None:
        self.root = tmp
        self.leaves = tmp / "memory-global" / "leaves"
        self.regs = self.leaves / "regularities"
        for d in (self.leaves / "experience", self.regs, self.leaves / "principles"):
            d.mkdir(parents=True)
        (self.leaves / "experience" / "exp.md").write_text(
            "---\nname: exp\ndescription: a sample\ntype: reference\nschema: difficulty/v1\n"
            f"generalized_into: []\ncreated: {DATE}\nlast_verified: {DATE}\n---\n\n# Exp\n",
            encoding="utf-8")

    def leaf(self, area: str, slug: str, fields: str, body: str) -> Path:
        path = self.leaves / area / f"{slug}.md"
        path.write_text(f"---\nname: {slug}\ndescription: a sample\ntype: reference\n{fields}"
                        f"created: {DATE}\nlast_verified: {DATE}\n---\n\n# Leaf\n\n{body}",
                        encoding="utf-8")
        return path

    def order_reg(self, slug="order-reg", introduced=True, section="## Derived from order\n\n"
                  + QUOTE + "\n") -> Path:
        return self.leaf("regularities", slug,
                         "schema: regularity/v1\ngeneralized: 1\ninduced_from: []\n"
                         f"order_derived: {TASK}\n" + (f"introduced_by: {TASK}\n" if introduced else ""),
                         "## Regularity\n\nA statement.\n\n" + section)

    def findings(self) -> set[str]:
        from lib import principle_provenance as pp
        return {f.kind for f in pp.unaccounted(pp.load_corpus(self.leaves))}

    def verify(self) -> int:
        return _run(_load("verify_pp_order", "verify-principle-provenance.py"),
                    ["--root", self.root])

    def promote(self, *extra) -> int:
        re_mod = _load("record_experience_order", "record-experience.py")
        return _run(re_mod, ["promote", "--root", self.root / "memory-global", "--slug", "new-reg",
                             "--name", "A new regularity", "--description", "a promoted one",
                             "--generality", "1", "--statement", "A statement.", "--date", DATE,
                             *extra])

    @property
    def written(self) -> Path:
        return self.regs / "new-reg.md"


def test_order_derived_is_a_root(tmp_path):
    from lib import principle_provenance as pp
    w = World(tmp_path)
    w.order_reg()
    corpus = pp.load_corpus(w.leaves)
    assert pp.rule_mark_state(corpus.find("order-reg"), corpus) == "ok"
    assert pp.NO_SOURCE not in w.findings()


def test_verify_accepts_order_derived(tmp_path):
    w = World(tmp_path)
    w.order_reg()
    assert w.verify() == 0


def test_order_quote_required(tmp_path):
    from lib import principle_provenance as pp
    w = World(tmp_path)
    w.order_reg(section="")
    assert pp.ORDER_NO_QUOTE in w.findings()
    assert w.verify() == 1


def test_order_quote_empty_refused(tmp_path):
    from lib import principle_provenance as pp
    w = World(tmp_path)
    w.order_reg(section="## Derived from order\n\n\n   \n\n## Refutation\n\nA claim.\n")
    assert pp.ORDER_NO_QUOTE in w.findings()


@pytest.mark.parametrize("quote", ["", "   "])
def test_promote_empty_quote_refused(tmp_path, quote):
    w = World(tmp_path)
    assert w.promote("--order-derived", TASK, "--order-quote", quote) != 0
    assert not w.written.exists()


def test_promote_writes_order_fields(tmp_path):
    from lib import principle_provenance as pp
    w = World(tmp_path)
    assert w.promote("--order-derived", TASK, "--order-quote", QUOTE) == 0
    text = w.written.read_text(encoding="utf-8")
    assert "schema: regularity/v1" in text
    assert f"order_derived: {TASK}" in text
    assert "## Derived from order" in text
    assert pp.order_quote(text) == QUOTE
    assert w.findings() == set()


def test_order_derived_introduced_defaulted(tmp_path):
    w = World(tmp_path)
    assert w.promote("--order-derived", TASK, "--order-quote", QUOTE) == 0
    assert f"introduced_by: {TASK}" in w.written.read_text(encoding="utf-8")


def test_order_no_introduced_flagged(tmp_path):
    from lib import principle_provenance as pp
    w = World(tmp_path)
    w.order_reg(introduced=False)
    assert pp.ORDER_NO_INTRODUCED in w.findings()
    assert w.verify() == 1


def test_introduced_by_stamped(tmp_path):
    from lib import principle_provenance as pp
    w = World(tmp_path)
    assert w.promote("--induced-from", "exp", "--introduced-by", "other-task") == 0
    text = w.written.read_text(encoding="utf-8")
    assert "introduced_by: other-task" in text
    assert pp.introduced_by(text) == "other-task"


def test_promote_order_without_induced(tmp_path):
    w = World(tmp_path)
    assert w.promote("--order-derived", TASK, "--order-quote", QUOTE) == 0
    assert w.written.is_file()


def test_promote_order_on_principle_refused(tmp_path):
    w = World(tmp_path)
    w.order_reg(slug="base-reg")
    assert w.promote("--principle", "--grounded-in", "base-reg", "--order-derived", TASK,
                     "--order-quote", QUOTE) != 0
    assert not (w.leaves / "principles" / "new-reg.md").exists()


def test_order_derived_principle_flagged(tmp_path):
    from lib import principle_provenance as pp
    w = World(tmp_path)
    w.order_reg(slug="base-reg")
    w.leaf("principles", "bad-norm",
           "schema: principle/v1\ngenerality: 1\ngrounded_in: [regularity:base-reg]\n"
           f"order_derived: {TASK}\nintroduced_by: {TASK}\n",
           "## Principle\n\nA norm.\n\n## Derived from order\n\n" + QUOTE + "\n")
    assert pp.ORDER_ON_PRINCIPLE in w.findings()
    assert w.verify() == 1
