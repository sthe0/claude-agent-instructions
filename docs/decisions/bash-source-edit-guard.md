# Decision: a PreToolUse Bash hook denies shell writes of literal text into tracked source

## Difficulty

The production-edit state gate (`hook-state-gate.py`) and the cross-session scope check
(`hook-scope-conflict.py`) are registered for `Edit|Write` only. A `sed -i`, a heredoc redirect or a
`tee` issued through `Bash` therefore edits tracked source with neither gate seeing it. The norm
"Edit tools only for source edits" (developer SKILL.md § While developing, CLAUDE.md § Limits) states
the rule; `scripts/hook-guard-bash-source-edit.py` is its structural half.

## Verdict model

A function of shell syntax and filesystem facts only — never of the written text
([regex-not-for-semantic-classification](../../memory-global/leaves/regex-not-for-semantic-classification.md)).
Heredoc bodies are blanked by `lib/shell_tokens.neutralize_heredoc_constructs` before tokenizing, so
the body cannot influence the verdict; the tests pin this (`payload_invariant_*`).

DENY when one command segment writes a target that is (a) produced by a recognized write shape and
(b) inside a tracked tree and (c) not allowlisted:

- **Shapes:** `sed -i` / `gsed -i`, `perl -i` (own flag-cluster parser, `-e`/`-f` aware), a `>` / `>>`
  redirect from `echo`, `printf` or a stdin-only `cat` (heredoc / here-string / no file operand), and
  `tee` when its pipeline starts with such a literal producer. `cat a > b` is a copy and is allowed.
- **Tracked tree:** the target's resolved path lies in a git work tree (`git rev-parse
  --is-inside-work-tree`, run with a 3 s timeout) or under an ancestor holding a `.arc` marker
  directory (a filesystem-only test; no VCS client is called). The target need not exist:
  the nearest existing ancestor directory decides, so `echo x > new_file.py` is denied.
- **Allowlist,** checked on both the nominal and the resolved path (a match on either allows):
  `agentctl.exempt_paths.is_engine_exempt` (memory / `/tmp/` fragments) and `scratch_roots()`.
  Symlinks into a git tree are resolved, so a symlinked write path cannot dodge the tree test, and
  a `memory-global` symlink into a repo is still allowed.
- **Effective cwd:** `lib/git_cwd.effective_git_cwd` follows a leading `cd` / `git -C`, so
  `cd repo && echo x > f` is judged against `repo`.

## The `~/.arc` exception

The home directory of a machine that has used arc carries its own `.arc` (global state: mount table,
store, token) — not a mount root. Treating it as a marker would make everything under `$HOME` a
tracked tree, including plain scratch directories. The ancestor walk therefore **stops at the home
directory** (realpath compare) and counts a `.arc` only strictly below it. Both real mounts on a
machine (`~/task-mounts/<name>/.arc`) lie below it and are detected. This refines the brief's
"nearest ancestor `.arc`" rule; `test_allow_plain_dir` and `test_allow_home_arc_state_dir` pin it.

## Fail-open surface

Always exit 0; deny only on a positively identified shape. Allowed without comment: unparsable JSON
envelope, a command the tokenizer rejects (unbalanced quote, unterminated heredoc), process
substitution, a target containing `$` or a backtick (unresolvable at hook time), a non-Bash tool, an
empty command, an unreadable target path, and any git error or timeout.

## Residuals (named, not claimed closed)

- **Not covered; the prose norm carries them:** `python3 -c`, `awk -i inplace`, `dd of=`, `xargs`,
  `find -exec`, and any interpreter that writes a file itself.
- **Walker false positives / negatives #298, #299** (sed script operand, `$VAR`, env-var and process
  substitution shapes) are out of scope here; the hook stays fail-open on all four.
- `shlex` loses quote information, so a quoted argument that begins with `>` looks like a glued
  redirect token. The hook drops such a token when it contains whitespace (an unquoted redirect
  word cannot), which covers `echo "> a quote"` and `printf '> %s'`; a whitespace-free quoted
  argument (`echo '>x'`) is still read as a redirect to `x` — a false deny only when `x` lies in a
  tracked tree. The payload-invariance claim holds for the written text of heredoc bodies and
  commit messages; it is not absolute for a quoted word shaped exactly like a glued redirect.
- A redirect glued to a preceding word (`x>foo`) follows `lib/bash_write_targets` behaviour.
- Follow-up (not in this change): the hook duplicates `_abs`, `_operands_until_redirect` and a git
  wrapper that also live in the walker; fold them into `lib/bash_write_targets` once the walker's
  own shapes (#298, #299) are settled.

## Registration

`install-reminder-hooks.sh` (`PreToolUse`, `Bash`, timeout 5), `lib/hook_wiring.GATE_BEARING_HOOKS`
(so `hook-canon-guard-wired-check.py` reports it when absent), `verify-layout-contract.sh`,
`rule-registry.toml`, `docs/components/hooks.md`, `scripts/README.md`, the crutch registry
(`gen_crutch_registry.CODE_ID_OVERRIDES`, regenerated `crutch_registry.toml`,
`test_crutch_inventory._AUDITED_ROWS`) and the hard-sink allowlist
(`test_no_semantic_unguarded._KNOWN_UNGUARDED_HARD_SINKS`) — a new deny sink is RED there by design.

## Testing

`scripts/tests/test_hook_guard_bash_source_edit.py` drives the hook only as a subprocess through its
stdin/stdout contract and never imports it, so a negative control can replace the file with an
always-allow or always-deny stub. Deny-side fixtures live under
`Path.home()/.cache/hook-guard-bash-source-edit-tests/<uuid>`, never under `tmp_path`/`$TMPDIR`: those
are scratch roots and would make every deny test pass vacuously. `allow_tmp_git_tree` uses `tmp_path`
on purpose, to prove the allowlist wins over work-tree detection.
