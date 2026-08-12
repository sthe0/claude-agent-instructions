"""Differential oracle for `lib/shell_tokens.strip_heredoc_bodies`: real bash is
the ground truth for "does this command write canon", and the guard must not go
from DENY to ALLOW on any command that does.

This is the stage's primary control, and it is a DIFFERENTIAL test rather than a
completeness test on purpose. The naive predicate -- "bash writes canon =>
the guard denies" -- is RED on the UNMODIFIED tree: `exec 3>`, `exec 3>>`,
`dd of=`, `cp` in forms the dest parser misses and `>|` all genuinely write and
are all allowed today. A control that fails before the change measures the
guard's pre-existing coverage, not this change. The predicate here is therefore

    NOT (bash writes canon AND the guard denied RAW AND the guard allows STRIPPED)

which is exactly the claim the stage makes: body removal takes nothing that was
denied and makes it allowed. Pre-existing bypasses stay pre-existing bypasses and
are named in `hook-guard-canon-readonly.py`'s NAMED RESIDUAL block, not silently
laundered through this file.

THREE DIFFERENT NUMBERS, which this file is careful never to conflate:

  CORPUS       -- every construction in `CASES`. The breadth of shell grammar the
                  rule has been pointed at. Asserted by `test_case_table_is_large_enough`.
  EXERCISED    -- the subset whose stripped form DIFFERS from its raw form. Only
                  these can carry the differential predicate: where stripping is a
                  no-op the guard receives the same bytes and returns the same
                  decision by construction, so the remaining cases are pruned and
                  no oracle run can add information about them. Pruned cases are
                  corpus, not control.
  BASH-REACHED -- the subset of EXERCISED that real bash was measured to actually
                  write canon in. Only these reach the `bash_writes` conjunct, so
                  only these can ever expose a widening.

A corpus of any size proves nothing if the rule stops recognizing it, so EXERCISED
and BASH-REACHED are both asserted at floors set from measurement (see
`test_body_removal_never_turns_a_real_write_from_deny_into_allow`). Reading the
corpus figure as if it were the control's strength is the specific mistake those
assertions exist to prevent.

ORACLE BLIND SPOT, stated rather than papered over: `bash_writes` observes the
filesystem after the command's foreground process group exits. A write performed
by a DETACHED background process (`cat <<'EOF' > >(bash)`, a `&`-backgrounded
consumer) can land after `subprocess.run` returns and be scored as "no write",
which would score a real regression as safe. It is bounded, not closed, by a
short fixed settle interval below; the structural defence is that every such
construction is disqualified by clause (ii) anyway.
"""
from __future__ import annotations

import importlib.util
import itertools
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = SCRIPTS_DIR / "hook-guard-canon-readonly.py"

sys.path.insert(0, str(SCRIPTS_DIR))
from lib import bash_write_targets, shell_tokens  # noqa: E402

_spec = importlib.util.spec_from_file_location("guard_hook_under_test", HOOK_SCRIPT)
guard_hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard_hook)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}

# Placeholder the case table writes instead of a real canon path, so the same
# string can be pointed at a hermetic git repo (for the guard) and at a throwaway
# directory (for bash). Not `{canon}`: several constructions contain literal
# braces, which `str.format` would eat.
CANON = "@CANON@"
MARKER = "evil.txt"
R = f"echo hi > {CANON}/{MARKER}"
NL = "\n"

# Interval allowed for a straggler write to land after the foreground command
# exits. Bounds -- does not close -- the blind spot named in the module docstring.
SETTLE_S = 0.25
BASH_TIMEOUT_S = 10


# --- the case table -------------------------------------------------------
#
# Ported from the measurement scripts that drove the rule's eight review rounds,
# grouped by the lead each round attacked. Names are kept so a failure points at
# the round that found the construction.
#
# Composition -- source under /tmp/cc-scratch/planreview/ (scratch; cases were
# ported, the files themselves are not imported) -> group in this file, so a
# reviewer can recount each source against the group it landed in:
#
#   difftest.py           -> _PARSE_DESYNC                              (20)
#   measure8.py  deny      -> _EXECUTED_BODY (12 non-interpreter forms) plus
#                             4 of its 16 named forms (bash<<EOF, sh<<EOF,
#                             cat|bash, env bash) folded into
#                             _INTERPRETER_CONSUMERS under those same names
#                             rather than duplicated
#   measure8.py  allow      -> FALSE_POSITIVES, first 5 entries
#   measure_closed.py      -> _INSIDE_RECOGNIZED_SHAPE, first 14 entries
#   measure_closed2.py     -> _INSIDE_RECOGNIZED_SHAPE, remaining 20 entries
#   measure_closed3.py     -> _DELIMITER_QUOTING                         (16)
#   measure_closed4.py     -> _EXPANSION_AND_TERMINATOR                  (16)
#   measure_closed5.py     -> _DELIMITER_BOUNDARY, 15 new delimiter-word
#                             attacks, plus 1 named false-positive re-test ("fp
#                             bare plain unquoted delim", a genuinely new
#                             payload, added to FALSE_POSITIVES); its other two
#                             re-tests were byte-identical to entries already
#                             present and are not duplicated
#   an independent review   -> _REVIEWER_FAMILIES (22) and
#     of this stage            _INERT_CONSUMER_WRITES (12). The review built its
#                             own 38-construction oracle and named eight families
#                             this table had not reached; both groups below were
#                             reconstructed from those families and measured here
#                             rather than copied over, so every verdict in them is
#                             one this file observed
#   the nine pre-existing   -> _COMMAND_LINE_WRITERS: exec 3>, exec 3>>,
#     bypasses                 dd of=, cp, >|, sed 'w', awk '{print >}' are
#                             named entries there; "sort -o" additionally gets
#                             a bare, heredoc-free entry there ("sort -o bare
#                             no heredoc"); "python3 <<EOF" is the "python"
#                             entry in _INTERPRETER_CONSUMERS -- python3 is not
#                             a recognized consumer, so stripping is a no-op on
#                             it either way, which IS the bypass
#
# _BOUND_ASYMMETRY and three _COMMAND_LINE_WRITERS entries (sed -i with
# heredoc, mv dest canon, git commit after heredoc) predate this sourcing pass
# and are kept as coverage beyond what the sources above require.

_PARSE_DESYNC = [
    ("arith subshell depth-reset", f"n=$(( (1+2) << 3 )){NL}{R}{NL}3"),
    ("arith nested parens", f"n=$(( ((1)) << 3 )){NL}{R}{NL}3"),
    ("for (( )) header", f"for (( i=0; i<2; i++ )); do :; done{NL}(( x = 1 << 2 )){NL}{R}{NL}2"),
    ("case pattern leading paren", f"case a in (a) :;; esac{NL}(( x = 1 << 2 )){NL}{R}{NL}2"),
    ("case pattern bare paren", f"case a in a) :;; esac{NL}(( x = 1 << 2 )){NL}{R}{NL}2"),
    ("subshell then arith", f"( echo a ){NL}(( x = 1 << 2 )){NL}{R}{NL}2"),
    ("ANSI-C quote", f"echo $'<<EOF'{NL}{R}{NL}EOF"),
    ("locale quote", f'echo $"<<EOF"{NL}{R}{NL}EOF'),
    ("backtick opens paren closes", f"x=`echo a` ; y=$(echo b){NL}(( z = 1 << 2 )){NL}{R}{NL}2"),
    ("here-string then write", f"cat <<< '{R}'{NL}{R}"),
    ("two heredocs one line", f"cat /dev/null <<A <<B{NL}{R}{NL}A{NL}x{NL}B"),
    ("escaped quote in dquote", f'echo "a\\"<<EOF"{NL}{R}{NL}EOF'),
    ("heredoc delim quoted", f"cat > /tmp/o <<'EOF'{NL}{R}{NL}EOF"),
    ("genuine heredoc tab form", f"cat > /tmp/o <<-EOF{NL}{R}{NL}\tEOF"),
    ("heredoc inside command sub", f"z=$( (echo a) ; cat <<E{NL}{R}{NL}E{NL})"),
    ("redirect before heredoc", f"{R} ; cat > /tmp/o <<EOF{NL}x{NL}EOF"),
    ("comment with heredoc op", f"# note <<EOF{NL}{R}{NL}EOF"),
    ("conditional shift", f"[[ 1 << 2 ]]{NL}{R}{NL}2"),
    ("stray close paren", f"echo a){NL}(( x = 1 << 2 )){NL}{R}{NL}2"),
    ("unmatched open paren", f"( echo a{NL}(( x = 1 << 2 )){NL}{R}{NL}2"),
]

