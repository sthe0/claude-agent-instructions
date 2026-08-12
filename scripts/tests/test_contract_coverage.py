"""Stage 10 of smd-act-defects-8: the planner-contract doc coverage walk, and a
mutation-derived enumerator over the four functions that decide whether a stage-field
edit is seen by any part of the replan/carry-forward/question-invalidation machinery.

Part A ties the planner's authoring contract (SKILL.md + policy.md) to submission.py's
own three field tables — the same guard test_rejected_shapes.py gives the SUBMISSION
seam's refusal behavior, aimed instead at the DOCUMENTATION an author reads before ever
reaching that seam. Part A′ then pins the naming predicate Part A is built on, over
SYNTHETIC text: the two parts control different objects, and neither substitutes for the
other (argued where Part A′ begins).

Part B answers a different question than test_renormalization.py's
`test_the_stage_residual_exhausts_the_stage_s_field_set` — that one asks whether the
RENORMALIZATION residual (gates._renorm_stage_residual, the re-sequencing gate at
`replan`) accounts for every Stage leaf. This asks whether the four ordinary
CHANGE-DECISION functions do: `plan.stage_carry_key` (PASSED carry-forward),
`cli._apply_refined_stage_fields` (what a refinement replan copies onto the live
stage), `plan.diff_plans` (refinement-vs-substantive classification), and
`plan.stage_question_key` (premise invalidation). The four do NOT cover the same set
by design — several asymmetries below are load-bearing (e.g. `stage_carry_key` omits
`principle`/`supplies.element` because carry-forward never needed them, per its own
docstring) — so this is a CONTRACT test pinning each function's ACTUAL, current
behavior per leaf, not an aspirational "everything must be seen everywhere" assertion.
Where the pinned behavior looks like a gap rather than a deliberate asymmetry, that is
recorded in the leaf's comment below and in this stage's own report, not silently
fixed here — this file's job is to make the current shape visible and guarded, not to
re-decide it.
"""
from __future__ import annotations

import copy
import inspect
import re
from pathlib import Path

import pytest

from agentctl.cli import _apply_refined_stage_fields
from agentctl.plan import (
    PlanDoc, PlanMeta, diff_plans, stage_carry_key, stage_question_key,
)
from agentctl.state import (
    Actor, Criterion, LandedSpec, Means, Outcome, Principle, Stage, Subject, Supply,
)
from agentctl.submission import (
    _ORDER_PARTS, _SUBSTANTIVE_META_FIELDS, _SUBSTANTIVE_SUBMISSION_FIELDS,
)

from dataclass_domain import leaf_paths

ROOT = Path(__file__).resolve().parents[2]

# --- Part A: the planner contract documents every submission-seam field -----


def _contract_text() -> str:
    skill = (ROOT / "skills" / "specializations" / "planner" / "SKILL.md").read_text()
    policy = (ROOT / "skills" / "specializations" / "planner" / "policy.md").read_text()
    return skill + "\n" + policy


#: A markdown list-item line — top-level `- `/`N. ` or an indented sub-bullet. Every
#: genuine field explanation in SKILL.md/policy.md takes this shape (a glossary
#: bullet or a numbered Plan-format item); a label mentioned only in running prose
#: that is not itself a list item is exactly the coincidental case the review flagged
#: (`method`/`goal`/`coverage`/... as ordinary English, or an unrelated identifier).
_LIST_ITEM_RE = re.compile(r"^\s*(?:-\s+|\d+\.\s+)")

#: An em dash separates a NAMING position from the rationale that follows it, and one
#: list item may hold SEVERAL — `SKILL.md`'s "Knowledge & preconditions" bullet names
#: `knowledge`, then `preconditions`, then `material_refs` / `knowledge_refs`, each in
#: front of its own dash. Splitting at the FIRST dash only (the prior shape) credited
#: the first and gave the rest zero, so eight assertions here were green solely because
#: `policy.md` happens to document those same fields one-per-bullet: consolidating that
#: glossary into one legitimate multi-field bullet would have turned them red with the
#: documentation fully present, which is a test making an author contort prose.
_EM_DASH = "—"

#: The naming position in front of a dash is its TRAILING run of code spans — the
#: `` `material_refs` / `knowledge_refs` `` of "… for the same reason. `material_refs` /
#: `knowledge_refs` — the structural projection …". Spans in a run may be joined only by
#: whitespace, markdown emphasis and a list separator; anything else ends the run. What
#: that buys is the run's WIDTH — where it starts, and so how many spans it holds — and
#: nothing else: in "… with `[[stage.supplies]] element = \"knowledge\"`). `preconditions`
#: —" the `). ` ends the run at `preconditions`, so the supplies span is not credited, and
#: in "… `functional_place` (the place this plan's product fills … is inadequate —" the
#: run is empty because the segment does not END in a span at all.
_TRAILING_SPANS_RE = re.compile(r"(?:`[^`]+`[\s*/,]*)+$")

