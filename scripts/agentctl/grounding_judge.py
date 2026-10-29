"""Grounding judge: a fail-open model judge of each link between a prescription and the knowledge it rests on.

A prescription (a principle) names its grounds explicitly. Whether a named
ground is *relevant*, *sufficient*, in *scope* and consistent with the prescription is meaning, so a
model decides it; whether a ref resolves and whether a given regularity is current is observable, so a
rule decides it before the model is asked (`given_currency`).

One model question carries two counterfactual tests -- forward (would refuting each listed ground put
the prescription in question) and backward (which candidate, if refuted, would, and is it listed) -- and
returns defects of two kinds that stay apart: `LINK_DEFECTS` (the link is wrong) and `GROUND_DEFECTS` (a
ground is wrong). A ground-only entry (a regularity -- given, induced or rule-derived -- or an ontology,
all knowledge and no prescription) gets no link defects.

The judge fails open: an outage yields an `unavailable` outcome with a warning, never a pass and never a
stamp. The digest of a link is defined once -- `link_inputs` over the whitelisted fields of the
prescription and of its grounds -- and every side (table, leaf, judge) hashes that text; the fields
include the entry's identity and the question asked of it, so two entries that share their grounds never
share a digest. A verdict is cached at `<cache_dir()>/<digest>.json` together with a hash of the text the
model was shown, and a record is served only to a call that shows the model the same text. Callers reach every seam as `grounding_judge.<name>` so a control
can replace one at its module attribute.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import tomllib
from datetime import date, timedelta
from pathlib import Path

from lib import host_llm, judge_ledger, leaf_forms
from lib import principle_provenance as pp
from lib.config_root import agentctl_dir
from lib.runtime_models import HOST_CLAUDE, model_for

from . import grounding
from .config import Thresholds

JUDGE_NAME = "grounding_link"
QUOTE_JUDGE_NAME = "order_quote"
LINK_DEFECTS = ("irrelevant", "insufficient", "scope_mismatch", "contradicting")
GROUND_DEFECTS = ("defective_ground", "unverified_given", "inadequate_distinction", "prescriptive_knowledge")
RULE_DEFECTS = ("unverified_given",)
UNFAITHFUL_QUOTE = "unfaithful_quote"

DIGEST_ONLY_FIELDS = ("id", "question")
LINK_FIELDS = ("principle", "grounded_in", "statement", "rule_derived", "distinction", "adequacy",
               "given", "given_source") + DIGEST_ONLY_FIELDS
PROMPT_FIELDS = tuple(f for f in LINK_FIELDS if f not in DIGEST_ONLY_FIELDS)
GROUND_FIELDS = ("ref", "statement", "distinction", "adequacy", "given", "given_source")

_JUDGE_COMPLEXITY = "medium"
_JUDGE_MODEL = model_for(HOST_CLAUDE, _JUDGE_COMPLEXITY)
_JUDGE_TIMEOUT_S = 185

_PROMPT_HEAD = (
    "You judge one link between a prescription and the knowledge it rests on. Answer with one JSON "
    'object and nothing else: {{"defects": [<names>], "reason": "<one sentence>"}}. "defects" is [] '
    "for a sound link.\n\n"
)
_LINK_TESTS = (
    "Forward test: for each listed ground, suppose it were refuted. Would that put the prescription in "
    "question and force a review of it? It need not force a change: a prescription may rest on several "
    "grounds, and a ground the prescription is overdetermined by is legitimate. Name `irrelevant` for a "
    "listed ground whose refutation would leave the prescription untouched, `insufficient` when the "
    "listed grounds together do not support it, `scope_mismatch` when a ground supports it only in a "
    "narrower or different scope than the prescription claims, `contradicting` when a ground says the "
    "opposite.\n"
    "Backward test: which candidate, if refuted, would force a review of the prescription? If it is "
    "not among the listed grounds, the prescription depends on knowledge it does not list: name "
    "`insufficient`.\n"
    "Ground defects: `defective_ground` when a listed ground is itself false or unsound; "
    "`prescriptive_knowledge` when a listed ground is a norm and no knowledge.\n"
)
_GROUND_TESTS = (
    "This entry is a ground and no prescription: judge only whether it is sound. Ground defects: "
    "`defective_ground` when its statement is false or unsound, `prescriptive_knowledge` when it is a "
    "norm and no knowledge. Name no other defect.\n"
)
_ONTOLOGY_QUESTION = (
    "Ontology adequacy: for each ontology ground, is its distinction adequate to what it distinguishes "
    "-- does it cut the cases the prescription needs apart, and no others? If not, name "
    "`inadequate_distinction`.\n"
)
_QUOTE_PROMPT = (
    "A statement is derived from a quotation of the user's order. Answer with one JSON object and "
    'nothing else: {{"faithful": true|false, "reason": "<one sentence>"}}. "faithful" is true when the '
    "quotation says what the statement attributes to it.\n\nQUOTATION:\n{quote}\n\nSTATEMENT:\n{statement}\n"
)


class JudgeUnavailable(Exception):
    """The model could not be consulted or its answer could not be read."""


def max_age_days() -> int:
    return Thresholds().given_regularity_max_age_days()


def cache_dir() -> Path:
    return agentctl_dir() / "grounding-judge-cache"


def _body(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 3)
        return text[end + 5:] if end >= 0 else text
    return text


def _section(text: str, heading: str) -> str:
    return (leaf_forms._section_text(_body(text), heading) or "").strip()


def _norm_refs(items) -> list[str]:
    out = set()
    for item in items or ():
        refs, bad = grounding.parse_ground_refs(str(item))
        out.update(ref.text() for ref in refs)
        out.update(bad)
    return sorted(out)


def _pick(source, keys) -> dict:
    picked = {}
    for key in keys:
        value = source.get(key)
        if key == "grounded_in":
            value = _norm_refs(value)
        elif isinstance(value, str):
            value = value.strip()
        if value:
            picked[key] = value
    return picked


def _canonical(fields: dict) -> str:
    return json.dumps(fields, sort_keys=True, ensure_ascii=False)


def link_inputs(prescription, grounds) -> str:
    """The canonical text a link's digest covers: the whitelisted fields of the prescription, then of each
    ground ordered by ref, one JSON line each."""
    picked = sorted((_pick(g, GROUND_FIELDS) for g in grounds), key=lambda g: g.get("ref", ""))
    return "\n".join([_canonical(_pick(prescription, LINK_FIELDS))] + [_canonical(g) for g in picked])


def link_digest(prescription, grounds) -> str:
    return hashlib.sha256(link_inputs(prescription, grounds).encode("utf-8")).hexdigest()


# --- the table side ---------------------------------------------------------------------------------


def table_entries(data) -> list[tuple[str, dict]]:
    """Every entry the verdicts file covers: each [[leaf]] of any class, each [[stamp]] and [[rebase]]."""
    out = []
    for kind in ("leaf", "stamp", "rebase"):
        for entry in data.get(kind, []):
            out.append((f"{entry.get('tier')}:{entry.get('path')}", entry))
    return out


def entry_question(tag, entry) -> str:
    """`ground` for an entry that is knowledge and no prescription (a regularity of any origin, an
    ontology); `link` for one that rests on other knowledge."""
    return "ground" if entry.get("class") in ("G", "O", "K") else "link"


def _entry_id(tier, path) -> str:
    return f"{tier}:{_stem(path)}"


def table_override(entry) -> str | None:
    return str(entry.get("override") or "").strip() or None


def _stem(path) -> str:
    return Path(str(path)).stem


def _table_ground(ref_text: str, data) -> dict:
    refs, _bad = grounding.parse_ground_refs(ref_text)
    if not refs:
        return {"ref": ref_text}
    ref = refs[0]
    for tier in grounding.fallback_tiers(ref.tier):
        for reg in data.get("regularity", []):
            if ref.kind == "regularity" and reg.get("tier") == tier and reg.get("slug") == ref.slug:
                return {"ref": ref.text(), "statement": reg.get("statement", "")}
        for leaf in data.get("leaf", []):
            if leaf.get("tier") != tier or _stem(leaf.get("path")) != ref.slug:
                continue
            if ref.kind == "regularity" and leaf.get("class") in ("K", "G"):
                return {"ref": ref.text(), **_pick(leaf, ("statement", "given", "given_source"))}
            if ref.kind == "ontology" and leaf.get("class") == "O":
                return {"ref": ref.text(), **_pick(leaf, ("distinction", "adequacy"))}
    return {"ref": ref.text()}


def table_grounds(entry, data) -> list[dict]:
    """The grounds the table names for `entry`, read from the table -- an in-place regularity as the
    table will write it, not as the corpus still holds it."""
    return [_table_ground(ref, data) for ref in _norm_refs(_entry_refs(entry))]


def _entry_refs(entry) -> list[str]:
    """The refs an entry is grounded in; a class M entry also rests on the regularity it names."""
    refs = list(entry.get("grounded_in") or [])
    if entry.get("class") == "M" and entry.get("regularity"):
        refs.append(f"regularity:{entry['regularity']}")
    return refs


def table_prescription(entry) -> dict:
    return {**entry, "grounded_in": _entry_refs(entry), "id": _entry_id(entry.get("tier"), entry.get("path")),
            "question": entry_question(None, entry)}


def table_link_inputs(entry, data) -> str:
    return link_inputs(table_prescription(entry), table_grounds(entry, data))


def table_link_digest(entry, data) -> str:
    return hashlib.sha256(table_link_inputs(entry, data).encode("utf-8")).hexdigest()


def table_candidates(data) -> list[dict]:
    out = []
    for reg in data.get("regularity", []):
        ref = grounding.GroundRef(reg.get("tier", "global"), "regularity", reg.get("slug", "")).text()
        out.append({"ref": ref, "statement": reg.get("statement", "")})
    for leaf in data.get("leaf", []):
        kind = {"K": "regularity", "G": "regularity", "O": "ontology"}.get(leaf.get("class"))
        if kind:
            ref = grounding.GroundRef(leaf.get("tier", "global"), kind, _stem(leaf.get("path"))).text()
            out.append({"ref": ref, **_pick(leaf, ("statement", "distinction", "adequacy"))})
    return out


# --- the leaf side ----------------------------------------------------------------------------------


def _ground_of(leaf) -> dict:
    text = leaf.text
    front = leaf_forms.frontmatter_of(text)
    schema = leaf_forms.field(front, "schema")
    out = {"last_verified": leaf_forms.field(front, "last_verified") or ""}
    if schema == "regularity/v1":
        out["statement"] = _section(text, "## Regularity")
        kind = leaf_forms.given_provenance(text)
        if kind:
            out["given"] = kind
            out["given_source"] = leaf_forms.field(front, "given_source") or ""
    elif schema == leaf_forms.ONTOLOGY_SCHEMA:
        out["distinction"] = _section(text, "## Distinction")
        out["adequacy"] = _section(text, "## Adequacy")
    return out


def leaf_question(schema) -> str:
    return "link" if schema == "principle/v1" else "ground"


def leaf_link_fields(leaf, corpus) -> tuple[dict, list[dict]]:
    """The prescription and ground fields a converted leaf carries, shaped like the table's. A principle
    leaf that was converted from `leaf/v1` carries its principle text; one that was not (a stamped
    principle, which the table holds by its grounded_in alone) does not."""
    text = leaf.text
    front = leaf_forms.frontmatter_of(text)
    schema = leaf_forms.field(front, "schema")
    prescription: dict = {"id": f"{leaf.tier}:{leaf.slug}", "question": leaf_question(schema)}
    refs: list[str] = []
    if schema == "principle/v1":
        raw = leaf_forms.field(front, "grounded_in") or ""
        refs = pp.parse_list(raw) if raw else []
        prescription["grounded_in"] = refs
        if leaf_forms.field(front, "converted_from"):
            prescription["principle"] = _section(text, "## Principle")
    elif schema == "regularity/v1":
        prescription.update(_ground_of(leaf))
        prescription["rule_derived"] = leaf_forms.field(front, "rule_derived") or ""
    elif schema == leaf_forms.ONTOLOGY_SCHEMA:
        prescription.update(_ground_of(leaf))
    grounds = []
    for text_ref in _norm_refs(refs):
        parsed, _bad = grounding.parse_ground_refs(text_ref)
        found = grounding.leaf_for(parsed[0], corpus) if parsed else None
        ground = _ground_of(found) if found is not None else {}
        grounds.append({"ref": text_ref, **ground})
    return prescription, grounds


def leaf_link_inputs(leaf, corpus) -> str:
    return link_inputs(*leaf_link_fields(leaf, corpus))


# --- the rule part ----------------------------------------------------------------------------------


def given_currency(ground, *, checked_paths=(), today=None) -> bool:
    """Whether a ground can be relied on as of `today`: a ground that is not given always is; a given one
    is current when its `last_verified` is within `max_age_days()` or its `given_source` is among the paths
    the citing change checked. Decided by rule, never by the model."""
    if not ground.get("given"):
        return True
    if ground.get("given_source") in set(checked_paths):
        return True
    try:
        verified = date.fromisoformat(str(ground.get("last_verified") or "")[:10])
    except ValueError:
        return False
    return (today or date.today()) - verified <= timedelta(days=max_age_days())


def candidate_grounds(corpus) -> list[dict]:
    """Every regularity and ontology statement in reach, uncapped."""
    out = []
    for area, kind in (("regularity", "regularity"), ("ontology", "ontology")):
        for leaf in corpus.of(area):
            ground = _ground_of(leaf)
            statement = ground.get("statement") or ground.get("distinction") or ""
            out.append({"ref": grounding.GroundRef(leaf.tier, kind, leaf.slug).text(), "statement": statement})
    return out


def ontology_question(grounds) -> str:
    return _ONTOLOGY_QUESTION if any(g.get("distinction") for g in grounds) else ""


# --- the model part ---------------------------------------------------------------------------------


def split_defects(names) -> tuple[list[str], list[str]]:
    """The defect names a model returned, apart: (link defects, ground defects), each in vocabulary order."""
    unknown = [n for n in names if n not in LINK_DEFECTS + GROUND_DEFECTS]
    if unknown:
        raise ValueError(f"the model returned unknown defect names: {unknown}")
    return ([n for n in LINK_DEFECTS if n in names], [n for n in GROUND_DEFECTS if n in names])


def _reply_object(text: str) -> dict:
    start = (text or "").find("{")
    if start < 0:
        raise ValueError("no JSON object in the reply")
    obj, _end = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise ValueError("the reply is not a JSON object")
    return obj


def parse_verdict(text: str) -> dict:
    obj = _reply_object(text)
    names = obj.get("defects", [])
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError("defects is not a list of names")
    link, ground = split_defects(names)
    return {"link_defects": link, "ground_defects": ground, "reason": str(obj.get("reason", ""))}


def _record_call(name, *, verdict, reason, timed_out=False, malformed=False, duration=None, prompt_chars=None):
    judge_ledger.decided(
        name, stage="call", verdict=verdict, reason=reason, timed_out=timed_out, malformed=malformed,
        threshold=_JUDGE_TIMEOUT_S, duration=duration, prompt_chars=prompt_chars,
    )


def _ask(name, prompt, runner, parse):
    """One model call, parsed with `parse`; JudgeUnavailable on every path that yields no readable answer."""
    if runner is None:
        judge_ledger.decided(name, stage="no_runner", verdict=False, reason="judge given no runner (fail-open)",
                             threshold=_JUDGE_TIMEOUT_S)
        raise JudgeUnavailable("no runner")
    judge_ledger.set_current_judge(name)
    start = time.monotonic()
    try:
        argv = host_llm.build_launch_argv(HOST_CLAUDE, _JUDGE_MODEL, lean=False)
        result = runner(argv, timeout=_JUDGE_TIMEOUT_S, stdin=prompt)
    except Exception as exc:
        _record_call(name, verdict=False, reason="judge raised (fail-open)",
                     duration=time.monotonic() - start, prompt_chars=len(prompt))
        raise JudgeUnavailable(f"the judge raised {type(exc).__name__}") from exc
    finally:
        judge_ledger.set_current_judge(None)
    duration = time.monotonic() - start
    if getattr(result, "timed_out", False):
        _record_call(name, verdict=False, reason="judge timed out (fail-open)", timed_out=True,
                     duration=duration, prompt_chars=len(prompt))
        raise JudgeUnavailable("the judge timed out")
    if result.returncode != 0:
        _record_call(name, verdict=False, reason="judge exited non-zero (fail-open)",
                     duration=duration, prompt_chars=len(prompt))
        raise JudgeUnavailable("the judge exited non-zero")
    try:
        parsed = parse(result.stdout or "")
    except ValueError as exc:
        _record_call(name, verdict=False, reason=f"judge answer unparseable: {exc} (fail-open)",
                     malformed=True, duration=duration, prompt_chars=len(prompt))
        raise JudgeUnavailable(f"the judge's answer was unreadable: {exc}") from exc
    _record_call(name, verdict=True, reason="", duration=duration, prompt_chars=len(prompt))
    return parsed


def _render(items, with_ref=True) -> str:
    lines = []
    for item in items:
        body = "; ".join(f"{k}: {item[k]}" for k in ("statement", "distinction", "adequacy", "given", "given_source")
                         if item.get(k))
        lines.append(f"- [{item.get('ref', '')}] {body}" if with_ref else f"- {body}")
    return "\n".join(lines) or "(none)"


def _shown(prescription, context) -> str:
    """The text of the prescription the model is shown: its whitelisted fields, then the caller's context."""
    shown = "\n".join(f"{k}: {v}" for k, v in _pick(prescription, PROMPT_FIELDS).items())
    return f"{shown}\n{context}" if context else shown


