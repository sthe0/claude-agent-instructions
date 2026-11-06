"""`telemetry.py check-profile-unchanged`: does the time-profile reader still produce the figures
the stage-1 reference froze, now that it reads through `phases.py`?

The reference (`reference-time-profile.json`) holds only aggregate figures, so the comparison
recomputes the aggregate from the same frozen inputs and never from anything live:

- the transcripts come from `transcripts-snapshot.tar` (gzip despite the name, streamed member by
  member, never the live projects directory), each member cut to the complete lines stamped before
  `reference_taken_at` + 1 s (the stamp is whole seconds, the lines milliseconds); the archive's last
  member, the root's own transcript prefix stage 1 cut by byte size, is read whole;
- the spawn, judge and policy ledgers and the engine state (with its session bindings) are the
  frozen copies beside `premises.json`, each checked against its sha256 there;
- the decay clock is pinned to `reference_taken_at` and the half-life is the reference's own;
- the profile is read through the forced transcript path and a fresh scratch cache.

Exit 0 when every figure agrees within one unit of its sixth significant digit; 1 when a figure
differs (named); 2 when a frozen input's sha256 differs or the profiled or skipped session set
differs (ids named, with whether their transcript is in the snapshot) — the coordinator reads 2 as a
premise failure, never as a pass. Transcripts with lines past the cut are named in the report.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from lib import engine_session_binding, time_profile

CUT_SLACK_MS = 1000
SIGNIFICANT_DIGITS = 6
EXIT_SAME, EXIT_FIGURE, EXIT_PREMISE = 0, 1, 2
LEDGER_FILES = {"spawn": ("spawn-ledger.jsonl", "spawn_ledger_sha256"),
                "judge": ("judge-ledger.jsonl", "judge_ledger_sha256"),
                "policy": ("policy-ledger.jsonl", "policy_ledger_sha256")}
STATE_FILE = ("state-snapshot.tar", "state_snapshot_sha256")


class PremiseError(Exception):
    """A frozen input is not the one the reference was taken from."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify(path: Path, expected, label: str) -> None:
    if not path.is_file():
        raise PremiseError(f"{label}: {path} is missing")
    actual = sha256_of(path)
    if actual != expected:
        raise PremiseError(f"{label}: sha256 {actual} is not the premises' {expected}")


def _ms(stamp) -> "int | None":
    return time_profile._epoch_or_iso_ms(stamp)


def _reference_ms(premises: dict) -> int:
    moment = datetime.fromisoformat(premises["reference_taken_at"])
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def _line_ms(raw: bytes) -> "int | None":
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    return _ms(obj.get("timestamp")) if isinstance(obj, dict) else None


def _member_names(snapshot: Path) -> list:
    with tarfile.open(snapshot, "r|*") as tar:
        return [member.name for member in tar if member.isreg()]


