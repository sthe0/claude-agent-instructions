#!/usr/bin/env python3
"""Mutation controls for the resource-grants work: run a stage's real verify on a wrong copy.

Difficulty removed: a negative control that only asserts "this test file would fail" proves
nothing about the stage's full verify, and a hand-written control per stage drifts from it.
This tool applies one wrong version of one production line (an exact textual substitution)
to a scratch copy of the repository and then runs ANY command there, so the control is the
stage's own verify on a mutated tree and can pass only by a real kill.

  python3 scripts/tests/resource_grant_mutation_control.py --list
  python3 scripts/tests/resource_grant_mutation_control.py --control [--run CMD]
  python3 scripts/tests/resource_grant_mutation_control.py --mutant NAME [--run CMD]

The scratch copy is a `git clone --local --quiet` of this repository at HEAD with the
uncommitted changes (modified, staged, deleted AND untracked files) copied over, so it holds
what the working tree holds. Nothing is written to the source tree.

CMD is ONE argv command split with shlex and run without a shell: `&&`, `;`, pipes and `$VAR`
are not interpreted. Wrap a compound check in a script and pass the script.

Exit codes:
  --mutant NAME --run CMD   CMD is run twice: first on the unmutated copy, which must exit 0
                            (otherwise nothing is mutated and the tool exits 0, since a red
                            run on the mutant would prove nothing), then on the mutated copy,
                            whose exit code is the tool's. 1 means CMD failed (a kill for a
                            verify command); 0 means CMD passed on the wrong code (survived).
                            pytest's 2/4/5 (collection error, usage, nothing collected) pass
                            through unchanged, so they never read as a kill.
                            EXCEPT: exit 0 when the anchor does not match exactly once, the
                            copy fails, the unmutated run is not green, or this tool itself
                            raises -- a broken control must never read as a kill (a crash
                            under --control is 7, never green).
  --mutant NAME             0 when the mutant applies; no command is run.
  --control [--run CMD]     no mutation. Without --run: 0 when every anchor of the catalogue
                            applies exactly once in the copy, 7 otherwise. With --run: CMD's
                            exit code on the unmutated copy (green is 0).
  --list                    0.

While CMD runs, RESOURCE_GRANT_ACTIVE_MUTANT=<name> is exported; the catalogue test, whose
anchor tests would fail inside a mutated copy, skips those tests when it sees it.
"""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_MUTANT_ENV = "RESOURCE_GRANT_ACTIVE_MUTANT"
CONTROL_RED = 7

PLAN_RESOURCES = "scripts/agentctl/plan_resources.py"
NORM_HANDOFF = "scripts/agentctl/norm_handoff.py"
PLAN = "scripts/agentctl/plan.py"
SPAWN = "scripts/spawn-specialist.py"
GRANTS = "scripts/agentctl/grants.py"
CLI = "scripts/agentctl/cli.py"
SEGMENT_RESOLVER = "scripts/agentctl/segment_resolver.py"
ADVISOR = "scripts/agentctl/advisor.py"
CODE_EFFECTS = "scripts/agentctl/code_effects.py"
HOOK = "scripts/hook-resource-grant-check.py"
GIT_SURFACE = "scripts/agentctl/git_surface.py"


@dataclass(frozen=True)
class Mutant:
    relpath: str
    anchor: str
    replacement: str
    description: str


