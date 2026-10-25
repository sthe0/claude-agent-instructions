"""Unit rows for `lib/bash_write_targets.py` — the lexer, on its own terms.

WHY THIS FILE EXISTS SEPARATELY from the two hook suites that consume the lexer. Both of
them assert VERDICTS (deny / allow), which is one bit downstream of a list of paths, so a
lexer that reaches the right decision from the wrong candidate set passes them. That is not
hypothetical, it is MEASURED: two mutations of this module — dropping `-m`/`-o`/`-g` from
`_COPY_VALUE_OPTS`, and forcing `dest_is_dir = False` — leave all 146 self-grant rows and all
36 canon rows passing, and die only here. Both mutations OVER-emit while still emitting the
real target, and a consumer that stops at its first definite hit cannot tell the difference.
The candidate list is this module's whole contract (`command_write_targets` returns paths,
over-inclusively and by design), and the defects this round fixed were wrong-candidate
defects — a `600` manufactured as a source path, and a `~` joined under the cwd. Rows here
pin the LIST, exactly, so a change to it fails here first and in the language of the thing
that changed.

The module has two consumers with different policies over the same candidates
(`hook-guard-permission-self-grant.py`, `hook-guard-canon-readonly.py`), which is why it was
extracted at all; a shared contract is worth a suite of its own.

EVERY EXPECTATION BELOW IS A MEASUREMENT, not a derivation. Two of them were written from
the source first and were wrong in the same direction — an ambiguous destination emits BOTH
readings, and `sed -i` emits its script token too — which is the argument for exact lists
rather than `in` checks.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib.bash_write_targets import command_write_targets  # noqa: E402

CWD = "/w"


# --- a destination directory resolves to the files inside it -------------------------

@pytest.mark.parametrize("command, expected", [
    # THE SPELLINGS THAT SAY DIRECTORY emit the joins ALONE: `cp s.json d/` does not write
    # `d`, it writes `d/s.json`, and reporting the directory made a consumer asking "is this
    # target a JSON document" answer no and allow the write.
    ("cp s.json d/", ["/w/d/s.json"]),
    ("mv s.json d/", ["/w/d/s.json"]),
    ("cp -t d s.json", ["/w/d/s.json"]),
    ("cp --target-directory=d s.json", ["/w/d/s.json"]),
    # THE AMBIGUOUS BARE TOKEN emits BOTH readings, because which one it means is not
    # decidable without a stat and this module does not touch the filesystem. `cp s.json d`
    # writes `d` if `d` is a file and `d/s.json` if it is a directory; the caller tests
    # candidates under its own policy, and over-emitting is safe for both of them.
    ("cp s.json d", ["/w/d", "/w/d/s.json"]),
    # MULTI-SOURCE: one join per source, and the directory is not among them.
    ("cp a.json b.json d/", ["/w/d/a.json", "/w/d/b.json"]),
    # `install`'s VALUE-TAKING OPTIONS are modelled, so `600` is not read as a source. This is
    # the row that pins `_COPY_VALUE_OPTS`: without `-m` in that table the lexer manufactures
    # `/w/d/600`, a path the command never writes, and the real target rides along beside a
    # fabrication.
    ("install -m 600 s.json d/", ["/w/d/s.json"]),
    ("install -m 600 s.json d", ["/w/d", "/w/d/s.json"]),
    ("install -o root -g root -m 600 s.json d/", ["/w/d/s.json"]),
    # A trailing separator on a SOURCE is stripped before the basename, so a directory copied
    # by name joins as itself rather than as an empty basename.
    ("cp src/ d/", ["/w/d/src"]),
    # One operand and no `-t` is not a well-formed copy: there is no source to join, so the
    # single token is reported as the destination, as this verb's handling always has.
    ("cp d", ["/w/d"]),
    ("cp", []),
    # Flags that take no value are skipped without being read as operands.
    ("cp -r -v a.json d/", ["/w/d/a.json"]),
])
def test_a_copy_destination_resolves_to_the_files_it_means(command, expected):
    assert command_write_targets(command, CWD) == expected


# --- `~` is expanded; `$VAR` is deliberately not ------------------------------------

@pytest.mark.parametrize("command, expected", [
    # `~` MEANS THE INVOKING USER'S HOME and this process runs as that user, so expanding it
    # names the path the command will really write. Until this round the literal `~` joined
    # under the cwd, and `<cwd>/~/.claude-agent/settings.json` is a path nothing is on --
    # which is how the self-grant gate read ABSENT ("a creation, nothing to widen") about the
    # most ordinary spelling of the write it exists to stop.
    ("cp s.json ~/.claude-agent/settings.json",
     ["/home/u/.claude-agent/settings.json", "/home/u/.claude-agent/settings.json/s.json"]),
    ("cp s.json ~/.claude-agent/", ["/home/u/.claude-agent/s.json"]),
    ("mv s.json ~/.claude-agent/settings.json",
     ["/home/u/.claude-agent/settings.json", "/home/u/.claude-agent/settings.json/s.json"]),
    ("install -m 600 s.json ~/.claude-agent/", ["/home/u/.claude-agent/s.json"]),
    # `_abs` is the single choke point for every target token, which is why the redirect and
    # the other verb families expand too rather than needing four separate fixes.
    ("echo x > ~/.claude-agent/settings.json", ["/home/u/.claude-agent/settings.json"]),
    ("tee ~/.claude-agent/settings.json", ["/home/u/.claude-agent/settings.json"]),
    # `sed -i` no longer emits its inline script as a candidate — the script token is
    # consumed before file operands are collected, so only the real file path appears.
    ("sed -i s/a/b/ ~/.claude-agent/settings.json",
     ["/home/u/.claude-agent/settings.json"]),
    # A bare `~` resolves, and the ambiguous-token rule applies on top of it.
    ("cp s.json ~", ["/home/u", "/home/u/s.json"]),
    # `~` anywhere but the front is NOT a home reference and must not become one.
    ("cp s.json d/~x", ["/w/d/~x", "/w/d/~x/s.json"]),
])
def test_a_leading_tilde_is_expanded(command, expected, monkeypatch):
    monkeypatch.setenv("HOME", "/home/u")
    assert command_write_targets(command, CWD) == expected


@pytest.mark.parametrize("command, expected", [
    # `HOME` and `TMPDIR` are the two names expanded from the hook's own environment: the
    # hook and the command's shell are spawned by the same harness, so they agree. This is
    # a premise, not a proof (see `_VarTracker`); no other inherited name gets this trust.
    ("cp s.json $HOME/.claude/settings.json",
     ["/home/u/.claude/settings.json", "/home/u/.claude/settings.json/s.json"]),
    ("cp s.json ${HOME}/.claude/", ["/home/u/.claude/s.json"]),
    ("echo x > $HOME/.claude/settings.json", ["/home/u/.claude/settings.json"]),
])
def test_a_variable_is_expanded_for_inherited_home_tmpdir(command, expected, monkeypatch):
    monkeypatch.setenv("HOME", "/home/u")
    assert command_write_targets(command, CWD) == expected


@pytest.mark.parametrize("command, expected", [
    # A name outside `HOME`/`TMPDIR` is NOT expanded, set or not: the token joins under the
    # cwd as the safe over-inclusive fallback. Each consumer names it as a residual.
    ("cp s.json $ZZZZUNDEF_299/.claude/settings.json",
     ["/w/$ZZZZUNDEF_299/.claude/settings.json", "/w/$ZZZZUNDEF_299/.claude/settings.json/s.json"]),
    ("echo x > $ZZZZUNDEF_299/.claude/settings.json", ["/w/$ZZZZUNDEF_299/.claude/settings.json"]),
])
def test_an_unlisted_variable_is_not_expanded(command, expected, monkeypatch):
    monkeypatch.setenv("ZZZZUNDEF_299", "/tmp/elsewhere")
    assert command_write_targets(command, CWD) == expected


def test_an_absent_home_still_resolves_to_the_invoking_users_home(monkeypatch):
    # MEASURED, because the first version of this row asserted the opposite from the docs:
    # with `HOME` deleted, `os.path.expanduser("~/x")` does NOT return the token unchanged --
    # it falls back to the passwd entry for `os.getuid()` and still yields that user's home.
    #
    # Which makes the fallback an ARGUMENT FOR the expansion rather than a caveat about it:
    # `~` resolves to the invoking user's home by two independent routes and to a guess by
    # neither, so a candidate built from it names a path this process really would write.
    # That is exactly the guarantee `$VAR` cannot offer.
    monkeypatch.delenv("HOME", raising=False)
    home = os.path.expanduser("~")
    assert os.path.isabs(home) and home != "~"
    assert command_write_targets("cp s.json ~/.claude-agent/", CWD) == [f"{home}/.claude-agent/s.json"]


# --- the rest of the contract, so a change to it is not silent ----------------------

@pytest.mark.parametrize("command, expected", [
    # Absolute destinations pass through the join untouched.
    ("cp s.json /abs/d/", ["/abs/d/s.json"]),
    # Redirect targets come first, in left-to-right order, before verb-based ones.
    ("cp s.json d/ > log.txt", ["/w/log.txt", "/w/d/s.json"]),
    ("echo x >> a.txt", ["/w/a.txt"]),
    ("echo x >a.txt", ["/w/a.txt"]),
    # Each segment of a list/pipeline is lexed on its own.
    ("cp a.json d/ && mv b.json e/", ["/w/d/a.json", "/w/e/b.json"]),
    # THE PATCH VERBS ANSWER WITH THE WORKING DIRECTORY, deliberately: which files a patch
    # writes is stated inside the patch BODY, so the directory is the only honest target
    # derivable from the command line.
    ("patch -p1 -i p.patch", ["/w"]),
    ("git apply p.patch", ["/w"]),
    # `sed` without an in-place flag writes nothing. With `-i`, only file operands
    # are returned — the inline script token is consumed without being added.
    ("sed s/a/b/ f.txt", []),
    ("sed -i s/a/b/ f.txt", ["/w/f.txt"]),
    # A verb this table does not model reports nothing -- for the self-grant gate an ALLOW by
    # a route that never reached a verdict, and axis-1 member (ii) of its named residuals.
    # Pinned here so that enumeration stays honest about what is outside it.
    ("tar -xf a.tar -C d", []),
    ("dd of=d/s.json", []),
    # An untokenizable command is fail-open BY CONTRACT: empty, not an exception. The canon
    # guard needs that; the self-grant gate pre-lexes the command itself rather than changing
    # another consumer's contract to suit its own direction.
    ('cp "unbalanced d/', []),
    ("", []),
])
def test_the_rest_of_the_lexer_contract(command, expected):
    assert command_write_targets(command, CWD) == expected


# --- a bare newline is a statement separator, exactly like `;` ----------------------

@pytest.mark.parametrize("command, expected", [
    # Finding 7: a bare `\n` between two statements is ordinary whitespace to plain
    # `shlex.split`, unlike `;`/`|`/`&`/`&&`/`||`, which DO become their own token once
    # whitespace-surrounded. This command has no heredoc and lexes cleanly WITHOUT this
    # fix too (`shlex.split` never raises), so it exercises `command_write_targets`'s
    # own tokenization directly -- not a heredoc-stripping fallback. Before
    # `normalize_newline_separators`, the two physical lines collapsed into one segment
    # whose first word is the write-incapable `echo`, hiding `tee` entirely, even though
    # real bash runs `tee` as its own statement.
    ("echo hi\ntee out.json </dev/null\n", ["/w/out.json"]),
])
def test_bare_newline_separated_statement_is_found_via_general_path(command, expected):
    assert command_write_targets(command, CWD) == expected


# --- a `#` comment must not hide a later statement's write target -------------------

@pytest.mark.parametrize("command, expected", [
    # A trailing `# comment` on the first line of a multi-line command is ordinary
    # shape, and once bare newlines are rewritten to `;` the whole text goes through
    # ONE tokenizer -- so a lexer left on `shlex.shlex`'s default `commenters='#'`
    # ends the token stream right there and the write on the next line is invisible.
    # These rows pin that the comment stays inert: the later target is still reported.
    ("git status # look first\ntee out.json </dev/null\n", ["/w/out.json"]),
    # `cp a.json d/s.json` reports both readings of an operand with no trailing slash
    # (destination file, and destination directory receiving `a.json`) -- the same
    # ambiguity the `cp s.json d` row above already pins. Unrelated to the comment.
    ("echo hi # note\ncp a.json d/s.json\n", ["/w/d/s.json", "/w/d/s.json/a.json"]),
    ("git log --oneline # a\nsed -i s/x/y/ d/s.json\n", ["/w/d/s.json"]),
])
def test_a_hash_comment_does_not_hide_a_later_write_target(command, expected):
    assert command_write_targets(command, CWD) == expected


# --- write process substitution adds cwd; read process substitution does not ----------

@pytest.mark.parametrize("command, expected", [
    # `>(tee y)` is a WRITE substitution: the outer command pipes its output into `tee`,
    # which may write anywhere — the real destination is not derivable from the command
    # line alone. `eff_cwd` is the same over-inclusive answer `patch`/`git apply` give.
    # The operator-exact tokenizer splits `>(tee y)` into `['>', '(', 'tee', ...]`, so
    # `redirect_targets` skips `(` to avoid a spurious `/w/(` artifact, and the nested
    # `tee y` is walked like any command, so its own target is named as well.
    ("echo hi >(tee y)", ["/w", "/w/y"]),
    # `<(cmd)` is a READ substitution: `cmd`'s stdout feeds the outer command's INPUT, so
    # `<(` adds nothing of its own (it used to add `eff_cwd`, a false deny in a canon cwd).
    # But the body is a command that really runs: `dd of=y` has no operand grammar here, so
    # a body whose verb is in `_UNMODELED_WRITERS` keeps the cwd candidate (fail closed),
    # while a body that only reads does not.
    ("git commit -F - <(dd if=x of=y bs=1)", ["/w"]),
    ("diff <(git show HEAD:config.md) <(cat /tmp/x.md)", []),
    ("diff <(python3 gen.py) x", ["/w"]),
    # A verb outside the table is the named residual: `chmod +x f` writes, the walker has no
    # grammar for it and the case it must not deny is `diff <(git show ...) ...`. Adding a
    # verb to `_UNMODELED_WRITERS` is a deliberate change of this row.
    ("diff <(chmod +x f) x", []),
    # No process substitution: no extra candidate.
    ("git commit -F -", []),
])
def test_write_process_substitution_adds_cwd_read_does_not(command, expected):
    assert command_write_targets(command, CWD) == expected


# --- comparison-operator text inside a quoted/heredoc argument is not a redirect ----

@pytest.mark.parametrize("command, expected", [
    # Round-2 finding B4: a real replay row against the agentctl state dir reported a
    # write target of the bare string `=`, traced to a `>=` comparison inside the BODY
    # of a `python3 - <<'EOF'` heredoc. `python3` is a `NON_SHELL_CONSUMERS` member, so
    # `neutralize_heredoc_constructs` blanks the body before tokenizing -- this pins that
    # the blanking actually removes the `>=` rather than leaving it for the redirect scan.
    ("python3 - <<'EOF'\nx = 5\nif x >= 3:\n    print('ok')\nEOF", []),
    # The second real row reported target `=5`, traced to `>=5` inside a QUOTED argument
    # (no heredoc at all) -- e.g. a `grep`/`python3 -c` pattern string. A quoted argument
    # is one shlex token; it must never be re-split into bash's own `>` operator plus a
    # trailing `=5` operand.
    ('grep -n "x >=5" f.txt', []),
    ('python3 -c "x = 5\nif x >=5:\n    pass"', []),
    ("cd /w && python3 - <<'EOF'\nx = 5\nif x >= 3:\n    print('ok')\nEOF", []),
    # Round-3 finding: a real replay row against the agentctl state dir reported phantom
    # targets `=` and `=5`, both traced to a `>=` comparison inside a `python3 - <<'EOF'`
    # heredoc BODY whose `<<` operator is not on the command's first physical line (`cd D`
    # is). The old `_recognized(..., allow_prior_statements=True)` looked for `<<` only in
    # `command_line(command)` (the first line), missed it here, and fell back to an
    # unscoped consumer check that saw `cd` -- not a `CONSUMERS`/`NON_SHELL_CONSUMERS`
    # member -- and disqualified the whole command. Disqualified means NOT neutralized: the
    # `>=` inside the still-present body then read as a real `>` redirect.
    ("cd D\npython3 - <<'EOF'\nif a >= 5:\nEOF", []),
])
def test_a_comparison_operator_in_a_quoted_or_heredoc_body_is_not_a_redirect(command, expected):
    assert command_write_targets(command, CWD) == expected


def test_an_unrecognized_prior_statement_on_an_earlier_line_still_lets_a_later_heredoc_body_be_neutralized():
    # `myunknowncmd` is on its own line before the heredoc-bearing statement -- an
    # unrecognized WORD, not an unrecognized SHAPE (no `_UNRECOGNIZED` token appears), so it
    # must not disqualify the later `cat <<'D' > f` statement it precedes. The body itself
    # carries a `>= 1` comparison: under the old first-line-only `<<` search this statement
    # was never recognized (the first line "myunknowncmd" is not a `CONSUMERS` member), so
    # the body was never neutralized and its `>=` read as a second, phantom write target
    # alongside the real one at `f`.
    assert command_write_targets("myunknowncmd\ncat <<'D' > f\nx >= 1\nD", CWD) == ["/w/f"]


# --- issue-299: the four false-positive shapes that triggered false canon denials ----

def test_issue299_sed_script_operand_not_treated_as_path():
    """The inline script token (`s/a/b/`) is not a file path; only the file operand is."""
    assert command_write_targets("sed -i 's/a/b/' /tmp/x.py", CWD) == ["/tmp/x.py"]


def test_issue299_sed_with_ef_flag_treats_all_operands_as_files():
    """`-e` supplies the script; every non-option token is then a file operand."""
    assert command_write_targets("sed -i -e 's/a/b/' f.txt g.txt", CWD) == [
        "/w/f.txt", "/w/g.txt"
    ]


def test_issue299_same_command_var_assignment_expands():
    """A VAR=value assignment in a preceding segment expands $VAR in a later segment."""
    assert command_write_targets("o=/tmp/out; echo hi > $o/f.txt", CWD) == ["/tmp/out/f.txt"]


def test_issue299_env_variable_expands(monkeypatch):
    """An inherited `$TMPDIR` expands rather than joining under the cwd."""
    monkeypatch.setenv("TMPDIR", "/tmp/outdir")
    assert command_write_targets("printf x > $TMPDIR/o.txt", CWD) == ["/tmp/outdir/o.txt"]


def test_issue299_read_process_substitution_does_not_add_cwd():
    """`<(cmd)` is a read substitution — the outer command writes nothing extra."""
    assert command_write_targets("diff <(cat /tmp/a) <(cat /tmp/b)", CWD) == []


@pytest.mark.parametrize("command, expected", [
    ("sed -i -f s.sed t.txt", ["/w/t.txt"]),
    ("sed -n -i 's/a/b/' t.txt u.txt", ["/w/t.txt", "/w/u.txt"]),
    ("sed --expression=s/a/b/ --in-place t.txt", ["/w/t.txt"]),
    ("sed -i --expression s/a/b/ t.txt", ["/w/t.txt"]),
    # A script option with its value glued on (`-es/x/y/`, `-fscript.sed`) consumes no
    # following token, so the file after it is still a write target.
    ("sed -i -es/x/y/ f", ["/w/f"]),
    ("sed -i -e's/a/b/' canon/f", ["/w/canon/f"]),
    ("sed -i -fscript.sed f", ["/w/f"]),
    ("sed -i --expr=s/a/b/ f", ["/w/f"]),
    # A cluster ending in `e`/`f` takes its value from the next token, wherever `-i` sits.
    ("sed -ne p -i f", ["/w/f"]),
    ("sed -i -ne p f", ["/w/f"]),
    ("sed -i --file s.sed f", ["/w/f"]),
    # `-ie` is in-place with suffix `e`, not a script flag: `p` is the script operand.
    ("sed -ie p f", ["/w/f"]),
])
def test_issue299_sed_script_operand_forms(command, expected):
    assert command_write_targets(command, CWD) == expected


def test_issue299_assignments_chain_and_cross_newlines():
    assert command_write_targets("a=/tmp/x; b=$a/y\necho > $b/f", CWD) == ["/tmp/x/y/f"]


def test_issue299_exported_assignment_expands_and_overrides_the_inherited_value(monkeypatch):
    monkeypatch.setenv("TMPDIR", "/tmp/inherited")
    assert command_write_targets("export TMPDIR=/elsewhere/t; echo > $TMPDIR/f", CWD) == ["/elsewhere/t/f"]
    assert command_write_targets("HOME=/elsewhere/h; echo > $HOME/f", CWD) == ["/elsewhere/h/f"]


def test_issue299_write_inside_a_process_substitution_is_still_a_candidate():
    assert "/w/canon/f" in command_write_targets("diff <(tee canon/f) y", CWD)
    assert "/w/canon/f" in command_write_targets("echo hi >(tee canon/f)", CWD)


# --- fail closed: a value that cannot be known for certain is never expanded ----------
#
# Every row below would hide a write to `/canon/...` behind `/tmp/...` if the walker
# trusted the wrong value. The expected answer is the unexpanded cwd join, the
# over-inclusive direction every consumer already handles.

@pytest.mark.parametrize("command", [
    # a prefix assignment is temporary: it must not replace the earlier value
    "o=/canon; o=/tmp true; echo > $o/f",
    # conditional, piped and backgrounded assignments may or may not have run
    "o=/canon; false && o=/tmp; echo > $o/f",
    "o=/canon; false || o=/tmp; echo > $o/f",
    "o=/canon; o=/tmp | cat; echo > $o/f",
    "o=/canon; o=/tmp & echo > $o/f",
    # once doubtful, a name stays doubtful even after a later literal assignment
    "o=/canon; false && o=/tmp; o=/tmp; echo > $o/f",
    # compound commands run assignments zero, one or many times, in any textual order
    "o=/tmp; if true; then o=/canon; fi; echo > $o/f",
    "o=/tmp; while true; do echo > $o/f; o=/canon; done",
    "o=/tmp; { o=/canon; }; echo > $o/f",
    "o=/tmp; (o=/canon); echo > $o/f",
    # builtins that assign through an operand discard everything known
    "read o; echo > $o/f",
    "o=/tmp; eval 'o=/canon'; echo > $o/f",
    "o=/tmp; printf -v o %s /canon; echo > $o/f",
    "o=/tmp; printf -vo %s /canon; echo > $o/f",
    "o=/tmp; printf -qvo %s /canon; echo > $o/f",
    # assignment by parameter expansion, from an empty value and within the walked word
    "o=; : ${o:=/canon}; echo > $o/f",
    "o=; echo ${o:=/canon} > $o/f",
    "o=/tmp; : ${o=/canon}; echo > $o/f",
    "o=/tmp; declare -n o=other; echo > $o/f",
    "o=/tmp; unset o; echo > $o/f",
    # appends, subscripts and non-literal values
    "o=/tmp; o+=/../canon; echo > $o/f",
    "o=$UNKNOWN_299/x; echo > $o/f",
    "o=`pwd`; echo > $o/f",
    # a `$` the shell leaves literal, or a parameter-expansion operator, is not a plain name
    "o=/tmp; echo > '$o/f'",
    "o=/tmp; echo > \\$o/f",
    "o=/tmp; echo > $'$o'/f",
    "o=/tmp; echo > ${o:-/canon}/f",
])
def test_a_variable_that_cannot_be_known_for_certain_is_not_expanded(command, monkeypatch):
    monkeypatch.setenv("TMPDIR", "/tmp")
    # the tokenizer drops the ANSI-C quoting, so `$'$o'/f` reaches the walker as `$$o/f`
    unexpanded = {
        "o=/tmp; echo > ${o:-/canon}/f": "/w/${o:-/canon}/f",
        "o=/tmp; echo > $'$o'/f": "/w/$$o/f",
    }.get(command, "/w/$o/f")
    assert command_write_targets(command, CWD) == [unexpanded]


@pytest.mark.parametrize("command, expected", [
    # the assignment is a prefix of a verb whose operand is not an assignment word, and an
    # opaque verb named only as an ARGUMENT discards nothing
    ("o=/tmp; echo read o; echo > $o/f", ["/tmp/f"]),
    ("o=/tmp; echo eval x=1; echo > $o/f", ["/tmp/f"]),
    # an opaque verb behind a prefix assignment or a wrapper still discards everything
    ("o=/tmp; A=1 read o; echo > $o/f", ["/w/$o/f"]),
    ("o=/tmp; command eval 'o=/canon'; echo > $o/f", ["/w/$o/f"]),
])
def test_an_opaque_verb_counts_only_at_the_verb_position(command, expected):
    assert command_write_targets(command, CWD) == expected


@pytest.mark.parametrize("command, expected", [
    ("HOME=/x/h; cp s ~/f", ["/x/h/f", "/x/h/f/s"]),
    ("HOME=/x/h; echo > ~/f", ["/x/h/f"]),
    ("echo > ~/f", ["/home/u/f"]),
])
def test_a_leading_tilde_follows_a_home_assigned_in_the_command(command, expected, monkeypatch):
    monkeypatch.setenv("HOME", "/home/u")
    assert command_write_targets(command, CWD) == expected


@pytest.mark.parametrize("command", [
    "export TMPDIR=/canon; false && export TMPDIR=/tmp; echo > $TMPDIR/f",
    "read TMPDIR; echo > $TMPDIR/f",
    "TMPDIR=/tmp true; TMPDIR=/canon; echo > $TMPDIR/f | cat",
])
def test_an_inherited_variable_is_dropped_when_the_command_may_change_it(command, monkeypatch):
    monkeypatch.setenv("TMPDIR", "/tmp")
    assert command_write_targets(command, CWD) == ["/w/$TMPDIR/f"]


def test_only_home_and_tmpdir_are_taken_from_the_hook_environment(monkeypatch):
    monkeypatch.setenv("REPO_299", "/tmp/elsewhere")
    assert command_write_targets("echo > $REPO_299/f", CWD) == ["/w/$REPO_299/f"]


# THE OTHER CONSUMER'S BEHAVIOUR CHANGED TOO, and it has rows of its own for it.
# `hook-guard-canon-readonly.py` shares this lexer, so the expansion makes it STRICTER: a
# `cp evil.md ~/canon-mirror/doc.md` aimed at a registered canon root was measured ALLOW
# before the expansion and DENY after (subprocess, fake `HOME`, `CLAUDE_CANON_ROOTS_FILE`,
# cwd outside canon), while `~/outside-of-canon.md` stayed ALLOW -- stricter in the right
# direction, no over-denial. Its pre-existing rows did not notice either way, so
# `test_hook_guard_canon_readonly.py` gained
# `test_bash_tilde_spelled_write_into_canon_roots_file_denies` and its ALLOW control to pin
# exactly that: MEASURED, removing `expanduser` from `_abs` fails the deny row alone --
# 1 failed, 35 passed out of that suite's 36 -- and the ALLOW control legitimately survives
# the mutation because it asserts ALLOW either way.


_REAL_LATER_LINE_HEREDOCS = json.loads(
    (Path(__file__).parent / "fixtures" / "bash_write_targets" / "later_line_heredoc_real.json").read_text()
)


@pytest.mark.parametrize("row_id", sorted(_REAL_LATER_LINE_HEREDOCS))
def test_the_real_replayed_later_line_heredoc_commands_have_no_write_target(row_id):
    # The two replay rows (by would-fire id) that reported the phantom `=` / `=5` targets,
    # copied verbatim apart from machine paths and the session uuid.
    assert command_write_targets(_REAL_LATER_LINE_HEREDOCS[row_id], CWD) == []


def test_a_heredoc_written_script_run_by_a_later_bash_statement_keeps_its_write_target():
    # Same result before and after the round-3 whole-command `<<` search.
    assert command_write_targets("cat <<'EOF' > f\nx\nEOF\nbash f", CWD) == ["/w/f"]


@pytest.mark.parametrize(
    "command",
    [
        # `$[...]` is one arithmetic word in bash; its `<<` is a shift, not a heredoc.
        "cd D\ncat $[1<<2 ]\necho x > /s/settings.json\n2",
        # A quoted decoy `<<` must not decide which prefix gets checked.
        "cd D\ncat \"<<\"\ncat $((1<<2 ))\necho x > /s/settings.json\n2",
        # Round-4: a harmless `<<<` first must not become the anchor for the check.
        "cat <<<x\ncat $[1<<2 ]\necho x > /s/settings.json\n2",
        "cat <<<x\ncat \"<<\"\ncat $((1<<2 ))\necho x > /s/settings.json\n2",
    ],
)
def test_an_arithmetic_shift_after_a_prior_line_does_not_hide_a_real_redirect(command):
    # Round-3 review blocking 1: both returned no target after 55148c3.
    assert command_write_targets(command, CWD) == ["/s/settings.json"]
