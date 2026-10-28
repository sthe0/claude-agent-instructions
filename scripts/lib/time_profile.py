"""Where a session's wall-clock goes: an exclusive time partition plus overlays.

Difficulty removed: nothing in Core measures time. Which mechanism to speed up
(a tool, a hook, a judge, a spawned specialist, the user's own reply latency) is
decided on guessed minutes, because the transcript holds the timestamps but no
reader turns them into per-mechanism minutes that add up.

Two kinds of figure, kept apart on purpose:

* PARTITION -- splits [first_ts, last_ts] of one session into keyed intervals
  that sum to wall-clock exactly (integer ms; `conservation_error_ms`). Every
  key, `model:gap` and `unattributed` included, is the sum of the intervals the
  walker assigned to it; no key is computed as wall-clock minus the others, so
  conservation tests the walker (an interval assigned twice, or dropped in the
  parallel split or the hook carve-out, breaks it).
* OVERLAYS -- `node:<NODE>` dwell, `spawn:<kind>` and `judge:<name>` durations.
  They run inside partition intervals (a spawn runs inside a Bash call, a judge
  inside a hook) and overlap each other, so they are NEVER added to the
  partition: no minute is counted twice.

Partition keys, all derived from structural fields (tool name, tool_use input
field, attachment command, shell token) -- the module does not import `re`:

  user:wait         end of an assistant turn to the next real user prompt, plus an
                    open AskUserQuestion call
  wait:background   a gap ending in a task-notification (queued_command) entry; a
                    queued_command carrying a message typed mid-turn is model:gap
  tool:<Name>       tool_use ts to the matching tool_result ts; Bash is
                    tool:Bash:<program> (script basename for an interpreter; the
                    first positional subcommand joins agentctl-cli.py and git as
                    `agentctl-cli.py:status`), Skill is skill:<name>, Agent is
                    tool:Agent:<subagent_type>, an MCP tool is mcp:<server>
  hook:<basename>   carved out of the interval ending at a hook attachment (an
                    attachment entry carrying command + durationMs); hooks the
                    transcript does not expose stay inside the interval they ran in
  model:gap         an interval that ends at an assistant entry outside any tool
                    or wait interval
  model:compact     an interval that ends at a compact_boundary system entry
  unattributed      an interval no rule above claims

Parallel tool calls split their shared interval equally (integer ms, the
remainder going to the earliest-opened calls). Entry types other than
assistant/user/attachment/system are bookkeeping: their time folds into the next
terminating interval.

Overlays join by the harness session id (the transcript stem): spawn rows by their
`session_id`, node dwell through the state file of the same id and of every engine
session the harness session drove (`engine-session-bindings` beside the state dir).

Cache: $TIME_PROFILE_STATE_DIR (default ~/.local/state/time-profile/). A session
is re-parsed only when its transcript fingerprint (mtime_ns, size) or WALKER_VERSION
(a hash of this module and the helpers it parses with) changes;
append-only ledgers are read from a stored byte offset and rescanned when the
file shrinks or its device/inode changes. The agentctl gate-log is never opened.
The module reads transcripts, ledgers and agentctl state and never writes them.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from lib import config_root, engine_session_binding, judge_ledger, shell_tokens, transcript_cost, transcript_turns

SKIP_REASONS = ("empty", "no_timestamps", "unparseable")

CACHE_VERSION = 2

USER_WAIT = "user:wait"
WAIT_BACKGROUND = "wait:background"
MODEL_GAP = "model:gap"
MODEL_COMPACT = "model:compact"
UNATTRIBUTED = "unattributed"

_TERMINATING_TYPES = frozenset({"assistant", "user", "attachment", "system"})
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
_MS = dt.timedelta(milliseconds=1)

def _walker_version() -> str:
    """Hash of the sources that decide a cached figure, so editing a label rule or the
    walker (here or in the helpers it tokenizes and parses with) invalidates the caches."""
    digest = hashlib.sha256(str(CACHE_VERSION).encode("ascii"))
    for source in (__file__, shell_tokens.__file__, transcript_cost.__file__, transcript_turns.__file__):
        try:
            digest.update(Path(source).read_bytes())
        except OSError:
            digest.update(str(source).encode("utf-8"))
    return digest.hexdigest()[:16]


WALKER_VERSION = _walker_version()

_SPAWN_LEDGER = Path.home() / ".local" / "log" / "claude-spawn-costs.jsonl"
_POLICY_LEDGER = Path.home() / ".local" / "log" / "claude-policy-ledger.jsonl"
_REPO_ROOT = Path(__file__).resolve().parents[2]
TRUNK_REFS = ("origin/main", "main", "HEAD")


@dataclass
class SessionProfile:
    session_id: str
    transcript: str
    first_ts_ms: int
    last_ts_ms: int
    partition: dict
    overlays: dict
    coverage: dict
    core_sha: str | None
    child_of: str | None
    kind: str


@dataclass
class Skipped:
    session_id: str
    transcript: str
    reason: str


@dataclass
class ProfileRun:
    profiles: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    reparsed: list = field(default_factory=list)


def conservation_error_ms(profile: SessionProfile) -> int:
    return sum(profile.partition.values()) - (profile.last_ts_ms - profile.first_ts_ms)


# --------------------------------------------------------------------------- readers


def iter_entries(path: Path | str) -> Iterator[dict]:
    """Transcript entries, streamed; unparseable lines are dropped by iter_jsonl."""
    yield from transcript_cost.iter_jsonl(path)


def iter_ledger_rows(path: Path | str, offset: int = 0) -> Iterator[tuple]:
    """(row | None, end_offset) per complete line from byte `offset`.

    `row` is None for a complete line that is not a JSON object; the end offset
    still advances past it. A final line without its newline is a write in
    progress and is left for the next read.
    """
    try:
        handle = Path(path).open("rb")
    except OSError:
        return
    with handle as fh:
        fh.seek(offset)
        pos = offset
        for raw in fh:
            if not raw.endswith(b"\n"):
                return
            pos += len(raw)
            try:
                obj = json.loads(raw)
            except ValueError:
                obj = None
            yield (obj if isinstance(obj, dict) else None), pos


# ------------------------------------------------------------------- time and labels


def _ts_ms(value) -> int | None:
    parsed = transcript_cost.parse_ts_or_none(value)
    return None if parsed is None else (parsed - _EPOCH) // _MS


def _finite_number(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _epoch_or_iso_ms(value) -> int | None:
    """An engine history `ts`: epoch seconds (the engine's own clock) or an ISO string."""
    if _finite_number(value):
        return int(round(value * 1000))
    return _ts_ms(value)


_PUNCTUATION = frozenset("();<>|&")
_WRAPPERS = frozenset({"env", "time", "nohup", "exec", "command", "sudo", "nice"})
_SKIPPED_PROGRAMS = frozenset({"cd", "export", "set", "source", ".", "true"})
_SHELL_INTERPRETERS = frozenset({"bash", "sh", "zsh", "node", "perl", "ruby"})
_SUBCOMMAND_PROGRAMS = frozenset({"agentctl-cli.py", "git"})
_VALUE_OPTIONS = frozenset({"-C", "-c", "--session", "--state-dir", "--git-dir", "--work-tree"})


def _statements(command: str) -> list | None:
    text = shell_tokens.command_line(shell_tokens.strip_heredoc_bodies(command))
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    statements: list = [[]]
    try:
        for token in lexer:
            if set(token) <= _PUNCTUATION:
                if statements[-1]:
                    statements.append([])
            else:
                statements[-1].append(token)
    except ValueError:
        return None
    return [words for words in statements if words]


def _is_assignment(word: str) -> bool:
    name, sep, _ = word.partition("=")
    return bool(sep) and name.isidentifier()


def _drop_prefix(words: list) -> list:
    while words:
        head = os.path.basename(words[0])
        if _is_assignment(words[0]):
            words = words[1:]
        elif head in _WRAPPERS:
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[1:]
        elif head == "timeout":
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[1:]
            words = words[1:]
        else:
            break
    return words


def _first_positional(args: list) -> str | None:
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in _VALUE_OPTIONS:
            skip = True
        elif not arg.startswith("-"):
            return arg
    return None


def _label_of(words: list, subcommands: bool) -> str:
    program = os.path.basename(words[0])
    args = words[1:]
    if program in _SHELL_INTERPRETERS or program.startswith("python"):
        label = None
        for i, arg in enumerate(args):
            if arg == "-m" and i + 1 < len(args):
                return args[i + 1]
            if arg == "-c" or (arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]):
                return program
            if not arg.startswith("-"):
                label, args = os.path.basename(arg), args[i + 1:]
                break
        if label is None:
            return program
    else:
        label = program
    if subcommands and label in _SUBCOMMAND_PROGRAMS:
        sub = _first_positional(args)
        if sub:
            return f"{label}:{sub}"
    return label


