#!/usr/bin/env python3
"""UserPromptSubmit hook: every advisory reminder that reads the user's prompt
(or the session's agentctl state), in one process.

Each reminder is a `check_*` function over a `Turn` returning stdout lines;
`main()` runs them in `CHECKS` order, prints what they return, and isolates
failures so one broken check never silences the others. Stdout becomes
additional context for the upcoming turn; exit 0 always — nothing here blocks.

Reminders:
  - context-growth: live context crossed a token band (once per band/session).
  - language: the prompt is mostly Cyrillic — replies must match the user's
    language (once per session).
  - self-improvement: the prompt carries a feedback signal (detection shared
    with the end-of-turn gate via `si_feedback_detect`).
  - tracker (+ mount-check): the prompt references a ticket or tracker; a
    ticket key whose dedicated task-mount differs from cwd's adds a nudge.
  - ticket-plan-sync: a tracker reference continues a session with an
    attached plan — verify the posted plan comment still matches it.
  - tracker-publish / experience-record: the agentctl plugins' mandatory
    phases are unrecorded (loudest at the RESOLUTION node).
  - instructions-refresh: once per session, OFFER a pull when the Core or
    project layer is behind, plus the deploy-integrity, worktree-fresh and
    relocate offers (only ever offers — never pulls, rebases or moves).

Silent in a judge child (`JUDGE_CHILD_ENV_VAR`): its prompt is not a real
user turn, and feedback-shaped example text in it must not fire anything.
Garbage on stdin is silent too. Every git or state failure is read as "nothing
to say".
"""
from __future__ import annotations

import datetime as dt
import functools
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import band_throttle, config_root  # noqa: E402
from lib.host_llm import JUDGE_CHILD_ENV_VAR  # noqa: E402
from si_feedback_detect import find_signals as find_feedback_signals  # noqa: E402
from si_feedback_detect import strip_injected_context  # noqa: E402


class Turn:
    """One UserPromptSubmit payload, with the agentctl session state read lazily."""

    def __init__(self, payload: dict):
        prompt = payload.get("prompt")
        self.prompt: str = prompt if isinstance(prompt, str) else ""
        session_id = payload.get("session_id")
        self.session_id: str = session_id if isinstance(session_id, str) else ""
        cwd = payload.get("cwd")
        self.cwd: str = cwd if isinstance(cwd, str) and cwd else os.getcwd()
        transcript_path = payload.get("transcript_path")
        self.transcript_path: str = transcript_path if isinstance(transcript_path, str) else ""
        # Once-per-session stamps are recorded here, after the lines are printed,
        # so a hook killed mid-run never spends a nudge it did not show.
        self.after_print: list[Callable[[], None]] = []

    @functools.cached_property
    def state(self) -> dict | None:
        if not self.session_id:
            return None
        path = config_root.resolve_agentctl_state_file(self.session_id)
        if path is None:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def plugin_bag(self, name: str) -> dict | None:
        if self.state is None:
            return None
        bag = (self.state.get("plugins") or {}).get(name)
        return bag if isinstance(bag, dict) else None

    @property
    def at_resolution_gate(self) -> bool:
        return bool(self.state) and self.state.get("node") == "RESOLUTION"


# ---------------------------------------------------------------------------
# context-growth
# ---------------------------------------------------------------------------

DEFAULT_BANDS = [120_000, 250_000]

# Stamps stay in /tmp, where the OS prunes them: a lost stamp only costs a
# repeated nudge, so the cheaper store is the right one.
STATE_ROOT = "/tmp"
STATE_PREFIX = "cc-context-nudge-"


def parse_bands() -> list[int]:
    raw = os.environ.get("CC_CONTEXT_NUDGE_BANDS", "")
    if not raw.strip():
        return DEFAULT_BANDS
    try:
        bands = sorted({int(x) for x in raw.split(",") if x.strip()})
        return bands or DEFAULT_BANDS
    except ValueError:
        return DEFAULT_BANDS


