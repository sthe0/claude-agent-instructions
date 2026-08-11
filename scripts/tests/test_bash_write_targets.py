"""Unit rows for `lib/bash_write_targets.py` — the lexer, on its own terms.

WHY THIS FILE EXISTS SEPARATELY from the two hook suites that consume the lexer. Both of
them assert VERDICTS (deny / allow), which is one bit downstream of a list of paths, so a
lexer that reaches the right decision from the wrong candidate set passes them. That is not
hypothetical, it is MEASURED: two mutations of this module — dropping `-m`/`-o`/`-g` from
`_COPY_VALUE_OPTS`, and forcing `dest_is_dir = False` — leave all 146 self-grant rows and all
34 canon rows passing, and die only here. Both mutations OVER-emit while still emitting the
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
    # `sed -i` emits its SCRIPT token as a candidate as well, which is the over-emission this
    # module's contract permits: `/w/s/a/b/` is a path nothing is on, so it answers nothing.
    # Pinned rather than tidied, because a reader who does not expect it will read the second
    # entry as the only one and mis-set their own policy.
    ("sed -i s/a/b/ ~/.claude-agent/settings.json",
     ["/w/s/a/b/", "/home/u/.claude-agent/settings.json"]),
    # A bare `~` resolves, and the ambiguous-token rule applies on top of it.
    ("cp s.json ~", ["/home/u", "/home/u/s.json"]),
    # `~` anywhere but the front is NOT a home reference and must not become one.
    ("cp s.json d/~x", ["/w/d/~x", "/w/d/~x/s.json"]),
])
def test_a_leading_tilde_is_expanded(command, expected, monkeypatch):
    monkeypatch.setenv("HOME", "/home/u")
    assert command_write_targets(command, CWD) == expected


@pytest.mark.parametrize("command, expected", [
    # `$VAR` IS NOT EXPANDED, AND THAT IS A DECISION -- pinned so it cannot drift into one by
    # accident in either direction. THE HOOK'S ENVIRONMENT IS NOT THE COMMAND'S: a command
    # supplies its own (`HOME=/x cp ...`) or names a variable exported in a shell this process
    # never saw, so substituting what this process happens to hold resolves the token to a
    # path the command may never write, and a consumer then answers confidently about the
    # wrong file. So the token joins under the cwd like any other relative one, and each
    # consumer names it as a residual instead. `_abs` carries the argument in full.
    ("cp s.json $HOME/.claude/settings.json",
     ["/w/$HOME/.claude/settings.json", "/w/$HOME/.claude/settings.json/s.json"]),
    ("cp s.json ${HOME}/.claude/", ["/w/${HOME}/.claude/s.json"]),
    ("echo x > $HOME/.claude/settings.json", ["/w/$HOME/.claude/settings.json"]),
])
def test_a_variable_is_not_expanded(command, expected, monkeypatch):
    monkeypatch.setenv("HOME", "/home/u")
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
    # `sed` without an in-place flag writes nothing.
    ("sed s/a/b/ f.txt", []),
    ("sed -i s/a/b/ f.txt", ["/w/s/a/b/", "/w/f.txt"]),
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


# THE OTHER CONSUMER'S BEHAVIOUR CHANGED TOO, and it has no row of its own.
# `hook-guard-canon-readonly.py` shares this lexer, so the expansion makes it STRICTER: a
# `cp evil.md ~/canon-mirror/doc.md` aimed at a registered canon root was measured ALLOW
# before the expansion and DENY after (subprocess, fake `HOME`, `CLAUDE_CANON_ROOTS_FILE`,
# cwd outside canon), while `~/outside-of-canon.md` stayed ALLOW -- stricter in the right
# direction, no over-denial. Its 34 rows do not notice either way: all 34 pass with the
# expansion removed. That consumer needs a `~`-into-canon row of its own, and adding one is
# outside this change's edit scope; it is reported as a recommendation instead.