#: Width is not position. A run may be perfectly bounded and still sit mid-clause, so a
#: naming run must additionally OPEN A STATEMENT: begin its segment (modulo the list
#: marker and emphasis characters), follow a sentence-terminating boundary, or follow a
#: colon that CLOSES AN EMPHASISED SUBJECT (`…:** `, the bullet-subject shape). A BARE
#: mid-sentence colon does not — see the last census row.
#:
#: The census below is the whole domain, enumerated over both files rather than recalled
#: from the labels this file happens to track. The two contract files hold 88 pre-dash
#: segments; `_TRAILING_SPANS_RE` refuses 67 of them for WIDTH — they do not end in a
#: code-span run at all — before position is ever consulted. The other 21 split by the
#: shape of the text in front of the run:
#:   13  `- ` and `- **`                             opens the list item     → credit
#:    1  `- **Knowledge & preconditions:** `         `:` closing a subject   → credit
#:    2  `… = "knowledge"`). ` and `… same reason. `  ends a sentence        → credit
#:    4  `` … `venue` on a ``, `…"). For `, `…; for ` mid-clause             → NO credit
#:    1  `… in the provenance ledger: `              bare mid-sentence `:`   → NO credit
#:
#: The two refusing rows are why the rule exists, and they are separate cases.
#:
#: MID-CLAUSE. `SKILL.md`'s dash-bracketed aside "… **declare** the venue — `verify_venue`
#: on a stage, `venue` on a `[[final_check]]` — instead of hardcoding an absolute `cd` …"
#: is about declaring a check's venue; the ` on a ` in front of the last span narrows the
#: run to that span alone, and `final_check` was credited from it. So the assertion for
#: that label held with `policy.md`'s glossary bullet — the one place in the contract that
#: tells an author the field is submission-required — deleted. That aside was the target;
#: the other three mid-clause refusals (`SKILL.md`'s "… opens **directly** with the
#: approval `AskUserQuestion`", and the two `measurable` / `acceptance-review` segments of
#: the Expected-result-image item) are correct refusals in their own right, not collateral.
#:
#: BARE COLON. `policy.md`'s ledger bullet ends "… Record each as a claim in the provenance
#: ledger: `agentctl ledger-add --status axiom|derivation|assumption ...` — …": a colon
#: introducing a COMMAND EXAMPLE, not naming a field. Crediting it handed the bullet's flag
#: vocabulary to the label domain, so `_label_documented("derivation", …)` was True off a
#: CLI flag list — with nothing in the contract documenting `Principle.derivation` at all.
#: The day that field enters a tracked field table, its assertion would go green off that
#: flag list: the same vacuity class the two rules above removed, one field-table edit away.
#:
#: `)` is deliberately NOT a terminator. The one census row that ends on a paren ends
#: `").`, credited by the `.` that follows it, so admitting a bare `)` could only ever ADD
#: the mid-clause aside `prose (an aside) `alpha` —`. Dropping it changes nothing on
#: either contract file (14 credited list items before and after, no label lost); the
#: close-paren case below pins the refusal so re-adding it turns red.
#:
#: `;`, `?`, and `!`, by contrast, ARE terminators, and the distinction from `)` is real:
#: a paren closes an aside INSIDE a clause, whereas a semicolon ends a clause that is
#: itself a statement, and `?`/`!` end a sentence outright. All three were already live
#: in this string, and none of them was exercised: the one census row that looks like it
#: turns on `;` (`"…; for "`) is refused because its subject ends on the word "for", not
#: because of the `;` — so no row in either contract file ends a naming position on `;`,
#: `?`, or `!`. Measured directly, before the three cases below existed and when the suite
#: stood at 78: dropping `;` alone (leaving `.?!`) changed nothing — the suite still
#: passed, and both contract files still credited exactly the same 14 list items and 19
#: distinct tokens, line for line. An element live in the rule and dead in every
#: measurement is an accident waiting to be "cleaned up" by the next reader, unlike `)`,
#: which is over-broad and belongs out; the three cases below pin each terminator on
#: synthetic text so none of them can go unnoticed again.
_STATEMENT_END = ".;?!"

