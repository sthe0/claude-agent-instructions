#!/usr/bin/env python3
"""Reusable resolver: assert every `--deselect` site in a plan names one and the
same set of node ids, each site individually guarding every node it deselects —
plus a REPORT-ONLY sweep of the plan's own prose for sentences that speak of the
tolerated-red set while carrying a cardinality word or a bare definite singular
reference to a member of the set.

Two halves, split along the structural/semantic seam
(memory-global/leaves/regex-not-for-semantic-classification.md):

  * STRUCTURAL — decidable, and BLOCKS. Every `verify_command` and every
    `final_check[*].command` containing a structurally-present `--deselect`
    (one outside any quoting or substitution — see below) is a SITE. A site
    DESELECTS the node ids named after each structurally-present `--deselect`
    flag, in either the `--deselect <node>` or the `--deselect=<node>`
    spelling; a structurally-present occurrence in neither spelling, or a
    fragment this resolver cannot read as shell structure at all, is itself a
    violation naming the site, never a silent skip — a site whose every
    occurrence went unread would otherwise deselect an unbounded set and
    report a clean, empty one. A node is GUARDED when the command contains a
    single-node pytest invocation naming that node — a node id, which carries
    `::`, not a directory — whose exit status is captured into a variable and
    compared to exactly 1, in a conjunct — not one beginning with `!`, which
    inverts the property rather than establishing it — that reaches the
    deselecting run through `&&` alone, so that the node being red is what
    lets that run happen. The guard invocation is recognised only in the
    exact literal shape `pytest <node> -q > /dev/null 2>&1; <var>=$?` — a
    different flag order, a different redirect, or none at all is unread,
    not proven absent. The binding is through the same variable name the
    invocation assigned, so a bare "-eq 1" anywhere in the command cannot
    pass as a guard for every node it contains. A site deselecting a node it
    does not guard, guarding a node it does not deselect, or naming a node
    set that disagrees with another site's, is a violation.

    "Structurally present" is decided by a quote-aware scan of the command
    text: a `;`, a `||`, a bare `&` (not `&&` and not a fd redirect like
    `2>&1`, `>&2`, or `&>`), a newline, or a `--deselect` flag counts only
    when it sits outside single quotes, double quotes, a `$(...)` command
    substitution, a backtick substitution, and a `#` comment (which runs
    forward from itself to its line's end) — the same constructs a real
    shell itself recognises as removing a character's ordinary meaning,
    though a comment removes forward to a line end rather than up to a
    paired delimiter. Inside any of them the character is ordinary text, not
    shell structure. When the scan cannot tell — an unterminated quote or
    substitution — it never guesses either a clean verdict or a violation for
    that fragment specifically; the enclosing site is failed instead, on the
    same "never a silent skip" terms as an unrecognised `--deselect`
    spelling.

  * SEMANTIC — a high-recall prefilter, never a judge. It selects sentences
    carrying exception-set vocabulary together with EITHER a cardinality word
    OR a bare definite singular reference to a member of the set ('the
    excluded node', 'the known-red test', 'that node') — which asserts the
    same thing without a numeral and is invisible to a cardinality filter
    alone. It only REPORTS, printed untruncated with field and line, every
    count DERIVED from the enumeration and never written into a format
    string, and it always exits 0 regardless of what it finds — a failure
    inside the sweep itself degrades to a "sweep unavailable" line rather
    than a traceback, so the reading cannot change the verdict on any path
    and not merely on every reading. A machine judging whether a sentence
    ASSERTS a count (the defect) rather than merely counting beside a full
    list (permitted) would be a regex adjudicating meaning, the anti-pattern
    this repository refuses. Its value is bounding what a reader must read,
    not deciding it for them.

Accepts a single plan path, on the same CLI shape as check-order-coverage.py,
whose structural half this script mirrors.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from agentctl.plan import PlanError, load_plan  # noqa: E402

# --- structural half -----------------------------------------------------

_DESELECT_RE = re.compile(r"--deselect(?:\s+|=)(?P<node>\S+?)(?=\s|;|\}|$)")
_GUARD_INVOCATION_RE = re.compile(
    r"pytest\s+(?P<node>\S+::\S+?)\s+-q\s*>\s*/dev/null\s+2>&1;\s*(?P<var>\w+)=\$\?"
)
_GUARD_COMPARISON_RE = re.compile(r"test\s+\$(?P<var>\w+)\s+-eq\s+(?P<val>\d+)")
# A fd redirect spelled with `&` (`2>&1`, `>&2`, `&>`, `&>>`) must be consumed
# by this scan so its `&` is never mistaken for `&&` or a bare `&` separator
# — the first alternative matches the numbered-fd forms, the second the
# `&>`/`&>>` forms — but neither is itself a separator; only the alternatives
# named in `_SEPARATORS` are.
_TOP_LEVEL_SCAN_RE = re.compile(r"[0-9]*[<>]&[0-9-]*|&>>?|&&|\|\||&|;|\n")
_SEPARATORS = frozenset({"&&", "||", "&", ";", "\n"})
_LABEL_ORDER_RE = re.compile(r"^(?P<kind>stage|final_check) (?P<index>\d+)")


class _UnscannableFragment(ValueError):
    """A shell fragment `_mask_quoted` cannot fully account for — an
    unterminated quote or substitution. Never resolved by guessing: the
    caller turns this into a violation naming the site."""


def _mask_quoted(command: str) -> str:
    """`command` with every character inside a single-quoted string, a
    double-quoted string, a `$(...)` command substitution, a backtick
    substitution, or a top-level `#` comment replaced by a space — same
    length, so a match position or span computed against the result still
    indexes `command` itself. A `\\` masks itself and the character it
    escapes together, everywhere but inside a single-quoted string, where
    backslash is ordinary text. A `#` starts a comment only when it sits at
    the start of `command` or immediately after whitespace, and the comment
    then masks everything up to (not including) the next newline. Raises
    `_UnscannableFragment` on an unterminated quote or substitution rather
    than guessing where it would have closed.

    What this scanner gets wrong in ways that do NOT raise: a `$(...)` frame
    is closed by the FIRST unmatched `)`, so a genuinely nested one — a
    `case … )` arm, or `$( (cd x; ls) ; b )` — closes the substitution early
    and reads everything after it at top level, with the frame stack back at
    `["TOP"]` by the time the scan ends, so nothing is raised; this is the
    one construct here that can misread silently rather than announce
    itself as unscannable. `$'...'` and `$"..."` (ANSI-C and locale-
    translated quoting) are scanned as ordinary `'...'`/`"..."` strings, so a
    `\\'` inside `$'...'` — an escape bash itself honours there — closes the
    quote early and mis-frames what follows. Arithmetic `$((...))` is
    mis-read the same way as the nested-`)` case, but harmlessly, since a
    bare `)` is neither a separator nor `--deselect`.

    What this scanner does NOT decide at all: parameter expansion
    (`${...}`), here-documents, and process substitution (`<(...)`/`>(...)`)
    are not tracked as their own contexts — a `;` or a quote inside one of
    those is read at face value, structurally present or not, exactly as if
    the construct were not there."""
    out = list(command)
    stack = ["TOP"]
    i, n = 0, len(command)
    while i < n:
        frame = stack[-1]
        c = command[i]
        if frame == "SQ":
            if c == "'":
                stack.pop()
            out[i] = " "
            i += 1
            continue
        if frame == "TOP" and c == "#" and (i == 0 or command[i - 1] in " \t\n"):
            end = command.find("\n", i)
            if end == -1:
                end = n
            for k in range(i, end):
                out[k] = " "
            i = end
            continue
        if c == "\\" and i + 1 < n:
            out[i] = out[i + 1] = " "
            i += 2
            continue
        if c == "`":
            if frame == "BT":
                stack.pop()
            else:
                stack.append("BT")
            out[i] = " "
            i += 1
            continue
        if c == '"':
            if frame == "DQ":
                stack.pop()
            else:
                stack.append("DQ")
            out[i] = " "
            i += 1
            continue
        if c == "'" and frame != "DQ":
            stack.append("SQ")
            out[i] = " "
            i += 1
            continue
        if command[i : i + 2] == "$(":
            stack.append("DS")
            out[i] = out[i + 1] = " "
            i += 2
            continue
        if c == ")" and frame == "DS":
            stack.pop()
            out[i] = " "
            i += 1
            continue
        if frame != "TOP":
            out[i] = " "
        i += 1
    if stack != ["TOP"]:
        raise _UnscannableFragment(f"unterminated {stack[-1]} in {command!r}")
    return "".join(out)


def _label_sort_key(label: str):
    """Numeric order within a kind, so `stage 10` sorts after `stage 2` — a
    plain string sort would put it before, since '1' < '2' character-wise."""
    m = _LABEL_ORDER_RE.match(label)
    return (m.group("kind"), int(m.group("index"))) if m else (label, -1)


def _is_deselect_site(cmd: str) -> bool:
    """Whether `cmd` contains a structurally-present `--deselect` — one that
    survives `_mask_quoted`, so a comment or a quoted string merely mentioning
    the flag does not make a command a site. A command the scanner cannot
    read as shell structure is still a site: `structural_violations` reports
    it as a violation rather than silently excluding it."""
    if "--deselect" not in cmd:
        return False
    try:
        return "--deselect" in _mask_quoted(cmd)
    except _UnscannableFragment:
        return True


def _sites(doc) -> list[tuple[str, str]]:
    """Every (label, command) with a structurally-present `--deselect`,
    labelled on the same grammar check-order-coverage.py's resolve_control
    accepts, in numeric label order."""
    out = []
    for stage in doc.stages:
        cmd = stage.criterion.verify_command
        if cmd and _is_deselect_site(cmd):
            out.append((f"stage {stage.index} verify_command", cmd))
    for i, fc in enumerate(doc.meta.final_check, start=1):
        if fc.command and _is_deselect_site(fc.command):
            out.append((f"final_check {i}", fc.command))
    return sorted(out, key=lambda pair: _label_sort_key(pair[0]))


def _deselected_nodes(masked_command: str) -> list[str]:
    return [m.group("node") for m in _DESELECT_RE.finditer(masked_command)]


def _top_level_separators(masked_command: str):
    """Every top-level statement/control separator in `masked_command`, in
    scan order. A fd redirect spelled with `&` (`2>&1`, `>&2`, `&>`, `&>>`)
    is consumed by `_TOP_LEVEL_SCAN_RE` so it cannot be mistaken for a
    separator starting mid-redirect, but it is never itself yielded — it
    changes where a command's output goes, not whether one command's exit
    status can gate the next. The single mechanism behind both
    `_conjunct_start` and `_reaches_deselect_conjunctively`, so they cannot
    drift apart on what counts as a separator."""
    for m in _TOP_LEVEL_SCAN_RE.finditer(masked_command):
        if m.group() in _SEPARATORS:
            yield m


def _conjunct_start(masked_command: str, pos: int) -> int:
    """The offset where the conjunct ending at `pos` begins: just past the
    nearest top-level operator before `pos`, or 0 if there is none."""
    start = 0
    for m in _top_level_separators(masked_command[:pos]):
        start = m.end()
    return start


def _negated_comparison(masked_command: str, comparison_start: int) -> bool:
    """Whether the conjunct containing the guard comparison beginning at
    `comparison_start` opens with `!` — a negated comparison licenses the
    deselecting run when the node comes back GREEN, the opposite of a guard."""
    conjunct_start = _conjunct_start(masked_command, comparison_start)
    return masked_command[conjunct_start:comparison_start].lstrip().startswith("!")


def _reaches_deselect_conjunctively(masked_command: str, start: int, first_deselect: int) -> bool:
    """Whether every top-level operator between a guard comparison ending at
    `start` and `first_deselect` is `&&`. A `;` that discards the comparison's
    result, a `||` that diverts it, a bare `&`, or a newline in between proves
    nothing about the node — textual precedence is not the property."""
    between = masked_command[start:first_deselect]
    if not between.lstrip().startswith("&&"):
        return False
    return all(m.group() == "&&" for m in _top_level_separators(between))


def _guarded_nodes(masked_command: str) -> set[str]:
    """Node ids whose exit status is captured into a variable and compared to
    exactly 1, in a non-negated conjunct reaching the first structurally-
    present `--deselect` in `masked_command` through `&&` alone. Callers only
    ever pass a `masked_command` that contains `--deselect` at least once."""
    first_deselect = masked_command.find("--deselect")
    prefix = masked_command[:first_deselect]
    captured = {
        m.group("var"): m.group("node") for m in _GUARD_INVOCATION_RE.finditer(prefix)
    }
    guarded = set()
    for m in _GUARD_COMPARISON_RE.finditer(prefix):
        if m.group("val") != "1":
            continue
        node = captured.get(m.group("var"))
        if node is None:
            continue
        if _negated_comparison(masked_command, m.start()):
            continue
        if not _reaches_deselect_conjunctively(masked_command, m.end(), first_deselect):
            continue
        guarded.add(node)
    return guarded


def structural_violations(doc) -> list[str]:
    """Every way the plan's `--deselect` sites fail the structural property.
    [] == clean, including a plan declaring no such sites at all."""
    sites = _sites(doc)
    if not sites:
        return []

    out: list[str] = []
    per_site: dict[str, tuple[frozenset[str], set[str]]] = {}
    for label, cmd in sites:
        try:
            masked = _mask_quoted(cmd)
        except _UnscannableFragment as exc:
            out.append(
                f"{label}: cannot be read as shell structure ({exc}) — a "
                f"fragment this resolver cannot decide is a violation naming "
                f"the site, never a silent skip and never a silent guard"
            )
            continue
        occurrences = masked.count("--deselect")
        nodes = _deselected_nodes(masked)
        named = len(nodes)
        if named != occurrences:
            out.append(
                f"{label}: {occurrences} `--deselect` occurrence(s) but only "
                f"{named} name a node this resolver can read — an unrecognised "
                f"spelling is a failure, never a silent skip"
            )
        per_site[label] = (frozenset(nodes), _guarded_nodes(masked))

    if not per_site:
        return out

    union: frozenset[str] = frozenset().union(*(nodes for nodes, _ in per_site.values()))
    for label, (nodes, _guarded) in sorted(per_site.items(), key=lambda kv: _label_sort_key(kv[0])):
        if nodes != union:
            out.append(
                f"{label}: deselects {sorted(nodes)}, which disagrees with the "
                f"union of every --deselect site's node set {sorted(union)}"
            )
    for label, (nodes, guarded) in sorted(per_site.items(), key=lambda kv: _label_sort_key(kv[0])):
        for node in sorted(nodes - guarded):
            out.append(
                f"{label}: deselects {node!r} without a guard proving it exits "
                f"1 in a conjunct reaching the deselecting run through `&&` — "
                f"either no such guard exists, or it exists but is written in "
                f"a spelling this resolver does not recognise (the required "
                f"literal shape is in this script's module docstring)"
            )
        for node in sorted(guarded - nodes):
            out.append(f"{label}: guards {node!r} (proven exit 1) but never deselects it")
    return out


# --- semantic half ---------------------------------------------------------

_VOCAB_RE = re.compile(
    r"\b(exception set|toleran\w*|deselect\w*|exclud\w*|known-red|inherited-red|"
    r"trunk-red|still-red)\b",
    re.IGNORECASE,
)
_CARDINALITY_RE = re.compile(
    r"\b(\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b",
    re.IGNORECASE,
)
_DEFINITE_SINGULAR_RE = re.compile(
    r"\b(the|that|this)\s+(?:[\w-]+\s+){0,3}(node|test)\b", re.IGNORECASE
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"‘“])")

# One physical line per field is this plan's own convention (no `"""` string
# anywhere in it) — verified before writing this scanner rather than assumed.
_FIELD_LINE_RE = re.compile(r'^(?P<key>[A-Za-z_][\w.]*)\s*=\s*"(?P<value>.*)"\s*$')
_TABLE_HEADER_RE = re.compile(r"^\[(?P<array>\[)?(?P<name>[\w.]+)\]\]?\s*$")
_INDEX_LINE_RE = re.compile(r"^index\s*=\s*(?P<n>\d+)\s*$")


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", " ")


def _field_sentences(plan_path: Path) -> list[tuple[str, int, str]]:
    """Every (field label, line number, sentence) across every string field
    declared directly in the plan's source text — scanned from the raw lines
    rather than the parsed doc, since only the raw text carries line numbers.

    A stage's own subtables (`[stage.principle]`, `[stage.landed]`) and every
    `[[final_check]]` block keep the ordinal that names WHICH stage or WHICH
    final_check they belong to — dropping it would still report an accurate
    line number, but a reader could no longer tell the field apart from its
    same-named sibling on a different stage/final_check without cross-checking
    that line by hand, defeating "bounding what a reader must read"."""
    table = "meta"
    stage_index: int | None = None
    final_check_index = 0
    out: list[tuple[str, int, str]] = []
    for lineno, line in enumerate(plan_path.read_text(encoding="utf-8").splitlines(), 1):
        header = _TABLE_HEADER_RE.match(line)
        if header:
            table = header.group("name")
            if header.group("array"):
                if table == "stage":
                    stage_index = None
                elif table == "final_check":
                    final_check_index += 1
            continue
        idx = _INDEX_LINE_RE.match(line)
        if idx and table == "stage":
            stage_index = int(idx.group("n"))
            continue
        field = _FIELD_LINE_RE.match(line)
        if not field:
            continue
        if table == "stage" or table.startswith("stage."):
            suffix = table[len("stage"):]
            label = f"stage {stage_index}{suffix}.{field.group('key')}"
        elif table == "final_check" or table.startswith("final_check."):
            suffix = table[len("final_check"):]
            label = f"final_check {final_check_index}{suffix}.{field.group('key')}"
        else:
            label = f"{table}.{field.group('key')}"
        text = _unescape(field.group("value"))
        for sentence in _SENTENCE_SPLIT_RE.split(text):
            sentence = sentence.strip()
            if sentence:
                out.append((label, lineno, sentence))
    return out


def semantic_report(plan_path: Path) -> list[tuple[str, int, str]]:
    """Every (field, line, sentence) carrying exception-set vocabulary together
    with a cardinality word or a bare definite singular reference. Report-only
    — the caller must never let this list affect an exit code."""
    hits = []
    for label, lineno, sentence in _field_sentences(plan_path):
        if not _VOCAB_RE.search(sentence):
            continue
        if _CARDINALITY_RE.search(sentence) or _DEFINITE_SINGULAR_RE.search(sentence):
            hits.append((label, lineno, sentence))
    return hits


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <plan.toml>", file=sys.stderr)
        return 2
    plan_path = argv[1]
    try:
        doc = load_plan(plan_path)
    except (OSError, PlanError) as exc:
        print(f"cannot load {plan_path!r}: {exc}", file=sys.stderr)
        return 1

    violations = structural_violations(doc)

    # Blanket except: the sweep re-reads the plan's raw text independently of
    # load_plan, and "the reading can never change the verdict" has to hold on
    # every path — a decode error or an I/O race inside a report-only half must
    # not become this process's exit code, nor suppress the FAIL block below.
    try:
        hits = semantic_report(Path(plan_path))
    except Exception as exc:
        print(
            f"SEMANTIC SWEEP — unavailable for {plan_path} "
            f"({exc.__class__.__name__}: {exc}); report-only, verdict unaffected — "
            f"0 candidate sentence(s) enumerated (not an empty worklist)"
        )
    else:
        print(f"SEMANTIC SWEEP — {len(hits)} candidate sentence(s) to read in {plan_path}:")
        for label, lineno, sentence in hits:
            print(f"  {label} (line {lineno}): {sentence}")

    if violations:
        print(
            f"FAIL — {len(violations)} exception-set problem(s) in {plan_path}:",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    sites = _sites(doc)
    # `_mask_quoted(c)` is unguarded here because it cannot raise: every site
    # already passed it once inside `structural_violations` above, and a
    # raise there appends to `violations`, which would have returned 1 before
    # reaching this line.
    all_nodes = (
        sorted(frozenset().union(*(_deselected_nodes(_mask_quoted(c)) for _, c in sites)))
        if sites
        else []
    )
    print(
        f"OK — {plan_path}: {len(sites)} --deselect site(s), "
        f"{len(all_nodes)} node(s), all identical and individually guarded"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