_EXECUTED_BODY = [
    ("write then exec", f"cat <<'EOF' > /tmp/s.sh{NL}{R}{NL}EOF{NL}bash /tmp/s.sh"),
    ("tee then source", f"cat <<EOF | tee /tmp/s2.sh > /dev/null{NL}{R}{NL}EOF{NL}. /tmp/s2.sh"),
    ("procsub to bash", f"cat <<'EOF' > >(bash){NL}{R}{NL}EOF"),
    ("procsub tee to sh", f"cat <<'EOF' | tee >(sh) > /dev/null{NL}{R}{NL}EOF"),
    ("line continuation", f"cat <<'EOF' > /tmp/a4.sh \\{NL}; bash /tmp/a4.sh{NL}{R}{NL}EOF"),
    ("shadow via function body", f"cat() {{ bash; }}; cat <<'EOF'{NL}{R}{NL}EOF"),
    ("shadow via function kw", f"function cat {{ bash; }}; cat <<'EOF'{NL}{R}{NL}EOF"),
    ("shadow via alias", f"alias cat=bash; cat <<'EOF'{NL}{R}{NL}EOF"),
    ("and-then bash", f"cat <<'EOF' > /tmp/a2.sh && bash /tmp/a2.sh{NL}{R}{NL}EOF"),
    ("brace group", f"{{ cat <<'EOF' > /tmp/a3.sh ; bash /tmp/a3.sh ; }}{NL}{R}{NL}EOF"),
    ("eval of command sub", f"eval \"$(cat <<'EOF'{NL}{R}{NL}EOF{NL})\""),
    ("xargs bash", f"cat <<'EOF' | xargs -I{{}} bash -c '{{}}'{NL}{R}{NL}EOF"),
]

_INTERPRETER_CONSUMERS = [
    ("bash", f"bash <<'EOF'{NL}{R}{NL}EOF"),
    ("sh", f"sh <<'EOF'{NL}{R}{NL}EOF"),
    ("zsh", f"zsh <<'EOF'{NL}{R}{NL}EOF"),
    ("absolute bash", f"/bin/bash <<'EOF'{NL}{R}{NL}EOF"),
    ("env bash", f"env bash <<'EOF'{NL}{R}{NL}EOF"),
    ("bash -s", f"bash -s <<'EOF'{NL}{R}{NL}EOF"),
    ("assignment prefix bash", f"V=1 bash <<'EOF'{NL}{R}{NL}EOF"),
    ("pipe to bash", f"cat <<'EOF' | bash{NL}{R}{NL}EOF"),
    ("pipe to absolute bash", f"cat <<'EOF' | /bin/bash{NL}{R}{NL}EOF"),
    ("pipe to sh dash", f"cat <<'EOF' | sh -{NL}{R}{NL}EOF"),
    ("pipe through tee to bash", f"cat <<'EOF' | tee /dev/null | bash{NL}{R}{NL}EOF"),
    ("statement then bash", f"x=1; bash <<'EOF'{NL}{R}{NL}EOF"),
    ("python", f"python3 <<'EOF'{NL}import os{NL}os.system('{R}'){NL}EOF"),
    ("perl", f"perl <<'EOF'{NL}system('{R}');{NL}EOF"),
    ("command builtin", f"command bash <<'EOF'{NL}{R}{NL}EOF"),
    ("backslash escaped name", f"\\bash <<'EOF'{NL}{R}{NL}EOF"),
    ("nohup bash", f"nohup bash <<'EOF'{NL}{R}{NL}EOF"),
    ("timeout bash", f"timeout 5 bash <<'EOF'{NL}{R}{NL}EOF"),
]