#: The emphasised-subject colon of the census's second row, spelled as BALANCED emphasis
#: whose opener BELONGS TO THE SUBJECT: an opening run that starts the head or follows
#: whitespace, the subject text, the colon, then the SAME closer. The first shape asked
#: only whether the text ended in emphasis characters at all, which is equally true when
#: the emphasis decorates the RUN rather than the subject — so `…ledger: *`alpha`*`,
#: `…ledger:_ `alpha``, and `…ledger:*`alpha`*` all credited off the bare mid-sentence
#: colon of the LAST census row, the one shape this rule exists to refuse. Balance is what
#: separates them, and it is why `head.endswith(":**") or head.endswith(":*")` is not the
#: fix: that still credits the no-space `…ledger:*`alpha`*` and additionally drops the
#: legitimate single-underscore subject `_Subject:_`.
#:
#: Balance alone is not enough either, because this is SEARCHED and a match may start
#: anywhere in `head`: an earlier bold word donates the opener while the trailing `*` of
#: an emphasised run donates the closer, and the refused shape returns one word away from
#: the case that pins it — `- **Note** on the provenance ledger:*`alpha`*` differs from
#: that case by its leading `**Note**` alone. The `(?:^|(?<=\s))` prefix is what ties the
#: opener to the subject rather than to whatever emphasis the line happens to carry.
#:
#: The subject is `(?:(?!\1).)+` and not `[^*_]+` because the latter forbids `*` and `_`
#: INSIDE the subject, refusing the legitimate ``**A `knowledge_refs` note:**`` and
#: `**The *real* subject:**` — a test making an author contort prose, the failure this
#: file condemns above. Anchoring alone (`(?:^|[^*_])(\*\*|\*|_+)[^*_]+:\1$`) closes the
#: witness while keeping both of those refusals: it reddens the two admit-direction cases
#: below.
#:
#: The closing `$` carries the other half of "the opener belongs to the subject": the
#: closed subject must END the head, so a run following ordinary prose LATER in the head
#: (`**Subject:** intro prose `alpha``) stays mid-clause. Round 7's sweep found nothing
#: red when `$` was dropped — an element live in the rule and dead in every measurement,
#: the same accident the terminator paragraph above records — so the case below pins it.
#:
#: Nine parts of this regex move independently, at the granularity where each element can
#: be mutated on its own:
#:
#:   1. the opener anchor `(?:^|(?<=\s))` — present or absent — pinned by
#:      `an earlier bold word must not supply the opener (round 7)`
#:   2. the lookbehind's strength, `\s` vs `[^*_]` — pinned by
#:      `the lookbehind admits an opener only after whitespace (round 8)`
#:   3. the alternation's MEMBERSHIP — dropping `_+` reddens the round-6 case, dropping
#:      `\*\*` reddens four
#:   4. the alternation's ORDER — an EQUIVALENT MUTANT: `(\*|\*\*|_+)` behaves
#:      identically, because the engine backtracks through the alternatives, so
#:      `**Subject:**` fails on `\*` (the tempered token immediately meets the second `*`)
#:      and then succeeds on `\*\*`. It is unpinnable by construction, and saying so is the
#:      point: an unkillable mutant is a fact about the rule, not a gap in the cases
#:   5. the `_+` quantifier — pinned by
#:      `doubled-underscore emphasis closes a subject too (round 9)`
#:   6. the tempered token `(?:(?!\1).)` vs `[^*_]` — pinned by the two round-7
#:      admit-direction cases
#:   7. the tempered token's quantifier, `+` vs `*` — pinned by
#:      `an emphasised subject must not be empty (round 9)`
#:   8. the closing backreference `\1` — pinned by
#:      `balance: the closer must be the opener, not merely emphasis (round 8)`
#:   9. the trailing `$` — pinned by
#:      `the subject's colon must END the head, not sit back in it (round 7)`
#:
#: Round 8's sweep found nothing red when `(?<=\s)` widened to `(?<=[^*_])`, yet the two
#: are not equivalent: bold `**Note**` is refused because its second `*` is immediately
#: followed by `*`, which kills the tempered token, but italic `*Note*` has no second `*`
#: — so for it the lookbehind alone stands between `- *Note* on the ledger:*`alpha`*` and
#: a credit, the round-7 witness in italic dress. Freeing the closer — trailing `\1` to
#: `(?:\*\*|\*|_+)` — was equally silent: balance is what refuses an unclosed italic
#: opener answering for a bold closer. Both were live in the rule and dead in every
#: measurement — the same accident the terminator paragraph above records — until round 8
#: pinned #2 and #8.
#:
#: Round 8 counted five parts and stopped there, missing #5 and #7: narrowing `_+` to `_`
#: and relaxing the tempered token's `+` to `*` both reddened nothing at round 8's commit,
#: and both were dismissed as reachable only by malformed markdown. Neither is. `_+` admits
#: a run of underscores, so `__Subject:__` — standard markdown bold, no less legitimate
#: than `**Subject:**` — closes a subject the narrowed rule would refuse; and a subject
#: with no subject in it, `- **:** …`, is exactly the shape ordinary prose invites once the
#: tempered token's `+` is read as `*`. The same contrived-input misjudgement round 7 made
#: about `*Note*` and round 8 itself corrected recurred one round later, on a different
#: axis.
#:
#: After this commit eight of the nine are pinned by their own case and the ninth (#4) is
#: an equivalent mutant, so the sweep has no survivor left that is not provably
#: behaviour-preserving. That is a fact about the sweep AT THIS COMMIT — tie any number
#: cited here to the commit it was measured at, not a live count to be re-quoted unchecked.
_EMPHASISED_SUBJECT_COLON_RE = re.compile(r"(?:^|(?<=\s))(\*\*|\*|_+)(?:(?!\1).)+:\1$")

#: A code span is credited by its identifier TOKENS, never by substring containment:
#: `means.method` names both `means` and `method`, `[meta.order.coverage]` names
#: `coverage`, and — the point — `knowledge_refs` does NOT name `knowledge` and
#: `customer_id` does NOT name `customer`. Containment made each of those pairs one
#: label: deleting the `customer` bullet from policy.md left `[customer]` green,
#: satisfied by the neighbouring `customer_id` bullet, on exactly the pair the contract
#: says does two different jobs.
_TOKEN_SPLIT_RE = re.compile(r"[^A-Za-z0-9_]+")


def _documenting_bullets(text: str) -> list[str]:
    return [line for line in text.splitlines() if _LIST_ITEM_RE.match(line)]


