# Verdict: the canon guard's deny of a bare `git commit` under cwd-reset is CORRECT

Issue #44's residual asks whether `hook-guard-canon-readonly.py` false-positives when it denies a
bare `git commit` — one carrying no `git -C <dir>` and no leading `cd <dir> &&` — issued from a
session whose tracked shell cwd points at the canonical checkout while the work lives in a linked
worktree. Its *Proposed* section assumes the deny is wrong and asks for the target tree to be
resolved "against the staged paths".

The claim had never been reproduced. This document records the reproduction, what it showed, and
which of the two candidate fixes was therefore applied.

## Procedure

Two independent questions had to be settled, because the reported symptom needs both to be true
before it is a false positive: the commit must actually target the worktree (a git question), and
the cwd the hook is handed must actually be the cwd the command runs in (a harness question).

**A — git semantics, hermetic and repeatable.** The script below builds a fixture under
`/tmp/repro-44/fixture`: a primary checkout `core` on `main` with one seed commit, plus a linked
worktree `wt`. It touches no real checkout and is safe to re-run — it `rm -rf`s only its own fixture
path. Its assertions are also pinned as a test — `test_bare_commit_from_canon_cwd_cannot_reach_worktree_index`
in `scripts/tests/test_hook_guard_canon_readonly.py` — so the premise this verdict rests on breaks
loudly if git's worktree-index behaviour ever changes.

```bash
set -u
export GIT_AUTHOR_NAME=Test GIT_AUTHOR_EMAIL=t@example.com
export GIT_COMMITTER_NAME=Test GIT_COMMITTER_EMAIL=t@example.com
FIX=/tmp/repro-44/fixture; rm -rf "$FIX"; mkdir -p "$FIX"
CORE="$FIX/core"; WT="$FIX/wt"
git init --quiet -b main "$CORE"
printf 'seed\n' > "$CORE/f.txt"
git -C "$CORE" add -A && git -C "$CORE" commit --quiet -m seed
git -C "$CORE" worktree add --quiet -b wt-branch "$WT" main

# A1: stage a change in the LINKED WORKTREE only, then compare the two indexes.
printf 'worktree edit\n' > "$WT/f.txt"; git -C "$WT" add f.txt
git -C "$WT" diff --cached --name-only
( cd "$CORE" && git diff --cached --name-only )
( cd "$CORE" && git status --porcelain )
( cd "$CORE" && echo "cwd=canon  -> $(git rev-parse --git-path index)" )
( cd "$WT"   && echo "cwd=wt     -> $(git rev-parse --git-path index)" )

# A2: bare `git commit` with process cwd = canon, worktree staged, canon index empty.
( cd "$CORE" && git commit -m 'bare commit from canon cwd'; echo "exit=$?" )
git -C "$CORE" log --oneline -1
git -C "$WT" diff --cached --name-only

# A3: the same, with something ALSO staged in canon.
printf 'canon edit\n' > "$CORE/g.txt"; ( cd "$CORE" && git add g.txt )
( cd "$CORE" && git commit -q -m 'bare commit from canon cwd, canon index non-empty'; echo "exit=$?" )
git -C "$CORE" log --oneline -1
git -C "$CORE" show --stat --oneline HEAD | sed -n '1,5p'
git -C "$WT" log --oneline -1
git -C "$WT" diff --cached --name-only

# A4: the signal issue #44's "Proposed" section wants to key on.
( cd "$CORE" && echo "show-toplevel   = $(git rev-parse --show-toplevel)" )
( cd "$CORE" && echo "git-dir         = $(git rev-parse --absolute-git-dir)" )
( cd "$CORE" && echo "staged paths    = [$(git diff --cached --name-only | tr '\n' ' ')]" )
( cd "$WT" && echo "show-toplevel   = $(git rev-parse --show-toplevel)" )
( cd "$WT" && echo "git-dir         = $(git rev-parse --absolute-git-dir)" )
```

**B — harness semantics, observed in-session.** Issue one Bash tool call containing `cd /tmp/repro-44 && pwd`,
then a second, separate Bash tool call containing only `pwd`. Compare the two, and read the
harness's own trailing notice on the first call.

## Observation

**A1 — a linked worktree has its own index, and canon cannot see it.**

```
--- worktree index (git -C $WT diff --cached --name-only):
f.txt
--- canon index seen from cwd=canon (cd $CORE; git diff --cached --name-only):
--- canon status seen from cwd=canon:
--- index file each cwd resolves to:
cwd=canon  -> .git/index
cwd=wt     -> /tmp/repro-44/fixture/core/.git/worktrees/wt/index
```