_INSIDE_RECOGNIZED_SHAPE = [
    ("sort -o canon", f"sort -o {CANON}/{MARKER} <<'EOF'{NL}x{NL}EOF"),
    ("tee canon argv", f"cat <<'EOF' | tee {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("two heredocs one cmd", f"cat <<A <<B{NL}first{NL}A{NL}{R}{NL}B"),
    ("tab-strip form", f"cat <<-EOF > /tmp/t.md{NL}\t> {CANON}/f.txt{NL}\tEOF"),
    ("delim double-quoted", f'cat <<"EOF" > /tmp/t.md{NL}> {CANON}/f.txt{NL}EOF'),
    ("delim backslashed", f"cat <<\\EOF > /tmp/t.md{NL}> {CANON}/f.txt{NL}EOF"),
    ("delim indented close", f"cat <<'EOF' > /tmp/t.md{NL}{R}{NL}  EOF{NL}EOF"),
    ("unterminated body", f"cat <<'EOF' > /tmp/t.md{NL}{R}"),
    ("body holds delim word", f"cat <<'EOF' > /tmp/t.md{NL}EOFX{NL}{R}{NL}EOF"),
    ("redirect to dev stdout", f"cat <<'EOF' > /dev/stdout{NL}{R}{NL}EOF"),
    ("head then canon redirect", f"head <<'EOF' > {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("pipe tee to canon", f"cat <<'EOF' | tee > {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("wc with canon arg", f"wc <<'EOF' {CANON}/f.txt{NL}x{NL}EOF"),
    ("base64 -d to canon", f"base64 -d <<'EOF' > {CANON}/{MARKER}{NL}eA=={NL}EOF"),
    ("tee -a canon", f"cat <<'EOF' | tee -a {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("tee two canon args", f"cat <<'EOF' | tee {CANON}/a.txt {CANON}/b.txt{NL}x{NL}EOF"),
    ("split into canon", f"split <<'EOF' - {CANON}/pre{NL}x{NL}EOF"),
    ("space after operator", f"cat << EOF > /tmp/t.md{NL}{R}{NL}EOF"),
    ("operator then tab delim", f"cat <<-\tEOF > /tmp/t.md{NL}\t{R}{NL}\tEOF"),
    ("two redirects", f"cat <<'EOF' > /tmp/t.md > {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("append to canon", f"cat <<'EOF' >> {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("IFS prefix", f"IFS= cat <<'EOF' > /tmp/t.md{NL}{R}{NL}EOF"),
    ("LC_ALL prefix", f"LC_ALL=C cat <<'EOF' > /tmp/t.md{NL}{R}{NL}EOF"),
    ("SHELL=bash prefix", f"SHELL=bash cat <<'EOF' > /tmp/t.md{NL}{R}{NL}EOF"),
    ("dev fd target", f"cat <<'EOF' > /dev/fd/1{NL}{R}{NL}EOF"),
    ("here-string canon operand", f'cat <<< "{CANON}/f.txt" > /tmp/t.md'),
    ("here-string to canon", f'cat <<< "x" > {CANON}/{MARKER}'),
    ("sort -o canon in pipe", f"cat <<'EOF' | sort -o {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("uniq then tee canon", f"cat <<'EOF' | uniq | tee {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("near-delim line in body", f"cat <<'EOF' > /tmp/t.md{NL} EOF{NL}{R}{NL}EOF"),
    ("nl -s canon", f"nl -s {CANON}/f.txt <<'EOF' > /tmp/t.md{NL}{R}{NL}EOF"),
    ("rev out to canon", f"rev <<'EOF' > {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("base64 out to canon", f"base64 <<'EOF' > {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("md5sum out to canon", f"md5sum <<'EOF' > {CANON}/{MARKER}{NL}x{NL}EOF"),
]

_DELIMITER_QUOTING = [
    ("partial quote in delim", f'cat <<E"O"F > /tmp/t.md{NL}$({R}){NL}EOF'),
    ("backslash mid delim", f"cat <<EO\\F > /tmp/t.md{NL}$({R}){NL}EOF"),
    ("backslash delim", f"cat <<\\EOF > /tmp/t.md{NL}$({R}){NL}EOF"),
    ("double-quoted delim", f'cat <<"EOF" > /tmp/t.md{NL}$({R}){NL}EOF'),
    ("tab form expanding", f"cat <<-EOF > /tmp/t.md{NL}\t$({R}){NL}\tEOF"),
    ("tab form quoted", f"cat <<-'EOF' > /tmp/t.md{NL}\t$({R}){NL}\tEOF"),
    ("two heredocs 1st quoted", f"cat <<'A' <<B{NL}x{NL}A{NL}$({R}){NL}B"),
    ("two heredocs 1st bare", f"cat <<A <<'B'{NL}$({R}){NL}A{NL}y{NL}B"),
    ("delim is a variable", f"cat <<$X > /tmp/t.md{NL}$({R}){NL}EOF"),
    ("body expands then canon", f"cat <<EOF > /tmp/t.md{NL}$({R}){NL}> {CANON}/f.txt{NL}EOF"),
    ("here-string bare cmdsub", f"cat <<< $({R})"),
    ("here-string dquoted cmdsub", f'cat <<< "$({R})" > /tmp/t.md'),
    ("here-string backslash", f"cat <<< \\$({R})"),
    ("body backtick", f"cat <<EOF > /tmp/t.md{NL}`{R}`{NL}EOF"),
    ("body arithmetic", f"cat <<EOF > /tmp/t.md{NL}$((1)){NL}EOF"),
    ("unbalanced delim quote", f"cat <<'EOF\" > /tmp/t.md{NL}$({R}){NL}EOF"),
]

_EXPANSION_AND_TERMINATOR = [
    ("tilde in body", f"cat <<EOF > /tmp/t.md{NL}~/x{NL}EOF"),
    ("brace expansion in body", f"cat <<EOF > /tmp/t.md{NL}{{a,b}}{NL}EOF"),
    ("glob in body", f"cat <<EOF > /tmp/t.md{NL}*.txt{NL}EOF"),
    ("history bang in body", f"cat <<EOF > /tmp/t.md{NL}!!{NL}EOF"),
    ("procsub text in body", f"cat <<EOF > /tmp/t.md{NL}>(bash){NL}EOF"),
    ("terminator trailing space", f"cat <<EOF > /tmp/t.md{NL}{R}{NL}EOF {NL}EOF"),
    ("terminator leading space", f"cat <<EOF > /tmp/t.md{NL}{R}{NL} EOF{NL}EOF"),
    ("terminator tab indented", f"cat <<EOF > /tmp/t.md{NL}{R}{NL}\tEOF{NL}EOF"),
    ("tab form untabbed term", f"cat <<-EOF > /tmp/t.md{NL}{R}{NL}EOF"),
    ("no trailing newline", f"cat <<EOF > /tmp/t.md{NL}{R}{NL}EOF"),
    ("here-string concat var", "cat <<< a$b > /tmp/t.md"),
    ("here-string bare word", "cat <<< hello > /tmp/t.md"),
    ("body single-quoted cmdsub", f"cat <<EOF > /tmp/t.md{NL}'$({R})'{NL}EOF"),
    ("body double-quoted cmdsub", f'cat <<EOF > /tmp/t.md{NL}"$({R})"{NL}EOF'),
    ("two bare heredocs", f"cat <<A <<B > /tmp/t.md{NL}$({R}){NL}A{NL}y{NL}B"),
    ("two heredocs 2nd expands", f"cat <<'A' <<B > /tmp/t.md{NL}y{NL}A{NL}$({R}){NL}B"),
]

_DELIMITER_BOUNDARY = [
    ("dot in delim", f"cat <<EOF.X > /tmp/junk.txt{NL}filler{NL}EOF.X{NL}{R}{NL}EOF"),
    ("dash in delim", f"cat <<EOF-X > /tmp/junk.txt{NL}filler{NL}EOF-X{NL}{R}{NL}EOF"),
    ("slash in delim", f"cat <<EOF/X > /tmp/junk.txt{NL}filler{NL}EOF/X{NL}{R}{NL}EOF"),
    ("plus in delim", f"cat <<EOF+X > /tmp/junk.txt{NL}filler{NL}EOF+X{NL}{R}{NL}EOF"),
    ("colon in delim", f"cat <<EOF:X > /tmp/junk.txt{NL}filler{NL}EOF:X{NL}{R}{NL}EOF"),
    ("dot delim quoted", f"cat <<'EOF.X' > /tmp/junk.txt{NL}filler{NL}EOF.X{NL}{R}{NL}EOF"),
    ("squote then glued word", f"cat <<'EOF'X > /tmp/junk.txt{NL}filler{NL}EOFX{NL}{R}{NL}EOF"),
    ("dquote then glued word", f'cat <<"EOF"X > /tmp/junk.txt{NL}filler{NL}EOFX{NL}{R}{NL}EOF'),
    ("delim then redirect", f"cat <<EOF>/tmp/junk.txt{NL}{R}{NL}EOF"),
    ("delim then pipe", f"cat <<EOF|wc{NL}{R}{NL}EOF"),
    ("here-string then redirect", f"cat <<< x>{CANON}/{MARKER}"),
    ("here-string quoted glued", 'cat <<< "x"y > /tmp/junk.txt'),
    ("here-string then pipe", "cat <<< x|wc > /tmp/junk.txt"),
    ("delim then tab", f"cat <<EOF\t> /tmp/junk.txt{NL}{R}{NL}EOF"),
    ("delim then paren", f"cat <<EOF){NL}{R}{NL}EOF"),
]

# Eight grammar families an independent review's own oracle covered and this
# table did not. Reconstructed here and measured against real bash: none is a
# regression, and nine of the twenty-two are EXERCISED, so they widen the
# control rather than only the corpus.
_REVIEWER_FAMILIES = [
    # A closing quote with no opener: the delimiter reader must not treat the
    # stray quote as if it closed something, and bash rejects the command
    # outright, so nothing may ride on the two agreeing.
    ("delim close squote no open", f"cat <<EOF' > /tmp/t.md{NL}{R}{NL}EOF"),
    ("delim close dquote no open", f'cat <<EOF" > /tmp/t.md{NL}{R}{NL}EOF'),
    ("delim squote pair after word", f"cat <<EOF'' > /tmp/t.md{NL}> {CANON}/f.txt{NL}EOF"),
    ("delim dquote pair after word", f'cat <<EOF"" > /tmp/t.md{NL}> {CANON}/f.txt{NL}EOF'),
    # A tab between the operator and the delimiter: bash accepts it, the reader
    # skips only spaces, so it must fail closed rather than misread the word.
    ("tab after operator", f"cat <<\tEOF > /tmp/t.md{NL}{R}{NL}EOF"),
    ("tab after operator quoted", f"cat <<\t'EOF' > /tmp/t.md{NL}{R}{NL}EOF"),
    # A redirect BEFORE the command word -- legal bash, and the shape clause has
    # to survive a command line that does not start with its consumer.
    ("redirect before command word", f"> /tmp/t.md cat <<'EOF'{NL}> {CANON}/f.txt{NL}EOF"),
    ("redirect first canon target", f"> {CANON}/{MARKER} cat <<'EOF'{NL}x{NL}EOF"),
    ("append first then command", f">> /tmp/t.md cat <<'EOF'{NL}> {CANON}/f.txt{NL}EOF"),
    # Here-string operands glued to the operator with no separating space.
    ("here-string glued squote", f"cat <<<'> {CANON}/f.txt'"),
    ("here-string glued dquote", f'cat <<<"> {CANON}/f.txt"'),
    ("here-string glued bare canon", f"cat <<<{CANON}/f.txt"),
    # An allowlisted consumer named by absolute path: `_consumer_ok` takes the
    # basename, so these must behave exactly as their bare-name siblings do --
    # including the interpreter one, which must stay denied.
    ("absolute cat consumer", f"/bin/cat <<'EOF' > /tmp/t.md{NL}> {CANON}/f.txt{NL}EOF"),
    ("absolute tee canon argv", f"cat <<'EOF' | /usr/bin/tee {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("absolute cat piped to absolute bash", f"/bin/cat <<'EOF' | /bin/bash{NL}{R}{NL}EOF"),
    # Pipelines with an empty element, where splitting on `|` yields a segment
    # that is no command at all.
    ("or-list then exec", f"cat <<'EOF' > /tmp/a5.sh || bash /tmp/a5.sh{NL}{R}{NL}EOF"),
    ("empty leading pipeline element", f"| cat <<'EOF' > /tmp/t.md{NL}> {CANON}/f.txt{NL}EOF"),
    ("pipe to empty then bash", f"cat <<'EOF' |&  bash{NL}{R}{NL}EOF"),
    # Carriage returns: bash's terminator match is byte-exact, this reader's is
    # `str.strip()`, so a CRLF body is a place the two could disagree.
    ("CRLF body and terminator", "cat <<'EOF' > /tmp/t.md\r\n> " + CANON + "/f.txt\r\nEOF\r\n"),
    ("CR on terminator only", f"cat <<'EOF' > /tmp/t.md{NL}> {CANON}/f.txt{NL}EOF\r"),
    # The operator on the second line, where `_command_line` saw only the first.
    ("heredoc on second line", f"cat /dev/null{NL}cat <<'EOF' > /tmp/t.md{NL}> {CANON}/f.txt{NL}EOF"),
    ("interpreter on second line", f"cat /dev/null{NL}bash <<'EOF'{NL}{R}{NL}EOF"),
]

# Allowlisted consumers that DO write canon, each with a strippable heredoc.
# This is the overlap the differential predicate actually needs -- body removal
# is exercised AND real bash reaches the marker -- and it is where the claim
# "body text only is ever removed, so a path on the command line survives" is
# put under load. These twelve are ONE invariant instanced twelve times, over a
# SUBSET of `CONSUMERS` (`rev`, `base64` and `md5sum` are not among them): they
# load the predicate's `bash_writes` conjunct heavily, but they add one family
# to its diversity, not twelve.
_INERT_CONSUMER_WRITES = [
    ("absolute cat tee canon", f"/bin/cat <<'EOF' | tee {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("head append to canon", f"head <<'EOF' >> {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("tail out to canon", f"tail <<'EOF' > {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("sha256sum out to canon", f"sha256sum <<'EOF' > {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("nl out to canon", f"nl <<'EOF' > {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("sort out to canon", f"sort <<'EOF' > {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("uniq out to canon", f"uniq <<'EOF' > {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("wc glued canon redirect", f"wc <<'EOF' >{CANON}/{MARKER}{NL}x{NL}EOF"),
    ("here-string append canon", f'cat <<< "x" >> {CANON}/{MARKER}'),
    ("absolute tee -a canon", f"cat <<'EOF' | /usr/bin/tee -a {CANON}/{MARKER}{NL}x{NL}EOF"),
    ("bare delim inert body canon", f"cat <<EOF > {CANON}/{MARKER}{NL}plain{NL}EOF"),
    ("here-string bare operand canon", f"cat <<< x > {CANON}/{MARKER}"),
]

_BOUND_ASYMMETRY = [
    ("amp after non-first-line heredoc", f"cat f0{NL}cat <<EOF & {R}{NL}body{NL}EOF"),
    ("and-and after non-first line", f"cat f0{NL}cat <<EOF && {R}{NL}body{NL}EOF"),
    ("amp on first line", f"cat <<EOF & {R}{NL}body{NL}EOF"),
    ("CR before terminator", f"cat <<EOF{NL}body1\r{NL}{R}{NL}EOF"),
]

# Write verbs on the COMMAND LINE, where body removal must change nothing at all.
_COMMAND_LINE_WRITERS = [
    ("sed -i with heredoc", f"sed -i 's/x/y/' {CANON}/seed.txt <<'EOF'{NL}x{NL}EOF"),
    ("cp dest canon", f"cp /tmp/junk.txt {CANON}/{MARKER}"),
    ("mv dest canon", f"mv /tmp/junk.txt {CANON}/{MARKER}"),
    ("git commit after heredoc", f"cat <<'EOF' > /tmp/m.txt{NL}msg{NL}EOF{NL}git commit -m x"),
    ("exec fd redirect", f"exec 3> {CANON}/{MARKER}"),
    ("exec fd append", f"exec 3>> {CANON}/{MARKER}"),
    ("dd of canon", f"dd of={CANON}/{MARKER} <<'EOF'{NL}x{NL}EOF"),
    ("noclobber override", f"echo hi >| {CANON}/{MARKER}"),
    ("sed w command", f"sed 'w {CANON}/{MARKER}' <<'EOF'{NL}x{NL}EOF"),
    ("awk print redirect", f"awk '{{print > \"{CANON}/{MARKER}\"}}' <<'EOF'{NL}x{NL}EOF"),
    # "sort -o" is one of the nine named pre-existing bypasses. The heredoc-
    # bearing forms above ("sort -o canon", "sort -o canon in pipe") test that
    # body removal leaves the -o argument visible; this bare form has no
    # heredoc at all, showing the write vector is untouched by stripping
    # because there is nothing for the stripper to act on in the first place.
    ("sort -o bare no heredoc", f"sort -o {CANON}/{MARKER} /dev/null"),
]

# The false positives the stage exists to remove: bash writes NOTHING, the guard
# denied before, and must allow after.
FALSE_POSITIVES = [
    ("fp blockquote gt", f"cat > /tmp/x.md <<'EOF'{NL}> quoted line{NL}EOF"),
    ("fp blockquote gtgt", f"cat > /tmp/x.md <<'EOF'{NL}>> quoted{NL}EOF"),
    ("fp here-string operand", 'cat <<< "> notes.txt"'),
    ("fp git commit mention", f"cat > /tmp/x.md <<'EOF'{NL}run git commit here{NL}EOF"),
    ("fp absolute canon path", f"cat > /tmp/x.md <<'EOF'{NL}> {CANON}/f.txt{NL}EOF"),
    # Delimiter UNQUOTED sibling of "fp absolute canon path": clause (vi) calls
    # the body inert either because the delimiter is quoted, OR because the
    # body has no `$`/backtick/backslash -- this exercises the second branch.
    ("fp bare plain unquoted delim", f"cat > /tmp/x.md <<EOF{NL}> {CANON}/f.txt{NL}EOF"),
]

CASES = (
    _PARSE_DESYNC
    + _EXECUTED_BODY
    + _INTERPRETER_CONSUMERS
    + _INSIDE_RECOGNIZED_SHAPE
    + _DELIMITER_QUOTING
    + _EXPANSION_AND_TERMINATOR
    + _DELIMITER_BOUNDARY
    + _REVIEWER_FAMILIES
    + _INERT_CONSUMER_WRITES
    + _BOUND_ASYMMETRY
    + _COMMAND_LINE_WRITERS
    + FALSE_POSITIVES
)


# --- oracle + guard drivers ------------------------------------------------

def _bash_available() -> bool:
    return shutil.which("bash") is not None


def bash_writes(command: str) -> bool:
    """Ground truth: does real bash create the marker under the mapped canon dir?

    The command runs in a throwaway directory with `@CANON@` pointed at it, so a
    construction that "writes canon" writes there and nowhere else. stdin is
    /dev/null -- a consumer left reading a terminal would otherwise hang the run.

    Nothing is caught here, deliberately. A blanket `except Exception: return
    False` would score a `TimeoutExpired` as "this construction writes nothing",
    which silently drops the case out of the differential predicate -- the one
    conjunct that makes it a control. A payload that times out is a broken
    payload and must fail the run loudly. Note that a shell syntax error is NOT
    an exception: `bash -c` reports it on stderr and exits nonzero, which is a
    perfectly good "wrote nothing" observation and is what several delimiter
    cases below rely on.
    """
    sandbox = tempfile.mkdtemp(prefix="shell-tokens-oracle-")
    try:
        subprocess.run(
            ["bash", "-c", command.replace(CANON, sandbox)],
            cwd=sandbox,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=BASH_TIMEOUT_S,
        )
        marker = os.path.join(sandbox, MARKER)
        if os.path.exists(marker):
            return True
        time.sleep(SETTLE_S)  # bounds the detached-write blind spot
        return os.path.exists(marker)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def guard_denies(canon: Path, command: str, cwd: Path) -> bool:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command.replace(CANON, str(canon))},
        "cwd": str(cwd),
    }
    return guard_hook.decide(payload) is not None


@pytest.fixture(scope="module")
def canon(tmp_path_factory):
    """A hermetic stand-in for the canonical checkout: a real git repo the guard
    recognizes as the primary Core worktree."""
    root = tmp_path_factory.mktemp("oracle") / "core"
    root.mkdir()
    env = {**os.environ, **GIT_ENV}
    subprocess.run(["git", "init", "--quiet", "-b", "main", "."], cwd=root, env=env, check=True)
    (root / "seed.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, env=env, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "seed"], cwd=root, env=env, check=True)
    previous = os.environ.get("CLAUDE_INSTRUCTIONS_REPO")
    os.environ["CLAUDE_INSTRUCTIONS_REPO"] = str(root)
    try:
        yield root
    finally:
        if previous is None:
            os.environ.pop("CLAUDE_INSTRUCTIONS_REPO", None)
        else:
            os.environ["CLAUDE_INSTRUCTIONS_REPO"] = previous


def test_case_table_is_large_enough():
    """A shrinking table is the cheapest way to make this control pass falsely.

    This is the CORPUS floor and the weakest of the three: breadth of grammar
    pointed at the rule, not control strength. The floors that carry the control
    are EXERCISED and BASH-REACHED, asserted in the differential test below.
    """
    assert len(CASES) >= 153, len(CASES)  # measured 186
    assert len({name for name, _ in CASES}) == len(CASES), "duplicate case names"


def _guard_decision_with(transform, canon, command, cwd) -> bool:
    """`guard_denies(canon, command, cwd)` as `decide()` would compute it if its
    Bash branch's `shell_tokens.neutralize_heredoc_constructs` call resolved to
    `transform` instead -- swaps the module attribute for the one call, then
    restores it, so both of `decide()`'s call sites (its own, and the one
    inside `bash_write_targets.command_write_targets`, which reaches the same
    module object via `from . import shell_tokens`) see `transform` uniformly."""
    saved = shell_tokens.neutralize_heredoc_constructs
    shell_tokens.neutralize_heredoc_constructs = transform
    try:
        return guard_denies(canon, command, cwd)
    finally:
        shell_tokens.neutralize_heredoc_constructs = saved


@pytest.mark.skipif(not _bash_available(), reason="no bash: oracle has no ground truth")
def test_body_removal_never_turns_a_real_write_from_deny_into_allow(canon):
    """The differential predicate, over every construction in the table.

    `decide()`'s Bash branch calls `shell_tokens.neutralize_heredoc_constructs`
    UNCONDITIONALLY now, on every command it receives -- so a plain
    `guard_denies(canon, raw, cwd)` no longer means "the guard with no heredoc
    handling at all": it already reflects today's shipped transform, on both
    sides of any comparison built from it alone. The claim this stage actually
    makes is narrower and comparative -- did SWITCHING decide()'s internal call
    from `strip_heredoc_bodies` to `neutralize_heredoc_constructs` ever turn a
    real write from denied to allowed -- so that is what this test measures:
    `_guard_decision_with(shell_tokens.strip_heredoc_bodies, ...)` reproduces
    what decide() used to compute (BEFORE this stage), and a plain
    `guard_denies(...)` is what it computes today (AFTER). Reusing `decide()`
    itself as the oracle of guard behaviour avoids hand-reimplementing
    `_is_git_commit`/`_canon_bash_write`'s call sequence a second time.

    Two names, "write then exec" and "tee then source", are EXPECTED to flip
    from denied to allowed and are excluded from `regressions` rather than
    failing the run: both are the accepted clause-(v) trade (a heredoc body
    persisted to a file and executed by a LATER statement, which dropping
    clause (v) for neutralization deliberately stops refusing on) -- the same
    trade `test_heredoc_body_persisted_and_run_by_later_statement_now_allows`
    in `test_guard_canon_bash_writes.py` pins directly. Silently including
    them here would make this test and that one contradict each other.
    """
    ACCEPTED_CLAUSE_V_TRADE = {"write then exec", "tee then source"}
    regressions = []
    exercised = 0
    bash_reached = 0
    for name, raw in CASES:
        neutralized = shell_tokens.neutralize_heredoc_constructs(raw)
        if neutralized == raw:
            # Today's shipped transform is a no-op on this input, so BEFORE and
            # AFTER reach `decide()` having had the identical construct handling
            # applied (none), and no oracle run can distinguish the two. Proof,
            # not sampling.
            continue
        exercised += 1
        if not bash_writes(raw):
            continue
        bash_reached += 1
        if name in ACCEPTED_CLAUSE_V_TRADE:
            continue
        for cwd in (canon, Path("/tmp")):
            before = _guard_decision_with(shell_tokens.strip_heredoc_bodies, canon, raw, cwd)
            after = guard_denies(canon, raw, cwd)
            if before and not after:
                regressions.append(f"{name} (cwd={cwd})")
    assert not regressions, "the strip-to-neutralize migration widened the guard: " + "; ".join(regressions)
    # Neither number is derivable from `len(CASES)`, and a rule change that
    # quietly stopped recognizing most of the table would leave the assertion
    # above vacuously true, so both counts are asserted. They are MEASURED, not
    # chosen: to re-derive them, add a `print` beside these asserts and run the
    # test with `-s`; both are plain loop counters over `CASES` and nothing else
    # feeds them.
    #
    # The two are asserted DIFFERENTLY, because only one of them can move on its
    # own. `exercised` is a pure function of `neutralize_heredoc_constructs` and
    # the table -- no environment feeds it -- so any drift in it IS a rule change
    # and there is no honest slack to grant: it is pinned exactly. `bash_reached`
    # additionally depends on what the local shell and coreutils really do, so it
    # carries a floor a little under the measured value (37).
    assert exercised == 93, (
        f"{exercised} of {len(CASES)} constructions were acted on by "
        "neutralize_heredoc_constructs, expected exactly 93: this count cannot "
        "move without a change to the recognition rule or the table"
    )
    assert bash_reached >= 30, (
        f"only {bash_reached} of {exercised} acted-on constructions actually wrote canon: "
        "the predicate's `bash_writes` conjunct is barely loaded"
    )


@pytest.mark.skipif(not _bash_available(), reason="no bash: oracle has no ground truth")
def test_named_false_positives_are_removed(canon):
    """The other direction: the cases the stage exists to fix really do flip.

    This asserts only the permanent claim -- bash writes nothing, so the FIXED
    guard must allow the command as given (`decide()` already neutralizes
    internally at its Bash entry point, so `raw` and `neutralized` reach the
    same decision). It does NOT assert "denied before neutralization": that
    held only against the pre-fix guard and was checked once, by hand, against
    `git show HEAD~1:scripts/hook-guard-canon-readonly.py` -- see the commit
    message for that result. A standing assertion of the old behaviour would
    fail forever once the fix landed. `neutralized`, not `stripped`, because
    `neutralize_heredoc_constructs` is what `decide()` actually calls now --
    see `_guard_decision_with`'s docstring above for why the two are not
    interchangeable here.
    """
    for name, raw in FALSE_POSITIVES:
        assert not bash_writes(raw), f"{name}: oracle says this really writes"
        assert not guard_denies(canon, raw, canon), f"{name}: still denied"
        neutralized = shell_tokens.neutralize_heredoc_constructs(raw)
        assert not guard_denies(canon, neutralized, canon), f"{name}: still denied after neutralization"


@pytest.mark.skipif(not _bash_available(), reason="no bash: oracle has no ground truth")
def test_oracle_goes_red_against_a_superseded_rule(canon):
    """Two-directional control: the oracle must FAIL a rule built to an earlier
    formulation, or it is not measuring anything.

    `_superseded_strip` is the round-8 rule -- clauses (i)-(v) without the
    delimiter-quoting clause (vi), so it strips a bare-delimiter body whose `$(...)`
    the SHELL expands and executes before any consumer runs.
    """
    def _superseded_strip(command: str) -> str:
        if not shell_tokens._recognized(command):
            return command
        saved = shell_tokens._body_inert
        shell_tokens._body_inert = lambda delimiter_quoted, text: True
        try:
            residue = shell_tokens._strip_bodies(command)
        finally:
            shell_tokens._body_inert = saved
        if residue != command and shell_tokens._holds_multiple_statements(residue):
            return command
        return residue

    caught = []
    for name, raw in CASES:
        stripped = _superseded_strip(raw)
        if stripped == raw or not bash_writes(raw):
            continue
        for cwd in (canon, Path("/tmp")):
            if guard_denies(canon, raw, cwd) and not guard_denies(canon, stripped, cwd):
                caught.append(name)
    assert caught, "the oracle passed a rule known to be unsound — it measures nothing"


# --- D1/D1a/D1c: coherence between the two appliers, and independence from the
# shared producer they both consume ---------------------------------------

def _recognized_by_strip(raw: str) -> bool:
    return (
        shell_tokens._recognized(raw, shell_tokens.CONSUMERS)
        and shell_tokens.strip_heredoc_bodies(raw) != raw
    )


def test_strip_and_neutralize_agree_on_command_line_tokens():
    """D1: for every construction BOTH appliers act on (i.e. `strip_heredoc_
    bodies` did not bail out on clause (iv)'s narrower consumer set or clause
    (v)'s residue check), `shlex.split` of the two outputs must agree --
    `_strip_bodies` COLLAPSES a construct to a single separator character,
    `neutralize_heredoc_constructs` BLANKS it to spaces of the same length,
    and both are pure whitespace, so a lexer that only sees WORDS cannot tell
    them apart. Run over both corpora, `CASES` and `FALSE_POSITIVES`, since
    the two appliers must agree identically on both the everyday grammar
    sweep and the named motivating false positives.

    D1b -- what this test CANNOT catch, stated rather than left implicit:
    both outputs are computed from the SAME `_removal_regions` walk, so a bug
    that NARROWS what that walk recognizes (stops finding a construct it used
    to) shrinks `_strip_bodies` and `neutralize_heredoc_constructs` together
    -- their tokens would still agree, now vacuously, against `raw`'s own
    unmodified tokens. Token equivalence between two consumers of one shared
    producer is a coherence check on the two APPLIERS, not a correctness
    check on the producer itself; only `test_strip_bodies_matches_a_frozen_
    independent_reimplementation` below (D1c), built from a walk that shares
    nothing with `_removal_regions`, can catch a narrowed producer.
    """
    for name, raw in CASES + FALSE_POSITIVES:
        if not _recognized_by_strip(raw):
            continue
        stripped = shell_tokens.strip_heredoc_bodies(raw)
        neutralized = shell_tokens.neutralize_heredoc_constructs(raw)
        assert shlex.split(stripped) == shlex.split(neutralized), (
            f"{name}: strip_heredoc_bodies and neutralize_heredoc_constructs "
            f"disagree on command-line tokens\n  stripped:    {stripped!r}\n"
            f"  neutralized: {neutralized!r}"
        )


# Floors on the D1a grid below, all four measured against the grid this file
# actually generates (234 cells) and re-measured whenever an axis changes.
#
# The three ACTED_ floors count cells `neutralize_heredoc_constructs` acts on,
# one per value clause (iv) can take, and pin CLASS MEMBERSHIP: the widened
# allowlist really does reach a `NON_SHELL_CONSUMERS`-only name, and really
# does not reach a name on neither list. They cannot catch a narrowed producer
# -- the neutralizer acts as soon as the walk finds ONE construct, so dropping
# every construct after the first leaves all three counts untouched.
# STRIPPED_FLOOR_TEE is the floor that can: `strip_heredoc_bodies` keeps clause
# (v), so a walk that stops early leaves the later constructs' body lines in
# the residue, the multi-statement check rejects it, and the count falls (30 ->
# 18 measured, against the reviewer's `return regions` narrowing of the `<<<`
# branch). Two different questions, deliberately two different numbers.
ACTED_FLOOR_TEE = 78
ACTED_FLOOR_NON_SHELL = 78
ACTED_FLOOR_UNKNOWN = 0
STRIPPED_FLOOR_TEE = 30

# The same stripper measurement, split by construct count -- asserted EXACTLY,
# not as a floor, because the split is where the grammar claim lives: a
# construction is strippable iff the walk reaches every construct in it, which
# (clause (v) rejecting the leftover body lines of a walk that stopped early)
# happens iff at most ONE of its operators is a `<<`/`<<-`. That is 3 of 3
# sequences at count 1, 5 of 9 at count 2 and 7 of 27 at count 3, each doubled
# by the operand axis. A bare sum would let a loss at one count hide behind a
# gain at another.
STRIPPED_ACTED_BY_COUNT = {1: 6, 2: 10, 3: 14}

# One class per value clause (iv) can take for `neutralize_heredoc_constructs`:
# a plain `CONSUMERS` member, a `NON_SHELL_CONSUMERS`-only member, and a name
# on neither. `myunknowncmd` rather than a real-but-unlisted binary (`curl`,
# say): the unknown class is asserted at EXACTLY zero, so it must rest on a
# name that cannot quietly join `CONSUMERS` one day and turn a real assertion
# into a vacuous one.
_GRID_CONSUMERS = {"tee": "tee", "non_shell": "python3", "unknown": "myunknowncmd"}

# The three operator forms, and every ORDERED sequence of one, two or three of
# them -- 3 + 9 + 27 = 39. Ordered, not "the same form repeated": bash's
# grammar is asymmetric (unbounded `<<<` repetitions, then at most ONE
# `<<`/`<<-`, which ends the walk), so `<<<` then `<<` and `<<` then `<<<` are
# different constructions and only a mixed sequence exercises the difference.
_GRID_FORMS = ("<<", "<<-", "<<<")
_GRID_SEQUENCES = tuple(
    seq
    for count in (1, 2, 3)
    for seq in itertools.product(_GRID_FORMS, repeat=count)
)

# Body shapes, rotated across the generated constructs rather than crossed as a
# fifth axis: each is a shape whose MISREADING as command-line syntax is the
# whole point of the neutralizer (a redirect, a git commit, an apostrophe that
# unbalances `shlex`), and each appears in cells of every class, count and
# operator form.
_GRID_BODIES = (
    "plain text",
    "> looks/like/a/redirect",
    "git commit -m nope",
    "other people's apostrophe",
)


def _grid_command(consumer: str, forms: tuple[str, ...], operand: bool, body_start: int) -> str:
    """One grid cell: `consumer`, one construct per entry of `forms` in order,
    optionally a trailing operand after the last operator, and the bodies the
    `<<`/`<<-` entries need, in the order bash reads them."""
    head = consumer
    bodies = []
    for index, form in enumerate(forms):
        body = _GRID_BODIES[(body_start + index) % len(_GRID_BODIES)]
        if form == "<<<":
            head += f" <<<hs{index + 1}"
            continue
        delimiter = f"D{index + 1}"
        head += f" {form}'{delimiter}'"
        indent = "\t" if form == "<<-" else ""  # `<<-` strips leading tabs
        bodies.append(f"{indent}{body}\n{indent}{delimiter}")
    if operand:
        head += f" {CANON}/{MARKER}"
    return head + ("\n" + "\n".join(bodies) if bodies else "")


def _grid_cells() -> tuple[tuple[str, tuple[str, ...], bool, str], ...]:
    cells: list[tuple[str, tuple[str, ...], bool, str]] = []
    for cls, consumer in _GRID_CONSUMERS.items():
        for forms in _GRID_SEQUENCES:
            for operand in (True, False):
                cells.append(
                    (cls, forms, operand, _grid_command(consumer, forms, operand, len(cells)))
                )
    return tuple(cells)


GRID_CELLS = _grid_cells()
GRID_COMMANDS = tuple(cell[3] for cell in GRID_CELLS)


def _widened_removal(raw: str) -> str:
    """The REMOVAL (collapse) form of `raw` under the same widened allowlist
    `neutralize_heredoc_constructs` uses -- i.e. what `strip_heredoc_bodies`
    would produce if clause (iv) reached `NON_SHELL_CONSUMERS` and clause (v)
    were dropped. The reference side for grid cells the shipped stripper
    refuses outright, which would otherwise have no removal form to compare a
    blanking against."""
    consumers = shell_tokens.CONSUMERS | shell_tokens.NON_SHELL_CONSUMERS
    if not shell_tokens._recognized(raw, consumers):
        return raw
    regions = shell_tokens._removal_regions(raw, consumers)
    if regions is None:
        return raw
    return shell_tokens._apply_regions(raw, regions)


def _tokens_or_raise(text: str):
    """`shlex.split(text)`, or the exception type it raised -- so two texts can
    be asserted equal as a PAIR: equal tokens, or equally untokenizable. A body
    holding an unbalanced apostrophe really does make `shlex` raise, and on a
    cell where neither transform acts both sides must raise alike rather than
    the assertion being skipped."""
    try:
        return ("tokens", shlex.split(text))
    except ValueError as exc:
        return ("raise", type(exc).__name__)


def test_neutralization_equivalence_grid():
    """D1a: a GENERATED grid over the four axes the shape space actually has --
    operator form (`<<`, `<<-`, `<<<`), construct count (1, 2, 3, as ordered
    sequences over the three forms), consumer class (clause (iv)'s three), and
    a trailing operand after the last operator (present / absent) -- 3 x 39 x 2
    = 234 cells.

    The multi-construct rows are the load-bearing ones: a walk that stops after
    the first construct is a real DENY-to-ALLOW widening on commands bash
    genuinely accepts (`tee <<<aaa <<<bbb f`), and no earlier control saw one,
    since the oracle corpus contains no multi-here-string case at all.

    Asserted per cell: `neutralize_heredoc_constructs` preserves length, and
    its output pairs with the corresponding REMOVAL form under `shlex` --
    equal tokens, or equally untokenizable. Asserted over the grid: the three
    class floors, plus the stripper floor that a narrowed producer breaks (see
    the constants' own comment for which floor answers which question).
    """
    assert len(_GRID_SEQUENCES) == 39, len(_GRID_SEQUENCES)
    assert len(GRID_CELLS) == 234, len(GRID_CELLS)

    acted = {cls: 0 for cls in _GRID_CONSUMERS}
    stripped_acted = {cls: 0 for cls in _GRID_CONSUMERS}
    stripped_by_count = {count: 0 for count in STRIPPED_ACTED_BY_COUNT}
    for cls, forms, operand, raw in GRID_CELLS:
        neutralized = shell_tokens.neutralize_heredoc_constructs(raw)
        assert len(neutralized) == len(raw), (cls, forms, operand, raw)
        stripped = shell_tokens.strip_heredoc_bodies(raw)
        reference = stripped if stripped != raw else _widened_removal(raw)
        assert _tokens_or_raise(neutralized) == _tokens_or_raise(reference), (
            f"{cls} {forms} operand={operand}: blanking and removal disagree\n"
            f"  raw:         {raw!r}\n  neutralized: {neutralized!r}\n"
            f"  removal:     {reference!r}"
        )
        if neutralized != raw:
            acted[cls] += 1
        if stripped != raw:
            stripped_acted[cls] += 1
            stripped_by_count[len(forms)] += 1

    assert acted["tee"] >= ACTED_FLOOR_TEE, acted
    assert acted["non_shell"] >= ACTED_FLOOR_NON_SHELL, acted
    assert acted["unknown"] == ACTED_FLOOR_UNKNOWN, acted
    assert stripped_acted["tee"] >= STRIPPED_FLOOR_TEE, stripped_acted
    assert stripped_by_count == STRIPPED_ACTED_BY_COUNT, stripped_by_count
    # The stripper's clause (iv) is the NARROW set, so it must never act
    # outside `CONSUMERS` -- the mirror image of the widened floors above.
    assert stripped_acted["non_shell"] == 0, stripped_acted
    assert stripped_acted["unknown"] == 0, stripped_acted


def test_heredoc_construct_spans_are_the_neutralizers_own_span_view():
    """S1: `heredoc_construct_spans` is a public export with no in-tree caller
    yet (stage 3's diff-region reader is its first), so nothing but a direct
    test pins it. Three claims: a span covers exactly the text its construct
    occupies, blanking the spans by hand reproduces
    `neutralize_heredoc_constructs` byte for byte over the whole D1a grid (the
    view and the applier cannot drift), and doubt yields `[]` rather than a
    partial list -- the all-or-nothing contract, on both the consumer and the
    body-expansion doubt points.
    """
    multi = "tee <<<aaa <<<bbb /tmp/f"
    spans = shell_tokens.heredoc_construct_spans(multi)
    assert spans == [(4, 10), (11, 17)], spans
    assert [multi[start:end] for start, end in spans] == ["<<<aaa", "<<<bbb"]

    heredoc = "cat <<'EOF' /tmp/f\nbody\nEOF"
    assert [
        heredoc[start:end] for start, end in shell_tokens.heredoc_construct_spans(heredoc)
    ] == ["<<'EOF'", "\nbody\nEOF"]

    # `[]` on doubt: a shell consumer (clause (iv)), and an unquoted delimiter
    # whose body the shell itself would expand (clause (vi)).
    assert shell_tokens.heredoc_construct_spans("bash <<'EOF'\necho hi\nEOF") == []
    assert shell_tokens.heredoc_construct_spans("cat <<EOF\n$(id)\nEOF") == []

    for raw in GRID_COMMANDS:
        by_hand = raw
        for start, end in reversed(shell_tokens.heredoc_construct_spans(raw)):
            # Blanked here rather than through the module's own `_blank_region`:
            # a mutation of that helper would move both sides of the comparison
            # together and this control would never see it.
            blanked = "".join(ch if ch == "\n" else " " for ch in raw[start:end])
            by_hand = by_hand[:start] + blanked + by_hand[end:]
        assert by_hand == shell_tokens.neutralize_heredoc_constructs(raw), raw


def _frozen_strip_bodies(command: str) -> str:
    """D1c: independent reimplementation of `strip_heredoc_bodies`, frozen at
    the shape the pre-Stage-2 character-by-character scanner had before it
    was inverted into `_removal_regions` + `_apply_regions`: it walks the
    SAME doubt points, via the shared and UNCHANGED-by-the-refactor predicate
    helpers (`_recognized`, `_pipeline_consumers_ok`, `_body_inert`,
    `_DELIMITER_WORD`, `_WORD_END`, `_holds_multiple_statements`), but builds
    output text directly and never calls `_removal_regions` or
    `_apply_regions`.

    This is the control D1 cannot be: two appliers that both consume
    `_removal_regions`'s output necessarily agree with EACH OTHER even if
    that shared producer were narrowed -- both would silently shrink together
    and their outputs would still match. Only a separate walk that shares
    nothing with the producer can catch that; this function, and the
    byte-identity test built on it, are that separate walk.
    """
    if not shell_tokens._recognized(command, shell_tokens.CONSUMERS):
        return command
    residue = _frozen_walk(command)
    if residue != command and shell_tokens._holds_multiple_statements(residue):
        return command
    return residue


def _frozen_walk(command: str) -> str:
    out: list[str] = []
    pos = 0
    i = 0
    n = len(command)
    quote = None
    while i < n:
        c = command[i]
        if quote is None and c == "\\":
            i += 2
            continue
        if quote is None and c in "'\"":
            quote = c
            i += 1
            continue
        if quote == '"' and c == "\\":
            i += 2
            continue
        if quote and c == quote:
            quote = None
            i += 1
            continue
        if quote is None:
            if c == "#" and (i == 0 or command[i - 1] in " \t\n"):
                j = command.find("\n", i)
                j = n if j < 0 else j
                i = j
                continue
            if command.startswith("<<<", i):
                if not shell_tokens._pipeline_consumers_ok(command, i, shell_tokens.CONSUMERS):
                    return command
                j = i + 3
                while j < n and command[j] == " ":
                    j += 1
                if j < n and command[j] in "'\"":
                    operand_quote = command[j]
                    k = command.find(operand_quote, j + 1)
                    if k == -1:
                        return command
                    if not shell_tokens._body_inert(operand_quote == "'", command[j + 1:k]):
                        return command
                    j = k + 1
                    if j < n and command[j] not in shell_tokens._WORD_END:
                        return command
                else:
                    start = j
                    while j < n and command[j] not in shell_tokens._WORD_END:
                        j += 1
                    if not shell_tokens._body_inert(False, command[start:j]):
                        return command
                out.append(command[pos:i])
                out.append(" ")
                pos = j
                i = j
                continue
            if command.startswith("<<", i):
                if not shell_tokens._pipeline_consumers_ok(command, i, shell_tokens.CONSUMERS):
                    return command
                j = i + 2
                if j < n and command[j] == "-":
                    j += 1
                while j < n and command[j] == " ":
                    j += 1
                match = shell_tokens._DELIMITER_WORD.match(command[j:])
                if not match:
                    return command
                backslash, open_quote, word, close_quote = match.groups()
                if open_quote and open_quote != close_quote:
                    return command
                delimiter_quoted = bool(backslash) or bool(open_quote)
                j += match.end()
                if j < n and command[j] not in shell_tokens._WORD_END:
                    return command
                lines = command[j:].split("\n")
                terminator = None
                for index, line in enumerate(lines[1:], start=1):
                    if line.strip() == word:
                        terminator = index
                        break
                if terminator is None:
                    return command
                if not shell_tokens._body_inert(delimiter_quoted, "\n".join(lines[1:terminator])):
                    return command
                line0_end = j + len(lines[0])
                pre_len = len("\n".join(lines[:terminator + 1]))
                pos_after_terminator = j + pre_len
                body_end = pos_after_terminator + 1 if pos_after_terminator < n else pos_after_terminator
                out.append(command[pos:i])
                pos = j
                out.append(command[pos:line0_end])
                out.append("\n")
                pos = body_end
                return "".join(out) + command[pos:]
        i += 1
    if quote is not None:
        return command
    out.append(command[pos:])
    return "".join(out)


_GRID_CASES = tuple((f"grid cell {index}", raw) for index, raw in enumerate(GRID_COMMANDS))


def test_strip_bodies_matches_a_frozen_independent_reimplementation():
    """D1c: `strip_heredoc_bodies` must agree byte-for-byte with `_frozen_
    strip_bodies` over both corpora AND over the D1a grid. Unlike D1, this
    control shares no code with `_removal_regions` on the reference side, so it
    is the one able to catch a narrowed (or widened) producer -- see D1's
    docstring for why token-equivalence between the two appliers cannot.

    The grid is included because the corpora alone cannot exercise the
    property: neither holds a single multi-here-string construction, so a walk
    narrowed to stop after the first construct stays byte-identical over both
    and this control passes vacuously. The grid's 234 generated cells are where
    the second and third constructs live."""
    for name, raw in CASES + FALSE_POSITIVES + list(_GRID_CASES):
        expected = shell_tokens.strip_heredoc_bodies(raw)
        actual = _frozen_strip_bodies(raw)
        assert actual == expected, (
            f"{name}: frozen reference disagrees with strip_heredoc_bodies\n"
            f"  shipped: {expected!r}\n  frozen:  {actual!r}"
        )


# --- D2/D3: named pins for the shipped guard's actual behaviour today -----

_CASES_BY_NAME = dict(CASES)

MUST_STILL_DENY = [
    "here-string then write",
    "tee canon argv",
    "absolute cat tee canon",
    "here-string bare operand canon",
    "sort out to canon",
    "wc glued canon redirect",
    "two heredocs 1st quoted",
    "amp after non-first-line heredoc",
    "absolute tee canon argv",
]


@pytest.mark.skipif(not _bash_available(), reason="no bash: oracle has no ground truth")
def test_nine_named_constructions_still_deny(canon):
    """D2: nine hand-picked constructions, spanning the parse-desync,
    shape-inside-recognized, inert-consumer-write, delimiter-quoting,
    bound-asymmetry and reviewer-absolute-path families, asserted
    individually rather than folded into the generic loop above -- so a
    future reader sees, by name, which specific attacks this migration is
    pinned not to have opened, without re-deriving them from
    `test_body_removal_never_turns_a_real_write_from_deny_into_allow`'s
    aggregate pass/fail. Picked from constructions this file measured, not
    guessed by name -- e.g. "sort -o canon" looks like it should belong here
    but is one of the nine pre-existing bypasses (see `_COMMAND_LINE_
    WRITERS`'s "sort -o bare no heredoc"): `sort -o` was never a detected
    write verb, heredoc or not, so it is excluded.

    COVERAGE, so the two D2 sets are not read as duplicates: these nine are
    drawn from the PRE-EXISTING corpus, assembled before the multi-construct
    grammar was understood, and measured across them the two-here-string,
    here-string-then-heredoc, heredoc-then-here-string and `<<-` tab-strip
    shapes each appear ZERO times. The shapes this migration newly reaches are
    pinned by construction in `test_nine_enumerated_constructions_still_deny`
    below; this set pins that what the corpus already knew about did not
    regress.
    """
    assert len(MUST_STILL_DENY) == 9, len(MUST_STILL_DENY)
    for name in MUST_STILL_DENY:
        raw = _CASES_BY_NAME[name]
        assert bash_writes(raw), f"{name}: oracle says this does not actually write"
        assert guard_denies(canon, raw, canon), f"{name}: no longer denied"


# The nine constructions the stage's plan enumerates, written out by
# CONSTRUCTION rather than selected by corpus name -- one per shape the
# region-producer inversion newly reaches, so this set cannot silently miss a
# shape the way a name-based selection from an older corpus already did.
ENUMERATED_MUST_STILL_DENY = [
    ("quoted heredoc, tee operand", f"tee <<'EOF' {CANON}/{MARKER}\nhello body\nEOF"),
    ("here-string, tee operand", f"tee <<<data {CANON}/{MARKER}"),
    ("two here-strings", f"tee <<<aaa <<<bbb {CANON}/{MARKER}"),
    ("here-string then heredoc", f"tee <<<aaa <<'EOF' {CANON}/{MARKER}\nbody\nEOF"),
    ("tab-strip heredoc", f"tee <<-'EOF' {CANON}/{MARKER}\n\tbody\n\tEOF"),
    ("shell consumer, body writes", f"bash <<'EOF'\necho x > {CANON}/{MARKER}\nEOF"),
    ("unknown consumer, body writes", f"myunknowncmd <<'EOF'\necho x > {CANON}/{MARKER}\nEOF"),
    ("unquoted delimiter, body expanded", f"python3 - <<EOF\n$(echo x > {CANON}/{MARKER})\nEOF"),
    ("heredoc then here-string", f"tee <<'A' <<<xxx {CANON}/{MARKER}\nb1\nA"),
]

# The one enumerated construction real bash cannot be made to write with: the
# consumer does not exist, so bash reports "command not found" and never runs a
# body it was only ever going to read as stdin data. It is pinned anyway, and
# its oracle claim is asserted in the NEGATIVE direction below, because what it
# pins is the guard's conservatism -- an unknown consumer's body is not TRUSTED,
# so the write named inside it must still deny -- not a measured write.
_NOT_BASH_REACHED = {"unknown consumer, body writes"}


@pytest.mark.skipif(not _bash_available(), reason="no bash: oracle has no ground truth")
def test_nine_enumerated_constructions_still_deny(canon):
    """D2, by construction: the nine commands the plan enumerates, each written
    out in full rather than looked up in the corpus. Eight carry the same
    bash-oracle guard as the name-selected set -- real bash is measured to write
    canon first, so a deny that stopped being a deny is a measured widening and
    not a claim about a command nobody ran. The ninth cannot be bash-reachable
    at all (see `_NOT_BASH_REACHED`) and asserts that explicitly rather than
    quietly dropping the guard.
    """
    assert len(ENUMERATED_MUST_STILL_DENY) == 9, len(ENUMERATED_MUST_STILL_DENY)
    for name, raw in ENUMERATED_MUST_STILL_DENY:
        if name in _NOT_BASH_REACHED:
            assert not bash_writes(raw), f"{name}: now bash-reachable -- give it the oracle guard"
        else:
            assert bash_writes(raw), f"{name}: oracle says this does not actually write"
        assert guard_denies(canon, raw, canon), f"{name}: no longer denied"


_DOUBLE_APPLICATION = f"tee <<'A' <<<xxx {CANON}/{MARKER}\nb1\nA"


def _once_only(transform):
    """`transform` on the FIRST call, then the identity -- the composed hook
    path with its second neutralization collapsed away, which is exactly the
    edit `test_double_application_is_load_bearing` has to be able to see."""
    state = {"used": False}

    def once(command: str) -> str:
        if state["used"]:
            return command
        state["used"] = True
        return transform(command)

    return once


@pytest.mark.skipif(not _bash_available(), reason="no bash: oracle has no ground truth")
def test_double_application_is_load_bearing(canon):
    """R6 item 2: `decide()` neutralizes once itself and `command_write_targets`
    neutralizes AGAIN internally, and the second application is not redundant --
    the walk ends at the first `<<`/`<<-`, so a command carrying a construct
    AFTER one needs two passes before its operand becomes visible. Nothing
    pinned that until now: collapsing the two calls into one is a real
    DENY-to-ALLOW widening on a command real bash writes, and every test stayed
    green.

    Pinned at both levels, since neither alone says it. MECHANISM: one
    application leaves no write target, two produce canon's. HOOK-OBSERVABLE:
    the shipped path denies, and the same path with the second application
    collapsed away (`_once_only`) allows -- a single `neutralize_heredoc_
    constructs` call cannot express the composition, so the count has to be
    asserted through `decide()` itself.
    """
    assert bash_writes(_DOUBLE_APPLICATION), "oracle says this does not actually write"
    command = _DOUBLE_APPLICATION.replace(CANON, str(canon))

    once = bash_write_targets.command_write_targets(command, str(canon))
    twice = bash_write_targets.command_write_targets(
        shell_tokens.neutralize_heredoc_constructs(command), str(canon)
    )
    assert once == [], f"one application already sees a target: {once}"
    assert str(canon / MARKER) in twice, twice

    assert guard_denies(canon, _DOUBLE_APPLICATION, canon), "the shipped path stopped denying"
    collapsed = _guard_decision_with(
        _once_only(shell_tokens.neutralize_heredoc_constructs), canon, _DOUBLE_APPLICATION, canon
    )
    assert not collapsed, (
        "a single application already denies -- this test can no longer tell the two-pass hook "
        "path from a one-pass one, so it has stopped pinning R6 item 2"
    )


@pytest.mark.skipif(not _bash_available(), reason="no bash: oracle has no ground truth")
def test_widened_consumer_body_introduces_no_new_spurious_deny(canon):
    """D3: `ruby`/`node`, added to clause (iv) only for neutralization (never
    for `strip_heredoc_bodies`, which still refuses them), must not gain a
    NEW spurious deny of their own -- a body that merely PRINTS something
    that looks like a canon write must still allow, exactly as the python /
    git-commit-mention false positives already pinned in
    `test_heredoc_body_neutralization.py` do for `python3`. This is a fresh
    construction (a `ruby` `puts`, not `python3`'s `print`), so it exercises
    one of the OTHER names `NON_SHELL_CONSUMERS` added, not a duplicate of
    T1/T3.
    """
    cmd = f"ruby <<'EOF'\nputs \"> {CANON}/should_not_matter\"\nEOF"
    assert not bash_writes(cmd), "oracle says this really writes"
    assert not guard_denies(canon, cmd, canon), "spuriously denied"