def _opens_a_statement(before: str) -> bool:
    """True when a run trailing `before` inside its segment sits in a naming position —
    `before` is empty once the list marker and markdown emphasis are stripped, it ends at
    a sentence-terminating boundary, or it ends at a colon that closes an EMPHASISED
    subject (`**Knowledge & preconditions:** `). The emphasis is what distinguishes the
    two colon shapes, so it is read off `head` — before the emphasis characters are
    stripped — and it must be BALANCED and OPENED BY THE SUBJECT, or else the run's own
    emphasis (or an earlier bold word's) answers for the subject's (see
    `_EMPHASISED_SUBJECT_COLON_RE`)."""
    head = _LIST_ITEM_RE.sub("", before).rstrip()
    subject = head.rstrip(" \t*_")
    if subject == "":
        return True
    if subject[-1] in _STATEMENT_END:
        return True
    return _EMPHASISED_SUBJECT_COLON_RE.search(head) is not None


def _naming_terms(bullet: str) -> set[str]:
    """Every identifier token this list item NAMES: for each em dash, the tokens of the
    code spans trailing the text in front of it, when that run opens a statement.

    A list item with NO em dash therefore names nothing. That is deliberate and it is a
    narrowing — `SKILL.md`'s "Problem and done criteria" item mentions `[meta] goal` and
    `[meta] done_criterion` in passing prose and no longer earns credit for them (they
    are credited by policy.md § Meta submission fields, the glossary bullet that exists
    to name them). The dash is what marks the naming/rationale boundary; without one
    there is no distinguished position, and crediting the item's last code span instead
    would credit whatever a bullet happens to END on — a file path, a cross-reference —
    which is the vacuity this predicate exists to remove."""
    terms: set[str] = set()
    for segment in bullet.split(_EM_DASH)[:-1]:
        run = _TRAILING_SPANS_RE.search(segment)
        if run is None or not _opens_a_statement(segment[: run.start()]):
            continue
        for span in re.findall(r"`([^`]+)`", run.group(0)):
            terms.update(t for t in _TOKEN_SPLIT_RE.split(span) if t)
    return terms


def _label_documented(label: str, text: str) -> bool:
    """True when `label` is NAMED by some markdown list item of the contract — an
    identifier token of a code span in that item's naming position — not merely present
    somewhere across ~235 lines of prose. Two vacuities this replaces, both found by
    review: `label in _contract_text()`, which passed on any incidental appearance of an
    ordinary English word (`method`, `goal`, `knowledge`, `coverage`, `requirements`, …)
    or an unrelated identifier (`plan.goal` in premise-gate prose); and its first
    tightening, which reached the right lines but still compared by substring inside
    them, so `customer_id` went on satisfying `customer`."""
    return any(label in _naming_terms(b) for b in _documenting_bullets(text))


def _overlap_smell_documented(text: str) -> bool:
    """True when ONE list item of `text` states the material_refs/knowledge_refs overlap
    convention. Rationale for the shape — and for how its reach differs from
    `_label_documented`'s — in the assertion that consumes it below."""
    return any(
        "smell" in b and "material_refs" in b and "knowledge_refs" in b
        for b in _documenting_bullets(text)
    )


# --- Part A′: the naming predicates themselves, pinned over SYNTHETIC text --

# Four review rounds established this predicate's behaviour by hand-running mutations
# over the real contract files, and each round found a shape the previous one had not
# considered. Nothing pinned the predicate itself, so any edit to it re-opened every
# shape at once and the only detector was another review round. These cases close that:
# one per shape, each id naming what that shape is here to hold.
#
# They do NOT duplicate the contract-file assertions above, and are not a weaker copy of
# them: those pin THE DOCUMENTATION (does the contract still name `preconditions`?),
# these pin THE PREDICATE (does a bare mid-sentence colon still refuse?). Two different
# objects — the documentation can be rewritten without touching the predicate, and the
# predicate can be rewritten without touching the documentation, and only a control per
# object catches both.

