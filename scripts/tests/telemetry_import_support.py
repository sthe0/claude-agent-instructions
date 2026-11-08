"""Shared fixtures of the importer tests: a generated world of ledgers, engine state files and
transcripts under a tmp directory (never the live ones), and builders of the frozen inputs
`check-profile-unchanged` reads."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lib import config_root, judge_ledger
from lib.telemetry import importer, live, profile_check, store
from telemetry_live_support import no_background_sweep, query

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def at(seconds: float) -> str:
    """An ISO timestamp `seconds` after the fixed base."""
    return (BASE + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def line(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"


class World:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.gate = config_root.agentctl_gate_log()
        self.decision = config_root.decision_log()
        self.judge = judge_ledger.ledger_path()
        self.quality = tmp_path / "ledgers" / "quality.jsonl"
        self.spawn = tmp_path / "ledgers" / "spawn.jsonl"
        self.state_dir = tmp_path / "engine" / "state"
        self.projects = tmp_path / "projects"
        self.commits: list = []
        for path in (self.gate, self.decision, self.judge, self.quality, self.spawn):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.projects.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(importer, "quality_ledger", lambda: self.quality)
        monkeypatch.setattr(importer, "spawn_ledger", lambda: self.spawn)
        monkeypatch.setattr(importer, "_commit_index", lambda: self.commits)
        monkeypatch.setattr(importer, "BATCH_DEADLINE_S", 30.0)
        monkeypatch.setattr(store, "DEADLINE_S", 30.0)
        monkeypatch.setattr(config_root, "agentctl_state_dir", lambda: self.state_dir)
        monkeypatch.setattr(config_root, "projects_roots", lambda: [self.projects])
        monkeypatch.setattr(config_root, "iter_transcripts",
                            lambda pattern="*/*.jsonl": sorted(self.projects.glob(pattern)))
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv(live.SYNTHETIC_ENV, raising=False)
        monkeypatch.delenv(live.SWITCH_ENV, raising=False)
        no_background_sweep(monkeypatch)

    def add(self, path: Path, *objs) -> list:
        """Append the objects (or raw bytes) as lines; each result is (offset, sha256, raw)."""
        out = []
        with open(path, "ab") as handle:
            for obj in objs:
                raw = obj if isinstance(obj, bytes) else line(obj)
                offset = handle.tell()
                handle.write(raw)
                out.append((offset, hashlib.sha256(raw).hexdigest(), raw))
        return out

    def gate_line(self, session: str, gate: str = "plan", passed: bool = True, seconds: float = 10) -> dict:
        return {"ts": at(seconds), "session": session, "gate": gate, "passed": passed}

    def state(self, session: str, history: list, task=None, parent=None, depth=0, weight=None) -> Path:
        data = {"session_id": session, "task_id": task or session, "recursion_depth": depth, "history": history}
        if parent:
            data["parent_link"] = {"parent_session": parent, "depth": depth}
        if weight:
            data["weight_class"] = weight
        path = self.state_dir / f"{session}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def transcript(self, session: str, lines: list, project: str = "p") -> Path:
        path = self.projects / project / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(line(obj) for obj in lines))
        return path

    def run(self, **kwargs) -> dict:
        return importer.run_import(**kwargs)

    def live_event(self, session: str, kind: str, source_key: str, fields=None, backref=None) -> str:
        event_id = live.emit_event(session, kind, source_key, fields or {}, backref)
        assert event_id
        return event_id

    def replay(self, session: str) -> dict:
        return store.replay(session)

    def id_of(self, path: Path, offset: int, sha: str) -> str:
        return store.imported_row_id(str(path), offset, sha, store.origin_id())

    def offsets(self, path: Path) -> list:
        return query("SELECT consumer, byte_offset FROM import_offsets WHERE source = ? ORDER BY consumer, byte_offset",
                     str(path))

    def one(self, sql: str, *args):
        rows = query(sql, *args)
        return rows[0][0] if rows else None

    def count(self, table: str = "events", where: str = "1 = 1", *args) -> int:
        return query(f"SELECT COUNT(*) FROM {table} WHERE {where}", *args)[0][0]


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def prompt(seconds: float, text: str = "go") -> dict:
    return {"type": "user", "timestamp": at(seconds), "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def reply(seconds: float, message_id: str = "m", blocks=None) -> dict:
    return {"type": "assistant", "timestamp": at(seconds),
            "message": {"id": message_id, "content": blocks or [{"type": "text", "text": "ok"}]}}


def bash_call(seconds: float, call_id: str, command: str) -> dict:
    return reply(seconds, f"m-{call_id}", [{"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}])


def bash_result(seconds: float, call_id: str, error: str = "") -> dict:
    block = {"type": "tool_result", "tool_use_id": call_id, "content": error or "done", "is_error": bool(error)}
    return {"type": "user", "timestamp": at(seconds), "message": {"role": "user", "content": [block]}}


# --- the frozen inputs of check-profile-unchanged --------------------------------------------

def _gz_tar(path: Path, members: "list[tuple[str, bytes | None]]") -> None:
    """A gzip tar named like the stage-1 snapshot; a member whose data is None is a directory."""
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


DAY = 86400.0


def cut_stamp() -> datetime:
    """The frozen reference's moment: 40 days after the base, so session ages differ by days."""
    return BASE + timedelta(seconds=40 * DAY)