def command_label(command, subcommands: bool = True) -> str | None:
    """The program a shell command runs, from its tokens; None when it has none."""
    if not isinstance(command, str):
        return None
    for words in _statements(command) or []:
        words = _drop_prefix(words)
        if words and os.path.basename(words[0]) not in _SKIPPED_PROGRAMS:
            return _label_of(words, subcommands)
    return None


def tool_key(name, tool_input) -> str:
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    name = name if isinstance(name, str) and name else "unknown"
    if name == "AskUserQuestion":
        return USER_WAIT
    if name == "Bash":
        label = command_label(tool_input.get("command"))
        return f"tool:Bash:{label}" if label else "tool:Bash"
    if name == "Skill":
        skill = tool_input.get("skill")
        return f"skill:{skill}" if isinstance(skill, str) and skill else "tool:Skill"
    if name in ("Agent", "Task"):
        return f"tool:Agent:{tool_input.get('subagent_type') or 'general-purpose'}"
    if name.startswith("mcp__"):
        return f"mcp:{name.split('__')[1]}"
    return f"tool:{name}"


def hook_key(attachment: dict) -> str | None:
    command = attachment.get("command")
    duration = attachment.get("durationMs")
    if not isinstance(command, str) or not _finite_number(duration):
        return None
    label = command_label(command, subcommands=False) or os.path.basename(command.strip()) or "unknown"
    return f"hook:{label}"


