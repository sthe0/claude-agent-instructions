#!/usr/bin/env python3
"""Negative control for the published-text form pickers: blind a picker's handle, expect silence.

Difficulty removed: a picker test that no wrong implementation can turn red is decoration.
Each named blind (scripts/tests/form_fixtures.py::FORM_BLINDS) rebinds one writer_rules
attribute, in-process, ONE AT A TIME (restored before the next), and every picker it targets
is run on its own fixture unblinded first and blinded second, so a combined mutation never
stands in for a per-picker result.

  python3 scripts/tests/form_picker_control.py --level picker --blind NAME [--blind NAME ...]
  python3 scripts/tests/form_picker_control.py --level hook   --blind length

Levels:
  picker  the targeted picker function itself on its fixture (a blind on one picker is not
          masked by another picker firing on the same body).
  hook    _check_text_rules of hook-published-text-writer-gate.py on TELEGRAPHIC_BODY and
          ESTIMATE_BODY with a counting runner answering NO. Meaningful only for the blind
          that silences every picker (length). Killed = exactly one runner call unblinded,
          none blinded.

Exit codes:
  3  every target killed (unblinded the picker fires, blinded it is silent) - the only
     discriminating outcome
  0  at least one target survived (blinded it still fires)
  4  vacuous: a target is already silent unblinded (hook level: zero or several runner calls
     unblinded), so the control proves nothing
  5  setup error (import or attribute missing)
  2  usage (argparse)
Precedence when several targets disagree: 5 > 4 > 0 > 3. An uncaught exception exits 1.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
HOOK = SCRIPTS_DIR / "hook-published-text-writer-gate.py"
for _path in (str(SCRIPTS_DIR), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

KILLED, SURVIVED, VACUOUS, SETUP_ERROR = 3, 0, 4, 5
_PRECEDENCE = (SETUP_ERROR, VACUOUS, SURVIVED, KILLED)


@contextlib.contextmanager
def _rebound(module, attribute: str, replacement):
    original = getattr(module, attribute)
    setattr(module, attribute, replacement)
    try:
        yield
    finally:
        setattr(module, attribute, original)


def _verdict(unblinded: bool, blinded: bool) -> int:
    if not unblinded:
        return VACUOUS
    return SURVIVED if blinded else KILLED


def _picker_target(wr, picker: str, body: str, attribute: str, replacement) -> int:
    unblinded = bool(getattr(wr, picker)(body))
    with _rebound(wr, attribute, replacement):
        blinded = bool(getattr(wr, picker)(body))
    return _verdict(unblinded, blinded)


class _HookRig:
    """The hook's text-rules check with a counting judge runner and a private ledger."""

    def __init__(self, tmp: Path):
        from agentctl import advisor
        from lib import published_body

        spec = importlib.util.spec_from_file_location("hook_published_text_writer_gate", HOOK)
        self.hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.hook)
        self.advisor = advisor
        self.published_body = published_body
        self.tmp = tmp
        self.calls: list = []

    def writer_rules(self):
        held = getattr(self.hook, "writer_rules", None)
        return held if held is not None else importlib.import_module("lib.writer_rules")

    def calls_for(self, body: str) -> int:
        self.calls.clear()

        def runner(argv, *, timeout, stdin=""):
            self.calls.append(argv)
            return types.SimpleNamespace(returncode=0, stdout="NO\n", stderr="", timed_out=False)

        env = {
            "AGENTCTL_JUDGE_LEDGER": str(self.tmp / "ledger.jsonl"),
            self.published_body.ADVISORY_SINK_ENV: str(self.tmp / "advisories.jsonl"),
        }
        saved = {k: os.environ.get(k) for k in (*env, self.hook._TEXT_RULES_KILLSWITCH_ENV)}
        os.environ.pop(self.hook._TEXT_RULES_KILLSWITCH_ENV, None)
        os.environ.update(env)
        original_runner = self.advisor.subprocess_runner
        self.advisor.subprocess_runner = runner
        try:
            resolution = self.published_body.Resolution(kind=self.published_body.TEXT, body=body, shape=1)
            self.hook._check_text_rules(resolution, "cmd")
        finally:
            self.advisor.subprocess_runner = original_runner
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return len(self.calls)


def _hook_targets(rig: _HookRig, fixtures, attribute: str, replacement) -> list[int]:
    wr = rig.writer_rules()
    verdicts = []
    for body in (fixtures.TELEGRAPHIC_BODY, fixtures.ESTIMATE_BODY):
        unblinded = rig.calls_for(body)
        if unblinded != 1:
            verdicts.append(VACUOUS)
            continue
        with _rebound(wr, attribute, replacement):
            blinded = rig.calls_for(body)
        verdicts.append(SURVIVED if blinded else KILLED)
    return verdicts


def run(level: str, blinds: list[str], fixtures) -> int:
    try:
        from lib import writer_rules as wr
    except ImportError as exc:
        print(f"setup error: {exc}", file=sys.stderr)
        return SETUP_ERROR
    verdicts: list[int] = []
    try:
        if level == "picker":
            for name in blinds:
                attribute, replacement, pickers = fixtures.FORM_BLINDS[name]
                for picker in pickers:
                    body = getattr(fixtures, fixtures.PICKER_FIXTURE[picker])
                    verdict = _picker_target(wr, picker, body, attribute, replacement)
                    print(f"{name} -> {picker}: {verdict}")
                    verdicts.append(verdict)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                rig = _HookRig(Path(tmp))
                for name in blinds:
                    attribute, replacement, _ = fixtures.FORM_BLINDS[name]
                    got = _hook_targets(rig, fixtures, attribute, replacement)
                    print(f"{name} -> hook: {got}")
                    verdicts += got
    except (ImportError, AttributeError, KeyError) as exc:
        print(f"setup error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return SETUP_ERROR
    return next((code for code in _PRECEDENCE if code in verdicts), SURVIVED)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--level", choices=("picker", "hook"), required=True)
    parser.add_argument("--blind", action="append", required=True)
    args = parser.parse_args(argv)
    try:
        import form_fixtures as fixtures
    except ImportError as exc:
        print(f"setup error: {exc}", file=sys.stderr)
        return SETUP_ERROR
    unknown = [b for b in args.blind if b not in fixtures.FORM_BLINDS]
    if unknown:
        parser.error(f"unknown blind {unknown}; known: {sorted(fixtures.FORM_BLINDS)}")
    if args.level == "hook" and any(b != "length" for b in args.blind):
        parser.error("the hook level is meaningful only for the length blind")
    return run(args.level, args.blind, fixtures)


if __name__ == "__main__":
    sys.exit(main())
