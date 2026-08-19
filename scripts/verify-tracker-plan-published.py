#!/usr/bin/env python3
"""Tracker-agnostic gate: is the APPROVED plan durably published on its ticket?

Difficulty (functional ground):
  verify-ticket-plan-sync.py answers "does a given comment's marker match the
  current plan" but never fetches anything itself — nothing upstream of it
  confirms a durable, backend-independent artifact (not just a comment that
  can be edited or deleted) actually carries the approved plan's bytes. This
  script closes that gap: it resolves the project's tracker backend, asks it
  for the marker comment and the durable artifact's digest (via the two read
  verbs `tracker_plan_marker` and `tracker_plan_artifact_digest` from
  registry.sh's provider contract), and reduces both answers to a single
  five-status verdict — reusing verify-ticket-plan-sync.py's own hash/marker
  definitions rather than re-deriving them.

Evaluation order (VERB-PRECEDENCE):
  1. The backend FILE must resolve (by NAME, then by file) or nothing further
     can run: UNVERIFIABLE/backend-unresolved.
  2. Both verbs undeclared: UNVERIFIABLE/verb-undeclared — no evidence at all.
  3. Any DECLARED verb that refutes (or fails to execute) decides the status —
     a refutation always outranks a sibling verb's non-declaration. When BOTH
     declared verbs refute, `tracker_plan_marker`'s refutation decides (the
     marker is the more direct evidence of what was actually posted).
  4. No refutation, but one verb is undeclared: that verb's own fail-closed
     status, reason=verb-undeclared.
  5. Both declared and both confirm: OK.

Exit 3 (UNVERIFIABLE) is reserved EXCLUSIVELY for steps 1-2 above — "no
evidence obtainable at all" — never for an ordinary drift/absence a declared
verb reported, and never for a backend that could not be sourced, a verb that
timed out, or any return code a verb chose for itself. Non-declaration is
therefore decided by a PROBE run separate from the run that INVOKES the verb,
and the probe must prove itself POSITIVELY: it prints a distinct token
(`__DECLARED__` / `__UNDECLARED__`) IN ADDITION TO its own sentinel return
code (0 / 97), and BOTH must agree before the verdict is trusted. That closes
the one route rc-alone left open — a source-time guard that calls `exit 97`
(rather than `return 1`) before the `declare -F` check ever runs terminates
the probe's shell with rc 97 but prints no token, so it now reads
"unavailable" (verb-failed, exit 1), never "undeclared" (exit 3). A verb
invocation itself never runs inside the probe, so no return code a verb
chooses for itself — 97 or 98 included — can ever forge the rc or the token,
and a source-time failure (a credentials guard aborting before the
`function` definitions) degrades fail-closed to verb-failed at exit 1 instead
of buying a free skip at exit 3.

Output contract:
  The bare status word on STDOUT (one line), and a single machine-parseable
  line on STDERR:
    STATUS=<word> REASON=<slug> BACKEND=<realpath|-> BACKEND_NAME=<name|->
    BACKEND_SOURCE=<flag|env|record|unset> VERB=<verb> KEY=<key>
    PLAN_SHA=<sha> ARTIFACT=<basename>
  Every VALUE is percent-encoded ('%', '=', whitespace and non-ASCII), so for
  ANY input the line splits on single spaces into exactly nine KEY=VALUE
  fields, each carrying exactly one '=' — a caller-supplied --key or plan
  basename can neither break the line nor inject a second field, and a byte
  that was never valid UTF-8 to begin with (an argv surrogate, a backend's
  mangled output) round-trips as its literal %XX escape instead of crashing
  the gate (encode_field encodes with errors="surrogateescape"). Decode a
  value with urllib.parse.unquote(value, errors="replace") — the one byte
  that still cannot be reassembled into UTF-8 becomes U+FFFD, never an
  exception. REASON is drawn from a closed set: backend-unresolved,
  verb-undeclared, verb-failed, absent, mismatch, ok.

  Any stderr text a verb's own seam produced (a credentials CLI's error
  message, a verb's diagnostic output) is surfaced ABOVE the contract line as
  separate "<verb>: <line>" lines — never merged into it — so a backend that
  writes its own "STATUS=..." text to stderr can never be mistaken for the
  one contract line.

Modes:
  --plan PATH --key KEY [--backend NAME]
                               Evaluate; print the status word on stdout and
                               the STATUS= line on stderr. Exit 0 for
                               OK, 1 for DRIFT/NO-PLAN/NO-ARTIFACT, 3 for
                               UNVERIFIABLE, 2 on a usage error (unreadable
                               plan file).
  --selftest                   Exercise all five statuses, both exit-3
                               sub-cases and three fail-closed seam routes
                               (a return-based source failure, a verb
                               returning the probe's sentinel rc, an
                               exit-based source-time guard that emits no
                               probe token) against fabricated backend .sh
                               files in a temp dir. Exit 0 iff all pass.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.plugin_dir import resolve_plugin_dir  # noqa: E402
from project_entry import projects  # noqa: E402

# Hyphenated sibling filename -> load by path (matches agent-stats.py's
# reuse of cost-report.py). Its plan_file_sha256/format_marker/extract_marker
# are the ONE canonical definitions; this module imports, never reimplements.
_PLAN_SYNC_SPEC = importlib.util.spec_from_file_location(
    "verify_ticket_plan_sync", SCRIPTS_DIR / "verify-ticket-plan-sync.py"
)
plan_sync = importlib.util.module_from_spec(_PLAN_SYNC_SPEC)
_PLAN_SYNC_SPEC.loader.exec_module(plan_sync)

MARKER_VERB = "tracker_plan_marker"
DIGEST_VERB = "tracker_plan_artifact_digest"

# registry.sh's contract normalizes every verb's failure into ONE degrade
# class (any nonzero = unavailable). These two sentinels belong to the PROBE
# run only (probe_verb), which never executes verb code — so no return code a
# verb chooses for itself can collide with them.
PROBE_UNDECLARED_RC = 97
PROBE_SOURCE_FAILED_RC = 98

# A tracker CLI stalled on a dead TCP connection must not hang the gate (and
# whatever hook calls it) forever: a timeout degrades to the same fail-closed
# verb-failed path as any other verb failure. Applies per bash run — a call
# site budgeting worst-case wall-clock should count four of them (probe +
# invoke, for each of the two verbs).
VERB_TIMEOUT_S = 60

HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# Percent-encoding safe set for a contract-line VALUE: every printable ASCII
# character except '%', '=' and space (quote() additionally never encodes
# A-Za-z0-9_.-~). Whitespace, '=' and non-ASCII are therefore always encoded.
FIELD_SAFE_CHARS = "!\"#$&'()*+,-./:;<>?@[\\]^`{|}~"

EXIT_BY_STATUS = {
    "OK": 0,
    "DRIFT": 1,
    "NO-PLAN": 1,
    "NO-ARTIFACT": 1,
    "UNVERIFIABLE": 3,
}


@dataclass(frozen=True)
class Outcome:
    kind: str  # "confirm" | "refute" | "undeclared"
    status: "str | None" = None
    reason: "str | None" = None


@dataclass(frozen=True)
class VerbRun:
    """One verb's trip through the backend seam.

    state "declared" carries the verb's own rc/stdout; "undeclared" means the
    probe proved the verb absent; "unavailable" means the seam itself failed
    (backend not sourceable, bash unusable, verb timed out) — which is a
    fail-closed verb failure, NOT an absence of evidence.
    """

    state: str  # "declared" | "undeclared" | "unavailable"
    rc: int = 0
    stdout: str = ""
    stderr: str = ""


def resolve_backend_name(
    explicit: "str | None",
    getenv: "Callable[[str], str | None]",
    project_lookup: "Callable[[str], str | None]",
    pwd: str,
) -> "tuple[str | None, str]":
    """The 3-rung + unset backend-NAME ladder: --backend flag > $CLAUDE_TRACKER_BACKEND
    > the project record's tracker_backend field > unset."""
    if explicit:
        return explicit, "flag"
    env_value = getenv("CLAUDE_TRACKER_BACKEND")
    if env_value:
        return env_value, "env"
    record_value = project_lookup(pwd)
    if record_value:
        return record_value, "record"
    return None, "unset"