# ------------------------------------------------------------------------- the walker


def _content_blocks(entry: dict) -> list:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _is_real_prompt(entry: dict) -> bool:
    return (
        entry.get("type") == "user"
        and not entry.get("isCompactSummary")
        and transcript_turns._is_real_user_prompt(entry)
    )


def _is_task_notification(attachment: dict) -> bool:
    """A queued_command that is a background-task notification, not a message typed mid-turn."""
    if attachment.get("commandMode") == "task-notification":
        return True
    prompt = attachment.get("prompt")
    return isinstance(prompt, str) and prompt.lstrip().startswith("<task-notification")


def _closed_key(entry: dict) -> str:
    kind = entry.get("type")
    if kind == "assistant":
        return MODEL_GAP
    if kind == "attachment":
        attachment = entry.get("attachment")
        if isinstance(attachment, dict) and attachment.get("type") == "queued_command":
            return WAIT_BACKGROUND if _is_task_notification(attachment) else MODEL_GAP
    elif kind == "user":
        if _is_real_prompt(entry):
            return USER_WAIT
    elif kind == "system" and entry.get("subtype") == "compact_boundary":
        return MODEL_COMPACT
    return UNATTRIBUTED


def _split_equally(total: int, keys: list) -> list:
    share, extra = divmod(total, len(keys))
    return [(key, share + (1 if i < extra else 0)) for i, key in enumerate(keys)]