def live_context_tokens(transcript_path: str) -> int | None:
    """Latest assistant-turn usage = current context size, or None."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    last = None
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                usage = (obj.get("message") or {}).get("usage")
                if isinstance(usage, dict):
                    last = usage
    except OSError:
        return None
    if not last:
        return None
    return (
        int(last.get("input_tokens", 0) or 0)
        + int(last.get("cache_read_input_tokens", 0) or 0)
        + int(last.get("cache_creation_input_tokens", 0) or 0)
    )


def highest_band(tokens: int, bands: list[int]) -> int:
    """Index+1 of the highest band crossed; 0 if none."""
    crossed = 0
    for i, b in enumerate(bands):
        if tokens >= b:
            crossed = i + 1
    return crossed


def context_growth_message(tokens: int, band_idx: int, n_bands: int) -> str:
    k = round(tokens / 1000)
    base = (
        f"[context-growth] Context ~{k}k tokens. "
        "If this prompt starts a different task, suggest `/clear` to the user "
        "before continuing. If it continues the current task, delegate verbose "
        "exploration (multi-file reads, log diving, broad search) to a sub-agent "
        "so only the conclusion returns, and avoid re-reading large files "
        "(CLAUDE.md § Cost discipline)."
    )
    if band_idx >= n_bands:
        base += (
            " Context is now very large — per-turn cache_read cost is high; "
            "strongly prefer `/clear` or a summarize-and-restart over carrying on."
        )
    return base


def check_context_growth(turn: Turn) -> list[str]:
    bands = parse_bands()
    tokens = live_context_tokens(turn.transcript_path)
    if tokens is None:
        return []
    band = highest_band(tokens, bands)
    if band == 0:
        return []
    if band <= band_throttle.fired_band(turn.session_id, STATE_ROOT, STATE_PREFIX):
        return []
    band_throttle.record_band(turn.session_id, band, STATE_ROOT, STATE_PREFIX, prune=False)
    return [context_growth_message(tokens, band, len(bands))]


# ---------------------------------------------------------------------------
# language
# ---------------------------------------------------------------------------

CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
LATIN_RE = re.compile(r"[A-Za-z]")

MIN_CYRILLIC = 4      # at least this many Cyrillic letters
MIN_RATIO = 0.30      # Cyrillic as a share of all alphabetic chars

LANGUAGE_STAMP_PRUNE_AGE_S = 30 * 86400


def is_cyrillic_prompt(prompt: str) -> bool:
    cyr = len(CYRILLIC_RE.findall(prompt))
    if cyr < MIN_CYRILLIC:
        return False
    total = cyr + len(LATIN_RE.findall(prompt))
    return (cyr / total) >= MIN_RATIO


def _language_stamp_dir() -> Path:
    return Path.home() / ".local" / "state" / "claude-language-reminder"


def _language_stamp(session_key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_key)
    return _language_stamp_dir() / f"{safe}.stamp"


def _language_already_said(session_key: str) -> bool:
    return _language_stamp(session_key).exists()


def _record_language_said(session_key: str) -> None:
    """Fail-open: a lost stamp only costs a repeated reminder."""
    try:
        stamp = _language_stamp(session_key)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        cutoff = time.time() - LANGUAGE_STAMP_PRUNE_AGE_S
        for old in stamp.parent.glob("*.stamp"):
            if old.stat().st_mtime < cutoff:
                old.unlink()
    except OSError:
        pass


def check_language(turn: Turn) -> list[str]:
    if not is_cyrillic_prompt(turn.prompt):
        return []
    session_key = turn.session_id or dt.date.today().isoformat()
    if _language_already_said(session_key):
        return []
    turn.after_print.append(lambda: _record_language_said(session_key))
    return [
        "[language-reminder] The user is writing in Russian. Per CLAUDE.md § Instruction "
        "language: user-facing replies — including technical narratives, retrospectives, "
        "and every AskUserQuestion's question + option labels — must be in Russian. "
        "(Instruction-repo files stay English.)"
    ]


# ---------------------------------------------------------------------------
# self-improvement
# ---------------------------------------------------------------------------

def check_self_improvement(turn: Turn) -> list[str]:
    signals = find_feedback_signals(turn.prompt)
    if not signals:
        return []
    return [
        f"[self-improvement-reminder] User prompt contains a feedback signal — "
        f"{'; '.join(signals)}. Consider invoking the `self-improvement` skill "
        f"this turn if this is agent-behavior feedback."
    ]


# ---------------------------------------------------------------------------
# tracker (+ mount-check, ticket-plan-sync)
# ---------------------------------------------------------------------------

TICKET_KEY_RE = re.compile(r"\b[A-Z]{2,10}-\d+\b")

# Ticket-key-shaped strings that are not tracker keys: a few standards, and
# the prefixes of model names ("GPT-4").
FALSE_POSITIVES = {
    "COVID-19", "ISO-8601", "ISO-4217", "ISO-3166",
    "RFC-822", "RFC-2822", "RFC-5322", "RFC-7231",
    "UTF-8", "UTF-16", "UTF-32",
    "ECMA-262", "ECMA-402",
    "SHA-1", "SHA-256", "SHA-512", "MD-5",
    "HTTP-1", "HTTP-2", "HTTP-3",
}
MODEL_NAME_PREFIXES = {"GPT", "LLAMA", "GEMMA", "GEMINI", "MISTRAL", "BERT", "QWEN"}


def find_ticket_keys(prompt: str) -> list[str]:
    return sorted({
        key for key in TICKET_KEY_RE.findall(prompt)
        if key not in FALSE_POSITIVES and key.split("-", 1)[0] not in MODEL_NAME_PREFIXES
    })



def mount_root() -> str | None:
    """Task-mount root, or None when the org convention is absent.

    org-neutral: active only when `CLAUDE_TASK_MOUNT_ROOT` is set or
    `~/task-mounts` exists; otherwise the whole mount check is a no-op.
    """
    root = os.environ.get("CLAUDE_TASK_MOUNT_ROOT") or os.path.expanduser("~/task-mounts")
    return root if root and os.path.isdir(root) else None


def mount_mismatches(keys: list[str], cwd: str | None) -> list[tuple[str, str, list[str]]]:
    """Ticket keys whose dedicated mount exists but cwd is in another mount.

    Returns (key, current_mount_segment, matching_mount_dirs). Empty when
    the check does not apply: no root, cwd outside the root, already in
    the ticket's mount, or no dedicated mount for the ticket.
    """
    root = mount_root()
    if not root or not cwd:
        return []
    prefix = root.rstrip(os.sep) + os.sep
    if not cwd.startswith(prefix):
        return []
    current = cwd[len(prefix):].split(os.sep, 1)[0]
    if not current:
        return []
    try:  # shallow, name-only — no recursion into the (FUSE-backed) mounts, no stat storm
        dirs = [e.name for e in os.scandir(root) if e.is_dir()]
    except OSError:
        return []
    out: list[tuple[str, str, list[str]]] = []
    for key in keys:
        if current.startswith(key):
            continue  # already in this ticket's mount
        matches = sorted(d for d in dirs if d.startswith(key))
        if matches:
            out.append((key, current, matches))
    return out


def check_tracker(turn: Turn) -> list[str]:
    keys = find_ticket_keys(turn.prompt)
    if not keys:
        return []
    lines = [
        f"[tracker-reminder] User prompt references a tracker — ticket key(s): {', '.join(keys)}. "
        f"Invoke the `tracker-management` skill (layered on top of normal coordination)."
    ]
    for key, current, matches in mount_mismatches(keys, turn.cwd):
        lines.append(
            f"[mount-check] cwd is in mount '{current}', but {key} has dedicated "
            f"mount(s): {', '.join(matches)}. If this session will touch "
            f"ticket-local files/VCS, switch to the ticket mount "
            f"(`claude-task {key}`) or its own session — proceeding here risks "
            f"mixing task working-trees."
        )
    return lines


def check_ticket_plan_sync(turn: Turn) -> list[str]:
    if not find_ticket_keys(turn.prompt):
        return []
    plan_path = (turn.state or {}).get("plan_path")
    if not isinstance(plan_path, str) or not plan_path:
        return []
    return [
        "[ticket-plan-sync] This continues ticket work with an attached plan "
        f"({plan_path}). Before proceeding, run `python3 scripts/"
        "verify-ticket-plan-sync.py --plan "
        f"{plan_path} --comment-file <last-plan-comment>` against the ticket's "
        "last-posted plan comment. On DRIFT or NO-PLAN, re-post the plan "
        "(with a fresh --emit-marker marker) before continuing — a mismatch "
        "here is a plan actualization, not silent divergence."
    ]


# ---------------------------------------------------------------------------
# agentctl plugin phases: tracker-publish, experience-record
# ---------------------------------------------------------------------------

# Must mirror plugins_tracker.MANDATORY_PHASES. Duplicated (not imported) so the
# hook never imports the engine.
TRACKER_MANDATORY_PHASES = ("plan", "result", "status")


def unpublished_phases(bag: dict | None) -> list[str]:
    """Mandatory tracker phases not yet recorded as published. [] when the tracker
    plugin is inactive or every mandatory phase is recorded."""
    if bag is None:
        return []
    published = bag.get("published_phases") or {}
    return [p for p in TRACKER_MANDATORY_PHASES if p not in published]


def check_tracker_publish(turn: Turn) -> list[str]:
    missing = unpublished_phases(turn.plugin_bag("tracker"))
    if not missing:
        return []
    phases = ", ".join(missing)
    if turn.at_resolution_gate:
        return [
            "[tracker-publish-reminder] The session is at the resolution gate and "
            f"the tracker plugin still has unpublished mandatory phase(s): {phases}. "
            "`agentctl resolve` will be BLOCKED until each is posted to the ticket "
            "and recorded with `agentctl plugin-record --plugin tracker --phase <p>`. "
            "Publish the comment(s) first, then record, then resolve."
        ]
    return [
        "[tracker-publish-reminder] The tracker plugin is active with "
        f"unpublished mandatory phase(s): {phases}. Post the corresponding "
        "ticket comment at its phase boundary (plan before approval, result and "
        "the status transition at close) and record it via `agentctl "
        "plugin-record --plugin tracker --phase <p>` — the resolution gate "
        "enforces this before the task closes."
    ]


def missing_experience_phases(bag: dict) -> list[str]:
    """Phases not yet recorded. Empty list means the flow is complete."""
    phases = []
    if not bag.get("searched"):
        phases.append("searched")
    if not (bag.get("recorded") or bag.get("skipped")):
        phases.append("recorded|skipped")
    return phases


def check_experience_record(turn: Turn) -> list[str]:
    bag = turn.plugin_bag("experience")
    if bag is None:
        return []  # plugin inactive (non-substantive or not yet activated)
    missing = missing_experience_phases(bag)
    if not missing:
        return []
    phases = ", ".join(missing)
    if turn.at_resolution_gate:
        return [
            "[experience-record-reminder] The session is at the resolution gate and "
            f"the experience leaf flow is incomplete — missing phase(s): {phases}. "
            "`agentctl resolve` will be BLOCKED until `searched` AND "
            "(`recorded` OR `skipped`) are recorded. "
            "Run `record-experience.py search <keywords>` first, then either "
            "`agentctl plugin-record --plugin experience --phase recorded` or "
            "`agentctl plugin-record --plugin experience --phase skipped --note <reason>`."
        ]
    return [
        "[experience-record-reminder] The experience leaf flow has "
        f"incomplete phase(s): {phases}. Before the task closes, run "
        "`record-experience.py search <keywords>` to check for an existing "
        "leaf, then record or skip via `agentctl plugin-record --plugin "
        "experience --phase <searched|recorded|skipped>` (skip needs --note)."
    ]


# ---------------------------------------------------------------------------
# instructions-refresh (per-session OFFERs)
# ---------------------------------------------------------------------------

# Kept well under the hook's registered timeout: up to two network fetches
# (core + project) can run sequentially in the worst case.
FETCH_TIMEOUT_S = 4
GIT_TIMEOUT_S = 3

# The whole refresh check shares the hook's 10 s registration with the cheap
# reminders, so its git calls stop once this budget is spent.
REFRESH_BUDGET_S = 6
_refresh_deadline: float | None = None


def _refresh_stamp_path() -> Path:
    return Path.home() / ".local" / "state" / "claude-instructions-refresh.stamp"


def _refresh_due(session_key: str) -> bool:
    """Fire once per session_key. A stamp holding any other key — a prior
    session, a stale calendar-day key, or a legacy ISO datetime from before
    the per-session rekey — reads as due (fail-open)."""
    try:
        raw = _refresh_stamp_path().read_text(encoding="utf-8").strip()
    except OSError:
        return True
    return raw != session_key


def _record_refresh(session_key: str) -> None:
    try:
        stamp = _refresh_stamp_path()
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(session_key, encoding="utf-8")
    except OSError:
        pass


def _run(args: list[str], cwd: str, timeout: float) -> subprocess.CompletedProcess | None:
    if _refresh_deadline is not None:
        timeout = min(timeout, _refresh_deadline - time.monotonic())
        if timeout <= 0:
            return None
    try:
        return subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except Exception:
        return None


def _count_commits(root: Path, range_expr: str) -> int | None:
    proc = _run(["git", "-C", str(root), "rev-list", "--count", range_expr], str(root), GIT_TIMEOUT_S)
    if proc is None or proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def _core_root() -> Path:
    return Path(os.environ.get("CLAUDE_INSTRUCTIONS_REPO", str(Path.home() / "claude-agent-instructions")))


def _core_behind(core_root: Path) -> int | None:
    """Fail-open: None on any git failure (offline, missing repo, no origin/main)."""
    fetch = _run(["git", "-C", str(core_root), "fetch", "origin", "main", "-q"], str(core_root), FETCH_TIMEOUT_S)
    if fetch is None or fetch.returncode != 0:
        return None
    return _count_commits(core_root, "HEAD..origin/main")


def _project_root(cwd: str) -> Path | None:
    proc = _run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], cwd, GIT_TIMEOUT_S)
    if proc is None or proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return Path(out) if out else None


def _has_tracked_claude_dir(root: Path) -> bool:
    proc = _run(["git", "-C", str(root), "ls-files", ".claude"], str(root), GIT_TIMEOUT_S)
    return bool(proc) and proc.returncode == 0 and bool(proc.stdout.strip())


def _project_behind(root: Path) -> int | None:
    """Fail-open: None on any git failure (offline, no upstream configured)."""
    fetch = _run(["git", "-C", str(root), "fetch", "-q"], str(root), FETCH_TIMEOUT_S)
    if fetch is None or fetch.returncode != 0:
        return None
    return _count_commits(root, "HEAD..@{upstream}")


def check_layers(cwd: str) -> list[tuple[str, Path, int, str]]:
    """Return (label, root, behind_count, pull_cmd) for every layer behind its upstream."""
    behind_layers: list[tuple[str, Path, int, str]] = []

    core_root = _core_root()
    core_behind = _core_behind(core_root)
    if core_behind:
        behind_layers.append((
            "Core", core_root, core_behind,
            f"cd {core_root} && scripts/sync-instructions-repo.sh pull",
        ))

    project_root = _project_root(cwd)
    if project_root is not None and project_root.resolve() != core_root.resolve():
        if _has_tracked_claude_dir(project_root):
            project_behind = _project_behind(project_root)
            if project_behind:
                behind_layers.append((
                    "Project", project_root, project_behind,
                    f"git -C {project_root} pull --ff-only",
                ))

    return behind_layers


def _settings_path() -> Path:
    override = os.environ.get("CLAUDE_SETTINGS_PATH")
    if override:
        return Path(override).expanduser()
    return config_root.agent_home() / "settings.json"


def check_branch(core_root: Path, default: str = "main") -> str | None:
    """Warn when the Core checkout's HEAD is not on `default`. Fail-open: None
    on any git failure (missing repo, detached HEAD, timeout) or when already
    on `default` — no network involved, a single local `rev-parse`."""
    proc = _run(["git", "-C", str(core_root), "rev-parse", "--abbrev-ref", "HEAD"], str(core_root), GIT_TIMEOUT_S)
    if proc is None or proc.returncode != 0:
        return None
    branch = proc.stdout.strip()
    if not branch or branch == default:
        return None
    return (
        f"Core checkout ({core_root}) is on {branch}, not {default} — live hooks run "
        f"stale code; switch with `git -C {core_root} switch {default}`"
    )


def distinct_roots(settings_path: Path) -> list[str]:
    """Every distinct checkout root referenced by a settings.json hook command
    (the substring before '/scripts/'). Fail-open: [] on any error (missing
    file, bad JSON, unexpected shape)."""
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        roots: set[str] = set()
        for groups in data.get("hooks", {}).values():
            for group in groups:
                for entry in group.get("hooks", []):
                    command = entry.get("command", "")
                    marker = "/scripts/"
                    idx = command.find(marker)
                    if idx == -1:
                        continue
                    roots.add(command[:idx].strip("'\""))
        return sorted(roots)
    except Exception:
        return []


def check_canonical_roots(settings_path: Path, core_root: Path) -> str | None:
    """Warn when any settings.json hook command's checkout root differs from
    the CANONICAL Core checkout (`core_root`, resolved). Homogeneity is not
    the norm — every hook command could agree on one root and still be wrong
    if that root is a feature worktree rather than the canonical checkout.
    Fail-open: None when there are no hook-command roots at all, when
    `core_root` itself is unresolvable, or when every root resolves to it."""
    roots = distinct_roots(settings_path)
    if not roots:
        return None
    try:
        canonical = core_root.resolve()
    except OSError:
        return None
    offending = []
    for raw in roots:
        try:
            resolved = Path(raw).resolve()
        except OSError:
            continue
        if resolved != canonical:
            offending.append(raw)
    if not offending:
        return None
    return (
        f"settings.json hooks reference {len(offending)} non-canonical checkout "
        f"root(s) ({', '.join(sorted(offending))}) — canonical Core checkout is "
        f"{core_root} — point every hook command at the canonical checkout."
    )


def _git_info(cwd: str):
    """(toplevel, git_dir_abs, git_common_dir_abs, branch) for `cwd`, or None on
    any failure. Duplicated from hook-guard-canon-readonly.py's _git_info (not
    imported) so a bug in one hook can never wedge the other."""
    proc = _run(
        ["git", "-C", cwd, "rev-parse",
         "--show-toplevel", "--git-dir", "--git-common-dir", "--abbrev-ref", "HEAD"],
        cwd, GIT_TIMEOUT_S,
    )
    if proc is None or proc.returncode != 0:
        return None
    lines = proc.stdout.splitlines()
    if len(lines) < 4:
        return None
    toplevel, git_dir, git_common_dir, branch = lines[0], lines[1], lines[2], lines[3]
    git_dir_abs = os.path.realpath(os.path.join(cwd, git_dir))
    git_common_abs = os.path.realpath(os.path.join(cwd, git_common_dir))
    return os.path.realpath(toplevel), git_dir_abs, git_common_abs, branch


def check_worktree_fresh(cwd: str) -> str | None:
    """D3: OFFER a rebase onto origin/main when cwd is a LINKED git worktree
    that is behind. Fetches origin/main first and only THEN counts commits —
    counting against a stale local ref could read 0-behind forever and the
    OFFER would never fire. Fail-open: None for a non-git cwd, the PRIMARY
    checkout (git_dir == git_common_dir; out of scope here), any git failure,
    or an up-to-date worktree."""
    info = _git_info(cwd)
    if info is None:
        return None
    toplevel, git_dir_abs, git_common_abs, _branch = info
    if git_dir_abs == git_common_abs:
        return None  # primary checkout, not a linked worktree

    fetch = _run(["git", "-C", toplevel, "fetch", "origin", "main", "-q"], toplevel, FETCH_TIMEOUT_S)
    if fetch is None or fetch.returncode != 0:
        return None
    behind = _count_commits(Path(toplevel), "HEAD..origin/main")
    if not behind:
        return None
    return (
        f"[worktree-fresh] this linked worktree ({toplevel}) is {behind} commit(s) behind "
        f"origin/main — OFFER the user a rebase via AskUserQuestion: "
        f"`git -C {toplevel} rebase origin/main` (never automatic)."
    )


def _read_canon_roots() -> list[str]:
    """Non-empty, non-comment lines of the canon-roots file, or [] on any error
    — fail-open. Duplicated from hook-guard-canon-readonly.py."""
    try:
        path = config_root.canon_roots_file()
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def _under_canon_root(target: str) -> bool:
    """True iff realpath(target) is, or is a descendant of, the realpath of any
    registered canon-roots entry."""
    target_real = os.path.realpath(target)
    for root in _read_canon_roots():
        try:
            root_real = os.path.realpath(root)
        except Exception:
            continue
        if target_real == root_real or target_real.startswith(root_real + os.sep):
            return True
    return False


def check_in_canon(cwd: str) -> str | None:
    """D2/R4: OFFER relocation when cwd is itself inside a canon — the PRIMARY
    Core checkout (any branch) or a path under a registered canon-roots entry
    — so a running session picks this up on its very next move, not only when
    hook-guard-canon-readonly.py blocks an actual edit. Fail-open: None on any
    git failure, a linked worktree, a second mount, or a plain non-canon dir."""
    in_primary = False
    info = _git_info(cwd)
    if info is not None:
        toplevel, git_dir_abs, git_common_abs, _branch = info
        if toplevel == os.path.realpath(str(_core_root())) and git_dir_abs == git_common_abs:
            in_primary = True

    if not in_primary and not _under_canon_root(cwd):
        return None
    return (
        f"[relocate] {cwd} is inside a canon (read-only from a live session) — do feature "
        f"work in an isolated copy instead: `scripts/session-isolate.sh <task-name>` (a linked "
        f"git worktree, or a second mount for other VCS backends)."
    )


def build_nudge(layers: list[tuple[str, Path, int, str]]) -> str:
    parts = [
        f"{label} ({root}) is {count} commit(s) behind — pull with `{cmd}`"
        for label, root, count, cmd in layers
    ]
    return (
        "[instructions-refresh] " + "; ".join(parts) + ". "
        "OFFER this refresh to the user via AskUserQuestion before running it — "
        "an explicit opt-in pull, never automatic."
    )


def check_instructions_refresh(turn: Turn) -> list[str]:
    session_key = turn.session_id or dt.date.today().isoformat()
    if not _refresh_due(session_key):
        return []
    turn.after_print.append(lambda: _record_refresh(session_key))

    global _refresh_deadline
    _refresh_deadline = time.monotonic() + REFRESH_BUDGET_S
    try:
        return _refresh_lines(turn)
    finally:
        _refresh_deadline = None


def _refresh_lines(turn: Turn) -> list[str]:
    lines: list[str] = []
    layers = check_layers(turn.cwd)
    if layers:
        lines.append(build_nudge(layers))

    deploy_warnings: list[str] = []
    branch_warning = check_branch(_core_root())
    if branch_warning:
        deploy_warnings.append(branch_warning)
    canonical_root_warning = check_canonical_roots(_settings_path(), _core_root())
    if canonical_root_warning:
        deploy_warnings.append(canonical_root_warning)
    if deploy_warnings:
        lines.append("[instructions-deploy] " + " ".join(deploy_warnings))

    worktree_offer = check_worktree_fresh(turn.cwd)
    if worktree_offer:
        lines.append(worktree_offer)

    relocate_offer = check_in_canon(turn.cwd)
    if relocate_offer:
        lines.append(relocate_offer)
    return lines


CHECKS = (
    check_context_growth,
    check_language,
    check_self_improvement,
    check_tracker,
    check_ticket_plan_sync,
    check_tracker_publish,
    check_experience_record,
    check_instructions_refresh,
)


def _spawned_child() -> bool:
    try:
        return int(os.environ.get("AGENT_RECURSION_DEPTH") or 0) >= 1
    except ValueError:
        return False


def _only_injected_context(prompt: str) -> bool:
    """A prompt with text, all of it harness-injected (a task-notification turn)."""
    return bool(prompt.strip()) and not strip_injected_context(prompt).strip()


def main() -> int:
    if os.environ.get(JUDGE_CHILD_ENV_VAR):
        return 0  # a sandboxed judge subprocess, not a real user turn — allow, no opinion
    if _spawned_child():
        return 0  # a specialist's prompt is the parent's brief, not a user turn
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    turn = Turn(payload)
    if _only_injected_context(turn.prompt):
        return 0
    for check in CHECKS:
        try:
            lines = check(turn)
        except Exception:
            turn.after_print.clear()
            continue
        for line in lines:
            print(line)
        sys.stdout.flush()
        for record in turn.after_print:
            try:
                record()
            except Exception:
                pass
        turn.after_print.clear()
    return 0


from lib import hook_telemetry  # noqa: E402


if __name__ == "__main__":
    sys.exit(hook_telemetry.run(main, mechanism="hook-prompt-reminders"))