#: (id, synthetic list item, the tokens `_naming_terms` must return). Each id names, in
#: one clause, the finding the case holds — a review round where the round established
#: it, the contract shape where the rule was there to read the shape correctly.
#:
#: Two of them are BASELINES rather than discriminators of one clause: the
#: opens-the-list-item and sentence-boundary positives survive an unconditional-True
#: `_opens_a_statement`, because a predicate that credits everything still credits them.
#: They are not decoration — both redden when `_TRAILING_SPANS_RE` loses its trailing
#: glue, which is what lets a run end on emphasis or whitespace, and each is among the
#: many positives that redden when its own clause is deleted (a removed `return True` can
#: only be caught by a positive; no negative can see it). What they cannot do is show that
#: position is consulted AT ALL — the negatives below them, not these positives, are what
#: prove that — so adding further positives of that shape would not raise the block's
#: power.
#:
#: The negative side has its own low-power entry, kept for a different reason:
#: `mid-clause (the final_check aside this rule was added for)` is uniquely reddened by no
#: mutation — re-admitting a bare `:` reddens the three bare-colon cases and round 7's
#: earlier-bold-word one, and re-adding `)` reddens the close-paren one. It earns its place
#: as the real contract shape the rule was added for, which the synthetic negatives around
#: it do not hold.
_NAMING_CASES = (
    ("opens-the-list-item (the glossary-bullet shape)",
     "- **`alpha`** — why it exists",
     {"alpha"}),
    ("emphasis-closed-subject-colon (round 4)",
     "- **Subject:** `alpha` — why it exists",
     {"alpha"}),
    ("single-underscore emphasis closes a subject too (round 6)",
     "- _Subject:_ `alpha` — why it exists",
     {"alpha"}),
    ("doubled-underscore emphasis closes a subject too (round 9)",
     "- __Subject:__ `alpha` — why it exists",
     {"alpha"}),
    ("sentence-boundary (the SKILL.md material_refs shape)",
     "- prose, and for the same reason. `alpha` — why it exists",
     {"alpha"}),
    ("close-paren-then-period (the SKILL.md preconditions shape)",
     '- prose (`[[stage.supplies]] element = "knowledge"`). `alpha` — why it exists',
     {"alpha"}),
    ("semicolon-ends-a-clause-statement (unpinned before this stage)",
     "- one clause; `alpha` — why it exists",
     {"alpha"}),
    ("question-mark-ends-a-sentence (unpinned before this stage)",
     "- a question? `alpha` — why it exists",
     {"alpha"}),
    ("exclamation-mark-ends-a-sentence (unpinned before this stage)",
     "- emphatic point! `alpha` — why it exists",
     {"alpha"}),
    ("mid-clause (the final_check aside this rule was added for)",
     "- prose declaring the venue on a `alpha` — why it exists",
     set()),
    ("bare-mid-sentence-colon (round 4)",
     "- prose recording each claim in the provenance ledger: `alpha` — why it exists",
     set()),
    ("bare colon before an emphasised RUN, spaced (round 5)",
     "- prose recording each claim in the provenance ledger: *`alpha`* — why it exists",
     set()),
    ("bare colon before an emphasised RUN, unspaced (round 5; refutes the obvious fix)",
     "- prose recording each claim in the provenance ledger:*`alpha`* — why it exists",
     set()),
    ("a close paren does not end a statement (round 6)",
     "- prose (a parenthetical aside) `alpha` — why it exists",
     set()),
    ("an earlier bold word must not supply the opener (round 7)",
     "- **Note** prose recording each claim in the provenance ledger:*`alpha`* — why it exists",
     set()),
    ("the lookbehind admits an opener only after whitespace (round 8)",
     "- *Note* on the provenance ledger:*`alpha`* — why it exists",
     set()),
    ("balance: the closer must be the opener, not merely emphasis (round 8)",
     "- *Subject:**`alpha`** — why it exists",
     set()),
    ("an emphasised subject must not be empty (round 9)",
     "- **:** `alpha` — why it exists",
     set()),
    ("an emphasised subject may hold an identifier (round 7)",
     "- **A `knowledge_refs` note:** `alpha` — why it exists",
     {"alpha"}),
    ("an emphasised subject may hold nested emphasis (round 7)",
     "- **The *real* subject:** `alpha` — why it exists",
     {"alpha"}),
    ("the subject's colon must END the head, not sit back in it (round 7)",
     "- **Subject:** intro prose `alpha` — why it exists",
     set()),
    ("multi-span run (round 2)",
     "- **`alpha`** / **`beta`** — why they exist",
     {"alpha", "beta"}),
    ("a later dash names too (round 2)",
     "- **Subject:** `alpha` — why it exists. And separately. `beta` — why it exists",
     {"alpha", "beta"}),
    ("the token split keeps `_` inside one identifier (round 1's splitter half)",
     "- **`alpha_id`** — why it exists",
     {"alpha_id"}),
    ("no em dash names nothing (deliberate strictness)",
     "- **`alpha`**",
     set()),
    ("the glue between spans does not reach across prose (round 6)",
     "- **`alpha`** and later `beta` — why they exist",
     set()),
    ("run must end the segment (the functional_place shape)",
     "- **`alpha`** (a parenthetical the run cannot cross) — why it exists",
     set()),
)


@pytest.mark.parametrize(
    "bullet,expected", [pytest.param(b, e, id=i) for i, b, e in _NAMING_CASES]
)
def test_the_naming_position_predicate_holds_shape_by_shape(bullet, expected):
    assert _naming_terms(bullet) == expected


def test_a_label_is_documented_only_from_a_list_item():
    """Round 1's original vacuity, in its purest form: the same naming position in
    running prose rather than in a list item credits nothing. `_documenting_bullets` is
    what carries this, and it is separately breakable from `_naming_terms`.

    The third assertion holds round 1's OTHER half at the level it actually lives on.
    `_naming_terms` returns a token SET, so every case above it is pinned through set
    membership and none of them can see how this function consults that set: relaxing
    `label in terms` to `any(label in t for t in terms)` restores `customer` credited off
    `customer_id` — the finding verbatim — with every other assertion in this file still
    green, because they all assert True and a superstring predicate is a superset."""
    assert _label_documented("alpha", "- **`alpha`** — why it exists")
    assert not _label_documented("alpha", "**`alpha`** — why it exists")
    assert not _label_documented("alpha", "- **`alpha_id`** — why it exists")