class _Walk:
    def __init__(self) -> None:
        self.partition: dict = {}
        self.timed = 0
        self.untimed = 0
        self.hook_ms = 0
        self.first: int | None = None
        self.last: int | None = None
        self._cursor: int | None = None
        self._open: dict = {}
        self._seen_calls: set = set()
        self._assistant_id = None
        self._tail_ts: int | None = None
        self._tail: list = []

    def _add(self, key: str, ms: int) -> None:
        if ms:
            self.partition[key] = self.partition.get(key, 0) + ms

    def _hook_attachment(self, entry: dict) -> "tuple | None":
        attachment = entry.get("attachment") if entry.get("type") == "attachment" else None
        key = hook_key(attachment) if isinstance(attachment, dict) else None
        return None if key is None else (key, max(0, int(attachment["durationMs"])))

    def _interval(self, entry: dict, ts: int, length: int) -> None:
        hook = self._hook_attachment(entry)
        if hook is not None:
            carved = min(length, hook[1])
            self._add(hook[0], carved)
            self.hook_ms += carved
            length -= carved
        booked: list = []
        if length:
            if self._open:
                booked = _split_equally(length, list(self._open.values()))
            else:
                booked = [(_closed_key(entry), length)]
            for key, ms in booked:
                self._add(key, ms)
        self._tail_ts, self._tail = ts, booked

    def _carve_same_ts(self, entry: dict, ts: int) -> None:
        """A hook attachment stamped like the entry before it (parallel hooks) takes its
        duration out of the interval that entry closed, never more than is left of it."""
        hook = self._hook_attachment(entry)
        if hook is None or ts != self._tail_ts:
            return
        key, want = hook
        for i in range(len(self._tail) - 1, -1, -1):
            if not want:
                break
            tail_key, ms = self._tail[i]
            taken = min(ms, want)
            want -= taken
            self._tail[i] = (tail_key, ms - taken)
            self.partition[tail_key] -= taken
            if not self.partition[tail_key]:
                del self.partition[tail_key]
            self._add(key, taken)
            self.hook_ms += taken

    def _apply(self, entry: dict) -> None:
        kind = entry.get("type")
        if kind == "assistant":
            message = entry.get("message")
            message_id = (message.get("id") if isinstance(message, dict) else None) or entry.get("requestId")
            if self._open and message_id and message_id != self._assistant_id:
                self._open.clear()
            if message_id:
                self._assistant_id = message_id
            for block in _content_blocks(entry):
                call_id = block.get("id")
                if block.get("type") == "tool_use" and isinstance(call_id, str) and call_id not in self._seen_calls:
                    self._seen_calls.add(call_id)
                    self._open[call_id] = tool_key(block.get("name"), block.get("input"))
        elif kind == "user":
            if _is_real_prompt(entry):
                self._open.clear()
            for block in _content_blocks(entry):
                if block.get("type") == "tool_result":
                    self._open.pop(block.get("tool_use_id"), None)

    def feed(self, entry: dict) -> None:
        ts = _ts_ms(entry.get("timestamp"))
        if ts is None:
            self.untimed += 1
            return
        self.timed += 1
        if self.first is None:
            self.first = self._cursor = self.last = ts
        self.last = max(self.last, ts)
        if entry.get("type") in _TERMINATING_TYPES:
            if ts > self._cursor:
                self._interval(entry, ts, ts - self._cursor)
                self._cursor = ts
            elif ts == self._cursor:
                self._carve_same_ts(entry, ts)
            self._apply(entry)

    def finish(self) -> None:
        if self.first is not None and self.last > self._cursor:
            self._add(UNATTRIBUTED, self.last - self._cursor)
            self._cursor = self.last


def _walk_transcript(path: Path) -> "_Walk | str":
    """The walked transcript, or a SKIP_REASONS string."""
    try:
        size = path.stat().st_size
    except OSError:
        return "empty"
    if size == 0:
        return "empty"
    walk = _Walk()
    seen = False
    for entry in iter_entries(path):
        seen = True
        if entry.get("isSidechain"):
            continue
        walk.feed(entry)
    if not seen:
        return "unparseable"
    if walk.first is None:
        return "no_timestamps"
    walk.finish()
    return walk


# ----------------------------------------------------------------------- ledger cache


def _resume_offset(saved: dict | None, stat: os.stat_result) -> int:
    if not saved:
        return 0
    if saved.get("ino") != stat.st_ino or saved.get("dev") != stat.st_dev:
        return 0
    offset = int(saved.get("offset") or 0)
    return 0 if stat.st_size < offset else offset


def _fold_judge(agg: dict, row: dict) -> None:
    session, name, duration = row.get("source"), row.get("judge"), row.get("duration")
    if row.get("kind") != "call" or not isinstance(session, str) or not isinstance(name, str):
        return
    if not _finite_number(duration):
        return
    by_name = agg.setdefault("by_session", {}).setdefault(session, {})
    by_name[name] = by_name.get(name, 0) + int(round(duration * 1000))


