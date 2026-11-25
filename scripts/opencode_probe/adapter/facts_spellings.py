"""The runs of A6 that observe three further behaviours of OpenCode's own tools, with no gate loaded, which the
gate adapter's path compare and edit mapping must not get wrong:

* ``undefined`` / ``null`` as a search path: a ``glob`` / ``grep`` ``path`` that is the text ``undefined`` or
  ``null`` (or a JSON null) searches the session directory, or a directory of that name;
* alternate spellings: a ``read`` of a path that does not exist as written, whose file exists under another
  spelling of the same name (NFC / NFD, a no-break or narrow no-break space for a space, a curly for a straight
  quote), is answered with that file, or is not;
* fuzzy ``edit``: an ``oldString`` that differs from the file's text by the quote spelling, the indentation
  or trailing space is replaced anyway, or the edit fails.

The session runs in ``work/sub`` of the isolated root. Every seed holds a mark that names the file it came from,
so the text a call returns, or the file an edit changed, says which spelling OpenCode ended on."""
from __future__ import annotations

import json
import unicodedata

from opencode_probe.adapter import facts_a6

SUBDIR = facts_a6.SUBDIR
NBSP, NARROW, CURLY = " ", " ", "’"


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def nfd(text: str) -> str:
    return unicodedata.normalize("NFD", text)


SEARCH_SEEDS = {
    "work/sub/undefined/sp-seed.txt": "SEARCH-SEED-UNDEFINED-DIR",
    "work/sub/null/sp-seed.txt": "SEARCH-SEED-NULL-DIR",
    "work/sub/sp-seed.txt": "SEARCH-SEED-SESSION-DIR",
}
SEARCH_FORMS = [("undefined", "undefined"), ("null", "null"), ("json-null", None), ("absent", "<absent>")]

# (label, name of the seeded file as written to disk, name the read asks for)
SPELLINGS = [
    ("nfc-on-disk-asks-nfd", nfc("café-sp.txt"), nfd("café-sp.txt")),
    ("nfd-on-disk-asks-nfc", nfd("résumé-sp.txt"), nfc("résumé-sp.txt")),
    ("space-on-disk-asks-nbsp", "a b-sp.txt", f"a{NBSP}b-sp.txt"),
    ("nbsp-on-disk-asks-space", f"c{NBSP}d-sp.txt", "c d-sp.txt"),
    ("narrow-nbsp-on-disk-asks-space", f"e{NARROW}f-sp.txt", "e f-sp.txt"),
    ("space-on-disk-asks-narrow-nbsp", "g h-sp.txt", f"g{NARROW}h-sp.txt"),
    ("straight-quote-on-disk-asks-curly", "it's-sp.txt", f"it{CURLY}s-sp.txt"),
    ("curly-quote-on-disk-asks-straight", f"it{CURLY}s2-sp.txt", "it's2-sp.txt"),
    ("exact-control", "exact-sp.txt", "exact-sp.txt"),
]
DIRECTORY_SPELLING = ("nfc-dir-on-disk-asks-nfd-dir", nfc("dé-sp") + "/inner-sp.txt",
                      nfd("dé-sp") + "/inner-sp.txt")

EDIT_TEXT = 'He said "hello" to the world\n    indented line  \nend\n'
EDITS = [
    ("exact-control", 'He said "hello" to the world'),
    ("curly-quotes", "He said “hello” to the world"),
    ("indentation-differs", "  indented line  "),
    ("trailing-space-missing", "    indented line"),
    ("collapsed-whitespace", 'He  said "hello"  to the world'),
    ("absent-control", "this text is not in the file"),
]


def seeds() -> dict[str, str]:
    out = dict(SEARCH_SEEDS)
    for label, on_disk, _ in [*SPELLINGS, DIRECTORY_SPELLING]:
        out[f"work/sub/{on_disk}"] = f"MARK:{label}"
    for label, _ in EDITS:
        out[f"work/sub/sp-edit-{label}.txt"] = EDIT_TEXT
    return out


def _calls() -> list[tuple[str, str, dict]]:
    calls = []
    for label, form in SEARCH_FORMS:
        args = {} if form == "<absent>" else {"path": form}
        calls.append((f"grep:{label}", "grep", {"pattern": "SEARCH-SEED", **args}))
        calls.append((f"glob:{label}", "glob", {"pattern": "sp-seed.txt", **args}))
    for label, _, asked in [*SPELLINGS, DIRECTORY_SPELLING]:
        calls.append((f"read:{label}", "read", {"path": asked}))
    for label, old in EDITS:
        calls.append((f"edit:{label}", "edit", {"path": f"sp-edit-{label}.txt", "oldString": old,
                                                "newString": "EDIT-DONE"}))
    return calls


def _entries(run) -> list[dict]:
    out = []
    for line in (run.read("spell.log") or "").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def spelling_facts(h) -> dict:
    """One session with no gate that makes each call of ``_calls`` in turn. Per call the record carries the
    argument the before-hook was handed and the head of the answer (None: it produced none); per edit the
    file's text afterwards (whether the replacement landed)."""
    calls = _calls()
    scenario = {"models": {"m1": [*({"tool_calls": [{"name": name, "arguments": args}]} for _, name, args in calls),
                                  {"text": "done"}]}}
    files = {**seeds(), **facts_a6.plugin_files("sdir", "forms_probe.mjs")}
    run = h.run(scenario, label="spellings", config={"plugins": ["file://{root}/sdir"]}, files=files,
                env={"FORMSPROBE_LOG": "{root}/spell.log"}, cwd=SUBDIR, timeout=300)
    entries = _entries(run)
    before = [e for e in entries if e.get("at") == "before"]
    after = {e.get("id"): e for e in entries if e.get("at") == "after"}
    per_call = {}
    for (label, _, _), entry in zip(calls, before):
        seen = after.get(entry.get("id"))
        per_call[label] = {"before_args": entry.get("args"),
                           "after_text_head": None if seen is None else seen.get("text_head")}
    edited = {label: (run.read(f"work/sub/sp-edit-{label}.txt") or "") for label, _ in EDITS}
    return {"exit_code": run.exit_code, "session_directory": SUBDIR, "calls": per_call,
            "edit_results": {label: {"replacement_landed": "EDIT-DONE" in text, "text": text}
                             for label, text in edited.items()},
            "before_count": len(before), "call_count": len(calls)}
