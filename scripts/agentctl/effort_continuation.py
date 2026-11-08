"""Where a live stage's identity and arming go when a replan leaves it unmatched (R3).

Pure functions over records; the engine builds the records and applies the result. An old record
is {index, uid, title, settled, outputs, record, check}, a new one {index, title, outputs, record}
(`record` is `effort_segments.match_identities`' input, `outputs` the normalized
`output_artifacts`). Nothing here reads the filesystem."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import effort_segments


def _split(text: str) -> tuple[bool, list[str]]:
    absolute = text.startswith("/")
    parts: list[str] = []
    for piece in text.split("/"):
        if piece in ("", "."):
            continue
        if piece == ".." and parts and parts[-1] != "..":
            parts.pop()
        elif piece == ".." and absolute:
            continue
        else:
            parts.append(piece)
    return absolute, parts


def normalize(entry: str, venues: tuple[str, ...] = ()) -> str:
    """An `output_artifacts` entry resolved lexically against the venue: `.`, `..` and a
    trailing slash settle, and an absolute entry under a venue root becomes relative to it. The
    venue itself is the empty string; an absolute entry outside every venue stays absolute."""
    absolute, parts = _split(str(entry).strip())
    if absolute:
        for venue in venues:
            _, root = _split(str(venue).strip())
            if root and parts[:len(root)] == root:
                return "/".join(parts[len(root):])
        return "/" + "/".join(parts)
    return "/".join(parts)


def overlaps(a: str, b: str) -> bool:
    """Equal, or one a path-component prefix of the other; the venue itself overlaps every
    relative entry."""
    a_abs, b_abs = a.startswith("/"), b.startswith("/")
    if a_abs != b_abs:
        return False
    if not a or not b:
        return True
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def covers(entry: str, own: str) -> bool:
    """`entry` is `own` or contains it."""
    if entry == own:
        return True
    return not entry.startswith("/") and not own.startswith("/") and (
        not entry or own.startswith(entry + "/"))


@dataclass
class Decision:
    matches: dict = field(default_factory=dict)
    carried: dict = field(default_factory=dict)
    breaches: list = field(default_factory=list)
    lost: list = field(default_factory=list)
    check_changed: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.breaches and not self.lost

    def reasons(self) -> list[str]:
        return [*self.breaches, *(
            f"live stage {title!r} has no continuation in this plan" for title in self.lost)]


def decide(old: list[dict], new: list[dict], live: set[str]) -> Decision:
    """The identity each new stage takes, the continuation of every live stage the carry rule
    leaves unmatched, and what breaks the claim check."""
    previous = [{**o["record"], "uid": o["uid"]} for o in old]
    matches = effort_segments.match_identities(previous, [n["record"] for n in new])
    decision = Decision(matches=matches)
    settled = {o["uid"] for o in old if o["settled"]}
    unsettled_old = [o for o in old if not o["settled"]]
    unsettled_new = [n for n in new if matches[n["index"]] not in settled]
    matched = {uid for uid in matches.values() if uid}
    old_defs = {o["record"]["key"] for o in old}
    new_defs = {n["record"]["key"] for n in new}
    for o in unsettled_old:
        if o["uid"] not in live:
            continue
        others = [x for x in unsettled_old if x is not o]
        own = [e for e in o["outputs"]
               if not any(overlaps(e, f) for x in others for f in x["outputs"])]
        mine = next((n for n in new if matches[n["index"]] == o["uid"]), None)
        if own:
            claimants = [n for n in unsettled_new
                         if any(overlaps(e, f) for e in own for f in n["outputs"])]
            free = [n for n in claimants if matches[n["index"]] is None]
            version = mine if mine is not None else free[0] if len(free) == 1 else None
            extra = [n for n in claimants if n is not version]
            if extra:
                decision.breaches.append(
                    f"live stage {o['title']!r}: stage(s) {[n['index'] for n in extra]} also claim its "
                    f"own outputs {own}")
            elif version is None:
                decision.lost.append(o["title"])
            elif not all(any(covers(f, e) for f in version["outputs"]) for e in own):
                decision.breaches.append(
                    f"live stage {o['title']!r}: its next version does not cover its own outputs {own}")
            elif any(overlaps(f, g) for f in version["outputs"]
                     if any(f != e and covers(f, e) for e in own)
                     for n in unsettled_new if n is not version for g in n["outputs"]):
                decision.breaches.append(
                    f"live stage {o['title']!r}: its next version widens onto another stage's outputs")
            elif mine is None:
                decision.carried[o["uid"]] = version["index"]
        elif o["uid"] not in matched:
            same = [n for n in unsettled_new
                    if matches[n["index"]] is None and n["record"]["key"] == o["record"]["key"]]
            novel = [n for n in unsettled_new
                     if matches[n["index"]] is None and n["record"]["key"] not in old_defs]
            lone = [x for x in unsettled_old if x["record"]["key"] not in new_defs]
            if len(same) == 1:
                decision.carried[o["uid"]] = same[0]["index"]
            elif len(novel) == 1 and lone == [o]:
                decision.carried[o["uid"]] = novel[0]["index"]
            else:
                decision.lost.append(o["title"])
    if decision.carried:
        decision.matches = effort_segments.match_identities(
            previous, [n["record"] for n in new],
            pinned={index: uid for uid, index in decision.carried.items()})
    by_uid = {o["uid"]: o for o in old}
    for n in new:
        uid = decision.matches.get(n["index"])
        before = by_uid.get(uid)
        if before is not None and uid in live and before["check"] != n["check"]:
            decision.check_changed.append(n["index"])
    return decision