def session_lines(start_s: float, past_cut: bool = False, edge: bool = False) -> list:
    """Four timed turns from `start_s`; `edge` adds a prompt half a second after the reference moment
    (inside the one-second slack), `past_cut` two turns well after it."""
    lines = [prompt(start_s), reply(start_s + 5), prompt(start_s + 20), reply(start_s + 30)]
    if edge:
        lines += [prompt(40 * DAY + 0.5), reply(40 * DAY + 0.7)]
    if past_cut:
        lines += [prompt(40 * DAY + 400), reply(40 * DAY + 410)]
    return lines


class Frozen:
    """A generated copy of the stage-1 inputs: premises, ledgers, state tar, transcripts tar and a
    reference recomputed from them (so an unmodified tree compares equal)."""

    def __init__(self, directory: Path):
        self.dir = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.premises_path = directory / "premises.json"
        self.reference_path = directory / "reference.json"
        self.snapshot = directory / "transcripts-snapshot.tar"
        self.members = [("proj/s1.jsonl", b"".join(line(o) for o in session_lines(0, edge=True))),
                        ("proj/s2.jsonl", b"".join(line(o) for o in session_lines(20 * DAY, past_cut=True))),
                        ("proj/root.jsonl", b"".join(line(o) for o in session_lines(40 * DAY - 60, past_cut=True)))]
        self.half_life = 30.0
        for name in ("spawn-ledger.jsonl", "judge-ledger.jsonl", "policy-ledger.jsonl"):
            (directory / name).write_bytes(b"")
        self.write_state()

    def write_state(self) -> None:
        _gz_tar(self.dir / "state-snapshot.tar",
                [("agentctl/", None), ("agentctl/state", None), ("agentctl/engine-session-bindings", None)])

    def write_snapshot(self) -> None:
        _gz_tar(self.snapshot, self.members)

    def write_premises(self, **override) -> dict:
        premises = {"reference_taken_at": cut_stamp().isoformat(), "transcript_snapshot_sha256": _sha(self.snapshot),
                    "spawn_ledger_sha256": _sha(self.dir / "spawn-ledger.jsonl"),
                    "judge_ledger_sha256": _sha(self.dir / "judge-ledger.jsonl"),
                    "policy_ledger_sha256": _sha(self.dir / "policy-ledger.jsonl"),
                    "state_snapshot_sha256": _sha(self.dir / "state-snapshot.tar"), **override}
        self.premises_path.write_text(json.dumps(premises), encoding="utf-8")
        return premises

    def freeze(self) -> "Frozen":
        self.write_snapshot()
        premises = self.write_premises()
        scratch = self.dir / "scratch"
        fresh = profile_check.recompute({"half_life_days": self.half_life}, premises, self.snapshot, self.dir, scratch)
        self.reference = fresh["payload"]
        self.save_reference()
        return self

    def save_reference(self) -> None:
        self.reference_path.write_text(json.dumps(self.reference), encoding="utf-8")

    def check(self) -> "tuple[int, dict]":
        return profile_check.check(self.reference_path, self.premises_path, self.snapshot)