CATALOGUE: "dict[str, Mutant]" = {
    "handoff-resources-carried": Mutant(
        NORM_HANDOFF,
        '"resources": list(resources or []),',
        '"resources": [],',
        "the handoff record drops the stage's typed resources",
    ),
    "workdir-write-baseline": Mutant(
        SPAWN,
        'baseline = agentctl_resources.FileResource(workdir, "write")',
        'baseline = agentctl_resources.FileResource(workdir, "read")',
        "the acceptEdits workdir baseline is read, not write",
    ),
    "workdir-baseline-needs-accept-edits": Mutant(
        SPAWN,
        'return permission_mode == "acceptEdits" and not write_grant_forces_accept_edits(kind, engine_grants)',
        "return not write_grant_forces_accept_edits(kind, engine_grants)",
        "the workdir baseline no longer depends on the resolved permission mode",
    ),
    "wildcard-kept-as-pattern": Mutant(
        PLAN_RESOURCES,
        'elif res.reason_class == "wildcard-tail":\n                out.rule_patterns.append(entry["rule"])',
        'elif res.reason_class == "wildcard-tail":\n                pass',
        "a wildcard-tail Bash rule is lost instead of kept as a rule pattern",
    ),
    "digest-check-detects-change": Mutant(
        PLAN,
        ") + order_place(doc.meta) + optional_stages_place(doc))",
        ") + order_place(doc.meta) + optional_stages_place(doc) + ('mutant',))",
        "the plan content digest moves; digest_neutrality_check.py must exit 1",
    ),
    "digest-check-detects-premise-key-change": Mutant(
        PLAN,
        "payload = repr((element, value))",
        "payload = repr((element, value, 'mutant'))",
        "only a plan-level premise key moves; digest_neutrality_check.py must exit 1",
    ),
    "push-own-branch-only": Mutant(
        GRANTS,
        'resources_.append(_resources.VcsRefResource(BRANCH_REMOTE, branch, "push"))',
        'resources_.append(_resources.VcsRefResource(BRANCH_REMOTE, "main", "push"))',
        "the derived push names trunk instead of the plan-declared branch",
    ),
    "push-trunk-refused": Mutant(
        GRANTS,
        '    if (folded in _TRUNK_BRANCHES or folded.startswith(("release-", "release/"))\n'
        "            or (head is not None and folded == head.casefold())):",
        "    if False:",
        "trunk, release-* and the remote HEAD branch are no longer refused",
    ),
    "ref-form-trunk-refused": Mutant(
        GRANTS,
        "    if not _is_plain_branch_name(branch, venue):",
        "    if False:",
        "HEAD and ref-path spellings of trunk (refs/heads/main, origin/main) derive a push",
    ),
    "branch-frozen-at-approve": Mutant(
        CLI,
        "    if stored is None:\n"
        "        return []\n"
        "    if entries_grants_sha256(stored) != state.approved_grants_sha256:\n"
        "        return []\n"
        '    frozen = (stored.get(str(stage_index)) or {}).get("branch")',
        "    if True:\n"
        "        return None\n"
        '    frozen = (stored.get(str(stage_index)) or {}).get("branch")',
        "spawn and stage-grants re-derive DR-P from the repository as it is now",
    ),
    "runtime-merge-carries-resources": Mutant(
        CLI,
        "        allow=allow, add_dirs=add_dirs, resources=stage_branch_resources(state, stage_index))\n\n\n"
        "def _effective_stage_grants_for_self_grant",
        "        allow=allow, add_dirs=add_dirs)\n\n\ndef _effective_stage_grants_for_self_grant",
        "the dispatch coverage drops the stage's DR-P resources",
    ),
    "self-grant-merge-carries-resources": Mutant(
        CLI,
        "        allow=allow, add_dirs=add_dirs, resources=stage_branch_resources(state, stage_index))\n\n\n"
        "def _consume_once_grants",
        "        allow=allow, add_dirs=add_dirs)\n\n\ndef _consume_once_grants",
        "the agent self-grant coverage drops the stage's DR-P resources",
    ),
    "self-grant-counts-resources": Mutant(
        CLI,
        "    approved.extend(coverage.resources)\n",
        "",
        "an own-branch push the plan approved becomes a question to the user",
    ),
    "uncovered-grants-dr-p-loop": Mutant(
        SPAWN,
        "    for want in _stage_branch_resources(state, stage_index):\n",
        "    for want in []:\n",
        "a child may be handed a branch push its parent never held",
    ),
    "refusal-resources-materialized": Mutant(
        CLI,
        "            for resource in _refusal_vcs_resources(req.raw):\n",
        "            for resource in []:\n",
        "the user's answer to a resource-only refusal grants nothing",
    ),
    "granted-resources-cover": Mutant(
        SPAWN,
        '            covering_resources.append(agentctl_resources.resource_from_dict(entry["resource"]))\n',
        "            pass\n",
        "a user-granted resource does not count towards the delegation's coverage",
    ),
    "dropped-surfaced": Mutant(
        CLI,
        '    if dropped:\n        d.data["dropped_grants"] = dropped',
        '    if False:\n        d.data["dropped_grants"] = dropped',
        "submit-plan no longer reports the dropped grants",
    ),
    "branch-only-plan-counts-as-granting": Mutant(
        PLAN,
        "    return any(not g.is_empty() for g in _effective_by_stage(doc, venue=venue, head=head).values())",
        "    return any(g.allow or g.add_dirs for g in _effective_by_stage(doc, venue=venue, head=head).values())",
        "a plan whose only grant is the own-branch push is treated as granting nothing",
    ),
    "resources-slot-empty": Mutant(
        GRANTS,
        "StageGrants(allow=allow, add_dirs=add_dirs, resources=branch_resources), dropped",
        "StageGrants(allow=allow, add_dirs=add_dirs, resources=[]), dropped",
        "the typed-resources slot of every stage's grants stays empty: no consumer sees DR-P",
    ),
    "boundary-view-sees-branch": Mutant(
        "scripts/agentctl/plan_resources.py",
        '        add(effective.resources if branch_resources is None\n'
        '            else branch_resources.get(stage.index, []), "branch", stage.branch or "")\n',
        "",
        "the autonomy boundary cannot see a replan that moves the stage's branch",
    ),
    "handoff-held-sees-vcs-resources": Mutant(
        CLI,
        "    held: list = handoff_vcs_resources(parent_link)\n",
        "    held: list = []\n",
        "a child's self-approval compares against a parent that holds no push",
    ),
    "branch-change-is-grant-growth": Mutant(
        PLAN,
        "        if new_eff[stage.index].effective_resources() - old_g.effective_resources():\n"
        "            return True",
        "        if False:\n"
        "            return True",
        "a replan that moves the stage's branch counts as a self-approvable refinement",
    ),
    "branch-resources-hashed": Mutant(
        PLAN,
        '    payload += _branch_hash_part({\n'
        '        idx: list((s.get("branch") or {}).get("resources") or []) for idx, s in entries.items()})\n',
        "",
        "the frozen DR-P resources fall out of the stored entries' hash",
    ),
    "refined-branch-limited-to-admitted": Mutant(
        PLAN,
        '        "resources": [d for d in now["branch"]["resources"] if tuple(sorted(d.items())) in admitted],',
        '        "resources": list(now["branch"]["resources"]),',
        "a refinement hands the child whatever the new plan derives, admitted or not",
    ),
    "refined-branch-frozen-only": Mutant(
        PLAN,
        '    admitted = {tuple(sorted(d.items())) for d in frozen.get("resources") or []}\n    return {',
        '    admitted = {tuple(sorted(d.items())) for d in frozen.get("resources") or []}\n'
        '    admitted |= {tuple(sorted(d.items())) for d in now["branch"]["resources"]}\n    return {',
        "a refinement re-admits what a live read of origin/HEAD or the refs now derives",
    ),
    "git-C-venue": Mutant(
        SEGMENT_RESOLVER,
        '    c_value = global_values.get("-C")\n',
        '    c_value = global_values.get("-C")\n'
        '    if c_value is not None:\n'
        '        return _unres("contract-unresolved", "git -C is outside the venue")\n',
        "segment mode refuses `git -C <dir>` again, as the all-or-nothing resolver does",
    ),
    "git-commit-resource": Mutant(
        SEGMENT_RESOLVER,
        '        out.append(resources.VcsRefResource(".", branch, "commit"))\n',
        '        pass\n',
        "a git commit/merge no longer lists the commit on the current branch",
    ),
    "cd-after-semicolon-unresolved": Mutant(
        SEGMENT_RESOLVER,
        '            cwd = None if self.moved else self.out\n',
        '            cwd = self.out\n',
        "a segment after `;`/`||` is resolved against the cwd an earlier cd may not have reached",
    ),
    "redirect-write": Mutant(
        SEGMENT_RESOLVER,
        '            out.append(_file_write(target_path))\n',
        '            pass\n',
        "a redirect target is no longer listed as a write",
    ),
    "semicolon-not-a-separator": Mutant(
        SEGMENT_RESOLVER,
        '            elif c in ";&|<>()":\n',
        '            elif c in "&|<>()":\n',
        "`;` stops being a separator, so the segments on both sides run together",
    ),
    "git-allowlist-widened": Mutant(
        SEGMENT_RESOLVER,
        '    "reset": GitForm(frozenset({"-q", "--quiet", "--mixed"})),\n',
        '    "reset": GitForm(frozenset({"-q", "--quiet", "--mixed"})),\n'
        '    "stash": GitForm(frozenset()),\n',
        "the git write allow-list gains `stash`, a ref-moving write nobody named",
    ),
    "git-commitish-reset-resolved": Mutant(
        SEGMENT_RESOLVER,
        '        if not (head_only if seen_dd else (head_only or before[0] == "HEAD")):\n',
        '        if False:\n',
        "`git reset <commit-ish>` resolves although it moves the current branch ref",
    ),
    "git-foreign-repo-resolved": Mutant(
        SEGMENT_RESOLVER,
        '    if ctx.venue_common is None or _common_dir(root) != ctx.venue_common:\n',
        '    if False:\n',
        "git in a repository other than the venue's resolves against that repository's refs",
    ),
    "branch-hazard-ignored": Mutant(
        SEGMENT_RESOLVER,
        '        if any(others):\n',
        '        if False:\n',
        "a commit is attributed to the current branch although another segment may switch it",
    ),
    "or-cd-known": Mutant(
        SEGMENT_RESOLVER,
        '        conditional = in_pipeline or self.or_seen or op_before == "&" or next_op == "&"\n',
        '        conditional = in_pipeline or op_before == "&" or next_op == "&"\n',
        "a cd reached through `||` is taken as having run",
    ),
    "unresolved-keeps-cwd": Mutant(
        SEGMENT_RESOLVER,
        '            self.out = None\n            self.moved = True\n            return\n',
        '            self.out = cwd\n            return\n',
        "an unreadable segment is assumed not to have moved the working directory",
    ),
    "opaque-without-known-cwd": Mutant(
        SEGMENT_RESOLVER,
        '    if cwd is None:\n        return _unres(_OUTSIDE_LIST, _CWD_UNCERTAIN)\n'
        '    if widening_targets.INTERPRETER_RE.match(prog):',
        '    if widening_targets.INTERPRETER_RE.match(prog):',
        "an interpreter segment is called opaque although its working directory is unknown",
    ),
    "ansi-c-quote-skipped": Mutant(
        SEGMENT_RESOLVER,
        '            raise _ScanError("ANSI-C quoting $\'...\' is not parsed: its escapes decide where the quote ends")\n',
        '            self.i += 1\n',
        "`$'..'` is read as a plain single-quoted string, so an escaped quote ends it early",
    ),
    "heredoc-substitution-ignored": Mutant(
        SEGMENT_RESOLVER,
        '            if not delimiter_quoted and ("$(" in body or "`" in body):\n',
        '            if False:\n',
        "a command substitution in an unquoted heredoc body is treated as inert text",
    ),
    "cd-symlink-tracked-physically": Mutant(
        SEGMENT_RESOLVER,
        '    if real != os.path.normpath(path):\n        return None\n',
        '    if False:\n        return None\n',
        "`cd link` is tracked as the link's target, so a later `..` is read from the wrong directory",
    ),
    "push-ref-unnormalised": Mutant(
        SEGMENT_RESOLVER,
        '            ref = dst[len("refs/heads/") :]\n',
        '            ref = dst\n',
        "a push to `refs/heads/<b>` or `HEAD` is listed under that spelling, not as the branch it names",
    ),
    "none-effect-nonliteral-operand": Mutant(
        SEGMENT_RESOLVER,
        '                    return _unres(_NONLITERAL, f"{prog!r} operand {tok.text!r} is non-literal: it may expand into a flag outside the safe set")\n',
        '                    continue\n',
        "an unreadable operand of a program with a closed flag set is assumed to be harmless",
    ),
    "branch-exists-not-required": Mutant(
        SEGMENT_RESOLVER,
        'not seen_dd and not _local_branch_exists(root, before[0]):\n',
        'not seen_dd and False:\n',
        "`git checkout <name>` resolves although only a remote-tracking branch exists and git would create one",
    ),
    "packed-refs-ignored": Mutant(
        SEGMENT_RESOLVER,
        '            return any(line.rstrip("\\n").endswith(f" refs/heads/{name}") for line in fh)\n',
        '            return False\n',
        "a local branch stored only in packed-refs is taken as missing",
    ),
    "backgrounded-list-ignored": Mutant(
        SEGMENT_RESOLVER,
        ' or _list_is_backgrounded(raws, k)\n',
        '\n',
        "a commit is attributed to the current branch although the list it is in runs in the background beside a checkout",
    ),
    "awk-nonliteral-program-read": Mutant(
        SEGMENT_RESOLVER,
        '    if any(t.nonlit for t in toks[1:]):\n        return _unres(_NONLITERAL, "an awk operand',
        '    if False:\n        return _unres(_NONLITERAL, "an awk operand',
        "an awk program held in a variable or positional is read as the quoted text and called harmless",
    ),
    "push-head-destination-mapped": Mutant(
        SEGMENT_RESOLVER,
        '        if not ref or ref == "HEAD":\n',
        '        if not ref:\n',
        "`git push origin <b>:HEAD` is listed as a push of `<b>` although git creates a remote ref named HEAD",
    ),
    "mkdir-p-leaf-only": Mutant(
        SEGMENT_RESOLVER,
        '        paths = tops\n',
        '        pass\n',
        "`mkdir -p a/b/c` lists only the leaf, not the topmost directory it creates",
    ),
    "nonliteral-code-opaque": Mutant(
        SEGMENT_RESOLVER,
        '        if invocation.token is None or invocation.token.nonlit:\n            return _unres(_NONLITERAL, "the python -c code',
        '        if invocation.token is None:\n            return _unres(_NONLITERAL, "the python -c code',
        "`python3 -c \"$CODE\"` is labelled opaque, so a later reader goes looking for the code in the wrong text",
    ),
    "script-operand-nonliteral": Mutant(
        SEGMENT_RESOLVER,
        '    if toks[1 + i].nonlit:\n',
        '    if False:\n',
        "an interpreter whose script operand is a variable is labelled a script-file",
    ),
    "combined-eval-flag-missed": Mutant(
        SEGMENT_RESOLVER,
        '    return any(ch in _EVAL_LETTERS.get(prog, "") for ch in flag[1:])\n',
        '    return len(flag) == 2 and flag[1] in _EVAL_LETTERS.get(prog, "")\n',
        "`perl -ne` and `node -pe` are not seen as inline code, only the bare `-e`",
    ),
    "stdin-body-without-body": Mutant(
        SEGMENT_RESOLVER,
        '    if not has_body:\n',
        '    if False:\n',
        "`echo x | python3` and `python3 < s.py` are labelled stdin-body although the code is not in the segment",
    ),
    "cdpath-search-ignored": Mutant(
        SEGMENT_RESOLVER,
        '    if (ctx.cdpath_unverified or os.environ.get("CDPATH")) and not _CDPATH_SAFE_RE.match(operands[0].text):\n',
        '    if False:\n',
        "`cd sub` is read as the working-directory entry although CDPATH may land it elsewhere",
    ),
    "push-source-namespace-ignored": Mutant(
        SEGMENT_RESOLVER,
        '        branch = _push_source_branch(root, src)\n',
        '        branch = _push_source_branch(root, src) or src\n',
        "`git push origin v1:topic` is listed as a push of branch `topic` although git creates `refs/tags/topic`",
    ),
    "push-source-tag-not-checked": Mutant(
        SEGMENT_RESOLVER,
        '    if _ref_exists(common, f"refs/tags/{src}") or _ref_exists(common, f"refs/{src}"):\n',
        '    if False:\n',
        "a name that is both a branch and a tag is taken as the branch, although git prefers the tag",
    ),
    "push-bare-destination-assumed-branch": Mutant(
        SEGMENT_RESOLVER,
        '        elif dst.startswith("refs/heads/"):\n            ref = dst[len("refs/heads/") :]\n',
        '        elif True:\n            ref = dst.removeprefix("refs/heads/")\n',
        "`git push origin b:other` is listed as a push of branch `other` although git updates an existing remote `refs/remotes/other` first",
    ),
    "multiple-stdin-bodies-resolved": Mutant(
        SEGMENT_RESOLVER,
        '    elif _stdin_body_count(raw) > 1:\n',
        '    elif False:\n',
        "`python3 <<A <<B` is labelled stdin-body although the segment text carries a body bash never reads",
    ),
    "push-config-mapping-ignored": Mutant(
        SEGMENT_RESOLVER,
        '            elif variable not in _REMOTE_VARIABLES_ALLOWED:\n',
        '            elif False:\n',
        "`git push origin topic` is listed as a push of `topic` although `remote.origin.push` (or `mirror`, ...) changes what is sent",
    ),
    "push-default-upstream-ignored": Mutant(
        SEGMENT_RESOLVER,
        "    if colonless_branch_source and default is not None and default not in _PUSH_DEFAULT_SAFE:\n",
        "    if False:\n",
        "`git push origin topic` is listed as a push of `topic` although `push.default=upstream` sends it to the branch's upstream",
    ),
    "push-config-unreadable-resolved": Mutant(
        SEGMENT_RESOLVER,
        '    if entries is None:\n        return "cannot read the git config"\n',
        '    if entries is None:\n        entries = [("remote.%s.url" % remote, "x")]\n',
        "a push is listed as resolved although the git config could not be read, so a mapping may exist",
    ),
    "push-config-key-allowlist-widened": Mutant(
        SEGMENT_RESOLVER,
        '            elif rest not in _PUSH_VARIABLES_ALLOWED:\n',
        '            elif False:\n',
        "`git push origin topic` is listed as a push of `topic` although `push.followTags` (or any other push.* key) also pushes tag refs",
    ),
    "push-remote-url-not-required": Mutant(
        SEGMENT_RESOLVER,
        "    if not has_url:\n",
        "    if False:\n",
        "`git push legacy topic` is listed as resolved although the remote comes from a legacy `.git/remotes` file or is a URL operand, whose push mapping is not read",
    ),
    "push-submodule-recurse-ignored": Mutant(
        SEGMENT_RESOLVER,
        '        elif section == "submodule" and rest == "recurse" and (value or "").lower() not in _BOOL_FALSE:\n',
        '        elif False:\n',
        "`git push origin topic` is listed as a push of `topic` although `submodule.recurse` makes it push the submodules too",
    ),
    "stdin-body-count-fd0-only": Mutant(
        SEGMENT_RESOLVER,
        '    return sum(1 for red in raw.redirects if red.op in _HEREDOC_OPS or red.op == "<<<")\n',
        '    return sum(1 for red in raw.redirects if (red.fd == "0" or red.fd is None) and (red.op in _HEREDOC_OPS or red.op == "<<<"))\n',
        "`python3 3<<B <<A` is labelled stdin-body although the first body in the text is the one on fd 3",
    ),
    "stdin-first-redirect-decides": Mutant(
        SEGMENT_RESOLVER,
        '            last = red\n',
        '            last = last or red\n',
        "`python3 <<'E' < s.py` is labelled stdin-body although the later redirection feeds the script file",
    ),
    "refusal-class-swapped": Mutant(
        SEGMENT_RESOLVER,
        '_NONLITERAL = "residual-syntax"\n',
        '_NONLITERAL = "contract-unresolved"\n',
        "a non-literal operand is reported under the class of an unreviewed form",
    ),
    "judge-fail-closed": Mutant(
        ADVISOR,
        'verdict, malformed = _ce_undeterminable("the code-effects judge timed out"), False',
        'verdict, malformed = CodeEffectsVerdict((), "the code-effects judge timed out", True), False',
        "a code-effects judge that times out answers with an empty effect list instead of UNDETERMINABLE",
    ),
    "judge-schema-validated": Mutant(
        ADVISOR,
        "    if set(obj) != _CE_ANSWER_KEYS:\n",
        "    if False:\n",
        "a code-effects answer with an extra field passes the schema check",
    ),
    "judge-cache-key": Mutant(
        CODE_EFFECTS,
        'advisor.CODE_EFFECTS_PROMPT_SHA256, advisor.CODE_EFFECTS_MODEL, venue, branch or "", code_sha256]',
        'advisor.CODE_EFFECTS_PROMPT_SHA256, code_sha256]',
        "the code-effects cache key forgets the venue, the branch and the model the verdict was made for",
    ),
    "module-allowlist-widened": Mutant(
        CODE_EFFECTS,
        "    if top not in _IMPORTABLE:\n",
        "    if False:\n",
        "a local or third-party module (or operator, pickle, importlib ...) is imported although its text is never shown to the judge",
    ),
    "stdlib-shadow-ignored": Mutant(
        CODE_EFFECTS,
        "            shadow = _shadowing_entry(search_dir, imported, script)\n",
        "            shadow = None\n",
        "a local os.py / json.py / __pycache__ entry that would be imported instead of the library module is not noticed",
    ),
    "star-import-allowed": Mutant(
        CODE_EFFECTS,
        '                if alias.name == "*":\n',
        "                if False:\n",
        "`from pkg import *` is accepted, so the names bound (and the submodules run) are hidden from the judge",
    ),
    "private-attribute-allowed": Mutant(
        CODE_EFFECTS,
        '            if node.attr.startswith("_") and node.attr not in _DUNDER_SAFE:\n',
        "            if False:\n",
        "`os._exit` / `argparse._sys` style private attributes, which reach modules the judge cannot see, are accepted",
    ),
    "dunder-allowed": Mutant(
        CODE_EFFECTS,
        '    return name.startswith("__") and name.endswith("__") and name not in _DUNDER_SAFE\n',
        "    return False\n",
        "`__builtins__` and the other dunder names are accepted, so run-time lookups are hidden from the judge",
    ),
    "encoding-cookie-ignored": Mutant(
        CODE_EFFECTS,
        "    if encoding not in _SOURCE_ENCODINGS:\n",
        "    if False:\n",
        "a PEP 263 cookie for utf-7 or latin-1 is ignored, so the judge reads a comment where the interpreter runs code",
    ),
    "sibling-write-ignored": Mutant(
        CODE_EFFECTS,
        "            hit = write_hit(path, collected)\n",
        "            hit = None\n",
        "a sibling segment that rewrites the script or plants a stdlib-named file is not noticed: the judge reads the old text",
    ),
    "stdlib-named-write-ignored": Mutant(
        CODE_EFFECTS,
        "    if collected.imports:\n",
        "    if False:\n",
        "a write that creates os.py / json.py / __pycache__ in the search directory is not noticed by code that imports",
    ),
    "own-write-to-source-ignored": Mutant(
        CODE_EFFECTS,
        "            own = write_hit(resource.path, collected)\n",
        "            own = None\n",
        "the code's own judge-named write to its script or to a stdlib-named file in its search directory is accepted",
    ),
    "sibling-unresolved-ignored": Mutant(
        CODE_EFFECTS,
        '        if other.status != "resolved":\n',
        "        if False:\n",
        "an unresolved or opaque sibling segment is assumed to write nothing",
    ),
    "branch-switch-ignored": Mutant(
        CODE_EFFECTS,
        "        if _may_switch_branch(root, segment, line_segments):\n",
        "        if False:\n",
        "a commit is attributed to the branch the judge was told although a sibling segment may switch it first",
    ),
    "nonfile-effect-rule-dropped": Mutant(
        ADVISOR,
        '    "- has ANY effect other than a file write or a git ref commit/push: a network request that can change remote "\n',
        '    "- uses the network: a network request that can change remote "\n',
        "the judge is no longer told that any effect other than a file write or a git ref commit/push is undeterminable",
    ),
    "interpreter-flag-allowlist-widened": Mutant(
        CODE_EFFECTS,
        '_SAFE_FLAG_LETTERS = frozenset("uBq")',
        '_SAFE_FLAG_LETTERS = frozenset("uBqIPESsxidvbWX")',
        "-I -P -E -S -s and the other options that move the import path or the code are read as harmless",
    ),
    "optimize-flag-allowed": Mutant(
        CODE_EFFECTS,
        '_SAFE_FLAG_LETTERS = frozenset("uBq")',
        '_SAFE_FLAG_LETTERS = frozenset("uBOq")',
        "-O is read as harmless although it makes __debug__ False and the flags are in neither the prompt nor the cache key",
    ),
    "path-env-ignored": Mutant(
        CODE_EFFECTS,
        "        if os.environ.get(name):\n",
        "        if False:\n",
        "a PYTHONPATH in the hook's environment is not noticed, so imports are read from the wrong directory",
    ),
    "sys-path-allowed": Mutant(
        CODE_EFFECTS,
        "                if node.attr not in _SYS_SAFE:\n",
        "                if False:\n",
        "code that edits sys.path or sys.modules is read as if the import path were unchanged",
    ),
    "script-dir-not-search-path": Mutant(
        CODE_EFFECTS,
        "            search_dir = script.parent\n",
        "            search_dir = cwd\n",
        "a script's imports are looked for in the working directory instead of the script's directory",
    ),
    "tilde-path-accepted": Mutant(
        ADVISOR,
        "            if path is None or path.startswith(_CE_FORBIDDEN_PATH_PREFIX):\n",
        "            if path is None:\n",
        "a judge-reported ~/x path is accepted and later expanded to the home directory",
    ),
    "validation-not-total": Mutant(
        ADVISOR,
        "        if not isinstance(kind, str) or not isinstance(op, str):\n",
        "        if False:\n",
        "a list-valued kind or op in an answer raises instead of reading UNDETERMINABLE",
    ),
    "cache-path-unwritten-reported": Mutant(
        CODE_EFFECTS,
        "str(path) if stored and path is not None else None",
        "str(path) if path is not None else None",
        "cache_path is reported although the record could not be written",
    ),
    "hook-denies-uncovered-write": Mutant(
        HOOK,
        "        if denied and may_deny(approved, ambient, command):\n",
        "        if False:\n",
        "the hook never denies: an uncovered write falls through to the harness prompt",
    ),
    "hook-allows-uncovered-write": Mutant(
        HOOK,
        "    uncovered = [r for r in changes if not covered_by(approved, r)]\n",
        "    uncovered = []\n",
        "a write outside the record's resources reads as covered and is allowed",
    ),
    "hook-active-at-root": Mutant(
        HOOK,
        "    if _depth(env) < 1:\n",
        "    if False:\n",
        "the hook decides at recursion depth 0, so the root coordinator is bound by a child's record",
    ),
    "hook-kill-switch-ignored": Mutant(
        HOOK,
        "    if not agentctl_config.resource_grant_hook_enabled(env, constants):\n",
        "    if False:\n",
        "the kill switch no longer stops the hook",
    ),
    "hook-error-allows": Mutant(
        HOOK,
        '        return Verdict(NONE, "error", message=',
        '        return Verdict(ALLOW, "error", message=',
        "a hook error is rendered as an allow",
    ),
    "hook-undeterminable-allows": Mutant(
        HOOK,
        "    return Verdict(NONE, UNDETERMINABLE, **base)\n",
        '    return Verdict(ALLOW, "covered", **base)\n',
        "a Bash call with an unresolved or undeterminable segment is allowed",
    ),
    "hook-unresolved-segment-resolved": Mutant(
        HOOK,
        '    if segment.status == "unresolved":\n',
        "    if False:\n",
        "an unresolved segment is read as one that changes nothing",
    ),
    "hook-opaque-code-unread": Mutant(
        HOOK,
        "    if opaque:\n        timeout = budget.next_timeout()\n",
        "    if False:\n        timeout = budget.next_timeout()\n",
        "an opaque segment's code is not sent to the code reader, so its writes are never seen",
    ),
    "hook-edit-denies": Mutant(
        HOOK,
        '    return Verdict(NONE, "uncovered-write", resources=shown, venue=approved.cwd)\n',
        '    return Verdict(DENY, "uncovered-write", resources=shown, venue=approved.cwd)\n',
        "an Edit or Write outside the record is denied instead of left to the harness",
    ),
    "hook-edit-allows-all": Mutant(
        HOOK,
        "    if covered_by(approved, resource):\n        return Verdict(ALLOW",
        "    if True:\n        return Verdict(ALLOW",
        "an Edit or Write is allowed whatever file it names",
    ),
    "hook-read-allows-all": Mutant(
        HOOK,
        "    kind = readable(approved, target)\n    if kind is None:\n",
        "    kind = readable(approved, target)\n    if False:\n",
        "a Read is allowed whatever file it names",
    ),
    "hook-scratchpad-of-any-session": Mutant(
        HOOK,
        'parts[i] == parent_session and parts[i + 1] == "scratchpad"',
        'parts[i + 1] == "scratchpad"',
        "any session's scratchpad is readable, not only the parent's",
    ),
    "hook-scratchpad-outside-temp-root": Mutant(
        HOOK,
        "    if not any(_under(real, root) for root in roots):\n        return False\n",
        "    if False:\n        return False\n",
        "a directory named <parent session>/scratchpad anywhere on disk counts as the scratchpad",
    ),
    "hook-allow-entry-ignored": Mutant(
        HOOK,
        "    if entry is not None:\n        return SegmentVerdict(text, HARNESS",
        "    if False:\n        return SegmentVerdict(text, HARNESS",
        "an entry of the child's permissions.allow no longer shelters a write from a deny",
    ),
    "hook-rule-match-ignores-redirect": Mutant(
        HOOK,
        "    if any(ch in RULE_UNSAFE_CHARS for ch in text):\n        return False\n",
        "",
        "a rule matches a segment carrying a redirect or substitution its text does not name",
    ),
    "hook-rule-prefix-without-word-boundary": Mutant(
        HOOK,
        '        return operand == prefix or operand.startswith(prefix + " ")\n',
        "        return operand.startswith(prefix)\n",
        "a prefix rule matches a longer word (`git add:*` matches `git addition`)",
    ),
    "hook-accept-edits-in-any-mode": Mutant(
        HOOK,
        "    if approved.mode != ACCEPT_EDITS or not changed:\n",
        "    if not changed:\n",
        "the acceptEdits auto-run is assumed in every permission mode, so no deny is made",
    ),
    "hook-deny-in-any-mode": Mutant(
        HOOK,
        "        approved.mode == ACCEPT_EDITS\n        and approved.allow_entries is not None\n",
        "        approved.allow_entries is not None\n",
        "an uncovered write is denied in every permission mode, not only the one probe Q6 measured",
    ),
    "hook-deny-without-explicit-lists": Mutant(
        HOOK,
        "        and approved.allow_entries is not None\n        and approved.add_dirs is not None\n",
        "",
        "a record without permissions_allow / add_dirs (a resumed child's old record) is read as empty lists and denies",
    ),
    "hook-strict-entry-suppresses-deny": Mutant(
        HOOK,
        "        and lenient_allow_entry([*approved.allow_entries, *ambient.allow], command) is None\n",
        "        and segment_allow_entry(approved, command) is None\n",
        "the strict rule match decides whether an allow entry stops a deny, so a `$` or `2>&1` call the harness allows is denied",
    ),
    "hook-deny-inside-add-dirs": Mutant(
        HOOK,
        "    roots = [approved.cwd, *(approved.add_dirs or []), *ambient.add_dirs]\n",
        "    roots = [approved.cwd, *ambient.add_dirs]\n",
        "a write into a directory the child was started with (the evidence or own-plan directory) is denied",
    ),
    "hook-plan-mode-decides": Mutant(
        HOOK,
        '    if str(payload.get("permission_mode") or "") == PLAN_MODE:\n',
        "    if False:\n",
        "plan mode, which the probe did not measure, gets an allow or a deny",
    ),
    "hook-cdpath-trusted": Mutant(
        HOOK,
        "tool_contracts.resolve_segments(command, approved.cwd, cdpath_unverified=True)",
        "tool_contracts.resolve_segments(command, approved.cwd, cdpath_unverified=False)",
        "`cd sub` is read as the working-directory entry although the Bash shell's CDPATH cannot be read from the hook",
    ),
    "hook-env-blocker-ignored": Mutant(
        HOOK,
        "    variable = env_blocker(approved, text)\n",
        "    variable = None\n",
        "a GIT_DIR / PYTHONPATH exported in the hook's environment does not make a git or python segment undeterminable",
    ),
    "hook-allow-entry-matches-whole-call": Mutant(
        HOOK,
        "    entry = segment_allow_entry(approved, text)\n",
        '    entry = segment_allow_entry(approved, " ".join(s.text for s in line))\n',
        "an allow entry for the first segment is matched against the whole line, so it shelters an uncovered tail",
    ),
    "hook-rule-pattern-matches-whole-call": Mutant(
        HOOK,
        "    pattern = _first_rule(approved.rule_patterns, text)\n",
        '    pattern = _first_rule(approved.rule_patterns, " ".join(s.text for s in line))\n',
        "a wildcard-tail rule pattern is matched against the whole line, so it covers an uncovered tail",
    ),
    "hook-judge-timeout-unbounded": Mutant(
        HOOK,
        "            budget = JudgeBudget(total, advisor.code_effects_judge_timeout_s())\n",
        "            budget = JudgeBudget(total, total)\n",
        "each judge call may take the hook's whole timeout instead of the judge's own",
    ),
    "hook-opaque-reads-unconfined": Mutant(
        HOOK,
        "    if opaque and not judged:\n",
        "    if False:\n",
        "opaque code that writes nothing is allowed although what it reads is not confined",
    ),
    "hook-opaque-reads-confined-by-any-write": Mutant(
        HOOK,
        "    if opaque and not judged:\n",
        "    if opaque and not changes:\n",
        "opaque code whose only write is its own redirect is allowed although what it reads is not confined",
    ),
    "hook-ambient-allow-ignored": Mutant(
        HOOK,
        "        and lenient_allow_entry([*approved.allow_entries, *ambient.allow], command) is None\n",
        "        and lenient_allow_entry(approved.allow_entries, command) is None\n",
        "an allow entry of the user, project or managed settings does not keep the hook from denying a call the harness allows",
    ),
    "hook-ambient-add-dirs-ignored": Mutant(
        HOOK,
        "    roots = [approved.cwd, *(approved.add_dirs or []), *ambient.add_dirs]\n",
        "    roots = [approved.cwd, *(approved.add_dirs or [])]\n",
        "a directory the settings add as a working directory does not count as one, so a write into it is denied",
    ),
    "hook-unparseable-settings-read-as-empty": Mutant(
        HOOK,
        "        data = json.loads(text)\n    except ValueError:\n        return None\n",
        "        data = json.loads(text)\n    except ValueError:\n        return Ambient()\n",
        "a settings file that is not JSON reads as empty, so the hook denies on settings it could not read",
    ),
    "hook-malformed-permissions-read-as-empty": Mutant(
        HOOK,
        "    if not isinstance(allow, list) or not isinstance(dirs, list):\n        return None\n",
        "    if not isinstance(allow, list) or not isinstance(dirs, list):\n        return Ambient()\n",
        "a settings file whose permissions lists have the wrong shape reads as empty, so the hook denies on it",
    ),
    "hook-project-settings-not-read": Mutant(
        HOOK,
        "    for base in dict.fromkeys([launch, segment_resolver.worktree_root(launch) or launch]):\n",
        "    for base in []:\n",
        "the project's .claude/settings files are not among the ambient settings",
    ),
    "hook-project-settings-read-from-cwd": Mutant(
        HOOK,
        "    launch = approved.launch_dir or approved.cwd\n",
        "    launch = approved.cwd\n",
        "the project settings are read from the hook's cwd, not from the directory the child was launched in",
    ),
    "hook-relative-add-dir-not-anchored": Mutant(
        HOOK,
        "    root = path.parent.parent\n",
        "    root = Path.cwd()\n",
        "a relative additionalDirectories entry is anchored on the hook's cwd, not on the settings file's root",
    ),
    "hook-user-settings-not-read": Mutant(
        HOOK,
        '    roots = [home / ".claude"]\n',
        "    roots = []\n",
        "the user's ~/.claude/settings files are not among the ambient settings",
    ),
    "hook-config-root-settings-not-read": Mutant(
        HOOK,
        "        if env.get(name):\n            roots.append(Path(env[name]).expanduser())\n",
        "        if False:\n            roots.append(Path(env[name]).expanduser())\n",
        "the settings of the config root (CLAUDE_CONFIG_DIR, CLAUDE_AGENT_HOME) are not among the ambient settings",
    ),
    "hook-carrier-ignores-env-blocker": Mutant(
        HOOK,
        "    for name in sorted(approved.env):\n        if name in GIT_ENV_EXACT or name.startswith(GIT_ENV_PREFIX):\n            return name\n",
        "    for name in sorted(approved.env):\n        if program == \"git\" and (name in GIT_ENV_EXACT or name.startswith(GIT_ENV_PREFIX)):\n            return name\n",
        "a GIT_* variable blocks only a git segment, so python run under GIT_DIR is checked as if it were plain",
    ),
    "hook-env-blocker-python-var-only-for-python": Mutant(
        HOOK,
        '        if name.startswith("PYTHON") and name not in PYTHON_ENV_HARMLESS:\n',
        '        if program.startswith("python") and name.startswith("PYTHON") and name not in PYTHON_ENV_HARMLESS:\n',
        "a PYTHON* variable blocks only a python segment, so git run under PYTHONPATH (a python hook or alias) is checked as if it were plain",
    ),
    "hook-env-blocker-blocks-plain-programs": Mutant(
        HOOK,
        "    if program in NON_CARRIER_PROGRAMS:\n        return None\n",
        "",
        "a GIT_*/PYTHON* variable makes even `cat` undeterminable",
    ),
    "hook-git-surface-snapshot-ignored": Mutant(
        HOOK,
        "        why = git_surface.mismatch(approved.git_surface, live[verdict.venue])\n",
        "        why = None\n",
        "a git run is allowed whatever the repository would execute, the spawn-time snapshot is never compared",
    ),
    "hook-git-surface-blocker-skipped": Mutant(
        HOOK,
        "        why = git_surface_blocker(approved, verdicts, budget)\n",
        "        why = None\n",
        "no segment that runs git is held to the snapshot",
    ),
    "hook-git-surface-harness-git-unchecked": Mutant(
        HOOK,
        "def runs_git(verdict: \"SegmentVerdict\") -> bool:\n",
        "def runs_git(verdict: \"SegmentVerdict\") -> bool:\n    if verdict.state == HARNESS:\n        return False\n",
        "a git segment the harness runs by an allow entry is not held to the snapshot",
    ),
    "hook-git-surface-neighbour-pinned-overlap-ignored": Mutant(
        HOOK,
        "        if any(_under(real, path) or _under(path, real) for path in pinned):\n            return True\n",
        "        if False:\n            return True\n",
        "a covered write onto a hook, config or attributes file beside a git run does not make the line undeterminable",
    ),
    "hook-git-surface-pattern-neighbour-not-counted": Mutant(
        HOOK,
        "    if verdict.opaque or verdict.pattern:\n        return True\n",
        "    if verdict.opaque:\n        return True\n",
        "a rule-pattern neighbour of a git run, whose writes were never resolved, counts as harmless",
    ),
    "hook-git-surface-opaque-neighbour-not-counted": Mutant(
        HOOK,
        "    if verdict.opaque or verdict.pattern:\n        return True\n",
        "    if verdict.pattern:\n        return True\n",
        "an opaque neighbour of a git run, whose writes the judge cannot enumerate, counts as harmless",
    ),
    "hook-git-surface-harness-neighbour-not-counted": Mutant(
        HOOK,
        "    if verdict.state == HARNESS and not verdict.changed and program not in READ_ONLY_PROGRAMS:\n        return True\n",
        "",
        "a harness-allowed neighbour that is not read-only, with no resolved writes, counts as harmless",
    ),
    "hook-git-surface-rewriting-subcommand-neighbour-ok": Mutant(
        HOOK,
        "        return not _preserves_worktree(verdict.text)\n",
        "        return False\n",
        "a git neighbour that rewrites tracked files (checkout, restore) cannot change what the next git run executes",
    ),
    "hook-git-surface-option-before-subcommand-ok": Mutant(
        HOOK,
        "    return [] if not tokens or tokens[0].startswith(\"-\") else tokens\n",
        "    return tokens\n",
        "`git -c core.hooksPath=... <subcommand>` is read as a plain subcommand",
    ),
    "hook-git-surface-lone-runner-option-unchecked": Mutant(
        HOOK,
        "            if not _after_plain_options(verdict.text):\n",
        "            if False:\n",
        "a lone git segment with an option that moves what git executes is not stopped",
    ),
    "hook-judge-child-guard-removed": Mutant(
        HOOK,
        "    if os.environ.get(JUDGE_CHILD_ENV_VAR):\n        return 0\n",
        "",
        "the hook decides inside the opaque-code judge's own child process",
    ),
    "hook-git-probe-timeout-ignores-deadline": Mutant(
        HOOK,
        "        return self._left(GIT_PROBE_TIMEOUT_S)\n",
        "        return GIT_PROBE_TIMEOUT_S\n",
        "the hooks-directory probe takes its fixed timeout whatever is left of the call's deadline",
    ),
    "hook-deadline-sliver-is-nothing": Mutant(
        HOOK,
        "        return 0 if left <= 0 else max(1, math.ceil(min(cap, left)))\n",
        "        return 0 if left <= 0 else int(min(cap, left))\n",
        "less than a second left of the deadline reads as no time at all",
    ),
    "hook-git-surface-unreadable-allows": Mutant(
        HOOK,
        "            return f\"cannot read what git would execute in {verdict.venue}: {exc}\"\n",
        "            continue\n",
        "a repository whose surface cannot be read is treated as one with nothing to execute",
    ),
    "git-surface-old-record-matches": Mutant(
        GIT_SURFACE,
        "        return \"the handoff record carries no git-surface snapshot\"\n",
        "        return None\n",
        "a record without the snapshot (the old format) is read as matching every live surface",
    ),
    "git-surface-unknown-repo-matches": Mutant(
        GIT_SURFACE,
        "        return f\"{live.repo} is not in the git-surface snapshot taken at spawn\"\n",
        "        return None\n",
        "a repository the snapshot does not list is read as unchanged",
    ),
    "git-surface-digest-only-compared": Mutant(
        GIT_SURFACE,
        "    if saved[\"digest\"] == live.digest and saved[\"parts\"] == live.parts:\n",
        "    if True:\n",
        "a live surface is read as the snapshot's whatever its digest, as long as the repository is listed",
    ),
    "git-surface-hooks-entry-dropped": Mutant(
        GIT_SURFACE,
        "                record[\"entries\"][name] = _entry(path)\n",
        "                record[\"entries\"][name] = {}\n",
        "the entries of a hooks directory (name, mode, bytes) are not part of the snapshot",
    ),
    "git-surface-symlink-target-bytes-dropped": Mutant(
        GIT_SURFACE,
        "        record[\"content\"] = _file_digest(real) if target is not None and stat.S_ISREG(target.st_mode) else \"not-a-file\"\n",
        "        record[\"content\"] = \"not-a-file\"\n",
        "the bytes of the file a symlinked hook entry resolves to are not part of the snapshot",
    ),
    "git-surface-config-dropped": Mutant(
        GIT_SURFACE,
        "        \"config\": _exec_entries(entries, base, paths),\n",
        "        \"config\": [],\n",
        "the config entries that name a program, and those programs' bytes, are not part of the snapshot",
    ),
    "git-surface-config-program-bytes-dropped": Mutant(
        GIT_SURFACE,
        "            record[\"programs\"] = {target: _entry(target) for target in targets}\n",
        "            record[\"programs\"] = {target: {} for target in targets}\n",
        "the bytes of a program a config entry names are not part of the snapshot",
    ),
    "git-surface-attributes-dropped": Mutant(
        GIT_SURFACE,
        "        \"attributes\": _attributes_part(toplevel, _resolve(info_attributes, base), attributes_file, timeout(), paths),\n",
        "        \"attributes\": {},\n",
        "the .gitattributes files are not part of the snapshot",
    ),
    "git-surface-frameworks-dropped": Mutant(
        GIT_SURFACE,
        "        \"frameworks\": _frameworks_part(toplevel, paths),\n",
        "        \"frameworks\": {},\n",
        "the hook framework files (.pre-commit-config.yaml, lefthook, .husky) are not part of the snapshot",
    ),
    "spawn-record-drops-git-surface": Mutant(
        SPAWN,
        "        git_surface=git_surface_snapshot([workdir, state.resolve_check_venue(CheckVenue.DELIVERY.value)]),\n",
        "",
        "the handoff record the spawn writes carries no git-surface snapshot, so no git run can be verified",
    ),
    "spawn-record-drops-launch-dir": Mutant(
        SPAWN,
        "        launch_dir=workdir or None,\n",
        "",
        "the handoff record carries no launch directory, so the hook reads project settings from its own cwd",
    ),
    "norm-handoff-verify-drops-git-surface": Mutant(
        NORM_HANDOFF,
        "        \"git_surface\": record.get(\"git_surface\") if isinstance(record.get(\"git_surface\"), dict) else None,\n",
        "        \"git_surface\": None,\n",
        "the verified record never hands the git-surface snapshot on",
    ),
    "spawn-record-drops-permissions-allow": Mutant(
        SPAWN,
        '    record["permissions_allow"] = list(settings.get("permissions", {}).get("allow", []))\n',
        "    pass\n",
        "the handoff record the spawn writes carries no permissions_allow, so the hook can never deny",
    ),
    "spawn-record-allow-before-baselines": Mutant(
        SPAWN,
        '    record["permissions_allow"] = list(settings.get("permissions", {}).get("allow", []))\n',
        '    record["permissions_allow"] = [e for e in settings.get("permissions", {}).get("allow", []) if e.startswith("Bash(")]\n',
        "the record's permissions_allow differs from the child's final permissions.allow",
    ),
    "hook-reads-unconfined": Mutant(
        HOOK,
        "    if not confined:\n",
        "    if False:\n",
        "a read-only program naming a path outside the approved resources is allowed",
    ),
    "hook-writes-no-log": Mutant(
        HOOK,
        "    if state_root is not None:\n        append_row(",
        "    if False:\n        append_row(",
        "no decision-log row is appended",
    ),
    "spawn-hook-unregistered": Mutant(
        SPAWN,
        "    if child_linked and resource_grant_hook_enabled(constants=constants):\n",
        "    if False:\n",
        "a linked child's --settings carries no resource-grant hook",
    ),
    "spawn-hook-registered-for-unlinked": Mutant(
        SPAWN,
        "    if child_linked and resource_grant_hook_enabled(constants=constants):\n",
        "    if resource_grant_hook_enabled(constants=constants):\n",
        "an unlinked child, which has no record to check against, is given the hook",
    ),
    "spawn-hook-ignores-kill-switch": Mutant(
        SPAWN,
        "    if child_linked and resource_grant_hook_enabled(constants=constants):\n",
        "    if child_linked:\n",
        "the hook is registered although the kill switch is off",
    ),
    "spawn-hook-timeout-hardcoded": Mutant(
        SPAWN,
        "register_resource_grant_hook(child_settings, resource_grant_hook_timeout_s(constants))",
        "register_resource_grant_hook(child_settings, 60)",
        "the hook's timeout in the child's settings ignores resource-grant-hook-timeout-s",
    ),
}