def project_tracker_backend(pwd: str) -> "str | None":
    """Default project_lookup: the resolved project record's tracker_backend."""
    records = projects._load_default()
    rec = projects.resolve(records, pwd=pwd)
    return rec.get("tracker_backend") if rec else None


def resolve_backend_file(name: "str | None", scripts_dir: Path, plugin_dir: Path) -> "Path | None":
    """Built-in trackers/<name>.sh first, then <plugin_dir>/trackers/<name>.sh."""
    if not name:
        return None
    builtin = scripts_dir / "project_entry" / "trackers" / f"{name}.sh"
    if builtin.is_file():
        return builtin
    plugin = plugin_dir / "trackers" / f"{name}.sh"
    if plugin.is_file():
        return plugin
    return None


def build_env(base_env: dict, script_dir: Path) -> dict:
    """The parent env verbatim, plus CLAUDE_ENTER_TASK_DIR defaulted to this
    script's directory when the parent does not already set it."""
    env = dict(base_env)
    env.setdefault("CLAUDE_ENTER_TASK_DIR", str(script_dir))
    return env


def probe_verb(backend_file: str, verb: str, env: dict) -> "tuple[str, str]":
    """Does <backend_file> DECLARE <verb>? Returns (state, stderr) where state is
    "declared" / "undeclared" / "unavailable", running no verb code at all — which
    is what makes the answer trustworthy: the probe's own sentinel rc (0 / 97)
    must AGREE with a matching positive token (__DECLARED__ / __UNDECLARED__)
    before a verdict is trusted, so a source-time guard that terminates the shell
    via `exit 97` (rather than `return 1`) before ever reaching the `declare -F`
    check prints no token and reads "unavailable", never "undeclared". A backend
    that will not source at all (a credentials guard returning before its
    definitions) is likewise "unavailable", never "undeclared"."""
    script = (
        'backend="$1"; verb="$2"; '
        f'source "$backend" || exit {PROBE_SOURCE_FAILED_RC}; '
        'declare -F "$verb" >/dev/null 2>&1 '
        '&& { printf "__DECLARED__\\n"; exit 0; }; '
        f'printf "__UNDECLARED__\\n"; exit {PROBE_UNDECLARED_RC}'
    )
    try:
        proc = subprocess.run(
            ["bash", "-c", script, "verify-tracker-plan-published", backend_file, verb],
            capture_output=True, text=True, env=env, timeout=VERB_TIMEOUT_S, errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError):
        return "unavailable", ""
    stdout = (proc.stdout or "").strip()
    if proc.returncode == 0 and stdout == "__DECLARED__":
        return "declared", proc.stderr
    if proc.returncode == PROBE_UNDECLARED_RC and stdout == "__UNDECLARED__":
        return "undeclared", proc.stderr
    return "unavailable", proc.stderr


