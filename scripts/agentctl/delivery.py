"""Delivery stamp sidecar — proof that a presented plan rendering actually
reached the user, kept OUTSIDE state.json for a structural reason, not a
stylistic one.

FileStateStore.save (store.py) is a plain, unlocked `Path.write_text` — no lock,
no temp+rename. That is safe today only because the engine process itself is the
sole writer of state.json. The delivery hook (Stage 3) is a SEPARATE,
out-of-process writer that must publish a fact ("delivery verified") the engine
later reads. If it wrote into state.json it would race an in-flight cli.py
`save()` — the two unlocked `write_text` calls could interleave into a torn or
lost write, and a corrupt state.json is unrecoverable short of `reset --force`
(discarding the whole task). So the stamp lives in its own tiny sidecar file.

The sidecar write is ATOMIC (tempfile.mkstemp in the TARGET directory +
os.replace) even though today there is exactly one writer of it. The atomicity
is for the READER, not the writer: a reader (gates.plan_presentation_blockers,
by way of read_stamp) must never observe a half-written file, and `os.replace`
is the only POSIX primitive that guarantees that regardless of writer count —
building that guarantee in now costs nothing and means a future second writer
(e.g. a retry path) never has to revisit this module.

`read_stamp` fails OPEN at the file-I/O layer: absent, corrupt, truncated,
non-dict, or missing-key content all resolve to None, uniformly, with no
distinction surfaced to the caller. The caller (gates.py) is what turns that
None into a block — the one deliberate fail-CLOSED inversion in this design,
justified there, not here: this module only ever reports "no positive fact is
available," never *why*.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

# The two legal values of DeliveryStamp.source: "hook" is a positive out-of-band
# verification; "override" is the human escape (cmd_confirm_delivery). Exported
# so cli.py/gates.py check membership against one definition instead of
# re-typing the literal strings.
SOURCE_HOOK = "hook"
SOURCE_OVERRIDE = "override"
DELIVERY_STAMP_SOURCES = (SOURCE_HOOK, SOURCE_OVERRIDE)


@dataclass
class DeliveryStamp:
    """One verified-delivery fact, bound to the plan version and rendering it
    verifies. `plan_sha256`/`rendering_sha256` mirror PlanPresentation's own
    bindings so gates.plan_presentation_blockers can require an exact match
    between "what was presented" and "what was verified delivered" without
    this module importing state.py. `source` is "hook" for a positive
    out-of-band verification (the delivery hook confirmed the rendering's
    bytes reached the transcript) or "override" for the human escape
    (`confirm-delivery`); `by`/`note` are populated only for "override"."""
    plan_path: str
    plan_sha256: str
    rendering_sha256: str
    verified_ts: float
    source: str
    by: str = ""
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "DeliveryStamp":
        return cls(
            plan_path=d["plan_path"],
            plan_sha256=d["plan_sha256"],
            rendering_sha256=d["rendering_sha256"],
            verified_ts=d["verified_ts"],
            source=d["source"],
            by=d.get("by", ""),
            note=d.get("note", ""),
        )


def stamp_path_for(state_file: Path) -> Path:
    """The sidecar path for a session's state file: `<state_file>.delivery.json`
    (`.json` -> `.delivery.json` via `with_suffix`, e.g. `abc.json` ->
    `abc.delivery.json`). Deriving FROM the already-resolved state file — rather
    than re-deriving a session's root independently — means a caller that used
    `lib.config_root.resolve_agentctl_state_file` to find a session across the
    current/legacy root split inherits that resolution for free: the sidecar
    always sits next to whichever state file was actually found."""
    return state_file.with_suffix(".delivery.json")


def write_stamp(state_file: Path, stamp: DeliveryStamp) -> None:
    """Atomically publish `stamp` as state_file's sidecar.

    mkstemp is created in `state_file.parent` (the TARGET directory) —
    `os.replace` only guarantees atomicity within a single filesystem, so a
    tempfile created under a different mount (e.g. the platform default /tmp)
    could make the replace raise instead of atomically renaming."""
    target = stamp_path_for(state_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=target.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(asdict(stamp), ensure_ascii=False, indent=2, sort_keys=True))
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_stamp(state_file: Path) -> DeliveryStamp | None:
    """The current delivery stamp for state_file's session, or None on ANY
    unreadable/corrupt/absent/malformed case (see module docstring — fail-open
    at this layer; the caller decides what None means)."""
    target = stamp_path_for(state_file)
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return DeliveryStamp.from_dict(data)
    except (KeyError, TypeError):
        return None


def delete_stamp(state_file: Path) -> None:
    """Remove a session's delivery sidecar, if present. Called by `agentctl
    reset` for HYGIENE only — a stale stamp cannot silently clear a later
    task's gate because the gate binds on plan_sha256/rendering_sha256, and a
    reset task gets a fresh plan. Deleting it anyway avoids leaving an orphaned
    file around and avoids any confusion from `status`/debugging output that
    would otherwise show a stamp for a task that no longer exists. Best-effort:
    a missing file or a permission error is not this command's problem to
    surface."""
    target = stamp_path_for(state_file)
    try:
        target.unlink()
    except OSError:
        pass