**A2 — a bare commit from cwd=canon does not commit the worktree's staged content.**

```
On branch main
nothing to commit, working tree clean
exit=1
--- did canon HEAD move? (should still be the seed commit)
26c3fc0 seed
--- is the worktree's staged change still uncommitted?
f.txt
```

**A3 — with canon's own index non-empty, the same command commits into canon.**

```
exit=0
--- canon HEAD after:
5492bb5 bare commit from canon cwd, canon index non-empty
--- files in that commit:
 g.txt | 1 +
 1 file changed, 1 insertion(+)
--- worktree HEAD after (unchanged, and its staged change still pending):
26c3fc0 seed
f.txt
```

**A4 — the signal the *Proposed* section wants to use points at canon, and the staged set is empty.**

```
--- from cwd=canon, what does git report as the target tree?
show-toplevel   = /tmp/repro-44/fixture/core
git-dir         = /tmp/repro-44/fixture/core/.git
staged paths    = []
--- for comparison, from cwd=worktree:
show-toplevel   = /tmp/repro-44/fixture/wt
git-dir         = /tmp/repro-44/fixture/core/.git/worktrees/wt
```

**B — a `cd` does not survive a tool call; the harness pins every call to the session directory.**
The first call returned `/tmp/repro-44` followed by the harness's own notice
`Shell cwd was reset to /home/the0/cai-wt-harm-fixes`; the second call, containing only `pwd`,
returned `/home/the0/cai-wt-harm-fixes`. The value the hook reads from the payload's `cwd` field and
the value the shell starts in are the same session directory, so they cannot diverge.

## Verdict

**The deny is correct.** It is not a false positive, and the case is not a detection gap.

A linked worktree keeps its index at `<git-common-dir>/worktrees/<name>/index`, not in the primary
checkout's `.git/index` (A1). A `git commit` resolves its index from the process cwd, so a bare
commit issued with the process cwd at the canonical checkout reads canon's index and writes canon's
HEAD. It has exactly two outcomes, both observed: it fails with `nothing to commit` when canon's
index is empty (A2), or it creates a commit in the canonical checkout when it is not (A3). At no
point can it commit the linked worktree's staged content — that content is invisible to it.

By B, the condition issue #44 describes is realizable exactly as it says, and for the reason it
says: the session directory *is* the process directory for every Bash call, so a session sitting in
canon runs its commands in canon. But that makes the guard's input correct rather than stale, and
therefore makes the deny correct too. The state the issue's *Acceptance* section requires — "process
cwd reset to the canonical checkout, but whose staged paths live in a linked worktree" — is not
reachable: from cwd=canon the staged path set is canon's, and it was empty (A4).

The workaround the two reported developers used — re-issuing the commit through
`subprocess.run(..., cwd=<worktree>)` — was therefore not a workaround at all. It was the correct
command, and the bare form it replaced would have committed nothing or committed into canon.

## Consequence for issue #44

**The *Proposed* section falls.** It asks to "resolve the actual repo/worktree of the commit's
target rather than trusting `payload_cwd`", suggesting `git rev-parse --show-toplevel` against the
staged paths. A4 shows that from cwd=canon this returns the canonical checkout and an empty staged
set: there is no signal pointing at a worktree, because the commit genuinely has no worktree target.
Implementing that proposal would not read a signal the guard is ignoring — it would have to
*invent* one, and every invention that turns this deny into an allow permits a real commit into
canon. In a guard, "fixing" a correct deny is widening it. Its *Acceptance* criterion falls with it,
its first clause describing an unreachable state.

The residual is therefore **ergonomic, not a detection defect**, and the defect is in the message.
The old deny explained that canon is read-only and pointed at `session-isolate.sh`, which is advice
for a session that has not isolated yet — useless to a developer who already has a worktree and
simply needs to address it. It never said that a bare commit here targets canon's own index, and it
never named the command that works. That is what cost two developers their debugging attention, and
it is what was fixed: the commit-path deny now states the cause and names
`git -C <worktree> commit ...`, listing the repository's live linked worktrees when it can resolve
them. `effective_git_cwd`'s conservative contract is unchanged — no detection was widened, and the
set of commands the guard permits is byte-for-byte what it was.

The matching norm is recorded in the developer specialization's commit-cadence guidance: a commit
issued from a session whose directory may be the canonical checkout must carry `git -C <worktree>`.