def _fold_spawn(agg: dict, row: dict) -> None:
    """Spawn rows are keyed by `session_id`, the spawning process's harness id (the
    transcript stem); `spawner_session` is the engine session and may differ, so it is
    only the fallback for rows written without `session_id`."""
    if row.get("event") != "spawn":
        return
    spawner = row.get("session_id") if isinstance(row.get("session_id"), str) else row.get("spawner_session")
    kind, duration = row.get("kind"), row.get("duration_ms")
    if not isinstance(kind, str):
        return
    if isinstance(spawner, str) and _finite_number(duration):
        by_kind = agg.setdefault("by_session", {}).setdefault(spawner, {})
        by_kind[kind] = by_kind.get(kind, 0) + int(duration)
    child = row.get("child_session_id")
    if isinstance(child, str) and isinstance(spawner, str):
        agg.setdefault("children", {})[child] = [spawner, kind]


def _fold_policy(agg: dict, row: dict) -> None:
    session, head = row.get("session_id"), row.get("instructions_head")
    if isinstance(session, str) and isinstance(head, str) and head:
        agg.setdefault("heads", {})[session] = head


_FOLDS = {"judge": _fold_judge, "spawn": _fold_spawn, "policy": _fold_policy}


def _advance_ledger(name: str, path: Path, saved: dict | None) -> dict:
    try:
        stat = path.stat()
    except OSError:
        return {}
    offset = _resume_offset(saved, stat)
    agg = saved.get("agg", {}) if offset and saved else {}
    fold = _FOLDS[name]
    if offset < stat.st_size:
        for row, end in iter_ledger_rows(path, offset):
            if row is not None:
                fold(agg, row)
            offset = end
    return {"dev": stat.st_dev, "ino": stat.st_ino, "offset": offset, "agg": agg}


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass
class Ledgers:
    judge_ms: dict = field(default_factory=dict)
    spawn_ms: dict = field(default_factory=dict)
    children: dict = field(default_factory=dict)
    heads: dict = field(default_factory=dict)


def refresh_ledgers(state_dir: Path | None, paths: dict) -> Ledgers:
    """Fold new ledger bytes into the stored aggregates; `paths` maps judge/spawn/policy to files."""
    cache_file = state_dir / "ledgers.json" if state_dir is not None else None
    stored = (_read_json(cache_file) if cache_file else None) or {}
    if stored.get("v") != WALKER_VERSION:
        stored = {}
    fresh: dict = {"v": WALKER_VERSION}
    for name, path in paths.items():
        saved = stored.get(name)
        if not isinstance(saved, dict) or saved.get("path") != str(path):
            saved = None
        entry = _advance_ledger(name, Path(path), saved)
        if entry:
            entry["path"] = str(path)
            fresh[name] = entry
    if cache_file is not None:
        _write_json(cache_file, fresh)
    agg = {name: fresh.get(name, {}).get("agg", {}) for name in _FOLDS}
    return Ledgers(
        judge_ms=agg["judge"].get("by_session", {}),
        spawn_ms=agg["spawn"].get("by_session", {}),
        children=agg["spawn"].get("children", {}),
        heads=agg["policy"].get("heads", {}),
    )


# ------------------------------------------------------------------ node dwell, core sha


def _transition_targets() -> dict:
    try:
        from agentctl.machine import TRANSITIONS
    except ImportError:
        return {}
    return {event: to for event, (_, to) in TRANSITIONS.items()}