def call_verb(backend_file: str, verb: str, args: "list[str]", env: dict) -> VerbRun:
    """Probe first, then invoke in a SEPARATE bash run, so the verb's own return
    code — 97 and 98 included — stays inside its one degrade class."""
    state, probe_stderr = probe_verb(backend_file, verb, env)
    if state != "declared":
        return VerbRun(state, stderr=probe_stderr)
    script = 'backend="$1"; verb="$2"; shift 2; source "$backend"; "$verb" "$@"'
    try:
        proc = subprocess.run(
            ["bash", "-c", script, "verify-tracker-plan-published", backend_file, verb, *args],
            capture_output=True, text=True, env=env, timeout=VERB_TIMEOUT_S, errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError):
        return VerbRun("unavailable")
    return VerbRun("declared", proc.returncode, proc.stdout, proc.stderr)


def parse_marker(run: VerbRun, extract_marker_fn: Callable) -> "dict | None":
    """The ONE place the marker verb's stdout is parsed; its result feeds both the
    marker classification and the artifact basename."""
    if run.state != "declared" or run.rc != 0:
        return None
    return extract_marker_fn(run.stdout)


def classify_marker(run: VerbRun, marker: "dict | None", plan_sha: str) -> Outcome:
    if run.state == "undeclared":
        return Outcome("undeclared")
    if run.state == "unavailable" or run.rc != 0:
        return Outcome("refute", "NO-PLAN", "verb-failed")
    if marker is None:
        return Outcome("refute", "NO-PLAN", "absent")
    if marker["plan_sha256"] != plan_sha:
        return Outcome("refute", "DRIFT", "mismatch")
    return Outcome("confirm")