class ScratchError(Exception):
    """The scratch copy could not be made."""


def _git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if proc.returncode != 0:
        raise ScratchError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def _paths(raw: bytes) -> "list[str]":
    return [p for p in raw.decode("utf-8", errors="surrogateescape").split("\0") if p]


def make_scratch(repo: Path, dest: Path) -> Path:
    """Clone `repo` at HEAD into `dest/repo` and copy its uncommitted state over:
    modified and staged files, deletions, and untracked (not ignored) files."""
    clone = dest / "repo"
    proc = subprocess.run(
        ["git", "clone", "--local", "--quiet", str(repo), str(clone)], capture_output=True, check=False)
    if proc.returncode != 0:
        raise ScratchError(f"git clone failed: {proc.stderr.decode(errors='replace').strip()}")
    changed = _paths(_git(repo, "diff", "HEAD", "--name-only", "--no-renames", "-z"))
    untracked = _paths(_git(repo, "ls-files", "--others", "--exclude-standard", "-z"))
    for rel in dict.fromkeys(changed + untracked):
        src, dst = repo / rel, clone / rel
        if src.is_symlink() or src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            shutil.copy2(src, dst, follow_symlinks=False)
        elif dst.is_symlink() or dst.exists():
            dst.unlink()
    return clone


def _anchor_count(root: Path, mutant: Mutant) -> int:
    """How many times the anchor occurs in the mutant's file under `root` (0 if unreadable)."""
    try:
        return (root / mutant.relpath).read_text(encoding="utf-8").count(mutant.anchor)
    except OSError:
        return 0