def _inputs_hash(question, prescription, context) -> str:
    return hashlib.sha256(f"{question}\n{_shown(prescription, context)}".encode("utf-8")).hexdigest()


def _prompt(question, prescription, grounds, candidates, context) -> str:
    shown = _shown(prescription, context)
    if question == "ground":
        return (_PROMPT_HEAD + f"ENTRY:\n{shown}\n\n" + _GROUND_TESTS + ontology_question([prescription]))
    return (_PROMPT_HEAD + f"PRESCRIPTION:\n{shown}\n\nLISTED GROUNDS:\n{_render(grounds)}\n\n"
            f"CANDIDATES (every regularity and ontology statement in reach):\n{_render(candidates)}\n\n"
            + _LINK_TESTS + ontology_question(grounds))


def unavailable_outcome(reason, digest=None) -> dict:
    """Fail open: a warning and no verdict -- never a pass, never a stamp."""
    return {"status": "unavailable", "verdict": None, "link_defects": [], "ground_defects": [],
            "reason": "", "digest": digest, "blocking": False,
            "warning": f"grounding judge unavailable: {reason}", "override": None, "overridden": False}


def apply_override(verdict, reason) -> dict:
    """Keep every defect of `verdict` and mark it overridden with `reason`."""
    if not reason:
        return verdict
    defects = list(verdict.get("link_defects") or []) + list(verdict.get("ground_defects") or [])
    return {**verdict, "override": reason, "overridden": bool(defects)}