def test_the_overlap_smell_predicate_needs_all_three_in_ONE_item():
    """Round 3 tightened this predicate from whole-text containment to one-list-item
    co-occurrence, measured the difference against a decoy, and then discarded the decoy
    — so nothing in the repo held it. `split_across_items` below IS that decoy, kept:
    all three needles are present in the text, the whole-text predicate matched it, this
    one refuses it, and the assertion above it states the needles so a reader can see the
    decoy is genuine rather than take the test's word for it."""
    one_item = "- a symbol in both `material_refs` and `knowledge_refs` is a smell"
    assert _overlap_smell_documented(one_item)

    split_across_items = (
        "- a symbol in both `material_refs` and `knowledge_refs` must be justified\n"
        "- an unrelated bullet about a code smell\n"
    )
    assert all(
        needle in split_across_items
        for needle in ("smell", "material_refs", "knowledge_refs")
    )
    assert not _overlap_smell_documented(split_across_items)

    #: and, as for `_label_documented`, running prose is not a list item
    assert not _overlap_smell_documented(
        "a symbol in both `material_refs` and `knowledge_refs` is a smell"
    )


@pytest.mark.parametrize("label", tuple(l for _a, l, _s in _SUBSTANTIVE_SUBMISSION_FIELDS))
def test_every_stage_submission_field_is_named_in_the_planner_contract(label):
    assert _label_documented(label, _contract_text()), (
        f"{label!r} is a substantive-stage submission requirement "
        f"(submission._SUBSTANTIVE_SUBMISSION_FIELDS) with no mention in the planner's "
        f"own authoring contract — an author following SKILL.md/policy.md alone would "
        f"never learn the seam refuses a plan omitting it"
    )


@pytest.mark.parametrize("label", tuple(l for _a, l in _SUBSTANTIVE_META_FIELDS))
def test_every_meta_submission_field_is_named_in_the_planner_contract(label):
    assert _label_documented(label, _contract_text()), (
        f"{label!r} is a substantive-plan [meta] submission requirement "
        f"(submission._SUBSTANTIVE_META_FIELDS) with no mention in the planner contract"
    )


@pytest.mark.parametrize("name,_why", _ORDER_PARTS)
def test_every_order_part_is_named_in_the_planner_contract(name, _why):
    assert _label_documented(name, _contract_text()), (
        f"[meta.order].{name} is a required order part (submission._ORDER_PARTS) with "
        f"no mention in the planner contract"
    )


def test_the_coverage_map_is_named_in_the_planner_contract():
    assert _label_documented("coverage", _contract_text())


def test_the_material_refs_knowledge_refs_overlap_smell_is_documented():
    """`_label_documented` is deliberately NOT the predicate here, and the difference is
    not a loophole: what must be documented is a RELATION between two fields, where a
    naming position names ONE label. The contract states this rule in a bullet's own
    subject ("A symbol named in **both** `material_refs` and `knowledge_refs` is a
    smell"), not in front of a dash, so no naming run holds it and none should.

    What does carry over is the part of that predicate which generalizes past labels:
    the claim must hold WITHIN a single documenting list item. The prior shape, `"smell"
    in _contract_text()`, was the same whole-file substring the review condemned for the
    per-label assertions — satisfiable by the word appearing anywhere across ~235
    concatenated lines while the two field names matched from an unrelated paragraph.

    TWO DIFFERENCES FROM THE PER-LABEL ASSERTIONS, both deliberate and neither hidden:

    REACH. Two list items satisfy this — `policy.md`'s "A symbol named in both … is a
    smell" bullet and `SKILL.md`'s "Knowledge & preconditions" item — so deleting either
    ONE alone leaves this green. That is a weaker reach than a per-label assertion, whose
    label typically hangs on a single glossary bullet, and it is the right reach for the
    claim being made: what must hold is that THE CONTRACT documents the convention, not
    that one particular file does.

    SUBSTRING, NOT TOKENS. `"smell" in b` is a substring test sitting three lines from a
    comment condemning substring containment, and the difference is the domain: that
    comment governs IDENTIFIER matching, where `knowledge_refs` must not satisfy
    `knowledge` and `customer_id` must not satisfy `customer`. "smell" is an English word
    in running prose, not an identifier — there is no token boundary to respect and no
    superstring in either file for it to falsely match.

    That argument covers ONE of the three needles, and the other two — `material_refs`
    and `knowledge_refs` — are identifiers, i.e. squarely the domain the token comment
    governs. Two things make substring safe for them. DIRECTION: the failure that comment
    records is a needle satisfied by a LONGER identifier, and these two needles ARE the
    longer forms; matching them by substring can only be wrong if the files hold something
    longer still (`material_refs_extra`), and neither file holds any superstring of either
    (measured over both, not assumed). FIXITY: unlike `_label_documented`, whose label
    domain is generated from submission.py's field tables and so grows whenever a field is
    added, these two needles are literals of this predicate — a new field cannot silently
    change what they match, so the measurement above does not go stale behind a field-table
    edit the way a generated domain would."""
    assert _overlap_smell_documented(_contract_text()), (
        "the contract must say, in one list item, that a symbol in BOTH material_refs "
        "and knowledge_refs is a smell the stage's own prose must justify — this is a "
        "documented convention, not a submission refusal, so nothing in submission.py "
        "enforces it"
    )