def classify_digest(run: VerbRun, plan_sha: str) -> Outcome:
    if run.state == "undeclared":
        return Outcome("undeclared")
    if run.state == "unavailable" or run.rc != 0:
        return Outcome("refute", "NO-ARTIFACT", "verb-failed")
    digest = (run.stdout or "").strip()
    if not HEX64_RE.match(digest):
        return Outcome("refute", "NO-ARTIFACT", "absent")
    if digest != plan_sha:
        return Outcome("refute", "NO-ARTIFACT", "mismatch")
    return Outcome("confirm")


def artifact_basename(marker: "dict | None", fallback_basename: str) -> str:
    """The artifact basename comes from the marker's `plan=` field when a marker was
    actually found; else falls back to --plan's own basename."""
    if marker and marker.get("plan"):
        return marker["plan"]
    return fallback_basename


def decide(marker: Outcome, digest: Outcome) -> "tuple[str, str, str]":
    """VERB-PRECEDENCE reduction of the two verb outcomes to (status, reason, verb)."""
    if marker.kind == "undeclared" and digest.kind == "undeclared":
        return "UNVERIFIABLE", "verb-undeclared", f"{MARKER_VERB},{DIGEST_VERB}"

    refuting = []
    if marker.kind == "refute":
        refuting.append((MARKER_VERB, marker))
    if digest.kind == "refute":
        refuting.append((DIGEST_VERB, digest))
    if refuting:
        # marker appended first above, so when both refute, marker decides (N6).
        verb, outcome = refuting[0]
        return outcome.status, outcome.reason, verb

    if marker.kind == "undeclared":
        return "NO-PLAN", "verb-undeclared", MARKER_VERB
    if digest.kind == "undeclared":
        return "NO-ARTIFACT", "verb-undeclared", DIGEST_VERB
    return "OK", "ok", "-"


def evaluate(
    plan_sha: str, key: str, backend_file: "Path | None", env: dict, fallback_basename: str,
    diagnostics: "list[tuple[str, str]] | None" = None,
) -> "tuple[str, str, str, str]":
    """Run both verbs against backend_file (or short-circuit when it's None) and
    return (status, reason, verb, artifact_basename). When `diagnostics` is given a
    list, append (verb, stderr) for every verb run whose seam produced non-empty
    stderr — evaluate() does no I/O itself; the caller decides whether/how to
    surface it (must-fix 3)."""
    if backend_file is None:
        return "UNVERIFIABLE", "backend-unresolved", "-", fallback_basename

    marker_run = call_verb(str(backend_file), MARKER_VERB, [key], env)
    _note_diagnostic(diagnostics, MARKER_VERB, marker_run)
    marker = parse_marker(marker_run, plan_sync.extract_marker)
    marker_outcome = classify_marker(marker_run, marker, plan_sha)
    basename = artifact_basename(marker, fallback_basename)

    digest_run = call_verb(str(backend_file), DIGEST_VERB, [key, basename], env)
    _note_diagnostic(diagnostics, DIGEST_VERB, digest_run)
    digest_outcome = classify_digest(digest_run, plan_sha)

    status, reason, verb = decide(marker_outcome, digest_outcome)
    return status, reason, verb, basename


def _note_diagnostic(diagnostics: "list[tuple[str, str]] | None", verb: str, run: VerbRun) -> None:
    """Record a verb's own seam stderr (a credentials CLI's error message, a verb's
    diagnostic output) so a caller can surface it — a verb that never even got
    probed successfully still deserves its explanation on the record."""
    if diagnostics is not None and run.stderr.strip():
        diagnostics.append((verb, run.stderr))