def _cache_read(cache, digest, inputs):
    """The record cached for `digest`, when it is a judged one of this very link shown the same text."""
    if cache is None:
        return None
    try:
        record = json.loads((Path(cache) / f"{digest}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    ok = record.get("status") == "ok" and record.get("digest") == digest and record.get("inputs") == inputs
    return record if ok else None


def _cache_write(cache, digest, record) -> None:
    if cache is None:
        return
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=cache, prefix=f".{digest}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True))
        os.replace(tmp, cache / f"{digest}.json")
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def judge_grounding_link(prescription, grounds, candidates, *, runner, cache=None, digest=None, today=None,
                         checked_paths=(), context="", question="link", override=None) -> dict:
    """Judge one link. The model's part is cached by the link's digest; the given-currency rule is decided
    on every call, before and apart from the model."""
    digest = digest or link_digest({**prescription, "question": question}, grounds)
    inputs = _inputs_hash(question, prescription, context)
    subjects = list(grounds) + ([prescription] if question == "ground" else [])
    stale = [d for d in RULE_DEFECTS
             if any(not given_currency(g, checked_paths=checked_paths, today=today) for g in subjects)]
    cached = _cache_read(cache, digest, inputs)
    if cached is not None:
        link, ground, reason = cached.get("link_defects", []), cached.get("ground_defects", []), cached.get("reason", "")
    else:
        try:
            reply = _ask(JUDGE_NAME, _prompt(question, prescription, grounds, candidates, context),
                         runner, parse_verdict)
        except JudgeUnavailable as exc:
            return unavailable_outcome(str(exc), digest)
        link, ground, reason = reply.get("link_defects", []), reply.get("ground_defects", []), reply.get("reason", "")
    if question == "ground":
        link = []
    ground = [d for d in GROUND_DEFECTS if (d in ground and d not in RULE_DEFECTS) or d in stale]
    outcome = {"status": "ok", "verdict": "defective" if link or ground else "sound", "link_defects": link,
               "ground_defects": ground, "reason": reason, "digest": digest, "blocking": False,
               "warning": None, "override": None, "overridden": False}
    _cache_write(cache, digest, {**{k: outcome[k] for k in ("status", "verdict", "link_defects", "ground_defects",
                                                              "reason", "digest")}, "inputs": inputs})
    return apply_override(outcome, override)


def table_record(outcome) -> dict:
    return {"verdict": outcome.get("verdict"), "status": outcome.get("status"),
            "link_defects": outcome.get("link_defects", []), "ground_defects": outcome.get("ground_defects", []),
            "digest": outcome.get("digest"), "warning": outcome.get("warning"), "reason": outcome.get("reason"),
            "override": outcome.get("override"), "overridden": bool(outcome.get("overridden"))}


def stamp_lines(record) -> list[str]:
    """The frontmatter lines a judged link earns: its digest when it is sound or its defects are
    overridden, the override reason beside it; nothing for an unjudged link or a defect left standing."""
    if record.get("status") != "ok":
        return []
    defective = bool(record.get("link_defects") or record.get("ground_defects"))
    reason = record.get("override")
    if defective and not reason:
        return []
    lines = [f"grounding_judged: {record.get('digest')}"]
    if reason:
        lines.append(f"grounding_override: {reason}")
    return lines


def _context_of(entry, corpus) -> str:
    if corpus is None or entry.get("class") is not None:
        return ""
    base = corpus.leaves_dir if entry.get("tier") == "global" else corpus.project_root
    text = corpus.read(Path(base) / entry["path"]) if base is not None else None
    return f"principle: {_section(text, '## Principle')}" if text else ""


def _last_verified(entry, corpus) -> str:
    if corpus is None or entry.get("class") not in ("G", "K"):
        return ""
    base = corpus.leaves_dir if entry.get("tier") == "global" else corpus.project_root
    text = corpus.read(Path(base) / entry["path"]) if base is not None else None
    return (leaf_forms.field(leaf_forms.frontmatter_of(text), "last_verified") or "") if text else ""


def judge_table(table_path, *, runner, cache=None, corpus=None, today=None, checked_paths=()) -> dict:
    """The verdicts of one bulk-conversion table, in the format `bulk.py` reads."""
    raw = Path(table_path).read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    cache = Path(cache) if cache is not None else cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    candidates = table_candidates(data)
    if corpus is not None:
        listed = {c["ref"] for c in candidates}
        candidates += [c for c in candidate_grounds(corpus) if c["ref"] not in listed]
    links = {}
    for tag, entry in table_entries(data):
        prescription = dict(table_prescription(entry), last_verified=_last_verified(entry, corpus))
        outcome = judge_grounding_link(
            prescription, table_grounds(entry, data), candidates, runner=runner, cache=cache,
            digest=table_link_digest(entry, data), today=today, checked_paths=checked_paths,
            context=_context_of(entry, corpus), question=entry_question(tag, entry),
            override=table_override(entry))
        links[tag] = table_record(outcome)
    return {"table_sha256": hashlib.sha256(raw).hexdigest(), "cache": str(cache), "links": links}


# --- a live corpus ----------------------------------------------------------------------------------


def judge_leaf(leaf, corpus, *, runner, cache=None, today=None, checked_paths=()) -> dict:
    prescription, grounds = leaf_link_fields(leaf, corpus)
    override = leaf_forms.field(leaf_forms.frontmatter_of(leaf.text), "grounding_override")
    shown_in_fields = "principle" in prescription
    context = "" if shown_in_fields or not prescription.get("grounded_in") else (
        f"principle: {_section(leaf.text, '## Principle')}")
    outcome = judge_grounding_link(prescription, grounds, candidate_grounds(corpus), runner=runner, cache=cache,
                                   today=today, checked_paths=checked_paths, context=context,
                                   question=prescription["question"], override=override)
    return table_record(outcome)


def judge_corpus(corpus, *, runner, cache=None, today=None, checked_paths=()) -> dict:
    """The record of every principle of the corpus that names grounds, by `<tier>:<slug>`."""
    return {f"{leaf.tier}:{leaf.slug}": judge_leaf(leaf, corpus, runner=runner, cache=cache, today=today,
                                                   checked_paths=checked_paths)
            for leaf in corpus.of("principle") if leaf_forms.field(leaf_forms.frontmatter_of(leaf.text), "grounded_in")}


# --- an order quote ---------------------------------------------------------------------------------


def _parse_quote_reply(text: str) -> dict:
    obj = _reply_object(text)
    if not isinstance(obj.get("faithful"), bool):
        raise ValueError("faithful is not a boolean")
    return obj


def order_quote_defects(quote, statement, *, runner) -> list[str]:
    """[`unfaithful_quote`] when the quotation does not say what the statement attributes to it, else []."""
    reply = _ask(QUOTE_JUDGE_NAME, _QUOTE_PROMPT.format(quote=quote, statement=statement), runner,
                 _parse_quote_reply)
    return [] if reply["faithful"] else [UNFAITHFUL_QUOTE]


def judge_order_quote(quote, statement, *, runner) -> dict:
    try:
        defects = order_quote_defects(quote, statement, runner=runner)
    except JudgeUnavailable as exc:
        return {**unavailable_outcome(str(exc)), "defects": []}
    return {"status": "ok", "verdict": "defective" if defects else "sound", "defects": defects,
            "blocking": False, "warning": None}