# --- Part B: the change-decision function family, per Stage leaf ------------


def _stage(index=2, **over):
    fields = dict(
        index=index, title="the stage under test",
        subject=Subject(material="m", result="r", invariants="inv",
                         material_refs=["a/b.py"], knowledge_refs=["c/d.py"]),
        means=Means(means="bash", method="run", procedure="1. a. 2. b."),
        actor=Actor(executor="in_thread", capability_required="cap", cost_tier="medium"),
        criterion=Criterion(
            criterion_type="measurable", done_criterion="dc",
            verify_command="pytest -q", expected_exit=0, observation="",
            verify_venue="delivery", verify_kind="landed",
            landed=LandedSpec(target="main", delivered_stage=1, remote="origin"),
            verify_venue_at_final="repo_root",
        ),
        principle=Principle(statement="s", source="src", derivation="der",
                             confidence="high", refutation="r"),
        conditions="cond", preconditions="pre", knowledge="know",
        supplies=[Supply(on=1, element="result", artifact="x")],
        output_artifacts=["out/path.py"], outcome=Outcome(), control=None,
    )
    fields.update(over)
    return Stage(**fields)


def _doc(stage):
    meta = PlanMeta(task_id="t", goal="g", done_criterion="dc",
                     criterion_type="measurable", weight_class="substantive",
                     external_research="none applies", repo_root=None,
                     delivery_worktree=None, final_check=[], order=None)
    return PlanDoc(meta=meta, stages=[_stage(index=1), stage])


def _get(obj, dotted):
    """Walk a dotted `leaf_paths` path, transparently indexing into a `list[Supply]`
    at [0] — the shape `leaf_paths` flattens a list-of-dataclasses field to (one
    element's fields, unindexed), matching `dataclass_domain`'s own convention."""
    for part in dotted.split("."):
        if isinstance(obj, list):
            obj = obj[0]
        obj = getattr(obj, part)
    return obj


def _set(obj, dotted, value):
    parts = dotted.split(".")
    for part in parts[:-1]:
        if isinstance(obj, list):
            obj = obj[0]
        obj = getattr(obj, part)
    if isinstance(obj, list):
        obj = obj[0]
    setattr(obj, parts[-1], value)


def _mutate(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, str):
        return (value or "") + "-MUTATED"
    if isinstance(value, int):
        return value + 1
    if isinstance(value, list):
        return list(value) + ["MUTATED"]
    if value is None:
        return "MUTATED"
    raise AssertionError(f"no mutation strategy for {value!r}")


#: `index` is matched-on by every function in this family (which stage an edit
#: belongs to), not a field OF a stage's definition — excluded from the leaf domain
#: below the same way `renormalization_blockers`'s own residual excludes it.
_EXCLUDED_FROM_ENUMERATION = {"index"}

_CD = ("carry_key", "apply_refined", "diff_plans", "question_key")

#: Per Stage leaf, which of the four change-decision functions detect a change to it —
#: derived by mutation in the test below, not asserted from reading the code alone.
#: `diff_plans` is recorded as ONE column though it runs two internal comparisons
#: (`_structural_signature`, then the `_prose` closure) because `_prose` has no
#: standalone entry point to call in isolation; a leaf marked here detects via either.
#:
#: Four cells below are gaps rather than intentional asymmetries — cross-checked
#: against the source, not left to the mutation run's silence alone, and surfaced in
#: this stage's own report rather than fixed here (this stage's engine-code edit is
#: authorized for exactly one line, `verify_venue_at_final`'s copy, not these):
#:  - `actor.capability_required`: invisible to carry_key, apply_refined, AND
#:    diff_plans. A replan correcting ONLY the required capability diffs as
#:    'no_change' and the correction is silently dropped — the exact failure class
#:    this key family exists to close (diff_plans' own cost_tier/verify_venue
#:    precedent comments name this pattern explicitly).
#:  - `supplies.element` / `supplies.artifact`: invisible to carry_key BY DESIGN
#:    (matching stage_question_key's own docstring: "carry-forward never needed
#:    them"), but ALSO invisible to diff_plans, which is not a stated design —
#:    `_structural_signature` reads only `depends_on`, i.e. `.on`.
#:  - `output_artifacts`: invisible to ALL FOUR. Not copied by
#:    `_apply_refined_stage_fields`, so a refinement replan correcting a stage's
#:    declared output_artifacts leaves the LIVE stage stale; and undetected by
#:    diff_plans, so such an edit diffs as 'no_change' outright.
#:  - `principle.*`: not copied by `_apply_refined_stage_fields`. Within that
#:    function's own stated contract (COVER `stage_carry_key`, which does not read
#:    `principle` either — so not a violation of it), but it means a corrected
#:    principle never reaches the live stage on a refinement replan, only on a fresh
#:    submission.
_STAGE_LEAF_COVERAGE: dict[str, frozenset[str]] = {
    "title": frozenset(_CD),
    "subject.material": frozenset({"apply_refined", "question_key"}),
    "subject.result": frozenset(_CD),
    "subject.invariants": frozenset(_CD),
    "subject.material_refs": frozenset(_CD),
    "subject.knowledge_refs": frozenset(_CD),
    "means.means": frozenset(_CD),
    "means.method": frozenset(_CD),
    "means.procedure": frozenset(_CD),
    "actor.executor": frozenset(_CD),
    "actor.capability_required": frozenset({"question_key"}),
    "actor.cost_tier": frozenset({"apply_refined", "diff_plans"}),
    "criterion.criterion_type": frozenset(_CD),
    "criterion.done_criterion": frozenset(_CD),
    "criterion.verify_command": frozenset(_CD),
    "criterion.expected_exit": frozenset(_CD),
    "criterion.observation": frozenset(),
    "criterion.verify_venue": frozenset(_CD),
    "criterion.verify_kind": frozenset(_CD),
    "criterion.landed.target": frozenset(_CD),
    "criterion.landed.delivered_stage": frozenset(_CD),
    "criterion.landed.remote": frozenset(_CD),
    "criterion.verify_venue_at_final": frozenset(_CD),
    "principle.statement": frozenset({"question_key"}),
    "principle.source": frozenset({"question_key"}),
    "principle.derivation": frozenset({"question_key"}),
    "principle.confidence": frozenset({"question_key"}),
    "principle.refutation": frozenset({"question_key"}),
    "conditions": frozenset(_CD),
    "preconditions": frozenset(_CD),
    "knowledge": frozenset(_CD),
    "supplies.on": frozenset(_CD),
    "supplies.element": frozenset({"apply_refined", "question_key"}),
    "supplies.artifact": frozenset({"apply_refined", "question_key"}),
    "output_artifacts": frozenset(),
    "outcome.status": frozenset(),
    "outcome.actual": frozenset(),
    "outcome.fail_digests": frozenset(),
    "outcome.cost_usd": frozenset(),
    "outcome.duration_ms": frozenset(),
    "outcome.spawn_count": frozenset(),
    "outcome.delivered_head": frozenset(),
    "control": frozenset(),
}