STATUS_LINE_KEYS = (
    "STATUS", "REASON", "BACKEND", "BACKEND_NAME", "BACKEND_SOURCE",
    "VERB", "KEY", "PLAN_SHA", "ARTIFACT",
)


def encode_field(value: str) -> str:
    """Percent-encode one contract-line VALUE. `--key 'ABC-123 STATUS=OK'` must not
    be able to split the line or forge a second field, so '%', '=' and every
    whitespace/non-ASCII character are escaped; ordinary keys, hex digests and
    paths pass through unchanged. errors="surrogateescape" so a byte that never
    was valid UTF-8 (an argv surrogate from a caller's --key) round-trips as its
    literal %XX escape instead of raising UnicodeEncodeError."""
    return quote(str(value), safe=FIELD_SAFE_CHARS, errors="surrogateescape")


def _format_status_line(
    status: str, reason: str, backend: str, backend_name: str, backend_source: str,
    verb: str, key: str, plan_sha: str, artifact: str,
) -> str:
    values = (status, reason, backend, backend_name, backend_source, verb, key, plan_sha, artifact)
    return " ".join(f"{k}={encode_field(v)}" for k, v in zip(STATUS_LINE_KEYS, values))


def _selftest() -> bool:
    ok_all = True
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        plan_path = tmp_path / "plan.toml"
        plan_path.write_text("[meta]\ntask_id = \"t\"\n", encoding="utf-8")
        plan_sha = plan_sync.plan_file_sha256(str(plan_path))
        fallback_basename = plan_path.name
        key = "ABC-123"
        env = build_env(os.environ, SCRIPTS_DIR)
        marker_line = plan_sync.format_marker(str(plan_path), plan_sha)

        counter = {"n": 0}

        def backend(body: str) -> Path:
            counter["n"] += 1
            f = tmp_path / f"backend-{counter['n']}.sh"
            f.write_text(body, encoding="utf-8")
            return f

        def check(label: str, backend_file: "Path | None", expect_status: str, expect_reason: str) -> None:
            nonlocal ok_all
            status, reason, _verb, _artifact = evaluate(plan_sha, key, backend_file, env, fallback_basename)
            print(f"selftest {label} case: {status}/{reason}")
            ok_all = ok_all and status == expect_status and reason == expect_reason

        # OK: both verbs declared and confirm.
        check(
            "OK",
            backend(
                f"tracker_plan_marker() {{ printf '%s\\n' '{marker_line}'; }}\n"
                f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{plan_sha}'; }}\n"
            ),
            "OK", "ok",
        )

        # DRIFT: marker present but names a stale hash; digest still confirms.
        stale_sha = "0" * 64 if plan_sha != "0" * 64 else "1" * 64
        stale_marker_line = plan_sync.format_marker(str(plan_path), stale_sha)
        check(
            "DRIFT",
            backend(
                f"tracker_plan_marker() {{ printf '%s\\n' '{stale_marker_line}'; }}\n"
                f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{plan_sha}'; }}\n"
            ),
            "DRIFT", "mismatch",
        )

        # NO-PLAN: marker verb declared but prints nothing; digest confirms.
        check(
            "NO-PLAN",
            backend(
                "tracker_plan_marker() { :; }\n"
                f"tracker_plan_artifact_digest() {{ printf '%s\\n' '{plan_sha}'; }}\n"
            ),
            "NO-PLAN", "absent",
        )

        # NO-ARTIFACT: marker confirms; digest verb declared but fails.
        check(
            "NO-ARTIFACT",
            backend(
                f"tracker_plan_marker() {{ printf '%s\\n' '{marker_line}'; }}\n"
                "tracker_plan_artifact_digest() { return 1; }\n"
            ),
            "NO-ARTIFACT", "verb-failed",
        )

        # UNVERIFIABLE / backend-unresolved: no backend file at all.
        check("UNVERIFIABLE(backend-unresolved)", None, "UNVERIFIABLE", "backend-unresolved")

        # UNVERIFIABLE / verb-undeclared: backend file exists but declares neither verb.
        check(
            "UNVERIFIABLE(verb-undeclared)",
            backend("# no verbs declared\n"),
            "UNVERIFIABLE", "verb-undeclared",
        )

        # A credentials guard aborting at SOURCE time leaves both verbs
        # undeclarable — that is a verb failure (exit 1), not "no evidence".
        check(
            "source-failure",
            backend(
                '[[ -z "${SELFTEST_FAKE_CREDENTIALS:-}" ]] && { echo "no credentials" >&2; return 1; }\n'
                "tracker_plan_marker() { :; }\n"
                "tracker_plan_artifact_digest() { :; }\n"
            ),
            "NO-PLAN", "verb-failed",
        )

        # A verb returning the probe's own sentinel rc is still just a failing
        # declared verb (exit 1), never an undeclared one.
        check(
            "verb-returns-probe-sentinel",
            backend(
                f"tracker_plan_marker() {{ return {PROBE_UNDECLARED_RC}; }}\n"
                f"tracker_plan_artifact_digest() {{ return {PROBE_UNDECLARED_RC}; }}\n"
            ),
            "NO-PLAN", "verb-failed",
        )

        # A source-time guard that terminates the shell via `exit 97` (not
        # `return 1`) before the `declare -F` check ever runs must not forge the
        # probe's own UNDECLARED sentinel by rc alone — no token, no undeclared
        # verdict, so this is a verb failure (exit 1), never exit 3.
        check(
            "source-time-exit-97-guard",
            backend(
                f"exit {PROBE_UNDECLARED_RC}\n"
                "tracker_plan_marker() { :; }\n"
                "tracker_plan_artifact_digest() { :; }\n"
            ),
            "NO-PLAN", "verb-failed",
        )

    return ok_all


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a ticket's tracker backend carries a durable artifact and marker "
            "comment matching the current TOML plan (tracker-agnostic; resolves the "
            "backend and calls its tracker_plan_marker / tracker_plan_artifact_digest "
            "verbs)."
        )
    )
    parser.add_argument("--plan", help="path to the TOML plan file")
    parser.add_argument("--key", help="the ticket key")
    parser.add_argument("--backend", help="explicit tracker backend name (overrides env/record)")
    parser.add_argument("--selftest", action="store_true", help="run the 5-status self-test and exit")
    args = parser.parse_args(argv)

    if args.selftest:
        return 0 if _selftest() else 1

    if not args.plan or not args.key:
        parser.error("--plan and --key are required (unless --selftest)")

    try:
        plan_sha = plan_sync.plan_file_sha256(args.plan)
    except OSError as exc:
        print(f"verify-tracker-plan-published: cannot read plan {args.plan!r}: {exc}", file=sys.stderr)
        return 2

    name, source = resolve_backend_name(args.backend, os.environ.get, project_tracker_backend, os.getcwd())
    plugin_dir = resolve_plugin_dir("CLAUDE_PROJECT_PLUGIN_DIR", "project-entry-plugins")
    backend_file = resolve_backend_file(name, SCRIPTS_DIR, plugin_dir)
    fallback_basename = Path(args.plan).name
    env = build_env(os.environ, SCRIPTS_DIR)

    diagnostics: "list[tuple[str, str]]" = []
    status, reason, verb, artifact = evaluate(
        plan_sha, args.key, backend_file, env, fallback_basename, diagnostics
    )
    backend_field = str(backend_file.resolve()) if backend_file is not None else "-"

    # A verb's own stderr (a credentials CLI's error message, a diagnostic line)
    # is surfaced ABOVE the contract line, one prefixed line per source line, so
    # it can never be mistaken for the one STATUS= line stage 3 parses.
    for source_verb, stderr_text in diagnostics:
        for line in stderr_text.splitlines():
            print(f"{source_verb}: {line}", file=sys.stderr)

    # Two streams, two audiences: the bare status word on stdout for a shell
    # caller (`$(...)` without having to parse the contract line), the full
    # machine-readable line on stderr for the gate and the journal.
    print(status)
    print(
        _format_status_line(
            status, reason, backend_field, name or "-", source, verb, args.key, plan_sha, artifact
        ),
        file=sys.stderr,
    )
    return EXIT_BY_STATUS[status]


if __name__ == "__main__":
    sys.exit(main())