def node_dwell(state_path: Path) -> dict | None:
    """Per-node dwell from consecutive history `ts` of one agentctl state file.

    Events carry no node, so the node is replayed through the engine's transition
    table. A gap with an untimed event inside it is dropped rather than guessed.
    """
    data = _read_json(state_path)
    history = data.get("history") if isinstance(data, dict) else None
    if not isinstance(history, list):
        return None
    targets = _transition_targets()
    node = "CLASSIFIED"
    dwell: dict = {}
    timed = untimed = 0
    prev_ts = prev_node = None
    gap_untimed = False
    for event in history:
        if not isinstance(event, dict):
            continue
        node = event["node"] if isinstance(event.get("node"), str) else targets.get(event.get("event"), node)
        ts = _epoch_or_iso_ms(event.get("ts"))
        if ts is None:
            untimed += 1
            gap_untimed = True
            continue
        timed += 1
        if prev_ts is not None and not gap_untimed and ts >= prev_ts:
            dwell[prev_node] = dwell.get(prev_node, 0) + ts - prev_ts
        prev_ts, prev_node, gap_untimed = ts, node, False
    return {"dwell": dwell, "timed": timed, "untimed": untimed}


def load_commit_index(repo_root: Path) -> list:
    """(commit epoch s, sha) newest first, along trunk; [] when `repo_root` is no repository.

    Trunk is origin/main, else main, else HEAD: run from a feature worktree, HEAD's own
    ancestry would stamp every later session with that branch's tip.
    """
    for ref in TRUNK_REFS:
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo_root), "rev-list", "--timestamp", ref],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if proc.returncode == 0:
            break
    else:
        return []
    index = []
    for line in proc.stdout.splitlines():
        stamp, _, sha = line.partition(" ")
        if stamp.isdigit() and sha:
            index.append((int(stamp), sha))
    return index


def _sha_at(index: list, ts_ms: int) -> str | None:
    cutoff = ts_ms // 1000
    for stamp, sha in index:
        if stamp <= cutoff:
            return sha
    return None


# ---------------------------------------------------------------------- session cache


class SessionCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _file(self, path: Path) -> Path:
        return self.root / (hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:40] + ".json")

    def get(self, path: Path) -> dict | None:
        entry = _read_json(self._file(path))
        return entry if isinstance(entry, dict) and entry.get("v") == WALKER_VERSION else None

    def put(self, path: Path, entry: dict) -> None:
        _write_json(self._file(path), {**entry, "v": WALKER_VERSION})


def _fingerprint(stat: os.stat_result) -> list:
    return [stat.st_mtime_ns, stat.st_size]


def _cache_fresh(entry: dict | None, fingerprint: list) -> bool:
    return entry is not None and entry.get("fp") == fingerprint


@dataclass
class Ctx:
    ledgers: Ledgers = field(default_factory=Ledgers)
    agentctl_state_dir: Path | None = None
    repo_root: Path = _REPO_ROOT
    commit_index: list | None = None
    cache: SessionCache | None = None
    reparsed: list = field(default_factory=list)

    def sha_at(self, ts_ms: int) -> str | None:
        if self.commit_index is None:
            self.commit_index = load_commit_index(self.repo_root)
        return _sha_at(self.commit_index, ts_ms)


def _transcript_part(path: Path, ctx: Ctx) -> dict:
    try:
        fingerprint = _fingerprint(path.stat())
    except OSError:
        fingerprint = [0, 0]
    entry = ctx.cache.get(path) if ctx.cache else None
    if _cache_fresh(entry, fingerprint):
        return entry
    ctx.reparsed.append(str(path))
    walked = _walk_transcript(path)
    if isinstance(walked, str):
        entry = {"fp": fingerprint, "skipped": walked}
    else:
        entry = {
            "fp": fingerprint, "skipped": None,
            "first_ts_ms": walked.first, "last_ts_ms": walked.last,
            "partition": walked.partition,
            "coverage": {"timed_events": walked.timed, "untimed_events": walked.untimed,
                         "exposed_hook_ms": walked.hook_ms},
        }
    if ctx.cache:
        ctx.cache.put(path, entry)
    return entry


def _engine_sessions(harness_session: str, state_dir: Path) -> list:
    """The harness session's own id, then every engine session it drove (the
    `engine-session-bindings` records beside the state dir): the two id spaces may differ."""
    ids = [harness_session]
    binding = state_dir.parent / engine_session_binding.BINDINGS_DIRNAME
    data = _read_json(binding / f"{config_root.sanitize_session_id(harness_session)}.json")
    driven = data.get("driven") if isinstance(data, dict) else None
    for entry in driven if isinstance(driven, list) else []:
        session = entry.get("session") if isinstance(entry, dict) else None
        if isinstance(session, str) and session not in ids:
            ids.append(session)
    return ids