def apply_mutant(root: Path, mutant: Mutant) -> bool:
    """Substitute the anchor in `root`; False (nothing written) unless it occurs exactly once."""
    if _anchor_count(root, mutant) != 1:
        return False
    target = root / mutant.relpath
    target.write_text(target.read_text(encoding="utf-8").replace(mutant.anchor, mutant.replacement),
                      encoding="utf-8")
    return True


def anchors_apply_once(root: Path, catalogue: "dict[str, Mutant]") -> "list[str]":
    """The names of catalogue entries whose anchor does not occur exactly once under `root`."""
    return [name for name, mutant in catalogue.items() if _anchor_count(root, mutant) != 1]


def run_command(cmd: str, cwd: Path, env_extra: "dict[str, str] | None" = None) -> int:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env_extra or {})}
    code = subprocess.run(shlex.split(cmd), cwd=cwd, env=env, check=False).returncode
    return 128 - code if code < 0 else code


def run_mutant(
    name: str, run: "str | None" = None, *, repo: Path = REPO_ROOT,
    catalogue: "dict[str, Mutant] | None" = None,
) -> int:
    mutant = (CATALOGUE if catalogue is None else catalogue).get(name)
    if mutant is None:
        print(f"resource-grant-mutation: no mutant named {name!r}", file=sys.stderr)
        return 0
    with tempfile.TemporaryDirectory(prefix="resource-grant-mutation-") as tmp:
        try:
            clone = make_scratch(repo, Path(tmp))
        except ScratchError as exc:
            print(f"resource-grant-mutation: scratch copy failed, no mutation applied: {exc}", file=sys.stderr)
            return 0
        if _anchor_count(clone, mutant) != 1:
            print(f"resource-grant-mutation: anchor of {name} does not match exactly once in "
                  f"{mutant.relpath}; nothing was mutated", file=sys.stderr)
            return 0
        if run is not None:
            baseline = run_command(run, clone)
            if baseline != 0:
                print(f"resource-grant-mutation: the command already exits {baseline} on the unmutated "
                      f"copy, so a red run on the mutant would prove nothing; no mutation applied",
                      file=sys.stderr)
                return 0
        if not apply_mutant(clone, mutant):
            print(f"resource-grant-mutation: {name} did not apply; nothing was mutated", file=sys.stderr)
            return 0
        print(f"resource-grant-mutation: applied {name} ({mutant.description})")
        if run is None:
            return 0
        return run_command(run, clone, {ACTIVE_MUTANT_ENV: name})