def _is_session_member(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return name.endswith(".jsonl") and len(parts) >= 2 and "subagents" not in parts


def _destination(projects: Path, name: str) -> Path:
    parts = PurePosixPath(name).parts
    return projects / "__".join(parts[:-1]) / parts[-1]


def _copy_member(source, destination: Path, cut_ms: "int | None") -> bool:
    """Copy complete lines stamped before `cut_ms` (all of them when None); True when the member
    holds a line at or past the cut."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    past = False
    with open(destination, "wb") as sink:
        for raw in source:
            if cut_ms is not None:
                if not raw.endswith(b"\n"):
                    continue
                stamp = _line_ms(raw)
                if past or (stamp is not None and stamp >= cut_ms):
                    past = True
                    continue
            sink.write(raw)
    return past


def extract_transcripts(snapshot: Path, projects: Path, cut_ms: int) -> "tuple[set, list]":
    """(stems of every session member, stems of those with lines past the cut)."""
    names = [name for name in _member_names(snapshot) if _is_session_member(name)]
    root_member = names[-1] if names else None
    stems, past = set(), []
    with tarfile.open(snapshot, "r|*") as tar:
        for member in tar:
            if not member.isreg() or not _is_session_member(member.name):
                continue
            stem = PurePosixPath(member.name).stem
            stems.add(stem)
            whole = member.name == root_member
            if _copy_member(tar.extractfile(member), _destination(projects, member.name),
                            None if whole else cut_ms):
                past.append(stem)
    return stems, sorted(past)


def extract_state(archive: Path, root: Path) -> Path:
    """Extract the engine state tar; return its `state` directory (the one with its session
    bindings beside it when several exist)."""
    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tar:
        for member in tar:
            target = (root / member.name).resolve()
            if root.resolve() not in target.parents and target != root.resolve():
                raise PremiseError(f"state snapshot member {member.name!r} escapes its directory")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isreg():
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "wb") as sink:
                    shutil.copyfileobj(tar.extractfile(member), sink)
    candidates = sorted(p for p in root.rglob("state") if p.is_dir())
    beside = [p for p in candidates if (p.parent / engine_session_binding.BINDINGS_DIRNAME).is_dir()]
    if not (beside or candidates):
        raise PremiseError("state snapshot holds no state directory")
    return (beside or candidates)[0]


@contextlib.contextmanager
def _pinned_environment(root: Path, cache: Path):
    pinned = {"CLAUDE_CONFIG_DIR": str(root), "CLAUDE_AGENT_HOME": str(root), "TIME_PROFILE_STATE_DIR": str(cache)}
    saved = {name: os.environ.get(name) for name in pinned}
    os.environ.update(pinned)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _unit(value: float) -> float:
    return 0.0 if value == 0 else 10.0 ** (math.floor(math.log10(abs(value))) - (SIGNIFICANT_DIGITS - 1))


def _within_unit(a, b) -> bool:
    if a == b:
        return True
    return abs(a - b) <= max(_unit(a), _unit(b)) * (1 + 1e-9)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def differences(reference, fresh, path: str = "") -> list:
    """Paths at which the two aggregate JSON trees differ by more than one unit of the sixth digit."""
    if _is_number(reference) and _is_number(fresh):
        return [] if _within_unit(reference, fresh) else [f"{path}: {reference} != {fresh}"]
    if isinstance(reference, dict) and isinstance(fresh, dict):
        out = []
        for key in sorted(set(reference) | set(fresh)):
            if key not in reference or key not in fresh:
                out.append(f"{path}.{key}: present in only one of the two")
            else:
                out.extend(differences(reference[key], fresh[key], f"{path}.{key}"))
        return out
    if isinstance(reference, list) and isinstance(fresh, list):
        if len(reference) != len(fresh):
            return [f"{path}: {len(reference)} entries != {len(fresh)}"]
        return [d for index, (a, b) in enumerate(zip(reference, fresh)) for d in differences(a, b, f"{path}[{index}]")]
    return [] if reference == fresh else [f"{path}: {reference!r} != {fresh!r}"]


def _session_set_differences(reference_run: dict, fresh_run: dict, stems: set) -> list:
    out = []
    ref_skipped = {s["session"] for s in reference_run.get("skipped", [])}
    new_skipped = {s["session"] for s in fresh_run.get("skipped", [])}
    for session in sorted(ref_skipped ^ new_skipped):
        side = "skipped only in the reference" if session in ref_skipped else "skipped only now"
        out.append(f"{session}: {side}, transcript {'in the snapshot' if session in stems else 'gone from the snapshot'}")
    if reference_run.get("profiled") != fresh_run.get("profiled"):
        out.append(f"profiled sessions: {reference_run.get('profiled')} != {fresh_run.get('profiled')}")
    return out


def recompute(reference: dict, premises: dict, snapshot: Path, inputs: Path, scratch: Path) -> dict:
    """The aggregate payload of the frozen inputs, with the transcripts' stems and those past the cut."""
    from lib.agent_report import time_profile as view

    pinned_ms = _reference_ms(premises)
    ledgers = {}
    for kind, (filename, key) in LEDGER_FILES.items():
        ledgers[kind] = inputs / filename
        _verify(ledgers[kind], premises[key], f"{kind} ledger")
    state_archive = inputs / STATE_FILE[0]
    _verify(state_archive, premises[STATE_FILE[1]], "engine state snapshot")
    state_dir = extract_state(state_archive, scratch / "engine")
    projects = scratch / "projects"
    stems, past = extract_transcripts(snapshot, projects, pinned_ms + CUT_SLACK_MS)
    with _pinned_environment(scratch / "home", scratch / "cache"):
        run = time_profile.profile_all(
            scratch / "cache", projects,
            agentctl_state_dir=state_dir,
            spawn_ledger=ledgers["spawn"],
            judge_ledger_path=ledgers["judge"],
            policy_ledger=ledgers["policy"],
            commit_index=[], source="transcripts")
    payload = view.build_payload(run, reference["half_life_days"], False, now_ms=pinned_ms)
    return {"payload": view._rounded(payload), "stems": stems, "past_cut": past}


def check(reference_path, premises_path, snapshot_path) -> "tuple[int, dict]":
    reference = json.loads(Path(reference_path).read_text(encoding="utf-8"))
    premises_file = Path(premises_path)
    premises = json.loads(premises_file.read_text(encoding="utf-8"))
    snapshot = Path(snapshot_path)
    scratch = Path(tempfile.mkdtemp(prefix="check-profile-unchanged-"))
    try:
        _verify(snapshot, premises["transcript_snapshot_sha256"], "transcripts snapshot")
        fresh = recompute(reference, premises, snapshot, premises_file.parent, scratch)
    except PremiseError as exc:
        return EXIT_PREMISE, {"verdict": "premise-failed", "reason": str(exc)}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    report = {"past_cut": fresh["past_cut"]}
    sets = _session_set_differences(reference.get("run", {}), fresh["payload"].get("run", {}), fresh["stems"])
    if sets:
        return EXIT_PREMISE, {**report, "verdict": "session-set-differs", "differences": sets}
    figures = differences({k: v for k, v in reference.items() if k != "run"},
                          {k: v for k, v in fresh["payload"].items() if k != "run"})
    if figures:
        return EXIT_FIGURE, {**report, "verdict": "figure-differs", "differences": figures}
    return EXIT_SAME, {**report, "verdict": "unchanged"}


def run(reference_path, premises_path, snapshot_path) -> int:
    code, report = check(reference_path, premises_path, snapshot_path)
    print(json.dumps(report, sort_keys=True))
    return code