def _node_part(session_id: str, ctx: Ctx) -> dict | None:
    if ctx.agentctl_state_dir is None:
        return None
    merged = None
    for engine_id in _engine_sessions(session_id, ctx.agentctl_state_dir):
        state_path = ctx.agentctl_state_dir / f"{config_root.sanitize_session_id(engine_id)}.json"
        part = node_dwell(state_path) if state_path.is_file() else None
        if part is None:
            continue
        if merged is None:
            merged = {"dwell": {}, "timed": 0, "untimed": 0}
        for name, ms in part["dwell"].items():
            merged["dwell"][name] = merged["dwell"].get(name, 0) + ms
        merged["timed"] += part["timed"]
        merged["untimed"] += part["untimed"]
    return merged


def profile_session(transcript_path, ctx: Ctx) -> "SessionProfile | Skipped":
    path = Path(transcript_path)
    session_id = path.stem
    part = _transcript_part(path, ctx)
    if part["skipped"]:
        return Skipped(session_id, str(path), part["skipped"])
    overlays: dict = {}
    coverage = dict(part["coverage"])
    node = _node_part(session_id, ctx)
    if node is not None:
        for name, ms in node["dwell"].items():
            overlays[f"node:{name}"] = ms
        coverage["node_timed_events"] = node["timed"]
        coverage["node_untimed_events"] = node["untimed"]
    for kind, ms in ctx.ledgers.spawn_ms.get(session_id, {}).items():
        overlays[f"spawn:{kind}"] = ms
    for name, ms in ctx.ledgers.judge_ms.get(session_id, {}).items():
        overlays[f"judge:{name}"] = ms
    child_of, kind = None, "main"
    if session_id in ctx.ledgers.children:
        child_of, kind = ctx.ledgers.children[session_id]
    core_sha = ctx.ledgers.heads.get(session_id) or ctx.sha_at(part["first_ts_ms"])
    return SessionProfile(
        session_id=session_id, transcript=str(path),
        first_ts_ms=part["first_ts_ms"], last_ts_ms=part["last_ts_ms"],
        partition=dict(part["partition"]), overlays=overlays, coverage=coverage,
        core_sha=core_sha, child_of=child_of, kind=kind,
    )


def default_state_dir() -> Path:
    override = os.environ.get("TIME_PROFILE_STATE_DIR")
    return Path(override).expanduser() if override else Path.home() / ".local" / "state" / "time-profile"


def profile_all(
    state_dir=None,
    projects_root=None,
    *,
    agentctl_state_dir=None,
    spawn_ledger=None,
    judge_ledger_path=None,
    policy_ledger=None,
    repo_root=None,
    commit_index=None,
) -> ProfileRun:
    """Profile every main-session transcript under `projects_root` (default: all roots)."""
    root = Path(state_dir).expanduser() if state_dir else default_state_dir()
    ledgers = refresh_ledgers(root, {
        "judge": Path(judge_ledger_path) if judge_ledger_path else judge_ledger.ledger_path(),
        "spawn": Path(spawn_ledger) if spawn_ledger else _SPAWN_LEDGER,
        "policy": Path(policy_ledger) if policy_ledger else _POLICY_LEDGER,
    })
    ctx = Ctx(
        ledgers=ledgers,
        agentctl_state_dir=Path(agentctl_state_dir) if agentctl_state_dir else config_root.agentctl_state_dir(),
        repo_root=Path(repo_root) if repo_root else _REPO_ROOT,
        commit_index=commit_index,
        cache=SessionCache(root / "sessions"),
    )
    paths = sorted(Path(projects_root).glob("*/*.jsonl")) if projects_root else config_root.iter_transcripts()
    run = ProfileRun()
    for path in paths:
        try:
            result = profile_session(path, ctx)
        except Exception:
            result = Skipped(Path(path).stem, str(path), "unparseable")
        (run.skipped if isinstance(result, Skipped) else run.profiles).append(result)
    run.reparsed = ctx.reparsed
    return run