def test_the_stage_leaf_coverage_map_is_exactly_the_stage_leaf_set():
    leaves = set(leaf_paths(Stage)) - _EXCLUDED_FROM_ENUMERATION
    assert leaves == set(_STAGE_LEAF_COVERAGE), (
        f"a Stage leaf is in neither this map nor its exclusion set: "
        f"{sorted(leaves - set(_STAGE_LEAF_COVERAGE))} unaccounted, "
        f"{sorted(set(_STAGE_LEAF_COVERAGE) - leaves)} listed but gone"
    )


@pytest.mark.parametrize("leaf,expected", sorted(_STAGE_LEAF_COVERAGE.items()))
def test_change_decision_coverage_is_pinned_by_mutation(leaf, expected):
    old_stage = _stage()
    new_stage = _stage()
    _set(new_stage, leaf, _mutate(_get(old_stage, leaf)))
    assert _get(old_stage, leaf) != _get(new_stage, leaf), "mutation was a no-op"

    detected = set()
    if stage_carry_key(old_stage) != stage_carry_key(new_stage):
        detected.add("carry_key")
    cur = copy.deepcopy(old_stage)
    _apply_refined_stage_fields(cur, new_stage)
    if _get(cur, leaf) != _get(old_stage, leaf):
        detected.add("apply_refined")
    if diff_plans(_doc(old_stage), _doc(new_stage)) != "no_change":
        detected.add("diff_plans")
    if stage_question_key(old_stage) != stage_question_key(new_stage):
        detected.add("question_key")

    assert detected == expected, (
        f"{leaf}: mutating it is detected by {sorted(detected)}, but "
        f"_STAGE_LEAF_COVERAGE says {sorted(expected)} — either the coverage map is "
        f"stale or a change-decision function's behavior moved"
    )


def test_cd_names_exactly_the_four_labels_the_mutation_harness_detects():
    """`_CD` is hand-written, unlike `_STAGE_LEAF_COVERAGE`'s per-leaf entries (derived
    by the mutation run above). What this binds it to is exactly one thing: the literal
    `detected.add("...")` calls inside `test_change_decision_coverage_is_pinned_by_
    mutation`'s own source — this harness's own probe vocabulary. So renaming a column
    in one place and not the other turns red, and a `_CD` entry no probe ever adds
    turns red.

    RESIDUAL, stated because the binding is easy to over-read: this proves `_CD` matches
    the PROBES, not that the probes match the engine. The family of four is
    hand-identified — there is no enumerator deriving "the functions that decide whether
    a stage-field edit is seen" from `agentctl` — so a FIFTH such function added to the
    engine with no probe added here leaves `_CD` and the probes perfectly in sync and
    both blind to it. Nothing in this file goes red for that; only a reader adding the
    probe closes it."""
    source = inspect.getsource(test_change_decision_coverage_is_pinned_by_mutation)
    labels = frozenset(re.findall(r'detected\.add\("([^"]+)"\)', source))
    assert labels == frozenset(_CD), (
        f"detected.add(...) calls name {sorted(labels)}, but _CD is {sorted(_CD)} — "
        f"one was edited without the other"
    )