def run_control(
    run: "str | None" = None, *, repo: Path = REPO_ROOT, catalogue: "dict[str, Mutant] | None" = None,
) -> int:
    catalogue = CATALOGUE if catalogue is None else catalogue
    with tempfile.TemporaryDirectory(prefix="resource-grant-mutation-") as tmp:
        try:
            clone = make_scratch(repo, Path(tmp))
        except ScratchError as exc:
            print(f"resource-grant-mutation: scratch copy failed: {exc}", file=sys.stderr)
            return CONTROL_RED
        missing = anchors_apply_once(clone, catalogue)
        if missing:
            print(f"resource-grant-mutation: anchor does not occur exactly once for: {', '.join(missing)}",
                  file=sys.stderr)
            return CONTROL_RED
        if run is None:
            print(f"resource-grant-mutation: control green -- {len(catalogue)} anchor(s) each occur exactly once")
            return 0
        return run_command(run, clone)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true")
    mode.add_argument("--control", action="store_true")
    mode.add_argument("--mutant", metavar="NAME")
    ap.add_argument("--run", metavar="CMD", help="one argv command (shlex-split, no shell) to run in the scratch copy")
    args = ap.parse_args(argv)
    try:
        if args.list:
            for name, mutant in CATALOGUE.items():
                print(f"{name}\t{mutant.relpath}\t{mutant.description}")
            return 0
        if args.control:
            return run_control(args.run)
        return run_mutant(args.mutant, args.run)
    except Exception:
        traceback.print_exc()
        if args.mutant is not None:
            print("resource-grant-mutation: internal error; exiting 0 so it cannot read as a kill", file=sys.stderr)
            return 0
        print("resource-grant-mutation: internal error; the control is not green", file=sys.stderr)
        return CONTROL_RED


if __name__ == "__main__":
    sys.exit(main())
