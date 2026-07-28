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
import os
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
from lib import shell_tokens  # noqa: E402

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
# put under load across every writing member of `CONSUMERS`.
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


@pytest.mark.skipif(not _bash_available(), reason="no bash: oracle has no ground truth")
def test_body_removal_never_turns_a_real_write_from_deny_into_allow(canon):
    """The differential predicate, over every construction in the table."""
    regressions = []
    exercised = 0
    bash_reached = 0
    for name, raw in CASES:
        stripped = shell_tokens.strip_heredoc_bodies(raw)
        if stripped == raw:
            # Identical input reaches the guard, so its decision is identical and
            # no oracle run can distinguish the two. Proof, not sampling.
            continue
        exercised += 1
        if not bash_writes(raw):
            continue
        bash_reached += 1
        for cwd in (canon, Path("/tmp")):
            if guard_denies(canon, raw, cwd) and not guard_denies(canon, stripped, cwd):
                regressions.append(f"{name} (cwd={cwd})")
    assert not regressions, "body removal widened the guard: " + "; ".join(regressions)
    # Neither number is derivable from `len(CASES)`, and a rule change that
    # quietly stopped recognizing most of the table would leave the assertion
    # above vacuously true, so both floors are asserted. They are MEASURED, not
    # chosen: as committed, this loop counts exercised=75 and bash-reached=28
    # over a corpus of 186. To re-derive them, add a `print` beside these asserts
    # and run the test with `-s`; both are plain loop counters over `CASES` and
    # nothing else feeds them. The floors sit a little under the
    # measured values so one incidental case whose bash verdict shifts (a missing
    # `zsh`, a differently-built coreutils) does not break the build, and far
    # enough above the pre-review values -- exercised=55, bash-reached=15 -- that
    # a regression back to those fails here.
    assert exercised >= 70, f"only {exercised} of {len(CASES)} constructions were stripped at all"
    assert bash_reached >= 25, (
        f"only {bash_reached} of {exercised} stripped constructions actually wrote canon: "
        "the predicate's `bash_writes` conjunct is barely loaded"
    )


@pytest.mark.skipif(not _bash_available(), reason="no bash: oracle has no ground truth")
def test_named_false_positives_are_removed(canon):
    """The other direction: the cases the stage exists to fix really do flip.

    This asserts only the permanent claim -- bash writes nothing, so the FIXED
    guard must allow the command as given (`decide()` already strips
    internally at its Bash entry point, so `raw` and `stripped` reach the same
    decision). It does NOT assert "denied before stripping": that held only
    against the pre-fix guard and was checked once, by hand, against
    `git show HEAD~1:scripts/hook-guard-canon-readonly.py` -- see the commit
    message for that result. A standing assertion of the old behaviour would
    fail forever once the fix landed.
    """
    for name, raw in FALSE_POSITIVES:
        assert not bash_writes(raw), f"{name}: oracle says this really writes"
        assert not guard_denies(canon, raw, canon), f"{name}: still denied"
        stripped = shell_tokens.strip_heredoc_bodies(raw)
        assert not guard_denies(canon, stripped, canon), f"{name}: still denied after stripping"


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
